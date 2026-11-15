"""Subelar Built-in Collector Plugin: Anthropic Claude Code (`claude`)."""

from __future__ import annotations

import tempfile
from typing import Any, Dict, List, Optional

from subelar_agent.providers.claude import ClaudeProvider


def capture_account(env: Dict[str, str], vault: Any, alias: Optional[str] = None) -> Any:
    provider = ClaudeProvider()
    return provider.capture_current_account(env=env, vault=vault, alias=alias)


def collect_quota(
    env: Dict[str, str],
    accounts: Optional[List[Dict[str, Any]]] = None,
    probe_dir: Optional[str] = None,
    vault: Any = None,
) -> Any:
    provider = ClaudeProvider()
    if probe_dir:
        return provider.collect(probe_dir=probe_dir, env=env, vault=vault)
    with tempfile.TemporaryDirectory(prefix="subelar-claude-") as tmp_dir:
        return provider.collect(probe_dir=tmp_dir, env=env, vault=vault)
