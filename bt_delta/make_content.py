"""Comment-led Make selection and merging; all domain selectors come from JSON."""

import re

from .transforms import (
    TransformError, _ASSIGNMENT, _append, _comment, _make_statements,
    _newline, _presence,
)


def _matches(value, patterns):
    return any(re.search(pattern, value) for pattern in patterns)


def _member_statement(variable, members):
    if len(members) == 1:
        return variable + " += " + members[0]
    return variable + " += \\\n" + " \\\n".join("    " + member for member in members)


def _words(value):
    """Split list members without splitting Make expressions or quoted values."""
    words, start, stack, quote, escaped = [], None, [], None, False
    for i, char in enumerate(value):
        if start is None and not char.isspace():
            start = i
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif quote:
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char in "({":
            stack.append(char)
        elif char in ")}" and stack:
            stack.pop()
        elif char.isspace() and not stack and start is not None:
            words.append(value[start:i])
            start = None
    if stack or quote or escaped:
        raise TransformError("Unfinished expression in selected Make list: " + value)
    if start is not None:
        words.append(value[start:])
    return words


def _entries(text, comment_patterns=()):
    """Keep leaf statements plus the exact guard spans needed to copy them."""
    lines, stack, entries, heading = text.splitlines(keepends=True), [], [], None
    for start, end, logical, _ in _make_statements(text):
        code = logical.strip()
        if not code:
            _, comment = _comment(lines[start].rstrip("\r\n"))
            if comment:
                # Only standalone comments delimit sections; inline comments and
                # blank lines do not cut a continued assignment or end a section.
                heading = start if _matches(comment, comment_patterns) else None
            continue
        span = (start, end)
        if re.match(r"^(?:ifeq|ifneq|ifdef|ifndef)(?:\s|\()|^if(?:\s|\[|\()", code):
            first = start
            while first and lines[first - 1].lstrip().startswith("#"):
                first -= 1
            stack.append({"branches": [(span, code)], "comment": (first, start)})
        elif re.match(r"^(?:else|elif)(?:\s|\[|\(|$)", code):
            stack[-1]["branches"].append((span, code))
        elif re.match(r"^(?:endif|fi)(?:\s|$)", code):
            stack.pop()["close"] = span
        else:
            guards = [(frame, len(frame["branches"])) for frame in stack]
            entries.append({
                "span": span, "code": code, "heading": heading, "guards": guards,
                "context": tuple(tuple(branch for _, branch in frame["branches"][:count])
                                 for frame, count in guards),
                "assignment": _ASSIGNMENT.match(code),
            })
    return lines, entries


def _render(lines, entries, overrides=None):
    """Reconstruct selected statements in source order with balanced conditions."""
    spans = {}
    for entry in entries:
        spans[entry["span"]] = (overrides or {}).get(entry["span"])
        if entry["heading"] is not None:
            spans[(entry["heading"], entry["heading"] + 1)] = None
        for frame, count in entry["guards"]:
            first, end = frame["comment"]
            for i in range(first, end):
                spans[(i, i + 1)] = None
            for span, _ in frame["branches"][:count]:
                spans[span] = None
            spans[frame["close"]] = None
    return "".join((replacement if replacement is not None else "".join(lines[first:last])).rstrip("\r\n") + "\n"
                   for (first, last), replacement in sorted(spans.items()))


