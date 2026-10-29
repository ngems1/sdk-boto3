"""Plain data structures shared by the scanner, reports and deletion plan."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class Account:
    """An AWS account to scan."""

    id: str
    name: str = ""

    @property
    def label(self) -> str:
        return f"{self.name} ({self.id})" if self.name and self.name != self.id else self.id


@dataclass(frozen=True)
class Volume:
    """An unattached EBS volume that matched the age threshold."""

    account_id: str
    account_name: str
    region: str
    volume_id: str
    volume_type: str
    size_gib: int
    iops: int | None
    throughput: int | None
    availability_zone: str
    create_time: datetime
    age_days: int
    encrypted: bool
    snapshot_id: str | None
    tags: dict[str, str] = field(default_factory=dict)
    monthly_cost: float = 0.0
    protected: bool = False

    @property
    def name(self) -> str:
        return self.tags.get("Name", "")

    @property
    def annual_cost(self) -> float:
        return round(self.monthly_cost * 12, 2)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["create_time"] = self.create_time.isoformat()
        data["name"] = self.name
        data["annual_cost"] = self.annual_cost
        return data


@dataclass(frozen=True)
class ScanError:
    """A failure scoped to one account (and optionally one region)."""

    account_id: str
    region: str | None
    stage: str
    code: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ScanResult:
    """Everything a scan produced."""

    generated_at: datetime
    min_age_days: int
    volumes: list[Volume] = field(default_factory=list)
    errors: list[ScanError] = field(default_factory=list)
    accounts: list[Account] = field(default_factory=list)
    regions_scanned: int = 0

    @property
    def total_monthly_cost(self) -> float:
        return round(sum(v.monthly_cost for v in self.volumes), 2)

    @property
    def deletable_volumes(self) -> list[Volume]:
        return [v for v in self.volumes if not v.protected]

    @property
    def deletable_monthly_cost(self) -> float:
        return round(sum(v.monthly_cost for v in self.deletable_volumes), 2)

    @property
    def total_size_gib(self) -> int:
        return sum(v.size_gib for v in self.volumes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at.isoformat(),
            "min_age_days": self.min_age_days,
            "summary": {
                "accounts_scanned": len(self.accounts),
                "regions_scanned": self.regions_scanned,
                "volume_count": len(self.volumes),
                "protected_count": len(self.volumes) - len(self.deletable_volumes),
                "total_size_gib": self.total_size_gib,
                "total_monthly_cost_usd": self.total_monthly_cost,
                "total_annual_cost_usd": round(self.total_monthly_cost * 12, 2),
                "deletable_monthly_cost_usd": self.deletable_monthly_cost,
                "error_count": len(self.errors),
            },
            "volumes": [v.to_dict() for v in self.volumes],
            "errors": [e.to_dict() for e in self.errors],
        }
