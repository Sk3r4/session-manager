"""Session Review — 人为触发的对话复盘，提取 Pattern 写入 Markdown 文件。

借鉴 Hermes Agent background_review.py 的 Prompt 设计，但：
- 不自动后台运行，由用户在前端点击触发
- 产出 Markdown 文件（人可直接读写） + SQLite 索引
"""

from __future__ import annotations

import json
import logging
import re
import yaml
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import LLM_CONFIG, PATTERNS_DIR, PATTERN_TYPES

logger = logging.getLogger(__name__)

REVIEW_PROMPT = """审查以下 AI 编程会话，提取可复用知识。

输出 YAML，不要废话，不要 Markdown 代码块包装：
```yaml
findings:
  - type: workflow|pitfall|technique|preference
    title: "一行摘要（≤30字）"
    body: |
      完整的 Markdown 正文，包含：
      ## 触发场景
      ## 步骤/要点
      ## 注意事项
    confidence: high|medium|low
```

类型说明：
  workflow   - 可复用的操作流程或工作步骤
  pitfall    - 陷阱/易错点及避坑方法
  technique  - 非平凡的技巧、绕过方案
  preference - 用户表达的偏好（风格、格式、工作流习惯）

规则：
  - 不要记录环境配置错误（缺包、没装工具、路径错误）
  - 不要记录关于工具的否定断言（"X 工具坏了"）
  - 一条 finding 对应一个知识点，不要多个合并
  - 没有值得记录的输出空 findings: []
  - body 用 Markdown 写，包含明确的步骤和注意事项

会话内容：
{transcript}
"""


def _build_transcript_text(messages: List[Dict[str, Any]], max_chars: int = 16000) -> str:
    """将消息列表转为可审阅的纯文本，超长时截断中间部分。"""
    lines = []
    total = 0
    for i, msg in enumerate(messages):
        role = msg.get("role", "unknown")
        content = msg.get("content", "") or ""
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict):
                    parts.append(item.get("text", "") or str(item))
                else:
                    parts.append(str(item))
            content = " ".join(parts)
        content = content.strip()
        if not content:
            continue
        prefix = {"user": "[用户]", "assistant": "[AI]", "tool": "[工具]", "system": "[系统]"}.get(role, f"[{role}]")
        line = f"{prefix} {content[:800]}"
        total += len(line)
        if total > max_chars:
            break
        lines.append(line)
    return "\n\n".join(lines)


def _extract_yaml_block(text: str) -> Optional[Dict]:
    """从 LLM 返回的文本中提取 YAML 块。"""
    match = re.search(r"```yaml\s*\n(.*?)```", text, re.DOTALL)
    if match:
        try:
            return yaml.safe_load(match.group(1))
        except Exception:
            pass
    match = re.search(r"```\s*\n(.*?)```", text, re.DOTALL)
    if match:
        try:
            return yaml.safe_load(match.group(1))
        except Exception:
            pass
    try:
        return yaml.safe_load(text)
    except Exception:
        pass
    for marker in ("findings:", "findings:\n"):
        idx = text.find(marker)
        if idx >= 0:
            try:
                return yaml.safe_load(text[idx:])
            except Exception:
                pass
    return None


def _call_llm(prompt: str) -> str:
    """调用配置的 LLM API。"""
    provider = LLM_CONFIG.get("provider", "openai")
    base_url = LLM_CONFIG.get("base_url", "").strip()
    api_key = LLM_CONFIG.get("api_key", "").strip()
    model = LLM_CONFIG.get("model", "gpt-4o-mini")

    if not api_key:
        raise RuntimeError("未配置 LLM API key（设置环境变量 SESSION_MGR_LLM_API_KEY 或 OPENAI_API_KEY）")

    if provider != "openai" and not base_url:
        raise RuntimeError(f"LLM provider 为 '{provider}' 但未配置 base_url（设置 SESSION_MGR_LLM_BASE_URL）")

    if provider == "openai":
        from openai import OpenAI
        client = OpenAI(api_key=api_key, timeout=60.0)
    else:
        from openai import OpenAI
        client = OpenAI(base_url=base_url, api_key=api_key, timeout=60.0)

    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=LLM_CONFIG.get("temperature", 0.3),
        max_tokens=LLM_CONFIG.get("max_tokens", 4096),
    )
    return resp.choices[0].message.content or ""


_PATTERN_FRONTMATTER_TEMPLATE = """---
name: {name}
type: {ptype}
title: "{title}"
category: {category}
project: {project}
tags: {tags}
source_sessions: {source_sessions}
created: {created_at}
updated: {updated_at}
status: draft
---

"""


def _safe_name(title: str) -> str:
    """从标题生成安全的文件名。"""
    name = title.lower().strip()
    name = re.sub(r"[^\w\s-]", "", name)
    name = re.sub(r"\s+", "-", name)
    return name[:60]


def run_review(session_id: str, provider_id: str,
               messages: List[Dict[str, Any]],
               project_dir: str = "") -> List[Dict[str, Any]]:
    """执行单人触发的 Session Review。

    Returns:
        生成的 Pattern 列表（含 file_path），供前端展示。
    """
    transcript = _build_transcript_text(messages)
    prompt = REVIEW_PROMPT.format(transcript=transcript)

    raw = _call_llm(prompt)
    parsed = _extract_yaml_block(raw)

    if not parsed or not isinstance(parsed, dict):
        logger.warning("Review: failed to parse LLM output for %s", session_id)
        return []

    findings = parsed.get("findings", [])
    if not findings:
        return []

    source_sid = f"{provider_id}::{session_id}"
    results = []
    timestamp = datetime.utcnow().isoformat()

    for f in findings:
        if not isinstance(f, dict):
            continue
        ptype = f.get("type", "technique")
        if ptype not in PATTERN_TYPES:
            ptype = "technique"
        title = f.get("title", "Untitled").strip()
        body = f.get("body", "").strip()
        confidence = f.get("confidence", "medium")
        if not title or not body:
            continue

        name = _safe_name(title)
        subdir = ptype + "s"  # preferences / workflows / pitfalls / techniques
        file_path = PATTERNS_DIR / subdir / f"{name}.md"

        tags_list = [ptype, confidence]
        category = ""
        tags_json = json.dumps(tags_list, ensure_ascii=False)
        sessions_json = json.dumps([source_sid], ensure_ascii=False)

        frontmatter = _PATTERN_FRONTMATTER_TEMPLATE.format(
            name=name, ptype=ptype, title=title, category=category,
            project=project_dir or "", tags=tags_json,
            source_sessions=sessions_json,
            created_at=timestamp, updated_at=timestamp,
        )
        full_content = frontmatter + body + "\n"

        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(full_content, encoding="utf-8")

        results.append({
            "name": name,
            "type": ptype,
            "title": title,
            "file_path": str(file_path),
            "file_path_rel": str(file_path.relative_to(PATTERNS_DIR.parent)),
            "confidence": confidence,
            "tags": tags_list,
            "source_sessions": [source_sid],
        })

    return results
