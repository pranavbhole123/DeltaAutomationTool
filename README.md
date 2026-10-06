# SLSI Bluetooth Delta

A separate Python 3.10+ tool based on the supplied SLSI checklist. The old `src` and CSV rules are not imported. All new code is in this folder. No Python packages need installing; the desktop interface uses Tkinter. Live operation requires the Perforce command-line client and an existing logged-in, mapped workspace.

## Run

Double-click `start.bat`, or run `python main.py` from this folder.

1. Enter current and reference system/vendor template names, and both CSC depot paths. Your M36X example is prefilled. You can paste the original `C OS:` / `Reference ...` format.
2. Enter Perforce server, user and workspace. Authenticate separately using your normal `p4 login`; the tool does not store passwords or log in automatically.
3. Enter the HCF folder and the intended `TARGET_PRODUCT` names. Chipset is read from the reference BoardConfig unless supplied. AP is read from its include, with the sheet's explicit s5e8835 → erd8835 mapping supported. Select JDM if applicable.
4. Generate a plan. This uses only read operations on Perforce and writes the plan locally.
5. Review the exact file diffs, reference revisions, source cells, REVIEW items, and BLOCKED items. Tick the review checkbox, then click **Apply reviewed changes to pending changelist**. Planned changes are applied; BLOCKED items are left untouched and shown again after execution for manual follow-up.

The result is a dedicated **pending** changelist. Nothing is submitted automatically. Build and perform the phone checks listed in the plan afterwards.

Try **Offline demo** to inspect the interface without credentials. Demo data is synthetic, and demo plans cannot be applied. It does not establish real M36X chipset or firmware facts.

## Command line

The GUI opens a **Live log** tab while working. It shows each Perforce request and its duration. Both GUI and CLI append diagnostics to `new/runtime.log`. Requests time out after 30 seconds by default; a timeout stops remaining planning checks. JSON connection setting `timeout_seconds` can override this for a known slow server. File discovery uses target-path suffixes instead of scanning every matching basename in a branch.

```powershell
python main.py demo --out reports/demo
python main.py plan examples/m36x.json --out reports/my-review
python main.py apply reports/my-review/plan.json --approve FULL_SHA256_FROM_PLAN --acknowledge-reviews
python -m unittest discover -s tests -v
```

Fill connection and model-specific fields in your own copy of `examples/m36x.json`. `--acknowledge-reviews` explicitly confirms review of every REVIEW item. A digest mismatch or any BLOCKED item prevents execution. Inputs changing after GUI planning disable approval until a matching plan is generated.

Each report contains `plan.txt` (readable summary and unified diffs), `empty-file-previews.txt` (file-wise reference/checklist content the rule would use for an empty target), and `plan.json` (exact content, hashes, revisions, input and mapping snapshots). The GUI colors added diff lines green and removed lines red, and shows the same inspection-only material in **4. Empty-file preview**. Execution creates a separate journal with the changelist number, per-file progress, and acknowledged blocked checks. It creates a pending changelist but never shelves or submits it. Report files contain depot content, so keep them with your project data.

Rules use a static checklist baseline plus editable reference selectors. Static keys, commands, packages, includes, and HAL entries remain required. `key_patterns`, `reference_package_patterns`, `reference_line_patterns`, `reference_command_patterns`, `reference_comment_patterns`, and HAL `name_patterns` add Bluetooth-related statements found in the corresponding reference file. Dynamic Make selections are limited to active unconditional statements; conditional or duplicate selections are blocked for review. Init discovery stays inside the configured event and can use comments such as `# Bluetooth`, `# BT`, and `# A2DP` to identify model-specific command blocks. Edit these selectors in `checklist/slsi.json` when naming conventions change.

## Compare bring-up changelists with the blank plan

Open **6. Changelist comparison**, enter one or more developer changelists separated by commas or spaces (for example `123456, 123457, 123458`), and set the current/reference system and vendor templates and CSC roots. Template fields are shared with tab 1; model, Perforce connection and exact path overrides use tab 2. Click **Generate blank plan & compare**.

The comparison generates blank destination files using the latest reference content and checklist rules. The current template supplies file destinations; its existing content does not determine what belongs in the blank plan. Reference headers/folder files, selected board statements, packages/includes, feature values, init commands, regional Bluetooth carrier values and the reference HCF filter block are included. Verification-only HAL/firmware/device checks are listed as manual items and contribute no blank write content.

The report compares this blank-file content with the edits introduced by the selected changelists, per filename:

