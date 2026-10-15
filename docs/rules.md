# JSON rule files, schema 2

A rule pack is one JSON document. It owns domain decisions; the runtime owns generic operations. There is no Python dispatch by pack name, worksheet, chipset or rule ID. `catalog.py` is a compatibility loader, not a second rule catalogue.

Start from [camera-rules.json](../examples/camera-rules.json) for a small non-Bluetooth example, or one of the packs in `checklist/`. The formal contract is [rules-v2.schema.json](../checklist/rules-v2.schema.json). Unknown fields, malformed regexes, duplicate JSON keys, unknown references, invalid operation/format combinations and dependency cycles fail validation.

```sh
python main.py validate-rules path/to/my-rules.json
python main.py plan my-inputs.json --catalog path/to/my-rules.json --out reports/my-plan
```

Validation does not query Perforce. Planning validates the pack and inputs before requesting server data. An invalid/missing explicitly selected file is an error; the bundled SLSI default is used only when no file is selected.

## Document structure

| Section | Responsibility |
| --- | --- |
| `schema_version`, `id`, `title` | Version 2, stable pack identity and display title |
| `source` | Optional descriptive source/workbook label |
| `inputs` | Named typed fields, defaults, required conditions and GUI labels |
| `sources` | Template inputs or explicit depot roots; current/reference roles |
| `lookups` | Data tables such as chip-to-firmware names |
| `derive` | Named expressions for identifiers, inference and eligibility |
| `preconditions` | Conditions that must hold before rules run |
| `files` | File/tree identity, syntax, routes, filters and comparison policy |
| `rules` | Ordered operations, conditions and dependencies |
| `report` | Optional report title and changelist description |
| `comparison` | Conditional exclusions with explanations |

`inputs`, `sources`, `files` and `rules` are required; empty objects are allowed where useful. IDs are arbitrary strings. Dotted input names address nested saved JSON; dotted derived names are variable IDs. Do not declare overlapping inputs such as both `current` and `current.model`.

A minimal complete reference-copy pack:

```json
{
  "schema_version": 2,
  "id": "audio-config",
  "title": "Audio configuration",
  "inputs": {
    "baseline": {"type": "string", "required": true},
    "destination": {"type": "string", "required": true}
  },
  "sources": {
    "old": {"kind": "template", "input": "baseline", "role": "reference"},
    "new": {"kind": "template", "input": "destination", "role": "current"}
  },
  "files": {
    "old.settings": {
      "source": "old", "kind": "file", "format": "make",
      "routes": [{"client_relative": "android/device/audio/settings.mk"}]
    },
    "new.settings": {
      "source": "new", "kind": "file", "format": "make",
      "routes": [{"client_relative": "android/device/audio/settings.mk"}]
    }
  },
  "rules": [{
    "id": "settings",
    "title": "Copy reference audio settings",
    "provenance": {"sheet": "Audio", "range": "B4"},
    "steps": [{
      "op": "make.ensure",
      "from": {"file": "old.settings"},
      "to": {"file": "new.settings"},
      "select": {"assignment_patterns": ["^AUDIO_"]},
      "policy": {"on_source_missing": "review", "on_target_missing": "create"}
    }]
  }]
}
```

Saved inputs for this pack contain `rules_file`, `baseline`, `destination`, `perforce` and optionally `paths`. `perforce` is shared infrastructure (`port`, `user`, `client`, optional `executable`/`timeout_seconds`), not repeated in the pack. `paths` maps file IDs to exact depot paths for explicit overrides. Overrides still must be inside the declared source View/root.

## Inputs and expressions

Input types: `string`, `boolean`, `path`, `depot`, `list`, `choice`. Fields support `label`, `help`, `default`, `required`, `required_when`, `pattern`, `choices`, `visible`, `group`, and `derive`. `visible: false` hides an advanced field while retaining values in saved inputs. `group` is descriptive metadata; the current GUI renders inputs in declaration order. Boolean values are actual JSON booleans; list values are arrays of strings. Patterns validate the whole value (each entry for lists). Depot fields reject wildcards and revision specifiers.

