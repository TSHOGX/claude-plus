"""
长时间运行代理系统 - 任务管理模块

任务树采用路径编码（Dewey Decimal），执行顺序为 DFS 前序遍历。
"""

import json
import os
from dataclasses import dataclass, asdict, field
from typing import List, Optional
from config import TaskStatus


def parse_task_id(task_id: str) -> List[int]:
    """解析任务 ID 为数值列表，用于排序

    "2.1.10" → [2, 1, 10]
    """
    try:
        return [int(x) for x in task_id.split('.')]
    except ValueError:
        # 兼容非纯数字 ID，按字符串排序
        return [ord(c) for c in task_id]


@dataclass
class Task:
    """任务数据结构

    ID 采用路径编码（如 "1", "1.1", "2.3.1"），自动决定执行顺序。
    """

    id: str  # 路径编码: "1", "1.1", "2.3.1"
    description: str
    steps: List[str] = field(default_factory=list)
    status: str = TaskStatus.PENDING
    session_id: Optional[str] = None
    error_message: Optional[str] = None
    notes: Optional[str] = None  # 执行备注，供 Orchestrator 参考

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Task":
        valid_fields = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in valid_fields}
        return cls(**filtered)

    def get_parent_id(self) -> Optional[str]:
        """获取父任务 ID: '2.1.3' → '2.1'"""
        parts = self.id.split('.')
        return '.'.join(parts[:-1]) if len(parts) > 1 else None

    def get_depth(self) -> int:
        """获取任务深度: '2.1.3' → 3"""
        return self.id.count('.') + 1


class TaskManager:
    """任务管理器 - 负责任务的加载、保存和状态管理

    任务按路径编码排序，执行顺序为 DFS 前序遍历。
    """

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
        """保存任务到 JSON 文件（按路径编码排序）"""
        os.makedirs(os.path.dirname(self.tasks_file), exist_ok=True)
        # 保存前排序，保持文件有序
        sorted_tasks = sorted(self.tasks, key=lambda t: parse_task_id(t.id))
        with open(self.tasks_file, "w", encoding="utf-8") as f:
            json.dump(
                [t.to_dict() for t in sorted_tasks], f, ensure_ascii=False, indent=2
            )

    def get_next_task(self) -> Optional[Task]:
        """获取下一个待处理的任务（DFS 前序遍历）

        按路径编码排序，返回第一个 pending 或 in_progress 的任务。
        """
        eligible_tasks = [
            t
            for t in self.tasks
            if t.status in (TaskStatus.PENDING, TaskStatus.IN_PROGRESS)
        ]
        if not eligible_tasks:
            return None
        # 按路径编码排序
        eligible_tasks.sort(key=lambda t: parse_task_id(t.id))
        return eligible_tasks[0]

    def get_task_by_id(self, task_id: str) -> Optional[Task]:
        """根据 ID 获取任务"""
        for task in self.tasks:
            if task.id == task_id:
                return task
        return None

    def get_children(self, task_id: str) -> List[Task]:
        """获取直接子任务"""
        prefix = task_id + '.'
        depth = task_id.count('.') + 2  # 子任务深度 = 父任务深度 + 1
        children = [
            t for t in self.tasks
            if t.id.startswith(prefix) and t.id.count('.') + 1 == depth
        ]
        return sorted(children, key=lambda t: parse_task_id(t.id))

    def get_subtree(self, task_id: str) -> List[Task]:
        """获取任务及其所有子孙任务"""
        prefix = task_id + '.'
        subtree = [t for t in self.tasks if t.id == task_id or t.id.startswith(prefix)]
        return sorted(subtree, key=lambda t: parse_task_id(t.id))

    def get_root_tasks(self) -> List[Task]:
        """获取所有根任务（顶层任务）"""
        roots = [t for t in self.tasks if '.' not in t.id]
        return sorted(roots, key=lambda t: parse_task_id(t.id))

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

    def add_task_from_dict(self, task_dict: dict) -> Task:
        """从字典创建并添加任务"""
        task = Task.from_dict(task_dict)
        self.add_task(task)
        return task

    def validate_task_dict(self, task_dict: dict) -> List[str]:
        """验证任务格式

        Returns:
            错误列表，空列表表示验证通过
        """
        errors = []

        # 必填字段检查
        if not task_dict.get("id"):
            errors.append("缺少必填字段: id")
        if not task_dict.get("description"):
            errors.append("缺少必填字段: description")

        # ID 格式检查（路径编码）
        task_id = task_dict.get("id", "")
        if task_id:
            parts = task_id.split('.')
            for part in parts:
                if not part.isdigit():
                    errors.append(f"ID 格式错误: '{task_id}'，应为数字路径编码（如 '1', '1.2', '2.1.3'）")
                    break

        # ID 唯一性检查
        if task_dict.get("id") and self.get_task_by_id(task_dict["id"]):
            errors.append(f"任务 ID 已存在: {task_dict['id']}")

        return errors

    def suggest_next_id(self, parent_id: Optional[str] = None) -> str:
        """建议下一个可用的任务 ID

        Args:
            parent_id: 父任务 ID，None 表示顶层任务

        Returns:
            建议的任务 ID
        """
        if parent_id is None:
            # 顶层任务
            roots = self.get_root_tasks()
            if not roots:
                return "1"
            max_id = max(int(t.id) for t in roots)
            return str(max_id + 1)
        else:
            # 子任务
            children = self.get_children(parent_id)
            if not children:
                return f"{parent_id}.1"
            # 获取最后一个子任务的编号
            last_child = children[-1]
            last_num = int(last_child.id.split('.')[-1])
            return f"{parent_id}.{last_num + 1}"

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
        """获取所有任务（按路径编码排序）"""
        return sorted(self.tasks, key=lambda t: parse_task_id(t.id))

    def print_tree(self) -> str:
        """打印任务树结构（用于调试）"""
        lines = []
        sorted_tasks = self.get_all_tasks()
        for task in sorted_tasks:
            depth = task.get_depth()
            indent = "  " * (depth - 1)
            status_icon = {
                TaskStatus.PENDING: "○",
                TaskStatus.IN_PROGRESS: "◐",
                TaskStatus.COMPLETED: "●",
                TaskStatus.FAILED: "✗",
            }.get(task.status, "?")
            lines.append(f"{indent}{status_icon} {task.id}: {task.description}")
        return "\n".join(lines)
