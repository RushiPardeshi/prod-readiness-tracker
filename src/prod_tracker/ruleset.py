"""Load and validate checks.yaml into the Ruleset model."""

from __future__ import annotations

from pathlib import Path

import yaml

from .models import Ruleset


def load_ruleset(path: Path) -> Ruleset:
    with open(path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    return Ruleset.model_validate(raw)
