# Session Manager — AI CLI 会话聚合管理

> 通过 MCP 让 AI 客户端自动获取本机 AI CLI 会话历史——"我之前在这个目录干过什么"的环境上下文，一键可得。

![Python](https://img.shields.io/badge/Python-3.11%2B-blue)
![MCP](https://img.shields.io/badge/MCP-server-2563EB)
![License](https://img.shields.io/badge/License-MIT-yellow)

---

## ✨ 这是什么

v2 起，Session Manager 的核心形态是 **MCP Server**：接入 Claude Code / Codex / ZCode 等 AI 客户端后，AI 可以直接读取、检索、归类你本机的历史会话，用于复盘、续接和跨会话协作。

| 能力 | 说明 |
|------|------|
| 🔍 **环境上下文** | AI 一键获知"当前项目/目录下我之前会话干过什么" |
| 🧠 **会话检索** | 按 provider、项目、关键词、全文（FTS5）搜索历史会话 |
| 📜 **转录读取** | 读取任意会话的完整对话内容（含工具调用） |
| 🏷️ **受控写入** | 合集归类、摘要/标签标注、会话改名（不暴露删除等破坏性操作） |
| 🔄 **索引刷新** | 按需重新扫描各 CLI 的会话目录，保持索引最新 |
| 🤖 **一键接入** | `session-manager-install` 自动写入客户端 MCP 配置（写前备份、幂等） |

### 支持的 Provider

| Provider | 数据源 |
|----------|--------|
| Claude Code | `~/.claude/projects/**/*.jsonl` |
| Codex CLI | `~/.codex/sessions/` + `~/.codex/archived_sessions/` |
| ZCode | `~/.zcode/cli/db.sqlite`（只读） |
| 通用 | 任意 JSON/JSONL 目录（`generic` 适配器） |

---

## 🚀 快速开始

### 方式一：pip 安装（推荐）

```bash
pip install session_manager-2.1.0-py3-none-any.whl

# 把 MCP 注册进本机已安装的 AI 客户端（ZCode / Claude Code / Codex）
session-manager-install
```

### 方式二：仓库运行

```bash
git clone https://github.com/Sk3r4/session-manager.git
cd session-manager
pip install -r requirements.txt
```

### 接入 MCP 客户端

在任意支持 MCP 的 AI 客户端配置中加入：

```json
{
  "mcpServers": {
    "sessions": {
      "command": "session-manager-mcp",
      "args": []
    }
  }
}
```

或直接以脚本方式运行：

```json
{
  "mcpServers": {
    "sessions": {
      "command": "python",
      "args": ["D:\\path\\to\\session-manager\\mcp_server.py"]
    }
  }
}
```

`session-manager-install` 会自动完成上述写入：只修改**已存在**的客户端配置（不为未安装的客户端创建配置），写前生成 `.bak` 备份，重复执行幂等。

---

## 🏗️ 项目结构

```
session-manager/
├── mcp_server.py         # MCP Server（stdio，核心入口）
├── setup_cli.py          # session-manager-install：一键注册 MCP 到客户端
├── config.py             # Provider 默认配置 + 数据目录解析
├── transcript_util.py    # 转录合并、智能摘要
├── errors.py
├── adapters/             # Provider 适配器（可扩展）
│   ├── base.py           # 适配器基类
│   ├── claude_code.py
│   ├── codex.py
│   ├── zcode.py
│   └── generic.py        # 通用 JSONL 扫描兜底
├── storage/              # SQLite 持久层（WAL）
│   ├── schema.py         # 版本化迁移（PRAGMA user_version 驱动，幂等）
│   ├── index.py          # IndexDB 门面
│   ├── sessions.py       # 会话 CRUD
│   ├── projects.py       # 项目聚合
│   ├── collections.py    # 合集归类
│   ├── tags.py           # 标签
│   ├── fts.py            # FTS5 全文索引
│   └── base.py
├── services/
│   ├── scan.py           # 扫描编排（调用 adapters 入库）
│   └── annotate.py       # AI 标注（摘要/标签）
└── data/                 # 运行时数据（index.db，自动创建，不入库）
```

---

## 💾 数据目录

| 模式 | 位置 |
|------|------|
| 仓库检出（含 `.git`/`static`） | `<repo>/data/` |
| 包安装（pip/uvx） | `~/.session-manager/` |
| 强制覆盖 | 环境变量 `SM_DATA_DIR` |

Web、MCP、后台标注线程共享同一个 `index.db`，WAL 模式保证多写者并发安全。

---

## 🔧 扩展 Provider

1. 在 `adapters/` 下新建适配器类，继承 `ProviderAdapter`
2. 实现 `detect()` / `scan_sessions()` / `load_transcript()`
3. 在 `adapters/__init__.py` 的 `build_adapter()` 注册
4. 在 `config.py` 的 `DEFAULT_PROVIDER_CONFIGS` 添加默认配置

---

## 📦 版本

- 当前：**v2.1.0**（瘦身后基线 schema v1）
- v1 时代（FastAPI Web 应用形态）的代码保留在 git 历史中，tag 之前可查

## 📄 许可证

[MIT License](LICENSE)
