#!/usr/bin/env python3
"""
长时间运行代理系统 - 主编排器

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
import threading
from queue import Queue, Empty

from config import (
    CHECK_INTERVAL,
    get_paths,
    is_safe_workspace,
    TASK_MODIFICATION_PROMPT,
    TASKS_CREATION_PROMPT,
    TASKS_REVISION_PROMPT,
    LEARN_PROMPT,
    TaskStatus,
    truncate_for_display,
)
from claude_runner import run_claude, make_printer, EventCallbacks
from task_manager import TaskManager, Task
from worker import WorkerProcess
from supervisor import Supervisor, Decision, SupervisorResult
from validator import PostWorkValidator
from orchestrator import TaskOrchestrator
from cost_tracker import CostTracker, CostSource, estimate_cost_from_log


class LongRunningAgent:
    """长时间运行代理编排器"""

    def __init__(self, workspace_dir: str = None, verbose: bool = True):
        # 解析 workspace 路径
        self.paths = get_paths(workspace_dir)
        self.workspace_dir = self.paths["workspace"]
        self.tasks_file = self.paths["tasks_file"]
        self.verbose = verbose

        # 初始化组件（使用动态路径）
        self.task_manager = TaskManager(self.tasks_file)
        self.supervisor = Supervisor(self.workspace_dir, verbose=verbose)
        self.orchestrator = TaskOrchestrator(self.workspace_dir, verbose=verbose)
        self.cost_tracker = CostTracker(self.workspace_dir)

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
                self._git_commit(f"chore: initial snapshot of {existing_files} existing files")
                print(f"✓ 已提交现有 {existing_files} 个文件作为初始快照")
        else:
            print("✓ Git 仓库已存在")
            # 检查是否有未提交的更改
            if self._has_uncommitted_changes():
                print("⚠️  检测到未提交的更改，建议先手动提交")

        # 确保 .claude_plus/ 和 CLAUDE.md 在 .gitignore 中
        self._ensure_gitignore_entry(".claude_plus/")
        self._ensure_gitignore_entry("CLAUDE.md")

        # 3. 检查任务文件（不自动创建）
        if not os.path.exists(self.tasks_file):
            print(f"\n⚠️  任务文件不存在: {self.tasks_file}")
            print("\n请创建 tasks.json 文件，格式如下：")
            print(
                """
