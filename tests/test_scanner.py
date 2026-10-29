from ebs_audit.models import Account
from ebs_audit.pricing import PriceBook
from ebs_audit.scanner import is_protected, list_enabled_regions, run_scan, scan_region

from .fakes import NOW, FakeEC2, FakeProvider, make_volume

ACCT = Account("111111111111", "prod")


def test_scan_region_filters_by_age_and_prices_volumes() -> None:
    ec2 = FakeEC2(
        [
            make_volume("vol-old", age_days=90, size=100, vtype="gp2"),
            make_volume("vol-new", age_days=5),
            make_volume("vol-edge", age_days=30, size=10, vtype="gp3"),
            make_volume("vol-used", state="in-use"),
        ]
    )
    vols = scan_region(ec2, account=ACCT, region="us-east-1", min_age_days=30, price_book=PriceBook(), now=NOW)
    by_id = {v.volume_id: v for v in vols}
    assert set(by_id) == {"vol-old", "vol-edge"}
    assert by_id["vol-old"].monthly_cost == 10.00
    assert by_id["vol-old"].age_days == 90
    assert by_id["vol-edge"].monthly_cost == 0.80
    # server-side filter on status=available was requested
    assert ec2.calls[0][1]["Filters"] == [{"Name": "status", "Values": ["available"]}]


def test_scan_region_marks_protected_volumes() -> None:
    ec2 = FakeEC2(
        [
            make_volume("vol-keep", tags={"DoNotDelete": "true", "Name": "db-restore"}),
            make_volume("vol-explicit-false", tags={"DoNotDelete": "false"}),
            make_volume("vol-plain"),
        ]
    )
    vols = scan_region(
        ec2,
        account=ACCT,
        region="us-east-1",
        min_age_days=0,
        price_book=PriceBook(),
        protect_tags=["DoNotDelete"],
        now=NOW,
    )
    protected = {v.volume_id: v.protected for v in vols}
    assert protected == {"vol-keep": True, "vol-explicit-false": False, "vol-plain": False}
    assert next(v for v in vols if v.volume_id == "vol-keep").name == "db-restore"


def test_is_protected() -> None:
    assert is_protected({"ebs-audit:keep": ""}, ["ebs-audit:keep"])
    assert not is_protected({"ebs-audit:keep": "no"}, ["ebs-audit:keep"])
    assert not is_protected({"Other": "x"}, ["ebs-audit:keep"])


def test_list_enabled_regions_requests_opt_in_filter() -> None:
    ec2 = FakeEC2(regions=["us-west-2", "eu-west-1"])
    assert list_enabled_regions(ec2) == ["eu-west-1", "us-west-2"]
    assert ec2.calls[0][1]["AllRegions"] is False


def test_run_scan_multi_account_multi_region_with_errors() -> None:
    a1, a2, a3 = Account("111111111111", "prod"), Account("222222222222", "dev"), Account("333333333333", "locked")
    provider = FakeProvider(
        {
            (a1.id, "us-east-1"): FakeEC2(
                [make_volume("vol-a", size=200, vtype="gp2")], regions=["us-east-1", "eu-west-1"]
            ),
            (a1.id, "eu-west-1"): FakeEC2([make_volume("vol-b", size=50, vtype="gp2")]),
            (a2.id, "us-east-1"): FakeEC2(
                [make_volume("vol-c", size=10, vtype="gp2")], regions=["us-east-1", "ap-south-1"]
            ),
            (a2.id, "ap-south-1"): FakeEC2(fail_with="UnauthorizedOperation"),
            (a3.id, "us-east-1"): FakeEC2(fail_with="AccessDenied"),
        }
    )
    result = run_scan(provider, [a1, a2, a3], min_age_days=30, price_book=PriceBook(), max_workers=4, now=NOW)

    # sorted by cost, descending
    assert [v.volume_id for v in result.volumes] == ["vol-a", "vol-b", "vol-c"]
    assert result.total_monthly_cost == 26.00
    assert result.regions_scanned == 4  # a3 never got past region discovery
    errors = {(e.account_id, e.region, e.stage, e.code) for e in result.errors}
    assert errors == {
        ("222222222222", "ap-south-1", "describe-volumes", "UnauthorizedOperation"),
        ("333333333333", None, "discover-regions", "AccessDenied"),
    }


def test_run_scan_with_explicit_regions_skips_discovery() -> None:
    ec2 = FakeEC2([make_volume("vol-x")])
    provider = FakeProvider({}, default=ec2)
    result = run_scan(provider, [ACCT], min_age_days=0, price_book=PriceBook(), regions=["us-east-1"], now=NOW)
    assert [v.volume_id for v in result.volumes] == ["vol-x"]
    assert not any(c[0] == "describe_regions" for c in ec2.calls)


def test_scan_result_summary() -> None:
    ec2 = FakeEC2(
        [
            make_volume("vol-1", size=100, vtype="gp2"),
            make_volume("vol-2", size=50, vtype="gp2", tags={"DoNotDelete": "1"}),
        ]
    )
    result = run_scan(
        FakeProvider({}, default=ec2),
        [ACCT],
        min_age_days=0,
        price_book=PriceBook(),
        regions=["us-east-1"],
        protect_tags=["DoNotDelete"],
        now=NOW,
    )
    summary = result.to_dict()["summary"]
    assert summary["volume_count"] == 2
    assert summary["protected_count"] == 1
    assert summary["total_monthly_cost_usd"] == 15.00
    assert summary["deletable_monthly_cost_usd"] == 10.00
    assert summary["total_annual_cost_usd"] == 180.00
