"""
长时间运行代理系统 - TaskOrchestrator 模块

负责任务编排：
- 调用 Claude Code 分析并直接修改 tasks.json
- 审视修改（用 git diff）
- 校验通过后 commit
"""

import subprocess
import json
import os
from dataclasses import dataclass
from typing import Optional

from config import CLAUDE_CMD, ORCHESTRATOR_PROMPT, ORCHESTRATOR_REVIEW_PROMPT, truncate_for_display


@dataclass
class OrchestratorResult:
    """编排结果"""
    success: bool
    message: str = ""
    cost_usd: float = 0.0  # Claude 调用总成本


class TaskOrchestrator:
    """任务编排器 - 调用 Claude Code 修改 tasks.json"""

    def __init__(self, workspace_dir: str, verbose: bool = True):
        self.workspace_dir = workspace_dir
        self.tasks_file = os.path.join(workspace_dir, "tasks.json")
        self.verbose = verbose
        self.max_orchestration_attempts = 3  # 编排重试次数
        self.max_review_attempts = 3          # 审视重试次数

    def orchestrate(self, trigger: str, context: str = "") -> OrchestratorResult:
        """
        触发任务编排

        Args:
            trigger: 触发原因（如 "任务 001 失败 3 次"）
            context: 额外上下文（如错误信息）

        Returns:
            OrchestratorResult
        """
        total_cost = 0.0

        if self.verbose:
            print(f"\n{'─' * 50}")
            print(f"🎭 TaskOrchestrator 启动")
            print(f"   触发原因: {trigger}")
            print(f"{'─' * 50}")

        # 1. 备份当前 tasks.json（只在开始时备份一次，防止重试时覆盖）
        backup = self._backup_tasks()

        # 2. 调用 Claude Code 进行编排（带重试）
        orchestration_result = None
        last_failure_reason = None

        for attempt in range(self.max_orchestration_attempts):
            if self.verbose:
                if attempt == 0:
                    print("   📝 调用 Claude Code 编排任务...")
                else:
                    print(f"   ⚠️  重试编排 ({attempt + 1}/{self.max_orchestration_attempts})...")

            # 构建 prompt，如果有上次失败原因则追加
            prompt = ORCHESTRATOR_PROMPT.format(
                trigger_reason=trigger,
                context=context if context else "无"
            )
            if last_failure_reason:
                prompt += f"\n\n⚠️ 上次编排未完成：{last_failure_reason}\n请确保完成后输出 ORCHESTRATION_DONE"

            result, cost = self._call_claude(prompt)
            total_cost += cost

            if result and "ORCHESTRATION_DONE" in result:
                orchestration_result = result
                break
            else:
                last_failure_reason = "输出中未包含 ORCHESTRATION_DONE 标记"
                if self.verbose:
                    print(f"   ⚠️  编排未完成：{last_failure_reason}")

        if not orchestration_result:
            self._restore_backup(backup)
            return OrchestratorResult(False, f"编排 {self.max_orchestration_attempts} 次均未完成", total_cost)

        if self.verbose:
            print("   ✅ 编排完成，开始审视...")

        # 3. 审视修改（最多尝试 max_review_attempts 次）
        for attempt in range(self.max_review_attempts):
            review_result, cost = self._call_claude(ORCHESTRATOR_REVIEW_PROMPT)
            total_cost += cost

            if review_result and "REVIEW_PASSED" in review_result:
                if self.verbose:
                    print("   ✅ 审视通过")
                break

            if self.verbose:
                print(f"   ⚠️  审视未通过，尝试修复 ({attempt + 1}/{self.max_review_attempts})")
        else:
            # 审视多次失败，回退
            if self.verbose:
                print(f"   ❌ 审视失败，回退更改")
            self._restore_backup(backup)
            return OrchestratorResult(False, "审视多次失败", total_cost)

        # 4. 校验 JSON 格式
        if not self._validate_tasks():
            if self.verbose:
                print("   ❌ JSON 校验失败，回退")
            self._restore_backup(backup)
            return OrchestratorResult(False, "JSON 格式无效", total_cost)

        # 5. Git commit（只提交 tasks.json）
        commit_success = self._commit_tasks(trigger)
        if commit_success:
            if self.verbose:
                print("   ✅ 已提交任务调整")
            return OrchestratorResult(True, "任务编排完成", total_cost)
        else:
            return OrchestratorResult(True, "任务已调整（无需提交）", total_cost)

    def _backup_tasks(self) -> Optional[str]:
        """备份 tasks.json 内容"""
        if os.path.exists(self.tasks_file):
            with open(self.tasks_file, "r", encoding="utf-8") as f:
                return f.read()
        return None

    def _restore_backup(self, backup: Optional[str]):
        """恢复 tasks.json"""
        if backup is None:
            return
        with open(self.tasks_file, "w", encoding="utf-8") as f:
            f.write(backup)
        # 撤销 git 中的更改
        subprocess.run(
            ["git", "checkout", "tasks.json"],
            cwd=self.workspace_dir,
            capture_output=True
        )

    def _call_claude(self, prompt: str) -> tuple:
        """调用 Claude Code（流式输出，无超时）

        Returns:
            (result, cost_usd) 元组
        """
        cost = 0.0
        try:
            process = subprocess.Popen(
                [
                    CLAUDE_CMD,
                    "-p",
                    "--output-format", "stream-json",
                    "--dangerously-skip-permissions",
                    prompt,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=self.workspace_dir,
            )

            full_result = ""
            for line in process.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                    evt_type = event.get("type", "")

                    if evt_type == "assistant" and self.verbose:
                        # 显示思考过程
                        content = event.get("message", {}).get("content", [])
                        for block in content:
                            if block.get("type") == "text":
                                text = block.get("text", "")
                                preview = truncate_for_display(text)
                                if preview:
                                    print(f"      💭 {preview}")

                    elif evt_type == "content_block_start" and self.verbose:
                        # 工具调用开始
                        cb = event.get("content_block", {})
                        if cb.get("type") == "tool_use":
                            tool_name = cb.get("name", "")
                            print(f"      🔧 {tool_name}")

                    elif evt_type == "result":
                        full_result = event.get("result", "")
                        cost = event.get("total_cost_usd", 0)
                        if self.verbose:
                            print(f"      💰 成本: ${cost:.4f}")

                except json.JSONDecodeError:
                    continue

            process.wait()

            if process.returncode != 0:
                stderr = process.stderr.read()
                if self.verbose:
                    print(f"   ⚠️  Claude 调用失败: {stderr[:100]}")
                return None, cost

            return full_result, cost

        except Exception as e:
            if self.verbose:
                print(f"   ⚠️  Claude 调用异常: {e}")
            return None, cost

    def _validate_tasks(self) -> bool:
        """校验 tasks.json 格式"""
        try:
            with open(self.tasks_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            if not isinstance(data, list):
                return False

            # 检查 ID 唯一性
            ids = [t.get("id") for t in data if "id" in t]
            if len(ids) != len(set(ids)):
                if self.verbose:
                    print("   ⚠️  存在重复的任务 ID")
                return False

            # 检查必填字段
            for task in data:
                if "id" not in task or "description" not in task:
                    if self.verbose:
                        print("   ⚠️  任务缺少必填字段")
                    return False

            return True

        except json.JSONDecodeError as e:
            if self.verbose:
                print(f"   ⚠️  JSON 解析失败: {e}")
            return False
        except Exception as e:
            if self.verbose:
                print(f"   ⚠️  校验异常: {e}")
            return False

    def _commit_tasks(self, trigger: str) -> bool:
        """提交 tasks.json 更改"""
        # 检查是否有更改
        result = subprocess.run(
            ["git", "diff", "--quiet", "tasks.json"],
            cwd=self.workspace_dir,
            capture_output=True
        )
        if result.returncode == 0:
            # 没有更改
            return False

        # 添加并提交
        subprocess.run(
            ["git", "add", "tasks.json"],
            cwd=self.workspace_dir,
            capture_output=True
        )

        commit_msg = f"chore(orchestrator): {trigger}"
        result = subprocess.run(
            ["git", "commit", "-m", commit_msg],
            cwd=self.workspace_dir,
            capture_output=True
        )

        return result.returncode == 0
