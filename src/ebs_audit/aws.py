"""boto3 session handling: the only module that imports boto3.

``SessionFactory`` assumes a role in each member account (cached, thread-safe)
and hands out clients with adaptive retries.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

import boto3
from botocore.config import Config

from . import __version__

log = logging.getLogger(__name__)

BOTO_CONFIG = Config(
    retries={"mode": "adaptive", "max_attempts": 10},
    user_agent_extra=f"ebs-audit/{__version__}",
    connect_timeout=10,
    read_timeout=60,
)


class SessionFactory:
    """Create boto3 sessions for member accounts via ``sts:AssumeRole``.

    * The caller's own account uses the base credentials unless ``assume_in_caller_account``
      is set (the management account usually has no EbsAuditRole).
    * Sessions are cached per account; clients are cached per (account, service, region).
    """

    def __init__(
        self,
        base_session: Any | None = None,
        *,
        role_name: str = "EbsAuditRole",
        external_id: str | None = None,
        session_name: str = "ebs-audit",
        duration_seconds: int = 3600,
        assume_in_caller_account: bool = False,
    ) -> None:
        self.base = base_session or boto3.Session()
        self.role_name = role_name
        self.external_id = external_id
        self.session_name = session_name
        self.duration_seconds = duration_seconds
        self.assume_in_caller_account = assume_in_caller_account
        self._sessions: dict[str, Any] = {}
        self._clients: dict[tuple[str, str, str], Any] = {}
        self._lock = threading.Lock()

        identity = self.base.client("sts", config=BOTO_CONFIG).get_caller_identity()
        self.caller_account: str = identity["Account"]
        self.partition: str = identity["Arn"].split(":")[1]
        log.info("Running as %s (partition %s)", identity["Arn"], self.partition)

    def role_arn(self, account_id: str) -> str:
        return f"arn:{self.partition}:iam::{account_id}:role/{self.role_name}"

    def session(self, account_id: str) -> Any:
        with self._lock:
            if account_id in self._sessions:
                return self._sessions[account_id]
            if account_id == self.caller_account and not self.assume_in_caller_account:
                sess = self.base
            else:
                params: dict[str, Any] = {
                    "RoleArn": self.role_arn(account_id),
                    "RoleSessionName": self.session_name,
                    "DurationSeconds": self.duration_seconds,
                }
                if self.external_id:
                    params["ExternalId"] = self.external_id
                creds = self.base.client("sts", config=BOTO_CONFIG).assume_role(**params)["Credentials"]
                sess = boto3.Session(
                    aws_access_key_id=creds["AccessKeyId"],
                    aws_secret_access_key=creds["SecretAccessKey"],
                    aws_session_token=creds["SessionToken"],
                )
                log.debug("Assumed %s", params["RoleArn"])
            self._sessions[account_id] = sess
            return sess

    def client(self, account_id: str, service: str, region: str) -> Any:
        key = (account_id, service, region)
        with self._lock:
            cached = self._clients.get(key)
        if cached is not None:
            return cached
        sess = self.session(account_id)
        new_client = sess.client(service, region_name=region, config=BOTO_CONFIG)
        with self._lock:
            return self._clients.setdefault(key, new_client)

    def organizations_client(self) -> Any:
        return self.base.client("organizations", config=BOTO_CONFIG)

    def pricing_client(self, region: str = "us-east-1") -> Any:
        return self.base.client("pricing", region_name=region, config=BOTO_CONFIG)
