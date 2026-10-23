"""Workbook-based WCN/QCA fixtures exercise JSON policy through generic workflows."""

import base64
import copy
from pathlib import Path
import tempfile
import unittest

from bt_delta.blank import BlankPlanner
from bt_delta.demo import DemoP4
from bt_delta.expressions import Values
from bt_delta.planner import Planner
from bt_delta.spec import load_spec, set_value

ROOT = Path(__file__).resolve().parents[1]
OFFLOAD = """ifeq ($(shell secspfbool SEC_PRODUCT_FEATURE_BLUETOOTH_SUPPORT_A2DP_OFFLOAD), y)
TARGET_HAS_SPLIT_A2DP_FEATURE := true
TARGET_HAS_BTHOST_IPC := true
$(eval $(call add-device-manifest, vendor/samsung/interfaces/bluetooth/a2dp/1.0/manifest.xml))
ifndef SEC_BT_DEVICE_FRAMEWORK_COMPATIBILITY_MATRIX_FILE
SEC_BT_DEVICE_FRAMEWORK_COMPATIBILITY_MATRIX_FILE := vendor/samsung/interfaces/bluetooth/a2dp/1.0/framework_compatibility_matrix.xml
ifdef DEVICE_FRAMEWORK_COMPATIBILITY_MATRIX_FILE
DEVICE_FRAMEWORK_COMPATIBILITY_MATRIX_FILE += $(SEC_BT_DEVICE_FRAMEWORK_COMPATIBILITY_MATRIX_FILE)
endif
endif
endif
"""
HALS = """<manifest>
<hal format="hidl"><name>android.hardware.bluetooth</name><transport>hwbinder</transport><version>1.0</version><interface><name>IBluetoothHci</name><instance>default</instance></interface></hal>
<hal format="hidl"><name>vendor.samsung.hardware.bluetooth</name><transport>hwbinder</transport><version>2.0</version><interface><name>ISehBluetooth</name><instance>default</instance></interface></hal>
</manifest>"""


