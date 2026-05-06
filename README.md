# Session Manager — AI CLI 会话管理中心

> 一站式管理 Claude Code、Kimi Code、Codex CLI 等 AI 编程助手的本地会话记录，支持浏览、搜索、恢复、导出。

![Python](https://img.shields.io/badge/Python-3.11%2B-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115%2B-green)
![License](https://img.shields.io/badge/License-MIT-yellow)

---

## ✨ 功能特性

| 功能 | 说明 |
|------|------|
| 🔍 **自动扫描** | 一键扫描本地已安装的 AI CLI 工具（Claude Code / Kimi Code / Codex），自动发现历史会话 |
| 🏷️ **状态管理** | 为会话打标签：未标注 / 进行中 / 已完成 / 待跟进 / 已归档 |
| 📌 **置顶会话** | 重要会话可置顶，始终排在列表最前 |
| 🔎 **全文搜索** | 按标题、摘要、项目目录关键词实时过滤 |
| 📝 **对话浏览** | 点击卡片查看完整对话记录，支持思考过程、工具调用、工具结果 |
| 🚀 **一键恢复** | 直接在正确的工作目录打开新终端并恢复会话，无需手动 `cd` |
| 📋 **复制命令** | 复制 resume 命令到剪贴板，方便在其他终端中使用 |
| 📤 **导出 Markdown** | 将会话导出为 `.md` 文件，含完整元信息和时间戳 |
| 📤 **导出 PDF** | 使用微软雅黑字体生成中文友好的 PDF 文档 |
| 🖥️ **Web 界面** | 基于 Tailwind CSS 的响应式单页应用，笔记本 / 大屏自适应 |

---

## 🏗️ 项目结构

```
session-manager/
├── adapters/              # Provider 适配器（可扩展）
│   ├── base.py            # 适配器基类
│   ├── claude_code.py     # Claude Code 适配器
│   ├── codex.py           # Codex CLI 适配器
│   ├── kimi_code.py       # Kimi Code 适配器（含 work_dirs 映射解析）
│   └── generic.py         # 通用文件扫描适配器
├── storage/
│   └── index.py           # SQLite 数据库操作
├── static/
│   └── index.html         # Web 前端（单页应用）
├── config.py              # Provider 配置与默认设置
├── exporter.py            # Markdown / PDF 导出
├── main.py                # FastAPI 入口
├── start.ps1              # Windows PowerShell 启动脚本
├── start.bat              # Windows CMD 启动脚本
├── requirements.txt       # Python 依赖
└── data/                  # 运行时数据（SQLite 数据库，自动创建）
```

---

## 🚀 快速开始

### 1. 克隆项目

```bash
git clone https://github.com/YOUR_GITHUB_USERNAME/session-manager.git
cd session-manager
```

### 2. 创建虚拟环境（推荐）

```bash
# Windows (PowerShell)
python -m venv .venv
.venv\Scripts\activate

# macOS / Linux
python3 -m venv .venv
source .venv/bin/activate
```

### 3. 安装依赖

```bash
pip install -r requirements.txt
```

### 4. 启动服务

```bash
# 方式一：直接启动
python -m uvicorn main:app --host 127.0.0.1 --port 7821

# 方式二：使用启动脚本（Windows）
.\start.ps1        # PowerShell
.\start.bat        # CMD
```

### 5. 打开浏览器

访问 [http://127.0.0.1:7821](http://127.0.0.1:7821)

---

## 📋 使用指南

### 首次使用

1. 打开页面后，点击 **"扫描全部"** 按钮，系统会自动检测已安装的 AI CLI 并导入历史会话
2. 如果没有自动检测到，检查 `config.py` 中的 `scan_paths` 是否匹配你的安装路径

### 日常操作

- **查看对话**：点击会话卡片，右侧滑出详情面板
- **修改状态**：在卡片右上角下拉选择新状态
- **置顶/取消置顶**：点击 📌 图标
- **恢复会话**：点击 "启动终端恢复"，会自动 `cd` 到正确目录并执行 resume 命令
- **导出记录**：在详情面板中点击 "导出 Markdown" 或 "导出 PDF"

### 支持的 Provider

| Provider | 数据路径（默认） | resume 命令 |
|----------|-----------------|------------|
| Claude Code | `~/.claude/projects/` | `claude -r "{session_id}"` |
| Kimi Code | `~/.kimi/sessions/` | `kimi -r "{session_id}"` |
| Codex CLI | `~/.codex/sessions/` | `codex resume "{session_id}"` |

> Kimi Code 的 resume 命令**必须在正确的项目目录下执行**，否则会导致恢复后丢失上下文。Session Manager 已自动处理此问题。

---

## ⚙️ 配置说明

首次启动后，会在 `data/providers.json` 中持久化配置。你可以手动编辑以：

- 启用/禁用某个 Provider
- 修改扫描路径（如果安装位置非默认）
- 添加自定义 Provider（需编写适配器）

```json
{
  "id": "my-provider",
  "name": "My Custom CLI",
  "enabled": true,
  "adapter_type": "generic",
  "scan_paths": ["C:/custom/path/sessions"],
  "file_patterns": ["*.jsonl"],
  "resume_command_template": "mycli resume \"{session_id}\""
}
```

---

## 🔧 扩展适配器

要支持新的 AI CLI 工具，只需：

1. 在 `adapters/` 下创建新的适配器类，继承 `ProviderAdapter`
2. 实现 `detect()`、`scan_sessions()`、`load_transcript()` 方法
3. 在 `adapters/__init__.py` 的 `build_adapter()` 中注册
4. 在 `config.py` 的 `DEFAULT_PROVIDER_CONFIGS` 中添加默认配置

参考 `adapters/kimi_code.py` 了解完整的实现范例。

---

## 🖥️ 开发环境

- **操作系统**: Windows 10/11（主要测试平台），理论兼容 macOS/Linux
- **Python**: 3.11+
- **前端**: 原生 HTML + Tailwind CSS CDN（零构建）
- **数据库**: SQLite（零配置）

### Windows 特殊说明

- PowerShell 默认使用 GBK 编码，Kimi CLI 的 Rich TUI 输出中文可能崩溃
- Session Manager 在启动终端时已自动注入 `chcp 65001` + UTF-8 编码设置
- 前端更新后请按 **Ctrl + F5** 强制刷新，避免浏览器缓存旧版本

---

## 📄 许可证

[MIT License](LICENSE)
