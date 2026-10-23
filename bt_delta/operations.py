"""Pure, format-aware selection and editing primitives shared by all rule files."""

from __future__ import annotations

import re
import copy
import xml.etree.ElementTree as ET
from xml.parsers import expat
from .transforms import (
    TransformError,
    _ASSIGNMENT,
    _make_statements,
    _comment,
    _newline,
    _append,
    transform,
)
from .make_content import select_content, apply_content
from .reference import (
    copy_make_settings,
    augment_actions_from_reference,
    apply_reference_action,
)


def matches(value, names=(), patterns=()):
    return value in names or any(re.search(p, value) for p in patterns)


def make_values(text, prefix="", patterns=()):
    result = {}
    for _, _, statement, depth in _make_statements(text):
        match = _ASSIGNMENT.match(statement)
        if match and (
            (prefix and match["key"].startswith(prefix))
            or matches(match["key"], patterns=patterns)
        ):
            if (
                depth
                or match["key"] in result
                or "$(" in match["value"]
                or "${" in match["value"]
            ):
                raise TransformError(
                    "Feature is conditional, duplicated or computed: " + match["key"]
                )
            result[match["key"]] = match["value"].strip()
    return result


def select_actions(op, reference, select, literal=None, context_patterns=()):
    if op == "make.content":
        return select_content(reference, select)
    if op == "make.ensure":
        if literal is not None:
            return [{"type": "assignments", "values": literal}]
        selected = copy_make_settings(
            "",
            reference,
            select.get("assignment_names", []),
            select.get("include_basenames", []),
            select.get("assignment_patterns", []),
        )
        return (
            [
                {
                    "type": "reference_assignments",
                    "reference": selected,
                    "keys": select.get("assignment_names", []),
                    "patterns": select.get("assignment_patterns", []),
                    "includes": select.get("include_basenames", []),
                }
            ]
            if selected
            else []
        )
    if op == "make.values":
        values = (
            literal
            if literal is not None
            else make_values(
                reference,
                select.get("prefix", ""),
                select.get("assignment_patterns", []),
            )
        )
        return (
            [{"type": "assignments", "values": values, "operator": "="}]
            if values
            else []
        )
    if op == "text.ensure":
        lines = (
            literal
            if literal is not None
            else [
                line
                for line in reference.splitlines()
                if matches(
                    line.strip(), select.get("lines", []), select.get("patterns", [])
                )
            ]
        )
        return (
            [
                {
                    "type": "text_lines",
                    "lines": lines,
                    "identity_pattern": select.get("identity_pattern"),
                }
            ]
            if lines
            else []
        )
    if op == "make.packages":
        action = {
            "type": "make_packages",
            "variable": select.get("variable", "PRODUCT_PACKAGES"),
            "packages": literal if literal is not None else select.get("names", []),
            "reference_package_patterns": select.get("patterns", []),
        }
    elif op == "make.includes":
        action = {
            "type": "ensure_lines",
            "lines": literal if literal is not None else select.get("lines", []),
            "reference_line_patterns": select.get("patterns", []),
        }
    elif op == "init.ensure":
        # With no event declared, retain each selected reference command's event.
        events = (
            [select["event"]]
            if select.get("event")
            else list(
                dict.fromkeys(
                    " ".join(_comment(line)[0].split())
                    for line in reference.splitlines()
                    if re.match(r"^on\s", line)
                )
            )
        )
        actions = []
        for event in events:
            action = {
                "type": "init_commands",
                "event": event,
                "commands": (
                    literal if literal is not None else select.get("commands", [])
                ),
                "reference_command_patterns": select.get("command_patterns", []),
                "reference_comment_patterns": select.get("comment_patterns", []),
                "context_patterns": list(context_patterns),
            }
            actions.extend(
                [action]
                if literal is not None
                else augment_actions_from_reference(reference, [action])
            )
        return actions
    else:
        raise TransformError("Unsupported content operation: " + op)
    return (
        [action]
        if literal is not None
        else augment_actions_from_reference(reference, [action])
    )


