"""Small stdlib validator for the JSON Schema vocabulary used by our rule grammar.

The published schema is also usable by editors and standard JSON Schema tools.
No rule document is allowed to extend this vocabulary or execute Python.
"""

import json
import re
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=2)
def document_schema(name):
    # Only bundled schemas may be referenced. Never fetch a URI supplied by a pack.
    if name not in {"rules-v2.schema.json", "comparison-v1.schema.json"}:
        raise ValueError("Unknown bundled schema: " + name)
    return json.loads(
        (Path(__file__).resolve().parent.parent / "checklist" / name).read_text()
    )


def rule_schema():
    return document_schema("rules-v2.schema.json")


def validate_document(value, schema_name="rules-v2.schema.json"):
    from .spec import SpecError

    root = document_schema(schema_name)
    types = {
        "object": lambda x: isinstance(x, dict),
        "array": lambda x: isinstance(x, list),
        "string": lambda x: isinstance(x, str),
        "boolean": lambda x: isinstance(x, bool),
        "integer": lambda x: isinstance(x, int) and not isinstance(x, bool),
        "number": lambda x: isinstance(x, (int, float)) and not isinstance(x, bool),
        "null": lambda x: x is None,
    }

    def check(value, schema, path, root):
        if "$ref" in schema:
            name, fragment = schema["$ref"].split("#", 1)
            target_root = document_schema(name) if name else root
            target = target_root
            for part in fragment.lstrip("/").split("/"):
                target = target[part]
            return check(value, target, path, target_root)
        for union in ("oneOf", "anyOf"):
            if union in schema:
                options = schema[union]
                if isinstance(value, dict):
                    # A discriminator narrows errors to the relevant operation.
                    candidates = [
                        s
                        for s in options
                        if "properties" in s
                        and all(
                            value.get(k) == v["const"]
                            for k, v in s["properties"].items()
                            if "const" in v
                        )
                        and set(s.get("required", [])).issubset(value)
                        and set(value).issubset(s["properties"])
                    ]
                    if len(candidates) == 1:
                        return check(value, candidates[0], path, root)
                errors, successes = [], 0
                for option in options:
                    try:
                        check(value, option, path, root)
                        successes += 1
                    except SpecError as exc:
                        errors.append(str(exc))
                if not successes or (union == "oneOf" and successes != 1):
                    if isinstance(value, dict) and "op" in value:
                        matching = [
                            s
                            for s in options
                            if s.get("properties", {}).get("op", {}).get("const")
                            == value["op"]
                        ]
                        if len(matching) == 1:
                            return check(value, matching[0], path, root)
                    raise SpecError(
                        path
                        + ": does not match rule grammar ("
                        + "; ".join(errors[:3])
                        + ")"
                    )
                return
        if "type" in schema:
            allowed = (
                schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
            )
            if not any(types[t](value) for t in allowed):
                raise SpecError(path + ": expected " + ", ".join(allowed))
        if "const" in schema and value != schema["const"]:
            raise SpecError(path + ": expected " + repr(schema["const"]))
        if "enum" in schema and value not in schema["enum"]:
            raise SpecError(path + ": expected one of " + repr(schema["enum"]))
        if isinstance(value, dict):
            missing = set(schema.get("required", [])) - value.keys()
            if missing:
                raise SpecError(path + ": missing " + ", ".join(sorted(missing)))
            props = schema.get("properties", {})
            for key, child in value.items():
                sub = props.get(key, schema.get("additionalProperties", {}))
                if sub is False:
                    raise SpecError(path + ": unknown field " + key)
                check(child, sub, path + "." + key, root)
        if isinstance(value, list):
            if len(value) < schema.get("minItems", 0) or len(value) > schema.get(
                "maxItems", float("inf")
            ):
                raise SpecError(path + ": invalid number of entries")
            for i, child in enumerate(value):
                check(child, schema.get("items", {}), path + f"[{i}]", root)
        if isinstance(value, str):
            if len(value) < schema.get("minLength", 0):
                raise SpecError(path + ": must not be blank")
            if "pattern" in schema and not re.search(schema["pattern"], value):
                raise SpecError(path + ": invalid value")

    label = "comparison" if schema_name == "comparison-v1.schema.json" else "rules"
    check(value, root, label, root)
