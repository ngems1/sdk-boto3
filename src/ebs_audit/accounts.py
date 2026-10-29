"""AWS Organizations account discovery."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from .models import Account

log = logging.getLogger(__name__)


def _is_active(raw: dict[str, Any]) -> bool:
    # Organizations is migrating from "Status" to "State"; accept either.
    state = raw.get("State") or raw.get("Status")
    return state == "ACTIVE"


def list_org_accounts(org_client: Any, ou_ids: Iterable[str] | None = None) -> list[Account]:
    """Return ACTIVE accounts in the organization, or only those under ``ou_ids`` (recursively).

    Must run with credentials of the management account or a delegated administrator.
    """
    raw_accounts: dict[str, dict[str, Any]] = {}
    ou_list = list(ou_ids or [])

    if not ou_list:
        for page in org_client.get_paginator("list_accounts").paginate():
            for acct in page.get("Accounts", []):
                raw_accounts[acct["Id"]] = acct
    else:
        for ou_id in ou_list:
            for acct in _accounts_under(org_client, ou_id):
                raw_accounts[acct["Id"]] = acct

    accounts = [Account(id=a["Id"], name=a.get("Name", "")) for a in raw_accounts.values() if _is_active(a)]
    skipped = len(raw_accounts) - len(accounts)
    if skipped:
        log.info("Skipping %d suspended / closed account(s)", skipped)
    return sorted(accounts, key=lambda a: a.id)


def _accounts_under(org_client: Any, parent_id: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    stack = [parent_id]
    seen: set[str] = set()
    while stack:
        parent = stack.pop()
        if parent in seen:
            continue
        seen.add(parent)
        for page in org_client.get_paginator("list_accounts_for_parent").paginate(ParentId=parent):
            found.extend(page.get("Accounts", []))
        for page in org_client.get_paginator("list_children").paginate(
            ParentId=parent, ChildType="ORGANIZATIONAL_UNIT"
        ):
            stack.extend(child["Id"] for child in page.get("Children", []))
    return found


def filter_accounts(
    accounts: Iterable[Account],
    include: Iterable[str] | None = None,
    exclude: Iterable[str] | None = None,
) -> list[Account]:
    """Keep only ``include`` ids (if given) and drop ``exclude`` ids."""
    include_set = set(include or [])
    exclude_set = set(exclude or [])
    return [a for a in accounts if (not include_set or a.id in include_set) and a.id not in exclude_set]
