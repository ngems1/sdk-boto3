"""End-to-end test against moto's in-memory AWS (runs in CI; skipped if moto is not installed)."""

from __future__ import annotations

import pytest

moto = pytest.importorskip("moto")
boto3 = pytest.importorskip("boto3")

from ebs_audit.aws import SessionFactory  # noqa: E402
from ebs_audit.models import Account  # noqa: E402
from ebs_audit.plan import apply_plan, build_plan, summarize  # noqa: E402
from ebs_audit.pricing import PriceBook  # noqa: E402
from ebs_audit.scanner import run_scan  # noqa: E402


@pytest.fixture(autouse=True)
def _aws_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "AWS_DEFAULT_REGION": "us-east-1",
    }.items():
        monkeypatch.setenv(key, value)


@moto.mock_aws
def test_scan_plan_apply_end_to_end() -> None:
    ec2 = boto3.client("ec2", region_name="us-east-1")
    instance = ec2.run_instances(ImageId="ami-12c6146b", MinCount=1, MaxCount=1)["Instances"][0]

    orphan = ec2.create_volume(AvailabilityZone="us-east-1a", Size=100, VolumeType="gp2")["VolumeId"]
    keep = ec2.create_volume(
        AvailabilityZone="us-east-1a",
        Size=10,
        VolumeType="gp3",
        TagSpecifications=[{"ResourceType": "volume", "Tags": [{"Key": "DoNotDelete", "Value": "true"}]}],
    )["VolumeId"]
    attached = ec2.create_volume(AvailabilityZone="us-east-1a", Size=5, VolumeType="gp2")["VolumeId"]
    ec2.attach_volume(VolumeId=attached, InstanceId=instance["InstanceId"], Device="/dev/sdf")

    factory = SessionFactory()
    account = Account(factory.caller_account, "moto")
    result = run_scan(
        factory,
        [account],
        min_age_days=0,
        price_book=PriceBook(),
        regions=["us-east-1"],
        protect_tags=["DoNotDelete"],
    )
    found = {v.volume_id: v for v in result.volumes}
    assert orphan in found and keep in found and attached not in found
    assert found[orphan].monthly_cost == 10.00
    assert found[keep].protected

    plan = build_plan(result, price_book=PriceBook())
    assert [e["volume_id"] for e in plan["entries"]] == [orphan]

    results = apply_plan(plan, factory, dry_run=False, waiter_delay=1, waiter_max_attempts=5)
    assert summarize(results) == {"deleted": 1}
    remaining = {v["VolumeId"] for v in ec2.describe_volumes()["Volumes"]}
    assert orphan not in remaining and keep in remaining

    snaps = ec2.describe_snapshots(OwnerIds=["self"])["Snapshots"]
    assert any(s.get("VolumeId") == orphan for s in snaps)

    # Second run is a no-op.
    assert summarize(apply_plan(plan, factory, dry_run=False)) == {"already-deleted": 1}


@moto.mock_aws
def test_org_discovery_and_assume_role() -> None:
    org = boto3.client("organizations", region_name="us-east-1")
    org.create_organization(FeatureSet="ALL")
    member = org.create_account(AccountName="member", Email="member@example.com")["CreateAccountStatus"]["AccountId"]

    from ebs_audit.accounts import list_org_accounts

    factory = SessionFactory()
    accounts = list_org_accounts(factory.organizations_client())
    ids = {a.id for a in accounts}
    assert member in ids and factory.caller_account in ids

    # Assuming into the member account works and yields a usable EC2 client.
    client = factory.client(member, "ec2", "eu-west-1")
    assert client.describe_volumes()["Volumes"] == []
    assert factory.role_arn(member).endswith(f"{member}:role/EbsAuditRole")
