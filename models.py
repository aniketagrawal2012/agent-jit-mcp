"""
Agent-JIT / FastMCP-Loom: Dynamic Just-In-Time Tool Schema Projection,
Semantic Sub-Schema Pruning, and Role-Attuned Capability Synthesis
for Enterprise Model Context Protocol in Gemini Enterprise & Vertex AI Agent Engine.

Core Data Models & Schemas.
"""

from __future__ import annotations

import hashlib
import json
import time
from enum import Enum
from typing import Any, Dict, List, Optional, Set
from pydantic import BaseModel, Field, field_validator


class SensitivityLevel(str, Enum):
    PUBLIC = "PUBLIC"
    INTERNAL = "INTERNAL"
    RESTRICTED = "RESTRICTED"
    CONFIDENTIAL = "CONFIDENTIAL"


class OperationType(str, Enum):
    READ = "READ"
    QUERY = "QUERY"
    CREATE = "CREATE"
    UPDATE = "UPDATE"
    DELETE = "DELETE"
    EXECUTE = "EXECUTE"
    COMPOSITE = "COMPOSITE"


class ODataField(BaseModel):
    name: str = Field(..., description="Technical field name, e.g., BELNR, BUKRS, DMBTR")
    type_name: str = Field("Edm.String", description="OData EDM type, e.g., Edm.String, Edm.Decimal")
    is_key: bool = Field(False, description="Whether this field is a primary entity key")
    is_nullable: bool = Field(True, description="Whether the field is nullable")
    label: str = Field("", description="Human-readable business label, e.g., Accounting Document Number")
    sensitivity: SensitivityLevel = Field(SensitivityLevel.INTERNAL, description="Data classification level")
    allowed_roles: Set[str] = Field(default_factory=set, description="Specific PFCG roles permitted to view this field (empty = inherited)")


class ODataNavigationProperty(BaseModel):
    name: str = Field(..., description="Navigation property name, e.g., to_JournalEntryItems")
    target_entity: str = Field(..., description="Target entity set name")
    cardinality: str = Field("1..*", description="Relational cardinality, e.g., 1..1, 1..*")


class ODataEntitySet(BaseModel):
    service_name: str = Field(..., description="OData service name, e.g., API_JOURNALENTRYITEMBASIC_SRV")
    entity_name: str = Field(..., description="Entity name, e.g., A_JournalEntryItemBasic")
    fields: Dict[str, ODataField] = Field(default_factory=dict, description="Dictionary of technical field definitions")
    primary_keys: List[str] = Field(default_factory=list, description="Primary key field names")
    navigation_properties: List[ODataNavigationProperty] = Field(default_factory=list, description="Outbound relational navigation paths")
    description: str = Field("", description="Business context description of the entity set")

    def estimate_schema_tokens(self) -> int:
        """Estimate token consumption of this entity set schema in an LLM tool catalog."""
        # Baseline OpenAPI serialization tokens: ~15 tokens per field + 50 tokens base metadata
        return 50 + sum(15 for _ in self.fields)


class PFCGAuthorization(BaseModel):
    role_name: str = Field(..., description="SAP PFCG role technical name, e.g., Z_FI_GL_AUDITOR_READ")
    authorized_services: Set[str] = Field(default_factory=set, description="Authorized OData service names (* for wildcard)")
    authorized_entities: Set[str] = Field(default_factory=set, description="Authorized Entity Set names (* for wildcard)")
    allowed_operations: Set[OperationType] = Field(default_factory=set, description="Permitted operation types")
    org_level_constraints: Dict[str, List[str]] = Field(
        default_factory=dict,
        description="SAP Org-level authorization fields, e.g., {'BUKRS': ['1000', '2000'], 'EKORG': ['1000']}"
    )
    restricted_fields_mask: Set[str] = Field(
        default_factory=set,
        description="Fields explicitly stripped from LLM visibility, e.g., salary, bank account details"
    )


