"""Small JSON file used to persist headset names/groups, video metadata and settings."""

import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)


class StateStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            log.error("could not read %s (%s); starting with empty state", self.path, exc)
            return {}

    def save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.replace(tmp, self.path)