[
  {
    "id": "1",
    "description": "第一个任务",
    "steps": ["步骤1", "步骤2"]
  },
  {
    "id": "1.1",
    "description": "子任务",
    "steps": ["步骤1", "步骤2"]
  },
  {
    "id": "2",
    "description": "第二个顶层任务",
    "steps": ["步骤1", "步骤2"]
  }
]
"""
            )
            return False
        else:
            print(f"✓ 任务文件: {self.tasks_file}")

        # 5. 进度由 git 管理（无需额外配置）
        print("\u2713 进度追踪: Git")

        # 6. 提交初始化脚本等配置文件
        if self._has_uncommitted_changes():
            self._git_commit("chore: add task management configuration files")
            print("✓ 配置文件已提交")

        print("\n初始化完成！")
        self._print_stats()
        return True

    def _ensure_gitignore_entry(self, entry: str):
        """确保 .gitignore 中包含指定条目"""
        gitignore_path = os.path.join(self.workspace_dir, ".gitignore")

        # 读取现有内容
        existing_entries = set()
        if os.path.exists(gitignore_path):
            with open(gitignore_path, "r") as f:
                existing_entries = {line.strip() for line in f if line.strip()}

        # 如果已存在则跳过
        if entry in existing_entries:
            return

        # 追加新条目
        with open(gitignore_path, "a") as f:
            if existing_entries:  # 文件非空时先加换行
                f.write("\n")
            f.write(f"{entry}\n")
        print(f"✓ 已添加 {entry} 到 .gitignore")

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

    def _format_duration(self, seconds: float) -> str:
        """格式化时长为 HH:MM:SS"""
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"

    def _print_realtime_event(self, evt: dict, elapsed_str: str):
        """实时打印事件"""
        evt_type = evt.get("type", "")

        if evt_type == "tool":
            name = evt.get("name", "")
            inp = evt.get("input", "")
            # 工具调用用蓝色高亮
            if inp:
                print(f"   [{elapsed_str}] \033[36m🔧 {name}\033[0m: {truncate_for_display(inp)}")
            else:
                print(f"   [{elapsed_str}] \033[36m🔧 {name}\033[0m")

        elif evt_type == "text":
            content = evt.get("content", "")
            # 思考内容用灰色
            print(f"   [{elapsed_str}] \033[90m💭 {truncate_for_display(content)}\033[0m")

        elif evt_type == "result":
            is_error = evt.get("is_error", False)
            result = evt.get("result", "")
            if is_error:
                print(f"   [{elapsed_str}] \033[31m❌ 错误: {result}\033[0m")
            else:
                print(f"   [{elapsed_str}] \033[32m✅ 完成: {result}\033[0m")

    def _display_handover_summary(self, summary: str):
        """展示交接摘要给用户"""
        print("\n" + "=" * 60)
        print("📝 Worker 交接摘要")
        print("=" * 60)
        # 逐行打印，添加缩进
        for line in summary.strip().split("\n"):
            # 标题行加粗
            if line.startswith("## "):
                print(f"\033[1m{line}\033[0m")
            else:
                print(f"   {line}")
        print("=" * 60 + "\n")

    def _generate_activity_summary(self, worker_log) -> str:
        """从日志中生成活动摘要（当没有交接摘要时使用）"""
        lines = ["## 执行情况（自动生成）"]
        lines.append("Worker 在中断前未能完成交接摘要，以下是从日志中提取的活动记录：")
        lines.append("")

        # 提取工具调用
        tool_calls = [e for e in worker_log.events if e.get("type") == "tool"]
        if tool_calls:
            lines.append("## 执行的操作")
            for evt in tool_calls[-10:]:  # 最近10个操作
                name = evt.get("name", "")
                inp = evt.get("input", "")
                if inp:
                    lines.append(f"- {name}: {truncate_for_display(inp)}")
                else:
                    lines.append(f"- {name}")
            lines.append("")

        # 提取思考内容
        text_events = [e for e in worker_log.events if e.get("type") == "text"]
        if text_events:
            lines.append("## 最后的思考")
            # 只取最后一个有意义的思考
            last_thought = text_events[-1].get("content", "")
            if last_thought:
                lines.append(truncate_for_display(last_thought))
            lines.append("")

        lines.append("## 下一步建议")
        lines.append("任务被用户中断，下一个 Worker 应该从头开始或继续上述操作")

        return "\n".join(lines)

    def _get_git_context(self) -> str:
        """获取 Git 历史作为进度上下文"""
        result = subprocess.run(
            ["git", "log", "--oneline", "-10"],
            cwd=self.workspace_dir,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        return "（首个任务，暂无历史）"

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

        if task.session_id:
            print(f"\n## 调试命令")
            print(f"   恢复会话查看详情：claude -r {task.session_id}")

    def _has_failed_tasks(self) -> bool:
        """检查是否有失败的任务"""
        return any(t.status == TaskStatus.FAILED for t in self.task_manager.get_all_tasks())

    def _get_failed_tasks_summary(self) -> str:
        """获取失败任务摘要"""
        failed = [t for t in self.task_manager.get_all_tasks() if t.status == TaskStatus.FAILED]
        if not failed:
            return "无失败任务"
        lines = ["失败任务列表:"]
        for t in failed:
            lines.append(f"- [{t.id}] {t.description}: {t.error_message or '未知错误'}")
        return "\n".join(lines)

    def _print_failed_tasks_detail(self):
        """打印失败任务详情（供用户排查）"""
        failed = [t for t in self.task_manager.get_all_tasks() if t.status == TaskStatus.FAILED]
        if not failed:
            return
        print("\n" + "─" * 50)
        print("❌ 未解决的失败任务:")
        for t in failed:
            print(f"   [{t.id}] {t.description}")
            if t.error_message:
                print(f"        错误: {t.error_message[:100]}")
            if t.notes:
                print(f"        备注: {t.notes[:100]}")
        print("─" * 50)
        print("建议: 手动编辑 tasks.json 或使用 'python3 main.py reset-task <id>' 重置")

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
        print(f"   检查间隔: {CHECK_INTERVAL}秒 | Supervisor: 每次检查都分析")
        print("   提示: 按 Ctrl+C 可安全终止\n")

        tasks_processed = 0
        current_worker = None
        commit_before_task = None
        failed_task_retries = 0  # 失败任务处理重试计数
        MAX_FAILED_RETRIES = 3   # 最大重试次数

        try:
            while True:
                # 检查是否达到最大任务数
                if max_tasks and tasks_processed >= max_tasks:
                    print(f"\n已达到最大任务数限制: {max_tasks}")
                    break

                # 优先处理失败任务：在获取下一个任务之前，检查是否有失败任务
                if self._has_failed_tasks():
                    if failed_task_retries >= MAX_FAILED_RETRIES:
                        print(f"\n⚠️  Orchestrator 已尝试 {MAX_FAILED_RETRIES} 次处理失败任务，仍有未解决的失败任务")
                        print("   请手动检查 tasks.json 中的 failed 任务")
                        self._print_failed_tasks_detail()
                        break

                    failed_task_retries += 1
                    print(f"\n🎭 检测到失败任务，调用 Orchestrator 处理 (尝试 {failed_task_retries}/{MAX_FAILED_RETRIES})...")
                    orch_result = self.orchestrator.orchestrate(
                        trigger="检测到失败任务，立即处理",
                        context=self._get_failed_tasks_summary()
                    )
                    # 记录 Orchestrator 成本
                    if orch_result.cost_usd > 0:
                        self.cost_tracker.add(
                            source=CostSource.ORCHESTRATOR,
                            cost_usd=orch_result.cost_usd,
                            details="Handle failed tasks"
                        )
                    # Orchestrator 处理后重新加载任务（可能已将 failed 改为 pending 或删除）
                    self.task_manager._load_tasks()
                    continue  # 重新检查是否还有失败任务
                else:
                    # 没有失败任务时重置计数器
                    failed_task_retries = 0

                # 获取下一个任务
                task = self.task_manager.get_next_task()
                if not task:
                    print("\n✅ 所有任务已完成!")
                    break

                # 记录任务开始前的 commit
                commit_before_task = self._get_last_good_commit()

                # 显示任务信息
                print(f"\n{'─' * 50}")
                print(f"📝 处理任务 [{task.id}]: {task.description}")
                if task.notes:
                    print(f"   📋 备注: {truncate_for_display(task.notes)}")
                print(f"{'─' * 50}")

                # 创建并启动 Worker
                worker = WorkerProcess(task, self.workspace_dir)
                current_worker = worker
                pid = worker.start()

                self.task_manager.mark_in_progress(task.id, f"worker_{pid}")

                print(f"   🚀 Worker 启动: PID {pid}")
                print(f"   📄 日志: {worker.log_file}")

                # 监督循环 - 实时显示日志，后台异步执行 supervisor
                check_count = 0
                decision_made = False
                last_supervisor_time = time.time()
                REALTIME_INTERVAL = 2  # 实时日志检查间隔（秒）

                # 后台 Supervisor 结果队列
                supervisor_queue = Queue()
                supervisor_thread = None

                print()  # 空行，准备实时输出

                while worker.is_alive():
                    time.sleep(REALTIME_INTERVAL)
                    elapsed = worker.elapsed_seconds()
                    elapsed_str = self._format_duration(elapsed)

                    # 实时显示新事件
                    new_events = worker.read_new_events()
                    for evt in new_events:
                        self._print_realtime_event(evt, elapsed_str)

                    # 检查后台 Supervisor 是否有结果
                    try:
                        while True:
                            sv_result, sv_check_count, sv_elapsed = supervisor_queue.get_nowait()
                            sv_elapsed_str = self._format_duration(sv_elapsed)
                            # 记录 Supervisor 成本
                            if sv_result.cost_usd > 0:
                                self.cost_tracker.add(
                                    source=CostSource.SUPERVISOR,
                                    cost_usd=sv_result.cost_usd,
                                    task_id=task.id,
                                    details=f"Check #{sv_check_count}"
                                )
                            # 显示 Supervisor 检查结果（不阻塞日志输出）
                            print(f"\n   {'─' * 40}")
                            print(f"   🔍 [{sv_elapsed_str}] Supervisor 检查 #{sv_check_count} 完成")
                            print(
                                f"      📋 决策: \033[1m{sv_result.decision.value}\033[0m | {sv_result.reason}"
                            )
                            if sv_result.cost_usd > 0:
                                print(f"      💰 成本: ${sv_result.cost_usd:.4f}")
                            print(f"   {'─' * 40}\n")

                            if sv_result.decision != Decision.CONTINUE:
                                self._handle_supervisor_decision(
                                    task, worker, sv_result, commit_before_task
                                )
                                decision_made = True
                                break
                    except Empty:
                        pass

                    if decision_made:
                        break

                    # 检查是否到达 supervisor 检查时间，且没有正在运行的检查
                    time_since_last_check = time.time() - last_supervisor_time
                    if time_since_last_check >= CHECK_INTERVAL and (supervisor_thread is None or not supervisor_thread.is_alive()):
                        check_count += 1
                        last_supervisor_time = time.time()
                        current_elapsed = elapsed

                        # 显示开始检查的提示
                        print(f"\n   \033[90m🔍 [{elapsed_str}] Supervisor 检查 #{check_count} 启动中...\033[0m")

                        # 在后台线程中执行 Supervisor 分析
                        def run_supervisor(task, worker, check_count, elapsed, queue):
                            try:
                                sv_result = self.supervisor.analyze(
                                    task, worker, check_count, elapsed
                                )
                                queue.put((sv_result, check_count, elapsed))
                            except Exception as e:
                                # 分析失败时返回继续等待
                                queue.put((SupervisorResult(decision=Decision.CONTINUE, reason=f"分析失败: {e}"), check_count, elapsed))

                        supervisor_thread = threading.Thread(
                            target=run_supervisor,
                            args=(task, worker, check_count, current_elapsed, supervisor_queue),
                            daemon=True
                        )
                        supervisor_thread.start()

                # Worker 自然结束
                if not decision_made:
                    # 取消正在进行的 Supervisor 分析（Worker 已完成，不需要继续检查）
                    if supervisor_thread and supervisor_thread.is_alive():
                        self.supervisor.cancel()
                    self._finalize_worker(task, worker, commit_before_task)

                current_worker = None
                commit_before_task = None
                tasks_processed += 1

        except KeyboardInterrupt:
            print("\n\n" + "=" * 60)
            print("⚠️  检测到 Ctrl+C，正在安全终止...")
            print("=" * 60)

            cleanup_result = None
            if current_worker:
                # 先读取日志，获取执行情况（在终止前）
                worker_log = current_worker.read_log()

                # 尝试从日志提取成本（即使被中断也可能有 result 事件）
                if worker_log.cost_usd > 0:
                    self.cost_tracker.add(
                        source=CostSource.WORKER,
                        cost_usd=worker_log.cost_usd,
                        task_id=task.id if task else None,
                        details="Interrupted by Ctrl+C"
                    )
                else:
                    # 尝试估算成本
                    estimated_cost = estimate_cost_from_log(current_worker.log_file)
                    if estimated_cost > 0:
                        self.cost_tracker.add(
                            source=CostSource.WORKER,
                            cost_usd=estimated_cost,
                            task_id=task.id if task else None,
                            details="Estimated (interrupted)",
                            estimated=True
                        )

                if current_worker.is_alive():
                    print(f"\n正在优雅终止 Worker...")
                    # 使用优雅关闭：先中断，然后让 Worker 执行清理工作
                    cleanup_result = current_worker.graceful_shutdown(
                        reason="用户按下 Ctrl+C 请求终止"
                    )
                    # 记录 cleanup 成本
                    if cleanup_result.cost_usd > 0:
                        self.cost_tracker.add(
                            source=CostSource.WORKER_CLEANUP,
                            cost_usd=cleanup_result.cost_usd,
                            task_id=task.id if task else None,
                            details="Graceful shutdown cleanup"
                        )
                    if cleanup_result.success:
                        print(f"   ✅ Worker 已优雅终止并完成清理")
                    else:
                        print(f"   ⚠️  Worker 已终止（清理可能不完整）")
                else:
                    print(f"\n   ✅ Worker 已结束")
                    cleanup_result = type(
                        "CleanupResult", (), {"success": True, "handover_summary": None, "cost_usd": 0.0}
                    )()

                # 记录中断信息到 task.notes（供下次 loop 使用）
                if task:
                    if cleanup_result and cleanup_result.handover_summary:
                        # 有交接摘要，写入 task.notes
                        self.task_manager.update_notes(task.id, f"中断交接:\n{cleanup_result.handover_summary}")
                        print(f"   📋 交接摘要已记录到 task.notes")
                        self._display_handover_summary(cleanup_result.handover_summary)
                    else:
                        # 没有交接摘要，从日志中生成活动记录
                        auto_summary = self._generate_activity_summary(worker_log)
                        self.task_manager.update_notes(task.id, f"中断交接:\n{auto_summary}")
                        print(f"   📋 活动记录已保存到 task.notes")
                        self._display_handover_summary(auto_summary)

            # 只有在清理失败时才回退代码
            cleanup_success = cleanup_result.success if cleanup_result else False
            if not cleanup_success and commit_before_task:
                print(f"\n   ⚠️  清理未完成，回退代码以确保一致性...")
                if self._git_reset_to(commit_before_task):
                    print(f"   ✅ 已回退到 commit: {commit_before_task[:8]}")
            elif cleanup_success:
                print(f"\n   ✅ Worker 已保存工作状态，代码保留")

            # 打印成本摘要
            self.cost_tracker.print_summary()

            print("\n下次可以继续运行: python3 main.py run")
            return

        # 打印最终统计
        print("\n" + "=" * 60)
        print("📈 运行完成")
        print("=" * 60)
        self._print_stats()
        self.cost_tracker.print_summary()

    def _handle_supervisor_decision(
        self, task: Task, worker: WorkerProcess, sv_result, commit_before: str
    ):
        """处理 Supervisor 的决策"""
        print(f"   📋 Supervisor 决策: {sv_result.decision.value}")
        print(f"   📋 原因: {sv_result.reason}")

        # 先读取日志（在终止前）
        worker_log = worker.read_log()

        # 记录 Worker 成本（即使被中断）
        if worker_log.cost_usd > 0:
            self.cost_tracker.add(
                source=CostSource.WORKER,
                cost_usd=worker_log.cost_usd,
                task_id=task.id,
                details="Interrupted by Supervisor"
            )
        else:
            # 尝试估算成本
            estimated_cost = estimate_cost_from_log(worker.log_file)
            if estimated_cost > 0:
                self.cost_tracker.add(
                    source=CostSource.WORKER,
                    cost_usd=estimated_cost,
                    task_id=task.id,
                    details="Estimated (supervisor interrupt)",
                    estimated=True
                )

        # 终止 Worker（使用优雅关闭）
        cleanup_result = None
        if worker.is_alive():
            cleanup_result = worker.graceful_shutdown(
                reason=f"Supervisor 决策: {sv_result.reason}"
            )
            # 记录 cleanup 成本
            if cleanup_result.cost_usd > 0:
                self.cost_tracker.add(
                    source=CostSource.WORKER_CLEANUP,
                    cost_usd=cleanup_result.cost_usd,
                    task_id=task.id,
                    details="Supervisor triggered cleanup"
                )

        # 记录交接或活动摘要到 task.notes
        if cleanup_result and cleanup_result.handover_summary:
            # 有交接摘要，写入 task.notes
            self.task_manager.update_notes(task.id, f"Supervisor中断:\n{cleanup_result.handover_summary}")
            print(f"   📋 交接摘要已记录到 task.notes")
            self._display_handover_summary(cleanup_result.handover_summary)
        else:
            # 没有交接摘要，从日志中生成活动记录
            auto_summary = self._generate_activity_summary(worker_log)
            self.task_manager.update_notes(task.id, f"Supervisor中断:\n{auto_summary}")
            print(f"   📋 活动记录已保存到 task.notes")
            self._display_handover_summary(auto_summary)

        if sv_result.decision == Decision.ORCHESTRATE:
            # 调用编排器重新审视任务列表
            result = self.orchestrator.orchestrate(
                trigger=f"Supervisor 决策: {sv_result.reason}",
                context=f"任务 [{task.id}]: {task.description}"
            )
            # 记录 Orchestrator 成本
            if result.cost_usd > 0:
                self.cost_tracker.add(
                    source=CostSource.ORCHESTRATOR,
                    cost_usd=result.cost_usd,
                    task_id=task.id,
                    details="Supervisor triggered orchestration"
                )
            if result.success:
                print(f"   ✅ 任务编排完成")
            else:
                print(f"   ⚠️  编排失败: {result.message}")
                self.task_manager.mark_failed(task.id, f"编排失败: {result.message}")

    def _finalize_worker(
        self, task: Task, worker: WorkerProcess, commit_before: str = None
    ):
        """处理 Worker 自然结束的情况 - 使用 Post-work 验证"""
        _ = commit_before  # 保留参数用于未来扩展
        log = worker.read_log()

        # 记录成本
        if log.cost_usd > 0:
            self.cost_tracker.add(
                source=CostSource.WORKER,
                cost_usd=log.cost_usd,
                task_id=task.id,
                details=f"Task completed: {task.description[:30]}"
            )
        print(f"   💰 成本: ${log.cost_usd:.4f} | 总成本: ${self.cost_tracker.get_session_cost():.4f}")

        # 检查 Claude CLI 是否返回错误（异常情况）
        if log.is_error:
            error_msg = log.result[:200] if log.result else "执行失败"
            print(f"   ❌ Worker 执行失败: {error_msg[:50]}...")
            self.task_manager.update_notes(task.id, f"执行失败: {error_msg[:100]}")
            self.task_manager.mark_failed(task.id, error_msg)
            return

        # Post-work 验证阶段
        print(f"\n   📋 Post-work 验证中...")
        validator = PostWorkValidator(self.workspace_dir, self.task_manager)
        result = validator.validate_and_commit(task)

        # 记录 Validator 成本
        if result.cost_usd > 0:
            self.cost_tracker.add(
                source=CostSource.VALIDATOR,
                cost_usd=result.cost_usd,
                task_id=task.id,
                details="Post-work validation"
            )

        if result.success:
            print(f"   ✅ 任务完成!")
            self.task_manager.mark_completed(task.id)
        else:
            # 验证失败，调用 Orchestrator
            print(f"   🎭 验证未通过，调用 Orchestrator...")
            orch_result = self.orchestrator.orchestrate(
                trigger=f"任务 [{task.id}] 验证失败",
                context=f"任务描述: {task.description}\n错误: {'; '.join(result.errors)}"
            )
            # 记录 Orchestrator 成本
            if orch_result.cost_usd > 0:
                self.cost_tracker.add(
                    source=CostSource.ORCHESTRATOR,
                    cost_usd=orch_result.cost_usd,
                    task_id=task.id,
                    details="Validation failed orchestration"
                )
            # Orchestrator 可能修改了 tasks.json，刷新内存
            self.task_manager._load_tasks()

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

        print("\n" + self._get_git_context())

    def add_task_from_prompt(self, user_request: str) -> bool:
        """根据用户自然语言描述修改任务列表，支持交互式反馈循环"""
        print("\n" + "=" * 60)
        print("🤖 Claude 正在分析需求并修改任务列表...")
        print("=" * 60)

        # 检查 tasks.json 是否存在
        if not os.path.exists(self.tasks_file):
            print(f"\n⚠️  tasks.json 不存在: {self.tasks_file}")
            print("   请先运行 'python3 main.py init' 初始化项目")
            return False

        # 构建 prompt
        prompt = TASK_MODIFICATION_PROMPT.format(user_request=user_request)

        # 调用 Claude Code（在 workspace 目录下）
        result, session_id = self._call_claude_for_modification(prompt)

        # 交互式反馈循环
        while True:
            if not (result and "TASKS_MODIFIED" in result):
                print("\n❌ 任务修改失败")
                return False

            # 校验修改后的 tasks.json
            if not self._validate_tasks_json():
                print("\n❌ 修改后的 tasks.json 格式无效")
                return False

            print("\n✅ tasks.json 修改成功！")

            # 重新加载并显示任务列表
            self.task_manager._load_tasks()
            self._show_generated_tasks()

            # 询问用户反馈
            print("\n" + "-" * 40)
            print("请确认任务列表：")
            print("  - 输入 y 确认")
            print("  - 输入反馈文字，Claude 将继续修改")
            print("-" * 40)

            user_input = input("\n确认或反馈: ").strip()

            if user_input.lower() == 'y':
                break
            elif user_input == '':
                print("已取消（输入为空）")
                return False
            else:
                # 用户提供反馈，resume session 继续修改
                if not session_id:
                    print("⚠️  无法获取会话 ID，无法继续修改")
                    print("请手动修改 tasks.json 或重新运行 task 命令")
                    return False

                print("\n" + "=" * 60)
                print("🔄 Claude 正在根据反馈继续修改...")
                print("=" * 60)

                result, session_id = self._call_claude_for_revision(
                    session_id, user_input
                )

        print(f"\n✅ 任务修改完成！")
        print(f"   运行 'python3 main.py run' 开始执行")
        return True

    def _call_claude_for_modification(self, prompt: str, resume_session_id: str = None):
        """调用 Claude Code 修改任务，返回 (result, session_id)"""
        return self._call_claude(prompt, resume_session_id, cost_details="add_task_from_prompt")

    def _call_claude(self, prompt: str, resume_session_id: str = None, cost_details: str = ""):
        """统一的 Claude 调用方法"""
        # 自定义回调以获取 session_id 和记录成本
        session_id = None
        cost_usd = 0.0

        def on_init(sid):
            nonlocal session_id
            session_id = sid

        def on_result(text, cost):
            nonlocal session_id, cost_usd
            cost_usd = cost
            if cost > 0:
                self.cost_tracker.add(
                    source=CostSource.TASK_GENERATION,
                    cost_usd=cost,
                    details=cost_details
                )
            print(f"\n   💰 成本: ${cost:.4f}")

        callbacks = EventCallbacks(
            on_init=on_init,
            on_text=lambda t: print(f"   💭 {t}"),
            on_tool=lambda n, i: print(f"   🔧 {n}: {i}" if i else f"   🔧 {n}"),
            on_result=on_result,
        )

        result = run_claude(
            prompt,
            workspace_dir=self.workspace_dir,
            resume_session_id=resume_session_id,
            callbacks=callbacks,
        )

        if result.is_error:
            print(f"❌ 调用失败: {result.result_text}")
            return None, None

        # 优先使用 result 中的 session_id
        return result.result_text, result.session_id or session_id

    def reset(self):
        """重置所有任务状态"""
        for task in self.task_manager.get_all_tasks():
            self.task_manager.reset_task(task.id)
        # 进度由 git 管理，无需清理
        print("✓ 所有任务已重置")

    def reset_single_task(self, task_id: str):
        """重置单个任务状态"""
        task = self.task_manager.get_task_by_id(task_id)
        if task:
            self.task_manager.reset_task(task_id)
            print(f"✓ 任务 [{task_id}] 已重置")
        else:
            print(f"❌ 未找到任务: {task_id}")

    def create_tasks_from_prompt(self, user_request: str) -> bool:
        """根据用户需求生成 tasks.json，支持交互式反馈循环"""
        import json as json_module

        print("\n" + "=" * 60)
        print("🤖 Claude 正在分析项目并生成任务...")
        print("=" * 60)

        # 检查 tasks.json 是否已存在
        if os.path.exists(self.tasks_file):
            print(f"\n⚠️  tasks.json 已存在: {self.tasks_file}")
            confirm = input("是否覆盖？(y/N): ").strip().lower()
            if confirm != 'y':
                print("已取消")
                return False

        # 构建 prompt（TASKS_GUIDE 规范已嵌入模板）
        prompt = TASKS_CREATION_PROMPT.format(user_request=user_request)

        # 调用 Claude Code（在 workspace 目录下）
        result, session_id = self._call_claude_for_creation(prompt)

        # 交互式反馈循环
        while True:
            if not (result and ("TASKS_CREATED" in result or "TASKS_MODIFIED" in result)):
                print("\n❌ 任务生成失败")
                return False

            # 校验生成的 tasks.json
            if not self._validate_tasks_json():
                print("\n❌ 生成的 tasks.json 格式无效")
                return False

            print("\n✅ tasks.json 生成成功！")

            # 显示生成的任务列表
            self._show_generated_tasks()

            # 询问用户反馈
            print("\n" + "-" * 40)
            print("请确认任务列表：")
            print("  - 输入 y 确认并继续")
            print("  - 输入反馈文字，Claude 将根据反馈修改任务列表")
            print("-" * 40)

            user_input = input("\n确认或反馈: ").strip()

            if user_input.lower() == 'y':
                # 用户确认，跳出循环
                break
            elif user_input == '':
                # 空输入视为取消
                print("已取消")
                return False
            else:
                # 用户提供反馈，resume session 修改任务
                if not session_id:
                    print("⚠️  无法获取会话 ID，无法 resume 修改")
                    print("请手动修改 tasks.json 后重新运行")
                    return False

                print("\n" + "=" * 60)
                print("🔄 Claude 正在根据反馈修改任务列表...")
                print("=" * 60)

                result, session_id = self._call_claude_for_revision(
                    session_id, user_input
                )
                # 循环继续，重新展示修改后的任务列表

        # 用户确认后，询问是否提交
        confirm_commit = input("\n是否提交到 Git？(y/N): ").strip().lower()
        if confirm_commit == 'y':
            self._git_commit("feat: initialize task list")
            print("✅ 已提交")
        else:
            print("ℹ️  未提交，你可以稍后手动提交")

        return True

    def _show_generated_tasks(self):
        """显示生成的任务列表"""
        import json as json_module
        try:
            with open(self.tasks_file, "r", encoding="utf-8") as f:
                tasks = json_module.load(f)

            print("\n📋 生成的任务列表:")
            print("-" * 40)
            for t in tasks:
                print(f"  [{t.get('id', '?')}] {t.get('description', '')}")
                if t.get('steps'):
                    for step in t['steps'][:2]:  # 只显示前两个步骤
                        print(f"      - {step}")
                    if len(t.get('steps', [])) > 2:
                        print(f"      ... 共 {len(t['steps'])} 个步骤")
            print("-" * 40)
            print(f"共 {len(tasks)} 个任务")
        except Exception:
            pass

    def _call_claude_for_creation(self, prompt: str, resume_session_id: str = None):
        """调用 Claude Code 生成任务，返回 (result, session_id)"""
        return self._call_claude(prompt, resume_session_id, cost_details="create_tasks_from_prompt")

    def _call_claude_for_revision(self, session_id: str, feedback: str):
        """Resume session 根据用户反馈修改任务列表"""
        prompt = TASKS_REVISION_PROMPT.format(feedback=feedback)
        return self._call_claude(prompt, session_id, cost_details="revise_tasks")

    def _validate_tasks_json(self) -> bool:
        """校验 tasks.json 格式"""
        import json as json_module
        try:
            with open(self.tasks_file, "r", encoding="utf-8") as f:
                data = json_module.load(f)

            if not isinstance(data, list):
                print("   ⚠️  tasks.json 应该是一个数组")
                return False

            # 检查 ID 唯一性
            ids = [t.get("id") for t in data if "id" in t]
            if len(ids) != len(set(ids)):
                print("   ⚠️  存在重复的任务 ID")
                return False

            # 检查必填字段
            for task in data:
                if "id" not in task or "description" not in task:
                    print("   ⚠️  任务缺少必填字段 (id/description)")
                    return False

            return True
        except json_module.JSONDecodeError as e:
            print(f"   ⚠️  JSON 解析失败: {e}")
            return False
        except Exception as e:
            print(f"   ⚠️  校验异常: {e}")
            return False

    def learn(self, suggestion: str) -> bool:
        """根据用户建议更新 CLAUDE.md"""
        print("\n" + "=" * 60)
        print("📚 Claude 正在学习并更新 CLAUDE.md...")
        print("=" * 60)

        prompt = LEARN_PROMPT.format(suggestion=suggestion)
        result_text, _ = self._call_claude(prompt, cost_details="learn")

        if result_text and "LEARNED" in result_text:
            print("\n✅ CLAUDE.md 已更新！")
            return True

        if result_text is None:
            return False

        return True



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
    init_parser = subparsers.add_parser("init", help="初始化工作环境")
    init_parser.add_argument(
        "prompt",
        nargs="?",
        default=None,
        help="可选：描述项目需求，Claude 将自动生成 tasks.json"
    )

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

    # task 命令
    task_parser = subparsers.add_parser("task", help="根据描述修改任务列表")
    task_parser.add_argument("description", help="任务修改描述")

    # learn 命令
    learn_parser = subparsers.add_parser("learn", help="学习建议并更新 CLAUDE.md")
    learn_parser.add_argument("suggestion", help="要学习的建议")

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
        # 如果提供了 prompt，生成 tasks.json
        if args.prompt:
            agent.create_tasks_from_prompt(args.prompt)
    elif args.command == "run":
        agent.run(max_tasks=args.max_tasks)
    elif args.command == "status":
        agent.status()
    elif args.command == "reset":
        agent.reset()
    elif args.command == "reset-task":
        agent.reset_single_task(args.task_id)
    elif args.command == "task":
        agent.add_task_from_prompt(args.description)
    elif args.command == "learn":
        agent.learn(args.suggestion)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