def select_content(reference, select, *, require_section=False):
    """Select whole comment sections and independently selected statements.

    All Make copy operations use this helper when they opt into comment
    selection. ``None`` lets a caller retain its original selector behavior
    when the reference contains no matching section with content.
    """
    lines, entries = _entries(reference, select.get("comment_patterns", []))
    if require_section and not any(entry["heading"] is not None for entry in entries):
        return None
    packages, includes, assignments = (select.get(key, {}) for key in ("packages", "includes", "assignments"))
    variable = packages.get("variable")
    selected, overrides = [], {}
    for entry in entries:
        code, assignment = entry["code"], entry["assignment"]
        if entry["heading"] is not None:
            selected.append(entry)
        elif assignment and assignment["key"] == variable:
            members = _words(assignment["value"])
            chosen = [member for member in members if member in packages.get("names", [])
                      or _matches(member, packages.get("patterns", []))]
            if chosen:
                if entry["guards"] and assignment["operator"] != "+=":
                    raise TransformError("Conditional reference list must use += before copying: " + variable)
                selected.append(entry)
                if not entry["guards"] or chosen != members:
                    overrides[entry["span"]] = _member_statement(variable, chosen)
        elif re.match(r"^-?include\s+", code):
            if (code in includes.get("lines", []) or _matches(code, includes.get("patterns", []))
                    or code.split(None, 1)[1].rsplit("/", 1)[-1] in includes.get("basenames", [])):
                selected.append(entry)
        elif assignment and (assignment["key"] in assignments.get("assignment_names", [])
                             or (assignments.get("prefix") and assignment["key"].startswith(assignments["prefix"]))
                             or _matches(assignment["key"], assignments.get("assignment_patterns", []))):
            selected.append(entry)
    # Distinct branches are legitimate; multiple scalar definitions in the
    # same context are ambiguous and must not silently discard a source value.
    scalars, include_paths = set(), {}
    for entry in selected:
        assignment = entry["assignment"]
        if assignment and assignment["operator"] != "+=" and assignment["key"] != variable:
            identity = (assignment["key"], entry["context"])
            if identity in scalars:
                raise TransformError("Duplicate selected reference assignment: " + assignment["key"])
            scalars.add(identity)
        include = re.match(r"^-?include\s+(.+)$", entry["code"])
        if include and include[1].rsplit("/", 1)[-1] in includes.get("basenames", []):
            identity = (include[1].rsplit("/", 1)[-1], entry["context"])
            if identity in include_paths and include_paths[identity] != entry["code"]:
                raise TransformError("Conflicting selected reference includes: " + identity[0])
            include_paths[identity] = entry["code"]
    content = _render(lines, selected, overrides)
    return [{"type": "make_content", "reference": content,
             "include_basenames": includes.get("basenames", []),
             "list_variables": [variable] if variable else []}] if selected else []


