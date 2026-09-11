"""Run configuration, target profiles and credential loading."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

TARGETS_DIR_DEFAULT = "/app/targets"


def targets_dir() -> Path:
    """Resolved at call time so tests (and operators) can point it elsewhere."""
    return Path(os.environ.get("TARGETS_DIR", TARGETS_DIR_DEFAULT))


@dataclass
class Target:
    name: str
    url: str
    login_url: str | None = None
    success_url_patterns: list[str] = field(default_factory=list)
    success_selectors: list[str] = field(default_factory=list)
    handoff_url_patterns: list[str] = field(default_factory=list)
    handoff_selectors: list[str] = field(default_factory=list)
    login_selectors: dict[str, str] = field(default_factory=dict)
    credentials: dict[str, str] = field(default_factory=dict)
    notes: str = ""
    # eager = a handoff rule immediately parks the session for a human
    # auto  = the agent tries solver + its own interaction first, handoff is the fallback
    # never = no human at all, the agent must clear everything itself
    handoff_mode: str = "auto"
    # URL the agent should land on after a successful auth (post-login target).
    after_login_url: str | None = None

    def describe(self) -> str:
        lines = [f"target: {self.name}", f"start url: {self.url}"]
        if self.login_url:
            lines.append(f"login url: {self.login_url}")
        if self.after_login_url:
            lines.append(f"expected post-login url: {self.after_login_url}")
        for key, sel in self.login_selectors.items():
            lines.append(f"selector[{key}]: {sel}")
        if self.credentials:
            creds = ", ".join(f"{k}={v}" for k, v in self.credentials.items())
            lines.append(f"credentials: {creds}")
        if self.notes:
            lines.append(f"notes: {self.notes}")
        lines.append(f"human handoff: {self.handoff_mode}")
        return "\n".join(lines)


def _expand(value: str) -> str:
    """Expand ${ENV_VAR} references so targets never hardcode secrets."""
    return os.path.expandvars(value)


def load_target(name: str) -> Target:
    path = Path(name)
    if not path.exists():
        path = targets_dir() / f"{name}.yaml"
    if not path.exists():
        raise SystemExit(f"target not found: {name} (looked in {targets_dir()})")

    raw = yaml.safe_load(path.read_text()) or {}
    creds: dict[str, str] = {}
    for k, v in (raw.get("credentials") or {}).items():
        if isinstance(v, str):
            creds[k] = _expand(v)
        else:
            creds[k] = v

    return Target(
        name=raw.get("name", path.stem),
        url=_expand(raw["url"]),
        login_url=_expand(raw["login_url"]) if raw.get("login_url") else None,
        success_url_patterns=list(raw.get("success_url_patterns") or []),
        success_selectors=list(raw.get("success_selectors") or []),
        handoff_url_patterns=list(raw.get("handoff_url_patterns") or []),
        handoff_selectors=list(raw.get("handoff_selectors") or []),
        login_selectors=dict(raw.get("login_selectors") or {}),
        credentials=creds,
        notes=raw.get("notes", ""),
        handoff_mode=str(raw.get("handoff_mode", "auto")).lower(),
        after_login_url=_expand(raw["after_login_url"]) if raw.get("after_login_url") else None,
    )


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def env_int(name: str, default: int) -> int:
    try:
        return int(env(name) or default)
    except ValueError:
        return default
