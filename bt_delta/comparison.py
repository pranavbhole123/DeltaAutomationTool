"""Compare the blank reference/checklist plan with edits from selected changelists."""
from __future__ import annotations

import base64
from collections import Counter
import difflib
import json
from pathlib import Path
import re
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from .blank import BlankPlanner, plan_from_previews
from .config import validate
from .comparison_report import explain_file, render_comparison
from .diagnostics import emit
from .csc import CARRIER_FILENAME, model_root
from .executor import supported_type, verify_workspace_path
from .perforce import MappingError, PerforceError, PerforceTimeout, parse_view, _revision
from .planner import decode, digest, save_plan
from .resolver import relative_for
from .transforms import _ASSIGNMENT, _comment, _make_statements

DELETES = {"delete", "move/delete"}
ADDS = {"add", "branch", "move/add"}


def parse_changelists(values):
    """Accept a list or comma/whitespace separated numbers; remove duplicates."""
    values = re.split(r"[,\s]+", values.strip()) if isinstance(values, str) else values
    if isinstance(values, int):
        values = [values]
    numbers = list(dict.fromkeys(_revision(value) for value in values if value != ""))
    if not numbers:
        raise ValueError("Enter at least one numbered developer changelist")
    return numbers


def _unique_json(pairs):
    value = {}
    for key, child in pairs:
        if key in value:
            raise ValueError(f"Duplicate JSON key: {key}")
        value[key] = child
    return value


def content_units(path, content, file_type="text"):
    """Comparable statements/packages/values. Unchanged units are subtracted."""
    if content is None:
        return []
    units = []
    def add(identity, value, text, line=None):
        units.append({"identity": identity, "value": value, "text": text, "line": line})
    if not file_type.startswith("text"):
        add("binary", digest(content), f"Binary {len(content)} bytes; SHA-256 {digest(content)}")
        return units
    text, _ = decode(content.replace(b"\r\n", b"\n"))
    if path.lower().endswith(".json"):
        def visit(value, trail):
            if isinstance(value, dict) and value:
                for key, child in value.items():
                    visit(child, trail + "/" + key.replace("~", "~0").replace("/", "~1"))
            elif isinstance(value, list) and value:
                for index, child in enumerate(value):
                    visit(child, trail + f"/{index}")
            else:
                rendered = json.dumps(value, sort_keys=True, ensure_ascii=False)
                add("json:" + trail, rendered, f"{trail} = {rendered}")
        if text.strip():
            visit(json.loads(text, object_pairs_hook=_unique_json), "")
    elif path.lower().endswith(".xml"):
        def visit(element, trail):
            name = element.tag
            if name == "hal":
                name += "[" + (element.findtext("name") or "") + "]"
            here = trail + "/" + name
            for key, value in element.attrib.items():
                add("xml:" + here + "/@" + key, value, f"{here}/@{key} = {value}")
            if not len(element):
                value = (element.text or "").strip()
                add("xml:" + here, value, f"{here} = {value}")
            for child in element:
                visit(child, here)
        if text.strip():
            visit(ET.fromstring(text), "")
            for match in re.finditer(r"<!--(.*?)-->", text, re.S):
                comment = match[1].strip()
                add("xml-comment:" + comment, comment, "<!--" + comment + "-->")
    elif path.lower().endswith(".rc"):
        scope = ""
        for index, raw in enumerate(text.splitlines(), 1):
            line = raw.strip()
            if re.match(r"^(on|service)\s", line):
                scope = " ".join(line.split())
            elif line:
                canonical = " ".join(line.split())
                add("init:" + scope + ":" + canonical, canonical, (scope + " → " if scope else "") + line, index)
    elif path.lower().endswith((".mk", ".common")) or "SecProductFeature" in path:
        conditions = []
        for start, end, logical, depth in _make_statements(text):
            line = " ".join(logical.split())
            if re.match(r"^(ifeq|ifneq|ifdef|ifndef)\b", line):
                conditions.append(line)
                continue
            if line == "endif":
                if conditions:
                    conditions.pop()
                continue
            if re.match(r"^else\b", line):
                if conditions:
                    conditions[-1] += " / " + line
                continue
            scope = " / ".join(conditions)
            assignment = _ASSIGNMENT.match(line)
            if assignment and assignment["key"] == "PRODUCT_PACKAGES":
                for package in assignment["value"].split():
                    add("package:" + scope + ":" + package, package, "PRODUCT_PACKAGES: " + package, start + 1)
            elif assignment:
                value = assignment["operator"] + " " + assignment["value"].strip()
                add("make:" + scope + ":" + assignment["key"], value,
                    (scope + " → " if scope else "") + assignment["key"] + " " + value, start + 1)
            elif line:
                add("make-line:" + scope + ":" + line, line, (scope + " → " if scope else "") + line, start + 1)
        for index, raw in enumerate(text.splitlines(), 1):
            _, comment = _comment(raw)
            if comment:
                add("comment:" + comment.strip(), comment.strip(), comment.strip(), index)
    else:
        for index, raw in enumerate(text.splitlines(), 1):
            if raw.strip():
                add("line:" + raw.strip(), raw.strip(), raw.strip(), index)
    return units


