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

from .catalog import default_catalog
from .csc import CARRIER_FILENAME, add_missing_features, carrier_files, carrier_json, missing_carrier_message, model_root
from .config import validate
from .resolver import DiscoveryMiss, Resolver, relative_for
from .diagnostics import emit
from .transforms import TransformError, transform
from .reference import apply_reference_action, augment_actions_from_reference, copy_make_settings
from .perforce import MappingError, PerforceTimeout, translate_path


def digest(value):
    return hashlib.sha256(value).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def seal(plan):
    plan.pop("digest", None)
    plan["digest"] = digest(canonical(plan))
    return plan


def verify_seal(plan):
    body = {k: v for k, v in plan.items() if k != "digest"}
    if plan.get("digest") != digest(canonical(body)):
        raise ValueError("Plan was modified or is corrupt. Generate and review a new plan.")


def decode(data):
    return data.decode("utf-8-sig"), "utf-8-sig" if data.startswith(b"\xef\xbb\xbf") else "utf-8"


def substitute(value, variables):
    if isinstance(value, str):
        for key, replacement in variables.items():
            value = value.replace("{" + key + "}", str(replacement))
        if re.search(r"\{(?:chipset|firmware|efs)\}", value):
            raise ValueError(f"Unresolved model setting: {value}")
        return value
    if isinstance(value, list):
        return [substitute(v, variables) for v in value]
    if isinstance(value, dict):
        return {k: substitute(v, variables) for k, v in value.items()}
    return value


def name_selector(prefix="", patterns=None):
    try:
        compiled = [re.compile(pattern) for pattern in (patterns or [])]
    except re.error as exc:
        raise TransformError(f"Invalid Bluetooth name pattern: {exc}") from exc
    return lambda name: bool(prefix and name.startswith(prefix)) or any(pattern.search(name) for pattern in compiled)


def make_values(text, prefix, patterns=None):
    values = {}
    selected = name_selector(prefix, patterns)
    depth = 0
    for line in text.splitlines():
        code = line.split("#", 1)[0].strip()
        if re.match(r"^(ifeq|ifneq|ifdef|ifndef)\b", code):
            depth += 1
        if code == "endif":
            depth -= 1
        match = re.match(r"^([A-Za-z0-9_]+)\s*(?::=|=|\?=|\+=)\s*(.*?)\s*$", code)
        if match and selected(match[1]):
            if depth or match[1] in values or "$(" in match[2] or "${" in match[2] or code.endswith("\\"):
                raise TransformError(f"Feature {match[1]} is conditional, duplicated or computed; resolve explicitly")
            values[match[1]] = match[2]
    return values


def hidl_version_at_least(actual, minimum):
    """Return whether a dotted numeric HIDL version meets the checklist minimum."""
    try:
        actual_parts = tuple(int(part) for part in actual.strip().split("."))
        minimum_parts = tuple(int(part) for part in minimum.strip().split("."))
    except (AttributeError, ValueError):
        return False
    width = max(len(actual_parts), len(minimum_parts))
    return actual_parts + (0,) * (width - len(actual_parts)) >= minimum_parts + (0,) * (width - len(minimum_parts))


def hcf_blocks(source):
    found = []
    pattern = r"(?m)^\s*ifneq\s*\(\s*\$\(filter\s+([^,]+),\s*\$\(TARGET_PRODUCT\)\s*\)\s*,\s*\)(.*?)^\s*endif\b[^\r\n]*"
    for match in re.finditer(pattern, source, re.S):
        body = match[2]
        if re.search(r"\b(?:ifeq|ifneq|ifdef|ifndef)\b", body):
            continue
        variant_match = re.search(r"\$\(HCF_PATH\)/([A-Za-z0-9_.-]+)", body)
        if not (variant_match and "$(TARGET_COPY_OUT_VENDOR)/firmware/wifi" in body and
                "PRODUCT_COPY_FILES" in body and "find-copy-subdir-files" in body):
            continue
        start = match.start()
        line_start = source.rfind("\n", 0, start) + 1
        previous_end = max(0, line_start - 1)
        previous_start = source.rfind("\n", 0, previous_end) + 1
        if source[previous_start:previous_end].strip().startswith("#"):
            start = previous_start
        found.append({"variant": variant_match[1], "products": match[1].split(),
                      "text": source[start:match.end()].strip(),
                      "start": start, "end": match.end()})
    return found



