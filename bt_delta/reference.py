"""Select and copy Bluetooth-related statements from resolved reference files."""
import copy
import re

from .transforms import (TransformError, _ASSIGNMENT, _body, _command_key, _comment,
                         _init_stanza_ranges, _make_statements, _newline,
                         _select_init_stanza, _tokens, transform)


def _patterns(values, label):
    try:
        return [re.compile(value) for value in values]
    except re.error as exc:
        raise TransformError(f"Invalid {label} pattern: {exc}") from exc


def _matches(patterns, value):
    return any(pattern.search(value) for pattern in patterns)


def _reference_packages(text, action):
    patterns = _patterns(action.get("reference_package_patterns", []), "package")
    static = set(action.get("packages", []))
    selected, conditional = [], set()
    for _, _, logical, depth in _make_statements(text):
        assignment = _ASSIGNMENT.match(logical)
        if not assignment or assignment["key"] != "PRODUCT_PACKAGES":
            continue
        for token in assignment["value"].split():
            if token not in static and not _matches(patterns, token):
                continue
            if depth:
                conditional.add(token)
            elif token not in selected:
                selected.append(token)
    ambiguous = conditional - set(selected)
    if ambiguous:
        raise TransformError("Reference Bluetooth package is conditional; review before copying: " +
                             ", ".join(sorted(ambiguous)))
    return selected


def _reference_lines(text, action):
    patterns = _patterns(action.get("reference_line_patterns", []), "line")
    static = {line.strip() for line in action.get("lines", [])}
    selected, conditional = [], set()
    for _, _, logical, depth in _make_statements(text):
        line = logical.strip()
        if not re.match(r"^-?include\s+\S+\s*$", line) or (line not in static and not _matches(patterns, line)):
            continue
        if depth:
            conditional.add(line)
        elif line not in selected:
            selected.append(line)
    ambiguous = conditional - set(selected)
    if ambiguous:
        raise TransformError("Reference Bluetooth include is conditional; review before copying: " +
                             ", ".join(sorted(ambiguous)))
    return selected


def _reference_init_commands(text, action):
    command_patterns = _patterns(action.get("reference_command_patterns", []), "init command")
    comment_patterns = _patterns(action.get("reference_comment_patterns", []), "init comment")
    event = str(action.get("event", "")).strip()
    if not event.startswith("on "):
        event = "on " + event
    event = " ".join(event.split())
    lines = text.splitlines(keepends=True)
    ranges = _init_stanza_ranges(lines, event)
    if not ranges:
        return []
    requested = []
    for command in action.get("commands", []):
        tokens = _tokens(command)
        requested.append((command, tokens, _command_key(tokens)))
    start, end = (ranges[0] if len(ranges) == 1
                  else _select_init_stanza(lines, ranges, requested, event))
    static_keys = {_command_key(_tokens(command)) for command in action.get("commands", [])}
    selected = []
    anchored = False
    for line in lines[start + 1:end]:
        code, comment = _comment(_body(line))
        if comment:
            anchored = _matches(comment_patterns, comment) if comment_patterns else False
        if not code.strip():
            if not comment:
                anchored = False
            continue
        tokens = _tokens(code)
        if not tokens or tokens[0] in ("on", "service", "import"):
            continue
        command = code.strip()
        direct = _matches(command_patterns, code) if command_patterns else False
        if (direct or anchored or _command_key(tokens) in static_keys) and command not in selected:
            selected.append(command)
    return selected


def augment_actions_from_reference(reference, actions):
    """Checklist entries select relevant reference content; they supply no defaults."""
    result = []
    for original in actions:
        action = copy.deepcopy(original)
        kind = action.get("type")
        if kind == "make_packages":
            action["packages"] = _reference_packages(reference, action)
            if not action["packages"]:
                continue
        elif kind == "ensure_lines":
            action["lines"] = _reference_lines(reference, action)
            if not action["lines"]:
                continue
        elif kind == "init_commands":
            action["commands"] = _reference_init_commands(reference, action)
            if not action["commands"]:
                continue
        elif kind == "assignments":
            # Keep the source operator/value/comment instead of checklist defaults.
            action = {"type": "reference_assignments", "keys": list(action["values"]), "reference": reference}
        else:
            raise TransformError("No reference selector implemented for action: " + str(kind))
        result.append(action)
    return result


def apply_reference_action(current, action):
    if action["type"] == "reference_assignments":
        return copy_make_settings(current, action["reference"], action["keys"], [])
    return transform(current, action)


def copy_make_settings(current, reference, keys, include_basenames, key_patterns=None, *, report=None):
    """Preserve reference values/operators/comments, and unrelated current lines.

    Selectors describe what to copy, never the desired values. Conditional,
    duplicated source settings require review. Absent settings are optional.
    """
    try:
        patterns = [re.compile(pattern) for pattern in (key_patterns or [])]
    except re.error as exc:
        raise TransformError(f'Invalid reference assignment key pattern: {exc}') from exc

    def selected(text, allowed=None):
        found = {}
        for start, end, logical, depth in _make_statements(text):
            assignment = _ASSIGNMENT.match(logical)
            token = None
            if assignment and (assignment['key'] in keys or any(pattern.search(assignment['key']) for pattern in patterns)):
                token = 'assignment:' + assignment['key']
            else:
                include = re.match(r'^\s*-?include\s+(.+?)\s*$', logical)
                if include:
                    paths = include[1].split()
                    matches = [name for name in include_basenames
                               if any(path.rsplit('/', 1)[-1] == name for path in paths)]
                    if matches:
                        if len(paths) != 1 or len(matches) != 1:
                            raise TransformError('Selected include must contain one path')
                        token = 'include:' + matches[0]
            if token and (allowed is None or token in allowed):
                if depth or token in found:
                    raise TransformError(f'Conditional or duplicate reference-copy setting: {token}')
                found[token] = (start, end)
        return found

    source = selected(reference)
    destination = selected(current, source)
    expected = {'assignment:' + k for k in keys} | {'include:' + n for n in include_basenames}
    missing = expected - source.keys()
    if report:
        report('Reference board selection: ' + str(len(source)) + ' statement(s); selectors are optional.')
        for token in sorted(missing):
            report('Absent in reference; skipped optional setting: ' + token + '. Existing current content is preserved.')
        for token in source:
            report('Selected reference setting: ' + token)
    source_lines = reference.splitlines(keepends=True)
    lines = current.splitlines(keepends=True)
    nl = _newline(current)
    replacements, additions = [], []
    for token, (start, end) in sorted(source.items(), key=lambda item: item[1][0]):
        statement = ''.join(source_lines[start:end]).replace('\r\n', '\n').replace('\n', nl)
        if token in destination:
            first, last = destination[token]
            # Keep the destination's final-newline convention when possible.
            if lines[last - 1].endswith(('\n', '\r')):
                statement = statement.rstrip('\r\n') + nl
            else:
                statement = statement.rstrip('\r\n')
            replacements.append((first, last, statement))
        else:
            additions.append(statement.rstrip('\r\n'))
    for start, end, statement in sorted(replacements, reverse=True):
        lines[start:end] = [statement]
    result = ''.join(lines)
    if additions:
        if result and not result.endswith(('\n', '\r')):
            result += nl
        result += nl.join(additions) + nl
    return result
