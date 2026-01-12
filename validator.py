"""
长时间运行代理系统 - Post-work 验证模块

负责在 Worker 执行完毕后：
- 运行语法检查
- 运行测试（如果有）
- 验证通过则生成 commit
- 验证失败则更新 task.notes
"""

import os
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

        # 2. 运行语法检查（Python 文件）
        python_files = [f for f in changed_files if f.endswith('.py')]
        if python_files:
            syntax_ok, syntax_errors = self._run_syntax_check(python_files)
            if not syntax_ok:
                error_msg = f"语法错误: {'; '.join(syntax_errors)}"
                print(f"   ❌ {error_msg}")
                self._update_task_notes(task, error_msg)
                return ValidationResult(success=False, errors=syntax_errors)
            print("   ✅ 语法检查通过")

        # 3. 运行测试（如果有 pytest）
        test_ok, test_errors = self._run_tests()
        if not test_ok:
            error_msg = f"测试失败: {'; '.join(test_errors)}"
            print(f"   ❌ {error_msg}")
            self._update_task_notes(task, error_msg)
            return ValidationResult(success=False, errors=test_errors)

        # 4. 调用 Claude 生成 commit 信息并提交
        commit_msg = self._generate_and_commit(task)
        if commit_msg:
            # 清除 notes（任务成功完成）
            self.task_manager.clear_notes(task.id)
            return ValidationResult(success=True, commit_message=commit_msg)
        else:
            # commit 生成失败，也算验证失败
            error_msg = "无法生成 commit 信息"
            self._update_task_notes(task, error_msg)
            return ValidationResult(success=False, errors=[error_msg])

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

    def _run_syntax_check(self, python_files: List[str]) -> tuple:
        """运行 Python 语法检查"""
        errors = []
        for file in python_files:
            file_path = os.path.join(self.workspace_dir, file)
            if not os.path.exists(file_path):
                continue
            
            result = subprocess.run(
                ["python", "-m", "py_compile", file_path],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                error_msg = result.stderr.strip() or f"{file} 语法错误"
                errors.append(error_msg[:100])  # 截断错误信息
        
        return (len(errors) == 0, errors)

    def _run_tests(self) -> tuple:
        """运行测试（如果有 pytest）"""
        # 检查是否有 pytest
        pytest_check = subprocess.run(
            ["which", "pytest"],
            capture_output=True,
        )
        
        # 检查是否有测试文件
        test_files = []
        for root, dirs, files in os.walk(self.workspace_dir):
            # 跳过隐藏目录和虚拟环境
            dirs[:] = [d for d in dirs if not d.startswith('.') and d not in ('venv', 'node_modules', '__pycache__')]
            for f in files:
                if f.startswith('test_') and f.endswith('.py'):
                    test_files.append(os.path.join(root, f))
        
        if pytest_check.returncode != 0 or not test_files:
            # 没有 pytest 或没有测试文件，跳过测试
            return (True, [])
        
        print("   🧪 运行测试...")
        result = subprocess.run(
            ["pytest", "-x", "-q", "--tb=line"],
            cwd=self.workspace_dir,
            capture_output=True,
            text=True,
            timeout=60,  # 最多 60 秒
        )
        
        if result.returncode != 0:
            # 提取失败信息
            error_lines = result.stdout.strip().split('\n')[-5:]  # 最后 5 行
            return (False, error_lines)
        
        print("   ✅ 测试通过")
        return (True, [])

    def _generate_and_commit(self, task) -> Optional[str]:
        """使用 Claude 生成 commit 信息并提交"""
        # 构建提示 - 精简版，让 Claude 自己用 git diff
        prompt = POST_WORK_PROMPT.format(
            task_id=task.id,
            task_description=task.description,
        )

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
                timeout=60,
                cwd=self.workspace_dir,
            )

            if result.returncode != 0:
                print(f"   ⚠️  Claude 调用失败")
                return self._fallback_commit(task)

            output_data = json.loads(result.stdout)
            output_text = output_data.get("result", "")

            # 解析输出
            if "VALIDATION_FAILED" in output_text:
                # Claude 认为验证失败
                reason = output_text.split("VALIDATION_FAILED:")[-1].strip()[:100]
                self._update_task_notes(task, f"验证失败: {reason}")
                return None

            if "COMMIT_MESSAGE_START" in output_text and "COMMIT_MESSAGE_END" in output_text:
                start = output_text.find("COMMIT_MESSAGE_START") + len("COMMIT_MESSAGE_START")
                end = output_text.find("COMMIT_MESSAGE_END")
                commit_msg = output_text[start:end].strip()
            else:
                # 使用默认 commit 信息
                commit_msg = f"Task [{task.id}]: {task.description}"

            # 执行 git commit
            return self._do_commit(commit_msg)

        except subprocess.TimeoutExpired:
            print("   ⚠️  生成超时，使用默认 commit 信息")
            return self._fallback_commit(task)
        except Exception as e:
            print(f"   ⚠️  生成失败: {e}")
            return self._fallback_commit(task)

    def _fallback_commit(self, task) -> Optional[str]:
        """使用默认格式生成 commit"""
        commit_msg = f"Task [{task.id}]: {task.description}"
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
