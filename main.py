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
import signal
import argparse
import subprocess
import threading
from queue import Queue, Empty
from datetime import datetime

from config import (
    DEFAULT_WORKSPACE_DIR,
    CHECK_INTERVAL,
    get_paths,
    is_safe_workspace,
    CLAUDE_CMD,
    TASK_GENERATION_PROMPT,
    TASKS_CREATION_PROMPT,
)
from task_manager import TaskManager, Task
# progress_log 已弃用，进度通过 git commit 和 task.notes 追踪
from worker import WorkerProcess
from supervisor import Supervisor, Decision, SupervisorResult
from validator import PostWorkValidator
from orchestrator import TaskOrchestrator


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

        # 确保 .claude_plus/ 在 .gitignore 中
        self._ensure_gitignore_entry(".claude_plus/")

        # 3. 检查任务文件（不自动创建）
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

        # 5. 进度由 git 管理（无需额外配置）
        print("\u2713 进度追踪: Git")

        # 6. 提交初始化脚本等配置文件
        if self._has_uncommitted_changes():
            self._git_commit("添加任务管理配置文件")
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

    def _get_worker_activity(self, worker: WorkerProcess) -> str:
        """获取 Worker 最近活动摘要"""
        log = worker.read_log()
        if not log.events:
            return ""

        # 获取最近的事件
        recent = log.events[-3:]
        activities = []
        for evt in recent:
            if evt["type"] == "tool":
                name = evt["name"]
                inp = evt.get("input", "")[:25]
                activities.append(f"{name}({inp})")
            elif evt["type"] == "text":
                activities.append(evt["content"][:35] + "...")

        return " → ".join(activities) if activities else ""

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
                print(f"   [{elapsed_str}] \033[36m🔧 {name}\033[0m: {inp}")
            else:
                print(f"   [{elapsed_str}] \033[36m🔧 {name}\033[0m")

        elif evt_type == "text":
            content = evt.get("content", "")
            # 思考内容用灰色
            print(f"   [{elapsed_str}] \033[90m💭 {content}\033[0m")

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

    def _generate_activity_summary(self, worker_log, activity_summary: str) -> str:
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
                    lines.append(f"- {name}: {inp[:60]}")
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
                lines.append(last_thought[:200])
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
        from config import TaskStatus
        return any(t.status == TaskStatus.FAILED for t in self.task_manager.get_all_tasks())

    def _get_failed_tasks_summary(self) -> str:
        """获取失败任务摘要"""
        from config import TaskStatus
        failed = [t for t in self.task_manager.get_all_tasks() if t.status == TaskStatus.FAILED]
        if not failed:
            return "无失败任务"
        lines = ["失败任务列表:"]
        for t in failed:
            lines.append(f"- [{t.id}] {t.description}: {t.error_message or '未知错误'}")
        return "\n".join(lines)

    def _print_failed_tasks_detail(self):
        """打印失败任务详情（供用户排查）"""
        from config import TaskStatus
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
                    self.orchestrator.orchestrate(
                        trigger="检测到失败任务，立即处理",
                        context=self._get_failed_tasks_summary()
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
                print(f"   优先级: {task.priority}")
                if task.notes:
                    print(f"   📋 备注: {task.notes[:50]}...")
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
                            # 显示 Supervisor 检查结果（不阻塞日志输出）
                            print(f"\n   {'─' * 40}")
                            print(f"   🔍 [{sv_elapsed_str}] Supervisor 检查 #{sv_check_count} 完成")
                            print(
                                f"      📋 决策: \033[1m{sv_result.decision.value}\033[0m | {sv_result.reason}"
                            )
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
                activity_summary = current_worker.get_log_summary(max_events=20)

                if current_worker.is_alive():
                    print(f"\n正在优雅终止 Worker...")
                    # 使用优雅关闭：先中断，然后让 Worker 执行清理工作
                    cleanup_result = current_worker.graceful_shutdown(
                        reason="用户按下 Ctrl+C 请求终止"
                    )
                    if cleanup_result.success:
                        print(f"   ✅ Worker 已优雅终止并完成清理")
                    else:
                        print(f"   ⚠️  Worker 已终止（清理可能不完整）")
                else:
                    print(f"\n   ✅ Worker 已结束")
                    cleanup_result = type(
                        "CleanupResult", (), {"success": True, "handover_summary": None}
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
                        auto_summary = self._generate_activity_summary(
                            worker_log, activity_summary
                        )
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

        # 先读取日志（在终止前）
        worker_log = worker.read_log()
        activity_summary = worker.get_log_summary(max_events=20)

        # 终止 Worker（使用优雅关闭）
        cleanup_result = None
        if worker.is_alive():
            cleanup_result = worker.graceful_shutdown(
                reason=f"Supervisor 决策: {sv_result.reason}"
            )

        # 记录交接或活动摘要到 task.notes
        if cleanup_result and cleanup_result.handover_summary:
            # 有交接摘要，写入 task.notes
            self.task_manager.update_notes(task.id, f"Supervisor中断:\n{cleanup_result.handover_summary}")
            print(f"   📋 交接摘要已记录到 task.notes")
            self._display_handover_summary(cleanup_result.handover_summary)
        else:
            # 没有交接摘要，从日志中生成活动记录
            auto_summary = self._generate_activity_summary(worker_log, activity_summary)
            self.task_manager.update_notes(task.id, f"Supervisor中断:\n{auto_summary}")
            print(f"   📋 活动记录已保存到 task.notes")
            self._display_handover_summary(auto_summary)

        if sv_result.decision == Decision.ORCHESTRATE:
            # 调用编排器重新审视任务列表
            result = self.orchestrator.orchestrate(
                trigger=f"Supervisor 决策: {sv_result.reason}",
                context=f"任务 [{task.id}]: {task.description}"
            )
            if result.success:
                print(f"   ✅ 任务编排完成")
                # 回退代码到任务开始前
                if commit_before and self._git_reset_to(commit_before):
                    print(f"   ✅ 已回退代码到: {commit_before[:8]}")
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
        self.total_cost += log.cost_usd
        print(f"   💰 成本: ${log.cost_usd:.4f} | 总成本: ${self.total_cost:.4f}")

        # 检查 Worker 是否报告了阻塞或错误
        if log.result and "TASK_BLOCKED" in log.result:
            error = log.result.split("TASK_BLOCKED:")[-1].strip()[:100]
            print(f"   ⏸️  任务被阻塞: {error}")
            self.task_manager.update_notes(task.id, f"阻塞: {error}")
            self.task_manager.mark_failed(task.id, error)
            return

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

        if result.success:
            print(f"   ✅ 任务完成!")
            self.task_manager.mark_completed(task.id)
        else:
            # 验证失败，调用 Orchestrator
            print(f"   🎭 验证未通过，调用 Orchestrator...")
            self.orchestrator.orchestrate(
                trigger=f"任务 [{task.id}] 验证失败",
                context=f"任务描述: {task.description}\n错误: {'; '.join(result.errors)}"
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

        print("\n" + self._get_git_context())

    def add_task_from_prompt(self, user_request: str):
        """根据用户自然语言描述生成并添加任务"""
        import json as json_module

        print("\n" + "=" * 60)
        print("🤖 分析需求，生成任务...")
        print("=" * 60)

        # 收集项目上下文
        context_parts = []
        print("   📂 收集项目上下文...")

        # 1. 读取 git log 获取历史
        git_log = self._get_git_context()
        if git_log and "暂无" not in git_log:
            context_parts.append(f"### 最近 Git 提交\n{git_log}")
            print("      \u2713 读取 git log")

        # 2. 获取现有任务描述
        existing_tasks = self.task_manager.get_all_tasks()
        if existing_tasks:
            task_list = "\n".join(
                [f"- [{t.id}] {t.description} ({t.status})" for t in existing_tasks]
            )
            context_parts.append(f"### 现有任务\n{task_list}")
            print(f"      ✓ 现有 {len(existing_tasks)} 个任务")

        # 3. 获取目录结构
        try:
            result = subprocess.run(
                ["find", ".", "-type", "f", "-name", "*.py", "-o", "-name", "*.js", "-o", "-name", "*.ts"],
                cwd=self.workspace_dir,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.stdout.strip():
                files = result.stdout.strip().split("\n")[:20]  # 最多 20 个文件
                context_parts.append(f"### 项目文件\n" + "\n".join(files))
                print(f"      ✓ 扫描到 {len(files)} 个代码文件")
        except:
            pass

        project_context = "\n\n".join(context_parts) if context_parts else "（新项目，暂无历史）"

        # 获取现有 ID
        existing_ids = [t.id for t in existing_tasks]
        ids_str = ", ".join(existing_ids) if existing_ids else "（暂无）"

        # 构建 prompt
        prompt = TASK_GENERATION_PROMPT.format(
            user_request=user_request,
            project_context=project_context,
            existing_ids=ids_str,
        )

        # 调用 Claude 生成任务（使用流式输出）
        print("\n   🧠 Claude 分析中...")
        print("   " + "-" * 40)

        try:
            process = subprocess.Popen(
                [
                    CLAUDE_CMD,
                    "-p",
                    "--verbose",
                    "--output-format", "stream-json",
                    "--dangerously-skip-permissions",
                    prompt,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=self.workspace_dir,
            )

            # 实时读取输出
            full_result = ""
            for line in process.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json_module.loads(line)
                    evt_type = event.get("type", "")

                    if evt_type == "assistant":
                        # 思考内容
                        content = event.get("message", {}).get("content", [])
                        for block in content:
                            if block.get("type") == "text":
                                text = block.get("text", "")
                                # 显示前 80 字符
                                preview = text[:80].replace("\n", " ")
                                if preview:
                                    print(f"   💭 {preview}...")

                    elif evt_type == "result":
                        full_result = event.get("result", "")
                        cost = event.get("total_cost_usd", 0)
                        print(f"   " + "-" * 40)
                        print(f"   💰 成本: ${cost:.4f}")

                except json_module.JSONDecodeError:
                    continue

            process.wait()

            if process.returncode != 0:
                stderr = process.stderr.read()
                print(f"❌ Claude 调用失败: {stderr}")
                return False

            # 提取 JSON
            json_start = full_result.find("[")
            json_end = full_result.rfind("]") + 1
            if json_start == -1 or json_end == 0:
                print(f"❌ 无法解析任务 JSON")
                print(f"   原始输出: {full_result[:200]}")
                return False

            tasks_data = json_module.loads(full_result[json_start:json_end])

            # 添加任务
            print("\n   📝 添加任务:")
            added_count = 0
            for task_dict in tasks_data:
                task = Task(
                    id=task_dict.get("id", f"auto_{len(existing_tasks) + added_count + 1}"),
                    description=task_dict.get("description", ""),
                    priority=task_dict.get("priority", 99),
                    steps=task_dict.get("steps", []),
                )
                self.task_manager.add_task(task)
                added_count += 1
                print(f"      ✅ [{task.id}] {task.description}")

            print(f"\n✅ 成功添加 {added_count} 个任务")
            print(f"   运行 'python3 main.py run' 开始执行")
            return True

        except json_module.JSONDecodeError as e:
            print(f"❌ JSON 解析失败: {e}")
            return False
        except Exception as e:
            print(f"❌ 生成失败: {e}")
            return False

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
        """根据用户需求，让 Claude 生成 tasks.json"""
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
        result = self._call_claude_for_creation(prompt)

        if result and "TASKS_CREATED" in result:
            # 校验生成的 tasks.json
            if self._validate_tasks_json():
                print("\n✅ tasks.json 生成成功！")

                # 显示生成的任务列表
                self._show_generated_tasks()

                # 询问用户是否提交
                confirm_commit = input("\n是否提交到 Git？(y/N): ").strip().lower()
                if confirm_commit == 'y':
                    self._git_commit("初始化任务列表")
                    print("✅ 已提交")
                else:
                    print("ℹ️  未提交，你可以稍后手动提交")

                return True
            else:
                print("\n❌ 生成的 tasks.json 格式无效")
                return False
        else:
            print("\n❌ 任务生成失败")
            return False

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

    def _call_claude_for_creation(self, prompt: str, timeout: int = 180):
        """调用 Claude Code 生成任务（流式输出）"""
        import json as json_module
        try:
            process = subprocess.Popen(
                [
                    CLAUDE_CMD,
                    "-p",
                    "--verbose",
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
                    event = json_module.loads(line)
                    evt_type = event.get("type", "")

                    if evt_type == "assistant":
                        # 显示思考过程摘要
                        content = event.get("message", {}).get("content", [])
                        for block in content:
                            if block.get("type") == "text":
                                text = block.get("text", "")
                                preview = text[:60].replace("\n", " ")
                                if preview:
                                    print(f"   💭 {preview}...")

                    elif evt_type == "result":
                        full_result = event.get("result", "")
                        cost = event.get("total_cost_usd", 0)
                        print(f"\n   💰 成本: ${cost:.4f}")

                except json_module.JSONDecodeError:
                    continue

            process.wait(timeout=timeout)
            return full_result

        except Exception as e:
            print(f"❌ 调用失败: {e}")
            return None

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

    # add 命令
    add_parser = subparsers.add_parser("add", help="根据描述新增任务")
    add_parser.add_argument("description", help="任务需求描述")

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
    elif args.command == "add":
        agent.add_task_from_prompt(args.description)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
