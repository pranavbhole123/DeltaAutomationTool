"""Conservative, pure text edits used by checklist rules.

No function reads a workspace, changes a file, or invokes Perforce. Ambiguous
source layouts must be resolved by a person before a plan can include an edit.
"""
from __future__ import annotations

import re
import shlex
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape


class TransformError(ValueError):
    """The requested edit cannot be made unambiguously."""


def _newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _one_line(value: object, label: str) -> str:
    value = str(value)
    if "\r" in value or "\n" in value or not value.strip():
        raise TransformError(f"{label} must be a nonblank single line")
    return value


def _comment(line: str) -> tuple[str, str]:
    """Split an unescaped comment outside quotes without altering either part."""
    quote = None
    escaped = False
    for i, char in enumerate(line):
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif quote:
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char == "#":
            return line[:i], line[i:]
    return line, ""


def _body(line: str) -> str:
    return line.rstrip("\r\n")


def _continued(line: str) -> bool:
    code, _ = _comment(_body(line))
    code = code.rstrip()
    return (len(code) - len(code.rstrip("\\"))) % 2 == 1


def _append(text: str, lines: list[str]) -> str:
    if not lines:
        return text
    nl = _newline(text)
    if text and not text.endswith(("\n", "\r")):
        text += nl
    return text + nl.join(lines) + nl


def _presence(report, message, *, review=False):
    if report:
        report({"message": message, "review": review})


def _ensure_lines(text: str, action: dict, *, report=None) -> str:
    present = {(_comment(line)[0].strip() or line.strip()): i + 1 for i, line in enumerate(text.splitlines())
               if line.strip()}
    missing = []
    for item in action.get("lines", []):
        line = _one_line(item, "line")
        requested = _comment(line)[0].split()
        is_include = bool(requested and requested[0] in ("include", "-include"))
        if is_include:
            matches = [(start + 1, logical.strip(), depth) for start, _, logical, depth in _make_statements(text)
                       if logical.split() and logical.split()[0] in ("include", "-include")
                       and set(requested[1:]).issubset(logical.split()[1:])]
            if matches and any(depth for _, _, depth in matches):
                raise TransformError(f"Include exists conditionally; review before changing: {line}")
            if matches:
                for number, actual, _ in matches:
                    different = actual.split()[0] != requested[0]
                    _presence(report, f"Include already present at line {number}: {actual}. No duplicate added. " +
                              (f"Reference requests {line}; include versus -include differs, so review applicability." if different else "Whitespace/comments do not create a missing include."),
                              review=different)
                continue
        comparable = ' '.join(requested) if is_include else (_comment(line)[0].strip() or line.strip())
        if comparable in present:
            _presence(report, f"Line already present at line {present[comparable]}: {line}. No duplicate added.")
        else:
            _presence(report, f"Line absent after scanning the whole current file; adding: {line}")
            missing.append(line)
            present[comparable] = len(text.splitlines()) + len(missing)
    if missing and text.splitlines() and _continued(text.splitlines()[-1]):
        raise TransformError("Cannot append after an unfinished continuation")
    return _append(text, missing)


_ASSIGNMENT = re.compile(
    r"^(?P<indent>[ \t]*)(?:(?:override|export)\s+)?"
    r"(?P<key>[A-Za-z_][A-Za-z0-9_.-]*)\s*"
    r"(?P<operator>:=|\?=|\+=|=)\s*(?P<value>.*)$"
)


