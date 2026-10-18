"""扫描期 AI 自动标注:对新增会话生成一句话总结 + 建议标签(后台线程)。

配置零新增——直接复用 Claude Code 的 ~/.claude/settings.json env 块:
    ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN   密钥(缺省 = 功能整体关闭)
    ANTHROPIC_BASE_URL                          端点(Anthropic 协议兼容网关)
    ANTHROPIC_MODEL                             模型
链路:扫描发现新会话 → 转录尾部(截断)送模型 → {"summary","tags","line"} →
update_summary + set_session_tags 写回 → 追踪线自动扩线、进度注记就位;
line(AI 归线)命中已知目标目录时写 dir_links——cwd 不在目标工作区的
会话(复盘/交接/技巧调整)也归入其目录时间线。
标注不阻塞扫描请求;/api/poll 携带完成计数,驱动前端刷新。
LLM 调用失败静默跳过,绝不影响扫描主流程。
"""
import json
import re
import threading
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from adapters import build_adapter
from config import USER_HOME
from storage.index import _norm_dir

DEFAULT_MODEL = "claude-haiku-4-5-20251001"
MAX_TRANSCRIPT_CHARS = 6000  # 送模型的转录尾部上限
MIN_TEXT_CHARS = 100  # 低于此正文量的会话视为无实质内容,跳过标注(垃圾会话实测 <50 字,真实对话 >100 字)
MAX_TAGS = 3
BATCH_LIMIT = 10  # 每轮扫描最多标注的新会话数
PROMPT = (
    "你是会话归档员。下面是某个 AI 编程助手会话的末尾片段。"
    "用一句中文(40字内)概括这个会话干成了什么/进行到哪(进度注记口径,不要复述提问),"
    "并给出最多3个简短主题标签(每个2-6字,如:黑盒、周报、重构)。\n"
    "已知目标工作区列表(仅用于 line 判定,逐行一个):\n{dirs}\n"
    "line:该会话明确属于列表中某个目标工作区时(即使运行目录不同,如复盘/交接/"
    "针对该目标的技巧调整)给出那个目录,否则为 null。\n"
    '只输出 JSON,形如 {"summary": "…", "tags": ["…"], "line": "目录或null"},不要输出其他内容。\n\n'
    "<transcript>\n{tail}\n</transcript>"
)

# 进行中集合 + 待前端感知计数(poll 携带);失败也必须计数——静默失败是
# 2026-09-23「补标注看似卡死」事故的根因(全部 401 被 try/except 吞了两天无人知)
_lock = threading.Lock()
_inflight: set = set()
_done_since_poll = 0
_failed_since_poll = 0


def take_annotations_done() -> int:
    """自上次 poll 以来完成的自动标注数(前端据此刷新)。"""
    global _done_since_poll
    with _lock:
        n = _done_since_poll
        _done_since_poll = 0
        return n


def take_annotate_failures() -> int:
    """自上次 poll 以来标注失败数(LLM 调用/解析失败)。前端 toast,绝不无声死掉。"""
    global _failed_since_poll
    with _lock:
        n = _failed_since_poll
        _failed_since_poll = 0
        return n


def llm_config(settings_path: Optional[Path] = None) -> Optional[Dict[str, str]]:
    """读 Claude Code settings.json 的 env 块;无密钥返回 None(功能关闭)。"""
    p = Path(settings_path) if settings_path else USER_HOME / ".claude" / "settings.json"
    try:
        env = json.loads(p.read_text(encoding="utf-8")).get("env") or {}
    except Exception:
        return None
    key = env.get("ANTHROPIC_API_KEY") or env.get("ANTHROPIC_AUTH_TOKEN")
    if not key:
        return None
    return {
        "base_url": (env.get("ANTHROPIC_BASE_URL") or "https://api.anthropic.com").rstrip("/"),
        "key": key,
        "token_auth": not env.get("ANTHROPIC_API_KEY"),  # AUTH_TOKEN → Bearer 头
        # 模型选择:优先网关的廉价档映射。ANTHROPIC_MODEL 可能是带 [1M] 等后缀的高档位,
        # Key 未必有权限——2026-09-23 实测 401「该模型不在 Key 的可用模型范围内」,
        # 导致标注自上线起全部静默失败;router 环境的 DEFAULT_HAIKU 映射才是白名单内的名字。
        "model": (env.get("ANTHROPIC_DEFAULT_HAIKU_MODEL")
                  or env.get("ANTHROPIC_MODEL")
                  or DEFAULT_MODEL),
    }


