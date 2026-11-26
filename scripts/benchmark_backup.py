# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
"""Device backup benchmark harness.

Measures a real backup end to end with instrumentation that does not depend on the
implementation under test:

* destination growth sampled from the filesystem (bytes, file count),
* CPU time and peak RSS of the ``idevicebackup2`` process read from ``/proc``,
* wall-clock stages derived from the first payload write,
* the progress events the application itself reports.

The same harness runs against the pre-optimisation and post-optimisation code, so the
comparison is like for like.

Usage::

    .venv/bin/python scripts/benchmark_backup.py \
        --destination "/path/to/dedicated/backup-dir" --mode full --label baseline

Nothing here writes to the device. The destination is created if missing and refused if it
already holds a backup of a different device.
"""

from __future__ import annotations

import argparse
import inspect
import json
import os
import plistlib
import resource
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gi  # noqa: E402

gi.require_version("GLib", "2.0")
from gi.repository import GLib  # noqa: E402

from devicedock.asyncutil import Job, OperationCancelled  # noqa: E402
from devicedock.devices import libimobile  # noqa: E402


@dataclass
class Sample:
    t: float
    bytes: int
    files: int
    rss_kb: int = 0
    cpu_s: float = 0.0


@dataclass
class BenchmarkResult:
    label: str
    mode: str
    udid: str
    destination: str
    started_at: str = ""
    wall_seconds: float = 0.0
    init_seconds: float = 0.0
    transfer_seconds: float = 0.0
    initial_bytes: int = 0
    initial_files: int = 0
    final_bytes: int = 0
    final_files: int = 0
    transferred_bytes: int = 0
    transferred_files: int = 0
    avg_mb_s: float = 0.0
    files_per_second: float = 0.0
    cpu_user_s: float = 0.0
    cpu_sys_s: float = 0.0
    peak_rss_mb: float = 0.0
    stall_events: int = 0
    max_stall_seconds: float = 0.0
    progress_event_count: int = 0
    cancel_latency_seconds: float | None = None
    status: str = ""
    error: str | None = None
    progress_events: list[dict] = field(default_factory=list)
    samples: list[dict] = field(default_factory=list)


def snapshot(root: Path) -> tuple[int, int]:
    """Iterative directory scan returning ``(bytes, files)``."""
    total = 0
    files = 0
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            files += 1
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except (FileNotFoundError, NotADirectoryError, PermissionError):
            continue
    return total, files


def find_tool_pid(name: str = "idevicebackup2") -> int | None:
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if cmdline and Path(cmdline[0].decode(errors="replace")).name == name:
            try:
                return int(entry.name)
            except ValueError:
                continue
    return None


def process_stats(pid: int) -> tuple[int, float]:
    """Return ``(peak_rss_kb, cpu_seconds)`` for one process, or zeros."""
    try:
        status = Path(f"/proc/{pid}/status").read_text()
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return 0, 0.0
    rss = 0
    for line in status.splitlines():
        if line.startswith("VmHWM:"):
            rss = int(line.split()[1])
            break
    fields = stat.rsplit(")", 1)[1].split()
    ticks = os.sysconf("SC_CLK_TCK")
    cpu = (int(fields[11]) + int(fields[12])) / ticks
    return rss, cpu


