# Rules explained in plain language

Think of the tool as following a recipe:

**Take these inputs → find these files → select this content → check or propose these edits → let a person review them.**

Python supplies reusable abilities such as reading Make assignments or finding XML elements. JSON tells those abilities which files, settings and conditions to use.

## 1. Which file does what?

| File | What you put in it |
| --- | --- |
| `slsi.json` or a different rules pack | What to find, what to check and what edits to propose |
| `slsi.comparison.json` or another companion | How to compare suggestions with developer changelists and latest files |
| Saved inputs JSON | Your actual template names, model, Perforce connection and optional changelist numbers |
| `rules-v2.schema.json` | The allowed structure of a rules pack |
| `comparison-v1.schema.json` | The allowed structure of a comparison configuration |

For example, the rules pack declares that a current template input is needed. Your saved inputs supply its actual name.

The schemas catch mistakes such as an unknown operation or a misspelled field. The loader also checks that referenced file IDs and variables exist.

The spreadsheet records the original checklist and provides source labels. During a run, the tool follows the selected JSON pack.

## 2. Every main section at a glance

| Section | The question it answers |
| --- | --- |
| `schema_version` | Which rules format does this file use? Currently `2`. |
| `id` | What is this pack's stable name? For example, `slsi`. |
| `title` | What readable name should the user see? |
| `source` | Where did these requirements come from? |
| `inputs` | What information can the user supply? |
| `sources` | Which templates or depot roots contain the reference and current files? |
| `lookups` | What fixed mappings should the tool know? |
| `derive` | What values should it calculate or extract? |
| `preconditions` | What must be true before the main rules run? |
| `files` | Which files or folders should it locate? |
| `rules` | What work should it perform on those files? |
| `report` | What report title and pending-changelist description should it use? |
| `comparison_file` | Where is this pack's comparison configuration? |

`schema_version`, `id`, `title`, `inputs`, `sources`, `files` and `rules` are required sections. Other sections are added when needed.

## 3. `inputs`: questions and options

An input describes a field the tool understands:

```json
{
  "model": {
    "type": "string",
    "label": "Model",
    "default": "",
    "required": true
  }
}
```

Read this as: “Accept a text value called `model`, display it as Model, and require a value.”

| Field | Simple meaning |
| --- | --- |
| `type` | Text (`string`), checkbox (`boolean`), local `path`, exact `depot` path, `list`, or `choice` |
| `label`, `help` | Explain the field to the user |
| `default` | Starting value when none is supplied |
| `required` | A value is needed |
| `required_when` | A value is needed only under a condition |
| `pattern` | A regular expression describing allowed values |
| `choices` | Allowed options for a choice field |
| `visible` | Whether to show it in the form; hidden inputs can still have saved values |
| `derive` | Fill an empty input using other inputs |
| `group` | Descriptive metadata; the current form follows declaration order |

For example, CSC paths can be required only when the CSC checkbox is enabled. A build-log path can remain optional.

## 4. `sources`: where the files come from

```json
{
  "reference.vendor": {
    "kind": "template",
    "input": "reference.vendor_template",
    "role": "reference"
  }
}
```

This means: “Use the template named in the `reference.vendor_template` input. Treat its files as reference files.”

`kind` can be `template` or `depot_root`. A depot-root source uses a declared `root` instead of a template input. `role` identifies `reference` or `current`; proposed write destinations must belong to a current source.

Names such as `reference.vendor` are IDs that connect sections together. Their spelling does not activate special Python behavior.

## 5. `lookups`: fixed answers in a table

```json
{
  "firmware": {
    "s5e8825": "papaya_s620",
    "s5e8835": "quartz_s621p"
  }
}
```

This table says which firmware family corresponds to each chipset. A lookup expression asks the table for the answer. Adding a table entry changes data in JSON.

## 6. `derive`: values the tool works out

Examples include reading `WLAN_CHIP` from a board file, extracting `TARGET` from an optional log, or combining a model name with `_common`.

```json
{
  "current.common_device": {
    "value": {
      "template": "${current.model}_common"
    }
  }
}
```

If the derived value `current.model` is `m34x`, this produces `m34x_common`.

| Field | Meaning |
| --- | --- |
| `value` | The expression that produces the answer |
| `when` | Calculate it only when applicable |
| `required` | Treat an unavailable required answer as a setup problem |
| `export` | Also record the answer at a named location in the resolved configuration |

