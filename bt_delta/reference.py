"""Select and copy selected statements from resolved reference files."""
import copy
import re

from .transforms import (TransformError, _ASSIGNMENT, _append, _body, _command_key, _comment,
                         _init_stanza_ranges, _make_statements, _newline,
                         _presence, _select_init_stanza, _tokens, transform)


def _patterns(values, label):
    try:
        return [re.compile(value) for value in values]
    except re.error as exc:
        raise TransformError(f"Invalid {label} pattern: {exc}") from exc


def _matches(patterns, value):
    return any(pattern.search(value) for pattern in patterns)


def _package_entries(text, variable="PRODUCT_PACKAGES"):
    """Record each package list together with its enclosing conditional branches."""
    lines, stack = text.splitlines(keepends=True), []
    for start, end, logical, _ in _make_statements(text):
        code = logical.strip()
        raw = ''.join(lines[start:end])
        if re.match(r'^(?:ifeq|ifneq|ifdef|ifndef)(?:\s|\()', code) or re.match(r'^if(?:\s|\[|\()', code):
            # Keep the explanatory comment immediately above the condition.
            first = start
            while first and lines[first - 1].lstrip().startswith('#'):
                first -= 1
            stack.append({'branches': [(code, ''.join(lines[first:end]))],
                          'close': 'fi' if re.match(r'^if(?:\s|\[|\()', code) else 'endif'})
        elif re.match(r'^(?:else|elif)(?:\s|$)', code):
            stack[-1]['branches'].append((code, raw))
        elif re.match(r'^(?:endif|fi)(?:\s|$)', code):
            stack.pop()
        else:
            assignment = _ASSIGNMENT.match(logical)
            if assignment and assignment['key'] == variable:
                yield {'start': start, 'assignment': assignment.groupdict(), 'text': raw,
                       'guards': copy.deepcopy(stack),
                       'context': tuple(tuple(' '.join(code.split()) for code, _ in frame['branches'])
                                        for frame in stack)}


def _reference_packages(text, action):
    patterns = _patterns(action.get("reference_package_patterns", []), "package")
    static = set(action.get("packages", []))
    selected, conditional = [], []
    for entry in _package_entries(text, action.get("variable", "PRODUCT_PACKAGES")):
        tokens = entry['assignment']['value'].split()
        packages = list(dict.fromkeys(token for token in tokens if token in static or _matches(patterns, token)))
        if not packages:
            continue
        # Validate literal package names using the ordinary package transform.
        transform('', {'type': 'make_packages', 'packages': packages})
        if entry['guards']:
            if entry['assignment']['operator'] != '+=':
                raise TransformError('Conditional reference PRODUCT_PACKAGES must use += before copying selected packages')
            entry['packages'] = packages
            conditional.append(entry)
        else:
            selected.extend(token for token in packages if token not in selected)
    return selected, conditional


def _apply_conditional_packages(current, entries, *, report=None, variable="PRODUCT_PACKAGES"):
    locations, added = {}, set()
    for entry in _package_entries(current, variable):
        for token in entry['assignment']['value'].split():
            locations.setdefault(token, []).append(entry)
    for source in entries:
        missing = []
        for package in source['packages']:
            if (package, source['context']) in added:
                continue
            found = locations.get(package, [])
            if found:
                matching = any(entry['context'] == source['context'] for entry in found)
                _presence(report, f"Conditional package {package} already exists at line(s) " +
                          ', '.join(str(entry['start'] + 1) for entry in found) + '. No duplicate added. ' +
                          ('Its enclosing conditions match reference.' if matching else
                           'Its enclosing conditions differ from reference; existing content is preserved for review.'),
                          review=not matching)
            else:
                missing.append(package)
                added.add((package, source['context']))
        if not missing:
            continue
        statement = (source['text'] if missing == source['assignment']['value'].split() else
                     transform('', {'type': 'make_packages', 'packages': missing, 'variable': variable}))
        prefix = ''.join(raw.rstrip('\r\n') + '\n' for guard in source['guards']
                         for _, raw in guard['branches'])
        suffix = ''.join(guard['close'] + '\n' for guard in reversed(source['guards']))
        block = prefix + statement.rstrip('\r\n') + '\n' + suffix
        block = block.replace('\r\n', '\n').replace('\n', _newline(current)).rstrip('\r\n')
        # Validate the complete wrapper before including it in a plan.
        list(_make_statements(block))
        _presence(report, 'Conditional package(s) absent from the whole current file: ' + ', '.join(missing) +
                  '. Copying their reference condition(s) and PRODUCT_PACKAGES statement:\n' + block)
        current = _append(current, [block])
    return current


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
        raise TransformError("Reference include is conditional; review before copying: " +
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
                  else _select_init_stanza(lines, ranges, requested, event, action.get("context_patterns", [])))
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
        direct = _matches(command_patterns, command) if command_patterns else False
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
            action["packages"], action["conditional_packages"] = _reference_packages(reference, action)
            if not action["packages"] and not action["conditional_packages"]:
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


def apply_reference_action(current, action, *, report=None):
    if action["type"] == "make_packages":
        current = transform(current, action, report=report)
        return _apply_conditional_packages(current, action.get('conditional_packages', []), report=report, variable=action.get('variable', 'PRODUCT_PACKAGES'))
    if action["type"] == "reference_assignments":
        forward = (lambda message: report({"message": message, "review": False})) if report else None
        return copy_make_settings(current, action["reference"], action["keys"], [], report=forward)
    return transform(current, action, report=report)


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
            if report:
                report(f'Existing {token} at current line(s) {first + 1}-{last}; reuse this location instead of adding a duplicate.')
            # Keep the destination's final-newline convention when possible.
            if lines[last - 1].endswith(('\n', '\r')):
                statement = statement.rstrip('\r\n') + nl
            else:
                statement = statement.rstrip('\r\n')
            replacements.append((first, last, statement))
        else:
            if report:
                report(f'{token} absent after scanning the whole current file; adding the selected reference statement.')
            additions.append(statement.rstrip('\r\n'))
    for start, end, statement in sorted(replacements, reverse=True):
        lines[start:end] = [statement]
    result = ''.join(lines)
    if additions:
        if result and not result.endswith(('\n', '\r')):
            result += nl
        result += nl.join(additions) + nl
    return result
