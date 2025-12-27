"""
长时间运行代理系统 - 进度日志模块
"""

import os
from datetime import datetime
from typing import List, Optional
from config import PROGRESS_ENTRY_FORMAT


class ProgressLog:
    """进度日志管理器 - 记录和读取任务进度"""

    def __init__(self, progress_file: str):
        self.progress_file = progress_file
        self._ensure_file_exists()

    def _ensure_file_exists(self):
        """确保进度文件存在"""
        os.makedirs(os.path.dirname(self.progress_file), exist_ok=True)
        if not os.path.exists(self.progress_file):
            with open(self.progress_file, "w", encoding="utf-8") as f:
                f.write("# 任务进度日志\n\n")
                f.write(f"创建时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")

    def append(
        self,
        task_id: str,
        description: str,
        status: str,
        session_id: Optional[str] = None,
        details: str = "",
    ):
        """追加进度记录"""
        entry = PROGRESS_ENTRY_FORMAT.format(
            timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            task_id=task_id,
            description=description,
            status=status,
            session_id=session_id or "N/A",
            details=details.strip() if details else "无",
        )
        with open(self.progress_file, "a", encoding="utf-8") as f:
            f.write(entry)

    def get_recent(self, n: int = 5) -> str:
        """获取最近 n 条记录"""
        if not os.path.exists(self.progress_file):
            return "无进度记录"

        with open(self.progress_file, "r", encoding="utf-8") as f:
            content = f.read()

        # 按 "---" 分割记录
        entries = content.split("---")
        # 过滤空条目
        entries = [
            e.strip() for e in entries if e.strip() and e.strip() != "# 任务进度日志"
        ]

        if not entries:
            return "无进度记录"

        # 获取最近 n 条
        recent = entries[-n:] if len(entries) >= n else entries
        return "\n---\n".join(recent)

    def get_summary(self) -> str:
        """获取进度摘要"""
        if not os.path.exists(self.progress_file):
            return "无进度记录"

        with open(self.progress_file, "r", encoding="utf-8") as f:
            content = f.read()

        # 统计完成和失败的任务
        completed_count = content.count("**状态**: completed")
        failed_count = content.count("**状态**: failed")
        in_progress_count = content.count("**状态**: in_progress")

        return f"""
## 进度摘要
- 已完成: {completed_count}
- 进行中: {in_progress_count}
- 失败: {failed_count}
- 总记录数: {completed_count + failed_count + in_progress_count}
"""

    def get_full_log(self) -> str:
        """获取完整日志"""
        if not os.path.exists(self.progress_file):
            return "无进度记录"

        with open(self.progress_file, "r", encoding="utf-8") as f:
            return f.read()

    def clear(self):
        """清除进度日志"""
        with open(self.progress_file, "w", encoding="utf-8") as f:
            f.write("# 任务进度日志\n\n")
            f.write(f"创建时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")

    def log_start(self, task_id: str, description: str, session_id: str):
        """记录任务开始"""
        self.append(
            task_id=task_id,
            description=description,
            status="in_progress",
            session_id=session_id,
            details="任务开始执行",
        )

    def log_complete(
        self, task_id: str, description: str, session_id: str, output: str = ""
    ):
        """记录任务完成"""
        self.append(
            task_id=task_id,
            description=description,
            status="completed",
            session_id=session_id,
            details=(
                f"任务成功完成\n\n输出摘要:\n{output[:500]}..."
                if len(output) > 500
                else f"任务成功完成\n\n输出:\n{output}"
            ),
        )

    def log_failed(self, task_id: str, description: str, session_id: str, error: str):
        """记录任务失败"""
        self.append(
            task_id=task_id,
            description=description,
            status="failed",
            session_id=session_id,
            details=f"任务失败\n\n错误信息:\n{error}",
        )

    def log_blocked(self, task_id: str, description: str, session_id: str, reason: str):
        """记录任务阻塞"""
        self.append(
            task_id=task_id,
            description=description,
            status="blocked",
            session_id=session_id,
            details=f"任务被阻塞\n\n原因:\n{reason}",
        )

    def log_background_start(self, task_id: str, description: str, pid: int):
        """记录后台任务启动"""
        self.append(
            task_id=task_id,
            description=description,
            status="background",
            session_id=f"PID:{pid}",
            details=f"""后台任务启动

- 进程ID: {pid}
- 查看状态: python3 main.py check-bg
- 查看日志: tail -f .claude_bg_{task_id}.log
- 终止进程: python3 main.py kill-bg {task_id}
""",
        )

    def log_timeout_snapshot(
        self, task_id: str, description: str, session_id: str, snapshot: str
    ):
        """记录超时快照"""
        self.append(
            task_id=task_id,
            description=description,
            status="timeout",
            session_id=session_id or "无",
            details=f"""任务超时中断

## 截至超时时的执行状态
{snapshot}

## 恢复方式
- 查看会话详情: claude -r {session_id}
- 重置任务: python3 main.py reset-task {task_id}
""",
        )
