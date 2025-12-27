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
    DEFAULT_WORKSPACE_DIR,
    MAX_RETRIES,
    CHECK_INTERVAL,
    MAX_TASK_DURATION,
    get_paths,
    is_safe_workspace,
)
from task_manager import TaskManager, Task
from progress_log import ProgressLog
from worker import WorkerProcess
from supervisor import Supervisor, Decision


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
        self.supervisor = Supervisor(self.workspace_dir, verbose=verbose)
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
            subprocess.run(["git", "init"], cwd=self.workspace_dir, capture_output=True)
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
            print(
                """
[
  {
    "id": "001",
    "description": "任务描述",
    "priority": 1,
    "steps": ["步骤1", "步骤2"]
  }
]
"""
            )
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
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            count += len([f for f in files if not f.startswith(".")])
        return count

    def _has_uncommitted_changes(self) -> bool:
        """检查是否有未提交的更改"""
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=self.workspace_dir,
            capture_output=True,
            text=True,
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
        with open(self.init_script, "w") as f:
            f.write(script_content)
        os.chmod(self.init_script, 0o755)

    def _git_commit(self, message: str):
        """执行 Git 提交"""
        try:
            # 添加所有更改
            subprocess.run(
                ["git", "add", "-A"], cwd=self.workspace_dir, capture_output=True
            )
            # 提交
            result = subprocess.run(
                ["git", "commit", "-m", message, "--allow-empty"],
                cwd=self.workspace_dir,
                capture_output=True,
                text=True,
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
            text=True,
        )
        return result.stdout.strip() if result.returncode == 0 else None

    def _git_reset_to(self, commit_hash: str) -> bool:
        """回退到指定的 commit"""
        try:
            result = subprocess.run(
                ["git", "reset", "--hard", commit_hash],
                cwd=self.workspace_dir,
                capture_output=True,
                text=True,
            )
            return result.returncode == 0
        except Exception as e:
            print(f"Git 回退失败: {e}")
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

    def run(self, max_tasks: int = None):
        """运行主循环处理任务（Supervisor-Worker 架构）"""
        import time

        # 检查任务文件是否存在
        if not os.path.exists(self.tasks_file):
            print(f"\n❌ 任务文件不存在: {self.tasks_file}")
            print("   请先创建任务文件或运行 'python3 main.py init'")
            return

        print("\n" + "=" * 60)
        print("🤖 开始处理任务（Supervised 模式）")
        print("=" * 60)
        print(f"   检查间隔: {CHECK_INTERVAL}秒 | 最大时长: {MAX_TASK_DURATION}秒")
        print("   提示: 按 Ctrl+C 可安全终止\n")

        tasks_processed = 0
        current_worker = None
        commit_before_task = None

        try:
            while True:
                # 检查是否达到最大任务数
                if max_tasks and tasks_processed >= max_tasks:
                    print(f"\n已达到最大任务数限制: {max_tasks}")
                    break

                # 获取下一个任务
                task = self.task_manager.get_next_task(max_retries=MAX_RETRIES + 1)
                if not task:
                    print("\n✅ 所有任务已完成!")
                    break

                # 检查重试次数
                if task.retries >= MAX_RETRIES:
                    print(f"\n⚠️  任务 [{task.id}] 已达到最大重试次数")
                    self._guide_user_for_failure(task)
                    break

                # 记录任务开始前的 commit
                commit_before_task = self._get_last_good_commit()

                # 显示任务信息
                retry_info = f" (重试 #{task.retries})" if task.retries > 0 else ""
                print(f"\n{'─' * 50}")
                print(f"📝 处理任务 [{task.id}]: {task.description}{retry_info}")
                print(f"   优先级: {task.priority}")
                print(f"{'─' * 50}")

                # 获取最近进度
                recent_progress = self.progress_log.get_recent(3)

                # 创建并启动 Worker
                worker = WorkerProcess(task, self.workspace_dir, recent_progress)
                current_worker = worker
                pid = worker.start()

                self.task_manager.mark_in_progress(task.id, f"worker_{pid}")
                self.progress_log.log_start(task.id, task.description, f"worker_{pid}")

                print(f"   🚀 Worker 启动: PID {pid}")
                print(f"   📄 日志: {worker.log_file}")

                # 监督循环
                check_count = 0
                decision_made = False

                while worker.is_alive():
                    time.sleep(CHECK_INTERVAL)
                    check_count += 1
                    elapsed = worker.elapsed_seconds()

                    if self.verbose:
                        print(f"   ⏱️  检查 #{check_count}: 已运行 {elapsed:.0f}s")

                    # 检查是否超过最大时长
                    if elapsed > MAX_TASK_DURATION:
                        print(f"   ⚠️  超过最大时长，请求 Supervisor 分析...")
                        sv_result = self.supervisor.analyze(task, worker)
                        self._handle_supervisor_decision(
                            task, worker, sv_result, commit_before_task
                        )
                        decision_made = True
                        break

                    # 快速检查是否有异常模式
                    if self.supervisor.quick_check(worker):
                        print(f"   🔍 检测到异常模式，请求 Supervisor 分析...")
                        sv_result = self.supervisor.analyze(task, worker)
                        if sv_result.decision != Decision.CONTINUE:
                            self._handle_supervisor_decision(
                                task, worker, sv_result, commit_before_task
                            )
                            decision_made = True
                            break
                        else:
                            print(f"   ✅ Supervisor: {sv_result.reason}")

                # Worker 自然结束
                if not decision_made:
                    self._finalize_worker(task, worker, commit_before_task)

                current_worker = None
                commit_before_task = None
                tasks_processed += 1

        except KeyboardInterrupt:
            print("\n\n" + "=" * 60)
            print("⚠️  检测到 Ctrl+C，正在安全终止...")
            print("=" * 60)

            if current_worker and current_worker.is_alive():
                print(f"\n正在终止 Worker...")
                current_worker.terminate(graceful=True)
                print(f"   ✅ Worker 已终止")

            if commit_before_task:
                if self._git_reset_to(commit_before_task):
                    print(f"   ✅ 已回退到 commit: {commit_before_task[:8]}")

            print("\n下次可以继续运行: python3 main.py run")
            return

        # 打印最终统计
        print("\n" + "=" * 60)
        print("📈 运行完成")
        print("=" * 60)
        self._print_stats()
        print(f"\n💰 总成本: ${self.total_cost:.4f}")

    def _handle_supervisor_decision(
        self, task: Task, worker: WorkerProcess, sv_result, commit_before: str
    ):
        """处理 Supervisor 的决策"""
        print(f"   📋 Supervisor 决策: {sv_result.decision.value}")
        print(f"   📋 原因: {sv_result.reason}")

        # 终止 Worker
        if worker.is_alive():
            worker.terminate(graceful=True)

        if sv_result.decision == Decision.SPLIT:
            # 分裂任务
            if sv_result.subtasks:
                if self.task_manager.split_task(task.id, sv_result.subtasks):
                    print(f"   ✅ 已拆分为 {len(sv_result.subtasks)} 个子任务")
                    # 回退代码
                    if commit_before and self._git_reset_to(commit_before):
                        print(f"   ✅ 已回退代码到: {commit_before[:8]}")
                else:
                    self.task_manager.mark_failed(task.id, "任务拆分失败")

        elif sv_result.decision == Decision.WAIT_BACKGROUND:
            # 转为后台运行（不终止，只是标记）
            self.task_manager.mark_background(
                task.id, worker.process.pid if worker.process else 0
            )
            self.progress_log.log_background_start(
                task.id, task.description, worker.process.pid if worker.process else 0
            )
            print(f"   🔄 任务已转为后台运行")
            print(f"   👀 查看进度: tail -f {worker.log_file}")

        elif sv_result.decision == Decision.INTERVENE:
            # 需要人工介入
            self.task_manager.mark_failed(
                task.id, sv_result.suggestion or "需要人工介入"
            )
            self._guide_user_for_failure(task)

    def _finalize_worker(
        self, task: Task, worker: WorkerProcess, commit_before: str = None
    ):
        """处理 Worker 自然结束的情况"""
        _ = commit_before  # 保留参数用于未来扩展
        log = worker.read_log()

        # 记录成本
        self.total_cost += log.cost_usd
        print(f"   💰 成本: ${log.cost_usd:.4f} | 总成本: ${self.total_cost:.4f}")

        if log.is_complete and not log.is_error:
            # 检查是否有完成标记
            if log.result and "TASK_COMPLETED" in log.result:
                print(f"   ✅ 任务完成!")
                self.task_manager.mark_completed(task.id)
                self.progress_log.log_complete(
                    task.id, task.description, log.session_id, log.result or ""
                )
                self._git_commit(f"完成任务 [{task.id}]: {task.description}")
            elif log.result and "TASK_BLOCKED" in log.result:
                error = log.result.split("TASK_BLOCKED:")[-1].strip()[:100]
                print(f"   ⏸️  任务被阻塞: {error}")
                self.task_manager.mark_failed(task.id, error)
                self.progress_log.log_blocked(
                    task.id, task.description, log.session_id, error
                )
            else:
                # 没有明确标记，假设完成
                print(f"   ✅ 任务完成（无明确标记）")
                self.task_manager.mark_completed(task.id)
                self.progress_log.log_complete(
                    task.id, task.description, log.session_id, log.result or ""
                )
                self._git_commit(f"完成任务 [{task.id}]: {task.description}")
        else:
            # 执行失败
            error_msg = log.result[:200] if log.result else "执行失败"
            print(f"   ❌ 任务失败: {error_msg[:50]}...")
            self.task_manager.mark_failed(task.id, error_msg)
            self.progress_log.log_failed(
                task.id, task.description, log.session_id, error_msg
            )

        # 清理 worker 日志（可选）
        # worker.cleanup()

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
                "failed": "❌",
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

    def check_background_tasks(self):
        """检查后台任务状态"""
        bg_tasks = self.task_manager.get_background_tasks()
        if not bg_tasks:
            print("\n没有正在运行的后台任务")
            return

        print("\n" + "=" * 60)
        print("🔄 后台任务状态")
        print("=" * 60)

        for task in bg_tasks:
            pid = task.background_pid
            running = self._is_process_running(pid)
            log_file = os.path.join(self.workspace_dir, f".worker_{task.id}.log")

            status_icon = "🟢" if running else "⚪"
            print(f"\n{status_icon} [{task.id}]: {task.description}")
            print(f"   PID: {pid} ({'运行中' if running else '已结束'})")
            print(f"   日志: {log_file}")

            # 显示日志尾部
            if os.path.exists(log_file):
                print("   最近输出:")
                try:
                    with open(log_file, "r") as f:
                        lines = f.readlines()[-5:]
                        for line in lines:
                            print(f"     {line.rstrip()[:80]}")
                except:
                    pass

            # 如果进程已结束，更新状态
            if not running:
                self._finalize_background_task(task, log_file)

    def _is_process_running(self, pid: int) -> bool:
        """检查进程是否运行中"""
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False

    def _finalize_background_task(self, task: Task, log_file: str):
        """处理已完成的后台任务"""
        import json as json_module

        # 尝试从日志中解析结果（stream-json格式：每行一个JSON事件）
        try:
            with open(log_file, "r") as f:
                content = f.read()
                # 按行倒序查找 type=result 的事件
                for line in reversed(content.strip().split("\n")):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json_module.loads(line)
                        if event.get("type") == "result":
                            if event.get("is_error"):
                                task.status = "failed"
                                task.error_message = event.get("result", "后台执行失败")
                            else:
                                task.status = "completed"
                            task.background_pid = None
                            self.task_manager.save_tasks()
                            print(f"   ✅ 状态已更新为: {task.status}")
                            return
                    except json_module.JSONDecodeError:
                        continue
        except:
            pass

        # 无法解析，标记为需要检查
        task.status = "pending"
        task.background_pid = None
        task.error_message = "后台执行结束，请检查日志确认结果"
        self.task_manager.save_tasks()
        print(f"   ⚠️  无法解析结果，任务已重置为pending")

    def view_task_log(self, task_id: str, lines: int = 50):
        """查看任务日志"""
        log_file = os.path.join(self.workspace_dir, f".worker_{task_id}.log")
        if not os.path.exists(log_file):
            print(f"❌ 日志文件不存在: {log_file}")
            return

        print(f"\n📄 任务 [{task_id}] 日志 (最后 {lines} 行):")
        print("-" * 50)
        with open(log_file, "r") as f:
            all_lines = f.readlines()
            for line in all_lines[-lines:]:
                print(line.rstrip())

    def kill_background_task(self, task_id: str):
        """终止后台任务"""
        task = self.task_manager.get_task_by_id(task_id)
        if not task:
            print(f"❌ 未找到任务: {task_id}")
            return

        if not task.background_pid:
            print(f"❌ 任务 [{task_id}] 不是后台任务")
            return

        pid = task.background_pid
        try:
            os.kill(pid, 9)  # SIGKILL
            print(f"✅ 已终止进程 PID {pid}")
        except ProcessLookupError:
            print(f"⚠️  进程 {pid} 已不存在")
        except Exception as e:
            print(f"❌ 终止进程失败: {e}")

        # 重置任务状态
        task.status = "pending"
        task.background_pid = None
        task.error_message = "后台任务被手动终止"
        self.task_manager.save_tasks()
        print(f"✅ 任务 [{task_id}] 已重置")


