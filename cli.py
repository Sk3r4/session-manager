"""Session Manager CLI — 命令行入口。

用法:
    python cli.py list                        # 列出全部会话
    python cli.py list --project Phage        # 按项目过滤
    python cli.py list --status 进行中        # 按状态过滤
    python cli.py search "bug fix"            # 全文搜索
    python cli.py status                      # 统计概览
    python cli.py scan                        # 触发扫描全部
    python cli.py health                      # 健康检查
"""

import argparse
import json
import sys
import urllib.request
import urllib.error

DEFAULT_BASE = "http://127.0.0.1:7821"


def api(path, method="GET", body=None, base=DEFAULT_BASE):
    """调用 Session Manager API。"""
    url = f"{base}{path}"
    data = json.dumps(body).encode("utf-8") if body else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read())
            print(f"Error ({e.code}): {err.get('error', e.reason)}", file=sys.stderr)
        except Exception:
            print(f"Error ({e.code}): {e.reason}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"无法连接到 Session Manager 服务 ({base})", file=sys.stderr)
        print("请先启动服务: python -m uvicorn main:app --host 127.0.0.1 --port 7821", file=sys.stderr)
        sys.exit(1)


def cmd_list(args):
    """列出会话。"""
    params = []
    if args.provider:
        params.append(f"provider={args.provider}")
    if args.status:
        params.append(f"status={args.status}")
    if args.project:
        params.append(f"project_dir={args.project}")
    query = "?" + "&".join(params) if params else ""
    sessions = api(f"/api/sessions{query}")
    if not sessions:
        print("(无会话)")
        return
    print(f"{'状态':<8} {'Provider':<12} {'标题':<40} {'项目':<30}")
    print("-" * 90)
    for s in sessions:
        status = s.get("status", "-")
        provider = s.get("provider_id", "-")
        title = (s.get("title") or s.get("session_id", "-"))[:38]
        project = (s.get("project_dir") or "-")[:28]
        print(f"{status:<8} {provider:<12} {title:<40} {project:<30}")
    print(f"\n共 {len(sessions)} 条")


def cmd_search(args):
    """全文搜索。"""
    results = api(f"/api/search?q={args.query}&limit={args.limit or 20}")
    if not results:
        print("(无结果)")
        return
    for r in results:
        rtype = r.get("result_type", "session")
        name = r.get("display_name", "-")[:60]
        print(f"[{rtype}] {name}")


def cmd_scan(args):
    """触发扫描。"""
    print("正在扫描...")
    result = api("/api/scan-all", method="POST")
    for r in result:
        print(f"  {r.get('provider_id')}: 找到 {r.get('found', 0)} 个会话")


def cmd_stats(args):
    """统计概览。"""
    summary = api("/api/stats")
    if not summary:
        print("(无数据)")
        return
    for p in summary:
        print(f"{p['provider_id']}: {p['total']} 个会话, {p['labeled']} 已标注, {p['pinned']} 置顶")

    health = api("/api/health")
    print(f"\n服务状态: {health['status']}")
    print(f"运行时间: {health['uptime_seconds']}s")


def cmd_health(args):
    """健康检查。"""
    health = api("/api/health")
    print(json.dumps(health, ensure_ascii=False, indent=2))


def cmd_launch(args):
    """恢复会话（复制命令到剪贴板或启动终端）。"""
    result = api(f"/api/sessions/{args.session_id}/copy-command")
    print(result["command"])


def main():
    parser = argparse.ArgumentParser(description="Session Manager CLI")
    sub = parser.add_subparsers(dest="command")

    p_list = sub.add_parser("list", help="列出会话")
    p_list.add_argument("--provider", help="按 provider 过滤")
    p_list.add_argument("--status", help="按状态过滤")
    p_list.add_argument("--project", help="按项目目录过滤")

    p_search = sub.add_parser("search", help="全文搜索")
    p_search.add_argument("query", help="搜索关键词")
    p_search.add_argument("--limit", type=int, default=20)

    sub.add_parser("scan", help="触发扫描全部 provider")
    sub.add_parser("stats", help="统计概览")
    sub.add_parser("health", help="健康检查")

    p_launch = sub.add_parser("launch", help="获取会话恢复命令")
    p_launch.add_argument("session_id", help="会话 ID（格式: provider::id）")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return

    {"list": cmd_list, "search": cmd_search, "scan": cmd_scan,
     "stats": cmd_stats, "health": cmd_health, "launch": cmd_launch,
    }[args.command](args)


if __name__ == "__main__":
    main()
