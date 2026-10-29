import csv
import json

from ebs_audit.models import Account, ScanError, ScanResult
from ebs_audit.pricing import PriceBook
from ebs_audit.report import render_markdown, write_reports
from ebs_audit.scanner import scan_region

from .fakes import NOW, FakeEC2, make_volume


def _result() -> ScanResult:
    acct = Account("111111111111", "prod")
    ec2 = FakeEC2(
        [
            make_volume("vol-1", size=100, vtype="gp2", tags={"Name": "old-db", "team": "data"}),
            make_volume("vol-2", size=500, vtype="st1"),
            make_volume("vol-3", size=20, vtype="gp3", tags={"DoNotDelete": "yes"}),
        ]
    )
    vols = scan_region(
        ec2,
        account=acct,
        region="us-east-1",
        min_age_days=30,
        price_book=PriceBook(),
        protect_tags=["DoNotDelete"],
        now=NOW,
    )
    return ScanResult(
        generated_at=NOW,
        min_age_days=30,
        volumes=sorted(vols, key=lambda v: -v.monthly_cost),
        errors=[ScanError("222222222222", "eu-west-1", "describe-volumes", "AccessDenied", "nope | denied")],
        accounts=[acct, Account("222222222222")],
        regions_scanned=2,
    )


def test_write_all_formats(tmp_path) -> None:  # type: ignore[no-untyped-def]
    paths = write_reports(_result(), tmp_path, ["csv", "json", "md"], "report")
    assert [p.name for p in paths] == ["report.csv", "report.json", "report.md"]

    rows = list(csv.DictReader((tmp_path / "report.csv").open()))
    assert len(rows) == 3
    first = next(r for r in rows if r["volume_id"] == "vol-1")
    assert first["name"] == "old-db"
    assert first["tags"] == "Name=old-db;team=data"
    assert first["monthly_cost"] == "10.0"

    data = json.loads((tmp_path / "report.json").read_text())
    assert data["summary"]["volume_count"] == 3
    assert data["summary"]["error_count"] == 1
    assert data["volumes"][0]["create_time"].startswith("2026-07")


def test_markdown_contains_totals_breakdowns_and_errors() -> None:
    md = render_markdown(_result())
    assert "# Unattached EBS volume report" in md
    assert "| Unattached volumes | 3 |" in md
    assert "| Estimated monthly cost | $34.10 |" in md  # 10 + 22.5 + 1.6
    assert "| Potential monthly savings | $32.50 |" in md
    assert "## By volume type" in md
    assert "`vol-2`" in md
    assert "nope / denied" in md  # pipes escaped so the table does not break


def test_markdown_empty_result() -> None:
    md = render_markdown(ScanResult(generated_at=NOW, min_age_days=7))
    assert "| Unattached volumes | 0 |" in md
    assert "## By account" not in md