def apply_actions(text, actions, report=None):
    for action in actions:
        before = text
        if action["type"] == "make_content":
            text = apply_content(text, action, report)
        elif action["type"] == "reference_assignments":
            text = copy_make_settings(
                text,
                action["reference"],
                action["keys"],
                action.get("includes", []),
                action.get("patterns", []),
                report=report,
            )
        elif action["type"] == "text_lines":
            text = ensure_text_lines(text, action, report)
        else:
            text = apply_reference_action(text, action, report=report)
        if action["type"] in ("reference_assignments", "assignments") and action.get(
            "on_value_difference"
        ) in ("review", "preserve"):
            # Restore changed existing settings, including selected includes.
            # Keep additions of missing identities and original comments/newlines.
            def setting_identity(statement):
                match = _ASSIGNMENT.match(statement)
                if match:
                    return "assignment:" + match["key"]
                include = re.match(r"^\s*-?include\s+(\S+)\s*$", statement)
                if include and include[1].rsplit("/", 1)[-1] in action.get(
                    "includes", []
                ):
                    return "include:" + include[1].rsplit("/", 1)[-1]
                return None

            existing = {}
            for first, last, statement, _ in _make_statements(before):
                key = setting_identity(statement)
                if key:
                    existing.setdefault(key, []).append((first, last, statement))
            lines = text.splitlines(keepends=True)
            for first, last, statement, _ in reversed(list(_make_statements(text))):
                key = setting_identity(statement)
                found = existing.get(key, [])
                if len(found) == 1 and statement != found[0][2]:
                    a, b, _ = found[0]
                    lines[first:last] = before.splitlines(keepends=True)[a:b]
                    if report:
                        report(
                            {
                                "message": "Existing value preserved: " + key,
                                "review": action["on_value_difference"] == "review",
                            }
                        )
            text = "".join(lines)
    return text


def ensure_text_lines(text, action, report=None):
    """Raw lines, including preprocessor directives; no Make comment parsing."""
    pattern = action.get("identity_pattern")
    lines = text.splitlines(keepends=True)

    def identity(line):
        match = re.search(pattern, line.strip()) if pattern else None
        return (match.groups() or (match.group(0),)) if match else None

    wanted_keys = [
        identity(line) for line in action["lines"] if identity(line) is not None
    ]
    if len(wanted_keys) != len(set(wanted_keys)):
        raise TransformError("Ambiguous selected text identities")
    for wanted in action["lines"]:
        if "\n" in wanted or "\r" in wanted:
            raise TransformError("text.ensure expects individual lines")
        key = identity(wanted)
        found = (
            [i for i, line in enumerate(lines) if identity(line) == key]
            if key is not None
            else []
        )
        if len(found) > 1:
            raise TransformError("Ambiguous current text identity: " + str(key))
        if any(line.strip() == wanted.strip() for line in lines):
            continue
        policy = action.get("on_value_difference", "replace")
        if found and policy != "replace":
            if report:
                report(
                    {
                        "message": "Existing text value preserved: "
                        + lines[found[0]].strip(),
                        "review": policy == "review",
                    }
                )
            continue
        if found:
            old = lines[found[0]]
            ending = (
                "\r\n" if old.endswith("\r\n") else "\n" if old.endswith("\n") else ""
            )
            lines[found[0]] = wanted + ending
        else:
            if lines and not lines[-1].endswith(("\n", "\r")):
                lines[-1] += _newline(text)
            lines.append(wanted + _newline(text))
    return "".join(lines)


def remove_content(text, select, *, make=False):
    lines = text.splitlines(keepends=True)
    changes = []
    if make:
        stack = []
        for first, last, statement, depth in _make_statements(text):
            code = statement.strip()
            if re.match(r"^(ifeq|ifneq|ifdef|ifndef)\b", code):
                stack.append(code)
                continue
            if re.match(r"^endif\b", code):
                stack.pop()
                continue
            if re.match(r"^else\b", code):
                stack[-1] += " / " + code
                continue
            if select.get("within") and not re.search(
                select["within"], " / ".join(stack)
            ):
                continue
            assignment = _ASSIGNMENT.match(statement)
            remove = bool(
                assignment
                and matches(
                    assignment["key"],
                    select.get("assignment_names", []),
                    select.get("assignment_patterns", []),
                )
            )
            if re.match(r"^\s*-?include\s", statement):
                remove |= matches(
                    statement.strip(),
                    select.get("lines", []),
                    select.get("include_patterns", []),
                )
            remove |= matches(
                statement.strip(), select.get("lines", []), select.get("patterns", [])
            )
            if assignment and assignment["key"] == select.get("variable"):
                tokens = assignment["value"].split()
                remaining = [
                    v
                    for v in tokens
                    if not matches(
                        v,
                        select.get("member_names", []),
                        select.get("member_patterns", []),
                    )
                ]
                if remaining != tokens:
                    replacement = (
                        (
                            assignment["key"]
                            + " "
                            + assignment["operator"]
                            + " "
                            + " ".join(remaining)
                            + _newline(text)
                        )
                        if remaining
                        else ""
                    )
                    changes.append((first, last, replacement))
                    continue
            if remove:
                changes.append((first, last, ""))
        # Prune only guards made empty by this edit. Pre-existing empty guards,
        # comments outside the affected spans, and unrelated branches are preserved.
        stack, guards = [], []
        for start, end, statement, _ in _make_statements(text):
            if re.match(r"^(ifeq|ifneq|ifdef|ifndef)\b", statement):
                stack.append(start)
            elif re.match(r"^endif\b", statement):
                guards.append((stack.pop(), end))
        prune = []
        for first, last in guards:
            affected = [(a, b, r) for a, b, r in changes if first < a < last]
            if not affected:
                continue
            raw = list(lines[first:last])
            for a, b, r in reversed(affected):
                raw[a - first : b - first] = [r]
            live = [
                code
                for _, _, code, _ in _make_statements("".join(raw))
                if code.strip()
                and not re.match(
                    r"^(ifeq|ifneq|ifdef|ifndef|else|endif)\b", code.strip()
                )
            ]
            if not live:
                prune.append((first, last, ""))
        prune = [
            span
            for span in prune
            if not any(other[0] < span[0] and span[1] <= other[1] for other in prune)
        ]
        changes = [
            change
            for change in changes
            if not any(first <= change[0] < last for first, last, _ in prune)
        ] + prune
        for first, last, replacement in sorted(changes, reverse=True):
            lines[first:last] = [replacement]
        result = "".join(lines)
        list(_make_statements(result))
        return result
    return "".join(
        line
        for line in lines
        if not matches(
            line.strip(), select.get("lines", []), select.get("patterns", [])
        )
    )


