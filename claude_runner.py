"""
统一的 Claude CLI 调用模块
"""

import json
import os
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from config import CLAUDE_CMD, truncate_for_display, summarize_tool_input


@dataclass
class ClaudeResult:
    """Claude 调用结果"""
    session_id: Optional[str] = None
    result_text: str = ""
    cost_usd: float = 0.0
    is_error: bool = False
    duration_ms: int = 0


@dataclass
class EventCallbacks:
    """事件回调集合"""
    on_init: Optional[Callable[[str], None]] = None        # session_id
    on_text: Optional[Callable[[str], None]] = None        # 文本内容
    on_tool: Optional[Callable[[str, str], None]] = None   # (tool_name, input_summary)
    on_result: Optional[Callable[[str, float], None]] = None  # (result_text, cost_usd)


def make_printer(indent: int = 3, verbose: bool = True) -> EventCallbacks:
    """创建带指定缩进的打印回调集"""
    if not verbose:
        return EventCallbacks()

    prefix = " " * indent
    return EventCallbacks(
        on_text=lambda t: print(f"{prefix}💭 {t}"),
        on_tool=lambda n, i: print(f"{prefix}🔧 {n}: {i}" if i else f"{prefix}🔧 {n}"),
        on_result=lambda t, c: print(f"\n{prefix}💰 成本: ${c:.4f}"),
    )


def parse_event(
    event: dict,
    callbacks: EventCallbacks,
    result: ClaudeResult,
) -> None:
    """解析单个 stream-json 事件"""
    evt_type = event.get("type", "")

    if evt_type == "system" and event.get("subtype") == "init":
        result.session_id = event.get("session_id")
        if callbacks.on_init:
            callbacks.on_init(result.session_id)

    elif evt_type == "assistant":
        content = event.get("message", {}).get("content", [])
        for block in content:
            block_type = block.get("type", "")
            if block_type == "text":
                text = block.get("text", "")
                preview = truncate_for_display(text)
                if preview and callbacks.on_text:
                    callbacks.on_text(preview)
            elif block_type == "tool_use":
                tool_name = block.get("name", "")
                inp = summarize_tool_input(tool_name, block.get("input", {}))
                if callbacks.on_tool:
                    callbacks.on_tool(tool_name, inp)

    elif evt_type == "result":
        result.result_text = event.get("result", "")
        result.cost_usd = event.get("total_cost_usd", 0.0)
        result.is_error = event.get("is_error", False)
        result.duration_ms = event.get("duration_ms", 0)
        result.session_id = event.get("session_id", result.session_id)
        if callbacks.on_result:
            callbacks.on_result(result.result_text, result.cost_usd)


def build_command(
    prompt: str,
    *,
    resume_session_id: Optional[str] = None,
    system_prompt: Optional[str] = None,
    verbose: bool = True,
) -> list[str]:
    """构建 Claude CLI 命令"""
    cmd = [CLAUDE_CMD, "-p"]

    if verbose:
        cmd.append("--verbose")

    cmd.extend(["--output-format", "stream-json"])
    cmd.append("--dangerously-skip-permissions")

    if resume_session_id:
        cmd.extend(["--resume", resume_session_id])

    if system_prompt:
        cmd.extend(["--append-system-prompt", system_prompt])

    cmd.append(prompt)
    return cmd


def run_claude(
    prompt: str,
    *,
    workspace_dir: str,
    resume_session_id: Optional[str] = None,
    system_prompt: Optional[str] = None,
    verbose: bool = True,
    callbacks: Optional[EventCallbacks] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> ClaudeResult:
    """
    执行 Claude CLI 并返回结果（管道模式，实时处理输出）

    Args:
        prompt: 提示词
        workspace_dir: 工作目录
        resume_session_id: 恢复的 session ID（可选）
        system_prompt: 追加的系统提示（可选）
        verbose: 是否显示详细输出
        callbacks: 事件回调（可选，默认使用 make_printer）
        cancel_check: 取消检查函数，返回 True 表示取消
    """
    if callbacks is None:
        callbacks = make_printer(indent=3, verbose=verbose)

    cmd = build_command(
        prompt,
        resume_session_id=resume_session_id,
        system_prompt=system_prompt,
        verbose=verbose,
    )

    result = ClaudeResult()
    process = None

    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=workspace_dir,
        )

        for line in process.stdout:
            # 检查取消
            if cancel_check and cancel_check():
                process.terminate()
                result.is_error = True
                result.result_text = "已取消"
                return result

            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
                parse_event(event, callbacks, result)
            except json.JSONDecodeError:
                continue

        process.wait()

        if process.returncode != 0:
            stderr = process.stderr.read() if process.stderr else ""
            result.is_error = True
            if not result.result_text:
                result.result_text = f"Claude 调用失败: {stderr[:200]}"

    except Exception as e:
        result.is_error = True
        result.result_text = f"调用异常: {e}"

    return result


