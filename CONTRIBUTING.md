# Contributing to Device Dock

Thanks for helping improve Device Dock. Bug reports, fixes, documentation and translations
are all welcome.

## Before you start

* Read the [README](README.md) and the [third-party notices](THIRD_PARTY_NOTICES.md).
* Open an issue for anything larger than a small fix, so we can agree on the approach.
* Do not include real device identifiers, serial numbers, Apple IDs, backup passwords or
  personal data in issues, commits, test fixtures or screenshots.

## Development setup

Ubuntu 24.04 is the reference environment.

```bash
sudo apt install python3-venv python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 \
                 libimobiledevice-utils usbmuxd ifuse fuse3 \
                 gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-libav

python3 -m venv --system-site-packages .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m devicedock
```

`./run.sh` does the same and creates the virtual environment on first use.

## Checks

Run the checks that cover your change before opening a pull request:

```bash
.venv/bin/python -m pyflakes devicedock tests scripts
.venv/bin/python -m pytest tests -q
dbus-run-session -- xvfb-run -a .venv/bin/python tests/ui_smoke.py --out /tmp/ui
```

If you change packaging or metadata, build and verify the package too:

```bash
./packaging/build-deb.sh
./packaging/verify-package.sh dist/device-dock_*_amd64.deb
```

Physical-device behaviour (backup, restore, file transfer, factory reset) cannot be covered
by the automated suite; describe how you tested it in the pull request.

## Coding expectations

* Keep the existing structure: worker threads for long operations, GTK work on the main
  loop, cancellation through `Job` tokens.
* Never write passwords, device identifiers or personal data to logs or settings.
* Add tests for behaviour you introduce or fix. Keep them in the existing test files where
  possible.
* Match the surrounding style; there is no formatter to run.

## Licensing

Device Dock is licensed under GPL-3.0-only. By contributing you agree that your
contribution is licensed under the same terms. Only submit code you have the right to
license this way, and keep third-party copyright notices intact.

## Commit and pull request conventions

* One logical change per pull request.
* Explain the problem and the fix in the pull request description; include steps to
  reproduce for bug fixes.
* Update documentation and `THIRD_PARTY_NOTICES.md` if you add or change a dependency.
