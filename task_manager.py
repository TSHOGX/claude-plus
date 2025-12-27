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
    category: str = "feature"
    steps: List[str] = field(default_factory=list)
    status: str = TaskStatus.PENDING
    session_id: Optional[str] = None
    error_message: Optional[str] = None
    retries: int = 0
    # 新增字段
    parent_id: Optional[str] = None  # 父任务ID（分裂时）
    timeout_snapshot: Optional[str] = None  # 超时快照
    background_pid: Optional[int] = None  # 后台进程ID
    estimated_duration: Optional[int] = None  # 预估时长（秒）

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
                    self.tasks = [Task.from_dict(t) for t in data]
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

    def get_next_task(self, max_retries: int = 2) -> Optional[Task]:
        """获取下一个待处理的任务（按优先级排序）

        包括：pending、in_progress、以及失败但未超过重试次数的任务
        排除：split（已分裂）、background（后台运行中）
        """
        eligible_tasks = [
            t
            for t in self.tasks
            if (
                t.status in (TaskStatus.PENDING, TaskStatus.IN_PROGRESS)
                or (t.status == TaskStatus.FAILED and t.retries < max_retries)
            )
            and t.status not in ("split", "background")  # 排除特殊状态
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
            task.retries += 1
            self.save_tasks()

    def reset_task(self, task_id: str):
        """重置任务状态为待处理"""
        task = self.get_task_by_id(task_id)
        if task:
            task.status = TaskStatus.PENDING
            task.error_message = None
            task.session_id = None  # 重置时清除 session_id
            task.retries = 0  # 重置重试次数
            self.save_tasks()

    def add_task(self, task: Task):
        """添加新任务"""
        self.tasks.append(task)
        self.save_tasks()

    def split_task(self, task_id: str, subtasks: List[dict]) -> bool:
        """分裂任务为子任务，保留原任务记录"""
        parent = self.get_task_by_id(task_id)
        if not parent:
            return False

        # 标记父任务为已分裂（而非删除）
        parent.status = "split"

        # 创建子任务，关联父任务
        for i, sub_data in enumerate(subtasks):
            sub_task = Task(
                id=sub_data.get("id", f"{task_id}_{i+1}"),
                description=sub_data["description"],
                priority=sub_data.get("priority", parent.priority + (i * 0.1)),
                steps=sub_data.get("steps", []),
                parent_id=task_id,
                category=parent.category,
            )
            self.tasks.append(sub_task)

        self.save_tasks()
        return True

    def mark_background(self, task_id: str, pid: int, estimated_duration: int = None):
        """标记任务为后台运行中"""
        task = self.get_task_by_id(task_id)
        if task:
            task.status = "background"
            task.background_pid = pid
            task.estimated_duration = estimated_duration
            self.save_tasks()

    def get_background_tasks(self) -> List[Task]:
        """获取所有后台运行的任务"""
        return [t for t in self.tasks if t.status == "background"]

    def get_stats(self) -> dict:
        """获取任务统计信息"""
        stats = {
            "total": len(self.tasks),
            "pending": 0,
            "in_progress": 0,
            "completed": 0,
            "failed": 0,
            "split": 0,
            "background": 0,
        }
        for task in self.tasks:
            if task.status in stats:
                stats[task.status] += 1
        return stats

    def get_all_tasks(self) -> List[Task]:
        """获取所有任务"""
        return self.tasks.copy()


def create_example_tasks() -> List[Task]:
    """创建示例任务（TODO 应用）"""
    return [
        Task(
            id="001",
            description="创建 Todo 数据模型",
            priority=1,
            category="core",
            steps=[
                "在 workspace 目录创建 todo_app 子目录",
                "创建 models.py 文件",
                "定义 Todo 类，包含 id, title, completed, created_at 字段",
                "添加 to_dict 和 from_dict 方法",
            ],
        ),
        Task(
            id="002",
            description="实现添加任务功能",
            priority=2,
            category="feature",
            steps=[
                "在 todo_app 目录创建 todo_manager.py",
                "实现 TodoManager 类",
                "实现 add_todo(title) 方法",
                "使用 JSON 文件存储数据",
            ],
        ),
        Task(
            id="003",
            description="实现列出任务功能",
            priority=3,
            category="feature",
            steps=[
                "在 TodoManager 中添加 list_todos() 方法",
                "支持过滤：全部、已完成、未完成",
                "按创建时间排序",
            ],
        ),
        Task(
            id="004",
            description="实现完成任务功能",
            priority=4,
            category="feature",
            steps=[
                "在 TodoManager 中添加 complete_todo(id) 方法",
                "更新 todo 的 completed 状态",
                "保存更新到文件",
            ],
        ),
        Task(
            id="005",
            description="实现删除任务功能",
            priority=5,
            category="feature",
            steps=[
                "在 TodoManager 中添加 delete_todo(id) 方法",
                "从列表中移除指定 todo",
                "保存更新到文件",
            ],
        ),
        Task(
            id="006",
            description="添加 CLI 交互界面",
            priority=6,
            category="feature",
            steps=[
                "创建 cli.py 文件",
                "使用 argparse 或简单的命令循环",
                "支持命令: add, list, complete, delete, quit",
                "美化输出格式",
            ],
        ),
    ]
