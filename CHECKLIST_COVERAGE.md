# Workbook coverage

Source: `159551_BT Delta Checklist.xlsx`. The checked-in JSON packs are the executable specifications; worksheet/range references explain their provenance. The spreadsheet is not read at runtime. A copied reference value and a literal worksheet requirement are distinguished explicitly in each step.

## SLSI — `checklist/slsi.json`

| Worksheet | Rule(s) | Behavior |
| --- | --- | --- |
| B4 | `system.header` | Copy reference header if current is absent; exclude it from CL comparison |
| B6:C6 | `system.board` | Select reference Bluetooth/WLAN assignments and include |
| B8 | `system.packages` | Select Bluetooth list members, retaining conditional guards |
| B10:C10, C10 | `system.features`, `csc.features` | Reference product flags; optional JSON-tree missing-key merge |
| B12, B13:B14 | `system.postfs`, `system.boot` | Reference init commands with their events |
| B18 | `vendor.bluetooth` | Copy missing reference directory members |
| B20 | `vendor.board` | Reference Bluetooth board settings |
| B22 | `vendor.packages` | Reference packages, includes and firmware-family assignment |
| B24:C24 | `vendor.init` | Model init commands, with declared JDM handling |
| B26 | `vendor.features` | Reference Bluetooth product flags |
| B28 | `vendor.postfs` | One rule for vendor-template root-init permissions |
| B30:C30 | `vendor.hals` | Verify reference-selected HAL identities/fields; no automatic HAL rewrite |
| B32 | `vendor.hcf` | Select reference copy filter; verify mapped HCF paths; synchronize filter |
| B34:C34 | `vendor.firmware` | Mapped firmware-family path presence only |
| B36 | `device.validation` | Manual device checks |
| B13, B24, B28, C10 | `sheet.review` | Retain worksheet caveats requiring human interpretation |

SLSI routing includes numbered EXYNOS anchors and separate current/reference identifiers. Its firmware lookup, AP candidates, optional current-log extraction and HCF selection live in the rules JSON; comparison policies and exclusions live in `slsi.comparison.json`. QCOM packs likewise link to their own `*.comparison.json` companions. Synthetic pre-refactor normal/blank output bytes are regression-tested using `tests/fixtures/slsi-v1-output.json`.

## QCOM-WCN — `checklist/qcom-wcn.json`

| Worksheet | Rule(s) | Behavior |
| --- | --- | --- |
| B4, B16 | `system.header`, `vendor.header` | Copy missing headers; vendor supports the worksheet's extensionless spelling too |
| B6, B18 | `system.board`, `vendor.board` | Select reference board/WLAN settings |
| B8, B20 | `system.packages`, `vendor.packages` | Select reference Bluetooth/QTI packages |
| B10, B24 | `system.features`, `vendor.features` | Select reference product feature values |
| B12, B22 | `system.init`, `vendor.model_init` | Reference system/model init commands |
| B26:B28 | `vendor.init` | One rule for reference qcom init permissions |
| B30 | `vendor.root_init` | Vendor-template root init commands, including worksheet path groups |
| B34:C34 | `vendor.hals` | Ensure selected reference HAL entries; review differing existing entries |
| B36:C36 | `vendor.firmware` | Look up WCN firmware filename, verify mapped presence |
| B24 | `floating.features` | Manual FloatingFeature review; no unspecified values invented |
| B32, B38 | `device.validation` | Manual build/device checks |

WCN filename mappings are declared for wcn6750, wcn6755, wcn6850, wcn6856, wcn7851 and wcn3990. Platform/model routing and optional current-log identifiers are separate from reference routing. Ambiguous header spellings or platform paths require an explicit selection.

## QCOM-QCA — `checklist/qcom-qca.json`

| Worksheet | Rule(s) | Behavior |
| --- | --- | --- |
| B3:B4 | `vendor.board` | Select reference board settings |
| B5, B9 | `vendor.ant_board`, `vendor.ant_packages` | ANT selection with explicit/inferred eligibility; unknown eligibility reviews |
| B6 | `vendor.offload_board` | Remove selected offload assignments/evaluation/matrix list members inside the declared guard |
| B8 | `vendor.packages` | Select reference Bluetooth packages |
| B10 | `vendor.offload_packages` | Remove the two declared offload packages |
| B11 | `vendor.legacy_include` | Remove the legacy model Bluetooth include |
| B13 | `vendor.header`, `vendor.header_define` | Copy missing reference header; ensure the worksheet's explicit define |
| C13 | `audio.validation` | Manual firmware-history and I2S/PCM review |
| B15 | `vendor.features`, `floating.features` | Reference product flags; manual FloatingFeature review |
| B17 | `vendor.init`, `vendor.slsi_init`, `vendor.slsi_eligibility` | Reference qcom init; explicit supplemental commands when SLSI AP eligibility is established |
| B19 | `vendor.qti_stack` | Remove selected QTI base entries/property |
| B21:C21 | `vendor.firmware`, `device.validation` | Mapped firmware presence plus manual device firmware-version check |
| B23 | `gpio.validation` | Manual GPIO/kernel review; worksheet has no complete applicable patch |
| B25:C25 | `vendor.voltage` | Explicit XML voltage requirement: 3700 |
| B27:C27 | `vendor.manifest`, `vendor.matrix` | Explicit XML HAL requirements |
| B29:C29 | `vendor.hearing_aid` | Ensure declared overlay value when eligible; JDM/MSM8998 excluded |
| B31:C31 | `kernel.hid` | Manual hid-apple patch/build check; no external patch content fabricated |

QCA6174 maps to `btfw32.tlv`; QCA9377 maps to `rampatch_tlv_tf_1.1.tlv`. Firmware verification performs no binary content reads. Applicable reference statements supply values unless the worksheet explicitly gives a literal or removal requirement. Unknown ANT, SLSI AP or hearing-aid applicability is reported rather than treated as success.

## Verification scope

Automated tests use synthetic Perforce Views, source files, revisions and workspace state. They exercise all packs, an unrelated camera pack, repeat-run idempotence, precise removals, latest-file comparison and generic input forms. They do not certify the proprietary source trees, live Android builds, firmware compatibility or phone behavior. Manual rows remain visible in detailed reports so unavailable evidence is not counted as PASS.
