# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
"""Human-readable exports: category tables and a full backup inventory report."""

from __future__ import annotations

import csv
import html
import json
from datetime import datetime
from pathlib import Path

from ..models import BackupInfo, human_size
from .categories import CategoryResult
from .manifest import ManifestQuery

#: Shared stylesheet for the generated HTML documents.
_STYLE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{title}</title>
<style>
 body {{ font-family: system-ui, sans-serif; margin: 2rem; color: #1a1a1a; }}
 table {{ border-collapse: collapse; width: 100%; font-size: 0.9rem; margin-bottom: 2rem; }}
 th, td {{ border-bottom: 1px solid #ddd; padding: 0.4rem 0.6rem; text-align: left; vertical-align: top; }}
 th {{ background: #f4f4f4; position: sticky; top: 0; }}
 tr:nth-child(even) td {{ background: #fafafa; }}
 .note {{ color: #555; font-size: 0.85rem; }}
 h1 {{ font-size: 1.4rem; }} h2 {{ font-size: 1.1rem; margin-top: 1.5rem; }}
</style></head>
<body>
"""


def export_category(result: CategoryResult, destination: Path) -> Path:
    """Write a category table as CSV, JSON or HTML depending on the file suffix."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    suffix = destination.suffix.lower()
    if suffix == ".json":
        destination.write_text(
            json.dumps(
                {
                    "category": result.title,
                    "available": result.available,
                    "note": result.note,
                    "error": result.error,
                    "columns": result.columns,
                    "rows": result.rows,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    elif suffix in (".html", ".htm"):
        destination.write_text(_category_html(result), encoding="utf-8")
    else:
        with open(destination, "w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(result.columns)
            writer.writerows(result.rows)
    return destination


def _category_html(result: CategoryResult) -> str:
    head = "".join(f"<th>{html.escape(column)}</th>" for column in result.columns)
    body = "".join(
        "<tr>" + "".join(f"<td>{html.escape(cell)}</td>" for cell in row) + "</tr>"
        for row in result.rows
    )
    note = f"<p class='note'>{html.escape(result.note)}</p>" if result.note else ""
    return _STYLE.format(title=html.escape(result.title)) + f"""<h1>{html.escape(result.title)}</h1>
<p class="note">Generated {datetime.now():%Y-%m-%d %H:%M} · {len(result.rows)} of {result.total} row(s)</p>
{note}
<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>
</body></html>
"""


def write_inventory_report(info: BackupInfo, manifest: ManifestQuery, destination: Path) -> Path:
    """Write an HTML report of the backup: device facts, domains and largest files."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    domains = sorted(manifest.domains_with_sizes(), key=lambda d: d.total_size, reverse=True)
    total_size = sum(d.total_size for d in domains)

    domain_rows = "".join(
        f"<tr><td>{html.escape(d.domain)}</td><td>{d.file_count}</td>"
        f"<td>{html.escape(human_size(d.total_size))}</td></tr>"
        for d in domains[:200]
    )
    facts = [
        ("Device", info.display_name),
        ("Model", f"{info.model_name} ({info.product_type or 'unknown'})"),
        ("Firmware", info.firmware or "unknown"),
        ("Last backup", info.last_backup_text),
        ("Encrypted", "Yes" if info.is_encrypted else "No"),
        ("Full backup", "Yes" if info.is_full_backup else "No"),
        ("Backup location", str(info.path)),
        ("Manifest entries", str(manifest.total_files())),
        ("Domains", str(len(domains))),
        ("Total payload size", human_size(total_size)),
    ]
    fact_rows = "".join(
        f"<tr><th>{html.escape(name)}</th><td>{html.escape(value)}</td></tr>" for name, value in facts
    )
    document = _STYLE.format(title=f"Backup report — {html.escape(info.display_name)}") + f"""<h1>Backup report: {html.escape(info.display_name)}</h1>
<p>Generated {datetime.now():%Y-%m-%d %H:%M}</p>
<h2>Device and backup facts</h2>
<table>{fact_rows}</table>
<h2>Domains (largest first)</h2>
<table><thead><tr><th>Domain</th><th>Files</th><th>Size</th></tr></thead>
<tbody>{domain_rows}</tbody></table>
</body></html>
"""
    destination.write_text(document, encoding="utf-8")
    return destination
