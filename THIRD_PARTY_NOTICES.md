# THIRD-PARTY NOTICES

**Device Dock** — Copyright © 2026 Device Dock Contributors.
Free software under the GNU General Public License, version 3 only (GPL-3.0-only);
see `LICENSE`.

This file is part of the distribution and must not be removed. It records every
third-party component the application uses, the licence that governs it, and the
attribution those licences require.

**This application is free software under GPL-3.0-only.** The notices below are
reproduced to satisfy the obligations of the third-party licences listed, and nothing in
them reduces the rights the GPL grants you over Device Dock itself.

---

## 1. How the application uses third-party code

Two categories matter legally, and they are treated differently:

| Category | Components | How it is used | Effect on this application |
|---|---|---|---|
| **Dynamically used / separate process** | GTK 4, libadwaita, PyGObject, pycairo, libimobiledevice, libusbmuxd, libplist, libsecret, usbmuxd | Imported at run time from the distribution's own packages, or executed as separate programs via `subprocess` | No source code is copied into this application, and no system library is bundled in the distribution. LGPL components are dynamically used, which the LGPL permits for any application when the replacement rights are preserved; GPL components are separate programs communicating through their documented interfaces. |
| **Python packages (permissive)** | `iphone_backup_decrypt`, `pycryptodome`, `cryptography`, `cffi`, `pycparser`, `qrcode` | Installed with `pip` for a source checkout, or taken from the distribution's packages; imported at run time | Permissive licences (MIT / BSD / Apache-2.0) are compatible with GPL-3.0; attribution obligations are met by this file. No third-party source is copied into this application's own modules. |
| **Bundled, compiled in** | `iphone_backup_decrypt` (MIT) | The `.deb` compiles the unmodified PyPI release into the standalone runtime, because the library is not packaged by Debian or Ubuntu and encrypted backups cannot be opened without it | MIT is GPL-3.0-compatible; the copyright and permission notice is reproduced in §4.1, installed as `/usr/share/doc/device-dock/bundled-licenses/iphone_backup_decrypt.txt`, and recorded in the machine-readable `copyright` file |
| **Bundled runtime** | CPython 3.12 (PSF-2.0), PyGObject (LGPL-2.1-or-later), `cryptography`, `pycryptodome`, `qrcode`, Pillow | Compiled into the self-contained Nuitka build under `/usr/lib/device-dock/app/` so the application does not use the host Python | Permitted: the permissive Python libraries require only notice retention (done here and in `/usr/share/doc/device-dock/bundled-licenses/`), and the LGPL PyGObject is dynamically loaded from the bundle with its licence text and the CPython/GLib sources publicly available for relinking. GTK, libadwaita and the other system libraries are still supplied by the distribution, not bundled |

No third-party licence below grants rights to this application itself; Device Dock is
licensed directly under GPL-3.0-only.

---

## 2. Runtime dependencies — system libraries

All of these are installed by the user's Linux distribution or by the user, and are **not
redistributed** with this application. Versions shown are those verified on the reference
system (Ubuntu 24.04); the application works with any compatible newer version. PyGObject
is the one exception: the `.deb` bundles its Python half so the application is independent
of the host Python, while the GTK/libadwaita libraries and typelibs it binds remain system
packages.

| Component | Verified version | Licence | Source |
|---|---|---|---|
| GTK 4 (`libgtk-4-1`, `gir1.2-gtk-4.0`) | 4.14.5 | LGPL-2.1-or-later | GNOME Project — https://gitlab.gnome.org/GNOME/gtk |
| libadwaita (`libadwaita-1-0`, `gir1.2-adw-1`) | 1.5.0 | LGPL-2.1-or-later | GNOME Project — https://gitlab.gnome.org/GNOME/libadwaita |
| PyGObject (`python3-gi`) — bundled Python side, system GTK/libadwaita | 3.48.2 | LGPL-2.1-or-later | GNOME Project — https://gitlab.gnome.org/GNOME/pygobject |
| pycairo (`python3-cairo`) | 1.25.1 | LGPL-2.1-only or MPL-1.1 | https://gitlab.gnome.org/GNOME/pycairo |
| libimobiledevice (`libimobiledevice6`) | 1.3.0 | LGPL-2.1-or-later | https://libimobiledevice.org |
| libimobiledevice utilities (`libimobiledevice-utils`: `idevice_id`, `ideviceinfo`, `idevicebackup2`, `idevicepair`) | 1.3.0 | LGPL-2.1-or-later | https://libimobiledevice.org |
| ifuse | distro version | LGPL-2.1-or-later | https://github.com/libimobiledevice/ifuse |
| libusbmuxd (`libusbmuxd6`) | 2.0.2 | LGPL-2.1-or-later | https://libimobiledevice.org |
| libplist (`libplist-2.0-4`) | 2.3.0 | LGPL-2.1-or-later | https://libimobiledevice.org |
| libsecret (`libsecret-1-0`, `gir1.2-secret-1`) — optional | 0.21.4 | LGPL-2.1-or-later | GNOME Project — https://gitlab.gnome.org/GNOME/libsecret |
| usbmuxd (daemon) | 1.1.1 | **GPL-2.0-or-later** | https://libimobiledevice.org |
| FUSE 3 (`fuse3`, `libfuse3-3`) | 3.14.0 | LGPL-2.1-only | https://github.com/libfuse/libfuse |
| GStreamer (`gstreamer1.0-plugins-base`, `-good`, `-libav`) | 1.24.2 / 1.24.1 | LGPL-2.0-or-later (plugins), LGPL-2.1-or-later (libav plugin) | https://gstreamer.freedesktop.org |
| gdk-pixbuf loaders (`libgdk-pixbuf-2.0-0`, `heif-gdk-pixbuf` — optional) | 2.42.10 / 1.17.6 | LGPL-2.1-or-later / LGPL-3.0-or-later (libheif) | https://gitlab.gnome.org/GNOME/gdk-pixbuf, https://github.com/strukturag/libheif |

