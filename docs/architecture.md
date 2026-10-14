# Architecture

![SLSI Bluetooth Delta architecture](architecture.svg)

[Open the SVG](architecture.svg) · [Editable Mermaid source](architecture.mmd)

- Planning and comparison read Perforce; applying an approved normal plan creates pending workspace edits.
- Discovery respects template Views. EXYNOS anchors support numeric suffixes; HCF and `bluetooth.mk` use the common `vendor/samsung/hardware/vendor` suffix independently of Cinnamon.
- HIDL/AIDL rules verify manifests. Firmware checks mapped path presence. HCF files are verified, while the selected `bluetooth.mk` filter can be updated.
- Comparison checks latest target content before reporting a suggestion missing. Init presence checks scan the entire file; `bdroid_buildcfg.h` is excluded from comparison.
- GUI work runs in a background thread with queued UI updates. Progress feeds Live log and runtime logging. `DemoP4` supplies synthetic offline data.

The SVG is a static layout of the architecture. Keep it and the Mermaid source in sync when changing the flow.