def select_hcf_block(reference_text, config):
    reference_blocks = hcf_blocks(reference_text)
    if not reference_blocks:
        raise ValueError("No recognized HCF TARGET_PRODUCT copy block in reference bluetooth.mk")
    configured = config.get("hcf_variant", "")
    if configured:
        candidates = [item for item in reference_blocks if item["variant"] == configured]
    else:
        model = config["model"].lower()
        candidates = [item for item in reference_blocks if item["variant"].lower().startswith(model)]
        if not candidates and len(reference_blocks) == 1:
            candidates = reference_blocks
    if len(candidates) != 1:
        found = ", ".join(sorted(item["variant"] for item in reference_blocks))
        raise ValueError(f"Cannot uniquely infer HCF model folder for {config['model']}; reference bluetooth.mk contains: {found}")
    selected = candidates[0]
    variant = selected["variant"]
    config["hcf_variant"] = variant
    if not config.get("products"):
        config["products"] = list(selected["products"])
    missing = {product for product in config["products"]
               if not any(re.fullmatch(re.escape(pattern).replace("%", ".*"), product)
                          for pattern in selected["products"])}
    if missing:
        raise ValueError("Selected HCF copy filter does not cover TARGET_PRODUCT: " + ", ".join(sorted(missing)))

    return selected