### Programs executed as separate processes (optional preview tools)

These are never linked into the application. It runs them with a fixed argument vector and
reads their output, which keeps their licences at arm's length; they are not redistributed.

| Program | Package | Licence | Used for |
|---|---|---|---|
| `pdftoppm` | `poppler-utils` | **GPL-2.0-or-later** (Poppler) | first page of a PDF as a preview image |
| `ffmpeg` | `ffmpeg` | LGPL-2.1-or-later or GPL-2.0-or-later depending on the distribution build | one video frame as a thumbnail |
| `ffprobe` | `ffmpeg` | as above | duration, title, artist and album of a media file |

### Notes on the LGPL components

The LGPL-2.1-or-later components above are used through their public interfaces as
shared system libraries (via `gobject-introspection`), as GStreamer plugins loaded by the
GTK media backend, or as standalone command-line programs. Use is therefore dynamic and
compatible with the GPL-3.0-only licensing of the application itself.

`usbmuxd` is the only GPL component in the mandatory list. It is a **separate daemon** the
application reaches through its documented socket, exactly as every libimobiledevice tool
does; it is not linked, bundled or modified. `pdftoppm` (Poppler, GPL-2.0-or-later) and
`ffmpeg` are likewise separate programs invoked by path, and are optional.

You retain the LGPL right to modify any of those libraries and continue using the
application with the modified version: they are installed and replaced by your
distribution, not vendored or bundled by this application, and nothing in this product
prevents their replacement or reverse engineering for debugging such modifications.

If you redistribute this application, you must **not** bundle these libraries into the
package; keep them as external dependencies so those rights remain effective.

### Notes on usbmuxd (GPL-2.0-or-later)

`usbmuxd` is a separate system daemon. This application never links against it, never
includes its code, and communicates with it only indirectly through libimobiledevice's
documented socket protocol. Invoking a GPL program as a separate process does not make
this application a derivative work of it.

`usbmuxd` is installed and managed by your distribution. **Do not ship `usbmuxd` inside a
distribution package of this application** — doing so would subject that bundled copy to
GPL-2.0-or-later source and notice obligations that the distribution already satisfies.
Declare it as an external runtime dependency instead.

### Note on libimobiledevice licensing detail

In the upstream libimobiledevice source tree the library and the command-line tools are
LGPL-2.1-or-later. Certain build-time helper macros (`m4/ax_python_devel.m4`,
`m4/ax_pthread.m4`) and Debian packaging files carry GPL-3.0-or-later. Those files are
not part of the installed binaries used by this application and are not redistributed
with it.

---

## 3. Runtime dependencies — Python packages

For a source checkout these are installed by the user (or by `run.sh` /
`packaging/install.sh`) from the Python Package Index into a virtual environment, or
supplied by the distribution as `python3-*` packages. The `.deb` bundles them inside the
standalone runtime (§1, bundled-runtime rows); no Python package is an external dependency
and no Python package has to be installed by the user.

| Component | Verified version | Licence | Source | Required by |
|---|---|---|---|---|
| `iphone_backup_decrypt` | 0.11.2 | MIT | https://github.com/jsharkey13/iphone_backup_decrypt — © 2019–2026 James Sharkey | direct |
| `pycryptodome` | 4.2.0 | BSD-2-Clause and public domain | https://www.pycryptodome.org — © Helder Eijs and contributors | `iphone_backup_decrypt` |
| `cryptography` | 50.0.2 | Apache-2.0 OR BSD-3-Clause | https://github.com/pyca/cryptography — © Python Cryptographic Authority | direct |
| `cffi` | 2.1.1 | MIT-0 (MIT No Attribution) | https://github.com/python-cffi/cffi — © Armin Rigo, Maciej Fijałkowski | `cryptography` |
| `pycparser` | 3.12 | BSD-3-Clause | https://github.com/eliben/pycparser — © 2008–2027 Eli Bendersky | `cffi` |

