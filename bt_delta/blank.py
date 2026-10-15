"""Generate checklist/reference content as if each destination file were blank."""

from __future__ import annotations

import base64
import copy
import difflib
from .planner import Planner, decode, digest, seal, verify_seal


def plan_from_previews(source_plan):
    """Use the actual tab-4 preview entries, irrespective of proposed edits."""
    verify_seal(source_plan)
    plan = copy.deepcopy(source_plan)
    plan["mode"] = "comparison"
    plan["comparison_basis"] = (
        "Tab 4 Empty-file preview content, grouped by target filename and rule."
    )
    grouped = {}
    snapshots = {item["path"]: item for item in source_plan.get("snapshots", [])}
    for preview in source_plan.get("previews", []):
        path = preview["target_path"]
        content = preview.get("content_base64")
        file_type = preview.get("file_type", "text")
        if content is None:
            source = snapshots.get(preview.get("reference_path"), {})
            if (
                not source.get("type", "text").startswith("text")
                and "content" in source
            ):
                content, file_type = source["content"], source["type"]
            else:
                content = base64.b64encode(preview["content"].encode("utf-8")).decode(
                    "ascii"
                )
        entry = grouped.setdefault(
            path,
            {
                "path": path,
                "local_path": "",
                "revision": None,
                "type": file_type,
                "before": "",
                "before_sha256": None,
                "rules": [],
                "sources": [],
                "preview_contents": [],
                "requirements": [],
            },
        )
        if preview["rule"] not in entry["rules"]:
            entry["rules"].append(preview["rule"])
        if preview["source"] not in entry["sources"]:
            entry["sources"].append(preview["source"])
        entry["preview_contents"].append(
            {
                "rule": preview["rule"],
                "content": content,
                "type": file_type,
                "format": preview.get("format"),
                "desired": preview.get("desired", "present"),
                "comparison": preview.get("comparison", {}),
            }
        )
        entry["requirements"] = [
            r for r in plan.get("requirements", []) if r["target_path"] == path
        ]
    for entry in grouped.values():
        snippets = list(
            dict.fromkeys(item["content"] for item in entry["preview_contents"])
        )
        raw = [base64.b64decode(content) for content in snippets]
        content = (
            b"".join(
                part + (b"\n" if part and not part.endswith(b"\n") else b"")
                for part in raw
            )
            if len(raw) > 1
            else raw[0]
        )
        entry.update(
            after=base64.b64encode(content).decode("ascii"),
            after_sha256=digest(content),
        )
        try:
            text, _ = decode(content)
            entry["diff"] = "".join(
                difflib.unified_diff(
                    [],
                    text.splitlines(True),
                    fromfile=entry["path"] + " (empty)",
                    tofile=entry["path"] + " (tab 4 preview)",
                )
            )
        except UnicodeDecodeError:
            entry["diff"] = (
                f"Binary preview: {len(content)} bytes; SHA-256 {digest(content)}"
            )
    plan["changes"] = list(grouped.values())
    return seal(plan)


class BlankPlanner(Planner):
    """The shared engine selects the same requirements without reading target content."""

    def __init__(self, p4, config, catalog=None):
        super().__init__(p4, config, catalog, blank=True)