def blocks(text, definition):
    result = []
    for match in re.finditer(definition["pattern"], text):
        body = match.group(definition.get("body_group", 0))
        if definition.get("reject_pattern") and re.search(
            definition["reject_pattern"], body
        ):
            continue
        if any(value not in body for value in definition.get("contains", [])):
            continue
        record = match.groupdict()
        eligible = True
        for key, pattern in definition.get("captures", {}).items():
            capture = re.search(pattern, body)
            if not capture:
                eligible = False
                break
            record[key] = capture.group(1)
        if not eligible:
            continue
        for key in definition.get("split", []):
            record[key] = record[key].split()
        start = match.start()
        if definition.get("include_preceding_comment"):
            line_start = text.rfind("\n", 0, start) + 1
            prev_end = max(0, line_start - 1)
            prev_start = text.rfind("\n", 0, prev_end) + 1
            if text[prev_start:prev_end].strip().startswith("#"):
                start = prev_start
        record.update(
            text=text[start : match.end()].strip(), start=start, end=match.end()
        )
        result.append(record)
    return result


def xml_nodes(text):
    """ElementTree nodes plus exact UTF-8 spans; preserve all unrelated XML bytes."""
    root = ET.fromstring(text)
    raw = text.encode("utf-8")
    parser = expat.ParserCreate()
    spans = []
    stack = []

    def start(name, attrs):
        at = parser.CurrentByteIndex
        # Find > outside quotes.
        quote = None
        stop = at
        while stop < len(raw):
            c = raw[stop]
            if quote:
                if c == quote:
                    quote = None
            elif c in (34, 39):
                quote = c
            elif c == 62:
                break
            stop += 1
        item = [at, None, stop + 1, raw[at:stop].rstrip().endswith(b"/")]
        spans.append(item)
        stack.append(item)

    def end(name):
        item = stack.pop()
        at = parser.CurrentByteIndex
        item[1] = item[2] if item[3] else raw.index(b">", at) + 1

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.Parse(raw, True)
    nodes = {
        id(node): (
            len(raw[: span[0]].decode("utf-8")),
            len(raw[: span[1]].decode("utf-8")),
            len(raw[: span[2]].decode("utf-8")),
            span[3],
        )
        for node, span in zip(root.iter(), spans)
    }
    return root, nodes


def xml_field(node, path):
    if path == ".":
        return [(node.text or "").strip()]
    if path.startswith("@"):
        return [node.get(path[1:])]
    return [(e.text or "").strip() for e in node.findall(path)]


def xml_selected(root, select):
    nodes = root.findall(select.get("path", ".//*"))
    field = select.get("name_path")
    if field:
        nodes = [
            n
            for n in nodes
            if any(
                matches(v or "", select.get("names", []), select.get("patterns", []))
                for v in xml_field(n, field)
            )
        ]
    return nodes


def version_at_least(actual, minimum):
    try:
        a = tuple(int(p) for p in actual.split("."))
        b = tuple(int(p) for p in minimum.split("."))
        n = max(len(a), len(b))
        return a + (0,) * (n - len(a)) >= b + (0,) * (n - len(b))
    except (ValueError, AttributeError):
        return False


