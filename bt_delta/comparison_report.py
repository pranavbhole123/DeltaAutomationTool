"""Explain tab-4 suggestions and developer edits without changing comparison rules."""


def signature(unit):
    return unit.get("identity"), unit.get("value")


def origins(unit):
    parts = []
    if unit.get("changelists"):
        parts.append("CL " + ", ".join(unit["changelists"]))
    if unit.get("line"):
        parts.append("line " + str(unit["line"]))
    if unit.get("sources"):
        parts.append(
            "rules "
            + ", ".join(dict.fromkeys(source["rule"] for source in unit["sources"]))
        )
    if unit.get("depot_source"):
        parts.append("depot " + unit["depot_source"])
    return "; ".join(parts)


def explain_file(item):
    """Add reasons while keeping existing missing/extra/matched lists intact."""
    findings = item["findings"] = []
    if item["errors"]:
        for error in item["errors"]:
            findings.append(
                {
                    "status": "unreadable",
                    "text": item["filename"],
                    "reason": error,
                    "suggested": None,
                    "actual": [],
                }
            )
        return
    for wanted in item["missing"]:
        if wanted.get("desired") == "absent":
            findings.append(
                {
                    "status": "missing",
                    "text": wanted["text"],
                    "reason": "Required removal is not satisfied: selected content remains in "
                    + item["latest"]["source"],
                    "suggested": wanted,
                    "actual": [],
                }
            )
            continue
        actual = [
            unit
            for unit in item["combined_additions"]
            if wanted.get("identity")
            and unit["identity"] == wanted["identity"]
            and unit["value"] != wanted["value"]
        ]
        latest_values = [
            unit
            for unit in item["latest_suggestions"]
            if wanted.get("identity")
            and unit["identity"] == wanted["identity"]
            and unit["value"] != wanted["value"]
        ]
        actual.extend(latest_values)
        status = "different_value" if actual else "missing"
        if actual:
            reason = "A different value exists for the same setting/key and context in the selected edits or latest depot revision; the evidence below identifies its source."
        elif not item["in_changelists"]:
            reason = "This file does not appear in any selected changelist."
        elif any(
            signature(wanted) == signature(unit) for unit in item["developer_removals"]
        ):
            actual = [
                unit
                for unit in item["developer_removals"]
                if signature(wanted) == signature(unit)
            ]
            reason = "The selected changelists remove this suggested content instead of introducing it."
        else:
            cancelled = [
                event
                for event in item["cancelled_edits"]
                if event["earlier"]["operation"] == "add"
                and signature(event["earlier"]) == signature(wanted)
            ]
            unchanged = [
                (delta["changelist"], unit)
                for delta in item["developer_changes"]
                for unit in delta.get("unchanged_suggestions", [])
                if signature(unit) == signature(wanted)
            ]
            if cancelled:
                reason = "An earlier selected changelist added this content, but a later selected changelist removed it; it does not survive in the combined additions."
                actual = [
                    unit
                    for event in cancelled
                    for unit in (event["earlier"], event["later"])
                ]
            elif unchanged:
                reason = (
                    "This content was unchanged in the historical CL "
                    + ", ".join(dict.fromkeys(number for number, unit in unchanged))
                    + ", but is absent from the latest depot revision."
                )
                actual = [
                    dict(unit, operation="unchanged", changelists=[number])
                    for number, unit in unchanged
                ]
            else:
                reason = "No surviving addition from the selected changelists matches this preview statement/value in the same file and context."
        reason += (
            " Missing: no surviving selected addition matches, and the suggestion is absent from "
            + item["latest"]["source"]
            + "."
        )
        findings.append(
            {
                "status": status,
                "text": wanted["text"],
                "reason": reason,
                "suggested": wanted,
                "actual": actual,
            }
        )
    for unit in item["matched"]:
        findings.append(
            {
                "status": "matched",
                "text": unit["text"],
                "reason": (
                    "A selected removal satisfies the required absence: "
                    if unit.get("desired") == "absent"
                    else "A surviving addition matches the preview statement/value in the same file and context: "
                )
                + origins(unit),
                "suggested": unit,
                "actual": [unit],
            }
        )
    for unit in item["already_present"]:
        suggested = unit.get("suggested", unit)
        reason = (
            "The suggestion is already present in the latest depot revision checked: "
            + unit["depot_source"]
            + ". It does not need to be introduced by a selected changelist."
        )
        if unit.get("desired") == "absent":
            reason = (
                "The selected content is already absent in "
                + unit["depot_source"]
                + "."
            )
        if unit.get("semantic_match"):
            reason = unit["semantic_match"] + " Evidence: " + unit["depot_source"] + "."
        elif signature(suggested) != signature(unit):
            reason += " A file-wide search found the same init command and arguments; its actual event and line are shown below. Event equivalence is not assessed by this presence check."
        findings.append(
            {
                "status": "already_present",
                "text": suggested["text"],
                "reason": reason,
                "suggested": suggested,
                "actual": [unit],
            }
        )
    for unit in item["extra"]:
        if unit.get("operation") == "action":
            reason = "This file operation is not represented by an add/edit suggestion in the tab-4 preview."
        elif unit.get("operation") == "remove":
            reason = "The developer removed this content; the removal is not covered by a matching preview replacement."
        elif item["extra_file"]:
            reason = "This file has no tab-4 content suggestion. Its developer additions are outside the generated preview baseline. Inspect related skipped/manual rule results before deciding whether to extend a rule."
        else:
            reason = "The developer introduced this statement/value, but no tab-4 suggestion matches it in this file and context."
        findings.append(
            {
                "status": "extra",
                "text": unit["text"],
                "reason": reason,
                "suggested": None,
                "actual": [unit],
            }
        )
    if not findings:
        findings.append(
            {
                "status": "no_content_difference",
                "text": item["filename"],
                "reason": "No comparable additions/removals differ from the preview baseline. Original file operations and raw diffs are shown above.",
                "suggested": None,
                "actual": [],
            }
        )


