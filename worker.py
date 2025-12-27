"""
长时间运行代理系统 - Worker 模块

WorkerProcess 封装 Claude CLI 的后台执行，提供：
- 启动任务（输出到日志文件）
- 读取/解析日志
- 安全终止 (SIGINT)
- 状态检查
"""

import os
import json
import signal
import subprocess
import time
from dataclasses import dataclass, field
from typing import Optional, List
from task_manager import Task
from config import CLAUDE_CMD, SYSTEM_PROMPT_TEMPLATE


@dataclass
class WorkerLog:
    """Worker 日志解析结果 - 按时序记录事件流"""

    session_id: Optional[str] = None
    model: Optional[str] = None
    events: List[dict] = field(
        default_factory=list
    )  # 时序事件流 [{"type": "text/tool", ...}]
    is_complete: bool = False
    is_error: bool = False
    result: Optional[str] = None
    cost_usd: float = 0.0
    duration_ms: int = 0


class WorkerProcess:
    """Worker 进程封装 - 管理 Claude CLI 后台执行"""

    def __init__(self, task: Task, workspace_dir: str, recent_progress: str = ""):
        self.task = task
        self.workspace_dir = workspace_dir
        self.recent_progress = recent_progress
        self.process: Optional[subprocess.Popen] = None
        self.log_file = os.path.join(workspace_dir, f".worker_{task.id}.log")
        self.start_time: Optional[float] = None

    def _build_system_prompt(self) -> str:
        """构建系统提示"""
        steps_text = "\n".join(
            f"  {i+1}. {step}" for i, step in enumerate(self.task.steps)
        )
        return SYSTEM_PROMPT_TEMPLATE.format(
            task_description=self.task.description,
            task_steps=steps_text if steps_text else "无具体步骤，请自行规划",
            recent_progress=(
                self.recent_progress if self.recent_progress else "这是第一个任务"
            ),
            workspace_dir=self.workspace_dir,
        )

    def _build_task_prompt(self) -> str:
        """构建任务提示"""
        return f"""请执行以下任务：

## 任务 ID: {self.task.id}
## 描述: {self.task.description}

## 步骤:
{chr(10).join(f"- {step}" for step in self.task.steps)}

请开始执行，完成后输出 TASK_COMPLETED，遇到问题输出 TASK_BLOCKED: <原因>。
"""

    def start(self) -> int:
        """启动 Worker 进程，返回 PID"""
        system_prompt = self._build_system_prompt()
        task_prompt = self._build_task_prompt()

        cmd = [
            CLAUDE_CMD,
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--dangerously-skip-permissions",
            "--append-system-prompt",
            system_prompt,
            task_prompt,
        ]

        # 打开日志文件
        log_f = open(self.log_file, "w")

        self.process = subprocess.Popen(
            cmd,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            cwd=self.workspace_dir,
            env={**os.environ, "NO_COLOR": "1"},
            start_new_session=True,  # 独立进程组
        )

        self.start_time = time.time()
        return self.process.pid

    def is_alive(self) -> bool:
        """检查进程是否存活"""
        if self.process is None:
            return False
        return self.process.poll() is None

    def elapsed_seconds(self) -> float:
        """返回已运行时间（秒）"""
        if self.start_time is None:
            return 0
        return time.time() - self.start_time

    def terminate(self, graceful: bool = True) -> bool:
        """终止进程

        Args:
            graceful: True 使用 SIGINT（安全终止），False 使用 SIGKILL
        """
        if self.process is None or not self.is_alive():
            return True

        try:
            if graceful:
                # SIGINT 让 Claude CLI 优雅退出
                os.kill(self.process.pid, signal.SIGINT)
                # 等待最多 5 秒
                for _ in range(50):
                    if not self.is_alive():
                        return True
                    time.sleep(0.1)
                # 超时则强制终止
                os.kill(self.process.pid, signal.SIGKILL)
            else:
                os.kill(self.process.pid, signal.SIGKILL)
            return True
        except (ProcessLookupError, OSError):
            return True  # 进程已不存在

    def read_log(self) -> WorkerLog:
        """读取并解析日志文件"""
        result = WorkerLog()

        if not os.path.exists(self.log_file):
            return result

        try:
            with open(self.log_file, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                        self._parse_event(event, result)
                    except json.JSONDecodeError:
                        continue
        except Exception:
            pass

        return result

    def _parse_event(self, event: dict, result: WorkerLog):
        """解析单个 stream-json 事件，按时序记录"""
        event_type = event.get("type", "")

        if event_type == "system":
            subtype = event.get("subtype", "")
            if subtype == "init":
                result.session_id = event.get("session_id")
                result.model = event.get("model")

        elif event_type == "assistant":
            message = event.get("message", {})
            for block in message.get("content", []):
                block_type = block.get("type", "")
                if block_type == "text":
                    text = block.get("text", "").strip()
                    if text:
                        # 避免重复添加相同的文本（流式更新可能重复）
                        if (
                            not result.events
                            or result.events[-1].get("content") != text[:150]
                        ):
                            result.events.append(
                                {"type": "text", "content": text[:150]}  # 截断长文本
                            )
                elif block_type == "tool_use":
                    tool_name = block.get("name", "unknown")
                    tool_input = block.get("input", {})

                    # 提取简要输入信息
                    input_summary = ""
                    if isinstance(tool_input, dict):
                        if tool_name == "Bash":
                            input_summary = tool_input.get("command", "")[:80]
                        elif tool_name in ("Read", "Write", "Edit"):
                            path = tool_input.get("file_path", "")
                            input_summary = os.path.basename(path)
                        elif tool_name == "Grep":
                            input_summary = tool_input.get("pattern", "")[:50]
                        elif tool_name == "Glob":
                            input_summary = tool_input.get("pattern", "")[:50]

                    result.events.append(
                        {"type": "tool", "name": tool_name, "input": input_summary}
                    )

        elif event_type == "result":
            result.is_complete = True
            result.is_error = event.get("is_error", False)
            result.result = event.get("result", "")
            result.cost_usd = event.get("total_cost_usd", 0.0)
            result.duration_ms = event.get("duration_ms", 0)
            result.session_id = event.get("session_id", result.session_id)

    def get_log_summary(self, max_events: int = 30) -> str:
        """获取日志摘要（用于 Supervisor 分析）- 按时序展示执行流程"""
        log = self.read_log()

        lines = []
        lines.append(f"运行时间: {self.elapsed_seconds():.0f}秒")

        if log.model:
            lines.append(f"模型: {log.model}")

        if log.events:
            lines.append(f"\n执行流程 (共{len(log.events)}条，最近{max_events}条):")
            for evt in log.events[-max_events:]:
                if evt["type"] == "text":
                    # 文本输出：显示思考内容
                    content = evt["content"]
                    lines.append(f"💬 {content}")
                elif evt["type"] == "tool":
                    # 工具调用
                    name = evt["name"]
                    inp = evt.get("input", "")
                    if inp:
                        lines.append(f"🔧 {name}: {inp}")
                    else:
                        lines.append(f"🔧 {name}")

        if log.is_complete:
            status = "错误" if log.is_error else "完成"
            lines.append(f"\n状态: {status}")
            lines.append(f"成本: ${log.cost_usd:.4f}")

        return "\n".join(lines)

    def get_result(self) -> WorkerLog:
        """获取最终结果（进程结束后调用）"""
        return self.read_log()

    def cleanup(self):
        """清理日志文件"""
        if os.path.exists(self.log_file):
            os.remove(self.log_file)
