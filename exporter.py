import io
from datetime import datetime
from typing import List, Dict, Any, Optional

from fpdf import FPDF


def _fmt_ts(ts: Optional[float]) -> str:
    if not ts:
        return ""
    try:
        if ts > 1e12:
            dt = datetime.fromtimestamp(ts / 1000.0)
        else:
            dt = datetime.fromtimestamp(ts)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(ts)


def _safe_filename(name: str) -> str:
    """生成安全的文件名。"""
    invalid = '<>:"/\\|?*'
    for ch in invalid:
        name = name.replace(ch, '_')
    return name[:100]


def export_markdown(session: dict, messages: List[Dict[str, Any]]) -> str:
    """导出为 Markdown 字符串。"""
    lines = []
    display_name = session.get("title") or session.get("session_id", "Unknown")
    lines.append(f"# Session: {display_name}")
    lines.append("")
    lines.append(f"- **Provider**: {session.get('provider_id', '-')}")
    lines.append(f"- **Session ID**: `{session.get('session_id', '-')}`")
    if session.get("project_dir"):
        lines.append(f"- **项目目录**: `{session['project_dir']}`")
    lines.append(f"- **创建时间**: {_fmt_ts(session.get('created_at'))}")
    lines.append(f"- **最后活跃**: {_fmt_ts(session.get('last_active_at'))}")
    lines.append(f"- **状态**: {session.get('status', '-')}")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 对话记录")
    lines.append("")

    if not messages:
        lines.append("> ⚠️ **此会话的消息已被 Kimi CLI compacting 清除，原始对话内容已不可恢复。**")
        lines.append("> 归档时间仅保留了会话元数据（标题、时间、项目目录等）。")
        lines.append("")

    for m in messages:
        role = m.get("role", "unknown")
        content = m.get("content", "") or ""
        ts = _fmt_ts(m.get("ts"))

        if role == "user":
            icon = "👤"
        elif role == "assistant":
            icon = "🤖"
        elif role == "tool":
            icon = "🔧"
        else:
            icon = "📝"

        time_str = f" | {ts}" if ts else ""
        lines.append(f"### {icon} {role.capitalize()}{time_str}")
        lines.append("")
        lines.append("```")
        lines.append(content)
        lines.append("```")
        lines.append("")

    return "\n".join(lines)


def export_pdf(session: dict, messages: List[Dict[str, Any]]) -> bytes:
    """导出为 PDF 字节流。"""
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)

    # Windows 中文字体
    font_path = r"C:\Windows\Fonts\msyh.ttc"
    pdf.add_font("MSYH", "", font_path, uni=True)
    pdf.add_font("MSYH", "B", font_path, uni=True)

    pdf.add_page()

    # 标题
    pdf.set_font("MSYH", "B", 16)
    display_name = session.get("title") or session.get("session_id", "Unknown")
    pdf.cell(0, 10, f"Session: {display_name}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    # 元信息
    pdf.set_font("MSYH", "", 10)
    pdf.cell(0, 6, f"Provider: {session.get('provider_id', '-')}", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 6, f"Session ID: {session.get('session_id', '-')}", new_x="LMARGIN", new_y="NEXT")
    if session.get("project_dir"):
        pdf.cell(0, 6, f"项目目录: {session['project_dir']}", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 6, f"创建时间: {_fmt_ts(session.get('created_at'))}", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 6, f"最后活跃: {_fmt_ts(session.get('last_active_at'))}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(5)

    # 对话记录
    for m in messages:
        role = m.get("role", "unknown")
        content = m.get("content", "") or ""
        ts = _fmt_ts(m.get("ts"))

        if role == "user":
            color = (30, 64, 175)
            label = "User"
        elif role == "assistant":
            color = (22, 163, 74)
            label = "Assistant"
        elif role == "tool":
            color = (161, 98, 7)
            label = "Tool"
        else:
            color = (100, 100, 100)
            label = role.capitalize()

        # Role 标签
        pdf.set_font("MSYH", "B", 11)
        pdf.set_text_color(*color)
        time_str = f" ({ts})" if ts else ""
        pdf.cell(0, 8, f"{label}{time_str}", new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(0, 0, 0)

        # 内容
        pdf.set_font("MSYH", "", 10)
        effective_width = pdf.w - pdf.l_margin - pdf.r_margin
        for line in content.split("\n"):
            line = line.strip()
            if not line:
                pdf.ln(2)
            else:
                pdf.multi_cell(effective_width, 5, line)
        pdf.ln(3)

    buf = io.BytesIO()
    pdf.output(buf)
    return buf.getvalue()
