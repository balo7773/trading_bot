#!/usr/bin/env python3
"""
config/__init__.py

Loads environment variables from .env and exposes an immutable configuration dataclass.
Enforces fail-safe defaults for Demo trading.
"""
from dataclasses import dataclass
import os
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class AppConfig:
    api_key: str
    identifier: str
    password: str
    environment: str
    is_demo: bool
    db_path: Path


def find_env_file(explicit_path: Optional[Path] = None) -> Optional[Path]:
    """Locates the .env file across standard project paths."""
    if explicit_path and explicit_path.exists():
        return explicit_path

    candidates = [
        Path.cwd() / ".env",
        Path(__file__).resolve().parent.parent / ".env",
        Path(__file__).resolve().parent / ".env",
    ]
    for p in candidates:
        if p.exists() and p.is_file():
            return p
    return None


def load_config(env_path: Optional[Path] = None) -> AppConfig:
    """
    Parses .env file and constructs AppConfig.
    Fails immediately if required credentials are missing.
    """
    target_env = find_env_file(env_path)
    env_vars: dict[str, str] = {}

    if target_env is not None:
        # utf-8-sig automatically strips any hidden UTF-8 BOM markers
        with open(target_env, "r", encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    env_vars[key.strip()] = val.strip().strip("'\"")

    api_key = env_vars.get("CAPITAL_API_KEY") or os.getenv("CAPITAL_API_KEY")
    identifier = env_vars.get("CAPITAL_IDENTIFIER") or os.getenv("CAPITAL_IDENTIFIER")
    password = env_vars.get("CAPITAL_PASSWORD") or os.getenv("CAPITAL_PASSWORD")
    raw_env = (env_vars.get("TRADING_ENVIRONMENT") or os.getenv("TRADING_ENVIRONMENT", "DEMO")).strip().upper()

    if not api_key:
        raise ValueError(
            f"Missing required configuration: CAPITAL_API_KEY. "
            f"(Resolved .env path: {target_env})"
        )
    if not identifier:
        raise ValueError("Missing required configuration: CAPITAL_IDENTIFIER")
    if not password:
        raise ValueError("Missing required configuration: CAPITAL_PASSWORD")

    # Hard safety rule: Must explicitly be "LIVE" to disable demo mode
    is_demo = raw_env != "LIVE"
    project_root = Path(__file__).resolve().parent.parent

    return AppConfig(
        api_key=api_key,
        identifier=identifier,
        password=password,
        environment="LIVE" if not is_demo else "DEMO",
        is_demo=is_demo,
        db_path=project_root / "trading_bot.db",
    )


__all__ = ["AppConfig", "load_config"]