def _make_statements(text: str):
    """Yield statements and conditional depth for mixed Make/shell files."""
    lines = text.splitlines(keepends=True)
    stack = []
    i = 0
    while i < len(lines):
        first = i
        fragments = []
        while True:
            code, _ = _comment(_body(lines[i]))
            continuation = _continued(lines[i])
            fragments.append(code.rstrip()[:-1] if continuation else code)
            i += 1
            if not continuation:
                break
            if i == len(lines):
                raise TransformError("Unfinished make continuation at end of file")
        logical = " ".join(fragments)
        stripped = logical.strip()
        if re.match(r"^(?:ifeq|ifneq|ifdef|ifndef)(?:\s|\()", stripped):
            yield first, i, logical, len(stack)
            stack.append("make")
        elif re.match(r"^if(?:\s|\[|\()", stripped):
            # SecProductFeature.common may mix shell conditionals with Make
            # assignments, e.g. `if [[ ... ]]; then`, `else`, `fi`.
            yield first, i, logical, len(stack)
            stack.append("shell")
        elif re.match(r"^endif(?:\s|$)", stripped):
            if not stack or stack[-1] != "make":
                raise TransformError("Unmatched make endif")
            stack.pop()
            yield first, i, logical, len(stack)
        elif re.match(r"^fi(?:\s|$)", stripped):
            if not stack or stack[-1] != "shell":
                raise TransformError("Unmatched shell fi")
            stack.pop()
            yield first, i, logical, len(stack)
        elif re.match(r"^elif(?:\s|\[|\()", stripped):
            if not stack or stack[-1] != "shell":
                raise TransformError("Unmatched shell elif")
            yield first, i, logical, len(stack)
        elif re.match(r"^else(?:\s|$)", stripped):
            if not stack:
                raise TransformError("Unmatched conditional else")
            yield first, i, logical, len(stack)
        else:
            yield first, i, logical, len(stack)
    if stack:
        kind = stack[-1]
        raise TransformError(f"Unclosed {kind} conditional")


def _assignments(text: str, action: dict, *, report=None) -> str:
    values = action.get("values", {})
    if not isinstance(values, dict):
        raise TransformError("assignments.values must be a mapping")
    operator = action.get("operator", ":=")
    if operator not in ("=", ":=", "?=", "+="):
        raise TransformError("Unsupported assignment operator")
    desired = {}
    for key, value in values.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", key):
            raise TransformError(f"Invalid assignment key: {key}")
        value = str(value)
        if "\r" in value or "\n" in value:
            raise TransformError(f"Assignment {key} must have a single-line value")
        desired[key] = value.strip()
    matches = {key: [] for key in desired}
    for start, end, logical, depth in _make_statements(text):
        match = _ASSIGNMENT.match(logical)
        if match and match["key"] in matches:
            matches[match["key"]].append((start, end, match, depth))
    lines = text.splitlines(keepends=True)
    missing = []
    for key, value in desired.items():
        found = matches[key]
        if len(found) > 1:
            raise TransformError(f"Duplicate active assignments for {key}")
        if not found:
            _presence(report, f"Assignment {key} absent after scanning the whole current file; adding its reference value.")
            missing.append(f"{key} {operator} {value}")
            continue
        start, end, match, depth = found[0]
        _presence(report, f"Assignment {key} already exists at line {start + 1}; reuse its location instead of adding a duplicate.")
        if match["value"].strip() == value:
            continue
        if depth:
            raise TransformError(f"Cannot change conditional assignment for {key}")
        if end - start != 1:
            raise TransformError(f"Cannot rewrite multiline assignment for {key}")
        original = lines[start]
        code, comment = _comment(_body(original))
        suffix = (code[len(code.rstrip()):] or " ") + comment if comment else ""
        ending = original[len(_body(original)):]
        prefix = code[:code.index(key)]
        lines[start] = f"{prefix}{key} {operator} {value}{suffix}{ending}"
    return _append("".join(lines), missing)


def _make_packages(text: str, action: dict, *, report=None) -> str:
    packages = []
    for item in action.get("packages", []):
        package = _one_line(item, "package")
        if not re.fullmatch(r"[A-Za-z0-9_./@+-]+", package):
            raise TransformError(f"Invalid literal package name: {package}")
        if package not in packages:
            packages.append(package)
    present = set()
    conditional = set()
    assigned = False
    locations = {}
    for start, _, logical, depth in _make_statements(text):
        match = _ASSIGNMENT.match(logical)
        if not match or match["key"] != "PRODUCT_PACKAGES":
            continue
        tokens = set(match["value"].split())
        for token in tokens:
            locations.setdefault(token, []).append(start + 1)
        if depth:
            conditional.update(tokens)
        elif match["operator"] in ("=", ":="):
            present = tokens
            assigned = True
        elif match["operator"] == "+=" or not assigned:
            present.update(tokens)
            assigned = True
    missing = [item for item in packages if item not in present]
    ambiguous = set(missing) & conditional
    if ambiguous:
        raise TransformError("Package exists only conditionally: " + ", ".join(sorted(ambiguous)))
    for package in packages:
        if package in locations:
            reset = package not in present
            _presence(report, f"Package {package} already appears in PRODUCT_PACKAGES at line(s) " +
                      ', '.join(map(str, locations[package])) + ". No duplicate added. " +
                      ("A later assignment resets the effective package list; review that reset instead of silently re-adding the package." if reset else "Found while scanning the whole file."),
                      review=reset)
    missing = [package for package in missing if package not in locations]
    for package in missing:
        _presence(report, f"Package {package} absent from all current PRODUCT_PACKAGES statements; adding it.")
    if not missing:
        return text
    if len(missing) == 1:
        return _append(text, ["PRODUCT_PACKAGES += " + missing[0]])
    block = ["PRODUCT_PACKAGES += \\"]
    block.extend("    " + item + (" \\" if i + 1 < len(missing) else "")
                 for i, item in enumerate(missing))
    return _append(text, block)