def _call_llm(cfg: Dict[str, str], prompt: str) -> Optional[str]:
    """Anthropic Messages 协议(stdlib urllib,零新依赖)。失败返回 None。"""
    body = json.dumps({
        "model": cfg["model"],
        "max_tokens": 300,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    headers = {"content-type": "application/json", "anthropic-version": "2023-06-01"}
    if cfg["token_auth"]:
        headers["authorization"] = f"Bearer {cfg['key']}"
    else:
        headers["x-api-key"] = cfg["key"]
    req = urllib.request.Request(cfg["base_url"] + "/v1/messages", data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return "".join(b.get("text", "") for b in data.get("content", []) if isinstance(b, dict))
    except Exception:
        return None


def _parse_result(text: Optional[str]) -> Optional[Dict[str, Any]]:
    """容错解析 {"summary","tags"}:直接解析失败则正则抠 JSON 块。"""
    if not text:
        return None
    try:
        obj = json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return None
        try:
            obj = json.loads(m.group(0))
        except Exception:
            return None
    summary = str(obj.get("summary") or "").strip()
    tags = [str(t).strip() for t in (obj.get("tags") or []) if str(t).strip()][:MAX_TAGS]
    line = str(obj.get("line") or "").strip()
    if line.lower() in ("", "null", "none"):
        line = ""
    if not summary:
        return None
    return {"summary": summary[:120], "tags": tags, "line": line or None}


def annotate_session_sync(db, adapter, session_id: str, raw_session_id: str, cfg: Dict[str, str],
                          dirs: List[str]) -> bool:
    """单会话:转录尾部 → LLM → 摘要+标签(+AI 归线)写回并登记(annotated_sessions)。
    dirs = 已知目标目录(AI 归线候选);成功返回 True。"""
    try:
        msgs = adapter.load_transcript(raw_session_id)
    except Exception:
        return False
    lines = [f"[{m.role}] {m.content}" for m in msgs
             if m.content and m.kind in ("text", "thinking")]
    if not lines:
        # 空转录:登记已处理,避免重复标注
        db.mark_annotated(session_id)
        return False
    # 垃圾会话直接跳过(打招呼/报错即中断的会话):不值得一次 LLM 调用,
    # 登记已处理防止重试(2026-09-24 实测:首批 5 个标注全是「你好/nihao/空会话」)
    if sum(len(l) for l in lines) < MIN_TEXT_CHARS:
        db.mark_annotated(session_id)
        return False
    tail = "\n".join(lines)[-MAX_TRANSCRIPT_CHARS:]
    # 用 replace 而非 str.format:提示词里的示例 JSON 花括号会被 format 当替换字段
    prompt = PROMPT.replace("{dirs}", "\n".join(dirs)).replace("{tail}", tail)
    result = _parse_result(_call_llm(cfg, prompt))
    if not result:
        global _failed_since_poll
        with _lock:
            _failed_since_poll += 1
        return False
    db.update_summary(session_id, result["summary"])
    if result["tags"]:
        db.set_session_tags(session_id, result["tags"])
    # AI 归线:line 归一化后须命中候选目录(防模型幻觉路径),才写符号链接
    line = result.get("line")
    if line:
        norm = _norm_dir(line)
        if norm and norm in {_norm_dir(d) for d in dirs}:
            db.link_to_dir(line, session_id)
    db.mark_annotated(session_id)
    return True


def auto_annotate(db, adapter, records) -> None:
    """同步核心:标注给定(新增)会话,每轮最多 BATCH_LIMIT 个。"""
    cfg = llm_config()
    if not cfg or not records:
        return
    global _done_since_poll
    dirs = db.known_dirs()  # AI 归线候选(已知目标工作区,近活跃优先)
    for r in records[:BATCH_LIMIT]:
        if annotate_session_sync(db, adapter, r.id, r.session_id, cfg, dirs):
            with _lock:
                _done_since_poll += 1


def auto_annotate_async(db, adapter, records) -> None:
    """后台线程执行,不阻塞扫描请求;inflight 去重防并发重复标注。"""
    if not records or not llm_config():
        return
    todo: List[Any] = []
    with _lock:
        for r in records:
            if r.id not in _inflight:
                _inflight.add(r.id)
                todo.append(r)
    if not todo:
        return

    def _run():
        try:
            auto_annotate(db, adapter, todo)
        finally:
            with _lock:
                for r in todo:
                    _inflight.discard(r.id)

    threading.Thread(target=_run, daemon=True, name="sm-annotate").start()
