# Delta Automation

A Python 3.10+ desktop and command-line tool for comparing reference files with a current Perforce template, proposing focused edits, and applying reviewed changes to a pending changelist.

**One JSON file defines a rule pack:** inputs, sources, file routes, derived values, selectors, conditions, operations and comparison policies. Python provides reusable parsing, discovery and editing operations. Selecting another pack does not require changing Python or registering its rule IDs.

Bundled packs:

| JSON | Scope |
| --- | --- |
| [slsi.json](checklist/slsi.json) | Existing SLSI Bluetooth workflow, migrated to schema 2 |
| [qcom-wcn.json](checklist/qcom-wcn.json) | QCOM-WCN worksheet |
| [qcom-qca.json](checklist/qcom-qca.json) | QCOM-QCA worksheet, including explicit removal rules |
| [camera-rules.json](examples/camera-rules.json) | Synthetic non-Bluetooth example demonstrating independent inputs, paths and rules |

See [rule authoring](docs/rules.md), [worksheet coverage](CHECKLIST_COVERAGE.md), and the [architecture diagram](docs/architecture.md).

## Run

No third-party Python packages are required. The GUI needs Tkinter. Live use needs the `p4` executable, an existing login ticket and a mapped writable workspace.

```sh
python main.py
# Or double-click start.bat on Windows.
```

1. In **1. Inputs**, select a JSON pack or use **Browse**. Fields come from that file. Drop additional packs into `checklist/` and click **Reload** to refresh the picker, or browse to a JSON anywhere.
2. In the **Perforce** section of the same Inputs tab, enter server, user and workspace. Authenticate separately with your normal `p4 login`; passwords are not requested or stored.
3. **Generate plan** performs read-only inspection. It records exact revisions, content and proposed diffs locally.
4. **2. Review & approve** shows statuses, reference/current filenames and proposed edits. Full reasons and evidence, including unresolved checks, are saved in `plan.txt` alongside the plan.
5. Apply the reviewed plan to a dedicated **pending changelist**. The tool never automatically shelves or submits. The execution journal records the changelist and per-file progress.

The GUI contains only Inputs and Review & approve. Empty-file previews, detailed reports and runtime diagnostics are saved to disk. Comparison reports are also saved without displaying them. The offline demo is available only through the CLI.

Changing inputs or the loaded JSON invalidates approval. A JSON changed on disk must be reloaded. An invalid selected pack reports an error; it does not silently run the SLSI pack instead.

The SLSI pack keeps the optional current build log, JDM and CSC controls. Log identifiers supplement **current** routing only; reference routing remains independent. CSC checks are off by default. Firmware checks verify the configured family/path exists in the mapped depot; they do not read or hash firmware binaries. Content hashes used for reviewed edit integrity and binary-copy comparison are separate from firmware presence checks.

## Command line

```sh
# Validate a rule file without contacting Perforce.
python main.py validate-rules checklist/qcom-wcn.json

# Synthetic offline example; its plan cannot be applied.
python main.py demo --out reports/demo

# Use a rule file explicitly, or save rules_file in the inputs JSON.
python main.py plan examples/m36x.json --catalog checklist/slsi.json --out reports/review
python main.py plan my-inputs.json --catalog checklist/qcom-qca.json --out reports/qca

# Compare one or more changelists.
python main.py compare my-inputs.json 123456 123457 --catalog checklist/qcom-wcn.json --out reports/comparison

# Apply only the exact reviewed saved plan.
python main.py apply reports/review/plan.json --approve FULL_DIGEST_FROM_PLAN --acknowledge-reviews
```

Fill your own connection/template values before live use. `examples/m36x.json` is a SLSI input example. [camera-inputs.json](examples/camera-inputs.json) shows a different input schema and a relative `rules_file` path, resolved relative to the inputs file.

`--acknowledge-reviews` acknowledges REVIEW findings. BLOCKED checks prevent CLI application unless `--acknowledge-blocked` explicitly requests applying the other planned edits while leaving blocked checks untouched. The GUI review checkbox acknowledges both categories. No acknowledgement invents a fix for an unresolved check.

