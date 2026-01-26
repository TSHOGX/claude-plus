# tasks.json 规范说明

## 核心思想

```
一个任务 = 一次 Claude 会话
任务树 = DFS 前序遍历
```

任务粒度应该足够小，让 Claude 在单次会话（10-15分钟）内完成，避免上下文溢出。

---

## 任务 ID 规范（路径编码）

任务 ID 采用层级编号（Dewey Decimal），用 `.` 分隔层级：

```
顶层任务:    1, 2, 3
子任务:      1.1, 1.2, 2.1
更深层级:    1.1.1, 2.3.4
```

**执行顺序**：按 ID 排序后从上往下依次执行（深度优先前序遍历）

```
1       ← 第1个执行
1.1     ← 第2个执行
1.1.1   ← 第3个执行
1.2     ← 第4个执行
2       ← 第5个执行
2.1     ← 第6个执行
```

**可视化**：
```
        1
       / \
     1.1  1.2
     /
   1.1.1

执行顺序: 1 → 1.1 → 1.1.1 → 1.2
```

---

## 结构定义

```json
[
  {
    "id": "1",                            // 必填：路径编码（如 "1", "1.2", "2.1.3"）
    "description": "实现用户登录功能",      // 必填：一句话描述任务目标
    "steps": [                             // 必填：具体执行步骤
      "创建 auth.py 文件",
      "实现 login(username, password) 函数",
      "添加密码验证逻辑"
    ]
  },
  {
    "id": "1.1",                           // 1 的子任务
    "description": "实现密码加密",
    "steps": ["添加 bcrypt 依赖", "实现 hash_password()"]
  },
  {
    "id": "2",                             // 另一个顶层任务
    "description": "实现用户注册功能",
    "steps": ["..."]
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
| **层级清晰** | 用 ID 层级表达依赖关系 | `2.1` 是 `2` 的子任务 |
| **可验证** | steps 应能检验完成情况 | "添加单元测试" / "函数应返回 True/False" |

---

## 任务树示例

### 简单线性任务

```json
[
  {"id": "1", "description": "创建项目结构", "steps": ["mkdir src", "touch README.md"]},
  {"id": "2", "description": "实现核心功能", "steps": ["创建 main.py"]},
  {"id": "3", "description": "添加测试", "steps": ["创建 test_main.py"]}
]
```

执行顺序: `1 → 2 → 3`

### 分支任务树

```json
[
  {"id": "1", "description": "项目初始化", "steps": ["创建目录结构"]},
  {"id": "1.1", "description": "配置开发环境", "steps": ["安装依赖"]},
  {"id": "1.2", "description": "配置生产环境", "steps": ["设置环境变量"]},
  {"id": "2", "description": "核心功能开发", "steps": []},
  {"id": "2.1", "description": "用户模块", "steps": []},
  {"id": "2.1.1", "description": "实现登录", "steps": ["创建 login()"]},
  {"id": "2.1.2", "description": "实现注册", "steps": ["创建 register()"]},
  {"id": "2.2", "description": "文章模块", "steps": []},
  {"id": "2.2.1", "description": "实现发布", "steps": ["创建 publish()"]},
  {"id": "3", "description": "测试与部署", "steps": ["运行测试", "部署"]}
]
```

执行顺序: `1 → 1.1 → 1.2 → 2 → 2.1 → 2.1.1 → 2.1.2 → 2.2 → 2.2.1 → 3`

可视化:
```
1 项目初始化
├── 1.1 配置开发环境
└── 1.2 配置生产环境
2 核心功能开发
├── 2.1 用户模块
│   ├── 2.1.1 实现登录
│   └── 2.1.2 实现注册
└── 2.2 文章模块
    └── 2.2.1 实现发布
3 测试与部署
```

---

## 插入任务

### 在末尾追加

已有: `1, 2, 3`，追加顶层任务: `4`

### 添加子任务

已有: `2`，添加子任务: `2.1, 2.2`

### 在中间插入

已有: `2.1, 2.3`，在中间插入: `2.2`

### 建议：使用稀疏编号

预留插入空间，使用 10, 20, 30 而非 1, 2, 3：

```json
[
  {"id": "1", "description": "阶段一"},
  {"id": "1.10", "description": "步骤一"},
  {"id": "1.20", "description": "步骤二"},
  {"id": "2", "description": "阶段二"}
]
```

后续插入 `1.15` 在 `1.10` 和 `1.20` 之间。

---

## 完整示例：博客系统

```json
[
  {"id": "1",
   "description": "项目基础设施",
   "steps": ["创建项目目录", "初始化 git", "创建 requirements.txt"]},

  {"id": "1.1",
   "description": "配置开发环境",
   "steps": ["创建 .env.example", "配置 logging"]},

  {"id": "2",
   "description": "数据层",
   "steps": []},

  {"id": "2.1",
   "description": "创建 Post 数据模型",
   "steps": ["创建 models/post.py", "定义 Post 类", "添加序列化方法"]},

  {"id": "2.2",
   "description": "创建 User 数据模型",
   "steps": ["创建 models/user.py", "定义 User 类"]},

  {"id": "3",
   "description": "业务层",
   "steps": []},

  {"id": "3.1",
   "description": "实现文章管理",
   "steps": []},

  {"id": "3.1.1",
   "description": "实现文章存储",
   "steps": ["创建 PostManager 类", "实现 save_post()", "JSON 持久化"]},

  {"id": "3.1.2",
   "description": "实现文章列表",
   "steps": ["添加 list_posts()", "支持分页", "按时间倒序"]},

  {"id": "3.1.3",
   "description": "实现文章搜索",
   "steps": ["添加 search_posts(keyword)", "搜索标题和内容"]},

  {"id": "3.2",
   "description": "实现用户管理",
   "steps": ["创建 UserManager", "实现 CRUD"]},

  {"id": "4",
   "description": "测试",
   "steps": ["编写单元测试", "运行 pytest"]}
]
```