Input `derive` supplies an absent/empty value using other inputs only. It does not read files or derived variables. Input defaults are resolved in dependency order. `required_when` determines requiredness when present. Pack-level `derive` may read declared source files and is evaluated lazily, with cycle detection:

```json
"derive": {
  "current.model": {
    "value": {"coalesce": [{"input": "current_model"}, {"input": "reference_model"}]}
  }
}
```

`"${current.model}"` interpolates a named derived variable in route/content/message strings. It does not refer directly to an input; expose the input through `derive` first. Make expressions such as `$(TARGET_PRODUCT)` stay literal. `{"literal": ...}` wraps expression data that must not be interpreted as an operation.

| Expression | Behavior |
| --- | --- |
| `input`, `var`, `literal`, `template` | Read saved field, derived value, literal data, interpolated string |
| `coalesce: [...]` | First resolved nonempty value; `false` and zero are valid values |
| `if`, `then`, `else` | Conditional value |
| `eq`, `ne`, `all`, `any`, `not`, `present`, `matches` | Eligibility; `matches` uses a regex search |
| `lower`, `strip`, `last_digits`, `list` | String/list normalization; `strip` also strips surrounding quotes |
| `lookup: {table, key}` | Read a declared lookup table; unknown key yields unresolved value |
| `file_text` | Read a declared file through its source binding |
| `assignment: {file, key}` | Extract one unconditional, noncomputed Make assignment |
| `regex: {text, pattern, group, many, exclude}` | Extract unique capture(s); ambiguity is explicit |
| `log: {input, label, pattern, ...}` | Extract an optional local log capture and log its use |
| `path_root: {path, component}` | Take a depot prefix through one unique named directory |

Missing values remain unresolved, not guessed. `all` and `any` short-circuit when a decisive value is known; otherwise an unresolved condition causes REVIEW. Optional log read/extraction failures log a reason and return no value, allowing explicit `coalesce` fallback. Log capture rules, identifier patterns and current/reference precedence belong in JSON. No expression executes Python, shell commands or Make.

Sources must be resolvable before reading files, so template names/depot roots cannot depend on a file read. Derived variables may optionally `export` values into saved resolved configuration; exports are recomputed on the next run. Use `required: true` for derivations without which planning must stop. A precondition is `{ "condition": <expression>, "message": "..." }`.

## Sources, routes and pairing

A source is either `{"kind":"template","input":"template_input","role":"current"}` or `{"kind":"depot_root","root":<expression>,"role":"reference"}`. `role` must be declared. Write destinations must be current sources. Sources, files, rules and steps may declare a `when` expression; put matching eligibility on dependent rules when an entire source is optional.

Files declare `kind: "file"` or `"tree"` and `format: "make"`, `"init"`, `"xml"`, `"json"`, `"text"` or `"binary"`. Each route uses exactly one method:

| Route | Interpretation |
| --- | --- |
| `client_relative` | Translate a known client-side path through the template View |
| `anchor_regex` + `relative` | Append a relative path after a matched depot anchor |
| `mapped_regex` + `append` | Match an already mapped depot directory; optionally filter a named `capture` through expression `allowed` |
| `depot` | Explicit depot path, string or expression, still checked against source coverage |

Routes are bounded candidates, not permission to scan the depot. Alternatives are collected and checked against effective View mappings/exclusions; the first live path is not blindly selected. Multiple matches require an override or narrower rules. Tree operations query only declared mapped subtrees. Unmapped candidates and queried-but-absent files have different diagnostics.

The SLSI/QCA packs declare `/EXYNOS[0-9]*/` where that family is appropriate, covering EXYNOS and numbered variants. The generic resolver contains no EXYNOS naming rule. Changing an AP directory or branch layout belongs in routes or derivation data.

Tree `filter` supports exact `basename` and `relative_regex` (see schema). Copy pairing is `{"method":"tree_relative"}` for equal relative paths below two declared roots, or `{"method":"view_relative","replacements":[["old_component", "new_component"]]}` for explicitly renamed client-side path components. Singular file copying pairs the resolved source and destination files. Missing destinations must have a unique mapped path.

## Operations

A step refers to files through `from: {file: "..."}` and `to: {file: "..."}`. Reference selection can be `select` on the step or inside `from`, never both. Where supported, `literal` replaces `from`: it is an explicit desired value recorded in this JSON, not an implicit checklist example used when reference data is missing.

