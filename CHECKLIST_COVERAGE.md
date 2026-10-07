# SLSI checklist coverage

Source: the corrected full SLSI text supplied by the user, preserved as `checklist/source_slsi.tsv`. Example branch/model/chip paths are resolved using actual current/reference template views.

| Cells | Rule IDs | Behavior |
|---|---|---|
| B4 | system.header | Copy reference bdroid header only if target is absent |
| B6:C6 | system.board | Copy selected WLAN/BT assignments and board include from reference system BoardConfig; no static values |
| B8 | system.packages | BluetoothAgent |
| B10:C10 | system.features | Reference Bluetooth product flags; preserve current-only flags for review |
| C10 | csc.features | Find only customer_carrier_feature_plain.json; check complete region subtrees and report genuinely missing regions with their file paths/types across the entire model; add missing files/keys at identical relative paths and preserve existing values (user-requested scope) |
| B12 | system.postfs | Commands in on post-fs-data |
| B13:B14 | system.boot | Boot logging, UART, BD address, EFS and dump permissions |
| B18 | vendor.bluetooth | Copy only missing reference Bluetooth files |
| B20 | vendor.board | Copy four BT assignments and board include from reference vendor BoardConfig; no static values |
| B22 | vendor.packages | 1.1 HAL packages, vendor/chip includes and mapped firmware family |
| B24:C24 | vendor.init | Correct init events; JDM EFS prefix |
| B26 | vendor.features | Reference vendor Bluetooth product flags |
| B28 | vendor.postfs | Vendor-template system root init; inferred post-fs-data event shown for review |
| B30:C30 | vendor.hals | Verify Android HIDL 1.0 and Samsung HIDL 2.0 entries |
| B32 | vendor.hcf | HCF presence and selected products' copy filter |
| B34:C34 | vendor.firmware | Four sheet-defined chipset/firmware mappings; revision/hash/release review |
| B36 | device.validation | Post-build phone address and *#2663# checks |

The separate Feature Flags tab remains a manual eligibility reference until its contents are supplied. The implemented SLSI instruction to refer to the previous OS is used for concrete product values. No old ANT, obsolete firmware aliases or old CSV checklist rules are imported.
