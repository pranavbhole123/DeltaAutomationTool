"""Small data-expression vocabulary for rule inputs; never evaluates code."""

from __future__ import annotations

import re
from pathlib import Path
from .spec import get_value, set_value, SpecError


class Unresolved(ValueError):
    pass


def present(value):
    return value is not None and value != "" and value != []


class Values:
    def __init__(self, spec, config, *, read=None, log=None):
        self.spec, self.config, self.read = spec, config, read
        self.log = log or (lambda message: None)
        self.cache, self.active, self.logs = {}, set(), {}

    def variable(self, name):
        if name in self.cache:
            return self.cache[name]
        if name in self.active:
            raise SpecError("Dependency cycle at " + name)
        self.active.add(name)
        try:
            item = self.spec["derive"][name]
            value = (
                self.eval(item["value"])
                if self.condition(item.get("when", True))
                else None
            )
            if item.get("required") and not present(value):
                raise Unresolved("No usable value for " + name)
            self.cache[name] = value
            if item.get("export") and present(value):
                set_value(self.config, item["export"], value)
            return value
        finally:
            self.active.remove(name)

    def template(self, text):
        def replace(match):
            value = self.variable(match[1])
            if not present(value):
                raise Unresolved("Set " + match[1] + " to resolve " + text)
            return str(value)

        return re.sub(r"\$\{([\w.]+)\}", replace, text)

    def condition(self, expression):
        value = self.eval(expression)
        if value is None:
            raise Unresolved(
                "Condition depends on an unresolved value: " + repr(expression)
            )
        return bool(value)

    def eval(self, expression):
        if not isinstance(expression, dict):
            return expression
        if "literal" in expression:
            return expression["literal"]
        if "var" in expression:
            return self.variable(expression["var"])
        if "input" in expression:
            return get_value(self.config, expression["input"])
        if "template" in expression:
            return self.template(expression["template"])
        if "coalesce" in expression:
            for item in expression["coalesce"]:
                try:
                    value = self.eval(item)
                except Unresolved:
                    continue
                if present(value):
                    return value
            return None
        if "if" in expression:
            return self.eval(
                expression["then"]
                if self.condition(expression["if"])
                else expression.get("else")
            )
        for op in ("lower", "strip", "last_digits"):
            if op in expression:
                value = self.eval(expression[op])
                if not present(value):
                    return None
                if op == "lower":
                    return str(value).lower()
                if op == "strip":
                    return str(value).strip().strip("\"'")
                digits = re.findall(r"\d+", str(value))
                return digits[-1] if digits else None
        if "list" in expression:
            return [v for item in expression["list"] if present(v := self.eval(item))]
        if "lookup" in expression:
            data = expression["lookup"]
            return self.spec["lookups"][data["table"]].get(str(self.eval(data["key"])))
        for op in ("eq", "ne"):
            if op in expression:
                a, b = [self.eval(x) for x in expression[op]]
                if a is None or b is None:
                    return None
                return (a == b) if op == "eq" else (a != b)
        if "present" in expression:
            return present(self.eval(expression["present"]))
        if "not" in expression:
            value = self.eval(expression["not"])
            return None if value is None else not value
        for op in ("all", "any"):
            if op in expression:
                unknown = False
                for item in expression[op]:
                    try:
                        value = self.eval(item)
                    except Unresolved:
                        value = None
                    if value is None:
                        unknown = True
                    elif op == "all" and not value:
                        return False
                    elif op == "any" and value:
                        return True
                return None if unknown else op == "all"
        if "matches" in expression:
            data = expression["matches"]
            value = self.eval(data["value"])
            return (
                None if value is None else bool(re.search(data["pattern"], str(value)))
            )
        if "file_text" in expression:
            if self.read is None:
                raise Unresolved("File content unavailable during input validation")
            return self.read(expression["file_text"])
        if "assignment" in expression:
            data = expression["assignment"]
            text = self.eval({"file_text": data["file"]})
            if text is None:
                return None
            from .transforms import _make_statements, _ASSIGNMENT

            found = []
            for _, _, statement, depth in _make_statements(text):
                match = _ASSIGNMENT.match(statement)
                if match and match["key"] == data["key"]:
                    if depth or "$(" in match["value"] or "${" in match["value"]:
                        raise ValueError(
                            "Conditional/computed input assignment: " + data["key"]
                        )
                    found.append(match["value"].strip().strip("\"'"))
            if len(found) > 1:
                raise ValueError("Duplicate input assignment: " + data["key"])
            return found[0] if found else None
        if "regex" in expression:
            data = expression["regex"]
            text = self.eval(data["text"])
            if text is None:
                return None
            found = [
                m.group(data.get("group", 1))
                for m in re.finditer(data["pattern"], str(text))
            ]
            excluded = [self.eval(v) for v in data.get("exclude", [])]
            found = list(dict.fromkeys(v for v in found if v not in excluded))
            if len(found) > 1 and not data.get("many"):
                raise Unresolved("Ambiguous extracted value: " + ", ".join(found))
            return found if data.get("many") else (found[0] if found else None)
        if "log" in expression:
            data = expression["log"]
            path = get_value(self.config, data["input"])
            if not path:
                return None
            if path not in self.logs:
                try:
                    self.logs[path] = (
                        Path(path)
                        .expanduser()
                        .read_text(encoding="utf-8-sig", errors="replace")
                    )
                except OSError as exc:
                    self.log(
                        f"Optional current build log could not be read: {exc}. Using existing inputs/inference."
                    )
                    self.logs[path] = ""
            values = set()
            for match in re.finditer(data["pattern"], self.logs[path]):
                value = match.group(data.get("group", 1)).strip()
                if re.fullmatch(data.get("value_pattern", r"[A-Za-z0-9_.-]+"), value):
                    values.add(value.lower() if data.get("lowercase", True) else value)
                else:
                    self.log(
                        f"Current build log: ignored invalid {data['label']} value {value!r}."
                    )
            if len(values) == 1:
                value = values.pop()
                self.log(
                    f"Current build log: {data['label']}={value} → {data.get('destination', data['label'])}."
                )
                return value
            self.log(
                "Current build log: "
                + (
                    "conflicting " + data["label"] + " values; "
                    if values
                    else data["label"] + " not found; "
                )
                + "using existing input/inference."
            )
            return None
        if "path_root" in expression:
            data = expression["path_root"]
            path, component = self.eval(data["path"]), self.eval(data["component"])
            if not path or not component:
                return None
            parts = path.rstrip("/")[2:].split("/")
            matches = [
                i
                for i, part in enumerate(parts[1:], 1)
                if part.lower() == component.lower()
            ]
            if len(matches) != 1:
                raise Unresolved(
                    f"Root path must contain one {component} directory: {path}"
                )
            return "//" + "/".join(parts[: matches[0] + 1])
        raise SpecError("Unsupported expression: " + repr(expression))
