"""Command-line interface.

ebs-audit scan --min-age-days 30 --output-dir reports --plan
ebs-audit apply-plan reports/deletion-plan.json --dry-run
ebs-audit apply-plan reports/deletion-plan.json --yes
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .accounts import filter_accounts, list_org_accounts
from .errors import PlanError, error_code, error_message
from .models import Account
from .plan import apply_plan, build_plan, load_plan, summarize, write_plan
from .pricing import PriceBook, apply_live_prices
from .report import write_reports
from .scanner import run_scan

log = logging.getLogger("ebs_audit")

EXIT_OK = 0
EXIT_FATAL = 1
EXIT_PARTIAL = 2
EXIT_FINDINGS = 3

FORMATS = ("csv", "json", "md")


# --------------------------------------------------------------------------- argument parsing


def _csv_list(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ebs-audit",
        description="Find unattached EBS volumes across an AWS Organization, estimate their cost "
        "and produce an optional, reviewable deletion plan.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    parser.add_argument("--log-format", default="text", choices=["text", "json"])
    parser.add_argument("--profile", help="AWS named profile for the base credentials")

    access = argparse.ArgumentParser(add_help=False)
    access.add_argument(
        "--role-name",
        default="EbsAuditRole",
        help="Role assumed in each member account (default: %(default)s)",
    )
    access.add_argument("--external-id", help="ExternalId required by the member role trust policy")
    access.add_argument("--session-name", default="ebs-audit", help="STS role session name (shows in CloudTrail)")
    access.add_argument(
        "--assume-in-caller-account",
        action="store_true",
        help="Also assume --role-name in the account running the tool",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", parents=[access], help="Discover unattached volumes and write reports")
    scan.add_argument(
        "--min-age-days",
        type=_positive_int,
        default=30,
        help="Only report volumes created at least N days ago (default: %(default)s)",
    )
    scope = scan.add_argument_group("scope")
    scope.add_argument(
        "--no-org", action="store_true", help="Scan only the current account (no AWS Organizations calls)"
    )
    scope.add_argument(
        "--accounts", type=_csv_list, default=[], help="Comma-separated account IDs to scan (skips org listing)"
    )
    scope.add_argument("--exclude-accounts", type=_csv_list, default=[], help="Account IDs to skip")
    scope.add_argument("--ou-ids", type=_csv_list, default=[], help="Only accounts under these OUs (recursive)")
    scope.add_argument(
        "--regions",
        type=_csv_list,
        default=[],
        help="Comma-separated regions (default: every enabled region per account)",
    )
    scope.add_argument(
        "--home-region",
        default="us-east-1",
        help="Region used for region discovery (use us-gov-west-1 / cn-north-1 in other partitions)",
    )
    scan.add_argument(
        "--protect-tag",
        action="append",
        default=[],
        help="Tag key that protects a volume from the deletion plan (repeatable). "
        "Default: DoNotDelete and ebs-audit:keep",
    )
    pricing = scan.add_argument_group("pricing")
    pricing.add_argument("--price-file", type=Path, help="JSON price overrides (see examples/prices.json)")
    pricing.add_argument(
        "--live-pricing", action="store_true", help="Fetch per-region $/GB-month from the AWS Price List API"
    )
    output = scan.add_argument_group("output")
    output.add_argument("--output-dir", type=Path, default=Path("reports"))
    output.add_argument("--formats", type=_csv_list, default=list(FORMATS), help="csv,json,md (default: all)")
    output.add_argument("--plan", action="store_true", help="Also write deletion-plan.json")
    output.add_argument("--no-snapshot", action="store_true", help="Plan deletes without a backup snapshot")
    output.add_argument("--plan-expiry-hours", type=_positive_int, default=72)
    output.add_argument(
        "--fail-on-findings",
        action="store_true",
        help="Exit 3 when deletable volumes are found (useful in CI/compliance jobs)",
    )
    scan.add_argument("--max-workers", type=_positive_int, default=16, help="Parallel account/region scans")

    apply = sub.add_parser("apply-plan", parents=[access], help="Execute a reviewed deletion plan")
    apply.add_argument("plan_file", type=Path)
    mode = apply.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Check permissions only (EC2 DryRun); change nothing")
    mode.add_argument("--yes", action="store_true", help="Do not prompt for confirmation")
    apply.add_argument("--no-wait", action="store_true", help="Do not wait for snapshots to complete before deleting")
    apply.add_argument("--protect-tag", action="append", default=[])
    apply.add_argument("--output-dir", type=Path, default=Path("reports"))
    return parser


# --------------------------------------------------------------------------- logging


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def configure_logging(level: str, fmt: str) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        _JsonFormatter() if fmt == "json" else logging.Formatter("%(asctime)s %(levelname)-7s %(message)s")
    )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for noisy in ("botocore", "boto3", "urllib3"):
        logging.getLogger(noisy).setLevel(max(logging.WARNING, root.level))


# --------------------------------------------------------------------------- commands

DEFAULT_PROTECT_TAGS = ["DoNotDelete", "ebs-audit:keep"]


def _protect_tags(args: argparse.Namespace) -> list[str]:
    return list(dict.fromkeys(DEFAULT_PROTECT_TAGS + list(args.protect_tag)))


def _factory(args: argparse.Namespace) -> Any:
    import boto3  # imported lazily so the rest of the CLI works (and tests run) without boto3

    from .aws import SessionFactory

    return SessionFactory(
        boto3.Session(profile_name=args.profile) if args.profile else None,
        role_name=args.role_name,
        external_id=args.external_id,
        session_name=args.session_name,
        assume_in_caller_account=args.assume_in_caller_account,
    )


def resolve_accounts(args: argparse.Namespace, factory: Any) -> list[Account]:
    if args.no_org:
        accounts = [Account(id=factory.caller_account, name="current")]
    elif args.accounts:
        accounts = [Account(id=a) for a in args.accounts]
    else:
        accounts = list_org_accounts(factory.organizations_client(), args.ou_ids)
    return filter_accounts(accounts, exclude=args.exclude_accounts)


def cmd_scan(args: argparse.Namespace, factory: Any) -> int:
    bad = [f for f in args.formats if f not in FORMATS]
    if bad:
        log.error("Unknown format(s) %s; choose from %s", bad, ",".join(FORMATS))
        return EXIT_FATAL

    price_book = PriceBook.from_file(args.price_file) if args.price_file else PriceBook()
    accounts = resolve_accounts(args, factory)
    if not accounts:
        log.error("No accounts to scan")
        return EXIT_FATAL

    result = run_scan(
        factory,
        accounts,
        min_age_days=args.min_age_days,
        price_book=price_book,
        regions=args.regions or None,
        home_region=args.home_region,
        protect_tags=_protect_tags(args),
        max_workers=args.max_workers,
    )

    if args.live_pricing:
        apply_live_prices(price_book, factory.pricing_client(), {v.region for v in result.volumes})
        # Re-price with live data (volumes are immutable, so rebuild them).
        result.volumes = sorted(
            (
                replace(
                    v, monthly_cost=price_book.monthly_cost(v.volume_type, v.size_gib, v.region, v.iops, v.throughput)
                )
                for v in result.volumes
            ),
            key=lambda v: (-v.monthly_cost, v.account_id, v.region, v.volume_id),
        )

    stem = f"ebs-unattached-{result.generated_at:%Y%m%dT%H%M%SZ}"
    for path in write_reports(result, args.output_dir, args.formats, stem):
        log.info("Wrote %s", path)

    if args.plan:
        plan = build_plan(
            result,
            price_book=price_book,
            snapshot_before_delete=not args.no_snapshot,
            expires_in_hours=args.plan_expiry_hours,
        )
        plan_path = args.output_dir / "deletion-plan.json"
        write_plan(plan, plan_path)
        log.info(
            "Wrote %s: %d volume(s), est. $%.2f/month savings. Review it, then run: ebs-audit apply-plan %s --dry-run",
            plan_path,
            plan["summary"]["volume_count"],
            plan["summary"]["monthly_savings_usd"],
            plan_path,
        )

    log.info(
        "Found %d unattached volume(s), %d GiB, est. $%.2f/month across %d account(s); %d error(s)",
        len(result.volumes),
        result.total_size_gib,
        result.total_monthly_cost,
        len(accounts),
        len(result.errors),
    )
    if result.errors:
        return EXIT_PARTIAL
    if args.fail_on_findings and result.deletable_volumes:
        return EXIT_FINDINGS
    return EXIT_OK


def confirm(plan: dict[str, Any], input_fn: Callable[[str], str] = input) -> bool:
    summary = plan["summary"]
    accounts = sorted({e["account_id"] for e in plan["entries"]})
    print(
        f"\nAbout to {'snapshot and ' if plan['snapshot_before_delete'] else ''}DELETE "
        f"{summary['volume_count']} EBS volume(s) in {len(accounts)} account(s) "
        f"(est. ${summary['monthly_savings_usd']:,.2f}/month).",
        file=sys.stderr,
    )
    if not sys.stdin.isatty() and input_fn is input:
        print("Refusing to prompt on a non-interactive terminal; pass --yes or --dry-run.", file=sys.stderr)
        return False
    answer = input_fn("Type 'delete' to continue: ")
    return answer.strip().lower() == "delete"


def cmd_apply(args: argparse.Namespace, factory_fn: Callable[[], Any]) -> int:
    try:
        plan = load_plan(args.plan_file)
    except PlanError as exc:
        log.error("%s", exc)
        return EXIT_FATAL

    if not plan["entries"]:
        log.info("Plan contains no volumes; nothing to do")
        return EXIT_OK
    if not args.dry_run and not args.yes and not confirm(plan):
        log.warning("Aborted by user")
        return EXIT_FATAL

    results = apply_plan(
        plan,
        factory_fn(),
        dry_run=args.dry_run,
        wait_for_snapshots=not args.no_wait,
        protect_tags=_protect_tags(args),
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = args.output_dir / f"apply-{'dryrun-' if args.dry_run else ''}{stamp}.json"
    counts = summarize(results)
    out.write_text(
        json.dumps(
            {
                "plan_id": plan["plan_id"],
                "dry_run": args.dry_run,
                "summary": counts,
                "results": [r.to_dict() for r in results],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    log.info("Results: %s (details in %s)", counts, out)
    return EXIT_PARTIAL if counts.get("failed") or counts.get("dry-run-denied") else EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(args.log_level, args.log_format)
    try:
        if args.command == "scan":
            return cmd_scan(args, _factory(args))
        return cmd_apply(args, lambda: _factory(args))
    except KeyboardInterrupt:
        log.error("Interrupted")
        return 130
    except Exception as exc:
        code = error_code(exc)
        if hasattr(exc, "response"):
            log.error("AWS error %s: %s", code, error_message(exc))
        else:
            log.exception("Unexpected error")
        return EXIT_FATAL
