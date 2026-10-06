"""Read-only planning, revision snapshots, explicit checks and exact file diffs."""
from __future__ import annotations

import base64
import difflib
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from .catalog import default_catalog
from .config import validate
from .resolver import Resolver, relative_for
from .transforms import TransformError, transform
from .reference import augment_actions_from_reference, copy_make_settings
from .perforce import PerforceTimeout


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
        self.config = validate(config)
        catalog_file = Path(__file__).resolve().parent.parent / "checklist" / "slsi.json"
        self.catalog = catalog if catalog is not None else (json.loads(catalog_file.read_text(encoding="utf-8")) if catalog_file.exists() else default_catalog())
        self.snapshots = {}
        self.edits = {}
        self.results = []
        self.previews = []
        self.rule_paths = []

    def preview(self, rule, target_path, content, *, reference_path=None, note=""):
        """Record inspection-only content; it is never passed to the executor."""
        if isinstance(content, bytes):
            try:
                content, _ = decode(content)
            except UnicodeDecodeError:
                content = f"<binary content: {len(content)} bytes; SHA-256 {digest(content)}>"
        self.previews.append({"rule": rule["id"], "title": rule["title"],
                              "source": rule["source"], "target_path": target_path,
                              "reference_path": reference_path, "note": note,
                              "content": content})

    def result(self, rule, status, message, paths=None):
        self.results.append({"rule": rule["id"], "title": rule["title"], "source": rule["source"],
                             "status": status, "message": message, "paths": paths or []})

    def snapshot(self, path, *, optional=False):
        if path in self.snapshots:
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
            return False
        if path not in self.edits:
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
        reference_board = self.resolver.discover("reference", "system", "board_config")
        reference_text, _ = decode(self.content(reference_board))
        reference_values = make_values(reference_text, "WLAN_")
        if reference_values.get("WLAN_VENDOR", "").strip('"') != "8":
            raise ValueError("Reference WLAN_VENDOR is not SLSI (8)")
        if not self.config.get("chipset"):
            values = reference_values
            if values.get("WLAN_VENDOR", "").strip('"') != "8":
                raise ValueError("Reference WLAN_VENDOR is not SLSI (8); choose an SLSI reference")
            chip = values.get("WLAN_CHIP", "").strip('"').lower()
            if not re.fullmatch(r"[a-z0-9_]+", chip):
                raise ValueError("Cannot infer WLAN_CHIP; enter chipset explicitly")
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
                    path = self.resolver.discover("reference", scope, "board_config")
                    text, _ = decode(self.content(path))
                    aps = set(re.findall(r"^\s*(?:-?include)\s+device/samsung/([^/\s]+)/BoardConfig[^\s]*", text, re.M))
                    aps.discard(self.config["common_device"])
                    if len(aps) == 1:
                        self.config["ap"] = aps.pop()
                        break
        self.variables = {"chipset": chip, "firmware": family,
                          "efs": "/efs" if self.config["jdm"] else "/mnt/vendor/efs"}
        ref_chip = reference_values.get("WLAN_CHIP", "").strip('"').lower()
        if ref_chip != chip:
            self.result({"id": "chip.review", "title": "Chipset differs from reference", "source": "SLSI!C6"},
                        "review", f"Input chipset {chip}; reference chipset {ref_chip}. Confirm this hardware change.")

    def build(self):
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
            results_start = len(self.results)
            try:
                self.apply_rule(rule)
            except Exception as exc:
                self.edits = saved_edits
                del self.results[results_start:]
                self.result(rule, "blocked", str(exc), self.rule_paths)
                if isinstance(exc, PerforceTimeout):
                    self.result({"id": "planning.stopped", "title": "Planning stopped", "source": "Perforce connection"},
                                "blocked", "Remaining rules were not run after the request timeout. Fix the reported request and generate a fresh plan.")
                    break
        self.result({"id": "sheet.review", "title": "Review checklist interpretation", "source": "SLSI!B13,B24,B28,C10"},
                    "review", "Sheet literally uses 'chown bluetooth bluetooth ro.bt.bdaddr_path'. Review that entry. "
                    "B28 omits the event; post-fs-data is inferred from B12. Product/floating features follow the reference OS; "
                    "review model and regional eligibility against the Feature Flags tab before approving. "
                    "Sample TRUE/FALSE feature values are not forced across models.")
        plan.update(config=self.config, templates=self.resolver.specs, checks=self.results,
                    snapshots=list(self.snapshots.values()), changes=list(self.edits.values()), previews=self.previews)
        return seal(plan)

    def apply_rule(self, rule):
        kind, scope, target = rule["kind"], rule["scope"], rule["target"]
        if kind == "manual":
            self.result(rule, "manual", rule["notes"])
            return
        if kind == "carrier_features":
            return self.carrier_features(rule)
        if kind in ("copy_if_reference", "copy_tree_if_reference"):
            source = self.resolver.discover("reference", scope, target, optional=True)
            if not source:
                self.result(rule, "skipped", "Absent in reference; conditional copy does not apply.")
                return
            sources = [r["depotFile"] for r in source] if isinstance(source, list) else [source]
            for src in sources:
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
        path = self.resolver.discover("current", scope, target)
        self.rule_paths = [path]
        if kind == "verify_firmware":
            snap = self.snapshot(path)
            expected = self.config.get("firmware_sha256", "").lower()
            if expected and expected != snap["sha256"]:
                raise ValueError(f"Firmware does not match approved SHA-256: {snap['sha256']}")
            self.result(rule, "pass" if expected else "review",
                        f"Firmware #{snap['revision']}, SHA-256 {snap['sha256']}. " +
                        ("Matches supplied approved release hash." if expected else "Confirm latest approved firmware with its owner; depot head alone does not prove latest approved release."), [path])
            return
        text, encoding = decode(self.content(path))
        if kind == "reference_make_settings":
            src = self.resolver.discover("reference", scope, target)
            snapshot = self.snapshot(src)
            reference, _ = decode(base64.b64decode(snapshot["content"]))
            if "WLAN_CHIP" in rule["keys"]:
                reference_chip = make_values(reference, "WLAN_").get("WLAN_CHIP", "").strip('"').lower()
                if reference_chip != self.config["chipset"]:
                    raise ValueError("Selected chipset differs from reference WLAN_CHIP; align the input before copying reference board settings")
            empty_preview = copy_make_settings("", reference, rule["keys"], rule.get("include_basenames", []),
                                               rule.get("key_patterns", []))
            self.preview(rule, path, empty_preview, reference_path=src,
                         note="Selected reference statements that would be placed into an empty target file.")
            after = copy_make_settings(text, reference, rule["keys"], rule.get("include_basenames", []),
                                       rule.get("key_patterns", []))
            changed = self.propose(path, after.encode(encoding), rule)
            self.result(rule, "change" if changed else "pass",
                        (f"Selected statements differ; proposed values, operators and include path come from reference #{snapshot['revision']}."
                         if changed else
                         f"Selected Bluetooth/WLAN statements and include already match reference #{snapshot['revision']}; no change planned. Other file content was not required to match."),
                        [src, path])
            return
        if kind == "verify_hals":
            root = ET.fromstring(text)
            checked_names = set()
            for expected in rule["expected_hals"]:
                matches = [h for h in root.findall(".//hal") if h.findtext("name") == expected["name"]]
                if len(matches) != 1:
                    raise ValueError(f"Expected exactly one {expected['name']} HAL")
                hal = matches[0]
                pairs = {(i.findtext("name"), instance.text) for i in hal.findall("interface") for instance in i.findall("instance")}
                versions = [v.text for v in hal.findall("version") if v.text]
                if (hal.get("format") != "hidl" or hal.findtext("transport") != "hwbinder"
                        or not any(hidl_version_at_least(version, expected["version"]) for version in versions)
                        or (expected["interface"], "default") not in pairs):
                    raise ValueError(f"HAL differs from checklist: {expected['name']}; review actual manifest")
                checked_names.add(expected["name"])
            dynamic_names = []
            if rule.get("name_patterns"):
                reference_path = self.resolver.discover("reference", scope, target)
                reference_text, _ = decode(base64.b64decode(self.snapshot(reference_path)["content"]))
                reference_root = ET.fromstring(reference_text)
                selected_name = name_selector("", rule["name_patterns"])
                for reference_hal in reference_root.findall(".//hal"):
                    name = reference_hal.findtext("name") or ""
                    if not selected_name(name) or name in checked_names:
                        continue
                    reference_matches = [h for h in reference_root.findall(".//hal") if h.findtext("name") == name]
                    current_matches = [h for h in root.findall(".//hal") if h.findtext("name") == name]
                    if len(reference_matches) != 1 or len(current_matches) != 1:
                        raise ValueError(f"Reference-selected Bluetooth HAL must occur exactly once in both manifests: {name}")
                    current_hal = current_matches[0]
                    reference_versions = [v.text for v in reference_hal.findall("version") if v.text]
                    current_versions = [v.text for v in current_hal.findall("version") if v.text]
                    reference_pairs = {(i.findtext("name"), instance.text)
                                       for i in reference_hal.findall("interface") for instance in i.findall("instance")}
                    current_pairs = {(i.findtext("name"), instance.text)
                                     for i in current_hal.findall("interface") for instance in i.findall("instance")}
                    if (reference_hal.get("format") != current_hal.get("format") or
                            reference_hal.findtext("transport") != current_hal.findtext("transport") or
                            any(not any(hidl_version_at_least(actual, minimum) for actual in current_versions)
                                for minimum in reference_versions) or
                            not reference_pairs.issubset(current_pairs)):
                        raise ValueError(f"Reference-selected Bluetooth HAL differs in current manifest: {name}")
                    dynamic_names.append(name)
                    checked_names.add(name)
            self.result(rule, "pass",
                        f"Required static HIDL entries, interfaces and minimum versions match; {len(dynamic_names)} additional reference-selected Bluetooth HAL(s) match.",
                        [*( [reference_path] if rule.get("name_patterns") else []), path])
            return
        if kind == "reference_features":
            src = self.resolver.discover("reference", scope, target)
            reference, _ = decode(base64.b64decode(self.snapshot(src)["content"]))
            selected = name_selector(rule["prefix"], rule.get("key_patterns", []))
            if rule["format"] == "make":
                values = make_values(reference, rule["prefix"], rule.get("key_patterns", []))
                current = make_values(text, rule["prefix"], rule.get("key_patterns", []))
                after = transform(text, {"type": "assignments", "values": values, "operator": "="}) if values else text
                empty_preview = transform("", {"type": "assignments", "values": values, "operator": "="}) if values else ""
            else:
                root = ET.fromstring(reference)
                elements = [e for e in root.iter() if selected(e.tag)]
                values = {e.tag: e.text or "" for e in elements}
                if len(values) != len(elements) or any(len(e) for e in elements):
                    raise ValueError("Duplicate/non-leaf reference floating feature elements")
                current_root = ET.fromstring(text)
                current = {e.tag: e.text for e in current_root.iter() if selected(e.tag)}
                after = transform(text, {"type": "xml_elements", "elements": values, "parent": current_root.tag}) if values else text
                empty_preview = "\n".join(f"<{key}>{value}</{key}>" for key, value in values.items()) + ("\n" if values else "")
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
        actions = rule["actions"]
        reference_path = None
        if any(any(key.startswith("reference_") for key in action) for action in actions):
            reference_path = self.resolver.discover("reference", scope, target)
            reference_text, _ = decode(base64.b64decode(self.snapshot(reference_path)["content"]))
            actions = augment_actions_from_reference(reference_text, actions)
        after = text
        empty_preview = ""
        for action in actions:
            after = transform(after, action)
            empty_preview = transform(empty_preview, action)
        self.preview(rule, path, empty_preview, reference_path=reference_path,
                     note=("Static checklist content plus Bluetooth-related statements discovered in the corresponding reference file."
                           if reference_path else
                           "Checklist-derived content that would be generated for an empty target; no reference file is used by this rule."))
        changed = self.propose(path, after.encode(encoding), rule)
        self.result(rule, "change" if changed else "pass", "Checklist edit proposed." if changed else "Already matches checklist.", [path])

    def verify_hcf(self, rule):
        reference_mk = self.resolver.discover("reference", "vendor", "hcf_makefile")
        current_mk = self.resolver.discover("current", "vendor", "hcf_makefile")
        self.rule_paths = [current_mk]
        reference_text, _ = decode(self.content(reference_mk))
        current_text, current_encoding = decode(self.content(current_mk))

        selected = select_hcf_block(reference_text, self.config)
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
        filter_changed = self.propose(current_mk, updated_mk.encode(current_encoding), rule)

        records = self.resolver.discover("current", "vendor", "hcf")
        hcf = [r["depotFile"] for r in records if r["depotFile"].lower().endswith(".hcf")]
        if not hcf:
            raise ValueError(f"No .hcf file found in chipset/model folder {self.config['chipset']}/{variant}")
        for path in hcf:
            self.snapshot(path)
        evidence = (selected["text"] + "\n\nResolved HCF folder: " + variant +
                    "\nTARGET_PRODUCT values: " + " ".join(selected["products"]) +
                    "\nHCF files:\n" + "\n".join(hcf) + "\n")
        self.preview(rule, current_mk, evidence, reference_path=reference_mk,
                     note="Reference bluetooth.mk filter block that current must contain, plus the exact current HCF files found.")
        self.result(rule, "change" if filter_changed else "pass",
                    ("Reference HCF filter block will be added/updated. " if filter_changed else "Reference HCF filter block already matches. ") +
                    f"Folder {variant} contains {len(hcf)} .hcf file(s); products: " +
                    ", ".join(self.config["products"]), [reference_mk, current_mk, *hcf])

    def carrier_features(self, rule):
        current_root = self.config["current"]["csc_path"]
        reference_root = self.config["reference"]["csc_path"]
        selected_key = name_selector(rule.get("prefix", "CarrierFeature_BT_"), rule.get("key_patterns", []))
        def listing(root):
            records = self.p4.files(root + "/.../customer_carrier_feature_plain.json")
            return {r["depotFile"][len(root) + 1:]: r["depotFile"] for r in records}
        current, reference = listing(current_root), listing(reference_root)
        if not current and not reference:
            self.result(rule, "review", "No carrier feature JSON found under either supplied CSC path.")
            return
        for relative in sorted(set(current) | set(reference)):
            if relative not in current or relative not in reference:
                self.result(rule, "review", f"No same-region counterpart for {relative}; no cross-region copy.")
                continue
            src, dst = reference[relative], current[relative]
            self.rule_paths.append(dst)
            source_text, _ = decode(base64.b64decode(self.snapshot(src)["content"]))
            target_text, encoding = decode(self.content(dst))
            def no_duplicates(pairs):
                d = {}
                for k, v in pairs:
                    if k in d:
                        raise ValueError(f"Duplicate JSON key: {k}")
                    d[k] = v
                return d
            source = json.loads(source_text, object_pairs_hook=no_duplicates)
            bt_values = []
            def collect_bt(value, trail=""):
                if isinstance(value, dict):
                    for key, child in value.items():
                        here = f"{trail}/{key}" if trail else key
                        if selected_key(key):
                            bt_values.append(f"{here} = {json.dumps(child, ensure_ascii=False)}")
                        elif isinstance(child, (dict, list)):
                            collect_bt(child, here)
                elif isinstance(value, list):
                    for index, child in enumerate(value):
                        collect_bt(child, f"{trail}[{index}]")
            collect_bt(source)
            self.preview(rule, dst, "\n".join(bt_values) + ("\n" if bt_values else ""),
                         reference_path=src,
                         note="Bluetooth carrier values and their JSON locations found in this same-region reference file.")
            target = json.loads(target_text, object_pairs_hook=no_duplicates)
            changes = []
            def visit(a, b, trail=""):
                if isinstance(a, dict) and isinstance(b, dict):
                    for key, value in a.items():
                        if selected_key(key):
                            if b.get(key) != value:
                                b[key] = value
                                changes.append(trail + key)
                        elif isinstance(value, (dict, list)):
                            if key not in b:
                                raise ValueError(f"Missing JSON structure at {trail + key}; no automatic structural copy")
                            visit(value, b[key], trail + key + "/")
                elif isinstance(a, list) and isinstance(b, list):
                    def has_bt(value):
                        if isinstance(value, dict):
                            return any(selected_key(k) or has_bt(v) for k, v in value.items())
                        return isinstance(value, list) and any(has_bt(v) for v in value)
                    if has_bt(a) or has_bt(b):
                        raise ValueError("BT features inside JSON arrays require explicit keyed mapping; positions may differ")
                elif isinstance(a, (dict, list)):
                    raise ValueError("Regional JSON structures differ")
            visit(source, target)
            if changes:
                # Full JSON formatting is shown in the saved diff for approval.
                nl = "\r\n" if "\r\n" in target_text else "\n"
                after = (json.dumps(target, indent=2, ensure_ascii=False) + "\n").replace("\n", nl)
                self.propose(dst, after.encode(encoding), rule)
            self.result(rule, "change" if changes else "pass", f"Same-region comparison: {relative}; updated {len(changes)} BT keys.", [src, dst])


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