### Runtime dependency — `qrcode`

Required: the Support Development dialog always draws both donation addresses as scannable
QR codes. It is declared in `requirements.txt` and installed by `packaging/install.sh`.

| Component | Verified version | Licence | Source |
|---|---|---|---|
| `qrcode` | 8.2 | BSD (project metadata also declares "Other/Proprietary License" for bundled data) | https://github.com/lincolnloop/python-qrcode — © Lincoln Loop and contributors |

Obligation if you ship it: retain the package's `LICENSE` file and this attribution. No
source code from `qrcode` is copied into this application's modules.

Standard-library modules (`sqlite3`, `plistlib`, `hashlib`, `subprocess`, `tempfile`,
`pathlib`, `csv`, `json`, `html`, `logging`, `threading`) are covered by the PSF Python
licence of the interpreter in use and are not redistributed here.

### Development-only dependencies (not distributed)

Used to run the test suite and lint; they are not part of the application and are not
included in any distribution package.

| Component | Verified version | Licence | Source |
|---|---|---|---|
| `pytest` | 9.2.0 | MIT | https://github.com/pytest-dev/pytest — © 2004 Holger Krekel and others |
| `pyflakes` | 4.1.0 | MIT | https://github.com/PyCQA/pyflakes — © PyCQA contributors |
| `pluggy` | 1.7.0 | MIT | https://github.com/pytest-dev/pluggy — © pytest-dev contributors |
| `iniconfig` | 2.3.1 | MIT | https://github.com/pytest-dev/iniconfig — © pytest-dev contributors |
| `packaging` | 24.0 | Apache-2.0 or BSD-2-Clause | https://github.com/pypa/packaging — © PyPA contributors |

---

## 4. Required notices

### 4.1 MIT — `iphone_backup_decrypt`

```
The MIT License

Copyright (c) 2019-2026 James Sharkey

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

### 4.2 BSD-2-Clause and public domain — `pycryptodome`

```
The source code in PyCryptodome is partially in the public domain
and partially released under the BSD 2-Clause license.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

  1. Redistributions of source code must retain the above copyright notice,
     this list of conditions and the following disclaimer.
  2. Redistributions in binary form must reproduce the above copyright notice,
     this list of conditions and the following disclaimer in the documentation
     and/or other materials provided with the distribution.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```

### 4.3 Apache-2.0 OR BSD-3-Clause — `cryptography`

Dual-licensed by the Python Cryptographic Authority. This application relies on the
Apache-2.0 option. The full texts are available at:

* Apache License 2.0 — https://www.apache.org/licenses/LICENSE-2.0
* BSD 3-Clause — https://github.com/pyca/cryptography/blob/main/LICENSE.BSD
* Additional notice — https://github.com/pyca/cryptography/blob/main/LICENSE

Apache-2.0 obligations that apply if you redistribute the package: retain this notice,
include a copy of the licence, and retain any `NOTICE` file shipped with it. Patent and
trademark terms of Apache-2.0 remain in force.

### 4.4 BSD-3-Clause — `pycparser`

```
pycparser -- A C parser in Python

Copyright (c) 2008-2027, Eli Bendersky
All rights reserved.