def compare_xml(reference, current, fields):
    differences = []
    for field in fields:
        path = field["path"]
        op = field.get("compare", "equal")
        if field.get("tuple"):

            def tuples(element):
                result = set()
                for node in element.findall(path):
                    from itertools import product

                    result.update(
                        product(*(xml_field(node, p) or [None] for p in field["tuple"]))
                    )
                return result

            wanted, actual = tuples(reference), tuples(current)
        else:
            wanted, actual = xml_field(reference, path), xml_field(current, path)
        if op == "equal":
            ok = wanted == actual
        elif op == "subset":
            ok = set(wanted).issubset(actual)
        elif op == "minimum_version":
            ok = all(any(version_at_least(a, b) for a in actual) for b in wanted)
        else:
            raise TransformError("Unknown XML comparator: " + op)
        if not ok:
            differences.append(
                f"{path}: "
                + ("reference minimums=" if op == "minimum_version" else "reference=")
                + f"{wanted}, current={actual}"
            )
    return differences


def xml_operation(reference, current, step, *, details=None):
    details = [] if details is None else details
    source_root, source_spans = xml_nodes(reference)
    selected = xml_selected(source_root, step.get("select", {}))
    # Literal XML may be a complete document containing the selected elements.
    current_root, current_spans = xml_nodes(current)
    identity = step.get("identity", [])

    def key(node):
        return (
            tuple(tuple(xml_field(node, p)) for p in identity)
            if identity
            else (node.tag,)
        )

    source_keys = [key(n) for n in selected]
    if len(source_keys) != len(set(source_keys)):
        raise TransformError("Ambiguous XML source identities")
    targets = xml_selected(
        current_root,
        (
            dict(step.get("select", {}), names=[], patterns=[".*"])
            if step.get("select", {}).get("name_path")
            else step.get("select", {})
        ),
    )
    edits = []
    notes = []
    missing = []
    for node in selected:
        found = [n for n in targets if key(n) == key(node)]
        if len(found) > 1:
            raise TransformError("Ambiguous XML target identity: " + str(key(node)))
        a, b, _, _ = source_spans[id(node)]
        snippet = reference[a:b]
        if not found:
            if step["op"] == "xml.ensure":
                missing.append(snippet)
            else:
                notes.append("Absent in current XML: " + str(key(node)))
            details.append(
                (
                    "ADD: "
                    if step["op"] == "xml.ensure"
                    else "REVIEW: Absent in current XML: "
                )
                + str(key(node))
            )
            continue
        differences = compare_xml(node, found[0], step.get("fields", []))
        if not step.get("fields") and xml_canonical(node) != xml_canonical(found[0]):
            differences = ["element differs from required content"]
        if differences:
            policy = step.get("policy", {}).get("on_value_difference", "review")
            if step["op"] == "xml.ensure" and policy == "replace":
                a, b, _, _ = current_spans[id(found[0])]
                edits.append((a, b, snippet))
                status = "CHANGE"
            elif step["op"] == "xml.ensure" and policy == "preserve":
                status = "PRESERVED"
            else:
                status = "REVIEW"
                notes.append(str(key(node)) + ": " + "; ".join(differences))
            details.append(
                status + ": " + str(key(node)) + ": " + "; ".join(differences)
            )
        else:
            details.append(
                "PASS: "
                + str(key(node))
                + "; format="
                + str(node.get("format", ""))
                + "; selected fields match."
            )
    if missing:
        parent = step.get("parent", ".")
        parents = [current_root] if parent == "." else current_root.findall(parent)
        if len(parents) != 1:
            raise TransformError("Ambiguous XML insertion parent")
        node = parents[0]
        a, b, opening, self_closed = current_spans[id(node)]
        nl = _newline(current)
        snippets = nl.join(
            "    " + x.replace("\r\n", "\n").replace("\n", nl) for x in missing
        )
        if self_closed:
            prefix = current[a:opening]
            edits.append(
                (a, b, prefix[:-2] + ">" + nl + snippets + nl + "</" + node.tag + ">")
            )
        else:
            closing = current.rfind("</", opening, b)
            edits.append((closing, closing, snippets + nl))
    for a, b, replacement in sorted(edits, reverse=True):
        current = current[:a] + replacement + current[b:]
    ET.fromstring(current)
    # Preview is a valid document containing only selected requirements.
    preview = xml_preview(source_root, selected) if selected else ""
    return current, preview, notes


def xml_preview(root, selected):
    """Keep selected subtrees and their ancestor paths in a valid document."""
    selected_ids = {id(node) for node in selected}

    def prune(node):
        if id(node) in selected_ids:
            result = copy.deepcopy(node)
            result.tail = None
            return result
        children = [
            child for original in node if (child := prune(original)) is not None
        ]
        if not children and node is not root:
            return None
        result = ET.Element(node.tag, node.attrib)
        result.extend(children)
        return result

    return ET.tostring(prune(root), encoding="unicode") + "\n"


def xml_canonical(node):
    return (
        node.tag,
        tuple(sorted(node.attrib.items())),
        (node.text or "").strip(),
        tuple(xml_canonical(c) for c in node),
    )