class UserPrincipal(BaseModel):
    principal_id: str = Field(..., description="Unique enterprise principal ID, e.g., aniketagrawal@google.com")
    tenant_id: str = Field(..., description="Multi-tenant identifier, e.g., deloitte-us-prod")
    sap_user_id: str = Field(..., description="Mapped SAP backend user ID, e.g., SAP_FIN_AUDITOR_01")
    pfcg_roles: List[str] = Field(default_factory=list, description="Assigned SAP PFCG security role names")
    active_authorizations: List[PFCGAuthorization] = Field(default_factory=list, description="Resolved PFCG authorization matrix")
    auth_token_hash: str = Field(..., description="SHA-256 hash of inbound SAML 2.0 / OAuth bearer token")

    def has_role(self, role_name: str) -> bool:
        return role_name in self.pfcg_roles or "*" in self.pfcg_roles


class ToolDefinition(BaseModel):
    tool_id: str = Field(..., description="Unique technical tool identifier, e.g., sap_gl_read_balances")
    name: str = Field(..., description="Function calling name exposed to LLM")
    description: str = Field(..., description="Detailed semantic description for LLM tool selection")
    service_name: str = Field(..., description="Originating backend service")
    entity_name: str = Field(..., description="Originating OData entity set")
    operation: OperationType = Field(OperationType.QUERY, description="Type of operation performed")
    required_role: str = Field(..., description="Required PFCG role to invoke this tool")
    parameters_schema: Dict[str, Any] = Field(default_factory=dict, description="OpenAPI v3 JSON Schema parameters")
    output_schema: Dict[str, Any] = Field(default_factory=dict, description="OpenAPI v3 JSON Schema output return")
    semantic_tags: List[str] = Field(default_factory=list, description="Vector embedding keywords and category tags")
    estimated_tokens: int = Field(350, description="Approximate token footprint when loaded in context")


class CompositeToolStep(BaseModel):
    step_id: str = Field(..., description="Execution sequence step ID")
    tool_id: str = Field(..., description="Referenced underlying atomic tool ID")
    input_mapping: Dict[str, str] = Field(
        default_factory=dict,
        description="Mapping from composite input arguments or prior step outputs, e.g. {'CompanyCode': '$input.CompanyCode'}"
    )
    filter_expression: Optional[str] = Field(None, description="OData $filter expression to apply dynamically")
    select_fields: List[str] = Field(default_factory=list, description="OData $select fields projection")


class CompositeToolWrapper(BaseModel):
    composite_tool_id: str = Field(..., description="Synthesized tool technical identifier")
    name: str = Field(..., description="Synthesized tool function name")
    description: str = Field(..., description="Dynamically synthesized prompt description explaining composite capability")
    steps: List[CompositeToolStep] = Field(default_factory=list, description="Ordered pipeline of atomic service invocations")
    parameters_schema: Dict[str, Any] = Field(default_factory=dict, description="Synthesized unified OpenAPI v3 parameter schema")
    required_roles: Set[str] = Field(default_factory=set, description="Union of all underlying step authorization roles")
    provenance_hash: str = Field(..., description="SHA-256 deterministic hash of the compiled workflow AST")


class JITProjectionContext(BaseModel):
    session_id: str = Field(..., description="Trace/session identifier")
    principal_id: str = Field(..., description="Authenticated user principal")
    user_goal: str = Field(..., description="Raw natural language prompt or workflow goal")
    initial_catalog_size: int = Field(..., description="Total available tools in monolithic repository")
    role_filtered_size: int = Field(..., description="Tools remaining after principal PFCG role attenuation")
    projected_tools: List[ToolDefinition] = Field(default_factory=list, description="JIT projected active tools")
    token_budget: int = Field(2000, description="Maximum token allocation for tool schemas")
    actual_projected_tokens: int = Field(..., description="Sum of projected tool schema tokens")
    token_reduction_pct: float = Field(..., description="Percentage token reduction vs monolithic catalog")
    synthesis_applied: bool = Field(False, description="Whether JIT composite tool synthesis was triggered")
    synthesized_tool: Optional[CompositeToolWrapper] = Field(None, description="Synthesized composite wrapper if created")
    projection_latency_ms: float = Field(..., description="Execution duration of projection engine in milliseconds")


