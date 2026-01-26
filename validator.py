"""
长时间运行代理系统 - Post-work 验证模块

负责在 Worker 执行完毕后：
- 调用 Claude 自主进行验证和提交
- 解析结果判断是否成功
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
    cost_usd: float = 0.0


class PostWorkValidator:
    """Post-work 阶段验证器"""

    def __init__(self, workspace_dir: str, task_manager):
        self.workspace_dir = workspace_dir
        self.task_manager = task_manager

    def validate_and_commit(self, task) -> ValidationResult:
        """让 Claude 自主验证并提交"""

        # 检查是否有变更
        changed_files = self._get_changed_files()
        if not changed_files:
            print("   📋 无代码变更，跳过验证")
            return ValidationResult(success=True)

        print(f"   📋 检测到 {len(changed_files)} 个变更文件")

        # 调用 Claude 自主验证和提交
        return self._run_post_work(task)

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
                parts = line.split()
                if len(parts) >= 2:
                    files.append(parts[-1])
        return files

    def _run_post_work(self, task) -> ValidationResult:
        """调用 Claude 执行 post-work"""
        prompt = POST_WORK_PROMPT.format(
            task_id=task.id,
            task_description=task.description,
        )

        cost_usd = 0.0
        try:
            print("   🔍 执行验证和提交...")
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
                return ValidationResult(success=False, errors=["Claude 调用失败"])

            output_data = json.loads(result.stdout)
            cost_usd = output_data.get("total_cost_usd", 0.0)

            # 通过 git status 判断是否完成
            if not self._get_changed_files():
                print("   ✅ 验证通过，已提交")
                self.task_manager.clear_notes(task.id)
                return ValidationResult(success=True, cost_usd=cost_usd)
            else:
                print("   ⚠️  仍有未提交的改动")
                return ValidationResult(success=False, errors=["仍有未提交的改动"], cost_usd=cost_usd)

        except Exception as e:
            print(f"   ⚠️  执行失败: {e}")
            return ValidationResult(success=False, errors=[str(e)], cost_usd=cost_usd)
