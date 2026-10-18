"""一键安装的注册环节:把已安装的 session-manager-mcp 写入本机检测到的
AI 客户端 MCP 配置(ZCode / Claude Code / Codex)。console script: session-manager-install。

设计约束:
- 零第三方依赖(不 import mcp,任何 Python 可跑);
- 只在配置文件**已存在**(即客户端已安装)时写入,不为未安装的客户端创建配置;
- 写前备份(.bak),幂等(重复运行结果一致),保留原有全部内容。
"""
import json
import shutil
import sys
from pathlib import Path


def _home() -> Path:
    return Path.home()


def find_mcp_command() -> str:
    """定位 session-manager-mcp 可执行文件:PATH 优先,回退 uv 工具目录。"""
    exe = shutil.which("session-manager-mcp")
    if exe:
        return exe
    suffix = ".exe" if sys.platform == "win32" else ""
    for p in [_home() / ".local" / "bin" / f"session-manager-mcp{suffix}"]:
        if p.exists():
            return str(p)
    return "session-manager-mcp"  # 裸命令兜底(依赖客户端环境的 PATH)


def _backup(path: Path) -> None:
    bak = path.with_suffix(path.suffix + ".bak")
    if path.exists() and not bak.exists():
        bak.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")


def _write_json(path: Path, mutate) -> bool:
    """读-改-写 JSON 配置,保留原有内容;返回是否有变更。"""
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"  [跳过] {path.name} 解析失败: {e}")
        return False
    before = json.dumps(cfg, ensure_ascii=False, sort_keys=True)
    mutate(cfg)
    if json.dumps(cfg, ensure_ascii=False, sort_keys=True) == before:
        return False
    _backup(path)
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


def register_zcode(cmd: str, home: Path = None) -> bool:
    """ZCode: ~/.zcode/cli/config.json → mcp.servers.sessions"""
    home = home or _home()
    cfg = home / ".zcode" / "cli" / "config.json"
    if not cfg.exists():
        return False
    changed = _write_json(
        cfg,
        lambda c: c.setdefault("mcp", {}).setdefault("servers", {}).__setitem__(
            "sessions", {"command": cmd, "args": []}
        ),
    )
    print(f"  [ZCode] {'已注册' if changed else '已存在,无需修改'} → {cfg}")
    return True


def register_claude_code(cmd: str, home: Path = None) -> bool:
    """Claude Code: ~/.claude.json → mcpServers.sessions(用户级)"""
    home = home or _home()
    cfg = home / ".claude.json"
    if not cfg.exists():
        return False
    changed = _write_json(
        cfg,
        lambda c: c.setdefault("mcpServers", {}).__setitem__(
            "sessions", {"command": cmd, "args": []}
        ),
    )
    print(f"  [Claude Code] {'已注册' if changed else '已存在,无需修改'} → {cfg}")
    return True


def register_codex(cmd: str, home: Path = None) -> bool:
    """Codex: ~/.codex/config.toml → [mcp_servers.sessions](文本追加,表不存在时)"""
    home = home or _home()
    cfg = home / ".codex" / "config.toml"
    if not cfg.exists():
        return False
    text = cfg.read_text(encoding="utf-8")
    if "mcp_servers.sessions" in text:
        print(f"  [Codex] 已存在,无需修改 → {cfg}")
        return True
    _backup(cfg)
    section = f'\n[mcp_servers.sessions]\ncommand = "{cmd}"\nargs = []\n'
    cfg.write_text(text.rstrip("\n") + "\n" + section, encoding="utf-8")
    print(f"  [Codex] 已注册 → {cfg}")
    return True


def main() -> int:
    cmd = find_mcp_command()
    print(f"session-manager v2 MCP 注册向导")
    print(f"命令定位: {cmd}\n")

    any_client = register_zcode(cmd) or False
    any_client = register_claude_code(cmd) or any_client
    any_client = register_codex(cmd) or any_client

    if not any_client:
        print("  未检测到任何 AI 客户端配置(ZCode/Claude Code/Codex)。")
        print("  安装客户端后重跑 session-manager-install,或手动在 mcpServers 配置:")
        print(f'    "sessions": {{"command": "{cmd}", "args": []}}')
        return 1

    print("\n完成。重启 AI 客户端会话后,工具列表出现 mcp__sessions__* 即为成功。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
