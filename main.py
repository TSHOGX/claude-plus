#!/usr/bin/env python3
"""
长时间运行代理系统 - 主编排器

基于 Anthropic 博客 "Effective Harnesses for Long-Running Agents" 的思路实现。
核心思想：
1. 每个会话只处理一个任务
2. 使用 JSON 文件管理任务状态
3. 使用进度日志记录历史
4. 使用 Git 追踪代码变更
"""

import os
import sys
import argparse
import subprocess
from datetime import datetime

from config import (
    DEFAULT_WORKSPACE_DIR, MAX_RETRIES, TASK_REFINEMENT_PROMPT,
    get_paths, is_safe_workspace
)
from task_manager import TaskManager, Task
from progress_log import ProgressLog
from session_runner import SessionRunner, SessionResult


class LongRunningAgent:
    """长时间运行代理编排器"""

    def __init__(self, workspace_dir: str = None, verbose: bool = True):
        # 解析 workspace 路径
        self.paths = get_paths(workspace_dir)
        self.workspace_dir = self.paths["workspace"]
        self.tasks_file = self.paths["tasks_file"]
        self.progress_file = self.paths["progress_file"]
        self.init_script = self.paths["init_script"]
        self.verbose = verbose

        # 初始化组件（使用动态路径）
        self.task_manager = TaskManager(self.tasks_file)
        self.progress_log = ProgressLog(self.progress_file)
        self.session_runner = SessionRunner(self.workspace_dir, verbose=verbose)
        self.total_cost = 0.0

    def initialize(self):
        """初始化工作环境"""
        print("=" * 60)
        print("🚀 初始化长时间运行代理系统")
        print("=" * 60)

        # 1. 创建工作目录
        os.makedirs(self.workspace_dir, exist_ok=True)
        print(f"✓ 工作目录: {self.workspace_dir}")

        # 2. 初始化 Git 并保护现有代码
        is_new_repo = not os.path.exists(os.path.join(self.workspace_dir, ".git"))
        if is_new_repo:
            subprocess.run(
                ["git", "init"],
                cwd=self.workspace_dir,
                capture_output=True
            )
            print("✓ Git 仓库已初始化")

            # 提交现有文件（保护原有代码）
            existing_files = self._count_files()
            if existing_files > 0:
                self._git_commit(f"初始快照: 保护现有 {existing_files} 个文件")
                print(f"✓ 已提交现有 {existing_files} 个文件作为初始快照")
        else:
            print("✓ Git 仓库已存在")
            # 检查是否有未提交的更改
            if self._has_uncommitted_changes():
                print("⚠️  检测到未提交的更改，建议先手动提交")

        # 3. 创建初始化脚本
        self._create_init_script()
        print(f"✓ 初始化脚本: {self.init_script}")

        # 4. 检查任务文件（不自动创建）
        if not os.path.exists(self.tasks_file):
            print(f"\n⚠️  任务文件不存在: {self.tasks_file}")
            print("\n请创建 tasks.json 文件，格式如下：")
            print('''
[
  {
    "id": "001",
    "description": "任务描述",
    "priority": 1,
    "steps": ["步骤1", "步骤2"]
  }
]
''')
            return False
        else:
            print(f"✓ 任务文件: {self.tasks_file}")

        # 5. 初始化进度日志
        print(f"✓ 进度日志: {self.progress_log.progress_file}")

        # 6. 提交初始化脚本等配置文件
        if self._has_uncommitted_changes():
            self._git_commit("添加任务管理配置文件")
            print("✓ 配置文件已提交")

        print("\n初始化完成！")
        self._print_stats()
        return True

    def _count_files(self) -> int:
        """统计 workspace 中的文件数量（不包括隐藏文件）"""
        count = 0
        for root, dirs, files in os.walk(self.workspace_dir):
            # 跳过隐藏目录
            dirs[:] = [d for d in dirs if not d.startswith('.')]
            count += len([f for f in files if not f.startswith('.')])
        return count

    def _has_uncommitted_changes(self) -> bool:
        """检查是否有未提交的更改"""
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=self.workspace_dir,
            capture_output=True,
            text=True
        )
        return bool(result.stdout.strip())

    def _create_init_script(self):
        """创建初始化脚本"""
        script_content = """#!/bin/bash
# 初始化脚本 - 每次会话开始时运行

echo "=== 环境初始化 ==="

# 确认工作目录
echo "工作目录: $(pwd)"

# 显示 Git 状态
echo ""
echo "=== Git 状态 ==="
git status --short

# 显示最近的提交
echo ""
echo "=== 最近提交 ==="
git log --oneline -5 2>/dev/null || echo "暂无提交"

echo ""
echo "=== 初始化完成 ==="
"""
        with open(self.init_script, 'w') as f:
            f.write(script_content)
        os.chmod(self.init_script, 0o755)

    def _git_commit(self, message: str):
        """执行 Git 提交"""
        try:
            # 添加所有更改
            subprocess.run(
                ["git", "add", "-A"],
                cwd=self.workspace_dir,
                capture_output=True
            )
            # 提交
            result = subprocess.run(
                ["git", "commit", "-m", message, "--allow-empty"],
                cwd=self.workspace_dir,
                capture_output=True,
                text=True
            )
            return result.returncode == 0
        except Exception as e:
            print(f"Git 提交失败: {e}")
            return False

    def _print_stats(self):
        """打印任务统计"""
        stats = self.task_manager.get_stats()
        print("\n📊 任务统计:")
        print(f"  总计: {stats['total']}")
        print(f"  待处理: {stats['pending']}")
        print(f"  进行中: {stats['in_progress']}")
        print(f"  已完成: {stats['completed']}")
        print(f"  失败: {stats['failed']}")

    def _get_last_good_commit(self) -> str:
        """获取最后一个成功的 commit hash"""
        result = subprocess.run(
            ["git", "log", "--oneline", "-1", "--format=%H"],
            cwd=self.workspace_dir,
            capture_output=True,
            text=True
        )
        return result.stdout.strip() if result.returncode == 0 else None

    def _git_reset_to(self, commit_hash: str) -> bool:
        """回退到指定的 commit"""
        try:
            result = subprocess.run(
                ["git", "reset", "--hard", commit_hash],
                cwd=self.workspace_dir,
                capture_output=True,
                text=True
            )
            return result.returncode == 0
        except Exception as e:
            print(f"Git 回退失败: {e}")
            return False

    def _refine_timeout_task(self, task: Task) -> bool:
        """细化超时任务：拆分为更小的子任务"""
        import json as json_module
        from config import CLAUDE_CMD

        print(f"\n🔧 任务 [{task.id}] 超时，正在细化任务...")

        # 构建细化提示
        prompt = TASK_REFINEMENT_PROMPT.format(
            task_id=task.id,
            description=task.description,
            steps="\n".join(f"- {s}" for s in task.steps)
        )

        # 调用 Claude 细化任务
        try:
            result = subprocess.run(
                [CLAUDE_CMD, "-p", "--output-format", "json", "--dangerously-skip-permissions", prompt],
                capture_output=True,
                text=True,
                timeout=120,
                cwd=self.workspace_dir
            )

            output_data = json_module.loads(result.stdout)
            output_text = output_data.get("result", "")

            # 提取 JSON 部分
            json_start = output_text.find("[")
            json_end = output_text.rfind("]") + 1
            if json_start == -1 or json_end == 0:
                print("   ❌ 无法解析细化结果")
                return False

            new_tasks_data = json_module.loads(output_text[json_start:json_end])

            # 移除原任务，添加新的细化任务
            self.task_manager.tasks = [t for t in self.task_manager.tasks if t.id != task.id]

            for t_data in new_tasks_data:
                new_task = Task(
                    id=t_data.get("id", f"{task.id}_{len(self.task_manager.tasks)}"),
                    description=t_data.get("description", ""),
                    priority=t_data.get("priority", task.priority),
                    steps=t_data.get("steps", []),
                    category=task.category
                )
                self.task_manager.tasks.append(new_task)

            self.task_manager.save_tasks()
            print(f"   ✅ 已将任务拆分为 {len(new_tasks_data)} 个子任务")
            return True

        except Exception as e:
            print(f"   ❌ 细化任务失败: {e}")
            return False

    def _guide_user_for_failure(self, task: Task):
        """指导用户处理非超时失败"""
        print("\n" + "=" * 60)
        print("❌ 任务执行失败，需要人工介入")
        print("=" * 60)

        print(f"\n## 失败任务信息")
        print(f"   ID: {task.id}")
        print(f"   描述: {task.description}")
        print(f"   错误: {task.error_message}")
        print(f"   重试次数: {task.retries}")

        print(f"\n## 建议操作")
        print("   1. 检查错误信息，确认问题原因")
        print("   2. 手动修复问题（代码、配置或环境）")
        print("   3. 修改 tasks.json 调整任务描述或步骤")
        print("   4. 重置任务状态后继续：")
        print(f"      python3 main.py reset-task {task.id}")
        print("   5. 重新运行：")
        print("      python3 main.py run")

        print(f"\n## 相关文件")
        print(f"   任务文件: {self.tasks_file}")
        print(f"   进度日志: {self.progress_log.progress_file}")

        if task.session_id:
            print(f"\n## 调试命令")
            print(f"   恢复会话查看详情：claude -r {task.session_id}")

    def _check_and_handle_max_retries(self, task: Task, last_status: str) -> bool:
        """检查并处理达到最大重试次数的任务

        返回:
            True: 已处理（超时细化成功），继续执行
            False: 需要退出流程（其他失败或细化失败）
        """
        if task.retries < MAX_RETRIES:
            return True  # 未达到最大重试，继续正常流程

        print(f"\n⚠️  任务 [{task.id}] 已达到最大重试次数 ({MAX_RETRIES})")

        if last_status == "timeout":
            # 超时：细化任务并重试
            print("   原因: 任务超时，可能太复杂")

            # 记录当前 commit 用于回退
            last_commit = self._get_last_good_commit()

            # 细化任务
            if self._refine_timeout_task(task):
                # 回退到超时前的状态
                if last_commit and self._git_reset_to(last_commit):
                    print(f"   ✅ 已回退到 commit: {last_commit[:8]}")
                return True  # 继续执行细化后的任务
            else:
                # 细化失败，当作其他失败处理
                self._guide_user_for_failure(task)
                return False
        else:
            # 其他失败：指导用户并退出
            self._guide_user_for_failure(task)
            return False

    def run(self, max_tasks: int = None):
        """运行主循环处理任务"""
        # 检查任务文件是否存在
        if not os.path.exists(self.tasks_file):
            print(f"\n❌ 任务文件不存在: {self.tasks_file}")
            print("   请先创建任务文件或运行 'python3 main.py init'")
            return

        print("\n" + "=" * 60)
        print("🤖 开始处理任务")
        print("=" * 60)
        print("   提示: 按 Ctrl+C 可安全终止并自动回退未完成的更改\n")

        tasks_processed = 0
        should_exit = False
        current_task = None
        commit_before_task = None

        try:
            while not should_exit:
                # 检查是否达到最大任务数
                if max_tasks and tasks_processed >= max_tasks:
                    print(f"\n已达到最大任务数限制: {max_tasks}")
                    break

                # 获取下一个任务（包括可重试的失败任务）
                task = self.task_manager.get_next_task(max_retries=MAX_RETRIES + 1)
                if not task:
                    print("\n✅ 所有任务已完成!")
                    break

                # 检查是否达到最大重试次数（在执行前检查）
                if task.retries >= MAX_RETRIES:
                    # 获取上次失败的状态
                    last_status = "timeout" if "超时" in (task.error_message or "") else "other"
                    if not self._check_and_handle_max_retries(task, last_status):
                        should_exit = True
                        break
                    # 细化成功后，重新获取任务
                    continue

                # 记录任务开始前的 commit（用于中断回退）
                commit_before_task = self._get_last_good_commit()
                current_task = task

                # 显示重试信息
                retry_info = f" (重试 #{task.retries})" if task.retries > 0 else ""

                # 处理任务
                print(f"\n{'─' * 50}")
                print(f"📝 处理任务 [{task.id}]: {task.description}{retry_info}")
                print(f"   优先级: {task.priority}")
                if task.error_message:
                    print(f"   ⚠️  上次失败原因: {task.error_message[:50]}...")
                print(f"{'─' * 50}")

                result = self._process_task(task)
                tasks_processed += 1

                # 任务完成，清除当前任务标记
                current_task = None
                commit_before_task = None

                # 记录成本
                self.total_cost += result.cost_usd
                print(f"   💰 本次成本: ${result.cost_usd:.4f} | 总成本: ${self.total_cost:.4f}")

                # 处理结果
                if result.is_completed():
                    print(f"   ✅ 任务完成!")
                    self.task_manager.mark_completed(task.id)
                    self.progress_log.log_complete(
                        task.id, task.description,
                        result.session_id, result.output
                    )
                    self._git_commit(f"完成任务 [{task.id}]: {task.description}")
                elif result.is_blocked():
                    print(f"   ⏸️ 任务被阻塞: {result.error}")
                    self.task_manager.mark_failed(task.id, result.error)
                    self.progress_log.log_blocked(
                        task.id, task.description,
                        result.session_id, result.error
                    )
                else:
                    # 失败（包括超时）
                    error_msg = result.error or "未知错误"
                    if result.status == "timeout":
                        error_msg = f"超时（{error_msg}）"

                    print(f"   ❌ 任务失败: {error_msg}")
                    self.task_manager.mark_failed(task.id, error_msg)
                    self.progress_log.log_failed(
                        task.id, task.description,
                        result.session_id, error_msg
                    )

                    # 保存 commit hash 用于后续可能的回退
                    task_obj = self.task_manager.get_task_by_id(task.id)
                    if task_obj:
                        task_obj.session_id = result.session_id  # 保留 session 用于调试

        except KeyboardInterrupt:
            print("\n\n" + "=" * 60)
            print("⚠️  检测到 Ctrl+C，正在安全终止...")
            print("=" * 60)

            if current_task and commit_before_task:
                print(f"\n正在回退任务 [{current_task.id}] 的未完成更改...")

                # 回退 Git
                if self._git_reset_to(commit_before_task):
                    print(f"   ✅ 已回退到 commit: {commit_before_task[:8]}")
                else:
                    print(f"   ❌ Git 回退失败，请手动执行: git reset --hard {commit_before_task}")

                # 重置任务状态
                self.task_manager.reset_task(current_task.id)
                print(f"   ✅ 已重置任务 [{current_task.id}] 状态")

            print("\n下次可以继续运行: python3 main.py run")
            return

        # 打印最终统计
        if not should_exit:
            print("\n" + "=" * 60)
            print("📈 运行完成")
            print("=" * 60)
            self._print_stats()
            print(f"\n💰 总成本: ${self.total_cost:.4f}")

    def _process_task(self, task: Task) -> SessionResult:
        """处理单个任务"""
        # 获取最近进度
        recent_progress = self.progress_log.get_recent(3)

        # 检查是否有之前的会话可以恢复（失败重试场景）
        if task.session_id and task.retries > 0:
            print(f"   📎 检测到之前的会话，尝试恢复: {task.session_id[:8]}...")

            # 构建重试提示，包含之前的错误信息
            retry_prompt = f"""请继续完成任务。

## 之前失败的原因
{task.error_message or '未知错误'}

## 任务描述
{task.description}

请修复问题并完成任务。完成后输出 TASK_COMPLETED。
"""
            result = self.session_runner.continue_session(task.session_id, retry_prompt)
        else:
            # 新任务，创建新会话
            result = self.session_runner.run_session(task, recent_progress)

        # 保存 session_id 到任务（用于失败后恢复）
        self.task_manager.mark_in_progress(task.id, result.session_id)

        # 记录开始
        self.progress_log.log_start(task.id, task.description, result.session_id)

        return result

    def status(self):
        """显示当前状态"""
        print("\n" + "=" * 60)
        print("📋 系统状态")
        print("=" * 60)

        self._print_stats()

        print("\n📜 任务列表:")
        for task in self.task_manager.get_all_tasks():
            status_icon = {
                "pending": "⏳",
                "in_progress": "🔄",
                "completed": "✅",
                "failed": "❌"
            }.get(task.status, "❓")
            print(f"  {status_icon} [{task.id}] {task.description}")

        print("\n" + self.progress_log.get_summary())

    def reset(self):
        """重置所有任务状态"""
        for task in self.task_manager.get_all_tasks():
            self.task_manager.reset_task(task.id)
        self.progress_log.clear()
        print("✓ 所有任务已重置")

    def reset_single_task(self, task_id: str):
        """重置单个任务状态"""
        task = self.task_manager.get_task_by_id(task_id)
        if task:
            self.task_manager.reset_task(task_id)
            print(f"✓ 任务 [{task_id}] 已重置")
        else:
            print(f"❌ 未找到任务: {task_id}")


