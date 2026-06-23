# Session Manager API 使用指南

> 所有端点 base URL: `http://127.0.0.1:7821` — 返回 JSON。

---

## 快速索引

| 你想做什么 | 端点 | 方法 |
|-----------|------|------|
| 存入新会话（任何 CLI） | `/api/sessions/ingest` | POST |
| 注册新 CLI 工具 | `/api/providers/register` | POST |
| 扫描本地历史会话 | `/api/scan-all` | POST |
| 查看全部会话 | `/api/sessions` | GET |
| 按条件过滤会话 | `/api/sessions?provider=...&status=...&search=...` | GET |
| 查看某条会话详情 | `/api/sessions/{id}` | GET |
| 修改会话标题/状态 | `/api/sessions/{id}` | PUT |
| 删除会话 | `/api/sessions/{id}` | DELETE |
| 置顶/取消置顶 | `/api/sessions/{id}/pin` / `/unpin` | POST |
| 查看对话内容 | `/api/sessions/{id}/transcript` | GET |
| 获取恢复命令 | `/api/sessions/{id}/copy-command` | GET |
| 启动终端恢复 | `/api/sessions/{id}/launch` | POST |
| 导出 Markdown | `/api/sessions/{id}/export?format=md` | GET |
| 导出 PDF | `/api/sessions/{id}/export?format=pdf` | GET |
| 批量归档 | `/api/archive/batch` | POST |
| 全文搜索 | `/api/search?q=关键词` | GET |
| 健康检查 | `/api/health` | GET |
| 统计概览 | `/api/stats` | GET |
| 列出项目 | `/api/projects` | GET |

---

## 核心端点详解

### 1. 存入新会话 — 主要入口

```bash
curl -X POST http://127.0.0.1:7821/api/sessions/ingest \
  -H "Content-Type: application/json" \
  -d '{
    "provider": "my-cli",
    "session_id": "session-abc123",
    "title": "修复登录 500 错误",
    "project_dir": "D:/projects/app",
    "messages": [
      {"role": "user", "content": "登录报 500，请排查", "ts": 1719000000.0},
      {"role": "assistant", "content": "我来检查后端日志...", "ts": 1719000001.0},
      {"role": "tool", "content": "[工具调用: grep] error.log", "ts": 1719000002.0}
    ]
  }'
```

**响应**:
```json
{
  "session_id": "my-cli::session-abc123",
  "action": "inserted",
  "archived": true,
  "archived_path": "D:/.../data/archives/...",
  "message_count": 3
}
```

**关键点**:
- `provider` 可以是任意字符串（不限于内置 adapter），未注册的 provider 自动创建
- `messages` 中每条需含 `role`(user/assistant/tool/system) 和 `content`(字符串或列表)
- `ts` 为 Unix 秒级时间戳（可选）
- 不用调 scan，数据立即可查、可搜索、可恢复

### 2. 注册新 CLI 工具

```bash
curl -X POST http://127.0.0.1:7821/api/providers/register \
  -H "Content-Type: application/json" \
  -d '{
    "id": "my-cli",
    "name": "My Custom AI CLI",
    "resume_command_template": "mycli resume \"{session_id}\""
  }'
```

注册后：
- 出现在 `GET /api/providers` 列表中
- `GET /api/sessions/{id}/copy-command` 返回 `mycli resume "session-abc123"`
- `POST /api/sessions/{id}/launch` 启动对应命令

### 3. 查看会话（带过滤）

```bash
# 全部会话
curl http://127.0.0.1:7821/api/sessions

# 某个项目的会话
curl "http://127.0.0.1:7821/api/sessions?project_dir=D:/projects/app"

# 进行中的会话
curl "http://127.0.0.1:7821/api/sessions?status=进行中"

# 组合过滤
curl "http://127.0.0.1:7821/api/sessions?provider=claude-code&status=已完成&search=bug"
```

### 4. 修改会话状态/标题

```bash
curl -X PUT http://127.0.0.1:7821/api/sessions/my-cli::abc123 \
  -H "Content-Type: application/json" \
  -d '{"status": "已完成", "title": "修复登录 500 → 已部署"}'
```

状态可选值: `未标注` / `进行中` / `已完成` / `待跟进` / `已归档`

### 5. 恢复会话

```bash
# 方式一：只拿命令文本（复制到剪贴板）
curl http://127.0.0.1:7821/api/sessions/my-cli::abc123/copy-command
# → {"command": "mycli resume \"abc123\""}

# 方式二：自动打开终端执行
curl -X POST http://127.0.0.1:7821/api/sessions/my-cli::abc123/launch
```

### 6. 导出

```bash
# Markdown
curl http://127.0.0.1:7821/api/sessions/my-cli::abc123/export?format=md -o session.md

# PDF
curl http://127.0.0.1:7821/api/sessions/my-cli::abc123/export?format=pdf -o session.pdf
```

---

## 框架/脚本对接示例

### PowerShell 脚本中推送会话

```powershell
# dispatch.ps1 结束时调用
$body = @{
    provider = "kimi-code"
    session_id = "abc123"
    title = "分析目标 X"
    project_dir = "D:\Webtools\projects\active\Phage"
    messages = @(
        @{ role = "user"; content = "开始分析"; ts = 1719000000 }
        @{ role = "assistant"; content = "分析完成..."; ts = 1719000060 }
    )
} | ConvertTo-Json -Depth 3

Invoke-RestMethod -Uri "http://127.0.0.1:7821/api/sessions/ingest" `
    -Method Post -Body $body -ContentType "application/json"
```

### Python 脚本中推送会话

```python
import requests, time

requests.post("http://127.0.0.1:7821/api/sessions/ingest", json={
    "provider": "custom-agent",
    "session_id": "task-001",
    "title": "Code review for PR #42",
    "project_dir": "/home/user/project",
    "messages": [
        {"role": "user", "content": "Review this PR", "ts": time.time()},
    ]
})
```

---

## 完整工作流示例

```bash
# 1. 注册新工具
curl -X POST http://127.0.0.1:7821/api/providers/register \
  -H "Content-Type: application/json" \
  -d '{"id":"dev-agent","name":"Dev Agent","resume_command_template":"dev-agent resume \"{session_id}\""}'

# 2. 推送第一次对话
curl -X POST http://127.0.0.1:7821/api/sessions/ingest \
  -H "Content-Type: application/json" \
  -d '{"provider":"dev-agent","session_id":"task-001","title":"Add login form","project_dir":"/projects/app","messages":[{"role":"user","content":"Make a login form","ts":1719000000}]}'

# 3. 浏览器查看: http://127.0.0.1:7821 → 搜索 "login form" → 点击查看详情

# 4. 下次需要恢复时
curl -X POST http://127.0.0.1:7821/api/sessions/dev-agent::task-001/launch
```

---

## 注意

- 服务默认监听 `127.0.0.1:7821`，仅本机可访问
- 所有数据存于 `data/index.db`（SQLite）+ `data/archives/`（Markdown）
- 会话 ID 格式为 `{provider_id}::{session_id}`
- 旧数据的扫描导入仍可用 `POST /api/scan-all`（走 adapter）
