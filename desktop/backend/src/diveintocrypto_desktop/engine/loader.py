"""Load the canonical engine configuration (indicator thresholds, weights, consensus)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from diveintocrypto_desktop.resources import read_resource_text

_RESOURCE_NAME = "engine/config/default.yaml"
_CONFIG_PATH = Path(__file__).parent / "config" / "default.yaml"


@lru_cache(maxsize=1)
def load_config() -> dict[str, Any]:
    """Return the full engine config dict.

    The same dict is consumed by both ``SignalService(config)`` (which reads
    ``indicator_thresholds[name]`` per indicator) and ``ConsensusEngine(config)``
    (which reads ``indicator_weights`` / ``consensus`` / ``no_trade``).

    Read via :func:`resources.read_resource_text` so frozen builds resolve
    the same packaged file; no domain default is changed here.
    """
    return yaml.safe_load(read_resource_text(_RESOURCE_NAME))
