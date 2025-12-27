"""
长时间运行代理系统 - Supervisor 模块

Supervisor 负责：
- 定期检查 Worker 进度
- 分析执行情况
- 决策：继续等待 / 分裂任务 / 调整策略
"""

import subprocess
import json
from dataclasses import dataclass
from typing import Optional, List
from enum import Enum
from task_manager import Task
from worker import WorkerProcess
from config import CLAUDE_CMD


class Decision(Enum):
    """Supervisor 决策"""

    CONTINUE = "continue"  # 继续等待
    SPLIT = "split"  # 任务太复杂，需要分裂
    WAIT_BACKGROUND = "wait"  # 需要长时间等待（训练等），转后台
    INTERVENE = "intervene"  # 需要人工介入


@dataclass
class SupervisorResult:
    """Supervisor 分析结果"""

    decision: Decision
    reason: str
    subtasks: Optional[List[dict]] = None  # 如果是 SPLIT，包含子任务
    suggestion: Optional[str] = None  # 如果是 INTERVENE，包含建议


# Supervisor 分析提示模板（改进版）
SUPERVISOR_PROMPT = """你是 Agent 执行监督者，负责分析 Worker 进度并做出决策。

## 任务信息
- 描述: {task_description}
- 已运行: {elapsed_time}
- 检查次数: {check_count}

## 执行流程
{worker_summary}

## 决策标准

### continue（继续）- 默认选择
- Agent 有新的工具调用或思考
- 正在进行有意义的调试
- 长时间操作有进展迹象（如训练 loss 在变化）

### split（拆分）
- 陷入循环（重复相同操作 5+ 次）
- 任务范围明显过大
- Agent 表示需要分步处理

### wait（后台）
- 执行长时间操作（训练/构建/下载）
- 无需实时交互
- 已设置等待进程完成

### intervene（人工介入）
- 遇到无法解决的阻塞
- 需要用户凭证/确认/决策
- 系统级错误（权限/网络等）

## 输出 JSON
{{"decision": "continue|split|wait|intervene", "reason": "简要原因（20字内）"}}
"""


class Supervisor:
    """任务执行监督者"""

    def __init__(self, workspace_dir: str, verbose: bool = True):
        self.workspace_dir = workspace_dir
        self.verbose = verbose

    def analyze(
        self, task: Task, worker: WorkerProcess, check_count: int = 0, elapsed: float = 0
    ) -> SupervisorResult:
        """分析 Worker 执行情况并做出决策"""
        # 获取 Worker 日志摘要
        worker_summary = worker.get_log_summary()

        # 格式化运行时长
        hours = int(elapsed // 3600)
        minutes = int((elapsed % 3600) // 60)
        secs = int(elapsed % 60)
        elapsed_time = f"{hours:02d}:{minutes:02d}:{secs:02d}"

        # 构建分析提示
        prompt = SUPERVISOR_PROMPT.format(
            task_description=task.description,
            worker_summary=worker_summary,
            check_count=check_count,
            elapsed_time=elapsed_time,
        )

        if self.verbose:
            print(f"   🔍 Supervisor 分析中...")

        # 调用 Claude 分析
        try:
            result = subprocess.run(
                [
                    CLAUDE_CMD,
                    "-p",
                    "--output-format",
                    "json",
                    "--dangerously-skip-permissions",
                    prompt,
                ],
                capture_output=True,
                text=True,
                timeout=60,
                cwd=self.workspace_dir,
            )

            output_data = json.loads(result.stdout)
            output_text = output_data.get("result", "")

            # 解析 JSON 响应
            return self._parse_response(output_text)

        except subprocess.TimeoutExpired:
            if self.verbose:
                print(f"   ⚠️  Supervisor 分析超时")
            return SupervisorResult(
                decision=Decision.CONTINUE, reason="分析超时，继续等待"
            )
        except Exception as e:
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
                "split": Decision.SPLIT,
                "wait": Decision.WAIT_BACKGROUND,
                "intervene": Decision.INTERVENE,
            }.get(decision_str, Decision.CONTINUE)

            subtasks = None
            if decision == Decision.SPLIT:
                subtasks = data.get("subtasks", [])
                # 验证子任务格式
                if not subtasks or not isinstance(subtasks, list):
                    return SupervisorResult(
                        decision=Decision.CONTINUE, reason="分裂任务格式错误，继续等待"
                    )

            suggestion = (
                data.get("suggestion") if decision == Decision.INTERVENE else None
            )

            return SupervisorResult(
                decision=decision,
                reason=reason,
                subtasks=subtasks,
                suggestion=suggestion,
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
