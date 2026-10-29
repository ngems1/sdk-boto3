import json
from datetime import timedelta

import pytest

from ebs_audit.errors import PlanError
from ebs_audit.models import Account, ScanResult
from ebs_audit.plan import (
    ACTION_DELETE,
    ACTION_SNAPSHOT_DELETE,
    TAG_PLAN_ID,
    apply_plan,
    build_plan,
    load_plan,
    summarize,
    write_plan,
)
from ebs_audit.pricing import PriceBook
from ebs_audit.scanner import scan_region

from .fakes import NOW, FakeEC2, FakeProvider, make_volume

ACCT = Account("111111111111", "prod")


def _scan(ec2: FakeEC2) -> ScanResult:
    vols = scan_region(
        ec2,
        account=ACCT,
        region="us-east-1",
        min_age_days=30,
        price_book=PriceBook(),
        protect_tags=["DoNotDelete"],
        now=NOW,
    )
    return ScanResult(generated_at=NOW, min_age_days=30, volumes=vols, accounts=[ACCT], regions_scanned=1)


def _ec2() -> FakeEC2:
    return FakeEC2(
        [
            make_volume("vol-1", size=100, vtype="gp2"),
            make_volume("vol-2", size=50, vtype="gp2"),
            make_volume("vol-keep", size=10, tags={"DoNotDelete": "true"}),
        ]
    )


def test_build_plan_excludes_protected_and_summarises() -> None:
    plan = build_plan(_scan(_ec2()), price_book=PriceBook(), now=NOW)
    assert {e["volume_id"] for e in plan["entries"]} == {"vol-1", "vol-2"}
    assert all(e["action"] == ACTION_SNAPSHOT_DELETE for e in plan["entries"])
    assert plan["summary"] == {
        "volume_count": 2,
        "monthly_savings_usd": 15.0,
        "snapshot_monthly_cost_usd_upper_bound": 7.5,
        "net_monthly_savings_usd": 7.5,
    }
    no_snap = build_plan(_scan(_ec2()), price_book=PriceBook(), snapshot_before_delete=False, now=NOW)
    assert all(e["action"] == ACTION_DELETE for e in no_snap["entries"])


def test_plan_round_trip_and_tamper_detection(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "plan.json"
    write_plan(build_plan(_scan(_ec2()), price_book=PriceBook(), now=NOW), path)
    assert load_plan(path, now=NOW)["summary"]["volume_count"] == 2

    tampered = json.loads(path.read_text())
    tampered["entries"].append({**tampered["entries"][0], "volume_id": "vol-not-reviewed"})
    path.write_text(json.dumps(tampered))
    with pytest.raises(PlanError, match="checksum"):
        load_plan(path, now=NOW)


def test_plan_expiry(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "plan.json"
    write_plan(build_plan(_scan(_ec2()), price_book=PriceBook(), expires_in_hours=1, now=NOW), path)
    with pytest.raises(PlanError, match="expired"):
        load_plan(path, now=NOW + timedelta(hours=2))


def test_load_plan_rejects_garbage(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "plan.json"
    path.write_text("{not json")
    with pytest.raises(PlanError, match="Cannot read"):
        load_plan(path)
    path.write_text(json.dumps({"plan_version": 99}))
    with pytest.raises(PlanError, match="plan_version"):
        load_plan(path)


def test_dry_run_changes_nothing() -> None:
    ec2 = _ec2()
    plan = build_plan(_scan(ec2), price_book=PriceBook(), now=NOW)
    results = apply_plan(plan, FakeProvider({}, default=ec2), dry_run=True)
    assert summarize(results) == {"dry-run-ok": 2}
    assert ec2.deleted == [] and ec2.snapshots == {}


def test_dry_run_reports_missing_permissions() -> None:
    ec2 = _ec2()
    plan = build_plan(_scan(ec2), price_book=PriceBook(), now=NOW)
    ec2.allow_delete = False
    assert summarize(apply_plan(plan, FakeProvider({}, default=ec2), dry_run=True)) == {"dry-run-denied": 2}


def test_apply_snapshots_then_deletes_and_is_idempotent() -> None:
    ec2 = _ec2()
    plan = build_plan(_scan(ec2), price_book=PriceBook(), now=NOW)
    provider = FakeProvider({}, default=ec2)

    first = apply_plan(plan, provider, dry_run=False)
    assert summarize(first) == {"deleted": 2}
    assert sorted(ec2.deleted) == ["vol-1", "vol-2"]
    assert len(ec2.snapshots) == 2
    assert all(s["Tags"][TAG_PLAN_ID] == plan["plan_id"] for s in ec2.snapshots.values())
    assert sorted(ec2.waited) == sorted(ec2.snapshots)  # waited before deleting
    assert "vol-keep" in ec2.volumes

    second = apply_plan(plan, provider, dry_run=False)
    assert summarize(second) == {"already-deleted": 2}
    assert len(ec2.snapshots) == 2  # no duplicate snapshots


def test_apply_reuses_snapshot_after_partial_failure() -> None:
    ec2 = _ec2()
    plan = build_plan(_scan(ec2), price_book=PriceBook(), now=NOW)
    provider = FakeProvider({}, default=ec2)

    original_delete = ec2.delete_volume
    calls = {"n": 0}

    def flaky_delete(VolumeId: str, DryRun: bool = False):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 1 and not DryRun:
            from .fakes import FakeAWSError

            raise FakeAWSError("RequestLimitExceeded")
        return original_delete(VolumeId=VolumeId, DryRun=DryRun)

    ec2.delete_volume = flaky_delete  # type: ignore[method-assign]
    first = apply_plan(plan, provider, dry_run=False)
    assert summarize(first) == {"deleted": 1, "failed": 1}
    assert len(ec2.snapshots) == 2

    second = apply_plan(plan, provider, dry_run=False)
    assert summarize(second) == {"already-deleted": 1, "deleted": 1}
    assert len(ec2.snapshots) == 2  # the earlier snapshot was re-used


def test_apply_skips_volumes_that_changed() -> None:
    ec2 = _ec2()
    plan = build_plan(_scan(ec2), price_book=PriceBook(), now=NOW)
    ec2.volumes["vol-1"]["State"] = "in-use"
    ec2.volumes["vol-1"]["Attachments"] = [{"InstanceId": "i-123"}]
    ec2.volumes["vol-2"]["Tags"] = [{"Key": "DoNotDelete", "Value": "true"}]
    results = {
        r.volume_id: r
        for r in apply_plan(plan, FakeProvider({}, default=ec2), dry_run=False, protect_tags=["DoNotDelete"])
    }
    assert results["vol-1"].status == "skipped" and "in-use" in results["vol-1"].message
    assert results["vol-2"].status == "skipped" and "protected" in results["vol-2"].message
    assert ec2.deleted == []


def test_apply_skips_resized_volume() -> None:
    ec2 = _ec2()
    plan = build_plan(_scan(ec2), price_book=PriceBook(), now=NOW)
    ec2.volumes["vol-1"]["Size"] = 500
    results = {r.volume_id: r.status for r in apply_plan(plan, FakeProvider({}, default=ec2), dry_run=False)}
    assert results == {"vol-1": "skipped", "vol-2": "deleted"}


def test_apply_without_snapshot_or_wait() -> None:
    ec2 = _ec2()
    plan = build_plan(_scan(ec2), price_book=PriceBook(), snapshot_before_delete=False, now=NOW)
    results = apply_plan(plan, FakeProvider({}, default=ec2), dry_run=False, wait_for_snapshots=False)
    assert summarize(results) == {"deleted": 2}
    assert ec2.snapshots == {} and ec2.waited == []