- **Extra files:** developer files absent from the blank plan, including files outside the selected template mappings.
- **Extra changes:** developer statements/values absent from the blank plan, including additional edits inside a planned file.
- **Missing from changelists:** blank-plan items not introduced by the selected changes. Items already present before those changes are still listed here, since this is a comparison of the selected edits rather than current bring-up completeness.
- **Matched:** blank-plan items introduced by those changes, with contributing changelist numbers.
- **Unreadable/blocked:** incomplete comparisons requiring follow-up. Unreadable file content is not classified as a match or a missing item.

Multiple changelists are combined per file. Submitted edits are processed in file-revision order, even if the numbers are entered in a different order. Content added in one selected changelist and removed/replaced in a later selected changelist is cancelled or superseded. Unselected intermediate edits are not included. Each changelist's original diff remains in the report. Pending edits follow submitted edits, in the order entered.

Choose a content source, or leave **auto** selected:

- **submitted:** extracts each file's own submitted edit using its submitted revision and preceding file revision. The blank plan always uses latest reference content; no historical template plan is generated.
- **shelved:** reads a developer's shelved file contents, including another workspace's shelf. Its edits are extracted against depot head, so old shelves can include differences from later submissions. Only shelved files are included.
- **workspace:** extracts unshelved edits from the configured local workspace against each file's have revision. Files must still be open in the specified changelist. Remote unshelved work must be shelved first.
- **auto:** selects the appropriate source for each changelist independently, allowing a mixture of submitted and pending changelists.

Comparison ignores line endings and outer whitespace. Make packages are compared by package name so additions to multiline lists can match a single-line blank statement. Init commands retain their event context, Make statements retain conditional context, and XML/JSON feature values retain their element/key paths. Comments are included. The report compares statement/value membership, not runtime behavior or semantic equivalence of arbitrary Make expressions. Binary content is compared by bytes and hashes. Raw diffs retain all original edits and ordering. Pending metadata and content are checked again before reporting.

The tool saves `comparison.txt`, `comparison.json`, and a `blank-plan/` folder containing the generated blank files as a plan and previews. Comparison plans are inspection-only and cannot be applied. Comparison never syncs, opens, shelves or submits files, and it does not replace the regular apply plan.

```powershell
python main.py compare examples/m36x.json 123456 123457 123458 --out reports/bringup-comparison
```

CLI exit status is 2 for an incomplete comparison, 1 for an operation failure and 0 for a complete report; complete reports may still contain differences.

## How paths are resolved

The Bluetooth header also uses only the mapped `EXYNOS/.../device/<common_device>/` folder. Change or add relative candidates in `checklist/slsi.json`:

```json
"path_rules": {
  "system": {
    "bluetooth_header": {
      "anchor": "/EXYNOS/",
      "relative": "{model}_sssi/device/{common_device}/Bluetooth/bdroid_buildcfg.h"
    }
  }
}
```

Each path is relative to the model-common folder. `{model}` and `{common_device}` placeholders are supported if needed in a future filename/subfolder. The branch and model folder come from the template View. Only these exact candidates are queried; no system/application fallback searches occur. If none exists in reference, the copy is skipped. If multiple candidates exist, select one with an exact path override. A missing current header is added from reference after approval; existing headers are compared without overwrite.

`BoardConfigCommon.mk` is resolved only from included View lines under `EXYNOS/.../device/<common_device>/`. The resolver appends the filename to that mapped folder and makes an exact file query. It never searches ESSI system roots, Cinnamon applications, or unrelated common-device folders for BoardConfig. Missing mappings require an explicit path override instead of a broad fallback search.

Paths in the spreadsheet are examples. The resolver reads each supplied template's actual `View` using `p4 client -o`, searches its mapped depot paths for logical checklist targets, and validates each candidate against effective mappings, including exclusions and ordered overrides. It does not construct COOSA/BENI paths by replacing depot names or copying template-name fragments.

For example, a reference `android/device/samsung/m36x_common/Bluetooth/bdroid_buildcfg.h` can map under `//MODEL/PROD_BENI/ONEUI_8_5/...`, while the current template maps the same build path under `//MODEL/PROD_COOSA/ONEUI_9_0/FLUMEN/...`. Each side is resolved independently. Reference file copies translate through that build path into the current view.

Ambiguous file matches are blocked and listed. Use the **exact depot overrides** JSON field to select a candidate, for example:

```json
{
  "current.system.floating_feature": "//YOUR_DEPOT/exact/SecFloatingFeature.xml",
  "reference.system.floating_feature": "//REFERENCE_DEPOT/exact/SecFloatingFeature.xml",
  "current.vendor.manifest": "//YOUR_DEPOT/exact/manifest.xml"
}
```