def render_comparison(report):
    counts = report["counts"]
    lines = [
        "Tab 4 Empty-file preview versus developer changelists: "
        + ", ".join(change["number"] for change in report["changelists"]),
        report["comparison_basis"],
        "Comparison run: " + report.get("run_id", "saved report"),
        "Rules: "
        + report["plan"].get("title", report["plan"].get("rules_id", "saved plan")),
        "Read-only. The baseline includes tab-4 content and declared removal requirements, including files needing no edit in the regular plan.",
        "MATCHED means covered by selected edits. ALREADY PRESENT means found in the latest depot revision checked, even when absent from the selected edits.",
        "MISSING additions are absent from both selected edits and latest depot content. A MISSING removal means selected content still remains in the latest file. Read failures are errors.",
        "Submitted CLs are combined in file-revision order; pending CLs follow in input order. Other edits are excluded from CL attribution but may satisfy suggestions through latest depot content.",
        "Formatting normalization retains Make/init context and XML/JSON key paths. Binary comparisons use byte hashes.",
        "When latest_scope is file, init commands are checked throughout the latest file, regardless of event or position.",
        (
            "INCOMPLETE: inspect file errors and blocked preview-source rules."
            if report["incomplete"]
            else "Comparison completed."
        ),
        f"Files: {counts['blank_plan_files']} suggested by tool (after exclusions); {counts['developer_files']} in compared CLs; {counts['extra_files']} extra; {counts['missing_files']} with missing suggestions; {counts['unreadable_files']} with errors.",
        f"Items: {counts.get('suggested_items', 0)} suggested; {counts['matched_items']} matched; {counts['already_present_items']} already present; {counts.get('missing_items', 0)} missing from selected edits and latest depot; {counts.get('extra_items', 0)} extra; {counts.get('value_differences', 0)} different values.",
    ]
    for name, template in report["plan"].get("templates", {}).items():
        lines.append("Source " + name + ": " + template.get("Client", "saved template"))
    lines.extend(["", "SELECTED CHANGELISTS"])
    for change in report["changelists"]:
        lines.extend(
            [
                f"CL {change['number']}: {change['status']}; source={change['content_source']}; developer={change['user']}@{change['client']}; {len(change['files'])} described file(s).",
                "Description: " + change["description"].strip(),
            ]
        )
    lines.extend("NOTE: " + warning for warning in report["warnings"])
    lines.extend(["", "RESULT INDEX (a file can appear in more than one category)"])
    for label, selected in (
        ("EXTRA FILES", lambda item: item["extra_file"]),
        ("EXTRA CHANGES", lambda item: bool(item["extra"]) and not item["extra_file"]),
        (
            "MISSING FROM SELECTED EDITS AND LATEST DEPOT",
            lambda item: bool(item["missing"]),
        ),
        ("MATCHED", lambda item: bool(item["matched"])),
        ("ALREADY PRESENT IN LATEST DEPOT", lambda item: bool(item["already_present"])),
        ("UNREADABLE", lambda item: bool(item["errors"])),
    ):
        matches = [item["path"] for item in report["files"] if selected(item)]
        lines.append(label + ": " + str(len(matches)) + " file(s)")
        lines.extend("  " + path for path in matches)
        if not matches:
            lines.append("  None.")
    lines.extend(
        ["", "=" * 80, "TAB 4 EMPTY-FILE PREVIEW BASELINE AND FILE-BY-FILE COMPARISON"]
    )
    if not report["plan"]["previews"]:
        lines.append(
            "No preview entries were generated. Inspect the Empty-file preview tab or saved blank-plan report for rule results."
        )

    def units(title, entries, prefix):
        lines.append(title + f" ({len(entries)} item(s))")
        if not entries:
            lines.append("  None.")
        for unit in entries:
            source = origins(unit)
            lines.append(
                prefix + " " + unit["text"] + (" [" + source + "]" if source else "")
            )

    for index, item in enumerate(report["files"], 1):
        lines.extend(
            [
                "",
                "=" * 80,
                f"FILE {index}/{len(report['files'])}: {item['path']}",
                "Filename: " + item["filename"],
                "Preview rules: " + (", ".join(item["rules"]) or "none"),
                "Developer CLs: "
                + (
                    ", ".join(item["changelists"])
                    or "file absent from selected changelists"
                ),
                "Template locations:",
            ]
        )
        lines.extend("  " + path for path in item["template_paths"])
        if item["outside_templates"]:
            lines.append(
                "  Outside configured current sources; included for completeness."
            )
        if item["extra_file"]:
            lines.append(
                "FILE RESULT: extra file — this path has no content suggestion in tab 4."
            )
        lines.extend(["", "1. WHAT THE TOOL REQUIRES"])
        if not item["preview_sources"]:
            lines.append(
                "No content preview exists for this file. The tool makes no content suggestion here."
            )
        for preview in item["preview_sources"]:
            lines.extend(
                [
                    f"Rule: {preview['rule']} — {preview['title']} ({preview['source']})",
                    "Target: " + preview["target_path"],
                    "Reference: "
                    + (preview.get("reference_path") or "no reference path recorded"),
                    "Reason / scope: "
                    + (
                        preview.get("note")
                        or "Selected reference content for an empty destination."
                    ),
                    "Exact tab-4 preview content:",
                    preview["content"],
                ]
            )
        units("Comparable suggestions", item["suggested"], "SUGGEST ADD:")
        for unit in item["suggested"]:
            for source in unit.get("sources", []):
                reference = source.get("reference_path") or "not recorded"
                if source.get("reference_revision") is not None:
                    reference += "#" + str(source["reference_revision"])
                lines.append(
                    f"  Source for {unit['text']}: rule={source['rule']}; worksheet={source['worksheet']}; reference={reference}; preview line={source.get('preview_line') or 'n/a'}"
                )
        lines.extend(["", "2. WHAT EACH CHANGELIST ACTUALLY CHANGED"])
        if not item["developer_changes"]:
            lines.append("None: this file is absent from all selected changelists.")
        for delta in item["developer_changes"]:
            lines.extend(
                [
                    "",
                    f"CL {delta['changelist']}: action={delta['action']}; source={delta['content_source']}; type={delta.get('comparison_type', delta['type'])}",
                    "Before: " + delta.get("before_source", "unavailable — see error"),
                    "After: " + delta.get("after_source", "unavailable — see error"),
                ]
            )
            if "before_sha256" in delta:
                lines.append(
                    f"Before bytes/hash: {delta['before_bytes']} / {delta['before_sha256']}; after bytes/hash: {delta['after_bytes']} / {delta['after_sha256']}"
                )
            if delta["error"]:
                lines.append("ERROR: " + delta["error"])
            units(
                "Added content / new values (line numbers refer to after)",
                delta["additions"],
                "+ ADD:",
            )
            units(
                "Removed content / old values (line numbers refer to before)",
                delta["removals"],
                "- REMOVE:",
            )
            units(
                "Suggested content already present and unchanged in this edit",
                delta.get("unchanged_suggestions", []),
                "UNCHANGED:",
            )
            lines.extend(
                [
                    "Original before/after diff:",
                    delta["diff"]
                    or "(No text diff, or content could not be read; see metadata/errors above.)",
                ]
            )
        lines.extend(["", "3. COMBINED EFFECT OF SELECTED CHANGELISTS"])
        units(
            "Surviving additions / new values", item["combined_additions"], "+ NET ADD:"
        )
        units(
            "Surviving removals / old values",
            item["developer_removals"],
            "- NET REMOVE:",
        )
        lines.append("Edits cancelled by later selected changelists:")
        if not item["cancelled_edits"]:
            lines.append("  None.")
        for event in item["cancelled_edits"]:
            earlier, later = event["earlier"], event["later"]
            lines.append(
                f"  {earlier['text']}: {earlier['operation'].upper()} [{origins(earlier)}] cancelled by {later['operation'].upper()} [{origins(later)}]."
            )
        lines.extend(["", "4. SUGGESTION VERSUS DEVELOPER RESULT, WITH REASONS"])
        if item["latest"]:
            lines.append(
                "Latest depot checked for uncovered suggestions: "
                + item["latest"]["source"]
            )
        elif item["planned"] and not item["errors"]:
            lines.append(
                "Latest depot check not needed: selected additions cover the suggestions."
            )
        for finding in item["findings"]:
            lines.append(f"[{finding['status'].upper()}] {finding['text']}")
            if finding["suggested"]:
                lines.append("  Tool suggests: " + finding["suggested"]["text"])
            if finding["actual"]:
                for actual in finding["actual"]:
                    label = (
                        "  Latest depot"
                        if actual.get("depot_source")
                        else "  Developer " + actual.get("operation", "change")
                    )
                    lines.append(
                        label + ": " + actual["text"] + " [" + origins(actual) + "]"
                    )
            else:
                lines.append(
                    "  Matching developer addition: none, or unavailable — see reason."
                )
            lines.append("  Reason: " + finding["reason"])
        if item["errors"]:
            lines.append(
                "Classification is incomplete; successful reads above are evidence only, not complete match/missing/extra findings."
            )
        units("MATCHED suggestions", item["matched"], "MATCH:")
        units(
            "ALREADY PRESENT IN LATEST DEPOT suggestions",
            item["already_present"],
            "PRESENT:",
        )
        units(
            "MISSING FROM SELECTED EDITS AND LATEST DEPOT suggestions",
            item["missing"],
            "MISSING:",
        )
        units("EXTRA developer changes", item["extra"], "EXTRA:")
    return "\n".join(lines).rstrip() + "\n"
