"""Generate checklist/reference content as if each destination file were blank."""
from __future__ import annotations

import base64
import copy
import difflib
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from .perforce import PerforceTimeout
from .csc import carrier_files, carrier_json, model_root
from .planner import (Planner, canonical, decode, digest, make_values, name_selector,
                      seal, select_hcf_block, substitute, verify_seal)
from .reference import augment_actions_from_reference, copy_make_settings
from .transforms import transform


def plan_from_previews(source_plan):
    """Use the actual tab-4 preview entries, irrespective of proposed edits."""
    verify_seal(source_plan)
    plan = copy.deepcopy(source_plan)
    plan["mode"] = "comparison"
    plan["comparison_basis"] = "Tab 4 Empty-file preview content, grouped by target filename and rule."
    grouped = {}
    snapshots = {item["path"]: item for item in source_plan.get("snapshots", [])}
    for preview in source_plan.get("previews", []):
        path = preview["target_path"]
        content = preview.get("content_base64")
        file_type = preview.get("file_type", "text")
        if content is None:
            source = snapshots.get(preview.get("reference_path"), {})
            if not source.get("type", "text").startswith("text") and "content" in source:
                content, file_type = source["content"], source["type"]
            else:
                content = base64.b64encode(preview["content"].encode("utf-8")).decode("ascii")
        entry = grouped.setdefault(path, {"path": path, "local_path": "", "revision": None,
                                         "type": file_type, "before": "", "before_sha256": None,
                                         "rules": [], "sources": [], "preview_contents": []})
        if preview["rule"] not in entry["rules"]:
            entry["rules"].append(preview["rule"])
        if preview["source"] not in entry["sources"]:
            entry["sources"].append(preview["source"])
        entry["preview_contents"].append({"rule": preview["rule"], "content": content, "type": file_type})
    for entry in grouped.values():
        snippets = list(dict.fromkeys(item["content"] for item in entry["preview_contents"]))
        raw = [base64.b64decode(content) for content in snippets]
        content = b"".join(part + (b"\n" if part and not part.endswith(b"\n") else b"") for part in raw) if len(raw) > 1 else raw[0]
        entry.update(after=base64.b64encode(content).decode("ascii"), after_sha256=digest(content))
        try:
            text, _ = decode(content)
            entry["diff"] = "".join(difflib.unified_diff([], text.splitlines(True),
                               fromfile=entry["path"] + " (empty)", tofile=entry["path"] + " (tab 4 preview)"))
        except UnicodeDecodeError:
            entry["diff"] = f"Binary preview: {len(content)} bytes; SHA-256 {digest(content)}"
    plan["changes"] = list(grouped.values())
    return seal(plan)


