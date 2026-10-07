"""Synthetic offline depot for demonstration and integration tests. No P4 calls."""
from __future__ import annotations

import copy
from pathlib import Path

from .config import validate
from .planner import Planner, seal
from .resolver import pattern_regex


class DemoP4:
    def __init__(self, root):
        self.port, self.user, self.client = "OFFLINE", "demo", "demo_workspace"
        self.root = str(Path(root).resolve())
        self.data, self.specs, self.have, self.opened, self.calls = {}, {}, {}, {}, []
        self.workspace = {"Client": self.client, "Update": "1", "Root": self.root,
                          "LineEnd": "unix", "View0": f"//... //{self.client}/...", "Options": "noallwrite noclobber"}
        self.specs[self.client] = self.workspace

    def identity(self):
        return {"serverAddress": "OFFLINE", "serverID": "synthetic", "userName": self.user, "clientName": self.client}

    def client_spec(self, name):
        return copy.deepcopy(self.specs[name])

    def workspace_spec(self):
        return self.client_spec(self.client)

    def files(self, pattern):
        regex = pattern_regex(pattern)
        return [{"depotFile": path, "rev": str(v[0]), "type": v[2], "action": "add"}
                for path, v in self.data.items() if regex.fullmatch(path)]

    def read_file(self, path, revision=None):
        row = self.data[path]
        if revision is not None and int(revision) != row[0]:
            raise ValueError("Fixture revision not available")
        return row[1]

    def where(self, path):
        return str(Path(self.root) / path[2:])

    def fstat(self, path):
        value = {}
        if path in self.data:
            value.update(headRev=self.data[path][0], headType=self.data[path][2])
        if path in self.have:
            value["haveRev"] = self.have[path]
        if path in self.opened:
            value["action"] = self.opened[path]
        return value

    def enable_writes(self):
        self.calls.append(("enable_writes",))

    def create_change(self, description):
        self.calls.append(("change", description))
        return "12345"

    def sync(self, path, revision):
        self.calls.append(("sync", path))
        local = Path(self.where(path))
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(self.read_file(path, revision))
        self.have[path] = int(revision)

    def edit(self, path, change):
        self.calls.append(("edit", path))
        self.opened[path] = "edit"

    def add(self, path, change, file_type=None):
        self.calls.append(("add", path, file_type))