def _signature(unit):
    return unit["identity"], unit["value"]


def _difference(left, right):
    remaining = Counter(_signature(unit) for unit in right)
    output = []
    for unit in left:
        key = _signature(unit)
        if remaining[key]:
            remaining[key] -= 1
        else:
            output.append(dict(unit))
    return output


def _diff(path, before, after, file_type):
    if not file_type.startswith("text"):
        return "\n".join(f"{label}: " + ("absent" if value is None else f"{len(value)} bytes; SHA-256 {digest(value)}")
                         for label, value in (("before", before), ("after", after))) + "\n"
    old, _ = decode((before or b"").replace(b"\r\n", b"\n"))
    new, _ = decode((after or b"").replace(b"\r\n", b"\n"))
    return "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                      fromfile=path + " (CL before)", tofile=path + " (CL after)"))


def _load_developer(p4, number, source):
    change = p4.describe_change(number)
    if source == "auto":
        if change["status"] == "submitted":
            source = "submitted"
        else:
            shelf = p4.describe_change(number, shelved=True)
            source = "shelved" if shelf["files"] else "workspace"
    if (source == "submitted") != (change["status"] == "submitted"):
        raise ValueError(f"CL {number} status does not match the selected content source")
    if source == "shelved":
        change = p4.describe_change(number, shelved=True)
        if not change["files"]:
            raise ValueError(f"CL {number} has no shelved files")
    if source == "workspace" and change["client"] != p4.client:
        raise ValueError(f"CL {number}: unshelved files require the configured developer local workspace; shelve remote work first")
    return {**change, "content_source": source}


def _read_delta(p4, change, entry):
    path, action, source = entry["path"], entry["action"], change["content_source"]
    number = change["number"]
    local = None
    if source == "workspace":
        state = p4.fstat(path)
        if str(state.get("change")) != number or state.get("action") != action:
            raise PerforceError(f"File is no longer open in CL {number}: {path}")
        file_type = state.get("type") or entry["type"] or "text"
        supported_type(file_type)
        revision = int(state.get("haveRev", 0))
        if action not in ADDS and revision <= 0:
            raise PerforceError(f"Cannot identify the local file's have revision: {path}")
        before = p4.read_file(path, revision) if action not in ADDS else None
        local = verify_workspace_path(p4.where(path), p4.workspace_spec())
        after = None if action in DELETES else local.read_bytes()
        before_source = f"{path}#{revision} (workspace have revision)"
        after_source = f"{local} (workspace CL {number})"
    elif source == "submitted":
        revision = entry["revision"]
        file_type = entry["type"] or "text"
        before = p4.read_file(path, revision - 1) if action not in ADDS and revision > 1 else None
        after = None if action in DELETES else p4.read_file(path, revision)
        before_source = f"{path}#{revision - 1}"
        after_source = f"{path}#{revision} (submitted CL {number})"
    else:
        records = p4.files(path)
        if len(records) > 1:
            raise PerforceError(f"Ambiguous depot head for shelved file: {path}")
        record = records[0] if records else {}
        file_type = entry["type"] or record.get("type", "text")
        before = p4.read_file(path, record["rev"]) if record and action not in ADDS else None
        after = None if action in DELETES else p4.read_shelved_file(path, number)
        before_source = f"{path}#{record.get('rev', '?')} (depot head at comparison)"
        after_source = f"{path} (shelf CL {number})"
    if before is None:
        before_source = "File absent before this action"
    if after is None:
        after_source = "File absent after this action"
    additions, removals, unchanged = [], [], []
    error = ""
    try:
        old, new = content_units(path, before, file_type), content_units(path, after, file_type)
        additions, removals = _difference(new, old), _difference(old, new)
        unchanged = _difference(new, additions)
    except Exception as exc:
        error = "File content was read, but statement/value extraction failed: " + str(exc)
    try:
        raw_diff = _diff(path, before, after, file_type)
    except UnicodeError as exc:
        raw_diff = "Text diff unavailable: " + str(exc)
        error = error or raw_diff
    for operation, units in (("add", additions), ("remove", removals)):
        for unit in units:
            unit.update(operation=operation, changelists=[number])
    return {**entry, "changelist": number, "content_source": source,
            "additions": additions, "removals": removals, "_unchanged": unchanged, "diff": raw_diff,
            "before_source": before_source, "after_source": after_source,
            "before_sha256": digest(before) if before is not None else None,
            "before_bytes": len(before) if before is not None else None,
            "after_bytes": len(after) if after is not None else None,
            "comparison_type": file_type,
            "after_sha256": digest(after) if after is not None else None,
            "exists_after": after is not None, "local": local, "error": error}


