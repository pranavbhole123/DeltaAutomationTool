"""SLSI checklist rules. Paths are logical targets resolved from template views.

Source: checklist/source_slsi.tsv. Sample a35x/branch/chip values are not defaults.
Add a rule here or supply a JSON catalog with the same schema to extend the tool.
"""
from __future__ import annotations
from .csc import CARRIER_FILENAME

CHIPSETS = {"s5e8535": "rice_s620", "s5e8835": "quartz_s621p",
            "s5e8825": "papaya_s620", "s5e8845": "rose_s621p"}

# Stable anchors come from template View mappings. Edit or append a route when
# a future branch changes layout; release/depot prefixes are never hardcoded.
PATH_RULES = {
    "system": {
        "board_config": {"anchor": "/EXYNOS/", "relative": "{model}_sssi/device/{common_device}/BoardConfigCommon.mk"},
        "bluetooth_header": {"anchor": "/EXYNOS/", "relative": "{model}_sssi/device/{common_device}/Bluetooth/bdroid_buildcfg.h"},
        "device_common": {"anchor": "/EXYNOS/", "relative": "{model}_sssi/device/{common_device}/device_common.mk"},
        "sec_product": {"anchor": "/EXYNOS/", "relative": "{model}_sssi/vendor/{common_device}/SecProductFeature.common"},
        "root_init": {"anchor": "/ESSI/android/", "relative": "system/core/rootdir/init.rc"}
    },
    "vendor": {
        "bluetooth_folder": {"anchor": "/EXYNOS/", "relative": "{model}_vendor/device/{common_device}/Bluetooth"},
        "board_config": {"anchor": "/EXYNOS/", "relative": "{model}_vendor/device/{common_device}/BoardConfigCommon.mk"},
        "device_common": {"anchor": "/EXYNOS/", "relative": "{model}_vendor/device/{common_device}/device_common.mk"},
        "model_init": [
            {"anchor": "/EXYNOS/", "relative": "{model}_vendor/device/{common_device}/init.{model}.rc"},
            {"anchor": "/EXYNOS/", "relative": "{model}_vendor/device/{common_device}/init.model.rc"}
        ],
        "sec_product": {"anchor": "/EXYNOS/", "relative": "{model}_vendor/vendor/{common_device}/SecProductFeature.common"},
        "root_init": {"anchor": "/ESSI/android/", "relative": "system/core/rootdir/init.rc"},
        "manifest": {"anchor": "/EXYNOS/android/", "relative": "device/samsung/{ap}/manifest.xml"},
        "hcf_makefile": {"anchor": "/VENDOR/Cinnamon/vendor/", "relative": "samsung/hardware/vendor/bluetooth/slsi/{chipset}/bluetooth.mk"},
        "hcf": {"anchor": "/VENDOR/Cinnamon/vendor/", "relative": "samsung/hardware/vendor/bluetooth/slsi/{chipset}/{hcf_variant}"},
        "firmware": {"anchor": "/EXYNOS/android/", "relative": "vendor/samsung_slsi/mx140/firmware/{firmware}/mx140.bin"}
    }
}

BT_KEYS = ["BOARD_HAVE_BLUETOOTH", "BOARD_HAVE_BLUETOOTH_SLSI",
           "BOARD_BLUETOOTH_BDROID_BUILDCFG_INCLUDE_DIR", "BLUEDROID_HCI_VENDOR_STATIC_LINKING"]
