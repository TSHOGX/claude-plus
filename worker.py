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
from config import CLAUDE_CMD, SYSTEM_PROMPT_TEMPLATE, CLEANUP_PROMPT_TEMPLATE, TASK_PROMPT_TEMPLATE


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


@dataclass
class CleanupResult:
    """清理结果 - 包含交接摘要"""

    success: bool = False
    handover_summary: Optional[str] = None  # 交接摘要内容
    cleanup_done: bool = False  # 是否输出了 HANDOVER_END
    cost_usd: float = 0.0  # cleanup 阶段的 Claude 调用成本


class WorkerProcess:
    """Worker 进程封装 - 管理 Claude CLI 后台执行"""

    def __init__(self, task: Task, workspace_dir: str):
        self.task = task
        self.workspace_dir = workspace_dir
        self.process: Optional[subprocess.Popen] = None
        # 日志目录：.claude_plus/logs/
        self.logs_dir = os.path.join(workspace_dir, ".claude_plus", "logs")
        os.makedirs(self.logs_dir, exist_ok=True)
        # 日志文件名使用 task id
        self.log_file = os.path.join(self.logs_dir, f"worker_{task.id}.log")
        self.start_time: Optional[float] = None
        # 实时日志追踪
        self._last_log_position: int = 0
        self._last_event_count: int = 0

    def _build_system_prompt(self) -> str:
        """构建系统提示"""
        return SYSTEM_PROMPT_TEMPLATE

    def _build_task_prompt(self) -> str:
        """构建任务提示"""
        steps_text = "\n".join(f"- {step}" for step in self.task.steps)

        # 如果有 notes，添加上下文信息
        notes_section = ""
        if self.task.notes:
            notes_section = f"\n## 上次执行记录\n{self.task.notes}\n"

        return TASK_PROMPT_TEMPLATE.format(
            task_description=self.task.description,
            task_steps=steps_text if steps_text else "- 无具体步骤，请自行规划",
            notes_section=notes_section,
        )

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
        """终止进程及其所有子进程

        Args:
            graceful: True 使用 SIGINT（安全终止），False 使用 SIGKILL
        """
        if self.process is None or not self.is_alive():
            return True

        try:
            # 获取进程组 ID（由于使用了 start_new_session=True，PGID 就是主进程 PID）
            try:
                pgid = os.getpgid(self.process.pid)
            except (ProcessLookupError, OSError):
                return True  # 进程已不存在

            if graceful:
                # 向整个进程组发送 SIGINT
                try:
                    os.killpg(pgid, signal.SIGINT)
                except (ProcessLookupError, OSError):
                    pass
                # 等待最多 5 秒
                for _ in range(50):
                    if not self.is_alive():
                        return True
                    time.sleep(0.1)
                # 超时则强制终止整个进程组
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    pass
            else:
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    pass
            return True
        except Exception:
            return True

    def get_session_id(self) -> Optional[str]:
        """从日志中获取会话 ID"""
        log = self.read_log()
        return log.session_id

    def graceful_shutdown(self, reason: str = "用户请求终止") -> CleanupResult:
        """优雅关闭：先中断，然后恢复会话执行清理工作

        Returns:
            CleanupResult 包含清理状态和交接摘要
        """
        result = CleanupResult()

        # 1. 获取会话 ID（在终止前）
        session_id = self.get_session_id()

        # 2. 发送 SIGINT 中断当前工作
        if self.is_alive():
            try:
                pgid = os.getpgid(self.process.pid)
                os.killpg(pgid, signal.SIGINT)
            except (ProcessLookupError, OSError):
                pass

            # 等待进程结束
            for _ in range(30):  # 最多等待 3 秒
                if not self.is_alive():
                    break
                time.sleep(0.1)

        # 如果进程还在运行，强制终止
        if self.is_alive():
            self.terminate(graceful=False)

        # 3. 如果没有会话 ID，无法恢复清理
        if not session_id:
            print("      ⚠️  无法获取会话 ID，跳过清理步骤")
            return result

        # 4. 使用 --resume 恢复会话，发送清理指令
        print(f"      🧹 正在执行清理工作...")
        cleanup_prompt = CLEANUP_PROMPT_TEMPLATE.format(reason=reason)

        cleanup_log_file = self.log_file.replace(".log", "_cleanup.log")

        cmd = [
            CLAUDE_CMD,
            "-p",
            "--output-format", "stream-json",
            "--dangerously-skip-permissions",
            "--resume", session_id,
            cleanup_prompt,
        ]

        try:
            with open(cleanup_log_file, "w") as log_f:
                cleanup_proc = subprocess.Popen(
                    cmd,
                    stdout=log_f,
                    stderr=subprocess.STDOUT,
                    cwd=self.workspace_dir,
                    env={**os.environ, "NO_COLOR": "1"},
                    start_new_session=True,
                )

            # 等待清理完成
            cleanup_proc.wait()

            # 5. 解析清理日志，提取交接摘要
            result = self._parse_cleanup_log(cleanup_log_file)

            if result.cleanup_done:
                print(f"      ✅ 清理工作已完成")
                if result.handover_summary:
                    print(f"      📋 已提取交接摘要")
            else:
                print(f"      ⚠️  清理完成（未检测到 HANDOVER_END 标记）")
                result.success = True  # 即使没有标记也算成功

            return result

        except Exception as e:
            print(f"      ❌ 清理失败: {e}")
            return result

    def _parse_cleanup_log(self, cleanup_log_file: str) -> CleanupResult:
        """解析清理日志，提取交接摘要和成本"""
        result = CleanupResult()

        if not os.path.exists(cleanup_log_file):
            return result

        try:
            with open(cleanup_log_file, "r") as f:
                content = f.read()

            # 检查是否有 HANDOVER_END 标记（表示清理完成）
            result.cleanup_done = "HANDOVER_END" in content
            result.success = result.cleanup_done

            # 提取成本（从 stream-json 格式中）
            for line in content.split("\n"):
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                    if event.get("type") == "result":
                        result.cost_usd = event.get("total_cost_usd", 0.0)
                        break
                except json.JSONDecodeError:
                    continue

            # 提取交接摘要（在 HANDOVER_START 和 HANDOVER_END 之间）
            # 从 stream-json 中提取文本内容
            full_text = self._extract_text_from_stream_json(content)

            if "HANDOVER_START" in full_text and "HANDOVER_END" in full_text:
                start_marker = "HANDOVER_START"
                end_marker = "HANDOVER_END"

                start_idx = full_text.find(start_marker) + len(start_marker)
                end_idx = full_text.find(end_marker)

                if start_idx < end_idx:
                    # 清理标记周围的 ``` 符号
                    handover = full_text[start_idx:end_idx].strip()
                    # 移除可能的 ``` 包裹
                    handover = handover.strip("`").strip()
                    result.handover_summary = handover

        except Exception as e:
            print(f"      ⚠️  解析清理日志失败: {e}")

        return result

    def _extract_text_from_stream_json(self, content: str) -> str:
        """从 stream-json 格式中提取所有文本内容"""
        texts = []
        for line in content.split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
                if event.get("type") == "assistant":
                    message = event.get("message", {})
                    for block in message.get("content", []):
                        if block.get("type") == "text":
                            texts.append(block.get("text", ""))
            except json.JSONDecodeError:
                continue
        return "\n".join(texts)

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

    def read_new_events(self) -> List[dict]:
        """增量读取新事件（用于实时显示）

        Returns:
            新事件列表，每个事件格式为 {"type": "tool"|"text", ...}
        """
        if not os.path.exists(self.log_file):
            return []

        new_events = []
        try:
            with open(self.log_file, "r") as f:
                # 跳到上次读取位置
                f.seek(self._last_log_position)

                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                        parsed = self._parse_event_for_display(event)
                        if parsed:
                            new_events.append(parsed)
                    except json.JSONDecodeError:
                        continue

                # 更新位置
                self._last_log_position = f.tell()
        except Exception:
            pass

        return new_events

    def _parse_event_for_display(self, event: dict) -> Optional[dict]:
        """解析事件用于实时显示"""
        event_type = event.get("type", "")

        if event_type == "assistant":
            message = event.get("message", {})
            for block in message.get("content", []):
                block_type = block.get("type", "")
                if block_type == "text":
                    text = block.get("text", "").strip()
                    if text and len(text) > 10:  # 忽略太短的文本
                        return {"type": "text", "content": text[:100]}
                elif block_type == "tool_use":
                    tool_name = block.get("name", "unknown")
                    tool_input = block.get("input", {})
                    input_summary = self._summarize_tool_input(tool_name, tool_input)
                    return {"type": "tool", "name": tool_name, "input": input_summary}

        elif event_type == "result":
            is_error = event.get("is_error", False)
            result_text = event.get("result", "")[:50]
            return {"type": "result", "is_error": is_error, "result": result_text}

        return None

    def _summarize_tool_input(self, tool_name: str, tool_input: dict) -> str:
        """提取工具输入的简要摘要"""
        if not isinstance(tool_input, dict):
            return ""

        if tool_name == "Bash":
            cmd = tool_input.get("command", "")
            # 提取命令的关键部分
            if len(cmd) > 60:
                return cmd[:57] + "..."
            return cmd
        elif tool_name in ("Read", "Write", "Edit"):
            path = tool_input.get("file_path", "")
            return os.path.basename(path)
        elif tool_name == "Grep":
            pattern = tool_input.get("pattern", "")[:30]
            path = tool_input.get("path", "")
            if path:
                return f"{pattern} in {os.path.basename(path)}"
            return pattern
        elif tool_name == "Glob":
            return tool_input.get("pattern", "")[:40]
        elif tool_name == "Task":
            return tool_input.get("description", "")[:40]
        elif tool_name == "WebFetch":
            url = tool_input.get("url", "")
            # 提取域名
            if "://" in url:
                url = url.split("://")[1].split("/")[0]
            return url[:40]
        elif tool_name == "WebSearch":
            return tool_input.get("query", "")[:40]
        else:
            # 尝试获取第一个有意义的值
            for key in ["pattern", "query", "command", "file_path", "path"]:
                if key in tool_input:
                    val = str(tool_input[key])[:40]
                    return val
        return ""

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
