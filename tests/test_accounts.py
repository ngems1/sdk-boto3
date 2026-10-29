from ebs_audit.accounts import filter_accounts, list_org_accounts
from ebs_audit.models import Account

from .fakes import FakeOrganizations

ACCOUNTS = [
    {"Id": "111111111111", "Name": "prod", "Status": "ACTIVE"},
    {"Id": "222222222222", "Name": "dev", "Status": "ACTIVE"},
    {"Id": "333333333333", "Name": "old", "Status": "SUSPENDED"},
    {"Id": "444444444444", "Name": "sandbox", "State": "ACTIVE"},  # newer API field
    {"Id": "555555555555", "Name": "closing", "State": "PENDING_CLOSURE"},
]


def test_lists_only_active_accounts_across_pages() -> None:
    accounts = list_org_accounts(FakeOrganizations(ACCOUNTS))
    assert [a.id for a in accounts] == ["111111111111", "222222222222", "444444444444"]
    assert accounts[0].name == "prod"


def test_ou_scope_is_recursive_and_deduplicated() -> None:
    tree = {
        "ou-root": {"accounts": ["111111111111"], "ous": ["ou-child"]},
        "ou-child": {"accounts": ["222222222222", "333333333333"], "ous": []},
    }
    org = FakeOrganizations(ACCOUNTS, tree)
    accounts = list_org_accounts(org, ["ou-root", "ou-child"])
    assert [a.id for a in accounts] == ["111111111111", "222222222222"]


def test_filter_accounts() -> None:
    accts = [Account("1"), Account("2"), Account("3")]
    assert [a.id for a in filter_accounts(accts, exclude=["2"])] == ["1", "3"]
    assert [a.id for a in filter_accounts(accts, include=["1", "3"], exclude=["3"])] == ["1"]


def test_account_label() -> None:
    assert Account("1", "prod").label == "prod (1)"
    assert Account("1").label == "1"
