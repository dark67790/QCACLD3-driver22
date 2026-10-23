# QCACLD3-driver22

This repository contains a Samsung QCACLD3 packet-injection patch and a script that applies it to the matching Samsung WLAN source tree. The GitHub Actions workflow downloads the source and toolchain, applies the patch, prepares kernel headers, and builds the external `wlan.ko` module.

Run the workflow manually from **Actions → Build QCACLD3 WLAN module → Run workflow**. The stripped `wlan.ko` is published as the `qcacld3-wlan-ko` artifact.

The workflow uses an empty `Module.symvers` when preparing headers, so modpost cannot validate kernel symbol versions. A successful compile does not establish that the artifact will load on a device; it must match that device's kernel ABI and symbol versions.