def _tokens(command: str) -> list[str]:
    try:
        return shlex.split(command, comments=True, posix=True)
    except ValueError as exc:
        raise TransformError(f"Invalid init command: {command}") from exc


def _command_key(tokens: list[str]):
    if not tokens:
        return None
    command = tokens[0]
    if command == "chmod" and len(tokens) == 3:
        return command, tokens[2]
    if command == "chown" and len(tokens) in (3, 4):
        return command, tokens[-1]
    if command in ("mkdir", "setprop") and len(tokens) >= 3:
        return command, tokens[1]
    return tuple(tokens)


_BT_CONTEXT = re.compile(
    r"(?i)(?:\bbluetooth\b|\bbluedroid\b|\bbt\b|bt_config|bdaddr|btpower|"
    r"/proc/bluetooth|ttySAC|ssrdump)"
)


def _init_stanza_ranges(lines: list[str], event: str) -> list[tuple[int, int]]:
    starts = []
    for i, line in enumerate(lines):
        code, _ = _comment(_body(line))
        if " ".join(code.split()) == event:
            starts.append(i)
    ranges = []
    for start in starts:
        end = len(lines)
        for i in range(start + 1, len(lines)):
            code, _ = _comment(_body(lines[i]))
            # A few production init.rc files contain accidentally unindented
            # commands inside a large action. Only real top-level headers end
            # an action; otherwise BT blocks later in the action are hidden.
            if code.strip() and re.match(r"^(?:on|service|import)\s", code):
                end = i
                break
        ranges.append((start, end))
    return ranges


def _select_init_stanza(lines: list[str], ranges: list[tuple[int, int]],
                        commands: list[tuple[str, list[str], object]], event: str):
    """Choose a repeated event using exact commands, targets, then BT context."""
    scored = []
    requested = {key: tokens for _, tokens, key in commands}
    for start, end in ranges:
        exact = targets = context = 0
        for line in lines[start + 1:end]:
            code, comment = _comment(_body(line))
            tokens = _tokens(code)
            if tokens:
                key = _command_key(tokens)
                if key in requested:
                    targets += 1
                    exact += tokens == requested[key]
            context += bool(_BT_CONTEXT.search(code)) + bool(_BT_CONTEXT.search(comment))
        # Exact requested commands dominate, target matches handle changed
        # permissions/owners, and comments only break ties or identify a BT block.
        scored.append((exact * 100 + targets * 10 + min(context, 9), start, end,
                       exact, targets, context))
    best_score = max(row[0] for row in scored)
    best = [row for row in scored if row[0] == best_score]
    if best_score == 0 or len(best) != 1:
        details = ", ".join(
            f"line {start + 1}: exact={exact}, targets={targets}, bt_context={context}"
            for _, start, _, exact, targets, context in scored)
        raise TransformError(f"Multiple matching init stanzas remain ambiguous: {event} ({details})")
    return best[0][1], best[0][2]


