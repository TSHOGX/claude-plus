# Claude Long-Running Agent

基于 [Anthropic 博客](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents) 思路实现的超长任务处理系统。

## 核心思想

```
一个任务 = 一次 Claude 会话
```

将复杂项目拆分为多个小任务，每个任务在单次 Claude 会话（10-15分钟）内完成，避免上下文溢出。

## 架构

```
┌─────────────────────────────────────────┐
│              main.py                    │
│           LongRunningAgent              │
├─────────────────────────────────────────┤
│  TaskManager   ProgressLog   Session    │
│  (tasks.json)  (progress.md) Runner     │
├─────────────────────────────────────────┤
│           workspace/                    │
│    你的代码 + tasks.json + progress.md  │
└─────────────────────────────────────────┘
```

## 快速开始

### 1. 准备工作目录

```bash
# 在现有项目中使用
python3 main.py -w ~/my-project init

# 或使用默认目录
python3 main.py init
```

### 2. 创建任务文件

在工作目录创建 `tasks.json`：

```json
[
  {
    "id": "001",
    "description": "创建用户数据模型",
    "priority": 1,
    "steps": ["创建 models/user.py", "定义 User 类", "添加序列化方法"]
  },
  {
    "id": "002",
    "description": "实现用户注册功能",
    "priority": 2,
    "steps": ["创建 UserManager 类", "实现 register() 方法", "添加密码加密"]
  }
]
```

### 3. 运行

```bash
# 执行所有任务
python3 main.py -w ~/my-project run

# 执行指定数量
python3 main.py -w ~/my-project run --max-tasks 3

# 查看状态
python3 main.py -w ~/my-project status
```

## 命令参考

| 命令 | 说明 |
|------|------|
| `init` | 初始化工作环境，创建 Git 快照保护现有代码 |
| `run` | 运行任务处理 |
| `status` | 显示当前状态 |
| `reset` | 重置所有任务 |
| `reset-task <id>` | 重置单个任务 |

全局参数：
- `-w/--workspace <path>` 指定工作目录
- `-q/--quiet` 静默模式（不显示 Claude 执行过程）

## 实时输出

默认情况下，系统会显示 Claude 的执行过程：

```
──────────────────────────────────────────────────
📝 处理任务 [001]: 创建 Todo 数据模型
   优先级: 1
──────────────────────────────────────────────────
   ┌─ Claude 执行中 ─────────────────────────
   │ 💬 我来创建 Todo 数据模型...
   │ 🔧 调用工具: Write
   │ 💬 文件已创建，现在添加序列化方法...
   │ 🔧 调用工具: Edit
   └─ 执行完成 (45.2s) ────────────────────
   ✅ 任务完成!
   💰 本次成本: $0.0234 | 总成本: $0.0234
```

使用 `-q` 参数可关闭实时输出：
```bash
python3 main.py -w ~/my-project -q run
```

## 安全终止 (Ctrl+C)

执行过程中按 **Ctrl+C** 可安全终止：

```
============================================================
⚠️  检测到 Ctrl+C，正在安全终止...
============================================================

正在回退任务 [001] 的未完成更改...
   ✅ 已回退到 commit: a1b2c3d4
   ✅ 已重置任务 [001] 状态

下次可以继续运行: python3 main.py run
```

系统会自动：
1. **回退 Git** - 撤销未完成任务的所有更改
2. **重置任务状态** - 任务变回 `pending`，下次继续执行

### 手动恢复（如果自动回退失败）

```bash
# 查看 commit 历史
git log --oneline -5

# 回退到指定 commit
git reset --hard <commit-hash>

# 重置任务状态
python3 main.py reset-task <task_id>
```

## 失败处理

### 超时失败
系统自动将任务拆分为更小的子任务，回退 Git 后重新执行。

### 其他失败
显示详细指导信息，用户手动修复后：
```bash
python3 main.py reset-task 001
python3 main.py run
```

## 安全保护

- **初始快照**：首次初始化时提交所有现有文件
- **Git 追踪**：每个任务完成后自动 commit
- **敏感目录**：禁止使用 `/etc`、`/usr` 等系统目录
- **回滚能力**：出问题可 `git reset --hard` 恢复

## 文件说明

| 文件 | 说明 |
|------|------|
| `tasks.json` | 任务列表（用户创建） |
| `progress.md` | 进度日志（自动生成） |
| `init.sh` | 初始化脚本（自动生成） |

## 任务编写规范

详见 [TASKS_GUIDE.md](./TASKS_GUIDE.md)

核心原则：
- **单一职责**：每个任务只做一件事
- **明确边界**：指明文件路径和函数名
- **依赖顺序**：用 priority 控制执行顺序
- **可验证**：steps 应能检验完成情况

## 配置

编辑 `config.py`：

```python
SESSION_TIMEOUT = 900  # 任务超时时间（秒）
MAX_RETRIES = 2        # 最大重试次数
```
