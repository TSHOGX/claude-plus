"""
长时间运行代理系统 - 会话运行器模块
"""

import subprocess
import json
import os
import sys
import time
from dataclasses import dataclass
from typing import Optional
from task_manager import Task
from config import (
    CLAUDE_CMD,
    SESSION_TIMEOUT,
    SYSTEM_PROMPT_TEMPLATE,
    COMPLETION_MARKERS
)


@dataclass
class SessionResult:
    """会话运行结果"""
    success: bool
    session_id: Optional[str]
    output: str
    error: Optional[str]
    status: str  # completed, blocked, failed, timeout
    cost_usd: float = 0.0
    duration_ms: int = 0

    def is_completed(self) -> bool:
        return self.status == "completed"

    def is_blocked(self) -> bool:
        return self.status == "blocked"


class SessionRunner:
    """Claude 会话运行器 - 封装 claude CLI 调用"""

    def __init__(self, workspace_dir: str, verbose: bool = True):
        self.workspace_dir = workspace_dir
        self.timeout = SESSION_TIMEOUT
        self.verbose = verbose  # 是否显示实时输出

    def build_system_prompt(self, task: Task, recent_progress: str = "") -> str:
        """构建系统提示"""
        steps_text = "\n".join(f"  {i+1}. {step}" for i, step in enumerate(task.steps))
        return SYSTEM_PROMPT_TEMPLATE.format(
            task_description=task.description,
            task_steps=steps_text if steps_text else "无具体步骤，请自行规划",
            recent_progress=recent_progress if recent_progress else "这是第一个任务",
            workspace_dir=self.workspace_dir
        )

    def build_task_prompt(self, task: Task) -> str:
        """构建任务提示"""
        return f"""请执行以下任务：

## 任务 ID: {task.id}
## 描述: {task.description}

## 步骤:
{chr(10).join(f"- {step}" for step in task.steps)}

请开始执行，完成后输出 TASK_COMPLETED，遇到问题输出 TASK_BLOCKED: <原因>。
"""

    def run_session(
        self,
        task: Task,
        recent_progress: str = "",
        continue_session: bool = False,
        session_id: Optional[str] = None
    ) -> SessionResult:
        """运行 Claude 会话处理任务"""

        # 构建命令
        cmd = [CLAUDE_CMD, "-p"]

        # 输出格式：verbose 模式使用流式 JSON，否则使用普通 JSON
        if self.verbose:
            cmd.extend(["--output-format", "stream-json", "--verbose"])
        else:
            cmd.extend(["--output-format", "json"])

        # 在非交互模式下跳过权限检查（仅在受信任的工作目录中使用）
        cmd.append("--dangerously-skip-permissions")

        # 如果是继续会话
        if continue_session and session_id:
            cmd.extend(["-r", session_id])
        else:
            # 添加系统提示
            system_prompt = self.build_system_prompt(task, recent_progress)
            cmd.extend(["--append-system-prompt", system_prompt])

        # 添加任务提示
        task_prompt = self.build_task_prompt(task)
        cmd.append(task_prompt)

        try:
            if self.verbose:
                # 流式执行，实时显示进度
                return self._run_streaming_session(cmd)
            else:
                # 静默执行
                return self._run_silent_session(cmd)

        except subprocess.TimeoutExpired:
            return SessionResult(
                success=False,
                session_id=None,
                output="",
                error=f"会话超时（{self.timeout}秒）",
                status="timeout"
            )
        except FileNotFoundError:
            return SessionResult(
                success=False,
                session_id=None,
                output="",
                error=f"未找到 claude 命令，请确保已安装 Claude Code CLI",
                status="failed"
            )
        except Exception as e:
            return SessionResult(
                success=False,
                session_id=None,
                output="",
                error=f"运行会话时发生错误: {str(e)}",
                status="failed"
            )

    def _run_streaming_session(self, cmd: list) -> SessionResult:
        """流式执行会话，实时显示进度"""
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=self.workspace_dir,
            env={**os.environ, "NO_COLOR": "1"}
        )

        # 收集所有输出用于最后解析
        result_data = {}
        last_text = ""

        print("   ┌─ Claude 执行中 ─────────────────────────")

        start_time = time.time()
        try:
            while True:
                # 检查超时
                if time.time() - start_time > self.timeout:
                    process.kill()
                    raise subprocess.TimeoutExpired(cmd, self.timeout)

                line = process.stdout.readline()
                if not line and process.poll() is not None:
                    break

                if line:
                    line = line.strip()
                    if not line:
                        continue

                    try:
                        event = json.loads(line)
                        event_type = event.get("type", "")

                        if event_type == "system":
                            # 初始化事件
                            subtype = event.get("subtype", "")
                            if subtype == "init":
                                model = event.get("model", "unknown")
                                print(f"   │ ⚙️  模型: {model}")

                        elif event_type == "assistant":
                            # Claude 的响应
                            message = event.get("message", {})
                            content = message.get("content", [])
                            for block in content:
                                block_type = block.get("type", "")
                                if block_type == "text":
                                    text = block.get("text", "")
                                    # 只显示新增的文本
                                    if text and text != last_text:
                                        if text.startswith(last_text):
                                            new_text = text[len(last_text):]
                                        else:
                                            new_text = text
                                        if new_text.strip():
                                            # 截断长文本
                                            display = new_text[:120].replace('\n', ' ').strip()
                                            if len(new_text) > 120:
                                                display += "..."
                                            if display:
                                                print(f"   │ 💬 {display}")
                                        last_text = text
                                elif block_type == "tool_use":
                                    tool_name = block.get("name", "unknown")
                                    print(f"   │ 🔧 调用工具: {tool_name}")

                        elif event_type == "result":
                            # 最终结果
                            result_data = event

                    except json.JSONDecodeError:
                        pass

        except subprocess.TimeoutExpired:
            raise

        # 读取剩余输出
        remaining = process.stdout.read()
        if remaining:
            for line in remaining.strip().split('\n'):
                if line:
                    try:
                        event = json.loads(line)
                        if event.get("type") == "result":
                            result_data = event
                    except json.JSONDecodeError:
                        pass

        elapsed = time.time() - start_time
        print(f"   └─ 执行完成 ({elapsed:.1f}s) ────────────────────")

        # 解析结果
        if result_data:
            session_id = result_data.get("session_id")
            output_text = result_data.get("result", "")
            cost = result_data.get("total_cost_usd", 0.0)
            duration = result_data.get("duration_ms", 0)
            is_error = result_data.get("is_error", False)

            if is_error:
                return SessionResult(
                    success=False,
                    session_id=session_id,
                    output=output_text,
                    error=output_text,
                    status="failed",
                    cost_usd=cost,
                    duration_ms=duration
                )

            status = self._parse_completion_status(output_text)
            return SessionResult(
                success=status == "completed",
                session_id=session_id,
                output=output_text,
                error=None if status == "completed" else self._extract_error(output_text, status),
                status=status,
                cost_usd=cost,
                duration_ms=duration
            )
        else:
            return SessionResult(
                success=False,
                session_id=None,
                output="",
                error="未收到 Claude 的结果",
                status="failed"
            )

    def _run_silent_session(self, cmd: list) -> SessionResult:
        """静默执行会话"""
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=self.timeout,
            cwd=self.workspace_dir,
            env={**os.environ, "NO_COLOR": "1"}
        )

        try:
            output_data = json.loads(result.stdout)
            session_id = output_data.get("session_id")
            output_text = output_data.get("result", "")
            cost = output_data.get("total_cost_usd", 0.0)
            duration = output_data.get("duration_ms", 0)
            is_error = output_data.get("is_error", False)

            if is_error:
                return SessionResult(
                    success=False,
                    session_id=session_id,
                    output=output_text,
                    error=output_text,
                    status="failed",
                    cost_usd=cost,
                    duration_ms=duration
                )

            status = self._parse_completion_status(output_text)
            return SessionResult(
                success=status == "completed",
                session_id=session_id,
                output=output_text,
                error=None if status == "completed" else self._extract_error(output_text, status),
                status=status,
                cost_usd=cost,
                duration_ms=duration
            )

        except json.JSONDecodeError:
            return SessionResult(
                success=False,
                session_id=None,
                output=result.stdout,
                error=f"无法解析 Claude 输出: {result.stderr}",
                status="failed"
            )

    def _parse_completion_status(self, output: str) -> str:
        """解析完成状态"""
        if COMPLETION_MARKERS["success"] in output:
            return "completed"
        elif COMPLETION_MARKERS["blocked"] in output:
            return "blocked"
        elif COMPLETION_MARKERS["error"] in output:
            return "failed"
        else:
            # 如果没有明确标记，假设任务完成（可能需要验证）
            return "completed"

    def _extract_error(self, output: str, status: str) -> str:
        """提取错误信息"""
        if status == "blocked":
            marker = COMPLETION_MARKERS["blocked"]
            if marker in output:
                idx = output.index(marker)
                return output[idx + len(marker):].split("\n")[0].strip()
        elif status == "failed":
            marker = COMPLETION_MARKERS["error"]
            if marker in output:
                idx = output.index(marker)
                return output[idx + len(marker):].split("\n")[0].strip()
        return "未知错误"

    def continue_session(self, session_id: str, prompt: str) -> SessionResult:
        """继续现有会话"""
        if self.verbose:
            output_format_args = ["--output-format", "stream-json", "--verbose"]
        else:
            output_format_args = ["--output-format", "json"]

        cmd = [
            CLAUDE_CMD, "-p",
            *output_format_args,
            "--dangerously-skip-permissions",
            "-r", session_id,
            prompt
        ]

        try:
            if self.verbose:
                return self._run_streaming_session(cmd)
            else:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                    cwd=self.workspace_dir
                )

                try:
                    output_data = json.loads(result.stdout)
                    return SessionResult(
                        success=not output_data.get("is_error", False),
                        session_id=output_data.get("session_id"),
                        output=output_data.get("result", ""),
                        error=None,
                        status="completed" if not output_data.get("is_error") else "failed",
                        cost_usd=output_data.get("total_cost_usd", 0.0),
                        duration_ms=output_data.get("duration_ms", 0)
                    )
                except json.JSONDecodeError:
                    return SessionResult(
                        success=False,
                        session_id=session_id,
                        output=result.stdout,
                        error="无法解析输出",
                        status="failed"
                    )

        except subprocess.TimeoutExpired:
            return SessionResult(
                success=False,
                session_id=session_id,
                output="",
                error=f"会话超时（{self.timeout}秒）",
                status="timeout"
            )
        except Exception as e:
            return SessionResult(
                success=False,
                session_id=session_id,
                output="",
                error=str(e),
                status="failed"
            )