def _init_commands(text: str, action: dict, *, report=None) -> str:
    event = _one_line(action.get("event", ""), "init event").strip()
    if not event.startswith("on "):
        event = "on " + event
    event = " ".join(event.split())
    commands = []
    requested = set()
    for item in action.get("commands", []):
        command = _one_line(item, "init command").strip()
        tokens = _tokens(command)
        if not tokens or tokens[0] in ("on", "service", "import"):
            raise TransformError("Expected an init command, not a stanza")
        key = _command_key(tokens)
        if key in requested:
            raise TransformError(f"Duplicate requested init command target: {key}")
        requested.add(key)
        commands.append((command, tokens, key))
    lines = text.splitlines(keepends=True)
    # Search commands throughout all actions, not just the requested event.
    # Keep section/line evidence: equal text in another event does not imply
    # equal execution timing, and a target with other values needs review.
    elsewhere = {}
    section = "before the first action"
    verbs = {tokens[0] for _, tokens, _ in commands}
    for i, line in enumerate(lines):
        code, _ = _comment(_body(line))
        if re.match(r"^(?:on|service|import)\s", code):
            section = ' '.join(code.split())
            continue
        if section.startswith('on ') and code.split() and code.split()[0] in verbs:
            tokens = _tokens(code)
            elsewhere.setdefault(_command_key(tokens), []).append((i, tokens, section))

    def already_elsewhere(command, tokens, key):
        found = elsewhere.get(key, [])
        if not found:
            _presence(report, f"Init command target absent from all current actions; adding '{command}' to [{event}].")
            return False
        details = '; '.join(f"line {i + 1} in [{header}]: " + ' '.join(actual) for i, actual, header in found)
        exact = all(actual == tokens for _, actual, _ in found)
        _presence(report, f"No addition of init command '{command}' to [{event}]: " +
                  ("the same command is already present elsewhere. " if exact else "the same command target already exists elsewhere with different arguments. ") +
                  details + ". Existing commands and sections are preserved. Review event timing" +
                  ("." if exact else " and the reference/current argument difference."), review=True)
        return True

    if not commands:
        return text
    ranges = _init_stanza_ranges(lines, event)
    if not ranges:
        commands = [entry for entry in commands if not already_elsewhere(*entry)]
        if not commands:
            return text
        if lines and _continued(lines[-1]):
            raise TransformError("Cannot append after an unfinished init continuation")
        return _append(text, [event] + ["    " + command for command, _, _ in commands])
    start, end = (ranges[0] if len(ranges) == 1
                  else _select_init_stanza(lines, ranges, commands, event))
    existing = {}
    for i in range(start + 1, end):
        code, _ = _comment(_body(lines[i]))
        tokens = _tokens(code)
        if tokens:
            existing.setdefault(_command_key(tokens), []).append((i, tokens))
    missing = []
    for command, tokens, key in commands:
        found = existing.get(key, [])
        if len(found) > 1:
            raise TransformError(f"Duplicate init command target in {event}: {key}")
        if not found:
            if not already_elsewhere(command, tokens, key):
                missing.append("    " + command)
        elif found[0][1] != tokens:
            # Replacing a local command must not create an exact duplicate of
            # the requested command already present in a different action.
            exact_elsewhere = [record for record in elsewhere.get(key, [])
                               if record[0] != found[0][0] and record[1] == tokens]
            if exact_elsewhere:
                already_elsewhere(command, tokens, key)
                continue
            i = found[0][0]
            _presence(report, f"Init command target already present at line {i + 1} in [{event}]; update its arguments in place instead of adding a command: {command}.")
            original = lines[i]
            code, comment = _comment(_body(original))
            indent = code[:len(code) - len(code.lstrip())]
            suffix = (code[len(code.rstrip()):] or " ") + comment if comment else ""
            lines[i] = indent + command + suffix + original[len(_body(original)):]
        else:
            _presence(report, f"Init command already present at line {found[0][0] + 1} in [{event}]: {command}. No duplicate added.")
    if missing:
        prefix = "".join(lines[:end])
        return _append(prefix, missing) + "".join(lines[end:])
    return "".join(lines)


def _parse_xml(text: str):
    try:
        return ET.fromstring(text)
    except ET.ParseError as exc:
        raise TransformError(f"Invalid XML: {exc}") from exc


def _xml_mask(text: str) -> str:
    return re.sub(r"<!--[\s\S]*?-->|<!\[CDATA\[[\s\S]*?\]\]>|<\?[\s\S]*?\?>",
                  lambda match: " " * len(match.group()), text)


def _opening(text: str, tag: str):
    pattern = r"<" + re.escape(tag) + r"(?=[\s/>])(?:\"[^\"]*\"|'[^']*'|[^'\">])*?>"
    matches = list(re.finditer(pattern, _xml_mask(text)))
    if len(matches) != 1:
        raise TransformError(f"Cannot locate unique XML element: {tag}")
    return matches[0]


