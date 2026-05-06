# AGENTS.md — Session Manager

本文件面向 AI 编码助手，描述项目架构、关键约定和开发注意事项。

## 项目概述

一个 FastAPI 后端 + 原生 HTML/JS 前端的本地 Web 应用，用于聚合管理多个 AI CLI 工具的会话历史。

## 技术栈

- **后端**: Python 3.11, FastAPI, Uvicorn, SQLite
- **前端**: 原生 HTML/JS, Tailwind CSS (CDN), 无构建步骤
- **导出**: fpdf2 (PDF), 纯文本 (Markdown)

## 核心架构

### 1. 适配器模式 (Adapters)

每个 AI CLI 工具有独立的 `ProviderAdapter` 子类：

- **ClaudeCodeAdapter** (`adapters/claude_code.py`): 解析 `.claude/projects/{project}/*.jsonl`
- **KimiCodeAdapter** (`adapters/kimi_code.py`): 解析 `.kimi/sessions/{workspace_hash}/{uuid}/context.jsonl`
- **CodexAdapter** (`adapters/codex.py`): 解析 `.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`
- **GenericFileAdapter** (`adapters/generic.py`): 通用 JSON/JSONL 扫描，兜底适配器

#### Kimi Code 特殊机制

Kimi CLI 的 `Session.find(work_dir, session_id)` 只在**当前目录对应的 workspace** 中查找 session。跨目录执行 `kimi -r "id"` 会导致 "not found, creating new session"（空会话）。

**解决方案**:
1. `_read_project_dir_from_state()` 从 `state.json` 的 `custom_title` 推断目录
2. `_read_project_dir_from_kimi_json()` 从 `~/.kimi/kimi.json` 的 `work_dirs[].path` 按 `last_session_id` 匹配
3. `get_resume_command()`: 已知 `project_dir` 时返回 `kimi -r "id"`（launch 时自动 cd），未知时降级为 `kimi -r`（交互式选择）

### 2. 数据库 (storage/index.py)

SQLite，单表 `sessions`，字段：
- `id`: `{provider_id}::{session_id}` 复合主键
- `provider_id`, `session_id`, `title`, `summary`, `project_dir`
- `status`: 默认 "未标注"，可选值见 `STATUS_OPTIONS`
- `created_at`, `last_active_at`: 秒级时间戳
- `source_path`: 原始数据文件绝对路径
- `raw_meta`: JSON 字符串
- `pinned`, `pinned_at`: 置顶标记

**upsert 策略**: 更新时保留已有的 `summary` 和 `status`，只更新时间戳和路径信息。

### 3. API 端点 (main.py)

- `GET /api/providers` — 列出所有 provider 及检测状态
- `POST /api/providers/{id}/scan` — 扫描单个 provider
- `POST /api/scan-all` — 扫描全部
- `GET /api/sessions` — 列表，支持 `?provider=&status=&search=` 过滤
- `GET /api/sessions/{id}` — 获取单条
- `PUT /api/sessions/{id}` — 更新 title / status
- `GET /api/sessions/{id}/transcript` — 加载完整对话
- `GET /api/sessions/{id}/copy-command` — 获取 resume 命令
- `POST /api/sessions/{id}/launch` — 在新 PowerShell 窗口中启动并 resume
- `GET /api/sessions/{id}/export?format=md|pdf` — 导出为 Markdown 或 PDF
- `POST /api/sessions/{id}/pin` / `unpin` — 置顶/取消置顶
- `GET /api/stats` — 各 provider 统计

### 4. 导出 (exporter.py)

- **Markdown**: 带元信息表格、角色 emoji、代码块包裹内容
- **PDF**: 使用 fpdf2 + Windows 微软雅黑字体 (`C:\Windows\Fonts\msyh.ttc`)，角色标签带颜色区分

## 关键约定

### 时间戳处理

各 provider 的时间戳格式不一：秒级/毫秒级/ISO 字符串。统一通过 `_parse_ts()` 处理：
- 数值 > 1e12 → 除以 1000（毫秒转秒）
- ISO 字符串 → `datetime.fromisoformat`

### 内容提取

Kimi 的 `content` 字段可能是字符串或列表（含 `text`/`think`/`thinking`/`tool_use`/`tool_result`）。`_extract_text_from_content()` 负责统一提取。

### Windows 编码

PowerShell 默认 GBK，Kimi TUI 中文输出会崩溃。`launch_session` 构建的命令必须以：
```powershell
chcp 65001 > $null; [Console]::OutputEncoding = [System.Text.Encoding]::UTF8; cd "project_dir"; resume_cmd
```

### 前端缓存

`index.html` 返回时带 `Cache-Control: no-store` 头，但浏览器仍可能缓存 JS。更新后务必提醒用户 **Ctrl + F5** 强制刷新。

## 开发注意事项

1. **端口占用**: 7821 若被旧进程占用，新代码不生效。启动脚本已包含端口检测逻辑。
2. **虚拟环境**: 避免使用被 pyenv shim 污染的系统 Python，优先使用 `.venv` 中的解释器。
3. **数据目录**: `data/` 目录和 `index.db` 由运行时自动创建，不应提交到 Git。
4. **新增 Provider**: 必须同时修改 `config.py`（默认配置）、`adapters/__init__.py`（注册）、创建适配器类。
5. **路径处理**: Windows 路径使用 `pathlib.Path`，避免硬编码 `\\` 或 `/` 分割。