def main():
    parser = argparse.ArgumentParser(
        description="长时间运行代理系统 - 基于 Claude CLI 的增量任务处理器"
    )

    # 全局参数
    parser.add_argument(
        "-w", "--workspace",
        type=str,
        default=None,
        help=f"指定工作目录（默认: ./workspace）"
    )
    parser.add_argument(
        "-q", "--quiet",
        action="store_true",
        help="静默模式，不显示 Claude 执行过程"
    )

    subparsers = parser.add_subparsers(dest="command", help="可用命令")

    # init 命令
    subparsers.add_parser("init", help="初始化工作环境")

    # run 命令
    run_parser = subparsers.add_parser("run", help="运行任务处理")
    run_parser.add_argument(
        "--max-tasks",
        type=int,
        default=None,
        help="最大处理任务数"
    )

    # status 命令
    subparsers.add_parser("status", help="显示当前状态")

    # reset 命令
    subparsers.add_parser("reset", help="重置所有任务状态")

    # reset-task 命令
    reset_task_parser = subparsers.add_parser("reset-task", help="重置单个任务状态")
    reset_task_parser.add_argument("task_id", help="要重置的任务 ID")

    args = parser.parse_args()

    # 安全检查
    if args.workspace:
        is_safe, error_msg = is_safe_workspace(args.workspace)
        if not is_safe:
            print(f"❌ {error_msg}")
            sys.exit(1)

    # 创建 agent
    agent = LongRunningAgent(args.workspace, verbose=not args.quiet)

    if args.command == "init":
        agent.initialize()
    elif args.command == "run":
        agent.run(max_tasks=args.max_tasks)
    elif args.command == "status":
        agent.status()
    elif args.command == "reset":
        agent.reset()
    elif args.command == "reset-task":
        agent.reset_single_task(args.task_id)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