def fixture(root):
    p4 = DemoP4(root)
    config = {"model": "m36x", "common_device": "m36x_common", "chipset": "s5e8835", "ap": "erd8835",
              "hcf_variant": "m36xxx", "products": ["m36xxx"], "jdm": False, "paths": {},
              "current": {"system_template": "DEMO_SYSTEM_CURRENT", "vendor_template": "DEMO_VENDOR_CURRENT", "csc_path": "//COOSA_CSC/m36x/OMC/ODM"},
              "reference": {"system_template": "DEMO_SYSTEM_REFERENCE", "vendor_template": "DEMO_VENDOR_REFERENCE", "csc_path": "//BENI_CSC/m36x/OMC/ODM"},
              "perforce": {"port": "OFFLINE", "user": "demo", "client": "demo_workspace"}}
    def put(path, value, type="text"):
        p4.data[path] = (1, value.encode("utf-8") if isinstance(value, str) else value, type)
    for role, branch, version in (("current", "COOSA", "ONEUI_9_0/FLUMEN"), ("reference", "BENI", "ONEUI_8_5/ONEUI_8_5_MR202601")):
        for scope in ("system", "vendor"):
            template = config[role][scope + "_template"]
            model_root = f"//MODEL/PROD_{branch}/{version}/Strawberry/EXYNOS/m36x_{'sssi' if scope == 'system' else 'vendor'}"
            system_partition = "SYSTEM_Q2" if role == "current" and scope == "system" else "SYSTEM"
            system_android = f"//{branch}/{scope.upper()}/{system_partition}/Strawberry/ESSI/android"
            vendor_android = f"//{branch}/{scope.upper()}/VENDOR/Strawberry/EXYNOS/android"
            cinnamon = f"//{branch}/{scope.upper()}/VENDOR/Cinnamon"
            spec = {"Client": template, "Update": "1", "Root": "unused",
                    "View0": f"{system_android}/... //{template}/android/system/...",
                    "View1": f"{vendor_android}/... //{template}/android/vendor_platform/...",
                    "View2": f"{cinnamon}/vendor/... //{template}/android/vendor/...",
                    "View3": f"{model_root}/device/m36x_common/... //{template}/android/device/samsung/m36x_common/...",
                    "View4": f"{model_root}/vendor/m36x_common/... //{template}/android/vendor/samsung/configs/m36x_common/..."}
            p4.specs[template] = spec
            put(model_root + "/device/m36x_common/BoardConfigCommon.mk", 'WLAN_VENDOR = 8\nWLAN_CHIP := "s5e8835"\ninclude device/samsung/erd8835/BoardConfig.mk\n')
            if role == "reference":
                path = model_root + "/device/m36x_common/BoardConfigCommon.mk"
                old = p4.data[path][1]
                put(path, old + b'BOARD_HAVE_BLUETOOTH := true\nBOARD_HAVE_BLUETOOTH_SLSI := true\nBOARD_BLUETOOTH_BDROID_BUILDCFG_INCLUDE_DIR := hardware/demo/reference/include\nBLUEDROID_HCI_VENDOR_STATIC_LINKING := false\ninclude vendor/demo/bluetooth/BluetoothBoardConfigCommon.mk\n')
            put(model_root + "/device/m36x_common/device_common.mk", "# Existing model packages\nPRODUCT_PACKAGES += ExistingPackage\n")
            put(model_root + "/device/m36x_common/init.m36x.rc", "on init\n    mkdir /unrelated 0755 root root\n\non post-fs-data\n    mkdir /data/keep 0755 system system\n")
            put(model_root + "/vendor/m36x_common/SecProductFeature.common", "UNRELATED_FEATURE=KEEP\nSEC_PRODUCT_FEATURE_BLUETOOTH_SUPPORT_A2DP_OFFLOAD=" + ("TRUE\n" if role == "reference" else "FALSE\n"))
            put(system_android + "/system/core/rootdir/init.rc", "on post-fs-data\n    mkdir /data/keep 0755 system system\n\non boot\n    setprop unrelated.keep yes\n")
            if role == "reference":
                put(model_root + "/device/m36x_common/Bluetooth/bdroid_buildcfg.h", "#pragma once\n// Synthetic reference header\n")
            if scope == "vendor":
                hals = ''.join(f'<hal format="hidl"><name>{name}</name><transport>hwbinder</transport><version>{ver}</version><interface><name>{interface}</name><instance>default</instance></interface></hal>' for name, ver, interface in [("android.hardware.bluetooth", "1.0", "IBluetoothHci"), ("vendor.samsung.hardware.bluetooth", "2.0", "ISehBluetooth")])
                put(vendor_android + "/device/samsung/erd8835/manifest.xml", "<manifest>" + hals + "</manifest>\n")
                put(cinnamon + "/vendor/samsung/hardware/vendor/bluetooth/slsi/s5e8835/m36xxx/bt.hcf", b"synthetic-hcf", "binary")
                put(cinnamon + "/vendor/samsung/hardware/vendor/bluetooth/slsi/s5e8835/bluetooth.mk", "ifneq ($(filter m36xxx, $(TARGET_PRODUCT)),)\nPRODUCT_COPY_FILES += $(call find-copy-subdir-files,*,$(HCF_PATH)/m36xxx,$(TARGET_COPY_OUT_VENDOR)/firmware/wifi)\nendif\n")
                put(vendor_android + "/vendor/samsung_slsi/mx140/firmware/quartz_s621p/mx140.bin", b"synthetic-firmware", "binary")
        put(config[role]["csc_path"] + "/INS/system/customer_carrier_feature_plan.json", '{"CarrierFeature_BT_EnableSAP": "FALSE", "Keep": true}\n')
    return p4, validate(config)


def demo_plan(directory):
    p4, config = fixture(Path(directory) / "synthetic_workspace")
    plan = Planner(p4, config).build()
    plan["mode"] = "demo"
    plan["source"] += " - SYNTHETIC DATA; not real M36X chipset or firmware evidence"
    return seal(plan)
