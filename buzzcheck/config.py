from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

CONFIG_DIR = Path.home() / ".buzzcheck"
CONFIG_PATH = CONFIG_DIR / "config.json"
TOKEN_PATH = CONFIG_DIR / "token.json"


class ConfigMissing(Exception):
    pass


@dataclass(frozen=True)
class Store:
    location_id: str
    chain: str
    name: str
    address: str
    resolved_at: str


def config_exists() -> bool:
    return CONFIG_PATH.exists()


def load_store(name: Optional[str] = None) -> Store:
    if not CONFIG_PATH.exists():
        raise ConfigMissing(
            "No store pinned. Run `buzzcheck setup` (once phase 2 lands) or "
            "run `buzzcheck stores --zip <zip>` and hand-write "
            f"{CONFIG_PATH}."
        )
    try:
        data = json.loads(CONFIG_PATH.read_text())
    except json.JSONDecodeError as e:
        raise ConfigMissing(f"Config at {CONFIG_PATH} is not valid JSON: {e}")
    stores = data.get("stores") or {}
    key = name or data.get("default_store") or "primary"
    if key not in stores:
        raise ConfigMissing(
            f"Store '{key}' not found in {CONFIG_PATH}. "
            "Available: " + (", ".join(stores) or "<none>")
        )
    s = stores[key]
    missing = [f for f in ("location_id",) if not s.get(f)]
    if missing:
        raise ConfigMissing(
            f"Store '{key}' in {CONFIG_PATH} is missing fields: {missing}"
        )
    return Store(
        location_id=str(s["location_id"]),
        chain=s.get("chain", "") or "",
        name=s.get("name", "") or "",
        address=s.get("address", "") or "",
        resolved_at=s.get("resolved_at", "") or "",
    )


def save_store(store: Store, name: str = "primary", *, make_default: bool = True) -> None:
    CONFIG_DIR.mkdir(mode=0o700, exist_ok=True)
    data: dict = {"default_store": name, "stores": {}}
    if CONFIG_PATH.exists():
        try:
            existing = json.loads(CONFIG_PATH.read_text())
            if isinstance(existing, dict):
                data = existing
                data.setdefault("stores", {})
                if make_default:
                    data["default_store"] = name
                else:
                    data.setdefault("default_store", name)
        except json.JSONDecodeError:
            pass
    data["stores"][name] = asdict(store)
    CONFIG_PATH.write_text(json.dumps(data, indent=2))
    os.chmod(CONFIG_PATH, 0o600)
