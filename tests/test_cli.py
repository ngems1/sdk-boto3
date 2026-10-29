import json
from pathlib import Path

import pytest

from ebs_audit import cli
from ebs_audit.models import Account
from ebs_audit.plan import build_plan, write_plan
from ebs_audit.pricing import PriceBook

from .fakes import NOW, FakeEC2, FakeOrganizations, FakeProvider, make_volume


class FakeFactory(FakeProvider):
    """SessionFactory stand-in: a FakeProvider plus the extra hooks the CLI uses."""

    caller_account = "999999999999"

    def __init__(self, ec2: FakeEC2, org_accounts: list[dict] | None = None) -> None:  # type: ignore[type-arg]
        super().__init__({}, default=ec2)
        self.org = FakeOrganizations(org_accounts or [])

    def organizations_client(self) -> FakeOrganizations:
        return self.org


def _args(*argv: str):  # type: ignore[no-untyped-def]
    return cli.build_parser().parse_args(list(argv))


def test_parser_defaults() -> None:
    args = _args("scan")
    assert args.min_age_days == 30
    assert args.role_name == "EbsAuditRole"
    assert args.formats == ["csv", "json", "md"]
    assert args.regions == []
    args = _args("scan", "--accounts", "1, 2", "--regions", "us-east-1,eu-west-1", "--protect-tag", "Keep")
    assert args.accounts == ["1", "2"]
    assert args.regions == ["us-east-1", "eu-west-1"]
    assert cli._protect_tags(args) == ["DoNotDelete", "ebs-audit:keep", "Keep"]


def test_parser_rejects_negative_age() -> None:
    with pytest.raises(SystemExit):
        _args("scan", "--min-age-days", "-1")


def test_apply_dry_run_and_yes_are_exclusive() -> None:
    with pytest.raises(SystemExit):
        _args("apply-plan", "p.json", "--dry-run", "--yes")


def test_resolve_accounts_modes() -> None:
    factory = FakeFactory(
        FakeEC2(),
        [
            {"Id": "111111111111", "Name": "a", "Status": "ACTIVE"},
            {"Id": "222222222222", "Name": "b", "Status": "ACTIVE"},
        ],
    )
    assert [a.id for a in cli.resolve_accounts(_args("scan", "--no-org"), factory)] == ["999999999999"]
    assert [a.id for a in cli.resolve_accounts(_args("scan", "--accounts", "5,6"), factory)] == ["5", "6"]
    org = cli.resolve_accounts(_args("scan", "--exclude-accounts", "222222222222"), factory)
    assert org == [Account("111111111111", "a")]


def test_cmd_scan_writes_reports_and_plan(tmp_path: Path) -> None:
    ec2 = FakeEC2([make_volume("vol-1", size=100, vtype="gp2"), make_volume("vol-2", tags={"DoNotDelete": "1"})])
    args = _args(
        "scan", "--no-org", "--regions", "us-east-1", "--min-age-days", "0", "--output-dir", str(tmp_path), "--plan"
    )
    assert cli.cmd_scan(args, FakeFactory(ec2)) == cli.EXIT_OK
    names = sorted(p.name for p in tmp_path.iterdir())
    assert "deletion-plan.json" in names
    assert any(n.endswith(".csv") for n in names) and any(n.endswith(".md") for n in names)
    plan = json.loads((tmp_path / "deletion-plan.json").read_text())
    assert [e["volume_id"] for e in plan["entries"]] == ["vol-1"]


def test_cmd_scan_exit_codes(tmp_path: Path) -> None:
    failing = FakeEC2(fail_with="AccessDenied")
    args = _args("scan", "--no-org", "--regions", "us-east-1", "--output-dir", str(tmp_path))
    assert cli.cmd_scan(args, FakeFactory(failing)) == cli.EXIT_PARTIAL

    args = _args(
        "scan",
        "--no-org",
        "--regions",
        "us-east-1",
        "--min-age-days",
        "0",
        "--output-dir",
        str(tmp_path),
        "--fail-on-findings",
    )
    assert cli.cmd_scan(args, FakeFactory(FakeEC2([make_volume("vol-1")]))) == cli.EXIT_FINDINGS

    args = _args("scan", "--no-org", "--formats", "pdf", "--output-dir", str(tmp_path))
    assert cli.cmd_scan(args, FakeFactory(FakeEC2())) == cli.EXIT_FATAL


def _plan_file(tmp_path: Path, ec2: FakeEC2) -> Path:
    from ebs_audit.scanner import run_scan

    result = run_scan(
        FakeProvider({}, default=ec2),
        [Account("111111111111")],
        min_age_days=0,
        price_book=PriceBook(),
        regions=["us-east-1"],
    )
    path = tmp_path / "plan.json"
    write_plan(build_plan(result, price_book=PriceBook()), path)
    return path


def test_cmd_apply_dry_run_then_real(tmp_path: Path) -> None:
    ec2 = FakeEC2([make_volume("vol-1")])
    plan = _plan_file(tmp_path, ec2)
    factory = FakeFactory(ec2)

    args = _args("apply-plan", str(plan), "--dry-run", "--output-dir", str(tmp_path))
    assert cli.cmd_apply(args, lambda: factory) == cli.EXIT_OK
    assert "vol-1" in ec2.volumes

    args = _args("apply-plan", str(plan), "--yes", "--output-dir", str(tmp_path))
    assert cli.cmd_apply(args, lambda: factory) == cli.EXIT_OK
    assert ec2.deleted == ["vol-1"]
    outputs = sorted(p.name for p in tmp_path.glob("apply-*.json"))
    assert len(outputs) == 2 and any("dryrun" in n for n in outputs)


def test_cmd_apply_rejects_tampered_plan(tmp_path: Path) -> None:
    plan = _plan_file(tmp_path, FakeEC2([make_volume("vol-1")]))
    data = json.loads(plan.read_text())
    data["entries"][0]["volume_id"] = "vol-other"
    plan.write_text(json.dumps(data))
    args = _args("apply-plan", str(plan), "--yes")
    assert cli.cmd_apply(args, lambda: pytest.fail("must not create AWS clients")) == cli.EXIT_FATAL


def test_confirm_requires_typing_delete() -> None:
    plan = {
        "summary": {"volume_count": 2, "monthly_savings_usd": 15.0},
        "snapshot_before_delete": True,
        "entries": [{"account_id": "1"}, {"account_id": "2"}],
    }
    assert cli.confirm(plan, input_fn=lambda _: "delete") is True
    assert cli.confirm(plan, input_fn=lambda _: "yes") is False


def test_main_reports_aws_errors_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    from .fakes import FakeAWSError

    def boom(_args):  # type: ignore[no-untyped-def]
        raise FakeAWSError("ExpiredToken", "The security token included in the request is expired")

    monkeypatch.setattr(cli, "_factory", boom)
    assert cli.main(["scan", "--no-org"]) == cli.EXIT_FATAL


def test_now_fixture_is_timezone_aware() -> None:
    assert NOW.tzinfo is not None
