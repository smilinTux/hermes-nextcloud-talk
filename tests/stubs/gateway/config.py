"""Minimal stand-in for hermes-agent's gateway/config.py, just enough surface
for the nextcloud_talk adapter tests to import and construct a
PlatformConfig. Not a copy of the real file -- the real upstream Platform
enum does not have a NEXTCLOUD_TALK member yet (this plugin registers it at
runtime via the plugin registry); this stub pre-adds it so both adapter
modules under test can do ``Platform("nextcloud_talk")`` without a real
Hermes checkout or any runtime registration dance.
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class Platform(Enum):
    LOCAL = "local"
    TELEGRAM = "telegram"
    WEBHOOK = "webhook"
    WECOM_CALLBACK = "wecom_callback"
    NEXTCLOUD_TALK = "nextcloud_talk"
    NCTALK = "nctalk"


@dataclass
class PlatformConfig:
    enabled: bool = False
    token: Optional[str] = None
    api_key: Optional[str] = None
    reply_to_mode: str = "first"
    extra: Dict[str, Any] = field(default_factory=dict)
