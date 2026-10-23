"""Versioned rules and companion comparison settings. No domain defaults live here."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SPEC = ROOT / "checklist" / "slsi.json"
FORMATS = {"text", "make", "init", "xml", "json", "binary"}
OPERATIONS = {
    "file.copy_if_missing",
    "tree.copy_missing",
    "file.exists",
    "path.assert",
    "make.content",
    "make.ensure",
    "make.packages",
    "make.includes",
    "make.values",
    "make.remove",
    "text.ensure",
    "text.remove",
    "block.sync",
    "block.remove",
    "init.ensure",
    "xml.verify",
    "xml.ensure",
    "json.merge_missing",
    "assert",
    "manual",
}
EXPRS = {
    "var",
    "input",
    "literal",
    "coalesce",
    "template",
    "lower",
    "strip",
    "last_digits",
    "regex",
    "assignment",
    "lookup",
    "if",
    "eq",
    "ne",
    "all",
    "any",
    "not",
    "present",
    "matches",
    "list",
    "log",
    "path_root",
    "file_text",
}


class SpecError(ValueError):
    pass


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SpecError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def get_value(values, path, default=None):
    current = values
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return current


def set_value(values, path, value):
    parts = path.split(".")
    for part in parts[:-1]:
        values = values.setdefault(part, {})
    values[parts[-1]] = value


def _object(value, fields, required, where):
    if not isinstance(value, dict):
        raise SpecError(f"{where}: expected an object")
    unknown, missing = value.keys() - set(fields), set(required) - value.keys()
    if unknown or missing:
        raise SpecError(
            f"{where}: unknown fields {sorted(unknown)}; missing fields {sorted(missing)}"
        )


def _regex(value, where):
    try:
        re.compile(value)
    except (re.error, TypeError) as exc:
        raise SpecError(f"{where}: invalid regex: {exc}") from exc


def input_order(spec):
    """Topological order for input-only defaults, independent of JSON key order."""
    ordered, active = [], set()

    def refs(value):
        if isinstance(value, dict):
            if "input" in value:
                yield value["input"]
            for key, child in value.items():
                if key != "literal":
                    yield from refs(child)
        elif isinstance(value, list):
            for child in value:
                yield from refs(child)

    def visit(name):
        if name in ordered:
            return
        if name in active:
            raise SpecError("Input dependency cycle at " + name)
        active.add(name)
        for dependency in refs(spec["inputs"][name].get("derive")):
            if dependency not in spec["inputs"]:
                raise SpecError("Unknown input: " + str(dependency))
            visit(dependency)
        active.remove(name)
        ordered.append(name)

    for name in spec["inputs"]:
        visit(name)
    return ordered


def _read_json(path, label):
    try:
        return json.loads(
            path.read_text(encoding="utf-8-sig"), object_pairs_hook=unique_object
        )
    except (OSError, ValueError) as exc:
        raise SpecError(f"Cannot load {label} file {path}: {exc}") from exc


def _resolve_comparison(spec, base_dir):
    """Expand the companion into the existing runtime shape, preserving plan hashes.

    The source files stay separate. Legacy inline packs and callers holding a
    resolved catalogue keep working without changing planner/comparison behavior.
    """
    if "comparison_file" not in spec:
        return
    if "comparison" in spec or any(
        "comparison" in file for file in spec["files"].values()
    ):
        raise SpecError(
            "comparison_file cannot be combined with inline comparison settings; "
            "move all comparison policies into the companion file"
        )
    path = Path(spec.pop("comparison_file")).expanduser()
    if not path.is_absolute():
        if base_dir is None:
            raise SpecError(
                "Relative comparison_file requires the rules directory; "
                "use load_spec(path) or validate_spec(..., base_dir=...)"
            )
        path = Path(base_dir) / path
    path = path.resolve()
    profile = _read_json(path, "comparison")
    from .schema import validate_document

    try:
        validate_document(profile, "comparison-v1.schema.json")
        if profile["rules_id"] != spec["id"]:
            raise SpecError(
                f"rules_id {profile['rules_id']!r} does not match rules pack {spec['id']!r}"
            )
        unknown = profile["files"].keys() - spec["files"].keys()
        if unknown:
            raise SpecError("Unknown comparison file IDs: " + ", ".join(sorted(unknown)))
    except SpecError as exc:
        raise SpecError(f"Invalid comparison file {path}: {exc}") from exc
    for name, policy in profile["files"].items():
        spec["files"][name]["comparison"] = policy
    if "exclude" in profile:
        spec["comparison"] = {"exclude": profile["exclude"]}


def validate_spec(spec, *, base_dir=None):
    spec = copy.deepcopy(spec)
    if not isinstance(spec, dict) or spec.get("schema_version") != 2:
        raise SpecError(
            "Rule schema_version must be 2. See docs/rules.md for migration; legacy implicit defaults are not used."
        )
    from .schema import validate_document

    validate_document(spec)
    _resolve_comparison(spec, base_dir)
    _object(
        spec,
        {
            "schema_version",
            "id",
            "title",
            "source",
            "inputs",
            "sources",
            "lookups",
            "derive",
            "files",
            "rules",
            "report",
            "preconditions",
            "comparison",
        },
        {"schema_version", "id", "title", "inputs", "sources", "files", "rules"},
        "rules file",
    )
    if spec["schema_version"] != 2:
        raise SpecError(
            "Rule schema_version must be 2. Migrate legacy catalogues using docs/rules.md; implicit Python defaults are no longer supported."
        )
    if not all(
        isinstance(spec[key], str) and spec[key].strip() for key in ("id", "title")
    ):
        raise SpecError("id and title must be nonblank strings")
    for key in ("inputs", "sources", "files", "derive", "lookups"):
        spec.setdefault(key, {})
        if not isinstance(spec[key], dict):
            raise SpecError(f"{key}: expected an object")
    for name, field in spec["inputs"].items():
        _object(
            field,
            {
                "type",
                "label",
                "default",
                "required",
                "required_when",
                "pattern",
                "choices",
                "visible",
                "group",
                "derive",
                "help",
            },
            {"type"},
            f"inputs.{name}",
        )
        if field["type"] not in (
            "string",
            "boolean",
            "path",
            "depot",
            "list",
            "choice",
        ):
            raise SpecError(f"inputs.{name}: unsupported type")
        if field["type"] == "choice" and not field.get("choices"):
            raise SpecError(f"inputs.{name}: choices must be nonempty")
        if "pattern" in field:
            _regex(field["pattern"], name)
        if "default" in field:
            value = field["default"]
            kind = field["type"]
            valid = (
                isinstance(value, bool)
                if kind == "boolean"
                else (
                    (isinstance(value, list) and all(isinstance(v, str) for v in value))
                    if kind == "list"
                    else isinstance(value, str)
                )
            )
            if not valid or (
                kind == "choice" and value and value not in field["choices"]
            ):
                raise SpecError(f"inputs.{name}: invalid default")
        if any(other.startswith(name + ".") for other in spec["inputs"]):
            raise SpecError("Overlapping input paths: " + name)
    for name, source in spec["sources"].items():
        _object(
            source,
            {"kind", "input", "root", "role", "when"},
            {"kind"},
            f"sources.{name}",
        )
        if source["kind"] not in ("template", "depot_root"):
            raise SpecError(f"sources.{name}: unsupported source kind")
        if source["kind"] == "template" and source.get("input") not in spec["inputs"]:
            raise SpecError(f"sources.{name}: unknown template input")
        if source["kind"] == "depot_root" and "root" not in source:
            raise SpecError(f"sources.{name}: missing root")
    for name, variable in spec["derive"].items():
        _object(
            variable,
            {"value", "export", "required", "when"},
            {"value"},
            f"derive.{name}",
        )
    for name, file in spec["files"].items():
        _object(
            file,
            {"source", "kind", "format", "routes", "filter", "comparison", "when"},
            {"source", "kind", "format", "routes"},
            f"files.{name}",
        )
        if (
            file["source"] not in spec["sources"]
            or file["kind"] not in ("file", "tree")
            or file["format"] not in FORMATS
        ):
            raise SpecError(f"files.{name}: invalid source, kind, or format")
        if not isinstance(file["routes"], list) or not file["routes"]:
            raise SpecError(f"files.{name}: routes must be a nonempty list")
        for route in file["routes"]:
            _object(
                route,
                {
                    "anchor_regex",
                    "relative",
                    "client_relative",
                    "mapped_regex",
                    "append",
                    "capture",
                    "allowed",
                    "depot",
                },
                (),
                f"files.{name}.routes",
            )
            kinds = sum(
                k in route
                for k in ("anchor_regex", "client_relative", "mapped_regex", "depot")
            )
            if kinds != 1:
                raise SpecError(f"files.{name}: each route needs one discovery method")
            for key in ("anchor_regex", "mapped_regex"):
                if key in route:
                    _regex(route[key], name)
        if "comparison" in file:
            _object(
                file["comparison"],
                {"include", "latest_scope", "list_variables", "xml_identity", "mode"},
                (),
                name + ".comparison",
            )
            if file["comparison"].get("latest_scope", "context") not in (
                "context",
                "file",
            ):
                raise SpecError(f"{name}: latest_scope must be context or file")
            if file["comparison"].get("mode") == "requirements" and file[
                "format"
            ] not in ("xml", "json"):
                raise SpecError(
                    f"{name}: requirements comparison mode is supported for XML and JSON"
                )
    if not isinstance(spec["rules"], list):
        raise SpecError("rules: expected a list")
    ids = set()
    for rule in spec["rules"]:
        _object(
            rule,
            {
                "id",
                "title",
                "provenance",
                "source",
                "when",
                "disabled_message",
                "depends_on",
                "steps",
            },
            {"id", "title", "steps"},
            "rule",
        )
        if rule["id"] in ids:
            raise SpecError("Duplicate rule ID: " + rule["id"])
        ids.add(rule["id"])
        if not isinstance(rule["steps"], list) or not rule["steps"]:
            raise SpecError(f"{rule['id']}: steps must be nonempty")
        for step in rule["steps"]:
            _object(
                step,
                {
                    "op",
                    "from",
                    "to",
                    "select",
                    "literal",
                    "policy",
                    "when",
                    "condition",
                    "message",
                    "status",
                    "pairing",
                    "replacements",
                    "fields",
                    "identity",
                    "parent",
                    "root",
                    "block",
                    "choose",
                    "related",
                    "inventory",
                    "assert_path",
                    "operator",
                },
                {"op"},
                rule["id"] + ".step",
            )
            if step["op"] not in OPERATIONS:
                raise SpecError(f"{rule['id']}: unsupported operation {step['op']}")
            op = step["op"]
            if "literal" in step:
                literal = step["literal"]
                if op in ("make.ensure", "make.values") and (
                    not isinstance(literal, dict)
                    or any(
                        not isinstance(v, (str, int, float, bool))
                        for v in literal.values()
                    )
                ):
                    raise SpecError(
                        rule["id"]
                        + ": literal must map assignment names to scalar values"
                    )
                if op in (
                    "text.ensure",
                    "make.packages",
                    "make.includes",
                    "init.ensure",
                ) and (
                    not isinstance(literal, list)
                    or any(not isinstance(v, str) for v in literal)
                ):
                    raise SpecError(rule["id"] + ": literal must be a list of strings")
                if op.startswith("xml."):
                    import xml.etree.ElementTree as ET

                    try:
                        ET.fromstring(literal)
                    except (ET.ParseError, TypeError) as exc:
                        raise SpecError(
                            rule["id"] + ": literal must be a complete XML document"
                        ) from exc
            select = step.get("from", {}).get("select", step.get("select", {}))
            if "select" in step and "select" in step.get("from", {}):
                raise SpecError(
                    rule["id"] + ": declare select once, on the step or its source"
                )
            if op == "init.ensure" and "literal" in step and not select.get("event"):
                raise SpecError(rule["id"] + ": literal init commands need an event")
            if op.startswith("xml."):
                import xml.etree.ElementTree as ET

                if not select:
                    raise SpecError(rule["id"] + ": XML selection is required")
                if (select.get("names") or select.get("patterns")) and not select.get(
                    "name_path"
                ):
                    raise SpecError(rule["id"] + ": XML name selection needs name_path")
                if not step.get("identity"):
                    raise SpecError(rule["id"] + ": XML identity must be nonempty")
                for path in [select.get("path", "."), step.get("parent", ".")] + [
                    f["path"] for f in step.get("fields", [])
                ]:
                    try:
                        if not path.startswith("@"):
                            ET.Element("root").findall(path)
                    except (SyntaxError, TypeError, KeyError) as exc:
                        raise SpecError(
                            rule["id"] + ": invalid supported XML path: " + path
                        ) from exc
            if (
                op == "make.remove"
                and (select.get("member_names") or select.get("member_patterns"))
                and not select.get("variable")
            ):
                raise SpecError(rule["id"] + ": member removal needs variable")
            for name in ("reference", "current"):
                if (
                    name in step.get("related", {})
                    and step["related"][name] not in spec["files"]
                ):
                    raise SpecError(
                        rule["id"] + ": unknown related file " + step["related"][name]
                    )
            if op in (
                "make.content",
                "make.ensure",
                "make.values",
                "make.packages",
                "make.includes",
                "init.ensure",
                "text.ensure",
                "xml.ensure",
                "xml.verify",
            ):
                if ("from" in step) == ("literal" in step):
                    raise SpecError(
                        rule["id"] + ": choose exactly one of from or literal"
                    )
                if "literal" not in step and not step.get(
                    "select", step.get("from", {}).get("select")
                ):
                    raise SpecError(rule["id"] + ": an explicit selector is required")
            if op == "make.content" and ("from" not in step or "literal" in step):
                raise SpecError(rule["id"] + ": make.content requires a reference source")
            if op == "tree.copy_missing" and step.get("pairing", {}).get(
                "method"
            ) not in ("tree_relative", "view_relative"):
                raise SpecError(rule["id"] + ": tree copy needs an explicit pairing")
            if op == "block.sync" and not all(
                step["block"].get(k) for k in ("identity", "compare")
            ):
                raise SpecError(
                    rule["id"] + ": block.sync needs identity and compare fields"
                )
            for side in ("from", "to"):
                if side in step:
                    _object(
                        step[side],
                        {"file", "select"},
                        {"file"},
                        rule["id"] + "." + side,
                    )
                    if step[side]["file"] not in spec["files"]:
                        raise SpecError(
                            f"{rule['id']}: unknown file {step[side]['file']}"
                        )
            for side in ("from", "to"):
                if side not in step:
                    continue
                f = spec["files"][step[side]["file"]]
                if (
                    side == "to"
                    and op not in ("file.exists", "path.assert", "xml.verify")
                    and spec["sources"][f["source"]]["role"] != "current"
                ):
                    raise SpecError(
                        rule["id"] + ": write destinations must use a current source"
                    )
                expected = op.split(".")[0]
                if (
                    expected in FORMATS
                    and f["format"] != expected
                    and not (expected == "text" and f["format"] == "make")
                ):
                    raise SpecError(
                        rule["id"]
                        + ": operation format differs from "
                        + step[side]["file"]
                    )
                if (
                    op in ("file.exists", "path.assert", "file.copy_if_missing")
                    and f["kind"] != "file"
                ):
                    raise SpecError(rule["id"] + ": operation requires a single file")
                if op == "tree.copy_missing" and f["kind"] != "tree":
                    raise SpecError(rule["id"] + ": operation requires trees")
            if (
                op == "json.merge_missing"
                and spec["files"][step["from"]["file"]]["kind"]
                != spec["files"][step["to"]["file"]]["kind"]
            ):
                raise SpecError(
                    rule["id"] + ": JSON merge requires matching file/tree kinds"
                )
            if "policy" in step:
                # Never accept a policy that this operation would silently ignore.
                policies = {
                    "file.copy_if_missing": {"on_source_missing"},
                    "tree.copy_missing": {"on_source_missing"},
                    "file.exists": {"on_missing"},
                    "path.assert": {"on_missing"},
                    "block.sync": {"on_source_missing", "on_empty_selection"},
                    "block.remove": {"on_target_missing"},
                    "make.remove": {"on_target_missing"},
                    "text.remove": {"on_target_missing"},
                    "json.merge_missing": {"on_source_missing", "on_target_missing"},
                }
                content_ops = {
                    "make.content",
                    "make.ensure",
                    "make.values",
                    "make.packages",
                    "make.includes",
                    "text.ensure",
                    "init.ensure",
                    "xml.ensure",
                    "xml.verify",
                }
                allowed = set(policies.get(op, ()))
                if (
                    op == "json.merge_missing"
                    and spec["files"][step["from"]["file"]]["kind"] == "tree"
                ):
                    allowed = {"report_current_only"}
                if op in content_ops:
                    allowed.update(
                        ("on_source_missing", "on_target_missing", "on_empty_selection")
                    )
                if op in ("make.content", "make.ensure", "make.values", "text.ensure", "xml.ensure"):
                    allowed.add("on_value_difference")
                if op == "init.ensure":
                    allowed.add("context_patterns")
                if op == "make.values":
                    allowed.add("report_current_only")
                _object(step["policy"], allowed, (), rule["id"] + ".policy")
                for key in (
                    "on_source_missing",
                    "on_target_missing",
                    "on_empty_selection",
                    "on_missing",
                ):
                    if key in step["policy"] and step["policy"][key] not in (
                        "review",
                        "skip",
                        "block",
                        "create",
                    ):
                        raise SpecError(f'{rule["id"]}: invalid {key}')
                if step["policy"].get("on_value_difference", "replace") not in (
                    "replace",
                    "review",
                    "preserve",
                ):
                    raise SpecError(f'{rule["id"]}: invalid on_value_difference')
                if "on_value_difference" in step["policy"] and op not in (
                    "make.content",
                    "make.ensure",
                    "make.values",
                    "text.ensure",
                    "xml.ensure",
                ):
                    raise SpecError(
                        rule["id"] + ": on_value_difference does not apply to " + op
                    )
                if "context_patterns" in step["policy"] and op != "init.ensure":
                    raise SpecError(
                        rule["id"] + ": context_patterns applies only to init.ensure"
                    )
    # Validate expression references and dependency cycles before network access.
    dependencies = {key: set() for key in spec["derive"]}
    file_dependencies = {key: set() for key in spec["files"]}

    def templates(value, deps=None):
        if isinstance(value, dict):
            for child in value.values():
                templates(child, deps)
        elif isinstance(value, list):
            for child in value:
                templates(child, deps)
        elif isinstance(value, str):
            for name in re.findall(r"\$\{([\w.]+)\}", value):
                if name not in spec["derive"]:
                    raise SpecError("Unknown template variable: " + name)
                if deps is not None:
                    deps.add("v:" + name)

    def visit(value, deps=None):
        if isinstance(value, list):
            for child in value:
                visit(child, deps)
        elif isinstance(value, dict):
            if set(value) == {"var"}:
                name = value["var"]
                if name not in spec["derive"]:
                    raise SpecError("Unknown derived variable: " + str(name))
                if deps is not None:
                    deps.add("v:" + name)
            if set(value) == {"input"} and value["input"] not in spec["inputs"]:
                raise SpecError("Unknown input: " + str(value["input"]))
            if set(value) in ({"file_text"}, {"assignment"}):
                name = value.get("file_text", value.get("assignment", {}).get("file"))
                if name not in spec["files"]:
                    raise SpecError("Unknown expression file: " + str(name))
                if deps is not None:
                    deps.add("f:" + name)
            if "lookup" in value and value["lookup"]["table"] not in spec["lookups"]:
                raise SpecError("Unknown lookup: " + value["lookup"]["table"])
            if "log" in value and value["log"]["input"] not in spec["inputs"]:
                raise SpecError("Unknown log input: " + value["log"]["input"])
            for name in ("regex", "log"):
                if set(value) == {name}:
                    data = value[name]
                    _regex(data["pattern"], name)
                    compiled = re.compile(data["pattern"])
                    group = data.get("group", 1)
                    if (
                        isinstance(group, int) and not 0 <= group <= compiled.groups
                    ) or (isinstance(group, str) and group not in compiled.groupindex):
                        raise SpecError("Unknown regex capture group: " + str(group))
            for key, child in value.items():
                if key == "literal":
                    # Literal JSON may itself contain keys such as input or var.
                    continue
                if (
                    key.endswith("pattern") or key.endswith("_regex") or key == "within"
                ) and isinstance(child, str):
                    _regex(child, key)
                if key.endswith("patterns") and isinstance(child, list):
                    for pattern in child:
                        _regex(pattern, key)
                if key == "captures":
                    for pattern in child.values():
                        _regex(pattern, key)
                        if re.compile(pattern).groups < 1:
                            raise SpecError("Block captures require capture group 1")
                visit(child, deps)
        elif isinstance(value, str):
            templates(value, deps)

    for key, value in spec.items():
        if key != "lookups":
            visit(value)
    for rule in spec["rules"]:
        for step in rule["steps"]:
            templates(step.get("literal"))
    for name, field in spec["inputs"].items():
        deps = set()
        visit(field.get("derive"), deps)
        visit(field.get("required_when"), deps)
        if deps:
            raise SpecError(
                "Input derivation/required_when must use inputs, not derived variables or depot files: "
                + name
            )
    input_order(spec)
    for key, item in spec["derive"].items():
        visit(item, dependencies[key])
    for key, item in spec["files"].items():
        visit(item, file_dependencies[key])
        visit(spec["sources"][item["source"]], file_dependencies[key])
    graph = {"v:" + k: v for k, v in dependencies.items()} | {
        "f:" + k: v for k, v in file_dependencies.items()
    }
    graph.update(
        {
            "r:" + r["id"]: {"r:" + x for x in r.get("depends_on", [])}
            for r in spec["rules"]
        }
    )
    visited, active = set(), set()

    def walk(key):
        if key not in graph:
            raise SpecError("Unknown dependency: " + key)
        if key in active:
            raise SpecError("Dependency cycle at " + key)
        if key in visited:
            return
        active.add(key)
        for child in graph[key]:
            walk(child)
        active.remove(key)
        visited.add(key)

    for key in graph:
        walk(key)

    def needs_file(keys):
        return any(k.startswith("f:") or needs_file(graph[k]) for k in keys)

    for name, source in spec["sources"].items():
        deps = set()
        visit(source, deps)
        if needs_file(deps):
            raise SpecError(
                "Source configuration must resolve before file reads: " + name
            )
    return spec


def load_spec(path=None, *, config=None):
    path = (
        Path(path or (config or {}).get("rules_file") or DEFAULT_SPEC)
        .expanduser()
        .resolve()
    )
    spec = _read_json(path, "rules")
    return validate_spec(spec, base_dir=path.parent)


def rule_source(rule):
    if "source" in rule:
        return rule["source"]
    p = rule.get("provenance", {})
    return (
        p.get("sheet", "") + ("!" + p["range"] if p.get("range") else "") or rule["id"]
    )
