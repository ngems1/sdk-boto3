"""Write scan results as CSV, JSON and a Markdown summary."""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from collections.abc import Callable, Iterable
from pathlib import Path

from .models import ScanResult, Volume

CSV_FIELDS = [
    "account_id",
    "account_name",
    "region",
    "availability_zone",
    "volume_id",
    "name",
    "volume_type",
    "size_gib",
    "iops",
    "throughput",
    "encrypted",
    "create_time",
    "age_days",
    "snapshot_id",
    "protected",
    "monthly_cost",
    "annual_cost",
    "tags",
]

TOP_N = 25


def write_reports(result: ScanResult, output_dir: Path, formats: Iterable[str], stem: str) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    writers: dict[str, Callable[[ScanResult, Path], None]] = {
        "csv": write_csv,
        "json": write_json,
        "md": write_markdown,
    }
    paths: list[Path] = []
    for fmt in formats:
        path = output_dir / f"{stem}.{fmt}"
        writers[fmt](result, path)
        paths.append(path)
    return paths


def write_csv(result: ScanResult, path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for vol in result.volumes:
            row = vol.to_dict()
            row["tags"] = ";".join(f"{k}={v}" for k, v in sorted(vol.tags.items()))
            writer.writerow(row)


def write_json(result: ScanResult, path: Path) -> None:
    path.write_text(json.dumps(result.to_dict(), indent=2, sort_keys=False) + "\n", encoding="utf-8")


def _money(value: float) -> str:
    return f"${value:,.2f}"


def _group(volumes: Iterable[Volume], key: Callable[[Volume], str]) -> list[tuple[str, int, int, float]]:
    buckets: dict[str, list[Volume]] = defaultdict(list)
    for vol in volumes:
        buckets[key(vol)].append(vol)
    rows = [
        (name, len(vols), sum(v.size_gib for v in vols), round(sum(v.monthly_cost for v in vols), 2))
        for name, vols in buckets.items()
    ]
    return sorted(rows, key=lambda r: (-r[3], r[0]))


def _table(headers: list[str], rows: Iterable[Iterable[object]]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return lines


def render_markdown(result: ScanResult) -> str:
    vols = result.volumes
    monthly = result.total_monthly_cost
    lines: list[str] = [
        "# Unattached EBS volume report",
        "",
        f"Generated **{result.generated_at:%Y-%m-%d %H:%M UTC}**. "
        f"Volumes unattached and created more than **{result.min_age_days} days** ago.",
        "",
        "## Summary",
        "",
    ]
    lines += _table(
        ["Metric", "Value"],
        [
            ["Accounts scanned", len(result.accounts)],
            ["Account/region pairs scanned", result.regions_scanned],
            ["Unattached volumes", len(vols)],
            ["Total size", f"{result.total_size_gib:,} GiB"],
            ["Estimated monthly cost", _money(monthly)],
            ["Estimated annual cost", _money(monthly * 12)],
            ["Protected (excluded from plan)", len(vols) - len(result.deletable_volumes)],
            ["Potential monthly savings", _money(result.deletable_monthly_cost)],
            ["Scan errors", len(result.errors)],
        ],
    )

    if vols:
        for title, key in (
            ("By account", lambda v: f"{v.account_name or '-'} ({v.account_id})"),
            ("By region", lambda v: v.region),
            ("By volume type", lambda v: v.volume_type),
        ):
            lines += ["", f"## {title}", ""]
            lines += _table(
                ["Name", "Volumes", "Size (GiB)", "Monthly cost"],
                [[n, c, f"{s:,}", _money(m)] for n, c, s, m in _group(vols, key)],
            )

        lines += ["", f"## Top {min(TOP_N, len(vols))} volumes by cost", ""]
        lines += _table(
            ["Account", "Region", "Volume", "Name", "Type", "GiB", "Age (days)", "Monthly", "Protected"],
            [
                [
                    v.account_id,
                    v.region,
                    f"`{v.volume_id}`",
                    v.name or "-",
                    v.volume_type,
                    v.size_gib,
                    v.age_days,
                    _money(v.monthly_cost),
                    "yes" if v.protected else "",
                ]
                for v in vols[:TOP_N]
            ],
        )

    if result.errors:
        lines += ["", "## Errors", "", "These accounts or regions could not be fully scanned:", ""]
        lines += _table(
            ["Account", "Region", "Stage", "Code", "Message"],
            [[e.account_id, e.region or "-", e.stage, e.code, e.message.replace("|", "/")] for e in result.errors],
        )

    lines += [
        "",
        "---",
        "_Costs are estimates based on on-demand list prices (storage + provisioned IOPS/throughput). "
        "Age is measured from the volume's creation time._",
        "",
    ]
    return "\n".join(lines)


def write_markdown(result: ScanResult, path: Path) -> None:
    path.write_text(render_markdown(result), encoding="utf-8")
