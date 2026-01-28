"""
长时间运行代理系统 - 配置文件
"""

import os
import shutil
import unicodedata

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
CHECK_INTERVAL = 1800  # Supervisor 检查间隔（秒），默认 30 分钟


def get_display_width() -> int:
    """根据终端宽度动态计算文本显示长度（80%可用宽度）"""
    try:
        terminal_width = shutil.get_terminal_size(fallback=(80, 24)).columns
    except Exception:
        terminal_width = 80
    # 预留前缀(25) + 后缀(5) 空间
    return max(40, int((terminal_width - 30) * 0.8))


def _char_width(char: str) -> int:
    """计算单个字符的显示宽度（中文等宽字符为2，其他为1）"""
    width_type = unicodedata.east_asian_width(char)
    return 2 if width_type in ('F', 'W') else 1


def truncate_for_display(text: str) -> str:
    """截断文本用于终端显示，换行替换为空格，正确处理中文宽度"""
    if not text:
        return ""
    text = text.replace("\n", " ").strip()
    max_width = get_display_width()

    # 计算显示宽度并截断
    current_width = 0
    for i, char in enumerate(text):
        char_w = _char_width(char)
        if current_width + char_w > max_width:
            return text[:i] + "..."
        current_width += char_w
    return text


def summarize_tool_input(tool_name: str, tool_input: dict) -> str:
    """提取工具输入的简要摘要"""
    if not isinstance(tool_input, dict):
        return ""

    if tool_name == "Bash":
        return truncate_for_display(tool_input.get("command", ""))
    elif tool_name in ("Read", "Write", "Edit"):
        path = tool_input.get("file_path", "")
        return os.path.basename(path)
    elif tool_name == "Grep":
        pattern = truncate_for_display(tool_input.get("pattern", ""))
        path = tool_input.get("path", "")
        if path:
            return f"{pattern} in {os.path.basename(path)}"
        return pattern
    elif tool_name == "Glob":
        return truncate_for_display(tool_input.get("pattern", ""))
    elif tool_name == "Task":
        return truncate_for_display(tool_input.get("description", ""))
    elif tool_name == "TaskOutput":
        return truncate_for_display(tool_input.get("task_id", ""))
    elif tool_name == "WebFetch":
        url = tool_input.get("url", "")
        if "://" in url:
            url = url.split("://")[1].split("/")[0]
        return truncate_for_display(url)
    elif tool_name == "WebSearch":
        return truncate_for_display(tool_input.get("query", ""))
    else:
        for key in ["subject", "description", "pattern", "query", "command", "file_path", "path", "task_id"]:
            if key in tool_input:
                return truncate_for_display(str(tool_input[key]))
    return ""

# 任务状态
class TaskStatus:
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


# Worker 系统提示模板（追加到系统提示）
SYSTEM_PROMPT_TEMPLATE = """你正在执行一个增量开发任务，开始前建议运行 git log --oneline -5 了解最近进展。"""

# 清理会话提示模板
CLEANUP_PROMPT_TEMPLATE = """⚠️ 紧急通知：任务需要终止，请立即执行清理工作。

## 终止原因
{reason}

## 必须完成的清理工作（按顺序执行）

### 1. 终止后台进程
查找你启动的后台进程，并终止它们。

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

注意：
- 不要直接修改 tasks.json 文件
- 只需在输出中包含上述交接摘要，系统会自动处理
"""



# 任务修改提示模板（用于 add 命令修改任务列表）
TASK_MODIFICATION_PROMPT = """根据用户需求，修改 tasks.json 任务列表。

## 用户需求
{user_request}

## 你的任务
1. 阅读 tasks.json 了解现有任务
2. 利用 git log 了解项目进展
3. 根据用户需求，修改任务列表（增、删、改皆可）
4. 直接编辑 tasks.json 文件

## tasks.json 规范

### 任务 ID
用 `.` 分隔的层级编号，深度优先前序遍历：1 → 1.1 → 1.2 → 2

### 结构
```json
[
  {{
    "id": "1",
    "description": "任务描述",
    "steps": ["步骤1", "步骤2"],
    "status": "pending|in_progress|completed|failed"
  }}
]
```

### 编写原则
- 单一职责, 每个任务只做一件事
- 适中粒度, 10-15 分钟可完成
- steps 是参考, 执行时可灵活调整

### 修改原则
- 新增任务时, 添加到合适位置, ID 不要重复
- 修改/删除 pending/in_progress/failed 状态的任务
- 保留 completed 状态的任务, 不要修改

## 输出
完成后输出 TASKS_MODIFIED"""

# 任务格式修复提示模板
TASKS_FIX_PROMPT = """tasks.json 存在以下格式问题，请修复：

{errors}

## 规范
每个任务必须包含: id, description, steps
- ID 使用路径编码（如 "1", "1.2", "2.1.3"）
- ID 不能重复

请直接修复 tasks.json 文件。"""

