"""Helpers for working with botocore ``ClientError`` without importing botocore.

Keeping botocore out of the core modules lets the business logic be unit-tested
with lightweight fakes; any exception exposing ``.response["Error"]`` works.
"""

from __future__ import annotations


class PlanError(Exception):
    """A deletion plan is invalid, tampered with or expired."""


def error_code(exc: BaseException) -> str:
    """Return the AWS error code of ``exc`` (e.g. ``AccessDenied``) or its class name."""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = response.get("Error", {}).get("Code")
        if code:
            return str(code)
    return type(exc).__name__


def error_message(exc: BaseException) -> str:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        message = response.get("Error", {}).get("Message")
        if message:
            return str(message)
    return str(exc)
