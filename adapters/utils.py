"""适配器共享工具函数：时间戳解析、内容提取等。"""

import json
import logging
from datetime import datetime
from typing import Any, Optional

logger = logging.getLogger("adapters")


def parse_ts(value: Any) -> Optional[float]:
    """统一将各种时间格式转为秒级时间戳。

    支持: Unix 秒/毫秒 (int/float), ISO 8601 字符串, 纯数字字符串。
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        if value > 1e12:
            return value / 1000.0
        return float(value)
    if isinstance(value, str):
        # 尝试 ISO 格式
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt.timestamp()
        except Exception:
            pass
        # 尝试纯数字字符串
        try:
            v = float(value)
            return parse_ts(v)
        except Exception:
            pass
    return None


def extract_text_from_content(content: Any) -> str:
    """从 AI CLI 的 content 字段提取纯文本。

    支持字符串和列表格式（Claude/Kimi/Codex），
    识别思考块、工具调用、工具结果等类型。
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict):
                t = c.get("type", "")
                if t == "text":
                    parts.append(c.get("text", ""))
                elif t in ("thinking", "think"):
                    key = t if t == "thinking" else "think"
                    parts.append(f"[思考] {c.get(key, '')}")
                elif t == "tool_use":
                    name = c.get("name", "")
                    inp = json.dumps(c.get("input", {}), ensure_ascii=False)
                    parts.append(f"[工具调用: {name}] {inp}")
                elif t == "tool_result":
                    result = c.get("content", "")
                    if isinstance(result, str):
                        parts.append(f"[工具结果] {result}")
                    else:
                        parts.append(f"[工具结果] {json.dumps(result, ensure_ascii=False)}")
                else:
                    # 兜底：取第一个非 type/encrypted 的字符串值
                    for k, v in c.items():
                        if isinstance(v, str) and v.strip() and k not in ("type", "encrypted"):
                            parts.append(v)
                            break
            elif isinstance(c, str):
                parts.append(c)
        return "\n".join(parts)
    return str(content)
