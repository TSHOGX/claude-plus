"""
长时间运行代理系统 - 任务管理模块
"""

import json
import os
from dataclasses import dataclass, asdict, field
from typing import List, Optional
from config import TaskStatus


@dataclass
class Task:
    """任务数据结构"""

    id: str
    description: str
    priority: int = 1
    steps: List[str] = field(default_factory=list)
    status: str = TaskStatus.PENDING
    session_id: Optional[str] = None
    error_message: Optional[str] = None
    notes: Optional[str] = None  # 执行备注（失败原因等），供 Orchestrator 参考

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Task":
        # 兼容旧格式：忽略不存在的字段
        valid_fields = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in valid_fields}
        return cls(**filtered)


class TaskManager:
    """任务管理器 - 负责任务的加载、保存和状态管理"""

    def __init__(self, tasks_file: str):
        self.tasks_file = tasks_file
        self.tasks: List[Task] = []
        self._load_tasks()

    def _load_tasks(self):
        """从 JSON 文件加载任务"""
        if os.path.exists(self.tasks_file):
            try:
                with open(self.tasks_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    # 支持两种格式：直接列表 或 {"tasks": [...]} 嵌套结构
                    if isinstance(data, list):
                        task_list = data
                    elif isinstance(data, dict) and "tasks" in data:
                        task_list = data["tasks"]
                    else:
                        task_list = []
                    self.tasks = [Task.from_dict(t) for t in task_list]
            except (json.JSONDecodeError, KeyError) as e:
                print(f"警告: 加载任务文件失败 - {e}")
                self.tasks = []
        else:
            self.tasks = []

    def save_tasks(self):
        """保存任务到 JSON 文件"""
        os.makedirs(os.path.dirname(self.tasks_file), exist_ok=True)
        with open(self.tasks_file, "w", encoding="utf-8") as f:
            json.dump(
                [t.to_dict() for t in self.tasks], f, ensure_ascii=False, indent=2
            )

    def get_next_task(self) -> Optional[Task]:
        """获取下一个待处理的任务（按优先级排序）

        只包括：pending 和 in_progress 的任务
        失败任务由 Orchestrator 处理
        """
        eligible_tasks = [
            t
            for t in self.tasks
            if t.status in (TaskStatus.PENDING, TaskStatus.IN_PROGRESS)
        ]
        if not eligible_tasks:
            return None
        # 按优先级排序，优先级数字越小越优先
        eligible_tasks.sort(key=lambda t: t.priority)
        return eligible_tasks[0]

    def get_task_by_id(self, task_id: str) -> Optional[Task]:
        """根据 ID 获取任务"""
        for task in self.tasks:
            if task.id == task_id:
                return task
        return None

    def mark_in_progress(self, task_id: str, session_id: str = None):
        """标记任务为进行中"""
        task = self.get_task_by_id(task_id)
        if task:
            task.status = TaskStatus.IN_PROGRESS
            task.session_id = session_id
            self.save_tasks()

    def mark_completed(self, task_id: str):
        """标记任务为已完成"""
        task = self.get_task_by_id(task_id)
        if task:
            task.status = TaskStatus.COMPLETED
            self.save_tasks()

    def mark_failed(self, task_id: str, error_message: str):
        """标记任务为失败"""
        task = self.get_task_by_id(task_id)
        if task:
            task.status = TaskStatus.FAILED
            task.error_message = error_message
            self.save_tasks()

    def reset_task(self, task_id: str):
        """重置任务状态为待处理"""
        task = self.get_task_by_id(task_id)
        if task:
            task.status = TaskStatus.PENDING
            task.error_message = None
            task.session_id = None
            task.notes = None
            self.save_tasks()

    def update_notes(self, task_id: str, notes: str):
        """更新任务备注（用于记录失败原因等）"""
        task = self.get_task_by_id(task_id)
        if task:
            task.notes = notes
            self.save_tasks()

    def clear_notes(self, task_id: str):
        """清除任务备注（任务完成时调用）"""
        task = self.get_task_by_id(task_id)
        if task:
            task.notes = None
            self.save_tasks()

    def add_task(self, task: Task):
        """添加新任务"""
        self.tasks.append(task)
        self.save_tasks()


    def get_stats(self) -> dict:
        """获取任务统计信息"""
        stats = {
            "total": len(self.tasks),
            "pending": 0,
            "in_progress": 0,
            "completed": 0,
            "failed": 0,
        }
        for task in self.tasks:
            if task.status in stats:
                stats[task.status] += 1
        return stats

    def get_all_tasks(self) -> List[Task]:
        """获取所有任务"""
        return self.tasks.copy()
