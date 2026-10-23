"""Read-only planning, revision snapshots, explicit checks and exact file diffs."""

from __future__ import annotations

import base64
import difflib
import hashlib
import json
import re
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from .spec import load_spec, validate_spec, rule_source, get_value, set_value
from .expressions import Values, Unresolved
from .operations import (
    select_actions,
    apply_actions,
    remove_content,
    blocks,
    xml_operation,
    xml_selected,
    xml_field,
    selected_make_keys,
)
from .json_ops import merge_missing, json_object
from .config import validate

from .resolver import DiscoveryMiss, Resolver, relative_for
from .diagnostics import emit
from .transforms import TransformError, transform
from .reference import (
    apply_reference_action,
    augment_actions_from_reference,
    copy_make_settings,
)
from .perforce import MappingError, PerforceTimeout, parse_view


def digest(value):
    return hashlib.sha256(value).hexdigest()


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def seal(plan):
    plan.pop("digest", None)
    plan["digest"] = digest(canonical(plan))
    return plan


def verify_seal(plan):
    body = {k: v for k, v in plan.items() if k != "digest"}
    if plan.get("digest") != digest(canonical(body)):
        raise ValueError(
            "Plan was modified or is corrupt. Generate and review a new plan."
        )


def decode(data):
    return data.decode("utf-8-sig"), (
        "utf-8-sig" if data.startswith(b"\xef\xbb\xbf") else "utf-8"
    )


