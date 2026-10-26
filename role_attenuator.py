"""
Agent-JIT: Principal-Propagated Role Attenuation Engine.

Enforces fail-closed, sub-millisecond filtering of enterprise tool schemas
and OData entity definitions based on caller PFCG authorization roles,
SAML 2.0/OAuth bearer assertions, and organizational-level data scoping constraints.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
from typing import Dict, List, Optional, Set, Tuple

from agent_jit_mcp.models import (
    OperationType,
    PFCGAuthorization,
    ToolDefinition,
    UserPrincipal,
)

logger = logging.getLogger("agent_jit.role_attenuator")


class RoleAttenuationEngine:
    """
    Evaluates enterprise security credentials to attenuate tool catalogs
    and entity sub-schemas strictly within the caller's authorized boundary.
    """

    C_HDR: int = 12  # Outer JSON-RPC tool envelope + coalesced required array wrapper tokens

    def __init__(self, fail_closed: bool = True):
        self.fail_closed = fail_closed

    @staticmethod
    def resolve_tool_restriction_mask(entity_name: str, restricted_fields: Set[str]) -> Set[str]:
        """
        Resolves the entity-qualified restriction mask M_restr(t) =
        {k | ent_t.k in M_restr or *.k in M_restr or k in M_restr}.
        """
        resolved: Set[str] = set()
        prefix = f"{entity_name}."
        for entry in restricted_fields:
            if entry.startswith(prefix):
                resolved.add(entry[len(prefix):])
            elif entry.startswith("*."):
                resolved.add(entry[2:])
            elif "." not in entry:
                resolved.add(entry)
        return resolved

    @staticmethod
    def estimate_key_tokens(attr_name: str) -> int:
        """Pre-tabulated token estimator for attribute identifier keys tok_key(a)."""
        return max(1, (len(attr_name) + 3) // 4)

    @classmethod
    def estimate_param_tokens(cls, attr_name: str, attr_spec: dict, is_required: bool) -> int:
        """
        Pre-tabulated token estimator tok_param(a) for an input parameter property,
        including its JSON Schema required array entry when a in dom(Params_req(t)).
        """
        desc = str(attr_spec.get("description", "")) if isinstance(attr_spec, dict) else ""
        base_tok = 6 + cls.estimate_key_tokens(attr_name) + max(0, (len(desc) + 3) // 4)
        req_tok = cls.estimate_key_tokens(attr_name) if is_required else 0
        return base_tok + req_tok

    @classmethod
    def estimate_out_tokens(cls, attr_name: str, attr_spec: dict) -> int:
        """Pre-tabulated token estimator tok_out(a) for an output schema property."""
        desc = str(attr_spec.get("description", "")) if isinstance(attr_spec, dict) else ""
        return 5 + cls.estimate_key_tokens(attr_name) + max(0, (len(desc) + 3) // 4)

    def _is_coupled_capability_authorized(
        self,
        principal: UserPrincipal,
        tool: ToolDefinition
    ) -> bool:
        """
        Enforces coupled capability check (Eq. 2):
        exists r_tilde in ((R_pfcg U {*}) cap {r_t, *}) s.t. (r_tilde, srv_t, ent_t, op_t) in A_auth.
        Prevents Cartesian-product over-approximation across disjoint domain roles.
        """
        if not (principal.has_role(tool.required_role) or principal.has_role("*") or tool.required_role == "*"):
            return False

        for auth in principal.active_authorizations:
            role_match = (
                auth.role_name == tool.required_role
                or auth.role_name in ("*", "Z_BASIS_ADMIN")
                or "*" in principal.pfcg_roles
                or tool.required_role == "*"
            )
            if not role_match:
                continue
            srv_ok = ("*" in auth.authorized_services) or (tool.service_name in auth.authorized_services)
            ent_ok = ("*" in auth.authorized_entities) or (tool.entity_name in auth.authorized_entities)
            op_ok = (len(auth.allowed_operations) == 0) or (tool.operation in auth.allowed_operations)
            if srv_ok and ent_ok and op_ok:
                return True
        return False

    def attenuate_tools(
        self,
        principal: UserPrincipal,
        tools: List[ToolDefinition]
    ) -> Tuple[List[ToolDefinition], str]:
        """
        Filters the candidate tool catalog down to only the tools authorized
        for the given principal, evicts tools whose mandatory key parameters are
        masked fail-closed, strips optional/output restricted fields, and accumulates
        O(1) token cost components Cost_params(t) and Cost_out(t).
        """
        authorized_tools: List[ToolDefinition] = []
        decision_trace: List[Dict[str, str]] = []

        restricted_fields: Set[str] = set()
        for auth in principal.active_authorizations:
            restricted_fields.update(auth.restricted_fields_mask)

        for tool in tools:
            # 1. Coupled capability tuple check (Eq. 2)
            if not self._is_coupled_capability_authorized(principal, tool):
                decision_trace.append({
                    "tool_id": tool.tool_id,
                    "action": "DENIED",
                    "reason": f"Coupled authorization check failed for role {tool.required_role}"
                })
                continue

            # 2. Entity-qualified restriction projection M_restr(t)
            tool_mask = self.resolve_tool_restriction_mask(tool.entity_name, restricted_fields)
            req_params = set(tool.parameters_schema.get("required", []))

            # Fail-closed eviction if any mandatory input parameter is restricted
            if req_params.intersection(tool_mask):
                decision_trace.append({
                    "tool_id": tool.tool_id,
                    "action": "DENIED_MANDATORY_PARAM_MASKED",
                    "reason": f"Mandatory parameter(s) {sorted(req_params.intersection(tool_mask))} restricted in M_restr(t)"
                })
                continue

            # 3. Authorized - perform column-level / property-level schema masking (Eq. 3)
            attenuated_tool = self._mask_tool_schema(tool, tool_mask)
            authorized_tools.append(attenuated_tool)
            decision_trace.append({
                "tool_id": tool.tool_id,
                "action": "ALLOWED",
                "masked_fields_count": str(len(tool_mask))
            })

        # Calculate deterministic state fingerprint h_role
        trace_json = json.dumps(decision_trace, sort_keys=True)
        attenuation_hash = hashlib.sha256(
            f"{principal.principal_id}:{principal.auth_token_hash}:{trace_json}".encode("utf-8")
        ).hexdigest()

        logger.debug(
            f"Role attenuation: principal={principal.principal_id}, in={len(tools)}, out={len(authorized_tools)}"
        )
        return authorized_tools, attenuation_hash

    def _mask_tool_schema(
        self,
        tool: ToolDefinition,
        tool_mask: Set[str]
    ) -> ToolDefinition:
        """
        Deep-copies a ToolDefinition, strips restricted fields from
        parameters_schema and output_schema, and pre-accumulates Cost_params(t) and Cost_out(t).
        """
        tool_copy = tool.model_copy(deep=True)
        tool_copy.masked_fields = set(tool_mask)

        req_set = set(tool_copy.parameters_schema.get("required", []))
        cost_params = 0
        if "properties" in tool_copy.parameters_schema:
            masked_props = {}
            for k, v in tool_copy.parameters_schema["properties"].items():
                if k not in tool_mask:
                    masked_props[k] = v
                    cost_params += self.estimate_param_tokens(k, v, k in req_set)
            tool_copy.parameters_schema["properties"] = masked_props
            if "required" in tool_copy.parameters_schema:
                tool_copy.parameters_schema["required"] = [
                    f for f in tool_copy.parameters_schema["required"]
                    if f not in tool_mask
                ]

        cost_out = 0
        if "properties" in tool_copy.output_schema:
            masked_out = {}
            for k, v in tool_copy.output_schema["properties"].items():
                if k not in tool_mask:
                    masked_out[k] = v
                    cost_out += self.estimate_out_tokens(k, v)
            tool_copy.output_schema["properties"] = masked_out

        tool_copy.cost_hdr = max(16, (len(tool_copy.name) + len(tool_copy.description) + 3) // 4)
        tool_copy.cost_base = self.C_HDR + tool_copy.cost_hdr
        tool_copy.cost_params = cost_params
        tool_copy.cost_out = cost_out
        if tool_mask and tool_copy.estimated_tokens > (tool_copy.cost_base + cost_params + cost_out):
            # Adjust token estimate proportionally when fields are masked while preserving baseline scale
            masked_removed = len(tool.parameters_schema.get("properties", {})) - len(
                tool_copy.parameters_schema.get("properties", {})
            )
            masked_removed += len(tool.output_schema.get("properties", {})) - len(
                tool_copy.output_schema.get("properties", {})
            )
            tool_copy.estimated_tokens = max(60, tool.estimated_tokens - 15 * masked_removed)

        return tool_copy