class Planner:
    def __init__(self, p4, config, catalog=None):
        self.p4 = p4
        self.run_id = uuid.uuid4().hex[:8]
        self.config = validate(config)
        catalog_file = Path(__file__).resolve().parent.parent / "checklist" / "slsi.json"
        self.catalog = catalog if catalog is not None else (json.loads(catalog_file.read_text(encoding="utf-8")) if catalog_file.exists() else default_catalog())
        self.snapshots = {}
        self.edits = {}
        self.results = []
        self.previews = []
        self.rule_paths = []

    def log(self, message):
        emit(self.p4, f"[run {self.run_id}] {message}")

    def start_log(self, mode):
        self.log(f"START {mode}: model={self.config['model']}; workspace={self.config['perforce']['client']}; "
                 f"CSC features={self.config['check_csc_features']}")
        for role in ("current", "reference"):
            for scope in ("system", "vendor"):
                self.log(f"Inputs: {role}.{scope} template={self.config[role][scope + '_template']}")

    def reference_actions(self, rule, reference):
        actions = augment_actions_from_reference(reference, rule["actions"])
        for action in actions:
            if action["type"] == "init_commands" and self.config["jdm"]:
                action["commands"] = [command.replace("/mnt/vendor/efs", "/efs") for command in action["commands"]]
                self.log(f"{rule['id']}: applied explicit JDM /efs selection to reference init commands.")
            self.log(f"{rule['id']}: reference-selected {action['type']}: " +
                     repr({key: value for key, value in action.items() if key in ('event', 'commands', 'packages', 'lines', 'keys')}))
        if not actions:
            self.log(f"{rule['id']}: no matching reference actions; checklist defaults will not be inserted.")
        return actions

    def presence_report(self, rule, path):
        def report(notice):
            self.log(f"{rule['id']}: file-wide presence check: {path}: {notice['message']}")
            if notice['review']:
                self.result(rule, 'review', notice['message'], [path])
        return report

    def preview(self, rule, target_path, content, *, reference_path=None, note=""):
        """Record inspection-only content; it is never passed to the executor."""
        raw_content = content if isinstance(content, bytes) else content.encode("utf-8")
        reference_snapshot = self.snapshots.get(reference_path, {})
        file_type = reference_snapshot.get("type", "text") if isinstance(content, bytes) else "text"
        if isinstance(content, bytes):
            try:
                content, _ = decode(content)
            except UnicodeDecodeError:
                content = f"<binary content: {len(content)} bytes; SHA-256 {digest(content)}>"
        self.previews.append({"rule": rule["id"], "title": rule["title"],
                              "source": rule["source"], "target_path": target_path,
                              "reference_path": reference_path, "note": note,
                              "content": content, "content_base64": base64.b64encode(raw_content).decode("ascii"),
                              "file_type": file_type})

    def result(self, rule, status, message, paths=None):
        self.log(f"[{status.upper()}] {rule['id']}: {message}")
        for path in paths or []:
            self.log(f"{rule['id']} evidence path: {path}")
        self.results.append({"rule": rule["id"], "title": rule["title"], "source": rule["source"],
                             "status": status, "message": message, "paths": paths or []})

    def snapshot(self, path, *, optional=False):
        if path in self.snapshots:
            self.log(f"Content cache reused: {path}#{self.snapshots[path]['revision']}; no new p4 print needed.")
            return self.snapshots[path]
        files = self.p4.files(path)
        if not files:
            if not optional:
                raise ValueError(f"Required file is absent: {path}")
            snapshot = {"path": path, "revision": None, "type": "text", "sha256": None, "content": ""}
        else:
            if len(files) != 1:
                raise ValueError(f"Expected a single file: {path}")
            rev = int(files[0]["rev"])
            content = self.p4.read_file(path, rev)
            snapshot = {"path": path, "revision": rev, "type": files[0].get("type", "text"),
                        "sha256": digest(content), "content": base64.b64encode(content).decode("ascii")}
        self.log(f"Snapshot recorded: {path}#{snapshot['revision']}; type={snapshot['type']}; SHA-256={snapshot['sha256']}")
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
            self.log(f"{rule['id']}: no content changes needed: {path}#{before['revision']}")
            return False
        if path not in self.edits:
            self.log(f"{rule['id']}: resolving local edit destination in workspace {self.config['perforce']['client']}: {path}")
            local = self.p4.where(path)
            self.edits[path] = {"path": path, "local_path": local, "revision": before["revision"],
                                "before_sha256": before["sha256"], "before": before["content"],
                                "type": file_type or before["type"], "rules": [], "sources": []}
        edit = self.edits[path]
        edit["after"] = base64.b64encode(content).decode("ascii")
        edit["after_sha256"] = digest(content)
        if rule["id"] not in edit["rules"]:
            edit["rules"].append(rule["id"])
            edit["sources"].append(rule["source"])
        try:
            old, _ = decode(original)
            new, _ = decode(content)
            edit["diff"] = "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                                       fromfile=path + (f"#{before['revision']}" if before["revision"] else " (absent)"),
                                                       tofile=path + " (proposed)"))
        except UnicodeDecodeError:
            edit["diff"] = f"Binary copy: {len(original)} -> {len(content)} bytes; SHA-256 {digest(content)}"
        self.log(f"{rule['id']}: proposed edit recorded for {path}; {len(original)} -> {len(content)} bytes; SHA-256 {digest(content)}")
        return True

    def context(self):
        path_rules = json.loads(json.dumps(self.catalog.get("path_rules", {})))
        legacy_headers = self.catalog.get("model_common_files", {}).get("bluetooth_header", [])
        if legacy_headers:
            path_rules.setdefault("system", {})["bluetooth_header"] = [
                {"anchor": "/EXYNOS/", "relative": "{model}_sssi/device/{common_device}/" + item}
                for item in legacy_headers]
        self.resolver = Resolver(self.p4, self.config, path_rules)
        # Explicit input may choose the new chip, but must not silently turn a
        # non-SLSI reference into an SLSI project.
        def board_values(scope):
            try:
                path = self.resolver.discover("reference", scope, "board_config")
            except DiscoveryMiss as exc:
                self.log(f"Optional setup board unavailable: {exc}")
                return {}
            return make_values(decode(self.content(path))[0], "WLAN_")

        reference_values = board_values("system")
        vendor = reference_values.get("WLAN_VENDOR", "").strip('"')
        if vendor and vendor != "8":
            raise ValueError("Reference WLAN_VENDOR is not SLSI (8)")
        if not self.config.get("chipset"):
            values = reference_values if reference_values.get("WLAN_CHIP") else board_values("vendor")
            vendor = values.get("WLAN_VENDOR", "").strip('"')
            if vendor and vendor != "8":
                raise ValueError("Reference WLAN_VENDOR is not SLSI (8)")
            chip = values.get("WLAN_CHIP", "").strip('"').lower()
            if not re.fullmatch(r"[a-z0-9_]+", chip):
                raise ValueError("No usable reference WLAN_CHIP was found; enter chipset explicitly to resolve chipset-specific files. No default chipset is imposed.")
            self.config["chipset"] = chip
        chip = self.config["chipset"].lower()
        self.config["chipset"] = chip
        family = self.catalog["chipsets"].get(chip)
        if not family:
            raise ValueError(f"Chipset {chip} is not in SLSI!C34. Add a verified chipset mapping to the catalog.")
        self.config["firmware"] = family
        if not self.config.get("ap"):
            if chip == "s5e8835":
                self.config["ap"] = "erd8835"  # Explicitly linked in SLSI!C30.
            else:
                for scope in ("vendor", "system"):
                    try:
                        path = self.resolver.discover("reference", scope, "board_config")
                    except DiscoveryMiss as exc:
                        self.log(f"AP inference skipped optional board: {exc}")
                        continue
                    text, _ = decode(self.content(path))
                    aps = set(re.findall(r"^\s*(?:-?include)\s+device/samsung/([^/\s]+)/BoardConfig[^\s]*", text, re.M))
                    aps.discard(self.config["common_device"])
                    if len(aps) == 1:
                        self.config["ap"] = aps.pop()
                        break
        self.variables = {"chipset": chip, "firmware": family,
                          "efs": "/efs" if self.config["jdm"] else "/mnt/vendor/efs"}
        ref_chip = reference_values.get("WLAN_CHIP", "").strip('"').lower()
        self.log(f"Resolved model inputs: chipset={chip}; AP={self.config.get('ap') or 'unresolved'}; firmware family={family}; HCF variant={self.config.get('hcf_variant') or 'infer from reference'}")
        if ref_chip and ref_chip != chip:
            self.result({"id": "chip.review", "title": "Chipset differs from reference", "source": "SLSI!C6"},
                        "review", f"Input chipset {chip}; reference chipset {ref_chip}. Confirm this hardware change.")

    def build(self):
        self.start_log("normal plan")
        identity = self.p4.identity()
        workspace = self.p4.workspace_spec()
        plan = {"schema_version": 1, "mode": "live", "created_at": datetime.now(timezone.utc).isoformat(),
                "catalog_sha256": digest(canonical(self.catalog)), "source": self.catalog["source"],
                "connection": self.config["perforce"], "identity": identity, "workspace": workspace}
        try:
            self.context()
        except Exception as exc:
            self.result({"id": "setup", "title": "Resolve project inputs", "source": "Inputs / SLSI!C6,C34"}, "blocked", str(exc))
            plan.update(config=self.config, templates=getattr(self, "resolver", None).specs if hasattr(self, "resolver") else {},
                        checks=self.results, snapshots=list(self.snapshots.values()), changes=[], previews=self.previews)
            return seal(plan)
        for source_rule in self.catalog["rules"]:
            rule = substitute(source_rule, self.variables)
            self.rule_paths = []
            # A failed rule must not leave a partial edit in the plan.
            saved_edits = json.loads(json.dumps(self.edits))
            results_start, previews_start = len(self.results), len(self.previews)
            self.log(f"RULE START {rule['id']}: {rule['title']}; scope={rule['scope']}; target={rule['target']}")
            try:
                self.apply_rule(rule)
            except Exception as exc:
                self.edits = saved_edits
                del self.results[results_start:]
                del self.previews[previews_start:]
                status = ("skipped" if exc.role == "reference" else "review") if isinstance(exc, DiscoveryMiss) else "blocked"
                self.result(rule, status, str(exc), self.rule_paths + (exc.paths if isinstance(exc, DiscoveryMiss) else []))
                if isinstance(exc, PerforceTimeout):
                    self.result({"id": "planning.stopped", "title": "Planning stopped", "source": "Perforce connection"},
                                "blocked", "Remaining rules were not run after the request timeout. Fix the reported request and generate a fresh plan.")
                    break
        self.result({"id": "sheet.review", "title": "Review checklist interpretation", "source": "SLSI!B13,B24,B28,C10"},
                    "review", "Checklist settings and command values are selection hints; only matching reference content is used. "
                    "B28 selects the reference post-fs-data event. Explicit JDM selection maps EFS paths to /efs. Product features follow the reference OS; "
                    "review model and regional eligibility against the Feature Flags tab before approving. "
                    "Sample TRUE/FALSE feature values are not forced across models.")
        plan.update(config=self.config, templates=self.resolver.specs, checks=self.results,
                    snapshots=list(self.snapshots.values()), changes=list(self.edits.values()), previews=self.previews)
        self.log(f"END normal plan: {len(self.results)} results; {len(self.edits)} proposed file edits; {len(self.previews)} previews.")
        return seal(plan)

    def apply_rule(self, rule):
        kind, scope, target = rule["kind"], rule["scope"], rule["target"]
        if kind == "manual":
            self.result(rule, "manual", rule["notes"])
            return
        if kind == "carrier_features":
            if not self.config["check_csc_features"]:
                self.result(rule, "skipped", "CSC feature checks are disabled. Select Check CSC features to include them.")
                return
            return self.carrier_features(rule)
        if kind in ("copy_if_reference", "copy_tree_if_reference"):
            source = self.resolver.discover("reference", scope, target, optional=True)
            if not source:
                self.result(rule, "skipped", "Absent in reference; conditional copy does not apply.")
                return
            sources = [r["depotFile"] for r in source] if isinstance(source, list) else [source]
            existing = self.resolver.discover('current', scope, target, optional=True)
            current_folder = None
            if isinstance(existing, list) and existing:
                current_folder = self.resolver.directory_root('current', scope, target, existing)
            source_folder = self.resolver.directory_root('reference', scope, target, source) if current_folder else None
            for src in sources:
                if isinstance(existing, str):
                    dst = existing
                    self.log(f"{rule['id']}: existing current file found by logical role: {dst}; compare it instead of adding a file at the reference filename.")
                elif current_folder:
                    dst = current_folder + src[len(source_folder):]
                    relative_for(self.resolver.views[f'current.{scope}'], dst)
                    self.log(f"{rule['id']}: mapped reference subpath into discovered current Bluetooth folder: {dst}")
                else:
                    dst = self.resolver.counterpart(src, scope)
                self.rule_paths.append(dst)
                source_snapshot = self.snapshot(src)
                target_snapshot = self.snapshot(dst, optional=True)
                content = base64.b64decode(source_snapshot["content"])
                self.preview(rule, dst, content, reference_path=src,
                             note="Complete reference file that would be copied if the current target were absent.")
                if target_snapshot["revision"] is not None:
                    identical = source_snapshot["sha256"] == target_snapshot["sha256"]
                    self.result(rule, "pass" if identical else "review",
                                (f"Current and reference files are byte-for-byte identical (SHA-256 {source_snapshot['sha256']}); no change planned."
                                 if identical else
                                 "Current file differs from reference. This conditional-copy rule will not overwrite an existing file; review both files manually."),
                                [src, dst])
                else:
                    self.propose(dst, content, rule, file_type=source_snapshot["type"])
                    self.result(rule, "change", "Copy missing reference file.", [src, dst])
            return
        if kind == "verify_hcf":
            return self.verify_hcf(rule)
        if kind == "verify_hals":
            return self.verify_reference_hals(rule)
        if kind == "verify_firmware":
            return self.verify_reference_firmware(rule)
        src = self.resolver.discover("reference", scope, target)
        self.rule_paths = [src]
        source_snapshot = self.snapshot(src)
        reference, _ = decode(base64.b64decode(source_snapshot["content"]))
        self.log(f"{rule['id']}: reference source={src}; revision={self.snapshots[src]['revision']}")
        path = self.resolver.discover("current", scope, target)
        self.rule_paths.append(path)
        self.log(f"{rule['id']}: current target={path}")
        text, encoding = decode(self.content(path))
        if kind == "reference_make_settings":
            snapshot = source_snapshot
            if "WLAN_CHIP" in rule["keys"]:
                reference_chip = make_values(reference, "WLAN_").get("WLAN_CHIP", "").strip('"').lower()
                if reference_chip and reference_chip != self.config["chipset"]:
                    raise ValueError("Selected chipset differs from reference WLAN_CHIP; align the input before copying reference board settings")
            selection_notes = []
            def selection_report(message):
                self.log(message)
                selection_notes.append(message)
            empty_preview = copy_make_settings("", reference, rule["keys"], rule.get("include_basenames", []),
                                               rule.get("key_patterns", []), report=selection_report)
            if not empty_preview:
                self.result(rule, "skipped", "Reference contains no selected board settings or includes; current content is preserved.", [src, path])
                return
            self.preview(rule, path, empty_preview, reference_path=src,
                         note="Selected reference statements that would be placed into an empty target file.")
            after = copy_make_settings(text, reference, rule["keys"], rule.get("include_basenames", []),
                                       rule.get("key_patterns", []), report=self.log)
            changed = self.propose(path, after.encode(encoding), rule)
            self.result(rule, "change" if changed else "pass",
                        (f"Selected statements differ; proposed values, operators and include path come from reference #{snapshot['revision']}."
                         if changed else
                         f"Selected Bluetooth/WLAN statements and include already match reference #{snapshot['revision']}; no change planned. Other file content was not required to match.") + "\n" + "\n".join(selection_notes),
                        [src, path])
            return
        if kind == "reference_features":
            selected = name_selector(rule["prefix"], rule.get("key_patterns", []))
            if rule["format"] == "make":
                values = make_values(reference, rule["prefix"], rule.get("key_patterns", []))
                current = make_values(text, rule["prefix"], rule.get("key_patterns", []))
                after = transform(text, {"type": "assignments", "values": values, "operator": "="},
                                  report=self.presence_report(rule, path)) if values else text
                empty_preview = transform("", {"type": "assignments", "values": values, "operator": "="}) if values else ""
            else:
                root = ET.fromstring(reference)
                elements = [e for e in root.iter() if selected(e.tag)]
                values = {e.tag: e.text or "" for e in elements}
                if len(values) != len(elements) or any(len(e) for e in elements):
                    raise ValueError("Duplicate/non-leaf reference feature XML elements")
                current_root = ET.fromstring(text)
                current = {e.tag: e.text for e in current_root.iter() if selected(e.tag)}
                after = transform(text, {"type": "xml_elements", "elements": values, "parent": current_root.tag},
                                  report=self.presence_report(rule, path)) if values else text
                empty_preview = transform(f"<{current_root.tag}></{current_root.tag}>\n",
                                          {"type": "xml_elements", "elements": values, "parent": current_root.tag}) if values else ""
            if not values:
                self.result(rule, "review", "Reference has no matching Bluetooth feature values; inspect feature selection.", [src, path])
                return
            self.preview(rule, path, empty_preview, reference_path=src,
                         note="Reference Bluetooth feature values that would be used for an empty target.")
            changed = self.propose(path, after.encode(encoding), rule)
            extras = sorted(set(current) - set(values))
            self.result(rule, "change" if changed else "pass", f"Compared {len(values)} reference Bluetooth flags.", [src, path])
            if extras:
                self.result(rule, "review", "Current-only flags preserved for review: " + ", ".join(extras), [path])
            return
        if kind != "transform":
            raise ValueError(f"Unknown rule kind: {kind}")
        actions = self.reference_actions(rule, reference)
        after, empty_preview = text, ""
        presence_start = len(self.results)
        for action in actions:
            after = apply_reference_action(after, action, report=self.presence_report(rule, path))
            empty_preview = apply_reference_action(empty_preview, action)
        if not empty_preview:
            self.result(rule, "skipped", "Reference has no matching statements for this rule. Checklist examples are optional; nothing is inserted.", [src, path])
            return
        self.preview(rule, path, empty_preview, reference_path=src,
                     note="Bluetooth content selected from the reference; checklist examples only guide selection.")
        changed = self.propose(path, after.encode(encoding), rule)
        review_presence = any(item['status'] == 'review' for item in self.results[presence_start:])
        self.result(rule, "change" if changed else ("review" if review_presence else "pass"),
                    ("Reference-selected edit proposed; existing entries were checked across the current file." if changed else
                     "No edit proposed; existing content at other locations needs review as detailed above." if review_presence else
                     "Already matches selected reference content."), [src, path])

    def verify_reference_hals(self, rule):
        reference_path = self.resolver.discover("reference", "vendor", "manifest")
        self.rule_paths = [reference_path]
        reference_root = ET.fromstring(decode(self.content(reference_path))[0])
        selected_name = name_selector("", rule.get("name_patterns", []))
        hints = {hal["name"] for hal in rule.get("expected_hals", [])}
        reference_hals = [hal for hal in reference_root.findall(".//hal")
                          if selected_name(hal.findtext("name") or "") or hal.findtext("name") in hints]
        found_names = {hal.findtext("name") for hal in reference_hals}
        for name in sorted(hints - found_names):
            self.log(f"{rule['id']}: checklist HAL {name} is absent in reference; no entry/version is required for it.")
        if not reference_hals:
            self.result(rule, "skipped", "Reference manifest contains no selected Bluetooth HIDL/AIDL entries; checklist HAL examples are not mandatory.", [reference_path])
            return
        path = self.resolver.discover("current", "vendor", "manifest")
        self.rule_paths.append(path)
        root = ET.fromstring(decode(self.content(path))[0])
        for reference_hal in reference_hals:
            name = reference_hal.findtext("name")
            reference_matches = [hal for hal in reference_hals if hal.findtext("name") == name]
            matches = [hal for hal in root.findall(".//hal") if hal.findtext("name") == name]
            if len(reference_matches) != 1 or len(matches) > 1:
                raise ValueError(f"Ambiguous Bluetooth HAL {name}: reference count={len(reference_matches)}, current count={len(matches)}; cannot choose an entry.")
            if not matches:
                self.result(rule, "review", f"Reference HAL {name} ({reference_hal.get('format')}) is absent in current manifest. Verification only; no XML was inserted.", [reference_path, path])
                continue
            current = matches[0]
            def pairs(hal):
                return {(i.findtext("name"), instance.text) for i in hal.findall("interface") for instance in i.findall("instance")}
            versions = [v.text.strip() for v in reference_hal.findall("version") if v.text]
            actual = [v.text.strip() for v in current.findall("version") if v.text]
            differences = []
            if reference_hal.get("format") != current.get("format"):
                differences.append(f"format: reference={reference_hal.get('format')}, current={current.get('format')}")
            if reference_hal.findtext("transport") != current.findtext("transport"):
                differences.append(f"transport: reference={reference_hal.findtext('transport')}, current={current.findtext('transport')}")
            if any(not any(hidl_version_at_least(value, minimum) for value in actual) for minimum in versions):
                differences.append(f"versions: reference minimums={versions}, current={actual}")
            if not pairs(reference_hal).issubset(pairs(current)):
                differences.append(f"interfaces/instances missing from current: {sorted(pairs(reference_hal) - pairs(current))}")
            expected_fq = {(item.text or "").strip() for item in reference_hal.findall("fqname")}
            current_fq = {(item.text or "").strip() for item in current.findall("fqname")}
            if not expected_fq.issubset(current_fq):
                differences.append(f"reference fqname entries missing from current: {sorted(expected_fq - current_fq)}")
            self.result(rule, "review" if differences else "pass",
                        f"Reference {reference_hal.get('format', 'unspecified')} HAL {name}: " +
                        ("; ".join(differences) if differences else "format, transport, interfaces, fqnames and reference minimum versions match."), [reference_path, path])

    def verify_reference_firmware(self, rule):
        reference = self.resolver.discover("reference", "vendor", "firmware")
        self.rule_paths = [reference]
        source = self.snapshot(reference)
        path = self.resolver.discover("current", "vendor", "firmware")
        self.rule_paths.append(path)
        current = self.snapshot(path)
        expected = self.config.get("firmware_sha256", "").lower()
        if expected and expected != current["sha256"]:
            raise ValueError(f"Firmware {path}#{current['revision']} does not match explicitly supplied approved SHA-256 {expected}; actual={current['sha256']}")
        self.result(rule, "pass" if expected else "review",
                    f"Current firmware {path}#{current['revision']}, SHA-256 {current['sha256']}; "
                    f"reference {reference}#{source['revision']}, SHA-256 {source['sha256']}. " +
                    ("Bytes match reference. " if current['sha256'] == source['sha256'] else "Bytes differ from reference. ") +
                    ("Matches supplied approved hash." if expected else "Verification only; confirm release suitability before replacing firmware."), [reference, path])

    def current_hcf(self, rule):
        records = self.resolver.discover("current", "vendor", "hcf")
        hcf = [record['depotFile'] for record in records if record['depotFile'].lower().endswith('.hcf')]
        self.rule_paths = list(hcf)
        if not hcf:
            self.result(rule, "review", "Couldn't find .hcf files in current. Searched: " +
                        "; ".join(self.resolver.attempts.get("current.vendor.hcf", [])) +
                        ". Other files found: " + ", ".join(record['depotFile'] for record in records))
            return None
        # Existence metadata is sufficient; never read or copy HCF binaries.
        for path in hcf:
            self.log(f"{rule['id']}: current HCF file exists: {path}; existence check only.")
        folder = self.resolver.directory_root('current', 'vendor', 'hcf', records)
        parent_mk = folder.rsplit('/', 1)[0] + '/bluetooth.mk'
        self.log(f"{rule['id']}: bluetooth.mk is one directory above HCF model folder: {parent_mk}")
        current_mk = self.resolver.discover('current', 'vendor', 'hcf_makefile', exact_path=parent_mk)
        self.rule_paths.append(current_mk)
        return current_mk, hcf

    def reference_hcf(self, rule, current_mk):
        # Translate the actual current Makefile's build path through the
        # reference View, so Cinnamon/Common depot layouts resolve separately.
        try:
            relative = relative_for(self.resolver.views['current.vendor'], current_mk)
            exact = translate_path(self.resolver.views['reference.vendor'], relative)
        except MappingError:
            exact = None
        reference_mk = self.resolver.discover("reference", "vendor", "hcf_makefile", optional=True, exact_path=exact)
        if not reference_mk:
            self.result(rule, "review", "Current HCF files exist. Reference bluetooth.mk was not found, so its TARGET_PRODUCT filter could not be compared. Searched: " +
                        "; ".join(self.resolver.attempts.get('reference.vendor.hcf_makefile', [])), self.rule_paths)
            return None
        self.rule_paths.append(reference_mk)
        reference_text, _ = decode(self.content(reference_mk))
        if not hcf_blocks(reference_text):
            self.result(rule, "pass", "Current HCF files exist. Reference bluetooth.mk has no recognized HCF TARGET_PRODUCT copy block; no filter change is suggested.", self.rule_paths)
            return None
        selected = select_hcf_block(reference_text, self.config)
        self.log(f"{rule['id']}: reference Make filter: folder={selected['variant']}; TARGET_PRODUCT={selected['products']}; reference HCF files are not queried.")
        return reference_mk, selected

    def verify_hcf(self, rule):
        current = self.current_hcf(rule)
        if current is None:
            return
        current_mk, hcf = current
        source = self.reference_hcf(rule, current_mk)
        if source is None:
            return
        reference_mk, selected = source
        current_text, current_encoding = decode(self.content(current_mk))

        variant = selected["variant"]

        current_candidates = [item for item in hcf_blocks(current_text) if item["variant"] == variant]
        if len(current_candidates) > 1:
            raise ValueError(f"Current bluetooth.mk has duplicate HCF filter blocks for {variant}")
        nl = "\r\n" if "\r\n" in current_text else "\n"
        reference_block = selected["text"].replace("\r\n", "\n").replace("\n", nl)
        if current_candidates:
            current_block = current_candidates[0]
            needs_filter_update = current_block["products"] != selected["products"]
            updated_mk = (current_text[:current_block["start"]] + reference_block +
                          current_text[current_block["end"]:]) if needs_filter_update else current_text
        else:
            if f"$(HCF_PATH)/{variant}" in current_text:
                raise ValueError(f"Current bluetooth.mk mentions {variant} in an unrecognized block; review manually")
            separator = "" if not current_text else ("" if current_text.endswith(("\n", "\r")) else nl) + nl
            updated_mk = current_text + separator + reference_block + nl
        if not any('/' + variant + '/' in path for path in hcf):
            self.result(rule, 'review', f"Current HCF search found files at another model folder, but the reference filter uses {variant}. "
                        "Found paths are listed below. No duplicate/missing-folder filter was added; review the variant and TARGET_PRODUCT mapping against the reference.",
                        [reference_mk, current_mk, *hcf])
            return
        filter_changed = self.propose(current_mk, updated_mk.encode(current_encoding), rule)
        evidence = ("Resolved HCF folder: " + variant +
                    "; TARGET_PRODUCT values: " + " ".join(selected["products"]) +
                    ". Verified HCF files: " + ", ".join(hcf))
        self.preview(rule, current_mk, selected["text"] + "\n", reference_path=reference_mk,
                     note="Reference bluetooth.mk filter block that current must contain. " + evidence)
        self.result(rule, "change" if filter_changed else "pass",
                    ("Reference HCF filter block will be added/updated. " if filter_changed else "Reference HCF filter block already matches. ") +
                    f"Folder {variant} contains {len(hcf)} .hcf file(s); products: " +
                    ", ".join(self.config["products"]), [reference_mk, current_mk, *hcf])

    def report_carrier_discovery(self, rule, discovery):
        if not discovery["matched_files"]:
            self.result(rule, "review", "Reference CSC discovery: " + missing_carrier_message(discovery))
        for region in discovery["skipped_regions"]:
            files = region["files"]
            self.result(rule, "skipped", f"No {CARRIER_FILENAME} anywhere under this reference region "
                        "after searching its entire subtree, including system/; left untouched. "
                        "Files found: " + ", ".join(f"{file['filename']} ({file['type']})" for file in files),
                        [region["path"], *[file["path"] for file in files]])

    def carrier_features(self, rule):
        current_root = model_root(self.config["current"]["csc_path"], self.config["model"])
        reference_root = model_root(self.config["reference"]["csc_path"], self.config["model"])
        current = carrier_files(self.p4, current_root)
        discovery = {}
        reference = carrier_files(self.p4, reference_root, details=discovery)
        self.report_carrier_discovery(rule, discovery)
        for relative, src in reference.items():
            dst = current_root + "/" + relative
            self.rule_paths.append(dst)
            source = self.snapshot(src)
            before = self.snapshot(dst, optional=True)
            content = base64.b64decode(source["content"])
            reference_json = carrier_json(decode(content)[0])
            self.preview(rule, dst, content, reference_path=src,
                         note="Reference keys for a blank JSON file; existing target values are preserved during normal planning.")
            if before["revision"] is None:
                changed = self.propose(dst, content, rule, file_type=source["type"])
                message = "Add missing carrier JSON at the same model-relative path: " + relative
            else:
                target_text, encoding = decode(self.content(dst))
                merged, added = add_missing_features(reference_json, carrier_json(target_text))
                changed = False
                if added:
                    newline = "\r\n" if "\r\n" in target_text else "\n"
                    after = (json.dumps(merged, indent=2, ensure_ascii=False) + "\n").replace("\n", newline)
                    changed = self.propose(dst, after.encode(encoding), rule)
                message = f"{relative}: added {len(added)} missing keys; existing values kept."
            self.result(rule, "change" if changed else "pass",
                        message, [src, dst])
        for relative in sorted(current.keys() - reference.keys()):
            self.result(rule, "review", "Current-only carrier file has no same-region reference; left untouched: " + relative,
                        [current[relative]])


