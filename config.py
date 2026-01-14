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
CHECK_INTERVAL = 1500  # Supervisor 检查间隔（秒），默认 25 分钟
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

# Worker 系统提示模板 - 精简版
SYSTEM_PROMPT_TEMPLATE = """你正在执行一个增量开发任务。

## 当前任务
{task_description}

## 任务步骤
{task_steps}

## 开始前
1. 阅读 CLAUDE.md 了解项目上下文和目标
2. 运行 git log --oneline -5 了解最近进展
3. 如果当前任务需要接续上次工作，查阅 tasks.json 中的 notes 字段

## 重要规则
1. 只专注于当前任务，不要尝试完成其他任务
2. 完成后输出 TASK_COMPLETED
3. 遇到阻塞输出 TASK_BLOCKED: <原因>
4. 遇到错误输出 TASK_ERROR: <错误描述>
"""

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
2. 如果用户提到了参考文件（如 todo.md、spec.md），读取它们
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
    "category": "feature",                 // 可选：分类（core/feature/bugfix/refactor）
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
  {{"id": "001", "priority": 1, "category": "core",
    "description": "创建 Post 数据模型",
    "steps": ["创建 models/post.py", "定义 Post 类", "添加序列化方法"]}},

  {{"id": "002", "priority": 2, "category": "feature",
    "description": "实现文章存储功能",
    "steps": ["创建 PostManager 类", "实现 save_post()", "JSON 持久化"]}},

  {{"id": "003", "priority": 3, "category": "feature",
    "description": "实现文章列表功能",
    "steps": ["添加 list_posts()", "支持分页", "按时间倒序"]}}
]
```

## 输出
1. 直接创建 tasks.json 文件
2. 完成后输出 TASKS_CREATED
"""

# Post-work 验证提示模板 - 包含自主测试
POST_WORK_PROMPT = """你是代码审核员。任务 [{task_id}]: {task_description} 刚执行完毕。

## 验证步骤
1. 运行 git diff --stat 查看改动范围
2. 语法检查：确保代码无语法错误
3. 单元测试：
   - 如果有现成测试，运行 pytest
   - 如果没有测试文件但改动了核心逻辑，编写简单测试验证功能
4. 端到端测试（如适用）：
   - Web 应用：启动服务，用 curl 或脚本验证关键端点
   - CLI 工具：运行命令验证输出
   - API：调用接口验证返回值
5. 测试通过后，根据判断决定是否可以提交

## 输出格式
验证通过:
VALIDATION_PASSED
COMMIT_MESSAGE_START
<编写高质量的 commit message，遵循 conventional commits 或项目既有风格>
COMMIT_MESSAGE_END

需要修复: 直接修复问题后重新验证

无法修复:
VALIDATION_FAILED: <原因>
"""


