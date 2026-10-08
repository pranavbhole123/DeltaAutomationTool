"""Input parsing without branch-name or OS-version assumptions."""
from __future__ import annotations

import json
import re
from pathlib import Path


class ConfigError(ValueError):
    pass


def parse_details(text: str) -> dict:
    """Accept current/reference details; ignore legacy CP lines."""
    result = {"current": {}, "reference": {}}
    section = None
    for original in text.splitlines():
        line = original.strip()
        if not line:
            continue
        if re.match(r"^CP\s+template\s*[:-]", line, re.I):
            continue
        if re.match(r"^(?:c\s*os|current(?:\s+os)?)\s*:", line, re.I):
            section = "current"
            continue
        if re.match(r"^reference\b", line, re.I):
            section = "reference"
            continue
        match = re.match(r"^(System\s+Template|Vendor\s+Template|CSC\s+path)\s*[:-]\s*(.+?)\s*$", line, re.I)
        if not match:
            raise ConfigError(f"Unrecognized input line: {line}")
        label, value = match.groups()
        key = {"system template": "system_template", "vendor template": "vendor_template",
               "csc path": "csc_path"}[re.sub(r"\s+", " ", label.lower())]
        target = result.get(section)
        if target is None:
            raise ConfigError("Add C OS: or Reference details before template lines")
        if key in target:
            raise ConfigError(f"Duplicate {label}")
        target[key] = value
    return result


def validate(config: dict) -> dict:
    config = json.loads(json.dumps(config))
    config.pop("cp_template", None)
    config.setdefault("check_csc_features", False)
    if not isinstance(config["check_csc_features"], bool):
        raise ConfigError("check_csc_features must be true or false")
    for role in ("current", "reference"):
        fields = ("system_template", "vendor_template", "csc_path") if config["check_csc_features"] else ("system_template", "vendor_template")
        for field in fields:
            if not str(config.get(role, {}).get(field, "")).strip():
                raise ConfigError(f"Missing {role}.{field}")
        csc = config[role].get("csc_path", "").rstrip("/")
        if csc and (not csc.startswith("//") or any(s in csc for s in ("..", "*", "#", "@", "\\"))):
            raise ConfigError(f"Invalid {role}.csc_path")
        config[role]["csc_path"] = csc
    for field in ("port", "user", "client"):
        if not config.get("perforce", {}).get(field):
            raise ConfigError(f"Missing perforce.{field}")
    if not config.get("model"):
        match = re.search(r"_D\d+_([A-Z0-9]+)-", config["current"]["system_template"], re.I)
        if not match:
            raise ConfigError("Enter model explicitly; it cannot be inferred from the template")
        config["model"] = match.group(1).lower()
    for key in ("model", "chipset", "ap", "common_device", "hcf_variant"):
        if config.get(key) and not re.fullmatch(r"[A-Za-z0-9_.-]+", config[key]):
            raise ConfigError(f"Invalid {key}")
    config.setdefault("common_device", config["model"] + "_common")
    config.setdefault("jdm", False)
    if not isinstance(config["jdm"], bool):
        raise ConfigError("jdm must be true or false")
    config.setdefault("products", [])
    if not isinstance(config["products"], list) or any(not re.fullmatch(r"[A-Za-z0-9_.-]+", p) for p in config["products"]):
        raise ConfigError("products must be a list of TARGET_PRODUCT names")
    if config.get("firmware_sha256") and not re.fullmatch(r"[0-9a-fA-F]{64}", config["firmware_sha256"]):
        raise ConfigError("firmware_sha256 must be a 64-digit SHA-256")
    config.setdefault("paths", {})
    discovery = config.setdefault("discovery", {})
    if not isinstance(discovery, dict):
        raise ConfigError("discovery must be an object")
    for key, default, maximum in (("timeout_seconds", 30, 300), ("max_queries", 24, 100), ("max_records", 2000, 100000)):
        discovery.setdefault(key, default)
        value = discovery[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= maximum:
            raise ConfigError(f"discovery.{key} must be greater than zero and at most {maximum}")
        if key != "timeout_seconds" and not isinstance(value, int):
            raise ConfigError(f"discovery.{key} must be an integer")
    return config


def load(path: str | Path) -> dict:
    return validate(json.loads(Path(path).read_text(encoding="utf-8-sig")))
