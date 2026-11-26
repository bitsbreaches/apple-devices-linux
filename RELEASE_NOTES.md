# Device Dock 1.2.0 — First Open Source Release (Public Beta)

**Device Dock** is a native GTK 4 / libadwaita application for managing iPhones, their files
and local iTunes/Finder backups on Linux. It is now **fully open source** under the
[GNU General Public License v3.0 only](LICENSE) (GPL-3.0-only).

Copyright © 2026 Device Dock Contributors.

## Install

Ubuntu 24.04 is the supported platform.

```bash
sha256sum -c device-dock_1.2.0_amd64.deb.sha256
sudo apt install ./device-dock_1.2.0_amd64.deb
device-dock                     # or launch “Device Dock” from the application menu
```

The package carries its own CPython runtime and Python libraries and does not use or depend
on the system Python. GTK 4, libadwaita, libimobiledevice, ifuse, usbmuxd and GStreamer
remain distribution packages, listed in `Depends`.

## What changed in 1.2.0

* **Renamed to Device Dock.** Package `device-dock`, command `device-dock`, Python package
  `devicedock`, application ID `io.github.bitsbreaches.DeviceDock`, repository
  [bitsbreaches/device-dock](https://github.com/bitsbreaches/device-dock).
* **Your data moves with you.** On first launch, settings, logs, cached data and the backup
  library are moved from the former `appledevices` directories to the new ones. The move is
  a rename, so backups are never copied or deleted, and an interrupted upgrade leaves either
  the old or the new location intact. Stored backup passwords remain available; entries from
  the old keyring schema are migrated on first read.
* **Clean upgrade.** The package `Breaks`/`Replaces` `apple-devices`, so `apt` removes the
  old launcher and desktop entry instead of leaving a duplicate.
* **New icon.** A neutral device-in-a-dock icon with scalable SVG and 48–512 px rasters.
* **Simpler navigation.** The Data categories page is no longer shown in the sidebar.
* **Open source.** The proprietary end-user licence agreement, its first-run acceptance
  dialog and all proprietary licensing notices are gone. The complete source code, tests,
  packaging and build scripts are published.

## Upgrading from 1.1.0 or earlier

```bash
sudo apt install ./device-dock_1.2.0_amd64.deb
```

`apt` removes `apple-devices` automatically. Launch Device Dock once and it moves your data;
existing backups, settings and saved passwords keep working.

## Verification

* Backend test suite and headless UI smoke test pass (see `tests/`).
* The package installs and runs on Ubuntu 24.04 with no Python package installed, with
  encrypted-backup unlocking verified through the bundled engine.
* Physical-device operations (backup, restore, file transfer, factory reset) require a
  connected iPhone and were not part of automated verification.

## Known limitations

See [README.md](README.md#known-limitations). Restore and factory reset need an unlocked
device with Find My iPhone turned off.

## Licence

GPL-3.0-only. Third-party components keep their own licences; see
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

Device Dock is not affiliated with, endorsed by or sponsored by Apple Inc.
