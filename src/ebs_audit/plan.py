"""Deletion plans: build, verify and (optionally) apply.

A plan is a reviewable JSON file. Applying it is a separate, explicit step that

* re-checks every volume (still exists, still unattached, unchanged, not protected),
* optionally snapshots it first (re-using a snapshot from an earlier partial run),
* refuses plans that are expired or whose checksum does not match (tampering / hand edits).
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .errors import PlanError, error_code, error_message
from .models import ScanResult
from .pricing import PriceBook
from .scanner import ClientProvider, is_protected

log = logging.getLogger(__name__)

PLAN_VERSION = 1
TAG_SOURCE_VOLUME = "ebs-audit:source-volume"
TAG_PLAN_ID = "ebs-audit:plan-id"

ACTION_SNAPSHOT_DELETE = "snapshot-and-delete"
ACTION_DELETE = "delete"


@dataclass(frozen=True)
class ActionResult:
    account_id: str
    region: str
    volume_id: str
    status: str  # deleted | already-deleted | skipped | dry-run-ok | dry-run-denied | failed
    snapshot_id: str | None = None
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------- build


def _checksum(body: dict[str, Any]) -> str:
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_plan(
    result: ScanResult,
    *,
    price_book: PriceBook,
    snapshot_before_delete: bool = True,
    expires_in_hours: int = 72,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    action = ACTION_SNAPSHOT_DELETE if snapshot_before_delete else ACTION_DELETE
    entries: list[dict[str, Any]] = [
        {
            "account_id": v.account_id,
            "account_name": v.account_name,
            "region": v.region,
            "volume_id": v.volume_id,
            "name": v.name,
            "volume_type": v.volume_type,
            "size_gib": v.size_gib,
            "create_time": v.create_time.isoformat(),
            "action": action,
            "monthly_savings": v.monthly_cost,
            "snapshot_monthly_cost": price_book.snapshot_monthly_cost(v.size_gib) if snapshot_before_delete else 0.0,
        }
        for v in result.deletable_volumes
    ]
    savings = round(sum(e["monthly_savings"] for e in entries), 2)
    snapshot_cost = round(sum(e["snapshot_monthly_cost"] for e in entries), 2)
    body: dict[str, Any] = {
        "plan_version": PLAN_VERSION,
        "tool_version": __version__,
        "generated_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=expires_in_hours)).isoformat(),
        "min_age_days": result.min_age_days,
        "snapshot_before_delete": snapshot_before_delete,
        "summary": {
            "volume_count": len(entries),
            "monthly_savings_usd": savings,
            "snapshot_monthly_cost_usd_upper_bound": snapshot_cost,
            "net_monthly_savings_usd": round(savings - snapshot_cost, 2),
        },
        "entries": entries,
    }
    body["plan_id"] = _checksum(body)[:16]
    body["checksum"] = _checksum(body)
    return body


def write_plan(plan: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- load / verify


def load_plan(path: Path, *, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    try:
        plan: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlanError(f"Cannot read plan {path}: {exc}") from exc

    if plan.get("plan_version") != PLAN_VERSION:
        raise PlanError(f"Unsupported plan_version {plan.get('plan_version')!r} (expected {PLAN_VERSION})")

    body = {k: v for k, v in plan.items() if k != "checksum"}
    if plan.get("checksum") != _checksum(body):
        raise PlanError("Plan checksum mismatch: the file was modified after it was generated. Re-run `scan --plan`.")

    expires = datetime.fromisoformat(plan["expires_at"])
    if now > expires:
        raise PlanError(f"Plan expired at {expires.isoformat()}. Re-run `scan --plan` to get a fresh plan.")
    return plan


# --------------------------------------------------------------------------- apply


def apply_plan(
    plan: dict[str, Any],
    provider: ClientProvider,
    *,
    dry_run: bool = True,
    wait_for_snapshots: bool = True,
    protect_tags: Sequence[str] = (),
    waiter_delay: int = 15,
    waiter_max_attempts: int = 240,
) -> list[ActionResult]:
    results: list[ActionResult] = []
    for entry in plan["entries"]:
        try:
            res = _apply_entry(
                entry,
                provider,
                plan_id=plan["plan_id"],
                dry_run=dry_run,
                wait_for_snapshots=wait_for_snapshots,
                protect_tags=protect_tags,
                waiter_delay=waiter_delay,
                waiter_max_attempts=waiter_max_attempts,
            )
        except Exception as exc:  # noqa: BLE001 - one bad volume must not stop the run
            res = _result(entry, "failed", message=f"{error_code(exc)}: {error_message(exc)}")
        log.info("%s %s %s -> %s %s", res.account_id, res.region, res.volume_id, res.status, res.message)
        results.append(res)
    return results


def _result(entry: dict[str, Any], status: str, snapshot_id: str | None = None, message: str = "") -> ActionResult:
    return ActionResult(
        account_id=entry["account_id"],
        region=entry["region"],
        volume_id=entry["volume_id"],
        status=status,
        snapshot_id=snapshot_id,
        message=message,
    )


def _apply_entry(
    entry: dict[str, Any],
    provider: ClientProvider,
    *,
    plan_id: str,
    dry_run: bool,
    wait_for_snapshots: bool,
    protect_tags: Sequence[str],
    waiter_delay: int,
    waiter_max_attempts: int,
) -> ActionResult:
    ec2 = provider.client(entry["account_id"], "ec2", entry["region"])
    volume_id = entry["volume_id"]

    # 1. Re-verify the volume right before acting on it.
    try:
        vols = ec2.describe_volumes(VolumeIds=[volume_id]).get("Volumes", [])
    except Exception as exc:
        if error_code(exc) == "InvalidVolume.NotFound":
            return _result(entry, "already-deleted", message="volume no longer exists")
        raise
    if not vols:
        return _result(entry, "already-deleted", message="volume no longer exists")
    vol = vols[0]
    tags = {t["Key"]: t.get("Value", "") for t in vol.get("Tags", []) or []}

    if vol.get("State") != "available" or vol.get("Attachments"):
        return _result(entry, "skipped", message=f"volume is now {vol.get('State')} / attached")
    if is_protected(tags, protect_tags):
        return _result(entry, "skipped", message="volume is now protected by tag")
    if int(vol.get("Size", -1)) != int(entry["size_gib"]) or vol.get("VolumeType") != entry["volume_type"]:
        return _result(entry, "skipped", message="volume changed since the plan was generated")

    # 2. Dry run: ask EC2 whether we *would* be allowed to delete it.
    if dry_run:
        try:
            ec2.delete_volume(VolumeId=volume_id, DryRun=True)
        except Exception as exc:
            code = error_code(exc)
            if code == "DryRunOperation":
                return _result(entry, "dry-run-ok", message=f"would {entry['action']}")
            if code == "UnauthorizedOperation":
                return _result(entry, "dry-run-denied", message="missing ec2:DeleteVolume permission")
            raise
        return _result(entry, "dry-run-ok", message=f"would {entry['action']}")

    # 3. Snapshot (idempotent: re-use a snapshot from an earlier run of the same plan).
    snapshot_id: str | None = None
    if entry["action"] == ACTION_SNAPSHOT_DELETE:
        snapshot_id = _existing_snapshot(ec2, volume_id, plan_id)
        if snapshot_id:
            log.info("Re-using snapshot %s for %s", snapshot_id, volume_id)
        else:
            snap_tags = [
                {"Key": TAG_SOURCE_VOLUME, "Value": volume_id},
                {"Key": TAG_PLAN_ID, "Value": plan_id},
                {"Key": "Name", "Value": f"ebs-audit backup of {tags.get('Name') or volume_id}"},
            ]
            snap = ec2.create_snapshot(
                VolumeId=volume_id,
                Description=f"ebs-audit pre-delete backup of {volume_id} (plan {plan_id})",
                TagSpecifications=[{"ResourceType": "snapshot", "Tags": snap_tags}],
            )
            snapshot_id = snap["SnapshotId"]
        if wait_for_snapshots:
            ec2.get_waiter("snapshot_completed").wait(
                SnapshotIds=[snapshot_id],
                WaiterConfig={"Delay": waiter_delay, "MaxAttempts": waiter_max_attempts},
            )

    # 4. Delete.
    try:
        ec2.delete_volume(VolumeId=volume_id)
    except Exception as exc:
        if error_code(exc) == "InvalidVolume.NotFound":
            return _result(entry, "already-deleted", snapshot_id, "volume disappeared during apply")
        raise
    return _result(entry, "deleted", snapshot_id, "snapshot taken" if snapshot_id else "")


def _existing_snapshot(ec2: Any, volume_id: str, plan_id: str) -> str | None:
    resp = ec2.describe_snapshots(
        OwnerIds=["self"],
        Filters=[
            {"Name": f"tag:{TAG_SOURCE_VOLUME}", "Values": [volume_id]},
            {"Name": f"tag:{TAG_PLAN_ID}", "Values": [plan_id]},
        ],
    )
    for snap in resp.get("Snapshots", []):
        if snap.get("State") in ("pending", "completed"):
            return str(snap["SnapshotId"])
    return None


def summarize(results: Sequence[ActionResult]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    return dict(sorted(counts.items()))