class RuleOutcome(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message
        super().__init__(message)


class Planner:
    def __init__(self, p4, config, catalog=None, *, blank=False):
        self.p4 = p4
        self.run_id = uuid.uuid4().hex[:8]
        self.catalog = (
            validate_spec(catalog) if catalog is not None else load_spec(config=config)
        )
        self.config = validate(config, self.catalog)
        self.input_config = json.loads(json.dumps(self.config))
        self.blank = blank
        self.snapshots, self.edits = {}, {}
        self.results, self.previews, self.requirements, self.rule_paths = [], [], [], []
        self.evidence = {}
        self.values = Values(
            self.catalog, self.config, read=self.optional_file, log=self.log
        )

    def log(self, message):
        emit(self.p4, f"[run {self.run_id}] {message}")

    def presence_report(self, rule, path, *, review_notes=None):
        def report(notice):
            self.log(
                f"{rule['id']}: file-wide presence check: {path}: {notice['message']}"
            )
            if notice["review"]:
                if review_notes is not None:
                    review_notes.append(notice["message"])
                else:
                    self.result(rule, "review", notice["message"], [path])

        return report

    def preview(self, rule, target_path, content, *, reference_path=None, note=""):
        """Record inspection-only content; it is never passed to the executor."""
        raw_content = content if isinstance(content, bytes) else content.encode("utf-8")
        reference_snapshot = self.snapshots.get(reference_path, {})
        file_type = (
            reference_snapshot.get("type", "text")
            if isinstance(content, bytes)
            else "text"
        )
        if isinstance(content, bytes):
            try:
                content, _ = decode(content)
            except UnicodeDecodeError:
                content = (
                    f"<binary content: {len(content)} bytes; SHA-256 {digest(content)}>"
                )
        self.previews.append(
            {
                "rule": rule["id"],
                "title": rule["title"],
                "source": rule["source"],
                "target_path": target_path,
                "reference_path": reference_path,
                "note": note,
                "content": content,
                "content_base64": base64.b64encode(raw_content).decode("ascii"),
                "file_type": file_type,
            }
        )

    def result(self, rule, status, message, paths=None):
        self.log(f"[{status.upper()}] {rule['id']}: {message}")
        for path in paths or []:
            self.log(f"{rule['id']} evidence path: {path}")
        self.results.append(
            {
                "rule": rule["id"],
                "title": rule["title"],
                "source": rule["source"],
                "status": status,
                "message": message,
                "paths": paths or [],
                "evidence": [
                    self.evidence[p] for p in paths or [] if p in self.evidence
                ],
            }
        )

    def snapshot(self, path, *, optional=False):
        if path in self.snapshots:
            self.log(
                f"Content cache reused: {path}#{self.snapshots[path]['revision']}; no new p4 print needed."
            )
            return self.snapshots[path]
        files = self.p4.files(path)
        if not files:
            if not optional:
                raise ValueError(f"Required file is absent: {path}")
            snapshot = {
                "path": path,
                "revision": None,
                "type": "text",
                "sha256": None,
                "content": "",
            }
        else:
            if len(files) != 1:
                raise ValueError(f"Expected a single file: {path}")
            rev = int(files[0]["rev"])
            content = self.p4.read_file(path, rev)
            snapshot = {
                "path": path,
                "revision": rev,
                "type": files[0].get("type", "text"),
                "sha256": digest(content),
                "content": base64.b64encode(content).decode("ascii"),
            }
        self.log(
            f"Snapshot recorded: {path}#{snapshot['revision']}; type={snapshot['type']}; SHA-256={snapshot['sha256']}"
        )
        self.snapshots[path] = snapshot
        return snapshot

    def content(self, path):
        if path in self.edits:
            return base64.b64decode(self.edits[path]["after"])
        return base64.b64decode(self.snapshot(path)["content"])

    def propose(self, path, content, rule, *, file_type=None):
        before = self.snapshot(path, optional=True)
        original = base64.b64decode(before["content"])
        if content == original and before["revision"] is not None:
            self.edits.pop(path, None)
            self.log(
                f"{rule['id']}: no content changes needed: {path}#{before['revision']}"
            )
            return False
        if path not in self.edits:
            self.log(
                f"{rule['id']}: resolving local edit destination in workspace {self.config['perforce']['client']}: {path}"
            )
            local = self.p4.where(path)
            self.edits[path] = {
                "path": path,
                "local_path": local,
                "revision": before["revision"],
                "before_sha256": before["sha256"],
                "before": before["content"],
                "type": file_type or before["type"],
                "rules": [],
                "sources": [],
            }
        edit = self.edits[path]
        edit["after"] = base64.b64encode(content).decode("ascii")
        edit["after_sha256"] = digest(content)
        if rule["id"] not in edit["rules"]:
            edit["rules"].append(rule["id"])
            edit["sources"].append(rule["source"])
        try:
            old, _ = decode(original)
            new, _ = decode(content)
            edit["diff"] = "".join(
                difflib.unified_diff(
                    old.splitlines(True),
                    new.splitlines(True),
                    fromfile=path
                    + (f"#{before['revision']}" if before["revision"] else " (absent)"),
                    tofile=path + " (proposed)",
                )
            )
        except UnicodeDecodeError:
            edit["diff"] = (
                f"Binary copy: {len(original)} -> {len(content)} bytes; SHA-256 {digest(content)}"
            )
        self.log(
            f"{rule['id']}: proposed edit recorded for {path}; {len(original)} -> {len(content)} bytes; SHA-256 {digest(content)}"
        )
        return True

    def optional_file(self, file_id):
        try:
            path = self.resolver.locate(file_id, optional=True)
        except DiscoveryMiss:
            return None
        return decode(self.content(path))[0] if path else None

    def _file(self, file_id, *, optional=False, absent=False):
        virtual = (
            set(self.edits).intersection(self.resolver.candidates(file_id))
            if self.edits and self.catalog["files"][file_id]["kind"] == "file"
            else set()
        )
        if virtual:
            live = self.resolver.locate(file_id, optional=True)
            choices = virtual | ({live} if live else set())
            if len(choices) != 1:
                raise MappingError("Multiple live/proposed files match " + file_id)
            path = choices.pop()
        else:
            path = self.resolver.locate(file_id, optional=optional, allow_absent=absent)
        paths = (
            [r["depotFile"] for r in path]
            if isinstance(path, list)
            else ([path] if path else [])
        )
        source_id = self.catalog["files"][file_id]["source"]
        role = self.catalog["sources"][source_id].get("role", "current")
        for name in paths:
            self.rule_paths.append(name)
            self.evidence[name] = {
                "path": name,
                "file": file_id,
                "source": source_id,
                "role": role,
            }
        return path

    def _message(self, value):
        return (
            str(self.values.eval(value))
            if isinstance(value, dict)
            else self.values.template(value)
        )

    def _missing(self, step, key, message, default="review"):
        policy = step.get("policy", {}).get(key, default)
        raise RuleOutcome(
            {"skip": "skipped", "block": "blocked"}.get(policy, policy), message
        )

    def context(self):
        for key, source in self.catalog["sources"].items():
            if source.get("input"):
                self.log(
                    f"Inputs: {key} template={get_value(self.config, source['input'])}"
                )
        self.resolver = Resolver(self.p4, self.config, self.catalog, values=self.values)
        for name, item in self.catalog["derive"].items():
            try:
                value = self.values.variable(name)
                if value is not None:
                    self.log(f"Resolved {name}={value}")
            except (Unresolved, ValueError) as exc:
                self.log(f"Derivation {name} unresolved: {exc}")
                if item.get("required"):
                    raise
        for item in self.catalog.get("preconditions", []):
            if not self.values.condition(item["condition"]):
                raise ValueError(self._message(item["message"]))

    def build(self):
        self.log(
            f"START {'empty-file preview' if self.blank else 'normal plan'}: rules={self.catalog['id']}; workspace={self.config['perforce']['client']}"
        )
        plan = {
            "schema_version": 1,
            "mode": "comparison" if self.blank else "live",
            "run_id": self.run_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "catalog_sha256": digest(canonical(self.catalog)),
            "source": self.catalog.get("source", self.catalog["title"]),
            "rules_id": self.catalog["id"],
            "rules_schema_version": 2,
            "input_config": self.input_config,
            "title": self.catalog.get("report", {}).get("title", self.catalog["title"]),
            "connection": self.config["perforce"],
            "identity": self.p4.identity(),
            "workspace": self.p4.workspace_spec(),
        }
        completed = {}
        try:
            self.context()
        except Exception as exc:
            self.result(
                {"id": "setup", "title": "Resolve project inputs", "source": "Inputs"},
                "blocked",
                str(exc),
            )
        else:
            pending = list(self.catalog["rules"])
            while pending:
                ready = next(
                    (
                        r
                        for r in pending
                        if all(d in completed for d in r.get("depends_on", []))
                    ),
                    None,
                )
                if ready is None:
                    raise ValueError("Unresolved rule dependencies")
                pending.remove(ready)
                rule = dict(ready, source=rule_source(ready))
                self.rule_paths = []
                previous = json.loads(json.dumps(self.edits))
                npreviews, nrequirements, nresults = (
                    len(self.previews),
                    len(self.requirements),
                    len(self.results),
                )
                self.log(f"RULE START {rule['id']}: {rule['title']}")
                try:
                    if any(
                        completed[d] not in ("pass", "change")
                        for d in rule.get("depends_on", [])
                    ):
                        raise RuleOutcome(
                            "review",
                            "A prerequisite rule is unresolved; no edits proposed.",
                        )
                    if not self.values.condition(rule.get("when", True)):
                        raise RuleOutcome(
                            "skipped",
                            rule.get("disabled_message", "Rule condition is disabled."),
                        )
                    statuses, messages = [], []
                    for step in rule["steps"]:
                        if not self.values.condition(step.get("when", True)):
                            continue
                        try:
                            status, message = self.execute_step(rule, step)
                        except RuleOutcome as exc:
                            if exc.status != "skipped":
                                raise
                            status, message = exc.status, exc.message
                        if step["op"] != "assert" or len(rule["steps"]) == 1:
                            statuses.append(status)
                        messages.append(message)
                    status = next(
                        (
                            s
                            for s in (
                                "blocked",
                                "review",
                                "change",
                                "pass",
                                "manual",
                                "skipped",
                            )
                            if s in statuses
                        ),
                        "skipped",
                    )
                    self.result(
                        rule,
                        status,
                        "\n".join(dict.fromkeys(messages)) or "No applicable steps.",
                        list(dict.fromkeys(self.rule_paths)),
                    )
                except Exception as exc:
                    self.edits = previous
                    del self.previews[npreviews:]
                    del self.requirements[nrequirements:]
                    del self.results[nresults:]
                    if isinstance(exc, RuleOutcome):
                        status, message = exc.status, exc.message
                    elif isinstance(exc, (Unresolved, DiscoveryMiss)):
                        status, message = "review", str(exc)
                    else:
                        status, message = "blocked", str(exc)
                    self.result(
                        rule,
                        status,
                        message,
                        list(
                            dict.fromkeys(
                                self.rule_paths
                                + (exc.paths if isinstance(exc, DiscoveryMiss) else [])
                            )
                        ),
                    )
                    if isinstance(exc, PerforceTimeout):
                        self.result(
                            {
                                "id": "planning.stopped",
                                "title": "Planning stopped",
                                "source": "Perforce",
                            },
                            "blocked",
                            "Remaining rules were not run after a timeout.",
                        )
                        break
                completed[rule["id"]] = status
        exclusions = []
        for item in self.catalog.get("comparison", {}).get("exclude", []):
            if self.values.condition(item.get("when", True)):
                exclusions.append({k: v for k, v in item.items() if k != "when"})
        try:
            description = self.values.template(
                self.catalog.get("report", {}).get("changelist", self.catalog["title"])
            )
        except Unresolved:
            description = self.catalog["title"]
        plan.update(
            config=self.config,
            templates=(
                getattr(self, "resolver", None).specs
                if hasattr(self, "resolver")
                else {}
            ),
            source_roles={
                k: v.get("role", "current") for k, v in self.catalog["sources"].items()
            },
            source_roots=(
                getattr(self, "resolver", None).roots
                if hasattr(self, "resolver")
                else {}
            ),
            checks=self.results,
            snapshots=list(self.snapshots.values()),
            changes=list(self.edits.values()),
            previews=self.previews,
            requirements=self.requirements,
            comparison_exclusions=exclusions,
            changelist_description=description,
            variables=self.values.cache,
        )
        plan = seal(plan)
        if self.blank:
            from .blank import plan_from_previews

            plan = plan_from_previews(plan)
        self.log(
            f"END {'empty-file preview' if self.blank else 'normal plan'}: {len(self.results)} checks; {len(plan['changes'])} file(s)."
        )
        return plan

    def requirement(
        self,
        rule,
        file_id,
        path,
        *,
        preview="",
        reference=None,
        desired="present",
        selector=None,
        step=None,
    ):
        file = self.catalog["files"][file_id]
        item = {
            "rule": rule["id"],
            "target_path": path,
            "file_id": file_id,
            "format": file["format"],
            "desired": desired,
            "comparison": file.get("comparison", {}),
            "source": rule["source"],
            "reference_path": reference,
            "preview": preview if isinstance(preview, str) else None,
        }
        if selector is not None:
            item["selector"] = selector
        if step is not None:
            item["step"] = step
        self.requirements.append(item)
        self.preview(
            rule,
            path,
            preview,
            reference_path=reference,
            note=(
                "Required absence: remove selected content if present."
                if desired == "absent"
                else "Content selected by the JSON rule; the reference supplies selected values."
            ),
        )
        self.previews[-1].update(
            format=file["format"],
            comparison=file.get("comparison", {}),
            desired=desired,
            requirement=item,
        )
        # Aggregate a rule's present snippets for the review/blank preview. Requirements
        # remain separate, so each operation retains its own comparison semantics.
        latest = self.previews[-1]
        earlier = next(
            (
                p
                for p in self.previews[:-1]
                if p["rule"] == rule["id"]
                and p["target_path"] == path
                and p.get("desired") == desired == "present"
                and p.get("format") not in ("xml", "binary")
            ),
            None,
        )
        if earlier is not None:
            content = earlier["content"].rstrip("\n") + "\n" + latest["content"]
            earlier.update(
                content=content,
                content_base64=base64.b64encode(content.encode("utf-8")).decode(
                    "ascii"
                ),
            )
            self.previews.pop()

    def _write(self, path, text, rule, encoding="utf-8", *, file_type=None):
        if self.blank:
            return True
        return self.propose(
            path,
            text.encode(encoding) if isinstance(text, str) else text,
            rule,
            file_type=file_type,
        )

    def _text(self, path, *, create=False):
        if self.blank:
            return "", "utf-8"
        before = self.snapshot(path, optional=create)
        raw = base64.b64decode(
            self.edits.get(path, before).get("after", before["content"])
        )
        return decode(raw)

    def parameters(self, value):
        if isinstance(value, str):
            return self.values.template(value)
        if isinstance(value, list):
            return [self.parameters(v) for v in value]
        if isinstance(value, dict):
            return {k: self.parameters(v) for k, v in value.items()}
        return value

    def execute_step(self, rule, step):
        step = dict(step)
        for side in ("from", "to"):
            if side in step:
                file_id = step[side]["file"]
                if not self.values.condition(
                    self.catalog["files"][file_id].get("when", True)
                ):
                    raise RuleOutcome(
                        "skipped", "File condition is disabled: " + file_id
                    )
        for key in (
            "select",
            "literal",
            "fields",
            "identity",
            "parent",
            "root",
            "block",
        ):
            if key in step:
                step[key] = self.parameters(step[key])
        if "from" in step and "select" in step["from"]:
            step["from"] = dict(
                step["from"], select=self.parameters(step["from"]["select"])
            )
        op = step["op"]
        if op == "manual":
            return step.get("status", "manual"), self._message(step["message"])
        if op == "assert":
            if not self.values.condition(step["condition"]):
                raise RuleOutcome(
                    step.get("status", "blocked"), self._message(step["message"])
                )
            return "pass", "Declared precondition satisfied."
        if op in ("file.exists", "path.assert"):
            try:
                path = self._file(step["to"]["file"])
            except DiscoveryMiss as exc:
                self._missing(step, "on_missing", str(exc))
            expected = self._message(step.get("assert_path", ""))
            if expected and expected not in path:
                return (
                    "review",
                    "Current path does not contain the mapped family " + expected,
                )
            return "pass", self._message(
                step.get("message", "Mapped current path exists.")
            )
        if (
            op == "json.merge_missing"
            and self.catalog["files"][step["from"]["file"]]["kind"] == "tree"
        ):
            return self.merge_tree(rule, step)
        if op == "block.sync":
            return self.sync_block(rule, step)
        source_id = step.get("from", {}).get("file")
        target_id = step.get("to", {}).get("file")
        source_path = None
        reference = ""
        if source_id:
            try:
                source_path = self._file(source_id, optional=True)
            except DiscoveryMiss as exc:
                self._missing(step, "on_source_missing", str(exc), "skip")
            if not source_path:
                self._missing(
                    step,
                    "on_source_missing",
                    "Absent in reference; no selected source content. Exact path(s) searched: "
                    + "; ".join(self.resolver.attempts.get(source_id, [])),
                    "skip",
                )
        if op in ("file.copy_if_missing", "tree.copy_missing"):
            sources = (
                [r["depotFile"] for r in source_path]
                if isinstance(source_path, list)
                else [source_path]
            )
            changed = False
            notes = []
            for src in sources:
                dst = self.resolver.paired(
                    src, source_id, target_id, step.get("pairing")
                )
                self.rule_paths.extend([src, dst])
                snap = self.snapshot(src)
                content = base64.b64decode(snap["content"])
                self.requirement(rule, target_id, dst, preview=content, reference=src)
                if not self.blank:
                    before = self.snapshot(dst, optional=True)
                    if before["revision"] is not None:
                        if before["sha256"] != snap["sha256"]:
                            notes.append(
                                "Current file differs from reference; existing file is preserved."
                            )
                        continue
                changed |= self._write(dst, content, rule, file_type=snap["type"])
            return ("review" if notes else "change" if changed else "pass"), (
                "\n".join(notes)
                or (
                    "Copy missing reference file."
                    if changed
                    else "Current and reference files are byte-for-byte identical; no change planned."
                )
            )
        if source_path:
            reference = decode(self.content(source_path))[0]
            self.log(
                f"{rule['id']}: reference source={source_path}; revision={self.snapshots[source_path]['revision']}"
            )
        select = step.get("from", {}).get("select", step.get("select", {}))
        if op in ("xml.verify", "xml.ensure") and not xml_selected(
            ET.fromstring(step.get("literal", reference)), select
        ):
            self._missing(
                step,
                "on_empty_selection",
                "Reference contains no selected XML entries; examples are not mandatory.",
                "skip",
            )
        create = (
            self.blank or step.get("policy", {}).get("on_target_missing") == "create"
        )
        try:
            path = self._file(target_id, absent=create)
        except DiscoveryMiss as exc:
            self._missing(step, "on_target_missing", str(exc))
        text, encoding = self._text(path, create=create)
        select = step.get("from", {}).get("select", step.get("select", {}))
        literal = step.get("literal")
        if op in ("make.remove", "text.remove", "block.remove"):
            if op == "block.remove":
                found = blocks(text, step["block"])
                if len(found) > 1:
                    raise ValueError("Ambiguous block removal")
                after = text
                for item in reversed(found):
                    after = after[: item["start"]] + after[item["end"] :]
            else:
                after = remove_content(text, select, make=op == "make.remove")
            self.requirement(
                rule, target_id, path, desired="absent", selector=select, step=step
            )
            changed = self._write(path, after, rule, encoding)
            return ("change" if changed else "pass"), (
                "Remove selected content."
                if changed
                else "Selected content is already absent."
            )
        if op in ("xml.verify", "xml.ensure"):
            spec_step = dict(step, select=select)
            xml_reference = literal if literal is not None else reference
            if self.blank and op == "xml.verify":
                return (
                    "manual",
                    "Verification-only rule; contributes no blank-file content.",
                )
            if not text:
                if not step.get("root") and not self.blank:
                    raise ValueError(
                        "Creating an XML document requires an explicit root"
                    )
                root = step.get("root") or ET.fromstring(xml_reference).tag
                text = "<" + root + "/>"
            details = []
            after, preview, notes = xml_operation(
                xml_reference, text, spec_step, details=details
            )
            if not preview:
                self._missing(
                    step,
                    "on_empty_selection",
                    "Reference contains no selected XML entries; examples are not mandatory.",
                    "skip",
                )
            if op == "xml.ensure":
                self.requirement(
                    rule,
                    target_id,
                    path,
                    preview=preview,
                    reference=source_path,
                    step=spec_step,
                )
            changed = (
                self._write(path, after, rule, encoding)
                if op == "xml.ensure"
                else False
            )
            return ("review" if notes else "change" if changed else "pass"), "\n".join(
                details
                + ([] if notes else ["Reference fields and minimum versions match."])
            )
        if op == "json.merge_missing":
            source = json_object(reference)
            after, added = merge_missing(
                source, json_object(text) if text.strip() else {}
            )
            rendered = json.dumps(after, indent=2, ensure_ascii=False) + "\n"
            self.requirement(
                rule,
                target_id,
                path,
                preview=reference,
                reference=source_path,
                step=step,
            )
            changed = self._write(
                path, rendered if added or not text.strip() else text, rule, encoding
            )
            return (
                "change" if changed else "pass"
            ), "Missing JSON keys added; existing values preserved."
        actions = select_actions(
            op,
            reference,
            select,
            literal,
            step.get("policy", {}).get("context_patterns", []),
        )
        difference = step.get("policy", {}).get("on_value_difference", "replace")
        for action in actions:
            action["on_value_difference"] = difference
        for action in actions:
            for replacement in step.get("replacements", []):
                old, new = replacement["old"], self._message(replacement["new"])
                for key in ("commands", "lines"):
                    if key in action:
                        action[key] = [value.replace(old, new) for value in action[key]]
        for action in actions:
            if step.get("operator"):
                action["operator"] = step["operator"]

        def preview_log(notice):
            message = notice if isinstance(notice, str) else notice["message"]
            self.log(
                f"{rule['id']}: empty-file preview: "
                + message.replace(
                    "whole current file; adding", "empty preview; adding"
                ).replace("current file", "empty preview")
            )

        empty = apply_actions("", actions, preview_log)
        if not empty:
            self._missing(
                step,
                "on_empty_selection",
                "Reference contains no matching selected statements; nothing is inserted.",
                "skip",
            )
        notes = []

        def report(notice):
            if isinstance(notice, str):
                self.log(f"{rule['id']}: current file: {notice}")
            else:
                self.presence_report(rule, path, review_notes=notes)(notice)

        after = apply_actions(text, actions, report)
        self.requirement(
            rule, target_id, path, preview=empty, reference=source_path, step=step
        )
        changed = self._write(path, after, rule, encoding)
        if step.get("policy", {}).get("report_current_only") and op == "make.values":
            # A section can contain arbitrary names and conditional flags, so
            # inspect selected identities rather than assuming one values action.
            extra = selected_make_keys(text, select) - selected_make_keys(
                empty, {"assignment_patterns": [".*"]}
            )
            if extra:
                notes.append(
                    "Current-only flags preserved for review: "
                    + ", ".join(sorted(extra))
                )
        return ("review" if notes else "change" if changed else "pass"), (
            (
                "\n".join(notes)
                + ("\nReference-selected edit proposed." if changed else "")
            )
            if notes
            else (
                "Reference-selected edit proposed."
                if changed
                else "Already match selected reference statements; no change planned. Other file content was not required to match."
            )
        )

    def sync_block(self, rule, step):
        source_id, target_id = step["from"]["file"], step["to"]["file"]
        try:
            src = self._file(source_id, optional=True)
        except DiscoveryMiss as exc:
            self._missing(step, "on_source_missing", str(exc), "skip")
        if not src:
            self._missing(
                step,
                "on_source_missing",
                "Absent in reference. Exact path(s) searched: "
                + "; ".join(self.resolver.attempts.get(source_id, [])),
                "skip",
            )
        reference = decode(self.content(src))[0]
        definition, choice = step["block"], step["choose"]
        all_blocks = blocks(reference, definition)
        if not all_blocks:
            self._missing(
                step,
                "on_empty_selection",
                "Reference has no recognized copy block; no default filter is imposed.",
                "skip",
            )
        requested = self.values.eval(choice.get("input"))
        prefix = self.values.eval(choice.get("prefix"))
        candidates = (
            [b for b in all_blocks if b[choice["field"]] == requested]
            if requested
            else [
                b
                for b in all_blocks
                if str(b[choice["field"]]).lower().startswith(str(prefix or "").lower())
            ]
        )
        if (
            not requested
            and not candidates
            and choice.get("single_fallback")
            and len(all_blocks) == 1
        ):
            candidates = all_blocks
        if len(candidates) != 1:
            raise ValueError(
                "Cannot uniquely infer selected block: "
                + ", ".join(str(b[choice["field"]]) for b in all_blocks)
            )
        selected = candidates[0]
        members = self.values.eval(choice.get("required_members")) or []
        if members:
            patterns = selected[choice["members_field"]]
            if any(
                not any(
                    re.fullmatch(re.escape(p).replace("%", ".*"), v) for p in patterns
                )
                for v in members
            ):
                raise ValueError(
                    "Selected copy filter does not cover requested members"
                )
        for key, target in choice.get("export", {}).items():
            if not get_value(self.config, target):
                set_value(self.config, target, selected[key])
        related = step.get("related", {})
        eligible = []
        for role in ("reference", "current"):
            if role == "current" and self.blank:
                continue
            if role not in related:
                continue
            file_id = related[role]
            records = self._file(file_id, optional=True)
            eligible = [
                r["depotFile"]
                for r in records
                if r["depotFile"].lower().endswith(related.get("suffix", "").lower())
            ]
            if not eligible:
                raise RuleOutcome(
                    "skipped" if role == "reference" else "review",
                    f"Couldn't find {related.get('suffix','matching')} files in {role} folder. Searched: "
                    + "; ".join(self.resolver.attempts[file_id])
                    + ". Files found: "
                    + ", ".join(r["depotFile"] for r in records),
                )
            if related.get("snapshot"):
                for path in eligible:
                    self.snapshot(path)
        dst = self._file(target_id, absent=self.blank)
        text, encoding = self._text(dst)
        identity = definition["identity"]
        found = [
            b for b in blocks(text, definition) if b[identity] == selected[identity]
        ]
        if len(found) > 1:
            raise ValueError("Current file has duplicate matching filter blocks")
        replacement = (
            selected["text"]
            .replace("\r\n", "\n")
            .replace("\n", "\r\n" if "\r\n" in text else "\n")
        )
        if found:
            block = found[0]
            changed = any(block[k] != selected[k] for k in definition["compare"])
            after = (
                text[: block["start"]] + replacement + text[block["end"] :]
                if changed
                else text
            )
        else:
            marker = definition.get("unrecognized_marker", "")
            for key, value in selected.items():
                marker = marker.replace("{" + key + "}", str(value))
            if marker and marker in text:
                raise ValueError(
                    "Current file mentions selected content in an unrecognized block"
                )
            after = (
                text
                + ("" if not text or text.endswith("\n") else "\n")
                + replacement
                + "\n"
            )
        self.requirement(
            rule, target_id, dst, preview=selected["text"] + "\n", reference=src
        )
        self.previews[-1]["note"] += " Files verified: " + ", ".join(
            eligible if related else []
        )
        changed = self._write(dst, after, rule, encoding)
        return ("change" if changed else "pass"), "Folder " + str(
            selected[identity]
        ) + ": reference copy filter " + (
            "will be added/updated." if changed else "already matches."
        )

    def merge_tree(self, rule, step):
        source_id, target_id = step["from"]["file"], step["to"]["file"]
        reference, inventory = self.resolver.inventory(source_id)
        current, _ = self.resolver.inventory(target_id)
        roots = self.resolver.candidates(source_id)
        targets = self.resolver.candidates(target_id)
        if len(roots) != 1 or len(targets) != 1:
            raise ValueError("Tree merge needs unique source/destination roots")
        root, dest = roots[0], targets[0]
        filename = (
            self.catalog["files"][source_id]
            .get("filter", {})
            .get("basename", "selected JSON")
        )
        meta = step.get("inventory", {})
        label = meta.get("label", "Tree")
        self.log(
            f"{label} discovery: {root}/...; {len(inventory)} files found; {len(reference)} named exactly {filename}."
        )
        selected = {r["depotFile"] for r in reference}
        regions = set()
        for record in inventory:
            path = record["depotFile"]
            directory, base = path.rsplit("/", 1)
            if base in meta.get("markers", []):
                regions.add(directory)
            if meta.get("filename_prefix") and base.startswith(meta["filename_prefix"]):
                regions.add(
                    directory.rsplit("/", 1)[0]
                    if directory.rsplit("/", 1)[-1].lower()
                    in meta.get("parent_dirs", [])
                    else directory
                )
        if inventory and not regions:
            regions.add(root)
        for region in sorted(regions):
            if any(p.startswith(region + "/") for p in selected):
                continue
            found = [r for r in inventory if r["depotFile"].startswith(region + "/")]
            for r in found:
                self.log(
                    f"{label} file found and skipped: {r['depotFile']} ({r.get('type','unknown')}); only {filename} is eligible."
                )
            self.result(
                rule,
                "skipped",
                f"No {filename} anywhere under this reference region after searching its entire subtree; left untouched. Files found: "
                + ", ".join(
                    r["depotFile"].rsplit("/", 1)[-1]
                    + " ("
                    + r.get("type", "unknown")
                    + ")"
                    for r in found
                ),
                [region, *[r["depotFile"] for r in found]],
            )
        changed = False
        for record in reference:
            src = record["depotFile"]
            dst = dest + src[len(root) :]
            self.rule_paths.extend([src, dst])
            self.log(f"{label} JSON file found: {src} ({record.get('type','text')}).")
            snap = self.snapshot(src)
            content = base64.b64decode(snap["content"])
            reference_json = json_object(decode(content)[0])
            self.requirement(rule, target_id, dst, preview=content, reference=src)
            before = None if self.blank else self.snapshot(dst, optional=True)
            if self.blank or before["revision"] is None:
                changed |= self._write(dst, content, rule, file_type=snap["type"])
            else:
                text, encoding = self._text(dst)
                merged, added = merge_missing(reference_json, json_object(text))
                if added:
                    after = (
                        json.dumps(merged, indent=2, ensure_ascii=False) + "\n"
                    ).replace("\n", "\r\n" if "\r\n" in text else "\n")
                    changed |= self._write(dst, after, rule, encoding)
        for record in current:
            self.log(
                f"{label} JSON file found: {record['depotFile']} ({record.get('type','text')})."
            )
        if not reference:
            return (
                "review",
                f"No {filename}: "
                + (
                    f"Perforce returned no files for {root}/..."
                    if not inventory
                    else f"Perforce returned {len(inventory)} files for {root}/..., but none matched the exact filename {filename}"
                )
                + "; check exact depot/directory casing and selection.",
            )
        expected = {dest + p[len(root) :] for p in selected}
        extras = {r["depotFile"] for r in current} - expected
        if extras and step.get("policy", {}).get("report_current_only"):
            self.result(
                rule,
                "review",
                "Current-only files have no same-region reference; left untouched.",
                sorted(extras),
            )
            return (
                "change" if changed else "pass"
            ), "Reference regions processed; current-only files retained."
        return (
            "change" if changed else "pass"
        ), "Added missing JSON files/keys; existing values kept."


def save_plan(plan, directory):
    verify_seal(plan)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    output = directory / "plan.json"
    output.write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")
    (directory / "plan.txt").write_text(summary(plan), encoding="utf-8")
    (directory / "empty-file-previews.txt").write_text(
        preview_summary(plan), encoding="utf-8"
    )
    return output


def summary(plan):
    counts = {
        status: sum(c["status"] == status for c in plan["checks"])
        for status in ("pass", "change", "blocked", "review", "manual", "skipped")
    }
    lines = [
        plan.get("title", "Delta plan"),
        f"Plan: {plan['digest']}",
        f"Mode: {plan['mode']}",
        f"Source: {plan['source']}",
        f"Files to change: {len(plan['changes'])}; checks: {counts}",
        (
            "Inspection-only comparison plan; cannot be approved or applied. Basis: "
            + plan.get("comparison_basis", "")
            if plan["mode"] == "comparison"
            else "Approval applies only to these exact changes. BLOCKED checks stay untouched and are reported after apply. Creates a pending changelist; never shelves or submits."
        ),
        "",
    ]
    lines.append(
        "Resolved inputs: "
        + json.dumps(plan.get("variables", plan.get("config", {})), ensure_ascii=False)
    )
    selection = plan.get("review_selection")
    if selection:
        lines.extend(["", "Reviewer selection from plan: " + selection["source_plan_digest"],
                      "Check results below describe the original generated plan; rules were not rerun after selection.",
                      "Only the selected diffs in this plan will be applied."])
        for file in selection["files"]:
            lines.append(file["path"])
            for key, label in (("included", "INCLUDE"), ("excluded", "EXCLUDE")):
                lines.extend(f"  {label} [{item['id']}]: {item['label']}" for item in file[key])
    for check in plan["checks"]:
        lines.extend(
            [
                "",
                f"[{check['status'].upper()}] {check['title']} ({check['source']})",
                check["message"],
                *check["paths"],
            ]
        )
    for change in plan["changes"]:
        lines.extend(
            [
                "",
                "=" * 72,
                f"{'ADD' if change['revision'] is None else 'EDIT'} {change['path']}",
                (
                    f"Workspace file: {change['local_path']}"
                    if change["local_path"]
                    else "Comparison only; no local workspace target required."
                ),
                "Checklist: " + ", ".join(change["sources"]),
                change["diff"],
            ]
        )
    return "\n".join(lines) + "\n"


def compact_summary(plan):
    """Approval view: statuses and filenames, followed by exact proposed diffs."""
    views = [
        (plan.get("source_roles", {}).get(key, key.split(".")[0]), parse_view(spec))
        for key, spec in plan.get("templates", {}).items()
    ]
    known_roles = {}
    for preview in plan.get("previews", []):
        for key, role in (("reference_path", "reference"), ("target_path", "current")):
            if preview.get(key):
                known_roles.setdefault(preview[key], set()).add(role)
    for check in plan.get("checks", []):
        for item in check.get("evidence", []):
            known_roles.setdefault(item["path"], set()).add(item["role"])
    for change in plan["changes"]:
        known_roles.setdefault(change["path"], set()).add("current")

    def roles_for(path):
        roles = set(known_roles.get(path, ()))
        for role, view in views:
            try:
                relative_for(view, path)
            except MappingError:
                continue
            roles.add(role)
        for source, root in plan.get("source_roots", {}).items():
            if path.startswith(root + "/"):
                roles.add(plan.get("source_roles", {}).get(source, "unknown"))
        return roles

    lines = []
    if plan.get("review_selection"):
        lines.extend(["Only checked edits are included below.",
                      "Check statuses describe the original plan; selection does not rerun the checks.", ""])
    for check in plan["checks"]:
        lines.append(
            f"[{check['status'].upper()}] {check['title']} ({check['source']})"
        )
        files = {"reference": [], "current": [], "unknown": []}
        for path in dict.fromkeys(check.get("paths", [])):
            for role in roles_for(path) or {"unknown"}:
                files[role].append(path.rsplit("/", 1)[-1])
        for role, label in (("reference", "Reference"), ("current", "Current")):
            lines.append(f"  {label}: " + (", ".join(files[role]) or "—"))
        if files["unknown"]:
            lines.append("  Unresolved role: " + ", ".join(files["unknown"]))
        lines.append("")
    lines.append("PROPOSED EDITS")
    if not plan["changes"]:
        lines.append("None.")
    for change in plan["changes"]:
        lines.extend(
            [
                "",
                f"{'ADD' if change['revision'] is None else 'EDIT'} {change['path']}",
                change["diff"],
            ]
        )
    return "\n".join(lines) + "\n"


def preview_summary(plan):
    lines = [
        "Empty-file and reference-source previews",
        "Inspection only: these previews are not workspace files and are never applied directly.",
        "Each section shows what one rule sees or would construct if its current target were empty.",
    ]
    previews = plan.get("previews", [])
    if not previews:
        lines.extend(["", "No preview content was available for this plan."])
    for item in previews:
        lines.extend(
            [
                "",
                "=" * 80,
                f"{item['title']} ({item['source']})",
                f"Target: {item['target_path']}",
            ]
        )
        if item.get("reference_path"):
            lines.append(f"Reference: {item['reference_path']}")
        if item.get("note"):
            lines.append("Meaning: " + item["note"])
        lines.extend(["-" * 80, item.get("content", "")])
    return "\n".join(lines).rstrip() + "\n"