Redistribution and use in source and binary forms, with or without modification,
are permitted provided that the following conditions are met:

  * Redistributions of source code must retain the above copyright notice, this
    list of conditions and the following disclaimer.
  * Redistributions in binary form must reproduce the above copyright notice,
    this list of conditions and the following disclaimer in the documentation
    and/or other materials provided with the distribution.
  * Neither the name of the copyright holder nor the names of its contributors
    may be used to endorse or promote products derived from this software
    without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR
ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
(INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON
ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
(INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```

### 4.5 MIT-0 — `cffi`

```
MIT No Attribution

Copyright (c) Armin Rigo, Maciej Fijałkowski and contributors

Permission is hereby granted, free of charge, to any person obtaining a copy of
this software and associated documentation files (the "Software"), to deal in
the Software without restriction, including without limitation the rights to
use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies
of the Software, and to permit persons to whom the Software is furnished to do
so.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

### 4.6 LGPL-2.1-or-later — GTK 4, libadwaita, PyGObject, libimobiledevice, libusbmuxd, libplist, libsecret, pycairo

These libraries are licensed under the GNU Lesser General Public License, version 2.1 or
later, © their respective contributors (GNOME Project; libimobiledevice contributors,
© 2008–2019). The licence text is installed on your system at
`/usr/share/common-licenses/LGPL-2.1` and published at
https://www.gnu.org/licenses/old-licenses/lgpl-2.1.html.

Statement of use: these libraries are used unmodified through their public APIs as shared
libraries or separate executables. This application does not include their source code,
does not statically link them, and does not restrict your ability to replace them. If you
redistribute this application, keep them as external dependencies.

### 4.7 GPL-2.0-or-later — `usbmuxd` (external daemon only)

`usbmuxd` is © libimobiledevice contributors and is licensed under the GNU General Public
License, version 2 or later. It is installed and run by your operating system as an
independent service. This application does not link to it, does not include its code, and
does not distribute it. The licence text is at `/usr/share/common-licenses/GPL-2` and
https://www.gnu.org/licenses/old-licenses/gpl-2.0.html.

If you redistribute `usbmuxd` (for example inside an appliance image), you must comply with
GPL-2.0-or-later independently, including providing the corresponding source.

### 4.8 Bundled runtime — CPython 3.12, PyGObject, `cryptography`, `pycryptodome`, `qrcode`, Pillow

The Debian package is a Nuitka standalone build. `/usr/lib/device-dock/app/` therefore
contains a CPython 3.12 runtime (© Python Software Foundation, PSF-2.0), PyGObject
(© GNOME Project, LGPL-2.1-or-later), `cryptography` (© The Python Cryptographic Authority
and contributors, Apache-2.0 OR BSD-3-Clause), `pycryptodome` (© Helder Eijs and
contributors, BSD-2-Clause and public domain), `iphone_backup_decrypt` (© 2019–2026 James
Sharkey, MIT), `qrcode` (© Lincoln Loop, BSD-3-Clause) and Pillow (© Secret Labs AB and
contributors, MIT-CMU/HPND).

The full notice texts are installed as `/usr/share/doc/device-dock/bundled-licenses/`:
`python3.12.txt`, `python3-gi.txt`, `cryptography.txt`, `pycryptodome.txt`,
`iphone_backup_decrypt.txt`, `qrcode.txt` and `pillow.txt`. Nuitka (Apache-2.0) is used only as a build tool and is not distributed.
No GTK, libadwaita, libimobiledevice, ifuse or `usbmuxd` code is bundled.

---

## 5. Licence-compatibility review

Every dependency is compatible with distributing Device Dock under GPL-3.0-only. The GPL
`usbmuxd` component is a system daemon communicating over its documented socket boundary;
the LGPL libraries are dynamically loaded and replaceable; the Python libraries are
permissively licensed and GPL-compatible.

| Question | Assessment |
|---|---|
| Is any dependency incompatible with GPL-3.0? | **No.** `usbmuxd` (GPL-2.0-or-later) is a separate daemon reached through its socket protocol; libimobiledevice and ifuse are LGPL-2.1-or-later; the Python libraries are MIT, BSD-2/3-Clause, MIT-0 and Apache-2.0, all of which may be combined with GPL-3.0 code. |
| Are the LGPL conditions met? | **Yes.** LGPL components are used unmodified as shared libraries or separate programs, attribution is given here, and the user can replace them; the package does not bundle GTK, libadwaita, libimobiledevice, ifuse or `usbmuxd`. |
| Is any third-party source copied into the application's modules? | **No.** Modules under `devicedock/` are original work. Third-party code is either imported from installed packages or, in the `.deb`, compiled unchanged into the standalone runtime next to the application; the MIT notice for `iphone_backup_decrypt` is retained here and in the installed licence texts. |
| What must a redistribution include? | The GPL-3.0 text (`LICENSE`), this file, the machine-readable `copyright`, and the licence texts of the bundled Python runtime and libraries in `bundled-licenses/`. Anyone distributing a modified binary must also make the corresponding source available, as GPL-3.0 requires. |
| Where is the corresponding source? | <https://github.com/bitsbreaches/device-dock>, at the tag matching the release. `packaging/build-deb.sh` builds the published package from that tree. |

**Recommended:** keep `usbmuxd`, GTK, libadwaita, libimobiledevice and ifuse as declared
*external* dependencies in any future `.deb`/Flatpak/AppImage, so that the LGPL replacement
rights and the GPL boundary above stay intact. The bundled PyGObject remains dynamically
loaded from `/usr/lib/device-dock/app/gi/`, and its LGPL-2.1 text and the CPython/GLib
sources remain publicly available, which preserves the relinking rights.

---

Copyright © 2026 Device Dock Contributors.

Device Dock is free software under the GNU General Public License, version 3 only
(GPL-3.0-only). See `LICENSE` for the full text.