Keys use `current|reference.system|vendor.target`. Targets are `board_config`, `device_common`, `sec_product`, `floating_feature`, `root_init`, `model_init`, `manifest`, `bluetooth_header`, `bluetooth_folder`, `hcf`, `hcf_makefile`, `firmware`. Directory overrides specify the directory, without a wildcard. File overrides must be inside the relevant template view. The existing writable workspace must map all target files; the tool does not modify its mappings.

`common_device` may be set in JSON when it differs from `<model>_common`. AP and model folder names can also be specified. Unsupported/ambiguous view syntax stops resolution rather than guessing.

## Checklist decisions

- System and vendor rules, SLSI firmware-family mappings, and source-cell references are in `checklist/slsi.json`.
- BoardConfig rules select assignment names and the Bluetooth board-include filename, not static values. System settings are copied from the reference system BoardConfig; vendor settings come from the reference vendor BoardConfig. Reference values, operators, comments and include paths are preserved. Missing, duplicate or conditional selected statements block the rule rather than falling back to defaults. Unrelated current settings are preserved. An explicit chipset that disagrees with the copied reference WLAN_CHIP must be corrected before applying.
- Product and floating Bluetooth features follow the reference OS, as the SLSI tab instructs. Current-only flags remain for review. Sample `TRUE` values are not imposed on every product. The separate Feature Flags tab has not been supplied as readable text; its feature-specific eligibility still requires review. Add verified feature-specific rules when that source is available.
- Carrier JSON compares the same relative region path under the two explicit CSC roots. Missing regional counterparts are reported, never replaced using another region. Only `CarrierFeature_BT_` values are changed; a changed JSON file is formatted and the complete diff shown.
- Headers/Bluetooth folders are copied only where missing in the current model and present in reference. Existing files are compared and reported; entire current folders are not replaced.
- Manifest entries and HCF copy filters are checked, not blindly rewritten. A mismatch blocks application until resolved. HIDL 1.0 manifest and 1.1 packages are preserved as separately specified by the sheet.
- The sheet's literal `chown bluetooth bluetooth ro.bt.bdaddr_path` is preserved and explicitly flagged for review. B28 has no event header; the tool uses `on post-fs-data`, inferred from B12, and reports that interpretation.
- Firmware existence/revision/hash is checked. Supply an approved release `firmware_sha256` to verify the exact release; otherwise latest-approved status is a REVIEW item. Depot head alone is not treated as release approval. Firmware binaries are not automatically upgraded.
- CP support is deferred. Legacy CP fields and pasted CP lines are ignored. Phone address/firmware checks remain post-build manual tasks.

## Execution behavior and recovery

Before mutation, the executor checks approval, every saved source/target revision and hash, template mappings, workspace mappings, and local state. It rejects files already opened by any workspace, dirty/untracked local targets, path collisions, and files outside the workspace root. It uses exact paths and pinned revisions, never force-syncs, never edits other changelists, and never automatically reverts or shelves.

Only UTF-8 text and binary files are supported for automatic writes. Keyword-expanded, UTF-16, symlink and other specialized Perforce types require manual handling. Conservative make/init parsing blocks ambiguous conditional assignments, duplicate commands and malformed continuations. Arbitrary make expressions are not evaluated.

Perforce operations are not a transaction. If a sync/open/write fails after approval, inspect the recorded pending changelist and execution journal. Completed edits and the original before-content remain available in the saved plan. No automatic rollback is attempted because it could destroy subsequent user edits. Generate a fresh plan after manually resolving partial work; the same execution journal is never overwritten.

## Extend

Edit `checklist/slsi.json` to add or change rules; both GUI and CLI load it. `catalog.py` retains the original readable factory. CLI `--catalog custom.json` selects another catalog. A rule carries an ID, logical target, worksheet source, kind and actions. `transforms.py` contains pure text operations, `resolver.py` template discovery, `planner.py` rule handlers/reporting, `executor.py` approval/preflight, and `perforce.py` the transport. Add tests for meaningful parsing and execution cases before enabling a new rule kind.

The supplied checklist text is preserved in `checklist/source_slsi.tsv` with row positions intact. See `CHECKLIST_COVERAGE.md` for the row-to-rule mapping.

## Validation status

Automated tests exercise a synthetic depot and mocked Perforce protocol, including the supplied COOSA/BENI mapping shape, read-only planning, approval refusal, stale plans, dirty files, regional isolation, JDM paths, idempotency, and partial failure journals. Real server access and device/build validation require your connection and hardware. No live Perforce mutation was performed while implementing this tool.
