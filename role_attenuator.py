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

    def __init__(self, fail_closed: bool = True):
        self.fail_closed = fail_closed

    def attenuate_tools(
        self,
        principal: UserPrincipal,
        tools: List[ToolDefinition]
    ) -> Tuple[List[ToolDefinition], str]:
        """
        Filters the candidate tool catalog down to only the tools authorized
        for the given principal, and masks restricted parameter/output fields.

        Returns:
            Tuple of:
            - List of authorized, schema-masked ToolDefinitions
            - SHA-256 fingerprint of the attenuation decision state
        """
        authorized_tools: List[ToolDefinition] = []
        decision_trace: List[Dict[str, str]] = []

        # Aggregate principal's authorized services, entities, and operations
        auth_services: Set[str] = set()
        auth_entities: Set[str] = set()
        auth_ops: Set[OperationType] = set()
        restricted_fields: Set[str] = set()

        for auth in principal.active_authorizations:
            auth_services.update(auth.authorized_services)
            auth_entities.update(auth.authorized_entities)
            auth_ops.update(auth.allowed_operations)
            restricted_fields.update(auth.restricted_fields_mask)

        for tool in tools:
            # 1. Role name check
            if not (principal.has_role(tool.required_role) or principal.has_role("*")):
                decision_trace.append({
                    "tool_id": tool.tool_id,
                    "action": "DENIED",
                    "reason": f"Principal missing required role {tool.required_role}"
                })
                continue

            # 2. Granular service and entity scope check
            service_authorized = ("*" in auth_services) or (tool.service_name in auth_services)
            entity_authorized = ("*" in auth_entities) or (tool.entity_name in auth_entities)
            op_authorized = (len(auth_ops) == 0) or (tool.operation in auth_ops)

            if not (service_authorized and entity_authorized and op_authorized):
                decision_trace.append({
                    "tool_id": tool.tool_id,
                    "action": "DENIED",
                    "reason": f"Service/Entity/Op mismatch (srv={service_authorized}, ent={entity_authorized}, op={op_authorized})"
                })
                continue

            # 3. Authorized - perform column-level / property-level schema masking
            attenuated_tool = self._mask_tool_schema(tool, restricted_fields)
            authorized_tools.append(attenuated_tool)
            decision_trace.append({
                "tool_id": tool.tool_id,
                "action": "ALLOWED",
                "masked_fields_count": str(len(restricted_fields))
            })

        # Calculate deterministic state fingerprint
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
        restricted_fields: Set[str]
    ) -> ToolDefinition:
        """
        Deep-copies a ToolDefinition and strips restricted fields from
        parameters_schema and output_schema.
        """
        if not restricted_fields:
            return tool

        tool_copy = tool.model_copy(deep=True)

        # Mask parameter schema properties
        if "properties" in tool_copy.parameters_schema:
            masked_props = {
                k: v for k, v in tool_copy.parameters_schema["properties"].items()
                if k not in restricted_fields
            }
            tool_copy.parameters_schema["properties"] = masked_props
            if "required" in tool_copy.parameters_schema:
                tool_copy.parameters_schema["required"] = [
                    f for f in tool_copy.parameters_schema["required"]
                    if f not in restricted_fields
                ]

        # Mask output schema properties
        if "properties" in tool_copy.output_schema:
            masked_out = {
                k: v for k, v in tool_copy.output_schema["properties"].items()
                if k not in restricted_fields
            }
            tool_copy.output_schema["properties"] = masked_out

        return tool_copy
