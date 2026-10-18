from adapters.base import ProviderAdapter, SessionMeta, Message
from adapters.generic import GenericFileAdapter
from adapters.claude_code import ClaudeCodeAdapter
from adapters.codex import CodexAdapter
from adapters.zcode import ZCodeAdapter

def build_adapter(config: dict) -> ProviderAdapter:
    adapter_type = config.get("adapter_type", "generic")
    if adapter_type == "claude-code":
        return ClaudeCodeAdapter(config)
    if adapter_type == "codex":
        return CodexAdapter(config)
    if adapter_type == "zcode":
        return ZCodeAdapter(config)
    return GenericFileAdapter(config)
