# Reproducible build environment for the Device Dock Debian package.
#
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
#
# The package bundles its own Python runtime, so it is built against the oldest supported
# distribution (Ubuntu 24.04, glibc 2.39) rather than the developer's host. Nuitka compiles
# the application to native code; the application's Python dependencies are
# installed from wheels here so the installed package needs no distribution Python packages.
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        binutils \
        ca-certificates \
        dpkg-dev \
        gcc \
        gir1.2-adw-1 \
        gir1.2-gtk-4.0 \
        gir1.2-secret-1 \
        libc6-dev \
        libpython3.12-dev \
        make \
        patchelf \
        python3 \
        python3-dev \
        python3-gi \
        python3-pip \
        python3-venv \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv --system-site-packages /opt/buildenv \
    && /opt/buildenv/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/buildenv/bin/pip install --no-cache-dir \
        "nuitka==4.3.5" \
        "iphone_backup_decrypt==0.11.2" \
        "cryptography==50.0.2" \
        "pycryptodome==4.2.0" \
        "qrcode==8.2" \
        "pillow==10.2.0"

ENV DEVICE_DOCK_BUILD_PYTHON=/opt/buildenv/bin/python
WORKDIR /src