| Operation | Selection and behavior |
| --- | --- |
| `file.copy_if_missing` | Copy a complete reference file only if absent; preserve differing existing files and report REVIEW |
| `tree.copy_missing` | Same behavior for selected tree members, using explicit pairing |
| `file.exists`, `path.assert` | Check mapped live existence; optional `assert_path` requires an expected substring; no content read/hash |
| `make.ensure` | Copy selected `assignment_names`, `assignment_patterns`, `include_basenames`; retain reference operators/comments |
| `make.values` | Copy scalar assignments selected by `prefix`/patterns; default operator `=`, optional explicit `operator` |
| `make.packages` | Copy selected list members (`names`/`patterns`) under explicit `variable`; preserve enclosing reference guards |
| `make.includes` | Copy reference include statements selected by `lines`/`patterns` |
| `make.remove` | Remove selected assignments, include/statement patterns, or members of explicit `variable`; optional guard `within` regex |
| `text.ensure` | Ensure raw `lines`/`patterns`; optional `identity_pattern` identifies a setting to update, including C preprocessor lines |
| `text.remove` | Remove selected raw lines without interpreting `#` as a Make comment |
| `init.ensure` | Select commands/comments, optionally within `event`; otherwise retain each reference event |
| `block.sync`, `block.remove` | Select bounded regex-captured blocks; sync requires a unique declared choice, removal rejects ambiguity |
| `xml.ensure`, `xml.verify` | Select elements by path/name; compare declared identity and fields; ensure may edit, verify never does |
| `json.merge_missing` | Recursively add missing object keys/files; retain existing values, arrays and current-only keys |
| `assert` | Check an expression, with a failure status/message |
| `manual` | Report an explicit manual/review instruction and provenance without inventing an edit |

Make scalar copying does not evaluate Make or copy ambiguous conditional assignments/includes. Conditional additive list entries can be copied with their complete guards. Existing members under different conditions are preserved for review. Member removal retains unrelated members/branches and removes only guards made empty by that edit. `text.ensure.identity_pattern` uses capture groups as the key, or the full matched text when no group exists.

Init editing searches the whole current file for duplicates. An identical command under a different event is preserved with a review notice; it is not relocated automatically. `policy.context_patterns` can select additional surrounding reference commands. Comparison's file-wide presence option is separate from editing semantics.

### XML

Specify `select.path` relative to the document root, optional `name_path` plus `names`/`patterns`, and a nonempty `identity` list. An attribute path starts with `@`. `parent` identifies the unique insertion parent (default root). `root` is required when creating an actual missing XML target. The parser supports ElementTree-style paths, not arbitrary XPath.

```json
{
  "op": "xml.ensure",
  "from": {"file": "old.devices"},
  "to": {"file": "new.devices"},
  "select": {"path": "./device", "name_path": "@name", "names": ["front"]},
  "identity": ["@name"],
  "fields": [
    {"path": "version", "compare": "minimum_version"},
    {"path": "driver", "compare": "equal"}
  ],
  "policy": {"on_value_difference": "review"}
}
```

Field comparisons are `equal`, `subset` or `minimum_version`. An optional `tuple` list declares child paths to compare as grouped values. Without fields, the selected subtree is compared as a whole. Duplicate identities are rejected. XML edits retain unrelated current bytes/comments; replacement is limited to a selected element. Default value differences produce REVIEW; explicit `replace` or `preserve` changes that policy. Verification-only rules contribute no blank edit content.

HIDL/AIDL is not a special Python operation. For SLSI the JSON selects HAL names, declares `name` and `@format` identity, and lists the version/transport/interface fields to verify. Other packs can select other names, attributes or elements.

### Blocks and JSON trees

`block` declares `pattern` with named captures, an `identity`, `compare` fields and optional `split` captures. `choose` supplies the capture `field`, an exact input or prefix expression, optional `single_fallback`, required members and exports. `related` may verify declared reference/current tree files by suffix, with content snapshotting only when explicitly requested. See SLSI's HCF rule for the full generic block example; HCF folder names and selection policy are all JSON data.

