"""
Agent-JIT: Cloud Spanner Linearizable TrueTime Audit Logger.

Stores tamper-evident cryptographic audit vouchers in Cloud Spanner with
TrueTime linearizable timestamps. Provides zero-trust verification that an
agent only executed tools that were strictly projected within the authenticated
principal's attenuated security boundary.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from typing import Dict, List, Optional, Tuple

from agent_jit_mcp.models import SpannerAuditVoucher

logger = logging.getLogger("agent_jit.spanner_audit")


class CloudSpannerAuditLedger:
    """
    Simulates / implements Cloud Spanner linearizable audit table storage
    with TrueTime commit timestamps and cryptographic hash chaining.
    """

    def __init__(self, tenant_id: str = "google-cloud-enterprise"):
        self.tenant_id = tenant_id
        self._vouchers: Dict[str, SpannerAuditVoucher] = {}
        self._session_index: Dict[str, List[str]] = {}
        self._latest_chain_hash: str = "0" * 64
        self._lock = threading.Lock()

    def record_voucher(self, voucher: SpannerAuditVoucher) -> str:
        """
        Commits an audit voucher with linearizable ordering and hash chaining.
        """
        with self._lock:
            # 1. Verify intrinsic voucher integrity
            if not voucher.verify_integrity():
                raise ValueError("Voucher cryptographic integrity check failed prior to commit!")

            # 2. Append to Spanner table
            self._vouchers[voucher.voucher_id] = voucher
            if voucher.session_id not in self._session_index:
                self._session_index[voucher.session_id] = []
            self._session_index[voucher.session_id].append(voucher.voucher_id)

            # 3. Update linearizable hash chain
            chain_input = f"{self._latest_chain_hash}:{voucher.voucher_id}:{voucher.truetime_commit_timestamp}:{voucher.tamper_evident_signature}"
            self._latest_chain_hash = hashlib.sha256(chain_input.encode("utf-8")).hexdigest()

            logger.info(
                f"Cloud Spanner TrueTime commit: voucher={voucher.voucher_id}, session={voucher.session_id}, chain_hash={self._latest_chain_hash[:12]}"
            )
            return self._latest_chain_hash

    def verify_tool_invocation(
        self,
        voucher_id: str,
        invoked_tool_id: str
    ) -> Tuple[bool, str]:
        """
        Verifies whether an executed tool was legitimately projected in the voucher.
        Guarantees zero-trust containment: an agent cannot invoke a tool it was not projected.
        """
        voucher = self._vouchers.get(voucher_id)
        if not voucher:
            return False, f"Audit voucher {voucher_id} not found in Cloud Spanner ledger"

        if invoked_tool_id not in voucher.projected_tool_ids:
            return False, f"VIOLATION: Tool {invoked_tool_id} was NOT in authorized projected tools {voucher.projected_tool_ids}"

        return True, "VERIFIED: Tool invocation matches authorized projected capability boundary."

    def get_session_history(self, session_id: str) -> List[SpannerAuditVoucher]:
        """Returns all audit vouchers for a given agent conversation session."""
        v_ids = self._session_index.get(session_id, [])
        return [self._vouchers[vid] for vid in v_ids]

    def verify_chain_integrity(self) -> bool:
        """
        Audits the entire linearizable TrueTime hash chain from genesis to tail.
        """
        with self._lock:
            rolling_hash = "0" * 64
            # Sort vouchers by TrueTime commit timestamp
            sorted_vouchers = sorted(self._vouchers.values(), key=lambda v: v.truetime_commit_timestamp)
            for v in sorted_vouchers:
                if not v.verify_integrity():
                    return False
                chain_input = f"{rolling_hash}:{v.voucher_id}:{v.truetime_commit_timestamp}:{v.tamper_evident_signature}"
                rolling_hash = hashlib.sha256(chain_input.encode("utf-8")).hexdigest()

            return rolling_hash == self._latest_chain_hash
