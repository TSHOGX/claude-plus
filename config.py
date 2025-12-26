"""
长时间运行代理系统 - 配置文件
"""

import os

# 默认工作目录
DEFAULT_WORKSPACE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "workspace")

# 敏感目录列表（禁止使用）
FORBIDDEN_DIRS = ["/", "/etc", "/usr", "/bin", "/sbin", "/var", "/System", "/Library"]


def get_paths(workspace_dir: str = None):
    """获取基于 workspace 的路径配置"""
    ws = os.path.abspath(os.path.expanduser(workspace_dir or DEFAULT_WORKSPACE_DIR))
    return {
        "workspace": ws,
        "tasks_file": os.path.join(ws, "tasks.json"),
        "progress_file": os.path.join(ws, "progress.md"),
        "init_script": os.path.join(ws, "init.sh"),
    }


def is_safe_workspace(path: str) -> tuple[bool, str]:
    """检查 workspace 路径是否安全"""
    abs_path = os.path.abspath(os.path.expanduser(path))
    for forbidden in FORBIDDEN_DIRS:
        if abs_path == forbidden or abs_path.startswith(forbidden + "/"):
            if abs_path.count("/") <= 2:  # 只禁止顶层目录
                return False, f"禁止使用系统目录: {forbidden}"
    return True, ""

# Claude CLI 配置
CLAUDE_CMD = "claude"
SESSION_TIMEOUT = 900  # 15 分钟超时（秒）

# 任务状态
class TaskStatus:
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"

# 完成标记（Claude 应该在输出中包含这些标记）
COMPLETION_MARKERS = {
    "success": "TASK_COMPLETED",
    "blocked": "TASK_BLOCKED:",
    "error": "TASK_ERROR:"
}

# 系统提示模板
SYSTEM_PROMPT_TEMPLATE = """你正在执行一个增量开发任务。你是一个专注、高效的开发代理。

## 当前任务
{task_description}

## 任务步骤
{task_steps}

## 最近进度
{recent_progress}

## 工作目录
{workspace_dir}

## 重要规则
1. 只专注于当前任务，不要尝试完成其他任务
2. 完成后明确报告 "TASK_COMPLETED" 表示成功
3. 如果遇到阻塞，报告 "TASK_BLOCKED: <原因>"
4. 如果遇到错误，报告 "TASK_ERROR: <错误描述>"
5. 保持代码变更最小化
6. 在 workspace 目录下工作
"""

# 最大重试次数
MAX_RETRIES = 2

# 进度日志格式
PROGRESS_ENTRY_FORMAT = """
---
## [{timestamp}] Task: {task_id}
**描述**: {description}
**状态**: {status}
**会话 ID**: {session_id}
**详情**:
{details}
"""

# 任务细化提示模板（超时时使用）
TASK_REFINEMENT_PROMPT = """分析以下超时的任务，将其细化为更小的子任务。

## 原任务
ID: {task_id}
描述: {description}
步骤: {steps}

## 要求
1. 将任务拆分为 2-4 个更小、更具体的子任务
2. 每个子任务应该能在 10 分钟内完成
3. 保持任务之间的依赖顺序

请直接输出 JSON 格式的任务列表，不要其他内容：
```json
[
  {{"id": "{task_id}_1", "description": "子任务1描述", "priority": 1, "steps": ["步骤1", "步骤2"]}},
  {{"id": "{task_id}_2", "description": "子任务2描述", "priority": 2, "steps": ["步骤1", "步骤2"]}}
]
```
"""