`{"input": "model"}` reads a supplied input. `{"var": "current.model"}` reads a derived value. `${current.model}` inserts that derived value into a string.

Derived values are calculated when needed. Their position in the JSON does not mean they all run immediately in that order.

### The small expression vocabulary

| Expression | Read it as |
| --- | --- |
| `input`, `var` | Read an input / a derived value |
| `literal`, `template` | Use this exact data / build a string with derived values |
| `coalesce` | Use the first available nonempty answer in this list |
| `if`, `then`, `else` | Choose an answer depending on a condition |
| `eq`, `ne` | Equals / does not equal |
| `all`, `any`, `not` | Every condition / at least one condition / reverse the condition |
| `present`, `matches` | Has a value / matches a regular expression |
| `lower`, `strip`, `last_digits`, `list` | Convert case, trim, extract trailing digits or build a list |
| `lookup` | Get an answer from a lookup table |
| `assignment` | Read a simple Make assignment from a declared file |
| `file_text` | Read a declared file's text |
| `regex` | Extract matching text |
| `log` | Extract a value from an optional local log and record its use in diagnostics |
| `path_root` | Extract a depot prefix through a named directory |

For example, a `coalesce` list can try an explicit input, then an extracted value, then a declared fallback. The order written in JSON determines precedence.

## 7. `preconditions`: checks before the main work

A precondition contains a `condition` and a failure `message`.

The current SLSI pack checks the derived reference `WLAN_VENDOR`: **allow it when absent or equal to `8`; block setup when a different value is explicitly present.**

That stops this pack from proceeding with a reference that explicitly identifies another vendor. It permits missing vendor information. Some template/file reads are needed to resolve this value before the check can run.

## 8. `files`: addresses for the things to inspect

```json
{
  "current.board": {
    "source": "current.vendor",
    "kind": "file",
    "format": "make",
    "routes": [
      {"client_relative": "android/device/samsung/m34x_common/BoardConfigCommon.mk"}
    ]
  }
}
```

This gives the file an ID (`current.board`), identifies its source, declares its syntax and explains how to find it.

`kind` is `file` or `tree` (a folder and its files). Supported formats are `make`, `init`, `xml`, `json`, `text` and `binary`. A tree can use `filter` to narrow files by `basename` or `relative_regex`. `when` makes a file declaration conditional.

There are four route methods:

| Route | Simple meaning |
| --- | --- |
| `client_relative` | Translate this workspace-relative path through the template View |
| `anchor_regex` + `relative` | Find a depot directory pattern, then append this relative path |
| `mapped_regex` + optional `append` | Match a directory already covered by the View; optionally append a path |
| `depot` | Use this explicit depot path, checked against the source's coverage |

For mapped routes, `capture` and `allowed` can restrict which directory matches are acceptable.

For example, `/EXYNOS[0-9]*/` matches `/EXYNOS/`, `/EXYNOS2/` and other numbered variants. That pattern is declared in JSON.

Every candidate must fit the effective View/root. An unmapped path and a server-confirmed missing file are different outcomes. Ambiguous matches need narrower configuration or an exact `paths` override in saved inputs.

## 9. `rules`: instructions using those addresses

A rule has an `id`, a readable `title` and one or more `steps`.

```json
{
  "id": "bluetooth.board",
  "title": "Copy reference Bluetooth board flags",
  "source": "SLSI!B6:C6",
  "steps": [
    {
      "op": "make.ensure",
      "from": {"file": "reference.board"},
      "to": {"file": "current.board"},
      "select": {"assignment_names": ["BOARD_HAVE_BLUETOOTH"]},
      "policy": {"on_target_missing": "create"}
    }
  ]
}
```

Read it as: “Find this assignment in the reference board file and propose ensuring it in the current board file. Allow creating a missing target when its mapped path is unambiguous.” The file IDs must also be declared in `files`.

If the reference says `BOARD_HAVE_BLUETOOTH := true`, that is the selected statement. An already matching current setting needs no edit. An absent setting gets an addition. A differing setting follows the operation's value-difference policy.

| Rule field | Meaning |
| --- | --- |
| `source` or `provenance` | A checklist/source label shown with results; `provenance` can contain `sheet` and `range` |
| `when` | Run only when this condition holds |
| `disabled_message` | Explanation when the rule is inapplicable |
| `depends_on` | Other rule IDs that must finish with PASS or CHANGE before this rule runs |
| `steps` | The operations to perform, in order |