# 任务创建提示模板（用于 init 命令生成 tasks.json）
TASKS_CREATION_PROMPT = """你是任务规划师。根据用户需求，为项目创建初始任务列表。

## 用户需求
{user_request}

## 你的任务
1. 探索当前目录，了解项目结构和现有代码
2. 如果用户提到了参考文件，阅读它们
3. 根据理解，创建 tasks.json 文件

## tasks.json 规范

### 核心思想
- 一个任务 = 一次 Claude 会话（10-15分钟）
- 任务树按 ID 深度优先前序遍历执行
- steps 是参考指引，执行时可灵活调整

### 任务 ID
用 `.` 分隔的层级编号，如：1 → 1.1 → 1.2 → 2 → 2.1

### 结构
```json
[
  {{
    "id": "1",
    "description": "任务描述",
    "steps": ["步骤1", "步骤2"]
  }}
]
```

### 编写原则
- **单一职责**: 每个任务只做一件事
- **层级清晰**: 用 ID 层级表达任务关系

### 示例
```json
[
  {{"id": "1", "description": "项目初始化", "steps": ["创建目录结构", "初始化 git"]}},
  {{"id": "1.1", "description": "配置开发环境", "steps": ["创建配置文件", "设置日志"]}},
  {{"id": "2", "description": "实现核心功能", "steps": ["设计数据模型", "实现业务逻辑"]}},
  {{"id": "3", "description": "测试与验证", "steps": ["编写测试", "运行验证"]}}
]
```

## 输出
1. 直接创建 tasks.json 文件
2. 完成后输出 TASKS_CREATED
"""

# Post-work 验证提示模板 - 自主验证和提交
POST_WORK_PROMPT = """任务: {task_description} 已执行完毕，请验证并提交改动。

## 建议步骤
1. **Review** - git diff 审查改动，确保一致性（无遗漏的关联修改）
2. **Clean** - 清理调试代码、无用注释、dead code，运行 linter（如有）
3. **Test** - 运行测试或功能验证
4. **Update Docs** - 同步更新受影响功能的文档
5. **Commit** - 提交改动，遵循项目 commit 风格

## 完成标准
确保 git status 干净（无未提交的改动）
"""

# Worker 任务提示模板
TASK_PROMPT_TEMPLATE = """## 任务
{task_description}

## 参考步骤
{task_steps}
{notes_section}
请开始执行。
"""

# Supervisor 分析提示模板 - 只读分析
SUPERVISOR_PROMPT = """你是 Agent 执行监督者。

## 重要约束
**禁止执行任何修改操作！** 你只能读取和分析，不能：
- 修改任何文件
- 执行任何命令
- 创建或删除文件
你的唯一任务是分析日志并输出 JSON 决策。

## 任务信息
- 描述: {task_description}
- 已运行: {elapsed_time}
- 日志文件: {log_file}

## 你的任务
1. 使用 Read 工具阅读日志文件了解 Worker 执行情况
2. 判断是否需要干预
3. 输出 JSON 决策（不要做其他事情）

## 决策选项
- continue: Worker 在正常工作（有新进展、正在调试等）
- orchestrate: 需要重新审视任务（陷入循环、任务太大、发现新问题、需要人工等）

## 输出格式
只输出一个 JSON，不要有其他内容：
{{"decision": "continue|orchestrate", "reason": "简要原因"}}
"""

# 编排提示模板
ORCHESTRATOR_PROMPT = """你是任务编排者。需要重新审视和调整任务列表。

## 触发原因
{trigger_reason}

## 额外上下文
{context}

## 你的任务
1. 阅读 CLAUDE.md 了解项目目标
2. 阅读 tasks.json 了解当前任务列表
3. 运行 git log --oneline -10 和 git diff 了解最近进展
4. 从全局角度审视并优化任务列表
5. 直接编辑 tasks.json 文件

## 任务格式
```json
{{
  "id": "2.1",
  "description": "任务描述",
  "steps": ["步骤1", "步骤2"],
  "status": "pending"
}}
```

ID 用 `.` 分隔，深度优先前序遍历：1 → 1.1 → 1.2 → 2

## 编排操作
- 增/删/改: 根据实际情况调整任务
- 拆分/合并: 调整任务粒度（10-15分钟可完成）
- 利用 notes: 在任务间传递上下文（进展、问题、建议）

## 注意事项
- failed 任务必须处理（不能保持 failed 状态）
- completed 任务不要修改
- ID 保持唯一
- 验证失败场景处理: 如果触发原因包含"验证失败"：
  1. 检查 git status 了解剩余未提交的文件
  2. 不要重复执行原任务
  3. 如果任务实际已完成，只是有多余文件：将原任务标记为 completed，创建新任务处理这些文件（如加入 .gitignore）
  4. 如果任务确实未完成，调整任务描述使其更清晰

完成后输出 ORCHESTRATION_DONE
"""

# 编排审视提示模板
ORCHESTRATOR_REVIEW_PROMPT = """请审视你刚才对任务列表的修改。

1. 运行 git diff tasks.json 查看改动
2. 检查：
   - JSON 格式是否正确
   - ID 是否唯一，是否使用路径编码格式（如 "1", "1.2", "2.1.3"）
   - 任务粒度是否合适（10-15分钟可完成）
   - notes 是否包含有用的上下文信息
   - 没有遗留 failed 状态的任务

如果发现问题，请修复。
如果没有问题，输出 REVIEW_PASSED
"""

# Learn 命令提示模板
LEARN_PROMPT = """用户希望你学习一条建议，并更新 CLAUDE.md。

## 用户建议
{suggestion}

## 你的任务
1. 阅读当前 CLAUDE.md
2. 探索 codebase 理解项目结构和规范
3. 根据用户建议，在 CLAUDE.md 中添加合适的指导语句

## 原则
- 保持简洁，避免冗余
- 新增内容应与现有内容风格一致
- 如果建议已被覆盖，无需重复添加

完成后输出 LEARNED
"""

# 任务修订提示模板（用于根据用户反馈修改任务列表）
TASKS_REVISION_PROMPT = """用户对任务列表提出了反馈，请根据反馈修改 tasks.json：

## 用户反馈
{feedback}

## 要求
1. 根据用户反馈修改 tasks.json
2. 保持任务 ID 格式规范（路径编码）
3. 修改完成后输出 TASKS_MODIFIED 确认
"""
