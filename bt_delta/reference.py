"""Select and copy Bluetooth-related statements from resolved reference files."""
import copy
import re

from .transforms import (TransformError, _ASSIGNMENT, _body, _command_key, _comment,
                         _init_stanza_ranges, _make_statements, _newline,
                         _select_init_stanza, _tokens)


def _patterns(values, label):
    try:
        return [re.compile(value) for value in values]
    except re.error as exc:
        raise TransformError(f"Invalid {label} pattern: {exc}") from exc


def _matches(patterns, value):
    return any(pattern.search(value) for pattern in patterns)


def _reference_packages(text, action):
    patterns = _patterns(action.get("reference_package_patterns", []), "package")
    if not patterns:
        return []
    static = set(action.get("packages", []))
    selected, conditional = [], set()
    for _, _, logical, depth in _make_statements(text):
        assignment = _ASSIGNMENT.match(logical)
        if not assignment or assignment["key"] != "PRODUCT_PACKAGES":
            continue
        for token in assignment["value"].split():
            if not _matches(patterns, token):
                continue
            if depth:
                conditional.add(token)
            elif token not in selected:
                selected.append(token)
    ambiguous = conditional - set(selected) - static
    if ambiguous:
        raise TransformError("Reference Bluetooth package is conditional; review before copying: " +
                             ", ".join(sorted(ambiguous)))
    return selected


def _reference_lines(text, action):
    patterns = _patterns(action.get("reference_line_patterns", []), "line")
    if not patterns:
        return []
    static = {line.strip() for line in action.get("lines", [])}
    selected, conditional = [], set()
    for _, _, logical, depth in _make_statements(text):
        line = logical.strip()
        if not re.match(r"^-?include\s+\S+\s*$", line) or not _matches(patterns, line):
            continue
        if depth:
            conditional.add(line)
        elif line not in selected:
            selected.append(line)
    ambiguous = conditional - set(selected) - static
    if ambiguous:
        raise TransformError("Reference Bluetooth include is conditional; review before copying: " +
                             ", ".join(sorted(ambiguous)))
    return selected


def _reference_init_commands(text, action):
    command_patterns = _patterns(action.get("reference_command_patterns", []), "init command")
    comment_patterns = _patterns(action.get("reference_comment_patterns", []), "init comment")
    if not command_patterns and not comment_patterns:
        return []
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
        if (direct or anchored) and _command_key(tokens) not in static_keys and command not in selected:
            selected.append(command)
    return selected


def augment_actions_from_reference(reference, actions):
    """Union static checklist actions with safe, unconditional reference matches."""
    result = copy.deepcopy(actions)
    for action in result:
        kind = action.get("type")
        if kind == "make_packages":
            action["packages"] = list(dict.fromkeys(action.get("packages", []) +
                                                     _reference_packages(reference, action)))
        elif kind == "ensure_lines":
            action["lines"] = list(dict.fromkeys(action.get("lines", []) +
                                                  _reference_lines(reference, action)))
        elif kind == "init_commands":
            action["commands"] = list(dict.fromkeys(action.get("commands", []) +
                                                     _reference_init_commands(reference, action)))
    return result


def copy_make_settings(current, reference, keys, include_basenames, key_patterns=None):
    """Preserve reference values/operators/comments, and unrelated current lines.

    Selectors describe what to copy, never the desired values. Conditional,
    duplicated or absent source settings require review instead of a fallback.
    """
    try:
        patterns = [re.compile(pattern) for pattern in (key_patterns or [])]
    except re.error as exc:
        raise TransformError(f'Invalid reference assignment key pattern: {exc}') from exc

    def selected(text):
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
            if token:
                if depth or token in found:
                    raise TransformError(f'Conditional or duplicate reference-copy setting: {token}')
                found[token] = (start, end)
        return found

    source = selected(reference)
    destination = selected(current)
    expected = {'assignment:' + k for k in keys} | {'include:' + n for n in include_basenames}
    missing = expected - source.keys()
    if missing:
        raise TransformError('Missing settings in reference; no static fallback: ' + ', '.join(sorted(missing)))
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
