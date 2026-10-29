"""Minimal in-memory stand-ins for boto3 clients (no AWS, no boto3 needed)."""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone
from typing import Any

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


class FakeAWSError(Exception):
    """Looks like botocore.exceptions.ClientError (has .response['Error'])."""

    def __init__(self, code: str, message: str = "boom") -> None:
        super().__init__(f"{code}: {message}")
        self.response = {"Error": {"Code": code, "Message": message}}


class FakePaginator:
    def __init__(self, pages_fn: Any) -> None:
        self._pages_fn = pages_fn

    def paginate(self, **kwargs: Any) -> list[dict[str, Any]]:
        return list(self._pages_fn(**kwargs))


class FakeWaiter:
    def __init__(self, ec2: FakeEC2) -> None:
        self.ec2 = ec2

    def wait(self, SnapshotIds: list[str], WaiterConfig: dict[str, Any] | None = None) -> None:
        self.ec2.waited.extend(SnapshotIds)
        for sid in SnapshotIds:
            self.ec2.snapshots[sid]["State"] = "completed"


def make_volume(
    volume_id: str,
    *,
    age_days: int = 90,
    size: int = 100,
    vtype: str = "gp3",
    iops: int | None = None,
    throughput: int | None = None,
    tags: dict[str, str] | None = None,
    state: str = "available",
    attachments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    vol: dict[str, Any] = {
        "VolumeId": volume_id,
        "Size": size,
        "VolumeType": vtype,
        "State": state,
        "AvailabilityZone": "us-east-1a",
        "CreateTime": NOW - timedelta(days=age_days),
        "Encrypted": True,
        "SnapshotId": "",
        "Attachments": attachments or [],
        "Tags": [{"Key": k, "Value": v} for k, v in (tags or {}).items()],
    }
    if iops is not None:
        vol["Iops"] = iops
    if throughput is not None:
        vol["Throughput"] = throughput
    return vol


class FakeEC2:
    _snap_ids = itertools.count(1)

    def __init__(
        self,
        volumes: list[dict[str, Any]] | None = None,
        regions: list[str] | None = None,
        fail_with: str | None = None,
        page_size: int = 2,
        allow_delete: bool = True,
    ) -> None:
        self.volumes = {v["VolumeId"]: v for v in (volumes or [])}
        self.regions = regions or ["us-east-1", "eu-west-1"]
        self.fail_with = fail_with
        self.page_size = page_size
        self.allow_delete = allow_delete
        self.snapshots: dict[str, dict[str, Any]] = {}
        self.deleted: list[str] = []
        self.waited: list[str] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []

    # -- discovery
    def describe_regions(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("describe_regions", kwargs))
        if self.fail_with:
            raise FakeAWSError(self.fail_with)
        return {"Regions": [{"RegionName": r} for r in self.regions]}

    def get_paginator(self, name: str) -> FakePaginator:
        assert name == "describe_volumes"

        def pages(**kwargs: Any) -> Any:
            self.calls.append(("describe_volumes", kwargs))
            if self.fail_with:
                raise FakeAWSError(self.fail_with)
            wanted = None
            for f in kwargs.get("Filters", []):
                if f["Name"] == "status":
                    wanted = set(f["Values"])
            vols = [v for v in self.volumes.values() if wanted is None or v["State"] in wanted]
            for i in range(0, max(len(vols), 1), self.page_size):
                yield {"Volumes": vols[i : i + self.page_size]}

        return FakePaginator(pages)

    def describe_volumes(self, VolumeIds: list[str]) -> dict[str, Any]:
        self.calls.append(("describe_volumes_by_id", {"VolumeIds": VolumeIds}))
        found = [self.volumes[v] for v in VolumeIds if v in self.volumes]
        if not found:
            raise FakeAWSError("InvalidVolume.NotFound", "The volume does not exist")
        return {"Volumes": found}

    # -- actions
    def delete_volume(self, VolumeId: str, DryRun: bool = False) -> dict[str, Any]:
        self.calls.append(("delete_volume", {"VolumeId": VolumeId, "DryRun": DryRun}))
        if DryRun:
            raise FakeAWSError("DryRunOperation" if self.allow_delete else "UnauthorizedOperation")
        if VolumeId not in self.volumes:
            raise FakeAWSError("InvalidVolume.NotFound")
        del self.volumes[VolumeId]
        self.deleted.append(VolumeId)
        return {}

    def create_snapshot(self, VolumeId: str, Description: str, TagSpecifications: list[Any]) -> dict[str, Any]:
        sid = f"snap-{next(self._snap_ids):04d}"
        tags = {t["Key"]: t["Value"] for t in TagSpecifications[0]["Tags"]}
        self.snapshots[sid] = {"SnapshotId": sid, "VolumeId": VolumeId, "State": "pending", "Tags": tags}
        self.calls.append(("create_snapshot", {"VolumeId": VolumeId}))
        return {"SnapshotId": sid, "State": "pending"}

    def describe_snapshots(self, OwnerIds: list[str], Filters: list[dict[str, Any]]) -> dict[str, Any]:
        def match(snap: dict[str, Any]) -> bool:
            for f in Filters:
                key = f["Name"].removeprefix("tag:")
                if snap["Tags"].get(key) not in f["Values"]:
                    return False
            return True

        return {"Snapshots": [s for s in self.snapshots.values() if match(s)]}

    def get_waiter(self, name: str) -> FakeWaiter:
        assert name == "snapshot_completed"
        return FakeWaiter(self)


class FakeOrganizations:
    def __init__(self, accounts: list[dict[str, Any]], tree: dict[str, dict[str, list[str]]] | None = None) -> None:
        self.accounts = accounts
        # tree: parent_id -> {"accounts": [ids], "ous": [ou ids]}
        self.tree = tree or {}

    def get_paginator(self, name: str) -> FakePaginator:
        by_id = {a["Id"]: a for a in self.accounts}
        if name == "list_accounts":
            return FakePaginator(lambda **_: [{"Accounts": self.accounts[:2]}, {"Accounts": self.accounts[2:]}])
        if name == "list_accounts_for_parent":
            return FakePaginator(
                lambda ParentId: [{"Accounts": [by_id[i] for i in self.tree.get(ParentId, {}).get("accounts", [])]}]
            )
        if name == "list_children":
            return FakePaginator(
                lambda ParentId, ChildType: [
                    {"Children": [{"Id": o, "Type": ChildType} for o in self.tree.get(ParentId, {}).get("ous", [])]}
                ]
            )
        raise AssertionError(name)


class FakeProvider:
    """ClientProvider backed by FakeEC2 instances keyed by (account, region)."""

    def __init__(self, ec2_by_target: dict[tuple[str, str], FakeEC2], default: FakeEC2 | None = None) -> None:
        self.ec2_by_target = ec2_by_target
        self.default = default or FakeEC2()
        self.requested: list[tuple[str, str, str]] = []

    def client(self, account_id: str, service: str, region: str) -> Any:
        self.requested.append((account_id, service, region))
        assert service == "ec2"
        return self.ec2_by_target.get((account_id, region), self.default)
