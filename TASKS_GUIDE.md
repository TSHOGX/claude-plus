# tasks.json 规范说明

## 核心思想

```
一个任务 = 一次 Claude 会话
```

任务粒度应该足够小，让 Claude 在单次会话（10-15分钟）内完成，避免上下文溢出。

---

## 结构定义

```json
[
  {
    "id": "001",                          // 必填：唯一标识符
    "description": "实现用户登录功能",      // 必填：一句话描述任务目标
    "priority": 1,                         // 必填：执行优先级（数字越小越优先）
    "steps": [                             // 必填：具体执行步骤
      "创建 auth.py 文件",
      "实现 login(username, password) 函数",
      "添加密码验证逻辑"
    ]
  }
]
```

### 系统自动管理的字段

| 字段 | 说明 |
|------|------|
| `status` | 状态：`pending` / `in_progress` / `completed` / `failed` |
| `session_id` | Claude 会话 ID |
| `error_message` | 失败时的错误信息 |
| `notes` | 执行备注，供 Worker 和 Orchestrator 参考 |

---

## 编写原则

| 原则 | 说明 | 示例 |
|------|------|------|
| **单一职责** | 每个任务只做一件事 | ✅ "创建数据模型" ❌ "创建模型并实现所有API" |
| **明确边界** | 指明文件路径和函数名 | ✅ "在 utils.py 中添加 format_date()" |
| **依赖顺序** | 用 priority 控制执行顺序 | 模型(1) → CRUD(2-5) → 界面(6) |
| **可验证** | steps 应能检验完成情况 | "添加单元测试" / "函数应返回 True/False" |

---

## 任务分解示例

**项目：博客系统**

```json
[
  {"id": "001", "priority": 1,
    "description": "创建 Post 数据模型",
    "steps": ["创建 models/post.py", "定义 Post 类", "添加序列化方法"]},

  {"id": "002", "priority": 2,
    "description": "实现文章存储功能",
    "steps": ["创建 PostManager 类", "实现 save_post()", "JSON 持久化"]},

  {"id": "003", "priority": 3,
    "description": "实现文章列表功能",
    "steps": ["添加 list_posts()", "支持分页", "按时间倒序"]},

  {"id": "004", "priority": 4,
    "description": "实现文章搜索功能",
    "steps": ["添加 search_posts(keyword)", "搜索标题和内容"]}
]
```
