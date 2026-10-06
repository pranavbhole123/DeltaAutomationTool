"""Apply only an explicitly approved, unchanged plan to a pending changelist."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import re
from datetime import datetime, timezone

from .planner import digest, verify_seal


class ApprovalError(ValueError):
    pass


class ExecutionError(RuntimeError):
    pass


def spec_fingerprint(spec):
    return {k: v for k, v in spec.items() if k in ("Root", "AltRoots", "Stream", "StreamAtChange", "LineEnd", "Options", "Host", "Owner")
            or k.startswith("View") or k.startswith("AltRoots")}


def supported_type(file_type):
    if not re.fullmatch(r"(?:text|binary)(?:\+[A-Za-z0-9]+)?", file_type) or "+" in file_type and "k" in file_type.split("+", 1)[1]:
        raise ExecutionError(f"Unsupported file type for automatic writes: {file_type}")


def local_bytes(content, file_type, line_end):
    if file_type.startswith("binary"):
        return content
    content = content.replace(b"\r\n", b"\n")
    if line_end in ("win", "local") and (line_end == "win" or os.name == "nt"):
        return content.replace(b"\n", b"\r\n")
    if line_end == "mac":
        return content.replace(b"\n", b"\r")
    return content


def verify_workspace_path(local, workspace):
    path = Path(local)
    if not path.is_absolute():
        raise ExecutionError("Workspace path is not absolute")
    roots = [workspace.get("Root", "")]
    alt = workspace.get("AltRoots", [])
    roots += alt if isinstance(alt, list) else []
    roots += [v for k, v in workspace.items() if re.fullmatch(r"AltRoots\d+", k)]
    resolved = path.resolve()
    if not any(r and r != "null" and resolved.is_relative_to(Path(r).resolve()) and resolved != Path(r).resolve() for r in roots):
        raise ExecutionError(f"File is outside workspace roots: {local}")
    for parent in (path, *path.parents):
        if parent.is_symlink() or getattr(parent, "is_junction", lambda: False)():
            raise ExecutionError(f"Symlink/junction workspace path requires manual handling: {parent}")
    return path


def check_local(p4, change, workspace):
    path = change["path"]
    if p4.where(path) != change["local_path"]:
        raise ExecutionError(f"Workspace mapping changed: {path}")
    local = verify_workspace_path(change["local_path"], workspace)
    state = p4.fstat(path)
    if state.get("action") or state.get("otherOpen") or any(k.startswith("otherOpen") for k in state):
        raise ExecutionError(f"File is already open by you or another workspace: {path}")
    file_type = change["type"]
    supported_type(file_type)
    if change["revision"] is None:
        if local.exists():
            raise ExecutionError(f"Untracked/local file would be overwritten: {local}")
    elif local.exists():
        if not local.is_file():
            raise ExecutionError(f"Not a regular file: {local}")
        have = int(state.get("haveRev", 0))
        if have <= 0:
            raise ExecutionError(f"Local file has no have revision; will not overwrite: {local}")
        pristine = p4.read_file(path, have)
        expected = local_bytes(pristine, file_type, workspace.get("LineEnd", "local"))
        if local.read_bytes() != expected:
            raise ExecutionError(f"Local changes detected; preserve or shelve them first: {local}")
    elif state.get("haveRev"):
        raise ExecutionError(f"A previously synced file is missing locally: {local}")
    return local


def check_snapshot(p4, snapshot):
    records = p4.files(snapshot["path"])
    rev = int(records[0]["rev"]) if len(records) == 1 else None
    if len(records) > 1 or rev != snapshot["revision"]:
        raise ExecutionError(f"Depot revision changed since planning: {snapshot['path']}")
    if rev is not None:
        content = p4.read_file(snapshot["path"], rev)
        if digest(content) != snapshot["sha256"]:
            raise ExecutionError(f"Depot content changed since planning: {snapshot['path']}")


def execute(p4, plan, approval, *, acknowledge_reviews=False, acknowledge_blocked=False, journal_path=None):
    """No mutation occurs until all approval and preflight checks pass.

    Failure after mutation leaves the dedicated pending CL and journal intact.
    It never reverts user work, auto-submits, or changes workspace mappings.
    """
    verify_seal(plan)
    if plan.get("schema_version") != 1 or plan.get("mode") != "live":
        raise ApprovalError("Only a supported live plan can be applied; demo plans cannot be applied")
    if approval != plan["digest"]:
        raise ApprovalError("Approval must be the complete SHA-256 of the plan you reviewed")
    blocked = [{k: check[k] for k in ("rule", "title", "source", "message", "paths")}
               for check in plan["checks"] if check["status"] == "blocked"]
    if blocked and not acknowledge_blocked:
        raise ApprovalError("Blocked checks will be left untouched; explicitly acknowledge them before applying the remaining planned changes")
    if any(c["status"] == "review" for c in plan["checks"]) and not acknowledge_reviews:
        raise ApprovalError("Review and acknowledge all REVIEW items before applying")
    for key in ("port", "user", "client"):
        if getattr(p4, key) != plan["connection"][key]:
            raise ExecutionError(f"Perforce {key} differs from approved connection")
    identity = p4.identity()
    for key in ("serverAddress", "serverID", "userName", "clientName"):
        if identity.get(key) != plan["identity"].get(key):
            raise ExecutionError(f"Perforce identity changed: {key}")
    workspace = p4.workspace_spec()
    if spec_fingerprint(workspace) != spec_fingerprint(plan["workspace"]):
        raise ExecutionError("Workspace specification changed; generate a new plan")
    for spec in plan["templates"].values():
        actual = p4.client_spec(spec["Client"])
        if spec_fingerprint(actual) != spec_fingerprint(spec):
            raise ExecutionError(f"Template view changed: {spec['Client']}")
    snapshots = {s["path"]: s for s in plan["snapshots"]}
    if len(snapshots) != len(plan["snapshots"]):
        raise ExecutionError("Duplicate plan snapshots")
    for snapshot in snapshots.values():
        check_snapshot(p4, snapshot)
    prepared = []
    targets = set()
    for change in plan["changes"]:
        snapshot = snapshots.get(change["path"])
        if not snapshot or snapshot["revision"] != change["revision"] or snapshot["sha256"] != change["before_sha256"]:
            raise ExecutionError("Change does not match its before snapshot")
        content = base64.b64decode(change["after"], validate=True)
        if digest(content) != change["after_sha256"]:
            raise ExecutionError("Proposed content hash mismatch")
        local = check_local(p4, change, workspace)
        target = os.path.normcase(str(local.resolve()))
        if target in targets:
            raise ExecutionError("Two depot files resolve to the same workspace file")
        targets.add(target)
        prepared.append((change, local, local_bytes(content, change["type"], workspace.get("LineEnd", "local"))))
    if not prepared:
        return {"status": "no_changes", "plan": plan["digest"], "change": None, "files": [], "blocked": blocked}
    journal_path = Path(journal_path or ("execution-" + plan["digest"][:12] + ".json"))
    if journal_path.exists():
        raise ExecutionError(f"Execution journal already exists; inspect it before another attempt: {journal_path}")
    if os.path.normcase(str(journal_path.resolve())) in targets:
        raise ExecutionError("Journal must not overwrite a target file")
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    journal = {"plan": plan["digest"], "status": "preflight_passed", "change": None, "files": [],
               "blocked": blocked, "started_at": datetime.now(timezone.utc).isoformat(),
               "acknowledged_reviews": acknowledge_reviews, "acknowledged_blocked": acknowledge_blocked}
    def save():
        journal_path.write_text(json.dumps(journal, indent=2), encoding="utf-8")
    save()
    try:
        p4.enable_writes()
        cl = p4.create_change(f"[Title] SLSI Bluetooth delta for {plan['config']['model']}\n"
                              f"[Checklist] {plan['source']}\n[Approved plan] {plan['digest']}\n"
                              "[Validation] Review plan checks and perform listed device checks after build.")
        journal.update(change=cl, status="applying")
        save()
        for change, local, content in prepared:
            check_snapshot(p4, snapshots[change["path"]])
            check_local(p4, change, workspace)
            item = {"path": change["path"], "local_path": str(local), "status": "starting"}
            journal["files"].append(item)
            save()
            if change["revision"] is not None:
                p4.sync(change["path"], change["revision"])
                expected = local_bytes(base64.b64decode(change["before"]), change["type"], workspace.get("LineEnd", "local"))
                if not local.exists() or local.read_bytes() != expected:
                    raise ExecutionError(f"Synced content does not match approved snapshot: {local}")
                p4.edit(change["path"], cl)
                item["status"] = "opened_for_edit"
                save()
                if local.read_bytes() != expected:
                    raise ExecutionError(f"Local file changed after opening: {local}")
                local.write_bytes(content)
            else:
                local.parent.mkdir(parents=True, exist_ok=True)
                verify_workspace_path(str(local), workspace)
                with local.open("xb") as handle:
                    handle.write(content)
                item["status"] = "written_pending_add"
                save()
                p4.add(str(local), cl, file_type=change["type"])
            if local.read_bytes() != content:
                raise ExecutionError(f"Post-write verification failed: {local}")
            item["status"] = "applied"
            save()
        journal["status"] = "applied_pending_review"
        save()
        return journal
    except Exception as exc:
        journal.update(status="failed_partial" if journal["change"] else "failed", error=str(exc))
        save()
        raise ExecutionError(f"{exc}. Pending CL: {journal['change']}; journal: {journal_path}. "
                             "Inspect the recorded files before recovery; nothing was submitted or automatically reverted.") from exc