BT_NAME_PATTERN = r"(?i)(?:bluetooth|bluedroid|bdroid|(?:^|_)bt(?:_|$)|(?:^|_)a2dp(?:_|$))"
BT_PACKAGE_PATTERN = r"(?i)(?:bluetooth|bluedroid|bdroid|libbt(?:[-_.]|$)|(?:^|[-_.])bt(?:[-_.]|$)|a2dp)"
BT_INCLUDE_PATTERN = r"(?i)(?:bluetooth|bluedroid|bdroid|(?:^|[/_.-])bt(?:[/_.-]|$)|a2dp)"
BT_INIT_PATTERN = r"(?i)(?:bluetooth|bluedroid|bdroid|bt_config|bdaddr|btpower|scsc_bt|(?:^|[./_-])bt(?:[./_-]|$)|/proc/bluetooth|ssrdump)"
BT_COMMENT_PATTERN = r"(?i)(?:\bbluetooth\b|\bbluedroid\b|\bbt\b|\ba2dp\b)"
POST_FS = ["mkdir /data/misc/bluedroid 02770 bluetooth bluetooth",
           "chmod 0660 /data/misc/bluedroid/bt_config.conf",
           "chown bluetooth bluetooth /data/misc/bluedroid/bt_config.conf",
           "mkdir /data/misc/bluetooth 0770 bluetooth bluetooth",
           "mkdir /data/misc/bluetooth/logs 0770 bluetooth bluetooth"]
EFS = ['setprop ro.bt.bdaddr_path "{efs}/bluetooth/bt_addr"',
       "chown bluetooth bluetooth ro.bt.bdaddr_path",
       "mkdir {efs}/bluetooth 0770 system bluetooth",
       "chown system bluetooth {efs}/bluetooth",
       "chown system bluetooth {efs}/bluetooth/bt_addr",
       "chmod 0770 {efs}/bluetooth", "chmod 0660 {efs}/bluetooth/bt_addr"]


def rule(id, title, source, scope, target, kind, **extra):
    return dict(id=id, title=title, source="SLSI!" + source, scope=scope,
                target=target, kind=kind, **extra)