Plans save `plan.json`, `plan.txt` (detailed reasons and diffs), and `empty-file-previews.txt`. Comparisons save `comparison.json` and `comparison.txt`. Apply writes an execution journal. GUI and CLI append diagnostics to `runtime.log`; Perforce requests default to a 30-second timeout, configurable through `perforce.timeout_seconds`.

## Changelist comparison

In **1. Inputs → Perforce**, enter one or more numbers in **Changelists (optional)**, separated by commas or spaces. **Generate plan** saves the comparison in the report folder without displaying its contents. Leave the box empty to skip comparison. The CLI `compare` command is also available. Saved inputs retain the numbers and may specify a content source:

```json
"comparison": {
  "changelists": ["123456", "123457"],
  "source": "auto"
}
```

With the changelist box empty, the GUI saves only the plan, previews and detailed report. A configured comparison saves `comparison.json`, `comparison.txt` and its blank baseline in the same report folder; none are displayed in the GUI. If that optional comparison fails, the plan is still saved and its folder contains `comparison-error.txt`. Reloading the same rule file retains these saved settings; selecting a different pack clears them.

Comparison reuses saved plan previews only when their rules and inputs still match; otherwise it regenerates the preview. Rules declare whether a file participates. SLSI excludes `bdroid_buildcfg.h` from comparison while retaining its copy-if-missing check and preview.

| Result | Meaning |
| --- | --- |
| Matched | Surviving selected edits introduce the suggestion, or perform a declared removal |
| Already present in latest depot | Latest target content satisfies an uncovered suggestion; for removal rules, selected content is already absent |
| Missing | No selected addition matches and latest content does not satisfy the addition; for removal rules, selected content still remains in latest |
| Extra files | CL paths with no generated comparable requirement at that exact depot path |
| Extra changes | CL additions/removals outside the generated requirements; this does not by itself mean the developer change is wrong |
| Unreadable/blocked | Evidence could not be obtained or interpreted; it is not counted as missing |

Latest checks read a pinned live depot revision, not a local workspace copy or old planning snapshot. File identity is the exact depot path: another branch containing the same basename is a different file. For init files with `latest_scope: "file"`, the whole file is searched before calling a command missing, including other events. This is a presence check and does not prove those events are interchangeable at runtime.

Make list members, conditional contexts and XML/JSON key paths are retained. XML/JSON files may opt into `mode: "requirements"` so declared minimum-version or missing-key merge semantics govern latest-file satisfaction. Other files use the selected preview content. Required removals are evaluated with the rule's removal selector. Comments may appear as extra changes; original diffs are always retained.

Submitted CL edits use each file's revision and preceding revision, combined in file-revision order. Pending CLs follow in input order. `--source` accepts `auto`, `submitted`, `shelved`, or `workspace`. Shelves are compared against depot head; unshelved workspace edits use the have revision and must belong to the configured local workspace. Unselected edits are never attributed to selected CLs, but may satisfy requirements through latest depot content.

## Verification and limits

```sh
python -m unittest discover -s tests -v
python -m compileall -q bt_delta tests
```

Tests cover routing/exclusions, conditional Make statements, XML/JSON editing, comparison, stale-plan protection, mock application, all three bundled packs and the independent camera pack. SLSI proposed bytes are compared against saved pre-refactor synthetic baseline outputs. Tkinter tests need Tk and a display and otherwise skip.

The JSON language supports the documented primitives, not arbitrary code or build execution. New files, names, paths and combinations of those primitives belong in JSON. A new file syntax or fundamentally new operation requires a reusable Python primitive and schema support. Ambiguous paths, unsupported Make constructs and unknown applicability produce explicit review/blocking outcomes. Workbook items requiring hardware, unspecified FloatingFeature choices or external patches remain manual, with worksheet references. Automated tests do not establish successful live Perforce deployment, Android builds or device behavior.
