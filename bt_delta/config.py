"""Input parsing without branch-name or OS-version assumptions."""

from __future__ import annotations

import json
import re
from pathlib import Path


class ConfigError(ValueError):
    pass


def role_settings(config, role):
    """Log identifiers supplement current settings only; reference stays independent."""
    values = dict(config)
    if role == "current":
        values.update(config.get("current", {}).get("build_log_inputs", {}))
        if values.get("model") != config.get("model"):
            values["common_device"] = values["model"] + "_common"
    return values


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
        match = re.match(
            r"^(System\s+Template|Vendor\s+Template|CSC\s+path)\s*[:-]\s*(.+?)\s*$",
            line,
            re.I,
        )
        if not match:
            raise ConfigError(f"Unrecognized input line: {line}")
        label, value = match.groups()
        key = {
            "system template": "system_template",
            "vendor template": "vendor_template",
            "csc path": "csc_path",
        }[re.sub(r"\s+", " ", label.lower())]
        target = result.get(section)
        if target is None:
            raise ConfigError("Add C OS: or Reference details before template lines")
        if key in target:
            raise ConfigError(f"Duplicate {label}")
        target[key] = value
    return result


def validate(config: dict, spec=None) -> dict:
    from .spec import load_spec, get_value, set_value, input_order
    from .expressions import Values, present, Unresolved

    spec = spec if spec is not None else load_spec(config=config)
    config = json.loads(json.dumps(config))
    config.pop("cp_template", None)
    config.pop("firmware_sha256", None)
    # Derived values are rebuilt, never trusted from a saved input file.
    for variable in spec.get("derive", {}).values():
        if variable.get("export") and variable["export"] not in spec["inputs"]:
            path = variable["export"].split(".")
            parent = get_value(config, ".".join(path[:-1])) if len(path) > 1 else config
            if isinstance(parent, dict):
                parent.pop(path[-1], None)
            for size in range(len(path) - 1, 0, -1):
                parent = (
                    get_value(config, ".".join(path[: size - 1]))
                    if size > 1
                    else config
                )
                if isinstance(parent, dict) and parent.get(path[size - 1]) == {}:
                    parent.pop(path[size - 1])
    for name, field in spec["inputs"].items():
        if get_value(config, name, ...) is ... and "default" in field:
            set_value(config, name, field["default"])
    for name, field in spec["inputs"].items():
        if (
            field["type"] == "boolean"
            and get_value(config, name) is not None
            and not isinstance(get_value(config, name), bool)
        ):
            raise ConfigError(f"{name} must be true or false")
        if field["type"] == "boolean" and get_value(config, name, ...) is None:
            raise ConfigError(f"{name} must be true or false")
    values = Values(spec, config)
    for name in input_order(spec):
        field = spec["inputs"][name]
        value = get_value(config, name)
        if not present(value) and "derive" in field:
            try:
                value = values.eval(field["derive"])
            except Unresolved:
                value = None
            if present(value):
                set_value(config, name, value)
    for name, field in spec["inputs"].items():
        value = get_value(config, name)
        required = field.get("required", False)
        if "required_when" in field:
            required = values.condition(field["required_when"])
        if required and not present(value):
            raise ConfigError(f"Missing {name}")
        if value is None and not required and field["type"] != "boolean":
            continue
        kind = field["type"]
        if kind == "boolean" and not isinstance(value, bool):
            raise ConfigError(f"{name} must be true or false")
        if kind in ("string", "path", "depot", "choice") and not isinstance(value, str):
            raise ConfigError(f"{name} must be a string or file path")
        if kind == "list" and (
            not isinstance(value, list) or any(not isinstance(v, str) for v in value)
        ):
            raise ConfigError(f"{name} must be a list of strings")
        if kind == "choice" and value and value not in field.get("choices", []):
            raise ConfigError(f"Invalid {name}")
        if field.get("pattern") and value:
            entries = value if kind == "list" else [value]
            if any(not re.fullmatch(field["pattern"], item) for item in entries):
                raise ConfigError(f"Invalid {name}")
        if kind == "depot" and value:
            from .perforce import validate_depot_path

            value = value.rstrip("/")
            try:
                validate_depot_path(value)
            except ValueError as exc:
                raise ConfigError(f"Invalid {name}: {exc}") from exc
            set_value(config, name, value)
    for field in ("port", "user", "client"):
        if not config.get("perforce", {}).get(field):
            raise ConfigError(f"Missing perforce.{field}")
    config.setdefault("paths", {})
    if not isinstance(config["paths"], dict):
        raise ConfigError("paths must be an object")
    for key in config["paths"]:
        if key not in spec["files"]:
            raise ConfigError("Unknown file ID in paths: " + key)
    return config


def load(path: str | Path, spec=None) -> dict:
    path = Path(path).resolve()
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    if config.get("rules_file") and not Path(config["rules_file"]).is_absolute():
        config["rules_file"] = str((path.parent / config["rules_file"]).resolve())
    return validate(config, spec)
