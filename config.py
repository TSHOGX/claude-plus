"""
长时间运行代理系统 - 配置文件
"""

import os

# 默认工作目录
DEFAULT_WORKSPACE_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "workspace"
)

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

# Supervisor 配置
CHECK_INTERVAL = 300  # Supervisor 检查间隔（秒），默认 5 分钟
# MAX_TASK_DURATION 已移除 - 改为由 Supervisor 智能判断，不设硬性上限


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
    "error": "TASK_ERROR:",
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
6. 在工作目录下工作
"""

# 最大重试次数
MAX_RETRIES = 2

# 优雅退出配置
GRACEFUL_SHUTDOWN_TIMEOUT = 60  # 清理会话最大时长（秒）

# 清理会话提示模板
CLEANUP_PROMPT_TEMPLATE = """⚠️ 紧急通知：任务需要终止，请立即执行清理工作。

## 终止原因
{reason}

## 必须完成的清理工作（按顺序执行）

### 1. 终止后台进程
使用 `ps aux | grep -E "python|node|npm"` 查找你启动的后台进程，使用 `kill` 终止它们。

### 2. 清理临时文件
删除不需要的临时文件（但保留有用的调试文件）。

### 3. 输出交接摘要（重要！）
完成清理后，请在最后输出以下格式的交接摘要，用于传递给下一个 Worker：

```HANDOVER_START```
## 当前进度
[描述已完成的工作，如：已完成X功能，正在进行Y步骤]

## 遇到的问题
[描述遇到的问题和尝试的解决方案]

## 下一步建议
[给下一个 Worker 的具体建议，如：需要先解决Z问题，建议从W开始]

## 关键文件
[列出重要的文件路径和说明]
```HANDOVER_END```

最后输出 "CLEANUP_DONE" 表示已完成。

注意：
- 不要直接修改 tasks.json 或 progress.md 文件
- 只需在输出中包含上述交接摘要，系统会自动处理
"""

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

# 任务生成提示模板
TASK_GENERATION_PROMPT = """根据用户需求，生成结构化的开发任务。

## 用户需求
{user_request}

## 项目上下文
{project_context}

## 现有任务 ID
{existing_ids}

## 输出格式
输出一个 JSON 数组，每个任务包含：
- id: 唯一标识，格式如 "001", "002"（避开已有 ID）
- description: 简洁的任务描述（20字内）
- priority: 优先级数字（越小越优先）
- steps: 具体步骤列表

## 要求
1. 任务粒度适中，每个任务可在 10 分钟内完成
2. 步骤具体可执行，指明文件路径
3. 任务间有依赖时，用 priority 控制顺序
4. 只输出 JSON，不要其他内容

```json
[
  {{"id": "xxx", "description": "...", "priority": N, "steps": ["...", "..."]}}
]
```"""