### What the step fields mean

| Field | Meaning |
| --- | --- |
| `op` | Which reusable operation to perform |
| `from`, `to` | Source and destination file IDs |
| `select` | Which assignments, list members, lines, commands or XML elements matter |
| `literal` | Explicit desired content, for operations that support supplying it directly |
| `when` | Whether this step applies |
| `policy` | What to do when content is absent or different |
| `pairing` | How files in two trees correspond, or how View-relative paths correspond |
| `replacements` | Explicit text substitutions supported by the operation |
| `operator` | An explicit Make assignment operator where supported |
| `assert_path` | Expected substring for a path-presence check |
| `condition`, `status`, `message` | What an assertion/manual check evaluates or reports, where supported |
| `identity`, `fields`, `parent`, `root` | XML matching, field checks and insertion details |
| `block`, `choose`, `related` | Find a complete block, choose the appropriate one and check related files |
| `inventory` | Labels/hints explaining JSON-tree coverage |

Only fields supported by the chosen operation are valid. `select` may appear inside `from` instead of on the step; declare it once.

`source` on a rule is a display/provenance label. `source` on a file identifies a declared template/root. These fields serve different purposes.

## 10. All supported operations

| Operation | What it does |
| --- | --- |
| `file.copy_if_missing` | Propose copying one complete reference file when the target is absent |
| `tree.copy_missing` | Propose copying missing reference files from a folder |
| `file.exists` | Check that a live file exists within mapped coverage |
| `path.assert` | Check mapped existence, optionally requiring an expected path substring |
| `make.ensure` | Ensure selected Make assignments/includes from the reference |
| `make.values` | Copy selected scalar Make settings, such as product-feature flags |
| `make.packages` | Copy selected members of a declared Make list variable |
| `make.content` | Copy content below selected comment headings, including arbitrary package names, includes, settings and calls |
| `make.includes` | Copy selected Make include statements |
| `make.remove` | Remove selected Make settings, statements or list members |
| `text.ensure` | Ensure selected text lines/settings |
| `text.remove` | Remove selected text lines |
| `init.ensure` | Ensure selected init commands in their reference events |
| `block.sync` | Ensure the selected complete reference block |
| `block.remove` | Remove a selected complete block |
| `xml.ensure` | Check selected XML elements and propose allowed additions/updates |
| `xml.verify` | Check selected XML elements and report results |
| `json.merge_missing` | Add missing JSON object keys/files while preserving existing values and arrays |
| `assert` | Check a declared condition |
| `manual` | Record an instruction requiring human/build/device checking |

For a package rule, `variable: "PRODUCT_PACKAGES"` identifies the list to edit. `names` or `patterns` select its members. Supported conditional additions retain their guards; unsupported or ambiguous content is reported for review.

The SLSI and QCOM-WCN package rules now use `make.content`. A heading such as `#BT` or `#Bluetooth` selects the statements below it until the next standalone comment. So `msnv12.bin` is selected even though its name does not contain “Bluetooth”. The JSON also declares package and include patterns that search outside those sections. That catches `include .../Bluetooth/Bluetooth.mk` even without a heading. Existing packages/includes anywhere in the current file are reused, and copied conditions remain conditions. All these selectors are configurable in the JSON.

For `init.ensure`, the tool finds the selected commands across all reference sections, including repeated `on boot` or `on post-fs` sections. It then checks the whole current file for each command. Missing commands use the reference event. If several current sections are equally suitable, it adds another section with that same event instead of blocking just because the header repeats. Actual conflicting command arguments still receive review.

For XML, `identity` answers “which element is this?” and `fields` answers “what should we check inside it?” For example, a HAL can be identified by name and format, then checked for version, transport and interface. Field checks can require equality, a subset or a minimum version.

## 11. `policy`: how to handle exceptions

| Policy field | Question it answers |
| --- | --- |
| `on_source_missing` | What if the reference file is absent? |
| `on_empty_selection` | What if nothing in the reference matches the selector? |
| `on_target_missing` | May we create a missing current target, or should we report it? |
| `on_value_difference` | Replace the selected differing value, preserve it, or request review? |
| `on_missing` | What result should an unsuccessful existence check produce? |
| `report_current_only` | Should extra current-only settings/files produce review findings? |
| `context_patterns` | Which nearby text helps choose where to insert missing init commands? |

