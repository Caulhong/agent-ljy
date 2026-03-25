# nanobot 🐈

轻量级个人 AI 助手框架，支持多用户、多会话、工具调用、长期记忆，提供 HTTP API 与 Web 前端。

---

## 目录

- [环境要求](#环境要求)
- [快速开始](#快速开始)
- [配置说明](#配置说明)
- [工作空间结构](#工作空间结构)
- [数据库说明](#数据库说明)
- [启动方式](#启动方式)
- [前端使用](#前端使用)
- [Docker 部署](#docker-部署)

---

## 环境要求

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)（推荐，替代 pip/venv）

安装 uv：

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

---

## 快速开始

```bash
# 1. 克隆项目
git clone <repo-url>
cd nanobot

# 2. 用 uv 创建虚拟环境并安装依赖（锁定版本）
uv sync

# 3. 复制配置文件
cp config.example.json ~/.nanobot/config.json

# 4. 编辑配置，填入 API Key
nano ~/.nanobot/config.json

# 5. 启动服务
uv run nanobot gateway
```

---

## 配置说明

配置文件默认路径：`~/.nanobot/config.json`

```json
{
  "agents": {
    "defaults": {
      "model": "deepseek/deepseek-chat",
      "stream": true,
      "temperature": 0.1,
      "maxTokens": 8192,
      "maxToolIterations": 40,
      "memoryWindow": 100
    }
  },
  "providers": {
    "deepseek": {
      "apiKey": "YOUR_DEEPSEEK_API_KEY",
      "apiBase": "https://api.deepseek.com/v1"
    }
  },
  "channels": {
    "sendProgress": true,
    "sendToolHints": true,
    "http": {
      "enabled": true,
      "host": "0.0.0.0",
      "port": 8000
    }
  }
}
```

### 支持的模型提供商

| 字段 | 说明 | apiBase |
|------|------|---------|
| `deepseek` | DeepSeek | `https://api.deepseek.com/v1` |
| `anthropic` | Claude | 无需填写 |
| `openai` | OpenAI | 无需填写 |
| `openrouter` | OpenRouter（多模型） | `https://openrouter.ai/api/v1` |
| `zhipu` | 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4` |
| `dashscope` | 阿里云通义千问 | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| `custom` | 任意 OpenAI 兼容接口 | 自定义 |

model 字段格式为 `provider/model-name`，例如：
- `deepseek/deepseek-chat`
- `anthropic/claude-opus-4-5`
- `openai/gpt-4o`
- `openrouter/google/gemini-2.0-flash-exp`

### 主要配置项

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `stream` | `false` | 是否流式输出 token |
| `sendProgress` | `true` | 是否将中间思考文字推送到前端 |
| `sendToolHints` | `true` | 是否将工具调用信息推送到前端 |
| `memoryWindow` | `100` | 触发记忆整合的消息数阈值 |
| `maxToolIterations` | `40` | 单次请求最大工具调用轮数 |

---

## 工作空间结构

启动后自动初始化，默认位于 `~/.nanobot/workspace/`：

```
~/.nanobot/
├── config.json                  # 主配置文件
└── workspace/
    ├── AGENTS.md                # Agent 全局指令（只读，不可修改）
    ├── SOUL.md                  # Agent 人格设定（只读）
    ├── TOOLS.md                 # 工具使用说明（只读）
    ├── HEARTBEAT.md             # 定时任务描述
    ├── sessions.db              # 会话数据库（SQLite）
    ├── skills/                  # 技能插件目录
    │   ├── weather/
    │   ├── github/
    │   └── ...
    └── users/
        └── {user_id}/           # 每个用户独立目录（可读写）
            ├── USER.md          # 用户画像（姓名、偏好、指令）
            ├── MEMORY.md        # 长期记忆（Agent 自动维护）
            └── HISTORY.md       # 对话历史摘要（append-only）
```

**注意**：`workspace/` 根目录下的 `.md` 文件是全局配置，Agent 不会修改。用户数据只存在于 `workspace/users/{user_id}/` 下。

### 自定义 Agent 行为

编辑 `workspace/AGENTS.md` 可以覆盖 Agent 的默认指令，例如：
- 修改默认语言
- 添加业务约束
- 配置定时任务规则

编辑 `workspace/SOUL.md` 可以调整 Agent 的人格与风格。

---

## 数据库说明

nanobot 使用 SQLite 存储会话和消息，无需额外安装数据库服务。

**文件路径**：`~/.nanobot/workspace/sessions.db`

### 表结构

```sql
-- 用户表
CREATE TABLE users (
    user_id    TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);

-- 会话表
CREATE TABLE sessions (
    key               TEXT PRIMARY KEY,   -- 格式: http:{user_id}:{session_id}
    user_id           TEXT,
    title             TEXT,               -- 会话标题（取首条用户消息）
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    last_consolidated INTEGER DEFAULT 0,  -- 已整合到记忆的消息偏移
    metadata          TEXT DEFAULT '{}'
);

-- 消息表
CREATE TABLE messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_key  TEXT NOT NULL,
    role         TEXT NOT NULL,    -- user / assistant / tool
    content      TEXT,
    timestamp    TEXT NOT NULL,
    extra        TEXT DEFAULT '{}' -- tool_calls / tool_call_id 等扩展字段
);
```

会话 key 的格式：
- HTTP 访问：`http:{user_id}:{session_id}`
- CLI 访问：`cli:{username}`

查看数据（需要安装 sqlite3）：

```bash
sqlite3 ~/.nanobot/workspace/sessions.db

# 查看所有用户
SELECT * FROM users;

# 查看某用户的会话列表
SELECT key, title, updated_at FROM sessions WHERE user_id = 'dqh';

# 查看某会话的消息
SELECT role, substr(content, 1, 80) FROM messages WHERE session_key = 'http:dqh:xxx';
```

---

## 启动方式

### 开发模式（推荐）

```bash
# 启动 HTTP API + Web 前端
uv run nanobot gateway

# 命令行交互模式
uv run nanobot chat

# 查看状态
uv run nanobot status
```

### 直接使用虚拟环境

```bash
# 创建虚拟环境
uv venv

# 激活
source .venv/bin/activate   # macOS/Linux
.venv\Scripts\activate      # Windows

# 安装依赖（锁定版本）
uv sync

# 启动
python -m nanobot gateway
```

### 依赖管理

```bash
# 安装所有依赖（含 dev）
uv sync --all-extras

# 更新 uv.lock
uv lock

# 添加新依赖
uv add some-package

# 查看依赖树
uv tree
```

---

## 前端使用

启动 `gateway` 后，用浏览器直接打开：

```
frontend/index.html
```

或通过静态文件服务器访问（支持本地直接打开，无需额外服务器）。

首次访问会弹出用户选择界面，创建用户后即可开始对话。

### API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/health` | 健康检查 |
| `POST` | `/api/chat` | 发送消息（同步等待回复） |
| `POST` | `/api/chat/stream` | 发送消息（SSE 流式回复） |
| `GET` | `/api/users` | 获取用户列表 |
| `POST` | `/api/users` | 创建用户 |
| `GET` | `/api/sessions` | 获取会话列表（`?user_id=xxx`） |
| `GET` | `/api/sessions/messages` | 获取会话消息（`?key=xxx`） |
| `DELETE` | `/api/sessions` | 删除会话（`?key=xxx`） |

---

## Docker 部署

```bash
# 构建并启动
docker compose up -d nanobot-gateway

# 查看日志
docker compose logs -f nanobot-gateway

# 停止
docker compose down
```

配置文件和数据会挂载到 `~/.nanobot/`，数据持久化在宿主机。

默认端口：`18790`（Docker）/ `8000`（本地直接运行）。
