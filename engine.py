"""
Agent-JIT: End-to-End Dynamic Tool Schema Projection & Execution Middleware.

Unified gateway integrating Role Attenuation, Semantic Sub-Schema Pruning,
JIT Composite Synthesis, and Cloud Spanner TrueTime Audit Logging for
Gemini Enterprise and Vertex AI Agent Engine.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from agent_jit_mcp.catalog_fixtures import get_enterprise_entities, get_enterprise_tools
from agent_jit_mcp.jit_synthesizer import JITCompositeSynthesizer
from agent_jit_mcp.models import (
    CompositeToolWrapper,
    JITProjectionContext,
    SpannerAuditVoucher,
    ToolDefinition,
    UserPrincipal,
)
from agent_jit_mcp.role_attenuator import RoleAttenuationEngine
from agent_jit_mcp.schema_pruner import SchemaPruningEngine
from agent_jit_mcp.spanner_audit import CloudSpannerAuditLedger

logger = logging.getLogger("agent_jit.engine")


class AgentJITEngine:
    """
    Main orchestration engine deployed inline between the LLM Reasoning Loop
    (Gemini Enterprise / Vertex AI Agent Engine) and backend FastMCP tool servers.
    """

    def __init__(
        self,
        base_tools: Optional[List[ToolDefinition]] = None,
        token_budget: int = 2000,
        enable_synthesis: bool = True
    ):
        self.catalog = base_tools if base_tools is not None else get_enterprise_tools()
        self.entities = get_enterprise_entities()
        self.token_budget = token_budget
        self.enable_synthesis = enable_synthesis

        self.attenuator = RoleAttenuationEngine(fail_closed=True)
        self.pruner = SchemaPruningEngine(token_budget=token_budget)
        self.pruner.build_graph(self.catalog, self.entities)
        self.synthesizer = JITCompositeSynthesizer()
        self.spanner_ledger = CloudSpannerAuditLedger()

        # In-memory execution registry for dynamic composite tools
        self._active_composites: Dict[str, CompositeToolWrapper] = {}

    def project_tools(
        self,
        user_goal: str,
        principal: UserPrincipal,
        session_id: str
    ) -> Tuple[List[ToolDefinition], SpannerAuditVoucher, JITProjectionContext]:
        """
        Executes end-to-end JIT projection:
        1. Principal role attenuation (fail-closed)
        2. Sub-schema pruning and reachability scoring
        3. Optional JIT composite tool synthesis
        4. Linearizable TrueTime Cloud Spanner voucher commitment
        """
        start_time = time.perf_counter()

        # Step 1: Principal-propagated role attenuation
        authorized_tools, role_hash = self.attenuator.attenuate_tools(principal, self.catalog)

        # Step 2: Check for JIT Composite Synthesis
        synthesized_wrapper: Optional[CompositeToolWrapper] = None
        candidate_tools = list(authorized_tools)

        if self.enable_synthesis:
            template = self.synthesizer.should_synthesize(user_goal, authorized_tools)
            if template:
                synthesized_wrapper = self.synthesizer.synthesize(template, authorized_tools, principal)
                self._active_composites[synthesized_wrapper.composite_tool_id] = synthesized_wrapper

                # Wrap composite into a candidate ToolDefinition
                composite_tool_def = ToolDefinition(
                    tool_id=synthesized_wrapper.composite_tool_id,
                    name=synthesized_wrapper.name,
                    description=synthesized_wrapper.description,
                    service_name="JIT_SYNTHESIS_PIPELINE",
                    entity_name=f"Composite_{template['pattern_id']}",
                    required_role=next(iter(synthesized_wrapper.required_roles)),
                    parameters_schema=synthesized_wrapper.parameters_schema,
                    output_schema={"type": "object"},
                    semantic_tags=["composite", "synthesized", "fast_turn", "jit"],
                    estimated_tokens=420
                )
                candidate_tools.insert(0, composite_tool_def)

        # Step 3: Goal-conditioned sub-schema pruning under token budget
        total_catalog_tokens = sum(t.estimated_tokens for t in self.catalog)
        context = self.pruner.prune_and_project(
            user_goal=user_goal,
            authorized_tools=candidate_tools,
            initial_monolithic_count=len(self.catalog),
            session_id=session_id,
            principal=principal,
            initial_monolithic_tokens=total_catalog_tokens
        )

        if synthesized_wrapper:
            context.synthesis_applied = True
            context.synthesized_tool = synthesized_wrapper

        context.projection_latency_ms = round((time.perf_counter() - start_time) * 1000.0, 3)

        # Step 4: TrueTime Cloud Spanner linearizable audit record
        projected_ids = [t.tool_id for t in context.projected_tools]
        voucher = SpannerAuditVoucher.generate(
            tenant_id=principal.tenant_id,
            session_id=session_id,
            principal_id=principal.principal_id,
            sap_user_id=principal.sap_user_id,
            user_goal=user_goal,
            role_state=role_hash,
            projected_tool_ids=projected_ids
        )
        self.spanner_ledger.record_voucher(voucher)

        return context.projected_tools, voucher, context

    def execute_tool(
        self,
        session_id: str,
        voucher_id: str,
        tool_id: str,
        arguments: Dict[str, Any],
        atomic_executor: Optional[Callable[[str, Dict[str, Any]], Dict[str, Any]]] = None
    ) -> Dict[str, Any]:
        """
        Executes a projected tool call with zero-trust validation against Cloud Spanner voucher.
        """
        # 1. Zero-trust voucher authorization verification
        is_valid, reason = self.spanner_ledger.verify_tool_invocation(voucher_id, tool_id)
        if not is_valid:
            logger.error(f"Security Alert: Unauthorized tool invocation attempt: {reason}")
            return {
                "status": "SECURITY_VIOLATION",
                "message": reason,
                "error_code": "403_FORBIDDEN_TOOL_INVOCATION"
            }

        # 2. Check if this is a synthesized composite tool
        if tool_id in self._active_composites:
            wrapper = self._active_composites[tool_id]
            default_exec = atomic_executor or (lambda tid, p: {"mock_response_for": tid, "params": p, "records": 1})
            return self.synthesizer.execute_composite_pipeline(wrapper, arguments, default_exec)

        # 3. Standard atomic tool execution
        if atomic_executor:
            return atomic_executor(tool_id, arguments)
        else:
            return {
                "status": "SUCCESS",
                "tool_id": tool_id,
                "result": f"Executed atomic tool {tool_id} successfully with params {arguments}"
            }
