import json

import pytest

from ebs_audit.pricing import PriceBook, apply_live_prices, fetch_storage_prices


@pytest.fixture
def book() -> PriceBook:
    return PriceBook()


@pytest.mark.parametrize(
    ("vtype", "size", "iops", "throughput", "expected"),
    [
        ("gp2", 100, None, None, 10.00),
        ("gp3", 100, 3000, 125, 8.00),  # baseline IOPS/throughput are free
        ("gp3", 100, 6000, 250, 8.00 + 3000 * 0.005 + 125 * 0.04),
        ("io1", 100, 1000, None, 12.50 + 65.00),
        ("st1", 500, None, None, 22.50),
        ("sc1", 1000, None, None, 15.00),
        ("standard", 10, None, None, 0.50),
    ],
)
def test_monthly_cost(book: PriceBook, vtype: str, size: int, iops: int, throughput: int, expected: float) -> None:
    assert book.monthly_cost(vtype, size, "us-east-1", iops, throughput) == pytest.approx(expected, abs=0.01)


def test_io2_tiered_iops(book: PriceBook) -> None:
    # 70k IOPS: 32k @ 0.065 + 32k @ 0.0455 + 6k @ 0.032
    expected = 10 * 0.125 + 32000 * 0.065 + 32000 * 0.0455 + 6000 * 0.032
    assert book.monthly_cost("io2", 10, "us-east-1", 70000) == pytest.approx(expected, abs=0.01)


def test_io2_single_tier(book: PriceBook) -> None:
    assert book.monthly_cost("io2", 10, "us-east-1", 1000) == pytest.approx(1.25 + 65.0, abs=0.01)


def test_unknown_type_falls_back_to_gp2(book: PriceBook, caplog: pytest.LogCaptureFixture) -> None:
    assert book.monthly_cost("gp9", 100, "us-east-1") == 10.00
    assert "gp9" in caplog.text


def test_region_override_and_file(tmp_path, book: PriceBook) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "prices.json"
    path.write_text(
        json.dumps(
            {
                "default": {"gp3": {"gb_month": 0.07}},
                "regions": {"eu-west-1": {"gp3": {"gb_month": 0.088}}},
                "snapshot_gb_month": 0.04,
            }
        )
    )
    loaded = PriceBook.from_file(path)
    assert loaded.monthly_cost("gp3", 100, "us-east-1") == 7.00
    assert loaded.monthly_cost("gp3", 100, "eu-west-1") == 8.80
    # IOPS price still inherited from built-in defaults
    assert loaded.prices_for("gp3", "eu-west-1")["iops_month"] == 0.005
    assert loaded.snapshot_monthly_cost(100) == 4.00


class _FakePricing:
    def __init__(self, items, fail=False):  # type: ignore[no-untyped-def]
        self.items, self.fail, self.calls = items, fail, []

    def get_paginator(self, name):  # type: ignore[no-untyped-def]
        outer = self

        class _P:
            def paginate(self, **kwargs):  # type: ignore[no-untyped-def]
                outer.calls.append(kwargs)
                if outer.fail:
                    raise RuntimeError("no pricing access")
                return [{"PriceList": [json.dumps(i) for i in outer.items]}]

        return _P()


def _price_item(vtype: str, usd: str, unit: str = "GB-Mo") -> dict:  # type: ignore[type-arg]
    return {
        "product": {"attributes": {"volumeApiName": vtype}},
        "terms": {"OnDemand": {"x": {"priceDimensions": {"y": {"unit": unit, "pricePerUnit": {"USD": usd}}}}}},
    }


def test_fetch_storage_prices_parses_price_list() -> None:
    client = _FakePricing(
        [_price_item("gp3", "0.0880000000"), _price_item("gp2", "0.11"), _price_item("io1", "0.065", "IOPS-Mo")]
    )
    assert fetch_storage_prices(client, "eu-west-1") == {"gp3": 0.088, "gp2": 0.11}
    filters = {f["Field"]: f["Value"] for f in client.calls[0]["Filters"]}
    assert filters == {"productFamily": "Storage", "regionCode": "eu-west-1"}


def test_apply_live_prices_overlays_and_tolerates_failure(book: PriceBook) -> None:
    apply_live_prices(book, _FakePricing([_price_item("gp3", "0.09")]), ["eu-west-1"])
    assert book.monthly_cost("gp3", 100, "eu-west-1") == 9.00
    apply_live_prices(book, _FakePricing([], fail=True), ["ap-south-1"])  # must not raise
    assert book.monthly_cost("gp3", 100, "ap-south-1") == 8.00
