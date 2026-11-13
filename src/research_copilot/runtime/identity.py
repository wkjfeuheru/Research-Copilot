"""Stable persisted identities, independent of source/package display names.

Runtime version, checkpoint thread ids, dispatch UUIDs, advisory locks and
approval receipts stay frozen so replay does not fork. The SQL session table
name is not part of that protocol and is `sessions`.
"""

from hashlib import blake2b
from uuid import NAMESPACE_URL, uuid5

RUNTIME_VERSION = "v2"
DEFAULT_POLICY_VERSION = "v2-policy-1"
SESSION_TABLE_NAME = "sessions"
LEGACY_SESSION_TABLE_NAME = "rc_v2_sessions"
PROJECTION_FORMAT = "v2-json-segments-1"


def checkpoint_thread_id(report_id: str) -> str:
    return f"v2:{report_id}"


def dispatch_identity(report_id: str, call_id: str) -> str:
    return uuid5(NAMESPACE_URL, f"{checkpoint_thread_id(report_id)}:{call_id}").hex


def confirmation_receipt_id(approval_key: str) -> str:
    return f"v2:{approval_key}"


def advisory_lock_identity(report_id: str) -> int:
    return int.from_bytes(
        blake2b(f"rc-v2:{report_id}".encode(), digest_size=8).digest(), signed=True
    )
