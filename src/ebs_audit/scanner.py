"""Find unattached EBS volumes across accounts and regions, in parallel."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Protocol

from .errors import error_code, error_message
from .models import Account, ScanError, ScanResult, Volume
from .pricing import PriceBook

log = logging.getLogger(__name__)


class ClientProvider(Protocol):
    """Anything that can hand out a boto3-style client for an account and region."""

    def client(self, account_id: str, service: str, region: str) -> Any: ...


def list_enabled_regions(ec2_client: Any) -> list[str]:
    """Regions enabled for the account (default regions plus opted-in ones)."""
    resp = ec2_client.describe_regions(
        AllRegions=False,
        Filters=[{"Name": "opt-in-status", "Values": ["opt-in-not-required", "opted-in"]}],
    )
    return sorted(r["RegionName"] for r in resp.get("Regions", []))


def is_protected(tags: dict[str, str], protect_tags: Iterable[str]) -> bool:
    """A volume is protected when it carries any protect tag key (value other than 'false')."""
    return any(key in tags and tags[key].strip().lower() not in ("false", "no", "0") for key in protect_tags)


def scan_region(
    ec2_client: Any,
    *,
    account: Account,
    region: str,
    min_age_days: int,
    price_book: PriceBook,
    protect_tags: Sequence[str] = (),
    now: datetime | None = None,
) -> list[Volume]:
    """Return unattached (status=available) volumes in one region older than ``min_age_days``."""
    now = now or datetime.now(timezone.utc)
    volumes: list[Volume] = []
    paginator = ec2_client.get_paginator("describe_volumes")
    for page in paginator.paginate(Filters=[{"Name": "status", "Values": ["available"]}]):
        for raw in page.get("Volumes", []):
            if raw.get("Attachments"):
                continue  # defensive: should never happen for status=available
            created: datetime = raw["CreateTime"]
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            age_days = (now - created).days
            if age_days < min_age_days:
                continue
            tags = {t["Key"]: t.get("Value", "") for t in raw.get("Tags", []) or []}
            vtype = raw.get("VolumeType", "standard")
            size = int(raw.get("Size", 0))
            iops = raw.get("Iops")
            throughput = raw.get("Throughput")
            volumes.append(
                Volume(
                    account_id=account.id,
                    account_name=account.name,
                    region=region,
                    volume_id=raw["VolumeId"],
                    volume_type=vtype,
                    size_gib=size,
                    iops=iops,
                    throughput=throughput,
                    availability_zone=raw.get("AvailabilityZone", ""),
                    create_time=created,
                    age_days=age_days,
                    encrypted=bool(raw.get("Encrypted", False)),
                    snapshot_id=raw.get("SnapshotId") or None,
                    tags=tags,
                    monthly_cost=price_book.monthly_cost(vtype, size, region, iops, throughput),
                    protected=is_protected(tags, protect_tags),
                )
            )
    return volumes


def run_scan(
    provider: ClientProvider,
    accounts: Sequence[Account],
    *,
    min_age_days: int,
    price_book: PriceBook,
    regions: Sequence[str] | None = None,
    home_region: str = "us-east-1",
    protect_tags: Sequence[str] = (),
    max_workers: int = 16,
    now: datetime | None = None,
) -> ScanResult:
    """Scan every (account, region) pair. Failures are recorded, never fatal."""
    now = now or datetime.now(timezone.utc)
    result = ScanResult(generated_at=now, min_age_days=min_age_days, accounts=list(accounts))

    # 1. Work out which regions to scan in each account.
    targets: list[tuple[Account, str]] = []
    for account in accounts:
        if regions:
            account_regions = list(regions)
        else:
            try:
                account_regions = list_enabled_regions(provider.client(account.id, "ec2", home_region))
            except Exception as exc:  # noqa: BLE001 - recorded and reported
                _record(result, account, None, "discover-regions", exc)
                continue
        targets.extend((account, r) for r in account_regions)

    result.regions_scanned = len(targets)
    log.info("Scanning %d account/region pair(s) across %d account(s)", len(targets), len(accounts))

    # 2. Fan out.
    def task(account: Account, region: str) -> list[Volume]:
        ec2 = provider.client(account.id, "ec2", region)
        return scan_region(
            ec2,
            account=account,
            region=region,
            min_age_days=min_age_days,
            price_book=price_book,
            protect_tags=protect_tags,
            now=now,
        )

    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
        futures = {pool.submit(task, a, r): (a, r) for a, r in targets}
        for future in as_completed(futures):
            account, region = futures[future]
            try:
                found = future.result()
            except Exception as exc:  # noqa: BLE001 - recorded and reported
                _record(result, account, region, "describe-volumes", exc)
                continue
            if found:
                log.info("%s %s: %d unattached volume(s)", account.label, region, len(found))
            result.volumes.extend(found)

    result.volumes.sort(key=lambda v: (-v.monthly_cost, v.account_id, v.region, v.volume_id))
    result.errors.sort(key=lambda e: (e.account_id, e.region or ""))
    return result


def _record(result: ScanResult, account: Account, region: str | None, stage: str, exc: BaseException) -> None:
    err = ScanError(
        account_id=account.id,
        region=region,
        stage=stage,
        code=error_code(exc),
        message=error_message(exc),
    )
    log.warning("%s %s %s failed: %s %s", account.label, region or "-", stage, err.code, err.message)
    result.errors.append(err)