def apply_content(current, action, report=None):
    reference = action["reference"]
    if action.get("operator"):
        # Honor an explicit scalar-operator option without converting additive
        # statements into resets or discarding reference guards/comments.
        raw_lines = reference.splitlines(keepends=True)
        for first, _, logical, _ in _make_statements(reference):
            assignment = _ASSIGNMENT.match(logical)
            if assignment and assignment["operator"] != "+=":
                raw = _ASSIGNMENT.match(raw_lines[first])
                if raw:
                    a, b = raw.span("operator")
                    raw_lines[first] = raw_lines[first][:a] + action["operator"] + raw_lines[first][b:]
        reference = "".join(raw_lines)
    source_lines, source = _entries(reference)
    lines, destination = _entries(current)
    # Recover heading spans for selected content, for additions only. Selection
    # has already happened; these comments cannot select unrelated statements.
    heading = None
    by_start = {entry["span"][0]: entry for entry in source}
    for first, _, logical, _ in _make_statements(reference):
        if not logical.strip() and source_lines[first].lstrip().startswith("#"):
            heading = first
        if first in by_start:
            by_start[first]["heading"] = heading
    lists = set(action["list_variables"])
    additions, overrides, replacements, introduced = [], {}, {}, set()

    def present(entry, found, label):
        same = any(other["context"] == entry["context"] for other in found)
        _presence(report, label + " already present at line(s) " +
                  ", ".join(str(other["span"][0] + 1) for other in found) +
                  ". No duplicate added." + ("" if same else " Enclosing conditions differ from reference; existing content preserved."),
                  review=not same)

    for entry in source:
        code, assignment, context = entry["code"], entry["assignment"], entry["context"]
        if assignment and assignment["key"] in lists:
            variable = assignment["key"]
            if context and assignment["operator"] != "+=":
                raise TransformError("Conditional reference list must use += before copying: " + variable)
            candidates = [other for other in destination if other["assignment"] and other["assignment"]["key"] == variable]
            missing = []
            for member in _words(assignment["value"]):
                identity = (variable, member, context)
                if identity in introduced:
                    continue
                found = [other for other in candidates if member in _words(other["assignment"]["value"])]
                if found:
                    present(entry, found, variable + ": " + member)
                    # A later unconditional reset can make an earlier member
                    # ineffective. Preserve the file and surface that conflict.
                    resets = [other for other in candidates if not other["context"] and other["assignment"]["operator"] in ("=", ":=", "::=")]
                    if resets and not any(other["span"][0] >= resets[-1]["span"][0] for other in found):
                        _presence(report, "Existing list member is before a later reset: " + member, review=True)
                else:
                    missing.append(member)
                    introduced.add(identity)
            if missing:
                additions.append(entry)
                # Never reset unrelated destination list members when adding.
                if missing != _words(assignment["value"]) or assignment["operator"] != "+=":
                    overrides[entry["span"]] = variable + " += " + " ".join(missing)
            continue

        include = re.match(r"^(-?include)\s+(.+)$", code)
        if include:
            identity = ("include", include[2])
            found = [other for other in destination if re.match(r"^-?include\s+" + re.escape(include[2]) + r"$", other["code"])]
            basename = include[2].rsplit("/", 1)[-1]
            if basename in action.get("include_basenames", []):
                identity = ("include", basename)
                found = [other for other in destination
                         if re.match(r"^-?include\s+", other["code"])
                         and other["code"].split(None, 1)[1].rsplit("/", 1)[-1] == basename]
                if sum(other["context"] == context for other in found) > 1:
                    raise TransformError("Duplicate current includes in the selected context: " + basename)
        elif assignment and assignment["operator"] != "+=":
            identity = ("assignment", assignment["key"])
            found = [other for other in destination if other["assignment"] and other["assignment"]["key"] == assignment["key"]]
            definitions = [other for other in found if other["assignment"]["operator"] != "+="]
            if sum(other["context"] == context for other in definitions) > 1:
                raise TransformError("Duplicate current assignments in the selected context: " + assignment["key"])
            # An initializer followed by += is a valid reference sequence.
            # Match its initializer without treating appends as duplicate values.
            # With only appends in current, retain them for review: inserting a
            # reset after them would discard existing content.
            found = definitions or found
        else:
            identity = ("statement", code)
            found = [other for other in destination if other["code"] == code]
        exact = [other for other in found if other["code"] == code]
        if include:
            # Separating whitespace is not an include-mode or path difference.
            exact = [other for other in found if other["code"].split(None, 1) == [include[1], include[2]]]
        if exact:
            present(entry, exact, code)
        elif found:
            same = [other for other in found if other["context"] == context]
            replaceable = ((assignment and assignment["operator"] != "+=")
                           or (include and basename in action.get("include_basenames", [])))
            if (replaceable and len(same) == 1
                    and not (assignment and same[0]["assignment"]["operator"] == "+=")):
                label = assignment["key"] if assignment else include[2]
                policy = action.get("on_value_difference", "replace")
                if policy == "replace":
                    span = same[0]["span"]
                    raw = "".join(source_lines[slice(*entry["span"])])
                    if span in replacements and replacements[span] != raw:
                        raise TransformError("Conflicting selected reference values for " + label)
                    replacements[span] = raw
                    _presence(report, "Updating existing Make setting in place: " + label)
                else:
                    _presence(report, "Existing value preserved: " + label, review=policy == "review")
            else:
                present(entry, found, code)
                _presence(report, "Existing Make statement differs; preserved for review: " + code, review=True)
        elif (identity, context) not in introduced:
            additions.append(entry)
            introduced.add((identity, context))

    nl = _newline(current)
    for (first, last), raw in sorted(replacements.items(), reverse=True):
        ending = nl if lines[last - 1].endswith(("\n", "\r")) else ""
        lines[first:last] = [raw.replace("\r\n", "\n").replace("\n", nl).rstrip("\r\n") + ending]
    result = "".join(lines)
    if additions:
        block = _render(source_lines, additions, overrides).replace("\n", nl).rstrip("\r\n")
        _presence(report, f"Copying {len(additions)} missing Make statement(s) with their reference conditions.")
        result = _append(result, [block])
    list(_make_statements(result))
    return result
