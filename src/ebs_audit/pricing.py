"""EBS cost estimation.

Default prices are AWS on-demand list prices for us-east-1 (USD). Regional prices
differ by roughly 0-30 %, so for accurate numbers either:

* pass ``--live-pricing`` to fetch per-region $/GB-month from the AWS Price List API, or
* pass ``--price-file`` with your own (e.g. negotiated / EDP) prices.
"""

from __future__ import annotations

import copy
import json
import logging
from collections.abc import Iterable
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# us-east-1 on-demand list prices (USD). Verify against https://aws.amazon.com/ebs/pricing/
DEFAULT_PRICES: dict[str, dict[str, Any]] = {
    "gp2": {"gb_month": 0.10},
    "gp3": {
        "gb_month": 0.08,
        "iops_month": 0.005,
        "free_iops": 3000,
        "throughput_month": 0.04,  # per MiB/s-month
        "free_throughput": 125,
    },
    "io1": {"gb_month": 0.125, "iops_month": 0.065},
    "io2": {
        "gb_month": 0.125,
        # [upper bound of tier (inclusive) or null, price per IOPS-month]
        "iops_tiers": [[32000, 0.065], [64000, 0.0455], [None, 0.032]],
    },
    "st1": {"gb_month": 0.045},
    "sc1": {"gb_month": 0.015},
    "standard": {"gb_month": 0.05},
}
DEFAULT_SNAPSHOT_GB_MONTH = 0.05
FALLBACK_TYPE = "gp2"


class PriceBook:
    """Look up EBS prices per region and volume type, with overrides."""

    def __init__(
        self,
        default: dict[str, dict[str, Any]] | None = None,
        regions: dict[str, dict[str, dict[str, Any]]] | None = None,
        snapshot_gb_month: float = DEFAULT_SNAPSHOT_GB_MONTH,
    ) -> None:
        self.default = copy.deepcopy(default if default is not None else DEFAULT_PRICES)
        self.regions = copy.deepcopy(regions or {})
        self.snapshot_gb_month = snapshot_gb_month
        self._warned: set[str] = set()

    @classmethod
    def from_file(cls, path: str | Path) -> PriceBook:
        """Load overrides from JSON: ``{"default": {...}, "regions": {...}, "snapshot_gb_month": 0.05}``.

        Values in the file are merged over the built-in defaults.
        """
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        book = cls()
        for vtype, prices in data.get("default", {}).items():
            book.default.setdefault(vtype, {}).update(prices)
        for region, types in data.get("regions", {}).items():
            for vtype, prices in types.items():
                book.set_region_price(region, vtype, **prices)
        if "snapshot_gb_month" in data:
            book.snapshot_gb_month = float(data["snapshot_gb_month"])
        return book

    def set_region_price(self, region: str, volume_type: str, **prices: Any) -> None:
        self.regions.setdefault(region, {}).setdefault(volume_type, {}).update(prices)

    def prices_for(self, volume_type: str, region: str) -> dict[str, Any]:
        base = self.default.get(volume_type)
        if base is None:
            if volume_type not in self._warned:
                log.warning("No price for volume type %r; using %s prices", volume_type, FALLBACK_TYPE)
                self._warned.add(volume_type)
            base = self.default[FALLBACK_TYPE]
        merged = dict(base)
        merged.update(self.regions.get(region, {}).get(volume_type, {}))
        return merged

    def monthly_cost(
        self,
        volume_type: str,
        size_gib: int,
        region: str,
        iops: int | None = None,
        throughput: int | None = None,
    ) -> float:
        """Estimated monthly cost (USD) of keeping the volume, rounded to cents."""
        p = self.prices_for(volume_type, region)
        cost = size_gib * float(p.get("gb_month", 0.0))

        if iops:
            if "iops_tiers" in p:
                cost += _tiered(iops, p["iops_tiers"])
            elif "iops_month" in p:
                billable = max(0, iops - int(p.get("free_iops", 0)))
                cost += billable * float(p["iops_month"])

        if throughput and "throughput_month" in p:
            billable_tp = max(0, throughput - int(p.get("free_throughput", 0)))
            cost += billable_tp * float(p["throughput_month"])

        return round(cost, 2)

    def snapshot_monthly_cost(self, size_gib: int) -> float:
        """Upper bound for a snapshot of the full volume (real snapshots are incremental)."""
        return round(size_gib * self.snapshot_gb_month, 2)


def _tiered(quantity: int, tiers: Iterable[list[Any]]) -> float:
    total = 0.0
    lower = 0
    for upper, price in tiers:
        cap = quantity if upper is None else min(quantity, int(upper))
        if cap > lower:
            total += (cap - lower) * float(price)
        if upper is None or quantity <= int(upper):
            break
        lower = int(upper)
    return total


def fetch_storage_prices(pricing_client: Any, region: str) -> dict[str, float]:
    """Fetch $/GB-month per volume type for ``region`` from the AWS Price List API.

    The Price List API endpoint lives in us-east-1 (or ap-south-1 / eu-central-1).
    Returns e.g. ``{"gp3": 0.088, "gp2": 0.11, ...}``; missing types are omitted.
    """
    prices: dict[str, float] = {}
    paginator = pricing_client.get_paginator("get_products")
    pages = paginator.paginate(
        ServiceCode="AmazonEC2",
        Filters=[
            {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Storage"},
            {"Type": "TERM_MATCH", "Field": "regionCode", "Value": region},
        ],
    )
    for page in pages:
        for raw in page.get("PriceList", []):
            item = json.loads(raw) if isinstance(raw, str) else raw
            vtype = item.get("product", {}).get("attributes", {}).get("volumeApiName")
            if not vtype:
                continue
            for term in item.get("terms", {}).get("OnDemand", {}).values():
                for dim in term.get("priceDimensions", {}).values():
                    if dim.get("unit") != "GB-Mo":
                        continue
                    usd = float(dim.get("pricePerUnit", {}).get("USD", 0) or 0)
                    if usd > 0:
                        prices[vtype] = usd
    return prices


def apply_live_prices(book: PriceBook, pricing_client: Any, regions: Iterable[str]) -> None:
    """Overlay live per-region storage prices on ``book`` (IOPS/throughput keep defaults)."""
    for region in sorted(set(regions)):
        try:
            live = fetch_storage_prices(pricing_client, region)
        except Exception as exc:  # noqa: BLE001 - pricing is best effort
            log.warning("Live pricing unavailable for %s (%s); using defaults", region, exc)
            continue
        for vtype, gb_month in live.items():
            book.set_region_price(region, vtype, gb_month=gb_month)
        log.debug("Live prices for %s: %s", region, live)
