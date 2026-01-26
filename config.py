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

# Worker 系统提示模板
SYSTEM_PROMPT_TEMPLATE = """你正在执行一个增量开发任务。

## 当前任务
{task_description}

## 任务步骤
{task_steps}

## 开始前
1. 运行 git log --oneline -5 了解最近进展
2. 如果当前任务需要接续上次工作，查阅 tasks.json 中的 notes 字段

## 重要规则
1. 只专注于当前任务，不要尝试完成其他任务
2. 完成后输出 TASK_COMPLETED
3. 遇到阻塞输出 TASK_BLOCKED: <原因>
4. 遇到错误输出 TASK_ERROR: <错误描述>
"""

# 优雅退出配置
GRACEFUL_SHUTDOWN_TIMEOUT = 600  # 清理会话最大时长（秒），默认 10 分钟

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

# 任务创建提示模板（用于 init 命令生成 tasks.json）
TASKS_CREATION_PROMPT = """你是任务规划师。根据用户需求，为项目创建初始任务列表。

## 用户需求
{user_request}

## 你的任务
1. 探索当前目录，了解项目结构和现有代码
2. 如果用户提到了参考文件，阅读它们
3. 根据理解，按照下面的规范创建 tasks.json 文件

## tasks.json 规范

### 核心思想
一个任务 = 一次 Claude 会话
任务粒度应该足够小，让 Claude 在单次会话（10-15分钟）内完成，避免上下文溢出。

### 结构定义
```json
[
  {{
    "id": "001",                          // 必填：唯一标识符
    "description": "实现用户登录功能",      // 必填：一句话描述任务目标
    "priority": 1,                         // 必填：执行优先级（数字越小越优先）
    "steps": [                             // 必填：具体执行步骤
      "创建 auth.py 文件",
      "实现 login(username, password) 函数",
      "添加密码验证逻辑"
    ]
  }}
]
```

### 编写原则
| 原则 | 说明 | 示例 |
|------|------|------|
| **单一职责** | 每个任务只做一件事 | ✅ "创建数据模型" ❌ "创建模型并实现所有API" |
| **明确边界** | 指明文件路径和函数名 | ✅ "在 utils.py 中添加 format_date()" |
| **依赖顺序** | 用 priority 控制执行顺序 | 模型(1) → CRUD(2-5) → 界面(6) |
| **可验证** | steps 应能检验完成情况 | "添加单元测试" / "函数应返回 True/False" |

### 任务分解示例
项目：博客系统

```json
[
  {{"id": "001", "priority": 1,
    "description": "创建 Post 数据模型",
    "steps": ["创建 models/post.py", "定义 Post 类", "添加序列化方法"]}},

  {{"id": "002", "priority": 2,
    "description": "实现文章存储功能",
    "steps": ["创建 PostManager 类", "实现 save_post()", "JSON 持久化"]}},

  {{"id": "003", "priority": 3,
    "description": "实现文章列表功能",
    "steps": ["添加 list_posts()", "支持分页", "按时间倒序"]}}
]
```

## 输出
1. 直接创建 tasks.json 文件
2. 完成后输出 TASKS_CREATED
"""

# Post-work 验证提示模板 - 自主验证和提交
POST_WORK_PROMPT = """任务 [{task_id}]: {task_description} 已执行完毕，请验证并提交改动。

## 你的职责
1. 验证代码改动是否正确
2. 验证通过后执行 git commit
3. 不需要提交的文件（日志、缓存等）加入 .gitignore

## 验证建议（根据实际情况选择）
- 查看 git diff 了解改动范围
- 语法/类型检查（如项目有配置）
- 运行测试（如项目有测试）
- 简单功能验证（如适用）

## Commit 格式建议
- 遵循项目现有的 commit 风格
- 若无现有风格，建议使用 conventional commits（如 feat:, fix:, refactor:）

## 完成标准
确保 git status 干净（无未提交的改动）
"""

# Worker 任务提示模板
TASK_PROMPT_TEMPLATE = """请执行以下任务：

## 任务 ID: {task_id}
## 描述: {task_description}

## 步骤:
{task_steps}

请开始执行，完成后输出 TASK_COMPLETED，遇到问题输出 TASK_BLOCKED: <原因>。
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
3. 运行 git log --oneline -10 了解最近进展
4. 根据触发原因，对 tasks.json 进行必要的调整：
   - 可以增加新任务（新发现的问题等）
   - 可以修改现有任务的描述/步骤/优先级
   - 可以删除不再需要的 pending 任务
5. 直接编辑 tasks.json 文件

## 处理失败任务 (status=failed)
对于失败的任务，你必须采取以下其一：
1. **重试**: 将 status 改为 "pending"，清除 error_message（如果是临时性问题）
2. **修改后重试**: 修改任务的 description/steps 后，将 status 改为 "pending"
3. **拆分**: 将复杂任务拆分为多个小任务，删除原任务
4. **删除**: 如果任务不再需要，直接删除

重要：不能让 failed 任务保持 failed 状态，必须处理！

## 约束
- 任务粒度适中（单任务 10-15 分钟内可完成）
- 保持 id 唯一
- 不要修改 status=completed 的任务
- 不要删除 status=in_progress 的任务

完成后输出 ORCHESTRATION_DONE
"""

# 编排审视提示模板
ORCHESTRATOR_REVIEW_PROMPT = """请审视你刚才对任务列表的修改。

1. 运行 git diff tasks.json 查看改动
2. 检查：
   - JSON 格式是否正确
   - ID 是否唯一
   - 是否意外删除了进行中的任务
   - 修改是否符合项目目标

如果发现问题，请修复。
如果没有问题，输出 REVIEW_PASSED
"""