def _combine(deltas, history=None):
    """Combine selected edits, cancelling additions later removed by another CL."""
    additions, removals = [], []
    for delta in deltas:
        for incoming, target, opposite in ((delta["removals"], removals, additions),
                                           (delta["additions"], additions, removals)):
            for unit in incoming:
                matched = next((index for index, other in enumerate(opposite)
                                if _signature(other) == _signature(unit)), None)
                if matched is not None:
                    earlier = opposite.pop(matched)
                    if history is not None:
                        history.append({"earlier": earlier, "later": dict(unit)})
                else:
                    target.append(dict(unit))
    return additions, removals


def compare_changelist(p4, config, numbers, *, source="auto", catalog=None, preview_plan=None):
    numbers = parse_changelists(numbers)
    run_id = uuid.uuid4().hex[:8]
    def log(message):
        emit(p4, f"[comparison {run_id}] {message}")
    log(f"START: CLs={', '.join(numbers)}; requested source={source}; model={config.get('model', 'infer from templates')}.")
    if source not in ("auto", "submitted", "shelved", "workspace"):
        raise ValueError("Unsupported changelist content source")
    changes = []
    for number in numbers:
        change = _load_developer(p4, number, source)
        changes.append(change)
        log(f"CL {number}: status={change['status']}; resolved content source={change['content_source']}; "
            f"developer={change['user']}@{change['client']}; {len(change['files'])} file(s).")
    if preview_plan is not None:
        supplied = validate(config)
        for key, value in supplied.items():
            if value not in (None, "", []) and value != preview_plan["config"].get(key):
                raise ValueError("Tab 4 preview inputs differ from current inputs; regenerate the preview before comparing.")
        source_plan = preview_plan
        log(f"Using supplied tab 4 previews: {len(source_plan['previews'])} entries; plan digest={source_plan['digest']}.")
    else:
        log("Generating reference-led empty-file previews for the comparison baseline.")
        source_plan = BlankPlanner(p4, config, catalog).build()
    plan = plan_from_previews(source_plan)
    planned = {entry["path"]: entry for entry in plan["changes"]}
    previews_by_path = {}
    snapshots = {entry['path']: entry for entry in plan.get('snapshots', [])}
    for preview in plan['previews']:
        previews_by_path.setdefault(preview['target_path'], []).append(preview)
    log(f"Baseline ready: {len(planned)} target file(s); {len(plan['previews'])} preview entries.")
    developer, pending_reads = {}, []
    warnings = []
    if not plan["config"]["check_csc_features"]:
        warnings.append("CSC feature checks are disabled; " + CARRIER_FILENAME + " changelist entries are excluded.")
    if not planned:
        warnings.append("Tab 4 has no preview entries. The comparison baseline is unavailable; inspect the rule results below.")
    for order, change in enumerate(changes):
        if change["content_source"] == "shelved":
            warnings.append(f"CL {change['number']}: shelved edits are extracted against depot head; only shelved files are included.")
        for entry in change["files"]:
            if not plan["config"]["check_csc_features"] and entry["path"].rsplit("/", 1)[-1] == CARRIER_FILENAME:
                warnings.append(f"CL {change['number']}: CSC feature file skipped: {entry['path']}")
                log(warnings[-1])
                continue
            try:
                log(f"Reading CL {change['number']} file: {entry['path']}; action={entry['action']}; type={entry['type']}.")
                delta = _read_delta(p4, change, entry)
                if change["content_source"] != "submitted" and delta["exists_after"]:
                    pending_reads.append((delta["local"], entry["path"], change["number"], delta["after_sha256"]))
                delta.pop("local")
                log(f"CL {change['number']} comparison sources: BEFORE {delta['before_source']}; AFTER {delta['after_source']}.")
                log(f"CL {change['number']} extracted edits: {len(delta['additions'])} additions/new values; {len(delta['removals'])} removals/old values; error={delta['error'] or 'none'}.")
                for operation in ('additions', 'removals'):
                    for unit in delta[operation]:
                        log(f"CL {change['number']} {operation.upper()} {entry['path']} line {unit.get('line') or 'n/a'}: {unit['text']}")
            except PerforceTimeout:
                raise
            except Exception as exc:
                delta = {**entry, "changelist": change["number"], "content_source": change["content_source"],
                         "additions": [], "removals": [], "diff": "", "error": str(exc)}
                log(f"ERROR reading CL {change['number']} {entry['path']}: {exc}")
            delta["input_order"] = order
            developer.setdefault(entry["path"], []).append(delta)
    views, mapping_errors = {}, []
    for key, spec in plan["templates"].items():
        if key.startswith("current."):
            try:
                views[key] = parse_view(spec)
            except MappingError as exc:
                mapping_errors.append(f"{key}: {exc}")
    results = []
    try:
        csc_path = plan["config"]["current"].get("csc_path", "")
        csc_root = model_root(csc_path, plan["config"]["model"]) + "/" if plan["config"]["check_csc_features"] and csc_path else None
    except ValueError as exc:
        csc_root = None
        mapping_errors.append(str(exc))
    for path in sorted(planned.keys() | developer.keys()):
        wanted = planned.get(path)
        deltas = sorted(developer.get(path, []), key=lambda delta: (
            delta["content_source"] != "submitted", delta["revision"] if delta["content_source"] == "submitted" else delta["input_order"]))
        item = {"path": path, "filename": path.rsplit("/", 1)[-1], "planned": wanted is not None,
                "in_changelists": bool(deltas), "changelists": list(dict.fromkeys(delta["changelist"] for delta in deltas)),
                "rules": wanted["rules"] if wanted else [], "template_paths": [],
                "missing": [], "extra": [], "matched": [], "developer_changes": deltas, "errors": [],
                "suggested": [], "preview_sources": previews_by_path.get(path, []),
                "combined_additions": [], "developer_removals": [], "cancelled_edits": [], "findings": []}
        log(f"FILE START {path}: in preview={wanted is not None}; CL order=" + (', '.join(delta['changelist'] for delta in deltas) or 'file absent from selected CLs'))
        for key, view in views.items():
            try:
                item["template_paths"].append(f"{key}: {relative_for(view, path)}")
            except MappingError:
                pass
        if csc_root and path.startswith(csc_root):
            item["template_paths"].append("current.csc: " + path[len(csc_root):])
        item["outside_templates"] = not item["template_paths"]
        item["extra_file"] = bool(deltas and not wanted)
        item["errors"] = [f"CL {delta['changelist']}: {delta['error']}" for delta in deltas if delta["error"]]
        if not planned:
            item["extra_file"] = False
            item["errors"].append("No tab-4 preview baseline is available; this file cannot be classified.")
        try:
            expected, seen = [], {}
            for snippet in wanted["preview_contents"] if wanted else []:
                preview = next((entry for entry in item['preview_sources'] if entry['rule'] == snippet['rule']), {})
                for unit in content_units(path, base64.b64decode(snippet["content"]), snippet["type"]):
                    signature = _signature(unit)
                    provenance = {"rule": snippet['rule'], "title": preview.get('title', ''),
                                  "worksheet": preview.get('source', ''), "reference_path": preview.get('reference_path'),
                                  "reference_revision": snapshots.get(preview.get('reference_path'), {}).get('revision'),
                                  "preview_line": unit.get('line')}
                    if signature not in seen:
                        unit['sources'] = []
                        expected.append(unit)
                        seen[signature] = unit
                    if provenance not in seen[signature]['sources']:
                        seen[signature]['sources'].append(provenance)
            item['suggested'] = expected
            for unit in expected:
                log(f"TOOL SUGGESTS {path}: {unit['text']}; rules=" + ', '.join(origin['rule'] for origin in unit['sources']))
            additions, removals = _combine([delta for delta in deltas if not delta["error"]], item['cancelled_edits'])
            item['combined_additions'] = additions
            signatures = {_signature(unit) for unit in expected}
            for delta in deltas:
                delta['unchanged_suggestions'] = [unit for unit in delta.pop('_unchanged', []) if _signature(unit) in signatures]
            item["missing"] = _difference(expected, additions)
            item["extra"] = _difference(additions, expected)
            remaining = list(additions)
            for unit in expected:
                index = next((index for index, other in enumerate(remaining) if _signature(other) == _signature(unit)), None)
                if index is not None:
                    matched = dict(remaining.pop(index))
                    matched['sources'] = unit['sources']
                    item["matched"].append(matched)
            expected_ids = {unit["identity"] for unit in expected}
            added_ids = {unit["identity"] for unit in additions}
            item["extra"].extend(unit for unit in removals if unit["identity"] not in expected_ids or unit["identity"] not in added_ids)
            item["developer_removals"] = removals
            if wanted and not expected and not item["errors"] and (not deltas or not deltas[-1].get("exists_after")):
                item["missing"].append({"text": "Blank plan includes this empty file; it was not supplied by the selected changelists."})
            for delta in deltas:
                if delta["action"] not in ("edit", "add"):
                    item["extra"].append({"operation": "action", "text": "Developer action: " + delta["action"], "changelists": [delta["changelist"]]})
            item["blank_content"] = wanted["diff"] if wanted else ""
        except Exception as exc:
            item["errors"].append("Blank content comparison: " + str(exc))
        if item["errors"]:
            item["matched"] = []
            item["missing"] = []
            item["extra"] = []
        for delta in deltas:
            delta.pop('_unchanged', None)
        explain_file(item)
        for finding in item['findings']:
            log(f"{finding['status'].upper()} {path}: {finding['text']}; reason={finding['reason']}")
        log(f"FILE END {path}: suggested={len(item['suggested'])}; matched={len(item['matched'])}; missing={len(item['missing'])}; extra={len(item['extra'])}; errors={len(item['errors'])}.")
        results.append(item)
    for change in changes:
        log(f"Rechecking CL {change['number']} metadata for changes during comparison.")
        current = p4.describe_change(change["number"], shelved=change["content_source"] == "shelved")
        if current != {key: value for key, value in change.items() if key != "content_source"}:
            raise PerforceError(f"CL {change['number']} changed during comparison; generate a fresh report")
    for local, path, number, expected_hash in pending_reads:
        log(f"Rechecking pending CL {number} content hash: {path}")
        content = local.read_bytes() if local is not None else p4.read_shelved_file(path, number)
        if digest(content) != expected_hash:
            raise PerforceError(f"CL {number} file content changed during comparison: {path}")
    counts = {"changelists": len(changes), "blank_plan_files": len(planned),
              "preview_rules": len({preview["rule"] for preview in plan["previews"]}), "developer_files": len(developer),
              "missing_from_changelists": sum(bool(item["missing"]) for item in results),
              "extra_files": sum(item["extra_file"] for item in results),
              "extra_changes": sum(bool(item["extra"]) for item in results),
              "matched_items": sum(len(item["matched"]) for item in results),
              "unreadable_files": sum(bool(item["errors"]) for item in results)}
    counts.update(suggested_items=sum(len(item['suggested']) for item in results),
                  missing_items=sum(len(item['missing']) for item in results),
                  extra_items=sum(len(item['extra']) for item in results),
                  value_differences=sum(finding['status'] == 'different_value' for item in results for finding in item['findings']))
    log("END: " + json.dumps(counts))
    return {"schema_version": 3, "run_id": run_id, "created_at": datetime.now(timezone.utc).isoformat(), "changelists": changes,
            "comparison_basis": "Tab 4 Empty-file preview versus changes introduced by the selected changelists.",
            "warnings": warnings + mapping_errors, "counts": counts, "files": results, "plan": plan,
            "incomplete": bool(not planned or mapping_errors or counts["unreadable_files"] or any(check["status"] == "blocked" for check in plan["checks"]))}


def comparison_summary(report):
    return render_comparison(report)


def save_comparison(report, directory):
    directory = Path(directory)
    save_plan(report["plan"], directory / "blank-plan")
    (directory / "comparison.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    output = directory / "comparison.txt"
    output.write_text(comparison_summary(report), encoding="utf-8")
    return output