def read_snapshot_state(destination: Path) -> str | None:
    status = destination / "Status.plist"
    if not status.exists():
        return None
    try:
        with status.open("rb") as stream:
            value = plistlib.load(stream)
        return value.get("SnapshotState") if isinstance(value, dict) else None
    except (OSError, plistlib.InvalidFileException, ValueError):
        return "unreadable"


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark one real device backup")
    parser.add_argument("--destination", required=True, type=Path)
    parser.add_argument("--mode", choices=("full", "auto"), default="auto",
                        help="full = pass --full; auto = incremental when a snapshot exists")
    parser.add_argument("--label", default="run")
    parser.add_argument("--udid")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--sample-interval", type=float, default=2.0)
    parser.add_argument("--stall-timeout", type=float, default=1800.0,
                        help="abort if no payload byte changes for this many seconds")
    parser.add_argument("--cancel-after", type=float, default=None, metavar="SECONDS",
                        help="Measure a bounded window: cancel the backup after this many "
                             "seconds and record how long cancellation takes to take effect.")
    parser.add_argument("--scope", action="append", metavar="DOMAIN[:PATH]",
                        help="restrict the backup to these domains/paths (repeatable). "
                             "Used to benchmark a repeatable, bounded set of files instead "
                             "of a multi-hour full backup.")
    args = parser.parse_args()

    udid = args.udid or (libimobile.list_device_ids() or [None])[0]
    if not udid:
        print("No device detected; connect and unlock the iPhone first.", file=sys.stderr)
        return 2

    destination = args.destination.expanduser()
    if destination.exists():
        info = destination / "Info.plist"
        if info.exists():
            try:
                with info.open("rb") as stream:
                    target = plistlib.load(stream).get("Target Identifier")
            except (OSError, plistlib.InvalidFileException, ValueError):
                target = None
            if target and target != udid:
                print(f"{destination} holds a backup of {target}, not {udid}; refusing.",
                      file=sys.stderr)
                return 2
    destination.mkdir(parents=True, exist_ok=True)

    initial_bytes, initial_files = snapshot(destination)
    previous_state = read_snapshot_state(destination)
    print(f"Destination        : {destination}")
    print(f"Existing content   : {initial_files} files, {initial_bytes / 1e6:.1f} MB "
          f"(SnapshotState={previous_state!r})")

    result = BenchmarkResult(label=args.label, mode=args.mode, udid=udid,
                             destination=str(destination),
                             initial_bytes=initial_bytes, initial_files=initial_files,
                             started_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    samples: list[Sample] = []
    stop = threading.Event()
    start = time.monotonic()
    rusage_before = resource.getrusage(resource.RUSAGE_CHILDREN)
    tool_pid: list[int] = []

    def sampler() -> None:
        while not stop.wait(args.sample_interval):
            now = time.monotonic() - start
            size, count = snapshot(destination)
            if not tool_pid:
                pid = find_tool_pid()
                if pid:
                    tool_pid.append(pid)
            rss, cpu = process_stats(tool_pid[0]) if tool_pid else (0, 0.0)
            samples.append(Sample(t=round(now, 2), bytes=size, files=count,
                                  rss_kb=rss, cpu_s=round(cpu, 2)))

    job = Job("benchmark-backup")
    events: list[dict] = []

    def on_progress(fraction: float, text: str) -> bool:
        events.append({"t": round(time.monotonic() - start, 2),
                       "fraction": round(fraction, 4), "text": text[:160]})
        return False

    job.set_progress_handler(on_progress)
    sampler_thread = threading.Thread(target=sampler, name="bench-sampler", daemon=True)
    sampler_thread.start()

    outcome: dict[str, object] = {}
    extra: dict[str, object] = {}
    signature = inspect.signature(libimobile.create_backup).parameters
    if "stall_timeout" in signature:
        extra["stall_timeout"] = args.stall_timeout
    if args.scope and "filter_entries" in signature:
        entries = []
        for scope in args.scope:
            domain, _, path = scope.partition(":")
            entries.append({"Domain": domain, "Path": path, "Included": True})
        extra["filter_entries"] = entries
        print(f"Scope              : {args.scope}")

    def work() -> None:
        try:
            outcome["value"] = libimobile.create_backup(
                udid, destination, job, full=(args.mode == "full"), **extra
            )
        except BaseException as exc:  # noqa: BLE001 - reported in the result
            outcome["error"] = exc

    worker = threading.Thread(target=work, name="bench-backup", daemon=True)
    worker.start()
    # Progress callbacks are dispatched through GLib, exactly as in the application, so
    # the main loop has to run here too.
    context = GLib.MainContext.default()
    cancel_at: float | None = None
    try:
        while worker.is_alive():
            while context.pending():
                context.iteration(False)
            if (args.cancel_after is not None and cancel_at is None
                    and time.monotonic() - start >= args.cancel_after):
                cancel_at = time.monotonic()
                print(f"Cancelling after {args.cancel_after:.0f} s…", file=sys.stderr)
                job.cancel()
            time.sleep(0.05)
        while context.pending():
            context.iteration(False)
    except KeyboardInterrupt:
        print("Interrupt received; cancelling backup…", file=sys.stderr)
        job.cancel()
        worker.join(timeout=60)
    finally:
        stop.set()
        sampler_thread.join(timeout=5)

    wall = time.monotonic() - start
    if cancel_at is not None:
        result.cancel_latency_seconds = round(time.monotonic() - cancel_at, 2)
    rusage_after = resource.getrusage(resource.RUSAGE_CHILDREN)
    final_bytes, final_files = snapshot(destination)

    result.wall_seconds = round(wall, 2)
    result.final_bytes = final_bytes
    result.final_files = final_files
    result.transferred_bytes = max(0, final_bytes - initial_bytes)
    result.transferred_files = max(0, final_files - initial_files)
    result.cpu_user_s = round(rusage_after.ru_utime - rusage_before.ru_utime, 2)
    result.cpu_sys_s = round(rusage_after.ru_stime - rusage_before.ru_stime, 2)
    result.progress_events = events
    result.progress_event_count = len(events)

    payload_start = next((s.t for s in samples if s.bytes > initial_bytes), None)
    result.init_seconds = round(payload_start, 2) if payload_start else 0.0
    result.transfer_seconds = round(wall - result.init_seconds, 2)
    if result.transfer_seconds > 0:
        result.avg_mb_s = round(result.transferred_bytes / result.transfer_seconds / 1e6, 3)
        result.files_per_second = round(result.transferred_files / result.transfer_seconds, 1)
    result.peak_rss_mb = round(max((s.rss_kb for s in samples), default=0) / 1024, 1)

    gaps: list[float] = []
    last_change = 0.0
    last_bytes = initial_bytes
    for sample in samples:
        if sample.bytes != last_bytes:
            last_bytes = sample.bytes
            last_change = sample.t
        else:
            gaps.append(round(sample.t - last_change, 1))
    result.stall_events = sum(1 for gap in gaps if gap >= 30)
    result.max_stall_seconds = max(gaps, default=0.0)
    result.samples = [asdict(s) for s in samples]

    error = outcome.get("error")
    if worker.is_alive() or cancel_at is not None:
        result.status = "cancelled"
        if isinstance(error, BaseException) and not isinstance(error, OperationCancelled):
            result.status = "error"
            result.error = f"{type(error).__name__}: {error}"
    elif isinstance(error, BaseException):
        result.status = "error"
        result.error = f"{type(error).__name__}: {error}"
    else:
        result.status = "completed"

    print()
    print(f"--- {result.label} ({result.mode}) ---")
    print(f"status             : {result.status} {result.error or ''}")
    print(f"wall               : {result.wall_seconds:.1f} s")
    print(f"payload start      : {result.init_seconds:.1f} s")
    print(f"transfer window    : {result.transfer_seconds:.1f} s")
    print(f"transferred        : {result.transferred_bytes / 1e6:.1f} MB "
          f"({result.transferred_files} files)")
    print(f"throughput         : {result.avg_mb_s:.2f} MB/s, "
          f"{result.files_per_second:.0f} files/s")
    print(f"cpu (user/sys)     : {result.cpu_user_s:.1f} / {result.cpu_sys_s:.1f} s")
    print(f"peak rss (tool)    : {result.peak_rss_mb:.1f} MB")
    print(f"progress events    : {result.progress_event_count}")
    print(f"max stall          : {result.max_stall_seconds:.0f} s "
          f"({result.stall_events} gap(s) >= 30 s)")
    if result.cancel_latency_seconds is not None:
        print(f"cancel latency     : {result.cancel_latency_seconds:.2f} s "
              "from request to the run returning")

    output = args.output or Path(f"benchmark-{result.label}.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(asdict(result), indent=2), encoding="utf-8")
    print(f"json               : {output}")
    return 0 if result.status == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())
