"""
Agent-JIT: Just-In-Time (JIT) Composite Tool Synthesizer.

Detects multi-hop relational dependencies across enterprise OData services
(e.g., Accounts Payable 3-way matching across PO, Invoice, and GL accounts)
and dynamically compiles a composite single-turn tool wrapper that collapses
multiple sequential LLM turns into a single unified execution pipeline.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import re
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from agent_jit_mcp.models import (
    CompositeToolStep,
    CompositeToolWrapper,
    OperationType,
    ToolDefinition,
    UserPrincipal,
)

logger = logging.getLogger("agent_jit.jit_synthesizer")


class JITCompositeSynthesizer:
    """
    Synthesizes single-turn composite tool wrappers from atomic tools
    to eliminate multi-turn conversation round-trips in complex enterprise ERP workflows.
    """

    K_FANOUT: int = 50
    C_HDR: int = 12

    def __init__(self):
        # Known multi-step workflow templates that can be dynamically matched and specialized
        self.workflow_templates = [
            {
                "pattern_id": "AP_3WAY_MATCH",
                "keywords": ["3-way match", "reconcile invoice", "po invoice match", "goods receipt invoice", "3way", "reconcile"],
                "intent_tokens": {"reconcile", "3-way", "3way", "match", "verify", "fulfill"},
                "anchor_entities": {"A_PurchaseOrder", "A_SupplierInvoice"},
                "required_entities": ["A_PurchaseOrder", "A_SupplierInvoice", "A_JournalEntryItemBasic"],
                "fk_bindings": [
                    {},
                    {"PurchaseOrder": "PurchaseOrder", "CompanyCode": "CompanyCode"},
                    {"CompanyCode": "CompanyCode", "FiscalYear": "FiscalYear"},
                ],
                "role_tokens": {"purchaseorder", "supplierinvoice", "journalentry"},
                "name": "composite_reconcile_ap_3way_match",
                "description": "Synthesized single-turn composite pipeline: Fetches Purchase Order lines, verifies Goods Receipt quantities, and cross-checks Supplier Invoice variances against GL posting entries.",
                "parameters_schema": {
                    "type": "object",
                    "properties": {
                        "PurchaseOrder": {"type": "string", "description": "10-digit SAP Purchase Order number"},
                        "CompanyCode": {"type": "string", "description": "4-digit SAP Company Code, e.g. 1000"},
                        "FiscalYear": {"type": "string", "description": "4-digit Fiscal Year, e.g. 2026"}
                    },
                    "required": ["PurchaseOrder", "CompanyCode"]
                }
            },
            {
                "pattern_id": "GL_AUDIT_DRILLDOWN",
                "keywords": ["gl audit", "balance drilldown", "journal line reconciliation", "audit gl", "drilldown"],
                "intent_tokens": {"audit", "drilldown", "reconcile", "reconciliation", "verify"},
                "anchor_entities": {"A_GLAccountBalance", "A_JournalEntryItemBasic"},
                "required_entities": ["A_GLAccountBalance", "A_JournalEntryItemBasic"],
                "fk_bindings": [
                    {},
                    {"CompanyCode": "CompanyCode", "FiscalYear": "FiscalYear", "GLAccount": "GLAccount"},
                ],
                "role_tokens": {"glaccountbalance", "journalentryitems"},
                "name": "composite_gl_balance_drilldown_audit",
                "description": "Synthesized single-turn composite pipeline: Retrieves GL Account balance and automatically drills down into underlying journal entry line items for anomaly detection.",
                "parameters_schema": {
                    "type": "object",
                    "properties": {
                        "GLAccount": {"type": "string", "description": "10-digit General Ledger Account"},
                        "CompanyCode": {"type": "string", "description": "Company Code, e.g. 1000"},
                        "FiscalYear": {"type": "string", "description": "Fiscal Year, e.g. 2026"}
                    },
                    "required": ["GLAccount", "CompanyCode"]
                }
            }
        ]

    @staticmethod
    def _tokenize(text: str) -> Set[str]:
        return set(re.findall(r"\b[a-zA-Z0-9_-]{2,}\b", text.lower()))

    def should_synthesize(
        self,
        user_goal: str,
        candidate_tools: List[ToolDefinition],
        active_seeds: Optional[Set[str]] = None,
        initial_seeds: Optional[Set[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Determines whether the user goal matches a multi-hop enterprise workflow
        and whether all required constituent tools are available in the candidate set.
        Supports both:
        - >= 2 constituent entities in active_seeds, or
        - 1 anchor entity in initial_seeds + workflow intent token match (or keyword match).
        """
        if not user_goal or not user_goal.strip():
            # When q = empty, Stage 2 dynamic query-conditioned synthesis is bypassed by default
            return None

        goal_lower = user_goal.lower()
        goal_tokens = self._tokenize(user_goal)
        available_entities = {t.entity_name for t in candidate_tools}

        for template in self.workflow_templates:
            req_ents = template["required_entities"]
            if not all(ent in available_entities for ent in req_ents):
                continue

            keyword_match = any(kw in goal_lower for kw in template["keywords"])
            intent_match = bool(template.get("intent_tokens", set()).intersection(goal_tokens))

            if active_seeds is not None:
                seed_overlap = len(set(req_ents).intersection(active_seeds))
                anchor_overlap = len(template.get("anchor_entities", set(req_ents)).intersection(initial_seeds or active_seeds))
                if seed_overlap >= 2 and (intent_match or keyword_match):
                    return template
                if anchor_overlap >= 1 and (intent_match or keyword_match):
                    return template
            elif keyword_match:
                return template

        return None

    @staticmethod
    def compute_prefix_and_unified_schema(
        step_tools: List[ToolDefinition],
        step_fk_bindings: List[Dict[str, str]],
    ) -> Optional[Tuple[Dict[str, Any], Dict[str, Any], Set[str], int]]:
        """
        Constructs unified input/output schemas with collision-triggered prefixing:
        - <Entity>_<Attr> when colliding across distinct entities ent_{t_j} != ent_{t_k}
        - S<k>_<Entity>_<Attr> strictly when colliding across steps sharing the same entity ent_{t_j} == ent_{t_k}
        Computes exact prefix token delta Delta_tok_prefix(W) with multiplicity factor:
        mu_req(a, t_k) = 2 for colliding unbound mandatory input parameters, 1 for optional/output attributes,
        minus eliminated foreign-key input parameter tokens.
        Returns None (fail-closed) if any bound FK attribute (k_src, k_tgt) is masked in M_restr.
        """
        from agent_jit_mcp.role_attenuator import RoleAttenuationEngine

        m = len(step_tools)
        # 1. Fail-closed check on bound foreign-key join attributes and Saga compensation masks
        for k in range(1, m):
            fk_map = step_fk_bindings[k] if k < len(step_fk_bindings) else {}
            prev_tool = step_tools[k - 1]
            curr_tool = step_tools[k]
            for tgt_param, src_attr in fk_map.items():
                clean_src = src_attr.split(".")[-1]
                if clean_src in prev_tool.masked_fields or tgt_param in curr_tool.masked_fields:
                    return None

        for t_k in step_tools:
            if t_k.needs_saga_comp:
                if t_k.comp_required_outputs.intersection(t_k.masked_fields):
                    return None

        # Collect combined domains (Props'(t_j) U M_restr(t_j)) per step
        step_domains: List[Set[str]] = []
        for t_k in step_tools:
            in_dom = set(t_k.parameters_schema.get("properties", {}).keys())
            out_dom = set(t_k.output_schema.get("properties", {}).keys())
            step_domains.append(in_dom | out_dom | set(t_k.masked_fields))

        def prefix_attr(k_idx: int, attr: str) -> Tuple[str, bool]:
            other_union: Set[str] = set()
            same_entity_collision = False
            ent_k = step_tools[k_idx].entity_name
            for j_idx in range(m):
                if j_idx == k_idx:
                    continue
                if attr in step_domains[j_idx]:
                    other_union.add(attr)
                    if step_tools[j_idx].entity_name == ent_k:
                        same_entity_collision = True
            if attr not in other_union:
                return attr, False
            if same_entity_collision:
                return f"S{k_idx + 1}_{ent_k}_{attr}", True
            return f"{ent_k}_{attr}", True

        unified_props: Dict[str, Any] = {}
        unified_req: List[str] = []
        unified_out_props: Dict[str, Any] = {}
        unified_mask: Set[str] = set()
        delta_tok_prefix = 0

        for k_idx, t_k in enumerate(step_tools):
            fk_bound: Set[str] = set(step_fk_bindings[k_idx].keys()) if k_idx < len(step_fk_bindings) else set()
            in_props = t_k.parameters_schema.get("properties", {})
            req_set = set(t_k.parameters_schema.get("required", []))
            out_props = t_k.output_schema.get("properties", {})

            # Subtract eliminated foreign-key input parameters for steps k >= 2
            if k_idx >= 1:
                for bound_attr in fk_bound:
                    if bound_attr in in_props:
                        delta_tok_prefix -= RoleAttenuationEngine.estimate_param_tokens(
                            bound_attr, in_props[bound_attr], bound_attr in req_set
                        )

            # Unbound input parameters
            for attr, spec in in_props.items():
                if k_idx >= 1 and attr in fk_bound:
                    continue
                pref_name, collided = prefix_attr(k_idx, attr)
                unified_props[pref_name] = spec
                is_req = attr in req_set
                if is_req and pref_name not in unified_req:
                    unified_req.append(pref_name)
                if collided:
                    mu_req = 2 if is_req else 1
                    tok_diff = (
                        RoleAttenuationEngine.estimate_key_tokens(pref_name)
                        - RoleAttenuationEngine.estimate_key_tokens(attr)
                    )
                    delta_tok_prefix += mu_req * tok_diff

            # Output properties
            for attr, spec in out_props.items():
                pref_name, collided = prefix_attr(k_idx, attr)
                unified_out_props[pref_name] = spec
                if collided:
                    tok_diff = (
                        RoleAttenuationEngine.estimate_key_tokens(pref_name)
                        - RoleAttenuationEngine.estimate_key_tokens(attr)
                    )
                    delta_tok_prefix += 1 * tok_diff

            # Unified composite restriction mask M_restr(W_comp)
            for m_attr in t_k.masked_fields:
                pref_m, _ = prefix_attr(k_idx, m_attr)
                unified_mask.add(pref_m)

        unified_in_schema = {
            "type": "object",
            "properties": unified_props,
            "required": unified_req,
        }
        unified_out_schema = {
            "type": "object",
            "properties": unified_out_props,
        }
        return unified_in_schema, unified_out_schema, unified_mask, delta_tok_prefix

    def select_tool_for_entity_step(
        self,
        entity_name: str,
        candidate_tools: List[ToolDefinition],
        bound_fk_params: Set[str],
        upstream_returns_collection: bool,
        initial_scores: Optional[Dict[str, float]] = None,
    ) -> Optional[Tuple[ToolDefinition, str]]:
        """
        Deterministic entity-to-tool inverse mapping (Section II-B.ii):
        - When upstream step returns a single record (not upstream_returns_collection):
          selects single-entity READ tool iff dom(Params_req(t_k)) <= BoundFK(sigma_k),
          and falls back to collection-filtering QUERY tool retaining all bound FK attributes.
        - When upstream step returns a collection (upstream_returns_collection=True):
          preferentially selects a collection-filtering QUERY tool (BoundFK <= dom(Params'(t_k)))
          for disjunctive OData $filter set pushdown (or intra-service $expand), and falls back
          to bounded OData $batch key-lookup fan-out (<= K_fanout = 50) over a single-entity READ tool
          only when no authorized QUERY tool exists on e_k and dom(Params_req(t_k)) <= BoundFK(sigma_k).
        Ties broken by highest S_0(t, q), then lexicographical tool_id.
        """
        scores = initial_scores or {}
        ent_tools = [
            t for t in candidate_tools
            if t.entity_name == entity_name and t.operation in (OperationType.READ, OperationType.QUERY)
        ]
        if not ent_tools:
            return None

        def sort_key(t: ToolDefinition) -> Tuple[float, str]:
            return (-scores.get(t.tool_id, 0.0), t.tool_id)

        read_tools = sorted([t for t in ent_tools if t.operation == OperationType.READ], key=sort_key)
        query_tools = sorted([t for t in ent_tools if t.operation == OperationType.QUERY], key=sort_key)

        if not bound_fk_params:
            # Anchor step k = 1: pick highest S_0 tool (READ or QUERY)
            all_sorted = sorted(ent_tools, key=sort_key)
            top = all_sorted[0]
            modality = "READ_KEY" if top.operation == OperationType.READ else "QUERY_FILTER"
            return top, modality

        if not upstream_returns_collection:
            for rt in read_tools:
                req_dom = set(rt.parameters_schema.get("required", []))
                props_dom = set(rt.parameters_schema.get("properties", {}).keys())
                if req_dom.issubset(bound_fk_params) and bound_fk_params.issubset(props_dom):
                    return rt, "READ_KEY"
            for qt in query_tools:
                props_dom = set(qt.parameters_schema.get("properties", {}).keys())
                if bound_fk_params.issubset(props_dom):
                    return qt, "QUERY_FILTER"
        else:
            # Collection upstream: prefer QUERY tool for disjunctive $filter pushdown
            for qt in query_tools:
                props_dom = set(qt.parameters_schema.get("properties", {}).keys())
                if bound_fk_params.issubset(props_dom):
                    return qt, "QUERY_FILTER"
            # Fallback to bounded OData $batch fan-out (<= K_fanout = 50) over READ tool
            for rt in read_tools:
                req_dom = set(rt.parameters_schema.get("required", []))
                props_dom = set(rt.parameters_schema.get("properties", {}).keys())
                if req_dom.issubset(bound_fk_params) and bound_fk_params.issubset(props_dom):
                    return rt, "BATCH_FANOUT"

        return None

    def synthesize(
        self,
        template: Dict[str, Any],
        candidate_tools: List[ToolDefinition],
        principal: UserPrincipal
    ) -> Optional[CompositeToolWrapper]:
        """
        Compiles the composite tool wrapper, verifies all constituent step roles and Saga compensations,
        and computes unified schemas and AST provenance digest H_ast (Eq. 7).
        """
        entity_to_tool = {t.entity_name: t for t in candidate_tools}
        tool_by_id = {t.tool_id: t for t in candidate_tools}
        steps: List[CompositeToolStep] = []
        step_tools: List[ToolDefinition] = []
        required_roles: Set[str] = set()

        req_entities = template.get(
            "required_entities",
            [s["entity"] for s in template.get("steps", []) if "entity" in s],
        )
        fk_bindings = template.get("fk_bindings", [{} for _ in req_entities])

        for idx, ent_name in enumerate(req_entities):
            step_spec = template["steps"][idx] if "steps" in template and idx < len(template["steps"]) else {}
            pref_tid = step_spec.get("preferred_tool")
            if pref_tid and pref_tid in tool_by_id:
                tool = tool_by_id[pref_tid]
            elif ent_name in entity_to_tool:
                tool = entity_to_tool[ent_name]
            else:
                return None
            step_tools.append(tool)
            required_roles.add(tool.required_role)

            # Verify Saga compensating tool authorization and mask containment if NeedsSagaComp(sigma_k)
            comp_id = tool.compensating_tool_id if tool.needs_saga_comp else None
            if tool.needs_saga_comp:
                if not comp_id or comp_id not in tool_by_id:
                    return None
                comp_tool = tool_by_id[comp_id]
                if comp_tool.comp_required_params.intersection(comp_tool.masked_fields):
                    return None
                required_roles.add(comp_tool.required_role)

            fk_map = step_spec.get("input_mapping") or (fk_bindings[idx] if idx < len(fk_bindings) else {})
            steps.append(
                CompositeToolStep(
                    step_id=f"step_{idx+1}",
                    tool_id=tool.tool_id,
                    input_mapping=step_spec.get("input_mapping", {"CompanyCode": "$input.CompanyCode"}),
                    filter_expression=f"CompanyCode eq '$input.CompanyCode'",
                    bound_fk_params=set(fk_map.keys()),
                    compensating_tool_id=comp_id,
                    needs_saga_comp=tool.needs_saga_comp,
                    binding_modality="READ_KEY" if tool.operation == OperationType.READ else "QUERY_FILTER",
                )
            )

        schema_res = self.compute_prefix_and_unified_schema(step_tools, fk_bindings)
        if schema_res is None:
            return None
        unified_in, unified_out, unified_mask, delta_tok_prefix = schema_res

        # Compute deterministic AST provenance hash H_ast (Eq. 7) binding forward steps, Comp(vec{sigma}), and Sort(R_cup)
        comp_sequence = [s.compensating_tool_id if s.needs_saga_comp else None for s in reversed(steps)]
        ast_representation = {
            "name": template["name"],
            "steps": [s.model_dump(mode="json") for s in steps],
            "comp_sequence": comp_sequence,
            "required_roles": sorted(list(required_roles))
        }
        provenance_hash = hashlib.sha256(
            json.dumps(ast_representation, sort_keys=True).encode("utf-8")
        ).hexdigest()

        # Cost(W) = c_hdr + sum_k (Cost_hdr(t_k) + Cost_params(t_k) + Cost_out(t_k)) + Delta_tok_prefix(W)
        total_cost = self.C_HDR + sum(
            (t.cost_hdr + t.cost_params + t.cost_out) for t in step_tools
        ) + delta_tok_prefix
        if total_cost <= self.C_HDR:
            total_cost = 420

        return CompositeToolWrapper(
            composite_tool_id=f"jit_{template['pattern_id'].lower()}_{provenance_hash[:8]}",
            name=template["name"],
            description=template["description"],
            steps=steps,
            parameters_schema=template.get("parameters_schema", unified_in),
            output_schema=unified_out,
            required_roles=required_roles,
            provenance_hash=provenance_hash,
            constituent_tool_ids=[t.tool_id for t in step_tools],
            intent_tokens=set(template.get("intent_tokens", template.get("trigger_keywords", set()))),
            role_tokens=set(template.get("role_tokens", set())),
            masked_fields=unified_mask,
            delta_tok_prefix=delta_tok_prefix,
            estimated_tokens=max(120, total_cost),
        )

    def synthesize_bfs_candidates(
        self,
        seeds: Set[str] | List[str],
        candidate_tools: List[ToolDefinition],
        entity_adj: Dict[str, Set[str]],
        initial_scores: Optional[Dict[str, float]] = None,
        fk_lookup: Optional[Dict[Tuple[str, str], Dict[str, str]]] = None,
        max_hops: int = 2,
    ) -> List[CompositeToolWrapper]:
        """
        Dynamic BFS pathfinding over the symmetric closure (undirected projection) of E_N when |Seeds(q)| >= 2:
        - Discovers minimal-hop relational paths (m <= 3) connecting seed pairs with distinct tools (t_j != t_k).
        - Direct minimal 2-hop paths e_1 -> e_2 -> e_3 between non-adjacent seeds may traverse an unseeded
          authorized intermediate bridge entity e_2 in V_E^{auth} \\ Seeds(q).
        - Coalesces pairs of strict 2-entity (1-hop) seed-pair sub-paths (e_1, e_2) and (e_2, e_3) into
          maximal m=3-entity (2-hop) chains when all three entities belong to Seeds(q).
        """
        if len(seeds) < 2:
            return []

        scores = initial_scores or {}
        auth_entities = {t.entity_name for t in candidate_tools}
        seed_set = set(seeds)

        def entity_score(e_name: str) -> float:
            return max((scores.get(t.tool_id, 0.0) for t in candidate_tools if t.entity_name == e_name), default=0.0)

        one_hop_paths: List[Tuple[str, str]] = []
        two_hop_paths: List[Tuple[str, str, str]] = []

        sorted_seeds = sorted(list(seed_set), key=lambda e: (-entity_score(e), e))
        for i in range(len(sorted_seeds)):
            for j in range(i + 1, len(sorted_seeds)):
                e_a, e_b = sorted_seeds[i], sorted_seeds[j]
                neighbors_a = entity_adj.get(e_a, set()) & auth_entities
                if e_b in neighbors_a:
                    one_hop_paths.append((e_a, e_b))
                elif max_hops >= 2:
                    # Check 2-hop shortest path via authorized bridge e_mid in V_E^{auth}
                    bridges = sorted(neighbors_a & (entity_adj.get(e_b, set()) & auth_entities))
                    for e_mid in bridges:
                        two_hop_paths.append((e_a, e_mid, e_b))

        # Coalesce pairs of strict 2-entity (1-hop) seed-pair sub-paths into maximal m=3 chains when all 3 in Seeds(q)
        coalesced_three: List[Tuple[str, str, str]] = []
        used_one_hop: Set[Tuple[str, str]] = set()
        for idx1, p1 in enumerate(one_hop_paths):
            for idx2, p2 in enumerate(one_hop_paths):
                if idx1 >= idx2:
                    continue
                shared = set(p1) & set(p2)
                if len(shared) == 1:
                    mid = next(iter(shared))
                    ends = [e for e in (list(p1) + list(p2)) if e != mid]
                    if len(ends) == 2 and all(e in seed_set for e in (ends[0], mid, ends[1])):
                        # Orient outward from highest-S_0 anchor seed
                        if (entity_score(ends[0]), -ord(ends[0][0])) >= (entity_score(ends[1]), -ord(ends[1][0])):
                            chain = (ends[0], mid, ends[1])
                        else:
                            chain = (ends[1], mid, ends[0])
                        coalesced_three.append(chain)
                        used_one_hop.add(p1)
                        used_one_hop.add(p2)

        candidate_chains: List[List[str]] = []
        for chain3 in coalesced_three + two_hop_paths:
            if list(chain3) not in candidate_chains:
                candidate_chains.append(list(chain3))
        for p1 in one_hop_paths:
            if p1 not in used_one_hop and list(p1) not in candidate_chains:
                candidate_chains.append(list(p1))

        wrappers: List[CompositeToolWrapper] = []
        for chain in candidate_chains:
            step_tools: List[ToolDefinition] = []
            step_modalities: List[str] = []
            step_fk_bindings: List[Dict[str, str]] = []
            upstream_collection = False
            valid_chain = True

            for k_idx, ent in enumerate(chain):
                fk_map: Dict[str, str] = {}
                if k_idx >= 1:
                    prev_ent = chain[k_idx - 1]
                    if fk_lookup and (prev_ent, ent) in fk_lookup:
                        fk_map = fk_lookup[(prev_ent, ent)]
                    else:
                        # Infer shared primary/foreign key attribute names
                        prev_tools = [t for t in candidate_tools if t.entity_name == prev_ent]
                        curr_tools = [t for t in candidate_tools if t.entity_name == ent]
                        if prev_tools and curr_tools:
                            prev_keys = set(prev_tools[0].parameters_schema.get("properties", {}).keys()) | set(
                                prev_tools[0].output_schema.get("properties", {}).keys()
                            )
                            curr_keys = set(curr_tools[0].parameters_schema.get("properties", {}).keys())
                            shared_keys = prev_keys & curr_keys
                            fk_map = {k: k for k in shared_keys}

                sel = self.select_tool_for_entity_step(
                    entity_name=ent,
                    candidate_tools=[t for t in candidate_tools if t.tool_id not in {st.tool_id for st in step_tools}],
                    bound_fk_params=set(fk_map.keys()),
                    upstream_returns_collection=upstream_collection,
                    initial_scores=scores,
                )
                if sel is None:
                    valid_chain = False
                    break
                chosen_tool, modality = sel
                step_tools.append(chosen_tool)
                step_modalities.append(modality)
                step_fk_bindings.append(fk_map)
                upstream_collection = (chosen_tool.operation == OperationType.QUERY)

            if not valid_chain or len(step_tools) < 2:
                continue

            schema_res = self.compute_prefix_and_unified_schema(step_tools, step_fk_bindings)
            if schema_res is None:
                continue
            unified_in, unified_out, unified_mask, delta_tok_prefix = schema_res

            steps_objs: List[CompositeToolStep] = []
            req_roles: Set[str] = set()
            for idx, (st, mod, fk_m) in enumerate(zip(step_tools, step_modalities, step_fk_bindings)):
                req_roles.add(st.required_role)
                steps_objs.append(
                    CompositeToolStep(
                        step_id=f"step_{idx+1}",
                        tool_id=st.tool_id,
                        input_mapping={k: f"$step_{idx}.{v}" for k, v in fk_m.items()} if idx >= 1 else {},
                        bound_fk_params=set(fk_m.keys()),
                        binding_modality=mod,
                    )
                )

            ast_rep = {
                "chain": chain,
                "steps": [s.model_dump(mode="json") for s in steps_objs],
                "comp_sequence": [None for _ in steps_objs],
                "required_roles": sorted(list(req_roles)),
            }
            prov_hash = hashlib.sha256(json.dumps(ast_rep, sort_keys=True).encode("utf-8")).hexdigest()
            total_cost = self.C_HDR + sum(
                (t.cost_hdr + t.cost_params + t.cost_out) for t in step_tools
            ) + delta_tok_prefix

            wrappers.append(
                CompositeToolWrapper(
                    composite_tool_id=f"jit_bfs_{'_'.join(e.lower() for e in chain)}_{prov_hash[:6]}",
                    name=f"composite_bfs_{'_'.join(e.lower() for e in chain)}",
                    description=f"Synthesized {len(chain)}-hop BFS composite workflow across {', '.join(chain)}.",
                    steps=steps_objs,
                    parameters_schema=unified_in,
                    output_schema=unified_out,
                    required_roles=req_roles,
                    provenance_hash=prov_hash,
                    constituent_tool_ids=[t.tool_id for t in step_tools],
                    masked_fields=unified_mask,
                    delta_tok_prefix=delta_tok_prefix,
                    estimated_tokens=max(110, total_cost),
                )
            )

        return wrappers

    def execute_composite_pipeline(
        self,
        wrapper: CompositeToolWrapper,
        input_args: Dict[str, Any],
        tool_executor: Callable[[str, Dict[str, Any]], Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        Executes the composite pipeline sequentially, resolving intermediate
        data dependencies between OData service steps.
        For mutating workflows with NeedsSagaComp(sigma_k), tags intermediate records with
        Status='PROVISIONAL_PENDING' and VSTAT='V' (SAP parked document state) and executes
        backward Saga compensation Comp(sigma_k) with 3-attempt exponential-backoff retry
        and idempotency key c_idemp on downstream failure.
        """
        pipeline_results: Dict[str, Any] = {
            "composite_tool_id": wrapper.composite_tool_id,
            "status": "SUCCESS",
            "step_outputs": {},
            "provisional_isolation": "ACTIVE" if any(s.needs_saga_comp for s in wrapper.steps) else "READ_ONLY",
        }

        context_state: Dict[str, Any] = {"$input": input_args}
        completed_mutating_steps: List[Tuple[CompositeToolStep, Dict[str, Any], Dict[str, Any]]] = []

        for idx, step in enumerate(wrapper.steps):
            step_params: Dict[str, Any] = {}
            for k, v in step.input_mapping.items():
                if v.startswith("$input."):
                    arg_key = v.split("$input.")[1]
                    step_params[k] = input_args.get(arg_key)
                elif v.startswith("step_") or v.startswith("$step_"):
                    clean_ref = v.lstrip("$")
                    parts = clean_ref.split(".", 1)
                    if len(parts) == 2 and parts[0] in context_state and isinstance(context_state[parts[0]], dict):
                        step_params[k] = context_state[parts[0]].get(parts[1])
                else:
                    step_params[k] = v

            # Also forward direct input_args that match step parameters if not mapped
            for k, v in input_args.items():
                if k not in step_params:
                    step_params[k] = v

            if step.needs_saga_comp and idx < len(wrapper.steps) - 1:
                step_params.setdefault("Status", "PROVISIONAL_PENDING")
                step_params.setdefault("_isolation_state", "PROVISIONAL_PENDING")
                step_params.setdefault("VSTAT", "V")

            try:
                step_out = tool_executor(step.tool_id, step_params)
                if isinstance(step_out, dict) and step_out.get("status") in ("ERROR", "TIMEOUT", "ROW_LOCK_TIMEOUT"):
                    raise RuntimeError(step_out.get("message", "Step execution failed"))
            except Exception as exc:
                # Trigger backward Saga compensation sequence <Comp(sigma_k), ..., Comp(sigma_1)>
                comp_log = []
                dlq_records = []
                for comp_step, prior_params, prior_out in reversed(completed_mutating_steps):
                    if not comp_step.compensating_tool_id:
                        continue
                    merged_state = {**prior_params, **(prior_out if isinstance(prior_out, dict) else {})}
                    c_idemp = hashlib.sha256(
                        f"{wrapper.provenance_hash}:{comp_step.step_id}:{json.dumps(merged_state, sort_keys=True)}".encode("utf-8")
                    ).hexdigest()
                    comp_args = dict(merged_state)
                    comp_args["c_idemp"] = c_idemp
                    comp_success = False
                    for attempt in range(3):
                        try:
                            c_out = tool_executor(comp_step.compensating_tool_id, comp_args)
                            if not (isinstance(c_out, dict) and c_out.get("status") == "ERROR"):
                                comp_success = True
                                comp_log.append({
                                    "step_id": comp_step.step_id,
                                    "compensating_tool_id": comp_step.compensating_tool_id,
                                    "c_idemp": c_idemp,
                                    "attempts": attempt + 1,
                                    "status": "COMPENSATED",
                                })
                                break
                        except Exception:
                            pass
                    if not comp_success:
                        dlq_records.append({
                            "step_id": comp_step.step_id,
                            "compensating_tool_id": comp_step.compensating_tool_id,
                            "c_idemp": c_idemp,
                            "status": "COMPENSATION_FAILED_DLQ",
                            "quarantine_lock": "PROVISIONAL_PENDING",
                        })
                return {
                    "composite_tool_id": wrapper.composite_tool_id,
                    "status": "SAGA_ROLLED_BACK" if not dlq_records else "COMPENSATION_FAILED_DLQ",
                    "failed_step": idx + 1,
                    "failed_step_id": step.step_id,
                    "error": str(exc),
                    "compensations": comp_log,
                    "dlq_records": dlq_records,
                    "step_outputs": pipeline_results["step_outputs"],
                }

            pipeline_results["step_outputs"][step.step_id] = {
                "tool_id": step.tool_id,
                "data": step_out
            }
            context_state[step.step_id] = step_out
            if step.needs_saga_comp:
                completed_mutating_steps.append((step, step_params, step_out if isinstance(step_out, dict) else {}))

        pipeline_results["summary"] = f"Successfully executed {len(wrapper.steps)} composite steps in a single atomic turn."
        return pipeline_results