class SpannerAuditVoucher(BaseModel):
    voucher_id: str = Field(..., description="UUIDv4 linearizable audit record identifier")
    tenant_id: str = Field(..., description="Enterprise customer tenant ID")
    session_id: str = Field(..., description="Agent session identifier")
    principal_id: str = Field(..., description="Authenticated caller principal")
    sap_user_id: str = Field(..., description="Impersonated backend SAP user")
    user_goal_hash: str = Field(..., description="SHA-256 hash of the incoming user request")
    role_attenuation_hash: str = Field(..., description="SHA-256 hash of the evaluated PFCG authorization state")
    projected_tool_ids: List[str] = Field(..., description="List of tool IDs projected into the active prompt")
    invoked_tool_id: Optional[str] = Field(None, description="Tool selected and invoked by the LLM")
    invocation_params_hash: Optional[str] = Field(None, description="SHA-256 hash of the parameters supplied by LLM")
    truetime_commit_timestamp: float = Field(
        default_factory=lambda: time.time(),
        description="Cloud Spanner TrueTime commit epoch timestamp with sub-millisecond precision"
    )
    tamper_evident_signature: str = Field(..., description="Cryptographic SHA-256 MAC linking voucher state")

    @classmethod
    def generate(
        cls,
        tenant_id: str,
        session_id: str,
        principal_id: str,
        sap_user_id: str,
        user_goal: str,
        role_state: str,
        projected_tool_ids: List[str],
        invoked_tool_id: Optional[str] = None,
        invocation_params: Optional[Dict[str, Any]] = None,
        voucher_id: Optional[str] = None
    ) -> SpannerAuditVoucher:
        import uuid
        v_id = voucher_id or str(uuid.uuid4())
        goal_hash = hashlib.sha256(user_goal.encode("utf-8")).hexdigest()
        role_hash = hashlib.sha256(role_state.encode("utf-8")).hexdigest()
        params_hash = hashlib.sha256(json.dumps(invocation_params or {}, sort_keys=True).encode("utf-8")).hexdigest() if invocation_params else None
        
        now = time.time()
        raw_payload = f"{v_id}:{tenant_id}:{session_id}:{principal_id}:{sap_user_id}:{goal_hash}:{role_hash}:{','.join(sorted(projected_tool_ids))}:{invoked_tool_id}:{params_hash}:{now:.6f}"
        signature = hashlib.sha256(raw_payload.encode("utf-8")).hexdigest()

        return cls(
            voucher_id=v_id,
            tenant_id=tenant_id,
            session_id=session_id,
            principal_id=principal_id,
            sap_user_id=sap_user_id,
            user_goal_hash=goal_hash,
            role_attenuation_hash=role_hash,
            projected_tool_ids=projected_tool_ids,
            invoked_tool_id=invoked_tool_id,
            invocation_params_hash=params_hash,
            truetime_commit_timestamp=now,
            tamper_evident_signature=signature
        )

    def verify_integrity(self) -> bool:
        """Verify the tamper-evident signature of this audit record."""
        raw_payload = f"{self.voucher_id}:{self.tenant_id}:{self.session_id}:{self.principal_id}:{self.sap_user_id}:{self.user_goal_hash}:{self.role_attenuation_hash}:{','.join(sorted(self.projected_tool_ids))}:{self.invoked_tool_id}:{self.invocation_params_hash}:{self.truetime_commit_timestamp:.6f}"
        expected = hashlib.sha256(raw_payload.encode("utf-8")).hexdigest()
        return self.tamper_evident_signature == expected