JSON tree merge pairs the same relative paths below unique roots. Optional `inventory` labels, marker names and directory hints explain regions with no eligible file. The operation remains a missing-key merge, not array concatenation or a value overwrite.

## Outcomes and policy

Rules run in JSON order once their `depends_on` IDs have completed. Dependency cycles are rejected. Only PASS/CHANGE dependencies satisfy prerequisites. Steps within a rule run in order; successful edits are visible to later steps/rules. A thrown error, failed assertion or unresolved required step rolls back that rule's proposed edits and previews. Other rules can still produce a plan. REVIEW notices may accompany safe proposed edits; inspect the exact diff.

| Policy | Default and meaning |
| --- | --- |
| `on_source_missing` | `skip`: do not invent reference content; may be `review`/`block` |
| `on_empty_selection` | `skip`: a selector matching nothing adds nothing; may be `review`/`block` |
| `on_target_missing` | `review` for content operations; `create` permits an unambiguous mapped target; `skip`/`block` available |
| `on_missing` | `review` for existence checks; may be `skip`/`block` |
| `on_value_difference` | `replace` for Make scalar/text ensure; `review` for XML. `preserve` keeps existing values without a review finding |
| `report_current_only` | Optional review of current-only scalar flags or JSON-tree files |

Policy keys that do not apply to an operation are rejected. File copying accepts only `on_source_missing`; it creates missing files and always preserves existing files. JSON-tree merge accepts `report_current_only`; single-file JSON merge accepts `on_source_missing` and `on_target_missing`. Block sync has its own explicit identity/comparison definition. `on_value_difference` applies to `make.ensure`, `make.values`, `text.ensure` and `xml.ensure`. Verification-only XML always reports differing fields for review. For Make it protects selected includes as well as assignments while still adding missing settings.

PASS means no change needed for evaluated scope; CHANGE means edits are proposed; REVIEW means evidence or differences need judgment; BLOCKED means a step could not safely complete; SKIPPED means explicitly inapplicable/absent optional content; MANUAL means a declared human/build/device check. A manual item is never presented as a verified device result.

## Comparison requirements

Per-file `comparison` supports:

- `include: false` to exclude a file.
- `latest_scope: "file"` to search the whole latest init file, or `"context"` (default) to retain event matching.
- `list_variables` to normalize declared Make lists by member.
- `xml_identity` to declare key fields per element name.
- `mode: "requirements"` for XML/JSON latest checks to follow declared field constraints/merge semantics; default `"reference"` compares preview content.

Top-level `comparison.exclude` entries support `basename` or `path_regex`, plus `when` and `reason`. Exclusions apply to previews and CL files. See SLSI for conditional CSC and header exclusions.

Additions first match surviving selected CL additions; uncovered additions are checked in latest depot content. Required removals are separate requirements, using the same selector to establish that unwanted content is absent. Unreadable latest files are errors, not missing. Exact paths remain distinct across branches.

Blank previews are inspection-only, may contain separate XML snippets, and are never executable plans. Normal and blank planning use the same rule interpreter. The normal plan aggregates edits against real target contents and retains snapshots for executor verification.

## Migrating and extending

Schema 1 is rejected deliberately: it depended on implicit Python rules and cannot guarantee that a supplied JSON is complete. The bundled SLSI pack has been migrated. For another legacy pack:

1. Declare every input, reference/current source and target file route in schema 2.
2. Move chipset/firmware tables, AP aliases, log regexes and eligibility into `lookups`/`derive`/`when`.
3. Translate selectors and transformations into explicit operations. Record constants as `literal`; record unspecified human checks as `manual`.
4. Declare comparison exclusions and context/identity policy; preserve worksheet provenance.
5. Validate, inspect a read-only plan, compare expected bytes, and apply only after reviewing the new plan.

Do not reuse approval from a schema-1 plan to validate a new pack. Compatibility helpers remain for existing imports/tests, but do not dispatch live domain rules.

Adding a new pack using supported operations needs only JSON. Adding a genuinely new parser/operation needs generic implementation, schema validation, tests and documentation; the engine cannot infer arbitrary behavior from prose. The XLSX is source material for authoring and provenance, not a runtime parser or a competing rule source.
