"""Choose a subset of a sealed plan without recomputing its reference evidence.

Make statements and complete conditionals are atomic; init sections are atomic.
Other formats stay whole-file. Choices always retain the original bytes or use
the proposed bytes, and a replacement's removal/addition cannot be separated.
"""

from __future__ import annotations

import base64
import copy
from dataclasses import dataclass
import difflib
import re

from .planner import decode, digest, seal, verify_seal
from .transforms import TransformError, _ASSIGNMENT, _make_statements


@dataclass(frozen=True)
class Chunk:
    text: str
    key: str
    label: str


@dataclass(frozen=True)
class Choice:
    id: str
    start: int
    end: int
    before: bytes
    after: bytes
    label: str


def _make_chunks(text):
    lines = text.splitlines(keepends=True)
    chunks, first, block = [], 0, None
    for start, end, logical, depth in _make_statements(text):
        code = logical.strip()
        if block is not None:
            if depth == 0 and re.match(r"^(endif|fi)(\s|$)", code):
                chunks.append(Chunk("".join(lines[first:end]), block, block))
                first, block = end, None
            continue
        if not code:
            continue  # Keep preceding comments/blank lines with their statement.
        if re.match(r"^(?:ifeq|ifneq|ifdef|ifndef)(?:\s|\()|^if(?:\s|\[|\()", code):
            block = code
            continue
        match = _ASSIGNMENT.match(logical)
        if match:
            key = "assignment:" + match["key"]
        elif re.match(r"^-?include\s", code):
            key = "include:" + code.split(None, 1)[1]
        elif (code.startswith("$(") and code.endswith(")")
              and code.count("(") == code.count(")")):
            key = "expression:" + code
        else:
            # Recipes, define/endef and arbitrary Make expressions may span
            # statements. Keep such files whole instead of guessing boundaries.
            raise ValueError("Make constructs require whole-file selection")
        chunks.append(Chunk("".join(lines[first:end]), key, code))
        first = end
    if first < len(lines):
        chunks.append(Chunk("".join(lines[first:]), "trailing", "Trailing comments / spacing"))
    return chunks


def _init_chunks(text):
    lines = text.splitlines(keepends=True)
    chunks, first, header = [], 0, "File preamble"
    for number, line in enumerate(lines):
        if re.match(r"^(?:on|service|import)\s", line):
            if number > first:
                chunks.append(Chunk("".join(lines[first:number]), header, header))
            first, header = number, line.strip()
    if first < len(lines):
        chunks.append(Chunk("".join(lines[first:]), header, header))
    return chunks


def _patches(before, after, format):
    old, old_encoding = decode(before)
    new, new_encoding = decode(after)
    if old_encoding != new_encoding or old_encoding != "utf-8":
        raise ValueError("Keep encoded file together")
    if old and not old.endswith(("\n", "\r")):
        raise ValueError("Keep final-line boundary together")
    parser = {"make": _make_chunks, "init": _init_chunks}[format]
    left, right = parser(old), parser(new)
    offsets = [0]
    for chunk in left:
        offsets.append(offsets[-1] + len(chunk.text.encode("utf-8")))
    patches, changed_keys = [], set()

    def add(i, end, j, stop):
        removed = "".join(c.text for c in left[i:end]).encode("utf-8")
        added = "".join(c.text for c in right[j:stop]).encode("utf-8")
        chunks = right[j:stop] or left[i:end]
        keys = {c.key for c in left[i:end] + right[j:stop]} - {"trailing"}
        if keys & changed_keys:
            # A move or repeated assignment must not expose its removal and
            # addition as independent choices, which could create duplicates.
            raise ValueError("Keep moved/repeated statements together")
        changed_keys.update(keys)
        action = "Change" if removed and added else "Add" if added else "Remove"
        label = action + ": " + " | ".join(c.label for c in chunks)
        patches.append(Choice(str(len(patches) + 1), offsets[i], offsets[end], removed, added, label))

    matcher = difflib.SequenceMatcher(None, [c.text for c in left], [c.text for c in right], autojunk=False)
    for tag, i, end, j, stop in matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag == "insert":
            for n in range(j, stop):
                add(i, i, n, n + 1)
        elif tag == "delete":
            for n in range(i, end):
                add(n, n + 1, j, j)
        else:
            # Match statement identities within a changed region, so adjacent
            # updated flags and newly added flags remain separate choices.
            keys = difflib.SequenceMatcher(None, [c.key for c in left[i:end]], [c.key for c in right[j:stop]], autojunk=False)
            for kind, a, b, c, d in keys.get_opcodes():
                if kind == "equal":
                    for x, y in zip(range(i + a, i + b), range(j + c, j + d)):
                        add(x, x + 1, y, y + 1)
                elif kind == "insert":
                    for y in range(j + c, j + d):
                        add(i + a, i + a, y, y + 1)
                elif kind == "delete":
                    for x in range(i + a, i + b):
                        add(x, x + 1, j + c, j + c)
                else:
                    add(i + a, i + b, j + c, j + d)
    return patches