def main():
    parser = argparse.ArgumentParser(
        description="长时间运行代理系统 - 基于 Claude CLI 的增量任务处理器"
    )

    # 全局参数
    parser.add_argument(
        "-w",
        "--workspace",
        type=str,
        default=None,
        help=f"指定工作目录（默认: ./workspace）",
    )
    parser.add_argument(
        "-q", "--quiet", action="store_true", help="静默模式，不显示 Claude 执行过程"
    )

    subparsers = parser.add_subparsers(dest="command", help="可用命令")

    # init 命令
    subparsers.add_parser("init", help="初始化工作环境")

    # run 命令
    run_parser = subparsers.add_parser("run", help="运行任务处理")
    run_parser.add_argument(
        "--max-tasks", type=int, default=None, help="最大处理任务数"
    )

    # status 命令
    subparsers.add_parser("status", help="显示当前状态")

    # reset 命令
    subparsers.add_parser("reset", help="重置所有任务状态")

    # reset-task 命令
    reset_task_parser = subparsers.add_parser("reset-task", help="重置单个任务状态")
    reset_task_parser.add_argument("task_id", help="要重置的任务 ID")

    # check-bg 命令
    subparsers.add_parser("check-bg", help="检查后台任务状态")

    # log 命令
    log_parser = subparsers.add_parser("log", help="查看任务日志")
    log_parser.add_argument("task_id", help="任务 ID")
    log_parser.add_argument("-n", "--lines", type=int, default=50, help="显示的行数")

    # kill-bg 命令
    kill_parser = subparsers.add_parser("kill-bg", help="终止后台任务")
    kill_parser.add_argument("task_id", help="任务 ID")

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
    elif args.command == "check-bg":
        agent.check_background_tasks()
    elif args.command == "log":
        agent.view_task_log(args.task_id, args.lines)
    elif args.command == "kill-bg":
        agent.kill_background_task(args.task_id)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
