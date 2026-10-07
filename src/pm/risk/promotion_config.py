"""PM-19: the blocker-to-risk promotion threshold, read from configuration.

The number of days is never a literal in the code that applies it. It comes from a committed
file (config/risk_promotion.yaml), overridden by the environment variable
PM_RISK_PROMOTION_THRESHOLD_DAYS when that is set. The file's location can be moved with
PM_RISK_PROMOTION_CONFIG.

  file missing, or threshold_days null/absent   -> None: no age requirement (PM-16 behaviour)
  value present but not a whole number >= 0      -> PromotionConfigError, never a default
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_PATH = Path(__file__).resolve().parents[3] / "config" / "risk_promotion.yaml"
ENV_THRESHOLD = "PM_RISK_PROMOTION_THRESHOLD_DAYS"
ENV_PATH = "PM_RISK_PROMOTION_CONFIG"
_WHOLE_NUMBER = re.compile(r"[0-9]+")


class PromotionConfigError(ValueError):
    """The promotion configuration is present but unusable."""


@dataclass(frozen=True)
class PromotionPolicy:
    threshold_days: int  # a blocker is promoted when it has been blocked for MORE days than this
    source: str  # where the number came from, for the person reading the output


def _validated(value: object, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PromotionConfigError(f"{where}: threshold_days must be a whole number of days, 0 or more, not {value!r}")
    return value


def load_promotion_policy(*, path: str | Path | None = None, env: Mapping[str, str] | None = None) -> PromotionPolicy | None:
    env = os.environ if env is None else env
    file_path = Path(path) if path is not None else Path(env.get(ENV_PATH) or DEFAULT_PATH)

    from_file: int | None = None
    if file_path.exists():
        try:
            data = yaml.safe_load(file_path.read_text())
        except yaml.YAMLError as exc:
            raise PromotionConfigError(f"{file_path}: not valid YAML ({exc.__class__.__name__})") from exc
        if data is not None and not isinstance(data, dict):
            raise PromotionConfigError(f"{file_path}: expected a mapping with a threshold_days key")
        raw = (data or {}).get("threshold_days")
        if raw is not None:
            from_file = _validated(raw, str(file_path))

    override = (env.get(ENV_THRESHOLD) or "").strip()
    if override:
        if not _WHOLE_NUMBER.fullmatch(override):
            raise PromotionConfigError(f"{ENV_THRESHOLD}: must be a whole number of days, not {override!r}")
        return PromotionPolicy(int(override), f"environment variable {ENV_THRESHOLD}")
    if from_file is None:
        return None
    return PromotionPolicy(from_file, str(file_path))
