"""
长时间运行代理系统 - 统一成本追踪模块

负责：
- 记录各组件的 token 消耗
- 持久化成本历史
- 生成成本统计报告
- 处理非常规退出的成本估算
"""

import json
import os
from dataclasses import dataclass, field
from typing import List, Optional
from datetime import datetime
from enum import Enum


class CostSource(Enum):
    """成本来源"""
    WORKER = "worker"
    WORKER_CLEANUP = "worker_cleanup"
    SUPERVISOR = "supervisor"
    ORCHESTRATOR = "orchestrator"
    VALIDATOR = "validator"
    TASK_GENERATION = "task_generation"
    UNKNOWN = "unknown"


@dataclass
class CostRecord:
    """单次成本记录"""
    source: CostSource
    cost_usd: float
    task_id: Optional[str] = None
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    details: str = ""
    estimated: bool = False  # 是否为估算值


@dataclass
class CostSummary:
    """成本摘要"""
    total_cost: float = 0.0
    by_source: dict = field(default_factory=dict)
    records: List[CostRecord] = field(default_factory=list)
    estimated_cost: float = 0.0  # 估算的成本（非常规退出）

    def to_dict(self) -> dict:
        return {
            "total_cost": self.total_cost,
            "estimated_cost": self.estimated_cost,
            "confirmed_cost": self.total_cost - self.estimated_cost,
            "by_source": self.by_source,
            "record_count": len(self.records),
        }


class CostTracker:
    """全局成本追踪器"""

    def __init__(self, workspace_dir: str):
        self.workspace_dir = workspace_dir
        self.cost_dir = os.path.join(workspace_dir, ".claude_plus")
        self.cost_file = os.path.join(self.cost_dir, "cost_history.jsonl")
        self.records: List[CostRecord] = []
        self._ensure_dir()

    def _ensure_dir(self):
        os.makedirs(self.cost_dir, exist_ok=True)

    def add(
        self,
        source: CostSource,
        cost_usd: float,
        task_id: str = None,
        details: str = "",
        estimated: bool = False,
    ):
        """记录一次成本"""
        if cost_usd <= 0:
            return

        record = CostRecord(
            source=source,
            cost_usd=cost_usd,
            task_id=task_id,
            details=details,
            estimated=estimated,
        )
        self.records.append(record)

        # 追加写入文件（持久化）
        try:
            with open(self.cost_file, "a", encoding="utf-8") as f:
                f.write(
                    json.dumps(
                        {
                            "source": source.value,
                            "cost_usd": cost_usd,
                            "task_id": task_id,
                            "timestamp": record.timestamp,
                            "details": details,
                            "estimated": estimated,
                        }
                    )
                    + "\n"
                )
        except Exception:
            pass  # 持久化失败不影响主流程

    def get_summary(self) -> CostSummary:
        """获取成本摘要"""
        summary = CostSummary(records=self.records)

        for record in self.records:
            summary.total_cost += record.cost_usd
            if record.estimated:
                summary.estimated_cost += record.cost_usd

            source_name = record.source.value
            if source_name not in summary.by_source:
                summary.by_source[source_name] = 0.0
            summary.by_source[source_name] += record.cost_usd

        return summary

    def get_session_cost(self) -> float:
        """获取本次会话总成本"""
        return sum(r.cost_usd for r in self.records)

    def print_summary(self, show_details: bool = False):
        """打印成本摘要"""
        summary = self.get_summary()

        if summary.total_cost == 0:
            print("\n💰 本次运行无成本记录")
            return

        print("\n" + "=" * 50)
        print("💰 成本统计")
        print("=" * 50)

        # 按来源分类
        if summary.by_source:
            print("\n按来源分类:")
            source_icons = {
                "worker": "🔨",
                "worker_cleanup": "🧹",
                "supervisor": "👀",
                "orchestrator": "🎭",
                "validator": "✅",
                "task_generation": "📝",
                "unknown": "❓",
            }
            for source, cost in sorted(summary.by_source.items(), key=lambda x: -x[1]):
                icon = source_icons.get(source, "❓")
                print(f"   {icon} {source:20s}: ${cost:.4f}")

        print(f"\n{'─' * 40}")
        if summary.estimated_cost > 0:
            confirmed = summary.total_cost - summary.estimated_cost
            print(f"   确认成本:   ${confirmed:.4f}")
            print(f"   估算成本:   ${summary.estimated_cost:.4f} (非常规退出)")
            print(f"   {'─' * 30}")
        print(f"   \033[1m总成本:     ${summary.total_cost:.4f}\033[0m")
        print("=" * 50)

        # 显示详细记录
        if show_details and self.records:
            print("\n详细记录:")
            for i, record in enumerate(self.records[-10:], 1):  # 最近10条
                est_mark = " (估算)" if record.estimated else ""
                task_info = f" [{record.task_id}]" if record.task_id else ""
                print(
                    f"   {i}. {record.source.value}{task_info}: ${record.cost_usd:.4f}{est_mark}"
                )


def estimate_cost_from_log(log_file: str) -> float:
    """从不完整的日志中估算成本

    用于处理非常规退出（如 SIGKILL）的情况，
    此时 Claude CLI 可能没有输出 result 事件。

    估算策略：
    1. 优先使用 result 事件中的 total_cost_usd
    2. 其次从 usage 事件或 assistant 消息中提取 token 数计算
    3. 最后基于模型定价估算

    Returns:
        估算的成本（USD）
    """
    if not os.path.exists(log_file):
        return 0.0

    input_tokens = 0
    output_tokens = 0

    try:
        with open(log_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)

                    # 方法1: 如果有 result 事件，直接使用（最准确）
                    if event.get("type") == "result":
                        cost = event.get("total_cost_usd", 0.0)
                        if cost > 0:
                            return cost

                    # 方法2: 从 assistant 消息中提取 usage
                    if event.get("type") == "assistant":
                        message = event.get("message", {})
                        usage = message.get("usage", {})
                        if usage:
                            input_tokens = max(
                                input_tokens, usage.get("input_tokens", 0)
                            )
                            output_tokens = max(
                                output_tokens, usage.get("output_tokens", 0)
                            )

                except json.JSONDecodeError:
                    continue

    except Exception:
        return 0.0

    # 根据 Claude Sonnet 定价估算
    # Input: $3/MTok, Output: $15/MTok
    if input_tokens > 0 or output_tokens > 0:
        estimated_cost = (input_tokens * 3 + output_tokens * 15) / 1_000_000
        return estimated_cost

    return 0.0


def extract_cost_from_json_output(stdout: str) -> float:
    """从 Claude CLI 的 JSON 输出中提取成本

    适用于 --output-format json 模式

    Args:
        stdout: Claude CLI 的标准输出

    Returns:
        成本（USD），提取失败返回 0.0
    """
    try:
        output_data = json.loads(stdout)
        return output_data.get("total_cost_usd", 0.0)
    except (json.JSONDecodeError, TypeError):
        return 0.0


def extract_cost_from_stream_json(content: str) -> float:
    """从 stream-json 格式的输出中提取成本

    适用于 --output-format stream-json 模式

    Args:
        content: 包含多行 JSON 的内容

    Returns:
        成本（USD），提取失败返回 0.0
    """
    for line in content.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
            if event.get("type") == "result":
                return event.get("total_cost_usd", 0.0)
        except json.JSONDecodeError:
            continue
    return 0.0