def _xml_elements(text: str, action: dict, *, report=None) -> str:
    elements = action.get("elements", {})
    if not isinstance(elements, dict):
        raise TransformError("xml_elements.elements must be a mapping")
    parent = action.get("closing_parent", action.get("parent", "permissions"))
    parent = re.sub(r"^</|>$", "", str(parent))
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", parent):
        raise TransformError("XML insertion parent must be a simple tag name")
    for tag, value in elements.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", tag):
            raise TransformError(f"Unsupported XML leaf tag: {tag}")
        value = str(value)
        root = _parse_xml(text)
        found = list(root.iter(tag))
        if len(found) > 1:
            raise TransformError(f"Duplicate XML leaf tag: {tag}")
        if found:
            _presence(report, f"XML element {tag} already exists in the current XML tree; reuse it instead of adding a duplicate, regardless of its position.")
            if len(found[0]):
                raise TransformError(f"XML element is not a leaf: {tag}")
            if (found[0].text or "") == value:
                continue
            opening = _opening(text, tag)
            if opening.group().endswith("/>"):
                replacement = text[opening.start():opening.end() - 2] + ">" + escape(value) + f"</{tag}>"
                text = text[:opening.start()] + replacement + text[opening.end():]
            else:
                closing = re.search(r"</" + re.escape(tag) + r"\s*>", _xml_mask(text)[opening.end():])
                if closing is None:
                    raise TransformError(f"Missing closing tag: {tag}")
                stop = opening.end() + closing.start()
                text = text[:opening.end()] + escape(value) + text[stop:]
        else:
            _presence(report, f"XML element {tag} absent from the whole current XML tree; adding it under {parent}.")
            parents = list(root.iter(parent))
            if len(parents) != 1:
                raise TransformError(f"Cannot locate unique XML insertion parent: {parent}")
            opening = _opening(text, parent)
            nl = _newline(text)
            line_start = text.rfind("\n", 0, opening.start()) + 1
            prefix = text[line_start:opening.start()]
            indent = prefix if not prefix.strip() else ""
            leaf = f"{indent}    <{tag}>{escape(value)}</{tag}>"
            if opening.group().endswith("/>"):
                replacement = text[opening.start():opening.end() - 2] + ">" + nl + leaf + nl + indent + f"</{parent}>"
                text = text[:opening.start()] + replacement + text[opening.end():]
            else:
                closes = list(re.finditer(r"</" + re.escape(parent) + r"\s*>", _xml_mask(text)))
                if len(closes) != 1:
                    raise TransformError(f"Cannot locate unique closing parent: {parent}")
                stop = closes[0].start()
                close_start = text.rfind("\n", 0, stop) + 1
                if not text[close_start:stop].strip():
                    text = text[:close_start] + leaf + nl + text[close_start:]
                else:
                    text = text[:stop] + nl + leaf + nl + indent + text[stop:]
        _parse_xml(text)
    _parse_xml(text)
    return text


def _replace_block(text: str, action: dict) -> str:
    before, after = action.get("before"), action.get("after")
    if not isinstance(before, str) or not before or not isinstance(after, str):
        raise TransformError("replace_block requires nonempty before and string after")
    # Rule literals use LF; adapt to the existing document without normalizing it.
    if _newline(text) == "\r\n":
        before = before.replace("\r\n", "\n").replace("\n", "\r\n")
        after = after.replace("\r\n", "\n").replace("\n", "\r\n")
    count = text.count(before)
    if count > 1:
        raise TransformError("The original replacement block occurs more than once")
    if count == 1:
        if after and after != before and after not in before and after in text:
            raise TransformError("Both original and replacement blocks already occur")
        return text.replace(before, after, 1)
    if not after or text.count(after) == 1:
        return text
    raise TransformError("Neither a unique original nor applied replacement block was found")


_TRANSFORMS = {
    "ensure_lines": _ensure_lines,
    "assignments": _assignments,
    "make_packages": _make_packages,
    "init_commands": _init_commands,
    "xml_elements": _xml_elements,
    "replace_block": _replace_block,
}


def transform(text: str, action: dict, *, report=None) -> str:
    """Return edited text or raise TransformError; never change external state."""
    if not isinstance(text, str) or not isinstance(action, dict):
        raise TransformError("transform requires text and an action mapping")
    kind = action.get("type", action.get("kind"))
    if kind == "copy_reference":
        raise TransformError("copy_reference is resolved by the planner")
    try:
        handler = _TRANSFORMS[kind]
    except (KeyError, TypeError) as exc:
        raise TransformError(f"Unknown transform type: {kind}") from exc
    if kind in ('init_commands', 'make_packages', 'ensure_lines', 'assignments', 'xml_elements'):
        return handler(text, action, report=report)
    return handler(text, action)