Available choices depend on the operation. Copy-if-missing preserves an existing differing file and reports it for review. XML differences default to review. Make scalar differences normally propose replacement unless configured otherwise.

## 12. The separate comparison configuration

The rules pack links to its companion using `comparison_file`, for example `slsi.comparison.json`. A relative filename is resolved from the rules file's directory.

The companion contains:

| Component | Purpose |
| --- | --- |
| `schema_version` | Comparison format version, currently `1` |
| `rules_id` | Must match the main rules pack's `id` |
| `files` | Comparison policies keyed by the same file IDs used in the rules pack |
| `exclude` | Filename/path exclusions, optionally controlled by `when` with a `reason` |

Each per-file policy can contain:

| Setting | Simple meaning |
| --- | --- |
| `include: false` | Leave this file out of changelist comparison |
| `list_variables` | Compare these Make variables member by member |
| `latest_scope: "file"` | Accept an init command found anywhere in the latest file |
| `latest_scope: "context"` | Require matching init context; the default |
| `xml_identity` | Fields that identify corresponding XML elements during comparison |
| `mode: "requirements"` | For XML/JSON latest checks, use declared field constraints/merge rules |
| `mode: "reference"` | Compare preview content; the default |

For example, `list_variables: ["PRODUCT_PACKAGES"]` means:

```make
PRODUCT_PACKAGES += BluetoothAgent libbt-vendor
```

Treat `BluetoothAgent` and `libbt-vendor` as separate comparison items. `PRODUCT_PACKAGES` is the variable name; the two names on the right are its members. The planning rule's selector decides which members should be proposed.

Comparison starts from the empty-file suggestions. It checks selected CL edits, then checks latest file content for uncovered suggestions before reporting them missing. An unreadable latest file produces an error. Different depot paths remain distinct.

Saved-input `comparison.changelists` and `comparison.source` still hold the run's CL numbers and content-source option. They are separate from these per-file policies.

## 13. `report`, review and saved outputs

`report.title` controls the report title. `report.changelist` supplies the pending-changelist description, with derived-value placeholders where needed.

The results mean:

| Status | Meaning |
| --- | --- |
| PASS | No edit is needed for the checked content |
| CHANGE | Edits are proposed |
| REVIEW | A difference or uncertainty needs judgment |
| BLOCKED | The rule could not complete safely |
| SKIPPED | The rule/content is inapplicable or optional and absent |
| MANUAL | A declared human/build/device check remains |

The review screen shows statuses, reference/current filenames and proposed edits. Checkboxes select which edits to include. Full reasons are saved in `plan.txt`; the exact plan is saved in `plan.json`. Empty previews, comparison reports and runtime logs are saved separately.

## 14. What happens when you click the buttons?

**Generate plan:** validate both configurations and inputs → resolve sources and required values → check preconditions → execute applicable rules → save the proposed plan → review/select edits → approve → apply exact selected bytes to a pending CL.

Planning reads files and prepares proposed changes. Applying is a separate action. The tool rechecks revisions/workspace state before writing. It does not automatically submit or shelve the pending CL.

Rules run in declared order as dependencies allow; steps run in order. Later operations see earlier proposed edits. A failed rule rolls back its own proposed changes while independent rules can continue.

**Check checklist differences:** generate fresh empty-file suggestions → compare with selected changelists → check latest content for uncovered suggestions → save the comparison report. This action runs independently of Generate plan.

## 15. Where to start when changing a pack

| You want to change… | Edit… |
| --- | --- |
| A user option | `inputs` |
| A template/root binding | `sources` |
| Chipset-to-firmware mappings | `lookups` |
| Log extraction or fallback precedence | `derive` |
| Eligibility of the whole pack | `preconditions` |
| A searched path or folder | `files.routes` |
| A flag/package/XML selection or proposed edit | `rules.steps` |
| Behavior when something is missing/different | The step's `policy` |
| CL comparison matching/exclusions | The companion comparison JSON |
| Report title or pending-CL description | `report` |

Validate your changes with:

```sh
python main.py validate-rules checklist/slsi.json
```

That validates the linked comparison file too. The detailed field contract is in [rules.md](rules.md); working examples are [camera-rules.json](../examples/camera-rules.json) and [its comparison companion](../examples/camera-rules.comparison.json).
