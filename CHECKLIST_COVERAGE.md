# SLSI checklist coverage

Source: the corrected full SLSI text supplied by the user, preserved as `checklist/source_slsi.tsv`. Example branch/model/chip paths are resolved using actual current/reference template views.

| Cells | Rule IDs | Behavior |
|---|---|---|
| B4 | system.header | Copy reference bdroid header only if target is absent |
| B6:C6 | system.board | Copy present reference WLAN/BT settings and optional board include; skip absent selectors |
| B8 | system.packages | Reference-selected Bluetooth packages; no default package insertion |
| B10:C10 | system.features | Reference Bluetooth product flags; preserve current-only flags for review |
| C10 | csc.features | Find only customer_carrier_feature_plain.json; check complete region subtrees and report genuinely missing regions with their file paths/types across the entire model; add missing files/keys at identical relative paths and preserve existing values (user-requested scope) |
| B12 | system.postfs | Reference-selected Bluetooth commands in on post-fs-data |
| B13:B14 | system.boot | Reference-selected boot logging, UART, BD address, EFS and dump commands |
| B18 | vendor.bluetooth | Copy only missing reference Bluetooth files |
| B20 | vendor.board | Copy present reference BT settings and optional board include; preserve current-only settings |
| B22 | vendor.packages | Reference-selected packages, includes and firmware assignment values/operators |
| B24:C24 | vendor.init | Discover init.<model>.rc or init.model.rc; reference commands in matching events; explicit JDM EFS conversion |
| B26 | vendor.features | Reference vendor Bluetooth product flags |
| B28 | vendor.postfs | Vendor-template reference system root init commands in post-fs-data |
| B30:C30 | vendor.hals | Verify reference-selected Bluetooth HIDL/AIDL entries; detailed differences for review |
| B32 | vendor.hcf | Current HCF existence only; exact parent bluetooth.mk; reference Make filter comparison, no reference HCF query or binary copy |
| B34:C34 | vendor.firmware | Reference/current revision and hash comparison; optional absence and explicit approved-hash review |
| B36 | device.validation | Post-build phone address and *#2663# checks |

The separate Feature Flags tab remains a manual eligibility reference until its contents are supplied. The implemented SLSI instruction to refer to the previous OS is used for concrete product values. No old ANT, obsolete firmware aliases or old CSV checklist rules are imported.