def start_claude_background(
    prompt: str,
    *,
    workspace_dir: str,
    log_file: Path,
    system_prompt: Optional[str] = None,
    verbose: bool = True,
) -> subprocess.Popen:
    """
    后台启动 Claude CLI，输出写入文件（用于 Worker）

    Returns:
        Popen 对象，调用方负责管理进程生命周期
    """
    cmd = build_command(
        prompt,
        system_prompt=system_prompt,
        verbose=verbose,
    )

    # 使用 shell 重定向避免文件句柄泄漏
    shell_cmd = " ".join(shlex.quote(arg) for arg in cmd)
    process = subprocess.Popen(
        f"{shell_cmd} > {shlex.quote(str(log_file))} 2>&1",
        shell=True,
        cwd=workspace_dir,
        env={**os.environ, "NO_COLOR": "1"},
        start_new_session=True,
    )

    return process


def resume_claude_background(
    prompt: str,
    *,
    workspace_dir: str,
    log_file: Path,
    session_id: str,
) -> subprocess.Popen:
    """
    后台恢复 Claude session，输出写入文件（用于 Worker 清理）
    """
    cmd = build_command(
        prompt,
        resume_session_id=session_id,
        verbose=False,
    )

    # 使用 shell 重定向避免文件句柄泄漏
    shell_cmd = " ".join(shlex.quote(arg) for arg in cmd)
    process = subprocess.Popen(
        f"{shell_cmd} > {shlex.quote(str(log_file))} 2>&1",
        shell=True,
        cwd=workspace_dir,
        env={**os.environ, "NO_COLOR": "1"},
        start_new_session=True,
    )

    return process


@dataclass
class ParsedLog:
    """解析后的日志结构"""
    session_id: Optional[str] = None
    model: Optional[str] = None
    events: list = field(default_factory=list)
    result: str = ""
    cost_usd: float = 0.0
    duration_ms: int = 0
    is_complete: bool = False
    is_error: bool = False


def parse_log_file(log_file: Path) -> ParsedLog:
    """解析 Claude 日志文件"""
    result = ParsedLog()

    if not log_file.exists():
        return result

    with open(log_file, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
                _parse_log_event(event, result)
            except json.JSONDecodeError:
                continue

    return result


def _parse_log_event(event: dict, result: ParsedLog) -> None:
    """解析单个日志事件（用于完整日志解析）"""
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
                    display_text = truncate_for_display(text)
                    # 避免重复
                    if not result.events or result.events[-1].get("content") != display_text:
                        result.events.append({"type": "text", "content": display_text})
            elif block_type == "tool_use":
                tool_name = block.get("name", "unknown")
                tool_input = block.get("input", {})
                input_summary = summarize_tool_input(tool_name, tool_input)
                result.events.append({"type": "tool", "name": tool_name, "input": input_summary})

    elif event_type == "result":
        result.is_complete = True
        result.is_error = event.get("is_error", False)
        result.result = event.get("result", "")
        result.cost_usd = event.get("total_cost_usd", 0.0)
        result.duration_ms = event.get("duration_ms", 0)
        result.session_id = event.get("session_id", result.session_id)


class IncrementalLogReader:
    """增量日志读取器（用于实时显示）"""

    def __init__(self, log_file: Path):
        self.log_file = log_file
        self._last_position = 0

    def read_new_events(self) -> list[dict]:
        """读取新事件"""
        if not self.log_file.exists():
            return []

        new_events = []
        try:
            with open(self.log_file, "r") as f:
                f.seek(self._last_position)
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                        parsed = self._parse_for_display(event)
                        if parsed:
                            new_events.append(parsed)
                    except json.JSONDecodeError:
                        continue
                self._last_position = f.tell()
        except Exception:
            pass

        return new_events

    def _parse_for_display(self, event: dict) -> Optional[dict]:
        """解析事件用于显示"""
        event_type = event.get("type", "")

        if event_type == "assistant":
            message = event.get("message", {})
            for block in message.get("content", []):
                block_type = block.get("type", "")
                if block_type == "text":
                    text = block.get("text", "").strip()
                    if text and len(text) > 10:
                        return {"type": "text", "content": truncate_for_display(text)}
                elif block_type == "tool_use":
                    tool_name = block.get("name", "unknown")
                    tool_input = block.get("input", {})
                    input_summary = summarize_tool_input(tool_name, tool_input)
                    return {"type": "tool", "name": tool_name, "input": input_summary}

        elif event_type == "result":
            is_error = event.get("is_error", False)
            result_text = event.get("result", "")
            return {"type": "result", "is_error": is_error, "result": truncate_for_display(result_text)}

        return None
