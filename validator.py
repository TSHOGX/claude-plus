"""
长时间运行代理系统 - Post-work 验证模块

负责在 Worker 执行完毕后：
- 调用 Claude 进行灵活验证（语法、测试等）
- 验证通过则生成 commit
- 验证失败则更新 task.notes
"""

import json
import subprocess
from dataclasses import dataclass, field
from typing import Optional, List

from config import CLAUDE_CMD, POST_WORK_PROMPT


@dataclass
class ValidationResult:
    """验证结果"""
    success: bool
    errors: List[str] = field(default_factory=list)
    commit_message: Optional[str] = None
    cost_usd: float = 0.0  # Claude 调用成本


class PostWorkValidator:
    """Post-work 阶段验证器"""

    def __init__(self, workspace_dir: str, task_manager):
        self.workspace_dir = workspace_dir
        self.task_manager = task_manager

    def validate_and_commit(self, task) -> ValidationResult:
        """验证变更，通过则 commit，失败则更新 notes"""

        # 1. 检查是否有变更
        changed_files = self._get_changed_files()
        if not changed_files:
            print("   📋 无代码变更，跳过验证")
            return ValidationResult(success=True)

        print(f"   📋 检测到 {len(changed_files)} 个变更文件")

        # 2. 调用 Claude 进行灵活验证并生成 commit 信息
        commit_msg, cost_usd = self._generate_and_commit(task)
        if commit_msg:
            # 清除 notes（任务成功完成）
            self.task_manager.clear_notes(task.id)
            return ValidationResult(success=True, commit_message=commit_msg, cost_usd=cost_usd)
        else:
            # 验证失败
            error_msg = "验证未通过"
            self._update_task_notes(task, error_msg)
            return ValidationResult(success=False, errors=[error_msg], cost_usd=cost_usd)

    def _get_changed_files(self) -> List[str]:
        """获取变更的文件列表"""
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=self.workspace_dir,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return []
        
        files = []
        for line in result.stdout.strip().split('\n'):
            if line.strip():
                # 格式: XY filename 或 XY -> oldname -> newname
                parts = line.split()
                if len(parts) >= 2:
                    files.append(parts[-1])
        return files

    def _generate_and_commit(self, task) -> tuple:
        """使用 Claude 生成 commit 信息并提交

        Returns:
            (commit_message, cost_usd) 元组
        """
        # 构建提示 - 精简版，让 Claude 自己用 git diff
        prompt = POST_WORK_PROMPT.format(
            task_id=task.id,
            task_description=task.description,
        )

        cost_usd = 0.0
        try:
            print("   💬 生成 commit 信息...")
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
                cwd=self.workspace_dir,
            )

            if result.returncode != 0:
                print(f"   ⚠️  Claude 调用失败")
                return self._fallback_commit(task), 0.0

            output_data = json.loads(result.stdout)
            output_text = output_data.get("result", "")
            cost_usd = output_data.get("total_cost_usd", 0.0)

            # 解析输出
            if "VALIDATION_FAILED" in output_text:
                # Claude 认为验证失败
                reason = output_text.split("VALIDATION_FAILED:")[-1].strip()[:100]
                self._update_task_notes(task, f"验证失败: {reason}")
                return None, cost_usd

            if "COMMIT_MESSAGE_START" in output_text and "COMMIT_MESSAGE_END" in output_text:
                start = output_text.find("COMMIT_MESSAGE_START") + len("COMMIT_MESSAGE_START")
                end = output_text.find("COMMIT_MESSAGE_END")
                commit_msg = output_text[start:end].strip()
            else:
                # 使用默认 commit 信息
                commit_msg = task.description

            # 执行 git commit
            return self._do_commit(commit_msg), cost_usd

        except Exception as e:
            print(f"   ⚠️  生成失败: {e}")
            return self._fallback_commit(task), cost_usd

    def _fallback_commit(self, task) -> Optional[str]:
        """使用默认格式生成 commit"""
        commit_msg = task.description
        return self._do_commit(commit_msg)

    def _do_commit(self, commit_msg: str) -> Optional[str]:
        """执行 git add 和 commit"""
        try:
            # git add -A
            subprocess.run(
                ["git", "add", "-A"],
                cwd=self.workspace_dir,
                capture_output=True,
            )

            # git commit
            result = subprocess.run(
                ["git", "commit", "-m", commit_msg],
                cwd=self.workspace_dir,
                capture_output=True,
                text=True,
            )

            if result.returncode == 0:
                print(f"   ✅ 已提交: {commit_msg[:50]}...")
                return commit_msg
            else:
                print(f"   ⚠️  commit 失败: {result.stderr[:50]}")
                return None

        except Exception as e:
            print(f"   ⚠️  commit 异常: {e}")
            return None

    def _update_task_notes(self, task, notes: str):
        """更新任务备注"""
        task.notes = notes
        self.task_manager.save_tasks()
