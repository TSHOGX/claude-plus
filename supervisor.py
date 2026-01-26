"""
长时间运行代理系统 - Supervisor 模块

Supervisor 负责：
- 定期检查 Worker 进度
- 分析执行情况
- 决策：继续等待 / 调用编排器
"""

import subprocess
import json
from dataclasses import dataclass
from typing import Optional, List
from enum import Enum
from task_manager import Task
from worker import WorkerProcess
from config import CLAUDE_CMD, SUPERVISOR_PROMPT


class Decision(Enum):
    """Supervisor 决策"""

    CONTINUE = "continue"      # 继续等待
    ORCHESTRATE = "orchestrate" # 需要调用任务编排器


@dataclass
class SupervisorResult:
    """Supervisor 分析结果"""

    decision: Decision
    reason: str
    cost_usd: float = 0.0  # Claude 调用成本


class Supervisor:
    """任务执行监督者"""

    def __init__(self, workspace_dir: str, verbose: bool = True):
        self.workspace_dir = workspace_dir
        self.verbose = verbose
        self._current_process: subprocess.Popen = None
        self._cancelled = False

    def cancel(self):
        """取消正在进行的分析"""
        self._cancelled = True
        if self._current_process and self._current_process.poll() is None:
            if self.verbose:
                print(f"   🛑 Supervisor 分析已取消")
            self._current_process.terminate()
            try:
                self._current_process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._current_process.kill()

    def analyze(
        self, task: Task, worker: WorkerProcess, check_count: int = 0, elapsed: float = 0
    ) -> SupervisorResult:
        """分析 Worker 执行情况并做出决策"""
        self._cancelled = False

        # 格式化运行时长
        hours = int(elapsed // 3600)
        minutes = int((elapsed % 3600) // 60)
        secs = int(elapsed % 60)
        elapsed_time = f"{hours:02d}:{minutes:02d}:{secs:02d}"

        # 构建分析提示 - 精简版，让 Claude Code 自己读日志
        prompt = SUPERVISOR_PROMPT.format(
            task_description=task.description,
            elapsed_time=elapsed_time,
            log_file=worker.log_file,
        )

        if self.verbose:
            print(f"   🔍 Supervisor 分析中...")

        # 调用 Claude 分析（不设超时，支持取消）
        try:
            self._current_process = subprocess.Popen(
                [
                    CLAUDE_CMD,
                    "-p",
                    "--output-format",
                    "json",
                    "--dangerously-skip-permissions",
                    prompt,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=self.workspace_dir,
            )

            stdout, _ = self._current_process.communicate()
            self._current_process = None

            # 检查是否被取消
            if self._cancelled:
                return SupervisorResult(decision=Decision.CONTINUE, reason="分析被取消")

            output_data = json.loads(stdout)
            output_text = output_data.get("result", "")
            cost_usd = output_data.get("total_cost_usd", 0.0)

            # 解析 JSON 响应
            result = self._parse_response(output_text)
            result.cost_usd = cost_usd
            return result

        except Exception as e:
            self._current_process = None
            if self._cancelled:
                return SupervisorResult(decision=Decision.CONTINUE, reason="分析被取消")
            if self.verbose:
                print(f"   ⚠️  Supervisor 分析失败: {e}")
            return SupervisorResult(decision=Decision.CONTINUE, reason=f"分析失败: {e}")

    def _parse_response(self, text: str) -> SupervisorResult:
        """解析 Supervisor 响应"""
        # 提取 JSON 部分
        json_start = text.find("{")
        json_end = text.rfind("}") + 1

        if json_start == -1 or json_end == 0:
            return SupervisorResult(
                decision=Decision.CONTINUE, reason="无法解析响应，继续等待"
            )

        try:
            data = json.loads(text[json_start:json_end])
            decision_str = data.get("decision", "continue").lower()
            reason = data.get("reason", "")

            decision = {
                "continue": Decision.CONTINUE,
                "orchestrate": Decision.ORCHESTRATE,
            }.get(decision_str, Decision.CONTINUE)

            return SupervisorResult(
                decision=decision,
                reason=reason,
            )

        except json.JSONDecodeError:
            return SupervisorResult(
                decision=Decision.CONTINUE, reason="JSON 解析失败，继续等待"
            )

    def quick_check(self, worker: WorkerProcess) -> bool:
        """快速检查 Worker 是否有明显问题（不调用 Claude）

        返回 True 表示需要详细分析，False 表示继续等待
        """
        log = worker.read_log()

        # 如果已完成，无需分析
        if log.is_complete:
            return False

        # 提取工具调用事件
        tool_events = [e for e in log.events if e.get("type") == "tool"]

        # 检查是否有重复的工具调用（可能陷入循环）
        if len(tool_events) >= 10:
            recent_tools = [
                f"{e['name']}:{e.get('input', '')}" for e in tool_events[-10:]
            ]
            unique_tools = set(recent_tools)
            if len(unique_tools) <= 3:
                return True  # 可能陷入循环

        return False