def save_plan(plan, directory):
    verify_seal(plan)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    output = directory / "plan.json"
    output.write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")
    (directory / "plan.txt").write_text(summary(plan), encoding="utf-8")
    (directory / "empty-file-previews.txt").write_text(preview_summary(plan), encoding="utf-8")
    return output


def summary(plan):
    counts = {status: sum(c["status"] == status for c in plan["checks"]) for status in ("pass", "change", "blocked", "review", "manual", "skipped")}
    lines = ["SLSI Bluetooth delta plan", f"Plan: {plan['digest']}", f"Mode: {plan['mode']}",
             f"Source: {plan['source']}", f"Files to change: {len(plan['changes'])}; checks: {counts}",
             ("Inspection-only comparison plan; cannot be approved or applied. Basis: " + plan.get("comparison_basis", "")
              if plan["mode"] == "comparison" else
              "Approval applies only to these exact changes. BLOCKED checks stay untouched and are reported after apply. Creates a pending changelist; never shelves or submits."), ""]
    for key in ("current", "reference"):
        lines.append(f"{key}: {json.dumps(plan['config'][key])}")
    lines.append("Resolved model settings: " + json.dumps({k: plan["config"].get(k) for k in ("model", "chipset", "firmware", "ap", "jdm", "hcf_variant", "products")}))
    for check in plan["checks"]:
        lines.extend(["", f"[{check['status'].upper()}] {check['title']} ({check['source']})", check["message"], *check["paths"]])
    for change in plan["changes"]:
        lines.extend(["", "=" * 72, f"{'ADD' if change['revision'] is None else 'EDIT'} {change['path']}",
                      f"Workspace file: {change['local_path']}" if change['local_path'] else "Comparison only; no local workspace target required.",
                      "Checklist: " + ", ".join(change["sources"]), change["diff"]])
    return "\n".join(lines) + "\n"


def preview_summary(plan):
    lines = ["Empty-file and reference-source previews",
             "Inspection only: these previews are not workspace files and are never applied directly.",
             "Each section shows what one rule sees or would construct if its current target were empty."]
    previews = plan.get("previews", [])
    if not previews:
        lines.extend(["", "No preview content was available for this plan."])
    for item in previews:
        lines.extend(["", "=" * 80,
                      f"{item['title']} ({item['source']})",
                      f"Target: {item['target_path']}"])
        if item.get("reference_path"):
            lines.append(f"Reference: {item['reference_path']}")
        if item.get("note"):
            lines.append("Meaning: " + item["note"])
        lines.extend(["-" * 80, item.get("content", "")])
    return "\n".join(lines).rstrip() + "\n"
