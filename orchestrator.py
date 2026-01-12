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

from config import CLAUDE_CMD


# 编排提示模板
ORCHESTRATOR_PROMPT = """你是任务编排者。需要重新审视和调整任务列表。

## 触发原因
{trigger_reason}

## 额外上下文
{context}

## 你的任务
1. 阅读 CLAUDE.md 了解项目目标
2. 阅读 tasks.json 了解当前任务列表
3. 运行 git log --oneline -10 了解最近进展
4. 根据触发原因，对 tasks.json 进行必要的调整：
   - 可以增加新任务（新发现的问题等）
   - 可以修改现有任务的描述/步骤/优先级
   - 可以删除不再需要的 pending 任务
5. 直接编辑 tasks.json 文件

## 约束
- 任务粒度适中（单任务 10-15 分钟内可完成）
- 保持 id 唯一
- 不要修改 status=completed 的任务
- 不要删除 status=in_progress 的任务

完成后输出 ORCHESTRATION_DONE
"""

ORCHESTRATOR_REVIEW_PROMPT = """请审视你刚才对任务列表的修改。

1. 运行 git diff tasks.json 查看改动
2. 检查：
   - JSON 格式是否正确
   - ID 是否唯一
   - 是否意外删除了进行中的任务
   - 修改是否符合项目目标

如果发现问题，请修复。
如果没有问题，输出 REVIEW_PASSED
"""


@dataclass
class OrchestratorResult:
    """编排结果"""
    success: bool
    message: str = ""


class TaskOrchestrator:
    """任务编排器 - 调用 Claude Code 修改 tasks.json"""

    def __init__(self, workspace_dir: str, verbose: bool = True):
        self.workspace_dir = workspace_dir
        self.tasks_file = os.path.join(workspace_dir, "tasks.json")
        self.verbose = verbose
        self.max_review_attempts = 3

    def orchestrate(self, trigger: str, context: str = "") -> OrchestratorResult:
        """
        触发任务编排

        Args:
            trigger: 触发原因（如 "任务 001 失败 3 次"）
            context: 额外上下文（如错误信息）

        Returns:
            OrchestratorResult
        """
        if self.verbose:
            print(f"\n{'─' * 50}")
            print(f"🎭 TaskOrchestrator 启动")
            print(f"   触发原因: {trigger}")
            print(f"{'─' * 50}")

        # 1. 备份当前 tasks.json（用于回退）
        backup = self._backup_tasks()

        # 2. 调用 Claude Code 进行编排
        if self.verbose:
            print("   📝 调用 Claude Code 编排任务...")

        prompt = ORCHESTRATOR_PROMPT.format(
            trigger_reason=trigger,
            context=context if context else "无"
        )

        orchestration_result = self._call_claude(prompt)
        if not orchestration_result or "ORCHESTRATION_DONE" not in orchestration_result:
            self._restore_backup(backup)
            return OrchestratorResult(False, "编排未完成")

        if self.verbose:
            print("   ✅ 编排完成，开始审视...")

        # 3. 审视修改（最多尝试 max_review_attempts 次）
        for attempt in range(self.max_review_attempts):
            review_result = self._call_claude(ORCHESTRATOR_REVIEW_PROMPT)

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
            return OrchestratorResult(False, "审视多次失败")

        # 4. 校验 JSON 格式
        if not self._validate_tasks():
            if self.verbose:
                print("   ❌ JSON 校验失败，回退")
            self._restore_backup(backup)
            return OrchestratorResult(False, "JSON 格式无效")

        # 5. Git commit（只提交 tasks.json）
        commit_success = self._commit_tasks(trigger)
        if commit_success:
            if self.verbose:
                print("   ✅ 已提交任务调整")
            return OrchestratorResult(True, "任务编排完成")
        else:
            return OrchestratorResult(True, "任务已调整（无需提交）")

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

    def _call_claude(self, prompt: str, timeout: int = 120) -> Optional[str]:
        """调用 Claude Code"""
        try:
            result = subprocess.run(
                [
                    CLAUDE_CMD,
                    "-p",
                    "--output-format", "json",
                    "--dangerously-skip-permissions",
                    prompt,
                ],
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=self.workspace_dir,
            )

            if result.returncode != 0:
                if self.verbose:
                    print(f"   ⚠️  Claude 调用失败: {result.stderr[:100]}")
                return None

            output_data = json.loads(result.stdout)
            return output_data.get("result", "")

        except subprocess.TimeoutExpired:
            if self.verbose:
                print("   ⚠️  Claude 调用超时")
            return None
        except Exception as e:
            if self.verbose:
                print(f"   ⚠️  Claude 调用异常: {e}")
            return None

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

        commit_msg = f"TaskOrchestrator: {trigger[:50]}"
        result = subprocess.run(
            ["git", "commit", "-m", commit_msg],
            cwd=self.workspace_dir,
            capture_output=True
        )

        return result.returncode == 0