def fixture(root, name, **overrides):
    spec = load_spec(ROOT / "checklist" / f"{name}.json")
    config = {
        "model": "a23xq" if name == "qcom-wcn" else "j4corelte",
        "radio_chip": "wcn3990" if name == "qcom-wcn" else "qca9377",
        "board_platform": "holi" if name == "qcom-wcn" else "msm8937_32go",
        "ant_support": "enabled",
        "slsi_ap": "yes",
        "hearing_aid": "enabled",
        "jdm": False,
        "perforce": {"port": "OFFLINE", "user": "demo", "client": "demo_workspace"},
        **overrides,
    }
    p4 = DemoP4(root)
    reads = []
    paths = {}
    for source, data in spec["sources"].items():
        template = source.replace(".", "_").upper()
        set_value(config, data["input"], template)
        p4.specs[template] = {
            "Client": template,
            "View0": f"//PACK/{source}/... //{template}/...",
        }
    values = Values(spec, config)
    # The provided fixture inputs resolve paths; content and expected results below
    # are independent, concrete workbook examples rather than selector-generated text.
    for key, file in spec["files"].items():
        route = next(
            r["client_relative"] for r in file["routes"] if "client_relative" in r
        )
        path = "//PACK/" + file["source"] + "/" + values.template(route)
        paths[key] = path
        kind = key.rsplit(".", 1)[-1]
        reference = key.startswith("reference.")
        if kind == "board":
            text = (
                (
                    "WLAN_VENDOR := 4\nWLAN_CHIP := "
                    + config["radio_chip"]
                    + "\nTARGET_BOARD_PLATFORM := "
                    + config["board_platform"]
                    + "\n"
                    "BOARD_HAVE_BLUETOOTH := true\nBOARD_HAS_QCA_BT_ROME := true\nBOARD_HAVE_BLUETOOTH_QCOM := true\n"
                    "WCNSS_FILTER_USES_SIBS := false\nBUILD_BT_WCNSS := true\nBOARD_HAS_QCA_BT_ROME_PCM_SLAVE := true\n"
                    'BLUETOOTH_UART_DEVICE_PORT = "/dev/ttyHS0"\nBOARD_BLUETOOTH_BDROID_BUILDCFG_INCLUDE_DIR := device/samsung/'
                    + config["model"]
                    + "_common/Bluetooth\n"
                    'BOARD_HAVE_ANT_WIRELESS := true\nBOARD_ANT_WIRELESS_DEVICE := "qualcomm-uart"\n'
                )
                if reference
                else "KEEP := yes\n" + (OFFLOAD if name == "qcom-qca" else "")
            )
        elif kind == "packages":
            text = (
                (
                    "PRODUCT_PACKAGES += BluetoothAgent android.hardware.bluetooth@1.0-service-qti android.hardware.bluetooth@1.0-impl-qti android.hardware.bluetooth@1.0-service-qti.rc\n"
                    "PRODUCT_PACKAGES += com.dsi.ant.antradio_library com.dsi.ant.antradio_library.xml com.qualcomm.qti.ant@1.0-impl com.qualcomm.qti.ant@1.0 com.qualcomm.qti.ant@1.0_vendor AntHalService ANTPlusPlugins ANTRadioService ANTPlusTest\n"
                )
                if reference
                else (
                    "PRODUCT_PACKAGES += KeepPackage\n"
                    + (
                        "ifeq ($(shell secspfbool SEC_PRODUCT_FEATURE_BLUETOOTH_SUPPORT_A2DP_OFFLOAD), y)\nPRODUCT_PACKAGES += vendor.samsung.hardware.bluetooth.a2dp@1.0-impl libbthost_if KeepOffloadPackage\nendif\ninclude device/samsung/"
                        + config["model"]
                        + "_common/Bluetooth/Bluetooth.mk\n"
                        if name == "qcom-qca"
                        else ""
                    )
                )
            )
        elif kind == "features":
            text = (
                "SEC_PRODUCT_FEATURE_BLUETOOTH_SUPPORT_DUAL_PLAY=TRUE\n"
                if reference
                else "KEEP=TRUE\n"
            )
        elif kind == "header":
            if not reference:
                continue
            text = "#define BOARD_HAVE_BLUETOOTH_QCOM FALSE\n#define KEEP_THIS TRUE\n"
        elif kind in ("init", "root_init", "model_init"):
            text = (
                (
                    "on post-fs-data\n    mkdir /data/misc/bluetooth 0770 bluetooth bluetooth\n    mkdir /data/log/bt 0770 bluetooth bluetooth\n"
                    "    chown bluetooth bluetooth /dev/ttyHS0\n    chmod 0660 /sys/kernel/boot_slpi/ssr\n"
                    "    chmod 0660 /dev/btpower\n    chown bluetooth system /dev/btpower\n    mkdir /data/vendor/ssrdump 0770 root system\n"
                )
                if reference
                else "on boot\n    mkdir /data/keep 0755 system system\n"
            )
        elif kind == "base":
            text = "TARGET_USE_QTI_BT_STACK := true\nifeq ($(TARGET_USE_QTI_BT_STACK), true)\nBT += libbluetooth_qti KeepStack\nendif\n"
        elif kind == "props":
            text = (
                "ro.bluetooth.library_name=libbluetooth_qti.so\nother.property=keep\n"
            )
        elif kind == "overlay":
            text = '<resources><!-- keep --><string name="unrelated">keep</string></resources>'
        elif kind == "manifest":
            text = HALS if reference else "<manifest><!-- keep --></manifest>"
        elif kind == "matrix":
            text = "<compatibility-matrix/>"
        elif kind == "firmware":
            p4.data[path] = (1, b"firmware must not be read", "binary")
            continue
        else:
            raise AssertionError(kind)
        p4.data[path] = (1, text.encode(), "text")
    original = p4.read_file

    def read(path, revision=None):
        reads.append(path)
        if path == paths["current.vendor.firmware"]:
            raise AssertionError("Firmware check must not read binary bytes")
        return original(path, revision)

    p4.read_file = read
    return p4, config, spec, paths, reads


class QualcommTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def run_pack(self, name, **kw):
        self.p4, self.config, self.spec, self.paths, self.reads = fixture(
            self.root, name, **kw
        )
        return self.build()

    def build(self, blank=False):
        return (BlankPlanner if blank else Planner)(
            self.p4, self.config, self.spec
        ).build()

    def changes(self, plan):
        return {
            c["path"]: base64.b64decode(c["after"]).decode(errors="replace")
            for c in plan["changes"]
        }

    def assert_clean(self, plan):
        self.assertFalse(
            [c for c in plan["checks"] if c["status"] in ("blocked", "review")],
            plan["checks"],
        )

    def checks(self, plan):
        return {c["rule"]: c for c in plan["checks"]}

    def test_comment_selected_firmware_and_includes_in_both_plan_modes(self):
        from test_make_content import MakeContentTests

        for pack in ("qcom-wcn",):
            self.p4, self.config, self.spec, self.paths, self.reads = fixture(self.root, pack)
            source = self.paths["reference.vendor.packages"]
            sample = MakeContentTests.sample.replace("xcover7pro", self.config["model"])
            self.p4.data[source] = (1, (sample + "# Camera\nPRODUCT_PACKAGES += CameraOnly\n").encode(), "text")
            for blank in (False, True):
                with self.subTest(pack=pack, blank=blank):
                    plan = self.build(blank)
                    self.assertEqual(self.checks(plan)["vendor.packages"]["status"], "change")
                    content = self.changes(plan)[self.paths["current.vendor.packages"]]
                    for member in ("msbtfw12.mbn", "msbtfw12.tlv", "msnv12.bin", "btcoex_cont_config"):
                        self.assertIn(member, content)
                    self.assertIn("include vendor/samsung/hardware/vendor/bluetooth/bluetooth_device.mk", content)
                    self.assertIn("include device/samsung/" + self.config["model"] + "_common/Bluetooth/Bluetooth.mk", content)
                    self.assertNotIn("CameraOnly", content)

    def test_wcn_all_reported_init_rules_scan_repeated_events(self):
        self.p4, self.config, self.spec, self.paths, self.reads = fixture(self.root, "qcom-wcn")
        cases = (
            ("system.init", "system.root_init", "on property:sys.boot_completed=1"),
            ("vendor.model_init", "vendor.model_init", "on post-fs"),
            ("vendor.init", "vendor.init", "on property:sys.boot_completed=1"),
            ("vendor.root_init", "vendor.root_init", "on late-fs"),
        )
        for _, file_id, event in cases:
            commands = (["mkdir /data/log/bt 0770 bluetooth bluetooth", "mkdir /data/log 0775 system log"]
                        if file_id == "vendor.root_init" else
                        ["mkdir /data/misc/bluetooth 0770 bluetooth bluetooth", "chmod 0660 /data/misc/bluetooth/config"])
            reference = (event + "\n    setprop unrelated.one yes\n" + event +
                         "\n    " + commands[0] + "\n" + event + "\n    " + commands[1] + "\n")
            current = event + "\n    setprop unrelated.current yes\n" + event + "\n    setprop keep.current yes\n"
            self.p4.data[self.paths["reference." + file_id]] = (1, reference.encode(), "text")
            self.p4.data[self.paths["current." + file_id]] = (1, current.encode(), "text")
        for blank in (False, True):
            with self.subTest(blank=blank):
                plan = self.build(blank)
                for rule, file_id, event in cases:
                    self.assertEqual(self.checks(plan)[rule]["status"], "change", self.checks(plan)[rule])
                    content = self.changes(plan)[self.paths["current." + file_id]]
                    self.assertIn(event, content)
                    if file_id == "vendor.root_init":
                        self.assertEqual(content.count("mkdir /data/log/bt"), 1)
                        self.assertIn("mkdir /data/log 0775 system log", content)
                    else:
                        self.assertEqual(content.count("mkdir /data/misc/bluetooth"), 1)
                        self.assertIn("chmod 0660 /data/misc/bluetooth/config", content)
                    self.assertNotIn("unrelated.one", content)

    def test_wcn_reference_and_firmware_workflow(self):
        plan = self.run_pack("qcom-wcn")
        self.assert_clean(plan)
        changes = self.changes(plan)
        self.assertIn(
            "WLAN_CHIP := wcn3990", changes[self.paths["current.vendor.board"]]
        )
        self.assertIn(
            "android.hardware.bluetooth@1.0-service-qti",
            changes[self.paths["current.vendor.packages"]],
        )
        self.assertIn("IBluetoothHci", changes[self.paths["current.vendor.manifest"]])
        self.assertIn("ISehBluetooth", changes[self.paths["current.vendor.manifest"]])
        self.assertIn(
            "/sys/kernel/boot_slpi/ssr", changes[self.paths["current.vendor.init"]]
        )
        self.assertEqual("pass", self.checks(plan)["vendor.firmware"]["status"])
        self.assertFalse(self.p4.calls)

    def test_qca_required_removals_and_additions(self):
        plan = self.run_pack("qcom-qca")
        self.assert_clean(plan)
        changes = self.changes(plan)
        board = changes[self.paths["current.vendor.board"]]
        self.assertNotIn("TARGET_HAS_SPLIT_A2DP_FEATURE", board)
        self.assertNotIn("add-device-manifest", board)
        self.assertNotIn("SEC_BT_DEVICE_FRAMEWORK_COMPATIBILITY_MATRIX_FILE", board)
        self.assertIn('BOARD_ANT_WIRELESS_DEVICE := "qualcomm-uart"', board)
        packages = changes[self.paths["current.vendor.packages"]]
        self.assertNotIn("libbthost_if", packages)
        self.assertNotIn("Bluetooth/Bluetooth.mk", packages)
        self.assertIn("KeepOffloadPackage", packages)
        self.assertIn("ANTPlusPlugins", packages)
        self.assertNotIn("libbluetooth_qti", changes[self.paths["current.vendor.base"]])
        self.assertIn("KeepStack", changes[self.paths["current.vendor.base"]])
        self.assertEqual(
            "other.property=keep\n", changes[self.paths["current.vendor.props"]]
        )
        header = changes[self.paths["current.vendor.header"]]
        self.assertIn("#define BOARD_HAVE_BLUETOOTH_QCOM TRUE", header)
        self.assertNotIn("QCOM FALSE", header)
        self.assertIn("KEEP_THIS", header)
        overlay = changes[self.paths["current.vendor.overlay"]]
        self.assertIn(">3700<", overlay)
        self.assertIn("config_hearing_aid_profile_supported", overlay)
        self.assertIn("<!-- keep -->", overlay)
        self.assertFalse(self.p4.calls)

    def test_qca_skips_ant_and_hearing_for_ineligible_model(self):
        plan = self.run_pack("qcom-qca", ant_support="disabled", jdm=True, slsi_ap="no")
        self.assert_clean(plan)
        checks = self.checks(plan)
        self.assertEqual("skipped", checks["vendor.ant_board"]["status"])
        self.assertEqual("skipped", checks["vendor.ant_packages"]["status"])
        self.assertEqual("skipped", checks["vendor.hearing_aid"]["status"])
        self.assertEqual("skipped", checks["vendor.slsi_init"]["status"])
        self.assertNotIn(
            "ANTPlusPlugins", self.changes(plan)[self.paths["current.vendor.packages"]]
        )

    def test_qca_unknown_eligibility_is_review_not_automatic(self):
        self.run_pack(
            "qcom-qca", ant_support="infer", hearing_aid="infer", slsi_ap="unknown"
        )
        board = self.paths["reference.vendor.board"]
        rev, text, kind = self.p4.data[board]
        self.p4.data[board] = (
            rev,
            text.replace(b'BOARD_ANT_WIRELESS_DEVICE := "qualcomm-uart"\n', b""),
            kind,
        )
        plan = self.build()
        checks = self.checks(plan)
        self.assertEqual("review", checks["vendor.ant_board"]["status"])
        self.assertEqual("review", checks["vendor.ant_packages"]["status"])
        self.assertIn(checks["vendor.slsi_eligibility"]["status"], ("manual", "review"))
        self.assertNotIn(
            "ANTPlusPlugins", self.changes(plan)[self.paths["current.vendor.packages"]]
        )

    def test_both_packs_idempotent_and_blank_read_only(self):
        for name in ("qcom-wcn", "qcom-qca"):
            with self.subTest(name=name):
                plan = self.run_pack(name)
                self.assert_clean(plan)
                for change in plan["changes"]:
                    self.p4.data[change["path"]] = (
                        2,
                        base64.b64decode(change["after"]),
                        change["type"],
                    )
                again = self.build()
                self.assertFalse(again["changes"])
                # QCA's intentional define change makes copy-if-missing report a
                # preserved header difference on subsequent runs, never a rewrite.
                self.assertFalse(
                    [c for c in again["checks"] if c["status"] == "blocked"]
                )
                self.reads.clear()
                blank = self.build(blank=True)
                self.assertFalse(
                    [c for c in blank["checks"] if c["status"] == "blocked"]
                )
                self.assertFalse([p for p in self.reads if "/current." in p])
                self.assertFalse(self.p4.calls)

    def test_current_build_log_changes_only_current_target_paths(self):
        self.run_pack("qcom-wcn")
        log = self.root / "build.log"
        log.write_text("TARGET: nextmodel\ntarget_board: kona\n")
        self.config["current_log_path"] = str(log)
        # Move fixture destinations to the logged current layout.
        for path, row in list(self.p4.data.items()):
            if "/current." in path:
                moved = path.replace("a23xq", "nextmodel").replace("/holi/", "/kona/")
                if moved != path:
                    self.p4.data[moved] = self.p4.data.pop(path)
        plan = self.build()
        self.assert_clean(plan)
        self.assertEqual("a23xq", plan["variables"]["reference.model"])
        self.assertEqual("holi", plan["variables"]["reference.ap"])
        self.assertEqual("nextmodel", plan["variables"]["current.model"])
        self.assertEqual("kona", plan["variables"]["current.ap"])
        self.assertTrue(
            any("/kona/framework_manifest.xml" in c["path"] for c in plan["changes"])
        )

    def test_unknown_firmware_family_requires_review_without_firmware_read(self):
        self.run_pack("qcom-wcn")
        self.config["radio_chip"] = "unlisted"
        plan = self.build()
        self.assertEqual("review", self.checks(plan)["vendor.firmware"]["status"])
        self.assertNotIn(self.paths["current.vendor.firmware"], self.reads)