def default_catalog():
    rules = [
        rule("system.header", "Copy reference bdroid_buildcfg.h if absent", "B4", "system", "bluetooth_header", "copy_if_reference"),
        rule("system.board", "Copy reference system Bluetooth board settings", "B6:C6", "system", "board_config", "reference_make_settings",
             keys=["WLAN_VENDOR", "WLAN_CHIP", *BT_KEYS],
             key_patterns=[BT_NAME_PATTERN],
             include_basenames=["BluetoothBoardConfigCommon.mk"]),
        rule("system.packages", "System BluetoothAgent package", "B8", "system", "device_common", "transform",
             actions=[{"type": "make_packages", "packages": ["BluetoothAgent"],
                       "reference_package_patterns": [BT_PACKAGE_PATTERN]}]),
        rule("system.features", "Compare system Bluetooth product features with reference", "B10:C10", "system", "sec_product", "reference_features",
             prefix="SEC_PRODUCT_FEATURE_BLUETOOTH_", key_patterns=[BT_NAME_PATTERN], format="make"),
        rule("csc.features", "Add missing customer_carrier_feature_plain.json files and keys across all regions", "C10", "csc", "carrier_features", "carrier_features",
             filename=CARRIER_FILENAME),
        rule("system.postfs", "System post-fs-data Bluetooth permissions", "B12", "system", "root_init", "transform",
             actions=[{"type": "init_commands", "event": "on post-fs-data", "commands": POST_FS,
                       "reference_command_patterns": [BT_INIT_PATTERN], "reference_comment_patterns": [BT_COMMENT_PATTERN]}]),
        rule("system.boot", "System boot Bluetooth paths and permissions", "B13:B14", "system", "root_init", "transform",
             actions=[{"type": "init_commands", "event": "on boot", "commands": [
                 "mkdir /data/log 0775 system log", "mkdir /data/log/bt 0770 bluetooth bluetooth",
                 "chown bluetooth bluetooth /dev/ttySAC1", "chmod 0660 /dev/ttySAC1", *EFS,
                 "chmod 0660 /dev/btpower", "chown bluetooth system /dev/btpower",
                 "mkdir /data/vendor/ssrdump 0770 root system", "chown bluetooth log /proc/bluetooth/uart/log"],
                       "reference_command_patterns": [BT_INIT_PATTERN], "reference_comment_patterns": [BT_COMMENT_PATTERN]}]),
        rule("vendor.bluetooth", "Copy missing files from reference Bluetooth folder", "B18", "vendor", "bluetooth_folder", "copy_tree_if_reference"),
        rule("vendor.board", "Copy reference vendor Bluetooth board settings", "B20", "vendor", "board_config", "reference_make_settings",
             keys=BT_KEYS, key_patterns=[BT_NAME_PATTERN], include_basenames=["BluetoothBoardConfigCommon.mk"]),
        rule("vendor.packages", "Vendor packages, includes and firmware family", "B22", "vendor", "device_common", "transform", actions=[
            {"type": "make_packages", "packages": ["libbt-vendor", "android.hardware.bluetooth@1.1-impl", "android.hardware.bluetooth@1.1-service"],
             "reference_package_patterns": [BT_PACKAGE_PATTERN]},
            {"type": "ensure_lines", "lines": ["include vendor/samsung/hardware/vendor/bluetooth/bluetooth_device.mk",
                                                   "include vendor/samsung/hardware/vendor/bluetooth/slsi/{chipset}/bluetooth.mk"],
             "reference_line_patterns": [BT_INCLUDE_PATTERN]},
            {"type": "assignments", "values": {"SLSI_WLBT_UNIFIED_FIRMWARE": "{firmware}"}}]),
        rule("vendor.init", "Vendor model init permissions (JDM aware)", "B24:C24", "vendor", "model_init", "transform", actions=[
            {"type": "init_commands", "event": "on init", "commands": ["chown bluetooth bluetooth /sys/module/scsc_bt/parameters/bluetooth_address"],
             "reference_command_patterns": [BT_INIT_PATTERN], "reference_comment_patterns": [BT_COMMENT_PATTERN]},
            {"type": "init_commands", "event": "on post-fs-data", "commands": EFS,
             "reference_command_patterns": [BT_INIT_PATTERN], "reference_comment_patterns": [BT_COMMENT_PATTERN]}]),
        rule("vendor.features", "Compare vendor Bluetooth product features with reference", "B26", "vendor", "sec_product", "reference_features",
             prefix="SEC_PRODUCT_FEATURE_BLUETOOTH_", key_patterns=[BT_NAME_PATTERN], format="make"),
        rule("vendor.postfs", "Vendor-template system root init permissions", "B28", "vendor", "root_init", "transform",
             actions=[{"type": "init_commands", "event": "on post-fs-data", "commands": POST_FS,
                       "reference_command_patterns": [BT_INIT_PATTERN], "reference_comment_patterns": [BT_COMMENT_PATTERN]}]),
        rule("vendor.hals", "Verify reference Bluetooth HIDL/AIDL entries", "B30:C30", "vendor", "manifest", "verify_hals",
             name_patterns=[r"(?i)(?:bluetooth|bluedroid|bdroid|(?:^|[._-])bt(?:[._-]|$)|a2dp)"],
             expected_hals=[{"name": "android.hardware.bluetooth", "version": "1.0", "interface": "IBluetoothHci"},
                            {"name": "vendor.samsung.hardware.bluetooth", "version": "2.0", "interface": "ISehBluetooth"}]),
        rule("vendor.hcf", "Verify HCF files and TARGET_PRODUCT copy filter", "B32", "vendor", "hcf", "verify_hcf"),
        rule("vendor.firmware", "Verify firmware and approved release", "B34:C34", "vendor", "firmware", "verify_firmware"),
        rule("device.validation", "After build: verify phone Bluetooth address and firmware", "B36", "device", "phone", "manual",
             notes="Compare the phone BT address with {efs}/bluetooth/bt_addr. Check BT firmware with *#2663#. Requires a built device."),
    ]
    return {"schema_version": 1, "source": "159551_BT Delta Checklist.xlsx / SLSI",
            "chipsets": CHIPSETS,
            "path_rules": PATH_RULES,
            "model_common_files": {"bluetooth_header": ["Bluetooth/bdroid_buildcfg.h"]},
            "rules": rules}
