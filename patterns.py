"""Pattern Manager — Markdown 文件本体 + SQLite 索引的 CRUD 与聚合。

Pattern 存储：
  - 本体：data/patterns/{type}s/{name}.md（Markdown + YAML frontmatter）
  - 索引：index.db patterns_idx 表（搜索/聚合用，不含全文）

借鉴 Hermes Agent curator.py 的前缀聚类和 Umbrella 合并思路。
"""

from __future__ import annotations

import json
import re
import yaml
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from config import PATTERNS_DIR, PATTERN_TYPES, PATTERN_STATUSES
from storage.index import IndexDB

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def read_pattern_file(name: str, ptype: str) -> Optional[Dict[str, Any]]:
    """读取 Pattern Markdown 文件，返回 {frontmatter, body, file_path}。"""
    subdir = ptype + "s"
    file_path = PATTERNS_DIR / subdir / f"{name}.md"
    if not file_path.exists():
        return None
    text = file_path.read_text(encoding="utf-8")
    m = FRONTMATTER_RE.match(text)
    if not m:
        return None
    try:
        fm = yaml.safe_load(m.group(1))
    except Exception:
        fm = {}
    body = text[m.end():].strip()
    return {"frontmatter": fm, "body": body, "file_path": file_path}


def write_pattern_file(name: str, ptype: str, title: str, body: str,
                       category: str = "", project: str = "",
                       tags: List[str] = None, source_sessions: List[str] = None,
                       status: str = "draft") -> Path:
    """写入或更新 Pattern Markdown 文件。"""
    subdir = ptype + "s"
    file_path = PATTERNS_DIR / subdir / f"{name}.md"
    file_path.parent.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.utcnow().isoformat()
    existing = read_pattern_file(name, ptype)
    created_at = timestamp
    if existing:
        created_at = existing["frontmatter"].get("created", timestamp)

    tags_json = json.dumps(tags or [], ensure_ascii=False)
    sessions_json = json.dumps(source_sessions or [], ensure_ascii=False)

    fm_text = f"""---
name: {name}
type: {ptype}
title: "{title}"
category: {category}
project: {project}
tags: {tags_json}
source_sessions: {sessions_json}
created: {created_at}
updated: {timestamp}
status: {status}
---

"""
    file_path.write_text(fm_text + body.strip() + "\n", encoding="utf-8")
    return file_path


def archive_pattern(name: str, ptype: str) -> Tuple[bool, str]:
    """将 Pattern 移动到 .archive/ 目录。"""
    subdir = ptype + "s"
    src = PATTERNS_DIR / subdir / f"{name}.md"
    if not src.exists():
        return False, f"Pattern file not found: {src}"
    dest_dir = PATTERNS_DIR / ".archive"
    dest_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    dest = dest_dir / f"{name}.{ts}.md"
    src.rename(dest)
    return True, str(dest)


def title_similarity(a: str, b: str) -> float:
    """计算两个标题的相似度（0.0 ~ 1.0）。

    使用简单的词集合 Jaccard 相似度，适配中英文混合标题。
    """
    import unicodedata
    # 归一化：小写 + 全角转半角
    a_norm = unicodedata.normalize("NFKC", a.lower().strip())
    b_norm = unicodedata.normalize("NFKC", b.lower().strip())
    if a_norm == b_norm:
        return 1.0

    # 按词切分（中文字符单独成词，英文按空格）
    def tokenize(s):
        tokens = []
        word = ""
        for ch in s:
            if "一" <= ch <= "鿿" or "぀" <= ch <= "ヿ":
                if word:
                    tokens.append(word)
                    word = ""
                tokens.append(ch)
            elif ch.isalnum():
                word += ch
            else:
                if word:
                    tokens.append(word)
                    word = ""
        if word:
            tokens.append(word)
        return set(tokens)

    a_set = tokenize(a_norm)
    b_set = tokenize(b_norm)
    if not a_set or not b_set:
        return 0.0
    return len(a_set & b_set) / len(a_set | b_set)


def restore_pattern(name: str) -> Optional[Path]:
    """从 .archive/ 恢复 Pattern。"""
    archive_dir = PATTERNS_DIR / ".archive"
    if not archive_dir.exists():
        return None
    candidates = sorted(archive_dir.glob(f"{name}.*.md"), reverse=True)
    if not candidates:
        return None
    src = candidates[0]
    fm = _parse_frontmatter(src)
    ptype = fm.get("type", "technique") if fm else "technique"
    dest = PATTERNS_DIR / f"{ptype}s" / f"{name}.md"
    if dest.exists():
        return None
    src.rename(dest)
    return dest


def _parse_frontmatter(file_path: Path) -> Dict[str, Any]:
    text = file_path.read_text(encoding="utf-8")
    m = FRONTMATTER_RE.match(text)
    if m:
        try:
            return yaml.safe_load(m.group(1)) or {}
        except Exception:
            pass
    return {}


def find_prefix_clusters(db: IndexDB) -> List[Dict[str, Any]]:
    """按 name 第一段前缀聚类，发现可合并的 Pattern 组。

    e.g. go-cross-compile, go-test-coverage, go-mod-tidy → cluster "go-*"
    """
    patterns = db.list_patterns(status="verified")
    active = [p for p in patterns if p.get("status") in ("draft", "verified")]

    clusters: Dict[str, List[Dict]] = {}
    for p in active:
        name = p["name"]
        prefix = name.split("-")[0]
        clusters.setdefault(prefix, []).append(p)

    result = []
    for prefix, members in clusters.items():
        if len(members) < 2:
            continue
        result.append({
            "prefix": prefix,
            "suggested_umbrella": f"{prefix}-tooling",
            "member_count": len(members),
            "members": [{"name": m["name"], "type": m["type"], "title": m["title"]}
                        for m in members],
        })
    return sorted(result, key=lambda c: c["member_count"], reverse=True)


def consolidate_cluster(db: IndexDB, prefix: str, umbrella_name: str) -> List[Dict]:
    """手动触发：将同一前缀的 Pattern 合并为 Umbrella Pattern。

    每个成员转为一个 ## 二级标题章节，原始文件归档。
    """
    members = [p for p in db.list_patterns() if p["name"].startswith(prefix + "-")]
    if len(members) < 2:
        return []

    body_parts = [f"# {umbrella_name}\n"]
    body_parts.append(f"> 合并自 {len(members)} 个 Pattern: "
                      + ", ".join(m["name"] for m in members) + "\n")

    src_sessions = []
    for m in members:
        fm_data = read_pattern_file(m["name"], m["type"])
        if fm_data:
            body_parts.append(f"## {m['title']}\n")
            body_parts.append(fm_data["body"])
            body_parts.append("")
            if "source_sessions" in fm_data.get("frontmatter", {}):
                try:
                    src_sessions.extend(json.loads(fm_data["frontmatter"]["source_sessions"]))
                except Exception:
                    pass

    src_sessions = sorted(set(src_sessions))
    file_path = write_pattern_file(
        name=umbrella_name, ptype="workflow", title=umbrella_name.replace("-", " ").title(),
        body="\n".join(body_parts),
        source_sessions=src_sessions,
        status="verified",
    )
    db.upsert_pattern(
        name=umbrella_name, ptype="workflow", title=umbrella_name.replace("-", " ").title(),
        file_path=str(file_path), source_sessions=json.dumps(src_sessions, ensure_ascii=False),
        status="verified",
    )

    # 归档原成员
    for m in members:
        archive_pattern(m["name"], m["type"])
        db.update_pattern_status(m["name"], "merged")

    return [dict(m) for m in members]