def change_choices(change, format):
    before = base64.b64decode(change["before"], validate=True)
    after = base64.b64decode(change["after"], validate=True)
    if change["type"].startswith("text") and format in ("make", "init"):
        try:
            choices = _patches(before, after, format)
            if choices:
                return choices
        except (UnicodeError, ValueError, TransformError):
            pass
    return [Choice("1", 0, len(before), before, after, "Complete file")]


def _assemble(before, choices, selected):
    output, cursor = [], 0
    for choice in choices:
        if choice.start < cursor or before[choice.start:choice.end] != choice.before:
            raise ValueError("Invalid review change boundaries")
        output.extend((before[cursor:choice.start], choice.after if choice.id in selected else choice.before))
        cursor = choice.end
    output.append(before[cursor:])
    return b"".join(output)


def _diff(change, content):
    try:
        if not change["type"].startswith("text"):
            raise ValueError("binary")
        old, _ = decode(base64.b64decode(change["before"]))
        new, _ = decode(content)
        return "".join(difflib.unified_diff(
            old.splitlines(True), new.splitlines(True),
            fromfile=change["path"] + (f"#{change['revision']}" if change["revision"] else " (absent)"),
            tofile=change["path"] + " (selected)",
        ))
    except (UnicodeError, ValueError):
        return f"Complete file: {len(content)} bytes"


class ReviewSelection:
    """Immutable original plan plus deterministic per-file review choices."""

    def __init__(self, plan):
        verify_seal(plan)
        if plan.get("mode") != "live" or plan.get("review_selection"):
            raise ValueError("Select edits from an original live plan")
        self.original = copy.deepcopy(plan)
        self.choices = {}
        for change in plan["changes"]:
            formats = {r["format"] for r in plan.get("requirements", []) if r["target_path"] == change["path"]}
            format = formats.pop() if len(formats) == 1 else "text"
            choices = change_choices(change, format)
            before = base64.b64decode(change["before"])
            if _assemble(before, choices, {c.id for c in choices}) != base64.b64decode(change["after"]):
                raise ValueError("Review choices do not reproduce the proposed file")
            self.choices[change["path"]] = choices

    def all_selected(self):
        return {path: {c.id for c in choices} for path, choices in self.choices.items()}

    def plan_for(self, selected):
        if set(selected) != set(self.choices):
            raise ValueError("Review selection must cover exactly the planned files")
        plan = copy.deepcopy(self.original)
        plan["changes"] = []
        audit = []
        for change in self.original["changes"]:
            path = change["path"]
            choices, chosen = self.choices[path], set(selected[path])
            if not chosen.issubset(c.id for c in choices):
                raise ValueError("Unknown review choice for " + path)
            audit.append({
                "path": path,
                "included": [{"id": c.id, "label": c.label} for c in choices if c.id in chosen],
                "excluded": [{"id": c.id, "label": c.label} for c in choices if c.id not in chosen],
            })
            if not chosen:
                continue
            before = base64.b64decode(change["before"])
            content = _assemble(before, choices, chosen)
            if change["revision"] is not None and content == before:
                continue
            edit = copy.deepcopy(change)
            edit.update(after=base64.b64encode(content).decode("ascii"), after_sha256=digest(content), diff=_diff(change, content))
            plan["changes"].append(edit)
        plan["review_selection"] = {"source_plan_digest": self.original["digest"], "files": audit}
        return seal(plan)