class BlankPlanner(Planner):
    """Current templates supply destinations only; current content is never read."""

    def write_blank(self, path, content, rule, *, file_type="text", reference_path=None):
        content = content.encode("utf-8") if isinstance(content, str) else content
        previous = self.edits.get(path)
        self.edits[path] = {"path": path, "local_path": "", "revision": None,
                            "type": file_type, "before": "", "before_sha256": None,
                            "after": base64.b64encode(content).decode("ascii"),
                            "after_sha256": digest(content),
                            "rules": list(dict.fromkeys((previous or {}).get("rules", []) + [rule["id"]])),
                            "sources": list(dict.fromkeys((previous or {}).get("sources", []) + [rule["source"]]))}
        try:
            text, _ = decode(content)
            diff = "".join(difflib.unified_diff([], text.splitlines(True), fromfile=path + " (blank)",
                                              tofile=path + " (blank plan)"))
        except UnicodeDecodeError:
            diff = f"Binary reference content: {len(content)} bytes; SHA-256 {digest(content)}"
        self.edits[path]["diff"] = diff
        self.preview(rule, path, content, reference_path=reference_path,
                     note="Content generated for a blank destination from reference/checklist rules.")
        self.result(rule, "change", "Included in the blank plan, independent of current file content.", [path])

    def blank_content(self, path):
        return decode(base64.b64decode(self.edits[path]["after"]))[0] if path in self.edits else ""

    def build(self):
        plan = {"schema_version": 1, "mode": "comparison", "created_at": datetime.now(timezone.utc).isoformat(),
                "catalog_sha256": digest(canonical(self.catalog)), "source": self.catalog["source"],
                "connection": self.config["perforce"], "identity": self.p4.identity(),
                "workspace": self.p4.workspace_spec(),
                "comparison_basis": "Blank destination files; latest reference content and checklist rules."}
        try:
            self.context()
        except Exception as exc:
            self.result({"id": "setup", "title": "Resolve blank-plan inputs", "source": "Inputs"}, "blocked", str(exc))
        else:
            for source_rule in self.catalog["rules"]:
                rule = substitute(source_rule, self.variables)
                self.rule_paths = []
                previous = copy.deepcopy(self.edits)
                previews_start, results_start = len(self.previews), len(self.results)
                try:
                    self.blank_rule(rule)
                except Exception as exc:
                    self.edits = previous
                    del self.previews[previews_start:]
                    del self.results[results_start:]
                    self.result(rule, "blocked", str(exc), self.rule_paths)
                    if isinstance(exc, PerforceTimeout):
                        self.result({"id": "planning.stopped", "title": "Blank planning stopped", "source": "Perforce"},
                                    "blocked", "Remaining rules were not run after a timeout.")
                        break
        plan.update(config=self.config, templates=self.resolver.specs if hasattr(self, "resolver") else {},
                    checks=self.results, snapshots=list(self.snapshots.values()),
                    changes=list(self.edits.values()), previews=self.previews)
        return seal(plan)

    def blank_rule(self, rule):
        kind, scope, target = rule["kind"], rule["scope"], rule["target"]
        if kind in ("manual", "verify_hals", "verify_firmware"):
            paths = [] if kind == "manual" else [self.resolver.blank_target(scope, target)]
            self.result(rule, "manual", rule.get("notes", "Verification-only rule; contributes no blank-file content."), paths)
            return
        if kind == "carrier_features":
            if not self.config["check_csc_features"]:
                self.result(rule, "skipped", "CSC feature checks are disabled. Select Check CSC features to include them.")
                return
            return self.blank_carrier(rule)
        if kind in ("copy_if_reference", "copy_tree_if_reference"):
            source = self.resolver.discover("reference", scope, target, optional=True)
            if not source:
                self.result(rule, "skipped", "No reference file to include in the blank plan.")
                return
            sources = [item["depotFile"] for item in source] if isinstance(source, list) else [source]
            for src in sources:
                path = self.resolver.counterpart(src, scope)
                self.rule_paths.append(path)
                snapshot = self.snapshot(src)
                self.write_blank(path, base64.b64decode(snapshot["content"]), rule,
                                 file_type=snapshot["type"], reference_path=src)
            return
        if kind == "verify_hcf":
            src = self.resolver.discover("reference", scope, "hcf_makefile")
            path = self.resolver.blank_target(scope, "hcf_makefile")
            self.rule_paths = [path]
            reference, _ = decode(self.content(src))
            selected = select_hcf_block(reference, self.config)
            self.write_blank(path, selected["text"] + "\n", rule, reference_path=src)
            return
        path = self.resolver.blank_target(scope, target)
        self.rule_paths = [path]
        current_blank = self.blank_content(path)
        src = None
        if kind == "reference_make_settings":
            src = self.resolver.discover("reference", scope, target)
            reference, _ = decode(self.content(src))
            if "WLAN_CHIP" in rule["keys"] and make_values(reference, "WLAN_").get("WLAN_CHIP", "").strip('"').lower() != self.config["chipset"]:
                raise ValueError("Selected chipset differs from reference WLAN_CHIP")
            after = copy_make_settings(current_blank, reference, rule["keys"], rule.get("include_basenames", []),
                                       rule.get("key_patterns", []))
        elif kind == "reference_features":
            src = self.resolver.discover("reference", scope, target)
            reference, _ = decode(self.content(src))
            if rule["format"] == "make":
                values = make_values(reference, rule["prefix"], rule.get("key_patterns", []))
                after = transform(current_blank, {"type": "assignments", "values": values, "operator": "="})
            else:
                root = ET.fromstring(reference)
                selected = name_selector(rule["prefix"], rule.get("key_patterns", []))
                elements = [element for element in root.iter() if selected(element.tag)]
                values = {element.tag: element.text or "" for element in elements}
                if len(values) != len(elements) or any(len(element) for element in elements):
                    raise ValueError("Duplicate/non-leaf reference feature XML elements")
                after = transform(current_blank or f"<{root.tag}></{root.tag}>\n",
                                  {"type": "xml_elements", "elements": values, "parent": root.tag})
            if not values:
                self.result(rule, "review", "Reference has no selected feature values.", [src, path])
                return
        elif kind == "transform":
            actions = rule["actions"]
            if any(any(key.startswith("reference_") for key in action) for action in actions):
                src = self.resolver.discover("reference", scope, target)
                reference, _ = decode(self.content(src))
                actions = augment_actions_from_reference(reference, actions)
            after = current_blank
            for action in actions:
                after = transform(after, action)
        else:
            raise ValueError(f"Unsupported blank-plan rule kind: {kind}")
        self.write_blank(path, after, rule, reference_path=src)

    def blank_carrier(self, rule):
        root = model_root(self.config["reference"]["csc_path"], self.config["model"])
        current_root = model_root(self.config["current"]["csc_path"], self.config["model"])
        discovery = {}
        reference = carrier_files(self.p4, root, details=discovery)
        self.report_carrier_discovery(rule, discovery)
        for relative, src in reference.items():
            path = current_root + "/" + relative
            self.rule_paths.append(path)
            source = self.snapshot(src)
            content = base64.b64decode(source["content"])
            carrier_json(decode(content)[0])
            self.write_blank(path, content, rule,
                             file_type=source["type"], reference_path=src)
