"""
Agent-JIT: Goal-Conditioned Sub-Schema Dependency Graph & Pruning Engine.

Constructs a bipartite capability reachability graph over enterprise OData
entity sets and tools. Given an active natural language user query and a token
budget constraint, computes semantic relevance with graph-hop propagation
and solves a bounded-knapsack projection to minimize context window bloat.
"""

from __future__ import annotations

import collections
import math
import re
import time
from typing import Dict, List, Optional, Set, Tuple

from agent_jit_mcp.models import (
    JITProjectionContext,
    ODataEntitySet,
    ToolDefinition,
    UserPrincipal,
)


class CapabilityGraph:
    """
    Directed bipartite multigraph connecting Tools to Entities, and Entities
    to related Entities via OData foreign-key navigation properties (symmetric closure).
    """

    def __init__(self):
        self.tool_to_entities: Dict[str, Set[str]] = collections.defaultdict(set)
        self.entity_to_tools: Dict[str, Set[str]] = collections.defaultdict(set)
        self.entity_adj: Dict[str, Set[str]] = collections.defaultdict(set)

    def register_tool(self, tool: ToolDefinition):
        self.tool_to_entities[tool.tool_id].add(tool.entity_name)
        self.entity_to_tools[tool.entity_name].add(tool.tool_id)

    def register_entity_relationship(self, source_entity: str, target_entity: str):
        if source_entity != target_entity:
            self.entity_adj[source_entity].add(target_entity)
            self.entity_adj[target_entity].add(source_entity)  # Undirected symmetric closure of E_N

    def get_undirected_degree(self, entity_name: str) -> int:
        """Returns undirected degree deg_{E_N}(ent_t) across the symmetric closure of E_N."""
        return len(self.entity_adj.get(entity_name, set()))

    def get_reachable_entities(
        self,
        start_entity: str,
        max_hops: int = 2,
        authorized_entities: Optional[Set[str]] = None,
    ) -> Dict[str, int]:
        """
        BFS exploration of related OData entities within max_hops restricted to
        authorized entities V_E^{auth}. Returns map of entity -> shortest hop distance d_N.
        """
        if authorized_entities is not None and start_entity not in authorized_entities:
            return {}
        visited: Dict[str, int] = {start_entity: 0}
        queue = collections.deque([(start_entity, 0)])

        while queue:
            curr, hops = queue.popleft()
            if hops >= max_hops:
                continue
            for neighbor in self.entity_adj.get(curr, set()):
                if authorized_entities is not None and neighbor not in authorized_entities:
                    continue
                if neighbor not in visited:
                    visited[neighbor] = hops + 1
                    queue.append((neighbor, hops + 1))
        return visited


class SchemaPruningEngine:
    """
    Evaluates goal-conditioned semantic relevance (Eq. 10), propagates relevance across
    the capability multigraph (Eq. 11) with adaptive h_max in {1, 2} and single-seed
    hop-decay admissibility theta_h = gamma^h * theta, and executes Stage 3 greedy
    value-density projection with Forward Blocking, intent-guarded Backward Replacement,
    and Liveness Override.
    """

    def __init__(
        self,
        token_budget: int = 2000,
        graph_decay_factor: float = 0.5,
        theta_seed: float = 0.40,
        tau_floor: float = 0.10,
        k_sat: float = 0.50,
    ):
        self.token_budget = token_budget
        self.decay_factor = graph_decay_factor
        self.theta_seed = theta_seed
        self.tau_floor = tau_floor
        self.k_sat = k_sat
        self.graph = CapabilityGraph()
        self.idf_table: Dict[str, float] = {}
        self.idf_median: float = 1.0
        self.avg_doc_len: float = 12.0

    def build_graph(self, tools: List[ToolDefinition], entities: Optional[List[ODataEntitySet]] = None):
        """Initializes the reachability graph and precomputes smoothed Robertson-Sparck Jones IDF."""
        self.graph = CapabilityGraph()
        doc_freqs: Dict[str, int] = collections.Counter()
        doc_lengths: List[int] = []

        for t in tools:
            self.graph.register_tool(t)
            doc_tokens = self._tokenize_list(self._tool_document_text(t))
            doc_lengths.append(len(doc_tokens))
            for tok in set(doc_tokens):
                doc_freqs[tok] += 1

        if entities:
            for ent in entities:
                for nav in ent.navigation_properties:
                    self.graph.register_entity_relationship(ent.entity_name, nav.target_entity)

        n_docs = max(len(tools), 1)
        self.avg_doc_len = sum(doc_lengths) / n_docs if doc_lengths else 12.0
        self.idf_table = {}
        for tok, df in doc_freqs.items():
            # Smoothed Robertson-Sparck Jones IDF: log(1 + (N - df + 0.5) / (df + 0.5))
            self.idf_table[tok] = math.log(1.0 + (n_docs - df + 0.5) / (df + 0.5))

        if self.idf_table:
            sorted_idfs = sorted(self.idf_table.values())
            self.idf_median = sorted_idfs[len(sorted_idfs) // 2]
        else:
            self.idf_median = 1.0

    def _tool_document_text(self, tool: ToolDefinition) -> str:
        return f"{tool.name} {tool.description} {tool.entity_name} {' '.join(tool.semantic_tags)}".lower()

    def _tokenize(self, text: str) -> Set[str]:
        """Lowercase alphanumeric tokenization returning set."""
        return set(re.findall(r"\b[a-zA-Z0-9_]{2,}\b", text.lower()))

    def _tokenize_list(self, text: str) -> List[str]:
        """Lowercase alphanumeric tokenization preserving term frequency."""
        return re.findall(r"\b[a-zA-Z0-9_]{2,}\b", text.lower())

    def _compute_semantic_score(self, query_tokens: Set[str], tool: ToolDefinition) -> float:
        """
        Computes dampened maximum-scaled BM25 relevance score s_sem(t, q) in [0, 1] (Eq. 10)
        with two-term median-IDF floor (2 * IDF_med) and lexical tag boosting.
        """
        if not query_tokens:
            return self.tau_floor

        doc_tokens = self._tokenize_list(self._tool_document_text(tool))
        # Expand compound tags (e.g. purchase_order -> purchase, order)
        for tag in tool.semantic_tags:
            for sub in tag.lower().split("_"):
                if len(sub) >= 2:
                    doc_tokens.append(sub)
        for sub in tool.entity_name.lower().split("_"):
            if len(sub) >= 2:
                doc_tokens.append(sub)

        if not doc_tokens:
            return 0.05

        tf_counts = collections.Counter(doc_tokens)
        k1 = 1.2
        b = 0.75
        doc_len = len(doc_tokens)
        avg_dl = max(self.avg_doc_len, 1.0)

        bm25_raw = 0.0
        bm25_max_raw = 0.0
        matched_any = False

        for w in query_tokens:
            idf_w = self.idf_table.get(w, self.idf_median)
            bm25_max_raw += idf_w * (k1 + 1.0)
            tf_w = tf_counts.get(w, 0)
            if tf_w > 0:
                matched_any = True
                denom = tf_w + k1 * (1.0 - b + b * (doc_len / avg_dl))
                bm25_raw += idf_w * ((tf_w * (k1 + 1.0)) / denom)

        if not matched_any:
            return 0.05

        # Eq. (10): Dampened maximum-scaled BM25 with 2 * IDF_med two-term floor
        denom_scale = max(bm25_max_raw, 2.0 * self.idf_median) + self.k_sat * self.idf_median
        s_sem = self.tau_floor + (1.0 - self.tau_floor) * (bm25_raw / max(denom_scale, 1e-6))

        # Exact tag or entity match reinforcement
        tag_tokens = set(t.lower() for t in tool.semantic_tags)
        for t in list(tag_tokens):
            tag_tokens.update(s for s in t.split("_") if len(s) >= 2)
        if query_tokens.intersection(tag_tokens):
            s_sem = min(1.0, s_sem + 0.30)

        return min(max(s_sem, 0.0), 1.0)

    def compute_parameterless_prior(
        self,
        authorized_tools: List[ToolDefinition],
        principal: UserPrincipal,
    ) -> Dict[str, float]:
        """
        Computes role-conditioned prior utility P_prior(t | R_pfcg) for parameterless
        initialization (q = empty):
        P_prior(t | R_pfcg) = (1 / |R_pfcg|) * sum_{r in R_pfcg} (1[t in T_{r}] / |T_{r}|)
                              * (1 + log(1 + deg_{E_N}(ent_t))).
        """
        roles = principal.pfcg_roles if principal.pfcg_roles else ["*"]
        role_to_tools: Dict[str, List[ToolDefinition]] = {}
        for r in roles:
            # T_{r_tilde} = {t' in T_auth | r_{t'} in {r_tilde, *} and (r_tilde, srv, ent, op) in A_auth}
            matching = []
            for t in authorized_tools:
                if t.required_role in {r, "*"} or r == "*":
                    matching.append(t)
            role_to_tools[r] = matching

        priors: Dict[str, float] = {}
        num_roles = max(len(roles), 1)
        for t in authorized_tools:
            role_sum = 0.0
            for r in roles:
                t_r = role_to_tools.get(r, [])
                if t in t_r and len(t_r) > 0:
                    role_sum += 1.0 / len(t_r)
            deg = self.graph.get_undirected_degree(t.entity_name)
            priors[t.tool_id] = (role_sum / num_roles) * (1.0 + math.log(1.0 + deg))
        return priors

    def _propagate_multigraph_scores(
        self,
        initial_scores: Dict[str, float],
        authorized_tools: List[ToolDefinition],
        h_max: int = 1,
    ) -> Tuple[Dict[str, float], Set[str], Set[str]]:
        """
        Evaluates Eq. (11) multigraph propagation up to hop horizon h_max in {1, 2}
        restricted to authorized entities V_E^{auth}. Also computes Seeds(q) and
        locked intermediate bridge entities for single-seed h_max=2 fallback.
        """
        v_e_auth: Set[str] = {t.entity_name for t in authorized_tools}
        entity_s_sem: Dict[str, float] = collections.defaultdict(float)
        for t in authorized_tools:
            entity_s_sem[t.entity_name] = max(entity_s_sem[t.entity_name], initial_scores.get(t.tool_id, 0.0))

        propagated: Dict[str, float] = {}
        for t in authorized_tools:
            base_s = initial_scores.get(t.tool_id, 0.0)
            reachable = self.graph.get_reachable_entities(
                t.entity_name, max_hops=h_max, authorized_entities=v_e_auth
            )
            max_neighbor_contrib = 0.0
            for target_ent, d_n in reachable.items():
                if d_n == 0:
                    continue
                decay = self.decay_factor ** d_n
                for t_prime_id in self.graph.entity_to_tools.get(target_ent, set()):
                    if t_prime_id != t.tool_id and t_prime_id in initial_scores:
                        contrib = decay * initial_scores[t_prime_id]
                        if contrib > max_neighbor_contrib:
                            max_neighbor_contrib = contrib
            propagated[t.tool_id] = max(base_s, max_neighbor_contrib)

        # Compute entity-level propagated score S(e, q) = max_{t in T_auth: ent_t = e} S(t, q)
        entity_s_prop: Dict[str, float] = collections.defaultdict(float)
        for t in authorized_tools:
            entity_s_prop[t.entity_name] = max(entity_s_prop[t.entity_name], propagated[t.tool_id])

        seeds: Set[str] = {e for e, score in entity_s_sem.items() if score >= self.theta_seed}
        locked_bridges: Set[str] = set()

        # If |Seeds(q)| == 1 and h_max == 2, apply hop-decay-scaled admissibility theta_h = gamma^h * theta
        if len(seeds) == 1 and h_max == 2:
            e_1 = next(iter(seeds))
            theta_1 = (self.decay_factor ** 1) * self.theta_seed  # 0.20
            theta_2 = (self.decay_factor ** 2) * self.theta_seed  # 0.10
            best_1hop: Optional[Tuple[float, str]] = None
            best_2hop: Optional[Tuple[float, str, str]] = None

            reach_map = self.graph.get_reachable_entities(e_1, max_hops=2, authorized_entities=v_e_auth)
            for e_cand, dist in reach_map.items():
                if dist == 1 and entity_s_prop.get(e_cand, 0.0) >= theta_1:
                    cand_tuple = (entity_s_prop[e_cand], e_cand)
                    if best_1hop is None or cand_tuple > best_1hop:
                        best_1hop = cand_tuple
                elif dist == 2 and entity_s_prop.get(e_cand, 0.0) >= theta_2:
                    # Find authorized bridge e_2 adjacent to both e_1 and e_cand
                    common_bridges = (
                        self.graph.entity_adj.get(e_1, set())
                        .intersection(self.graph.entity_adj.get(e_cand, set()))
                        .intersection(v_e_auth)
                    )
                    for e_2 in common_bridges:
                        chain_score = 0.5 * (entity_s_prop[e_2] + entity_s_prop[e_cand])
                        cand_2 = (chain_score, e_2, e_cand)
                        if best_2hop is None or cand_2 > best_2hop:
                            best_2hop = cand_2

            # Lock intermediate bridge of higher-ranked 2-hop chain so lower-ranked 1-hop cannot evict it
            if best_2hop is not None and (best_1hop is None or best_2hop[0] >= best_1hop[0]):
                _, e_2_bridge, e_3_target = best_2hop
                seeds.add(e_2_bridge)
                seeds.add(e_3_target)
                locked_bridges.add(e_2_bridge)
            elif best_1hop is not None:
                seeds.add(best_1hop[1])

        return propagated, seeds, locked_bridges

    def prune_and_project(
        self,
        user_goal: str,
        authorized_tools: List[ToolDefinition],
        initial_monolithic_count: int,
        session_id: str,
        principal: UserPrincipal,
        initial_monolithic_tokens: Optional[int] = None,
        active_composites: Optional[Dict[str, CompositeToolWrapper]] = None,
    ) -> JITProjectionContext:
        """
        Projects the optimal sub-schema under the strict token budget constraint B
        using Stage 3 greedy value-density selection with Forward Blocking,
        intent-guarded Backward Replacement (with fires(W) <= 2m cutoff), and Liveness Override.
        """
        start_time = time.perf_counter()
        composites_map = active_composites or {}

        query_tokens = self._tokenize(user_goal)
        is_parameterless = len(query_tokens) == 0

        # 1. Base semantic scoring (or parameterless prior ranking when q = empty)
        initial_scores: Dict[str, float] = {}
        if is_parameterless:
            initial_scores = self.compute_parameterless_prior(authorized_tools, principal)
        else:
            for tool in authorized_tools:
                initial_scores[tool.tool_id] = self._compute_semantic_score(query_tokens, tool)

        # 2. Multigraph reachability propagation with adaptive h_max in {1, 2}
        propagated_scores, seeds, _ = self._propagate_multigraph_scores(
            initial_scores, authorized_tools, h_max=1
        )
        if not is_parameterless and len(seeds) < 2:
            # Adaptive restart with h_max = 2
            propagated_scores, seeds, _ = self._propagate_multigraph_scores(
                initial_scores, authorized_tools, h_max=2
            )

        # Update composite wrapper utility super-additively (Eq. 8 and Section I parameterless template prior):
        # S(W, q) = sum_{t_k in Constituents_fwd(W)} S(t_k, q) + Delta_comp
        # When q = empty: P_prior(W) = sum_{t_k} P_prior(t_k | R_pfcg) + 0.25 * max_{t_k} P_prior(t_k | R_pfcg)
        for tool in authorized_tools:
            if tool.tool_id in composites_map:
                wrapper = composites_map[tool.tool_id]
                constituent_ids = wrapper.constituent_tool_ids or [s.tool_id for s in wrapper.steps]
                sum_const_score = sum(
                    propagated_scores.get(cid, 0.0) for cid in constituent_ids
                )
                max_const_score = max(
                    (propagated_scores.get(cid, 0.0) for cid in constituent_ids),
                    default=initial_scores.get(tool.tool_id, 0.5),
                )
                if is_parameterless:
                    propagated_scores[tool.tool_id] = sum_const_score + 0.25 * max_const_score
                else:
                    propagated_scores[tool.tool_id] = max(sum_const_score, max_const_score) + 0.25

        # 3. Stage 3 Greedy Value-Density Knapsack Solver with:
        #    - Rule 1: Forward Blocking
        #    - Rule 2: Intent-Guarded Backward Replacement (AdmittedOf[t], Watchers[t_k], fires(W) <= 2m)
        #    - Rule 3: Liveness Override
        tool_by_id: Dict[str, ToolDefinition] = {t.tool_id: t for t in authorized_tools}
        scored_tools = [
            (tool, propagated_scores.get(tool.tool_id, 0.0))
            for tool in authorized_tools
        ]
        # Sort descending by value density rho(x) = S(x, q) / Cost_tok(x), tie-breaking by score
        scored_tools.sort(
            key=lambda item: (
                item[1] / max(item[0].estimated_tokens, 1),
                item[1],
            ),
            reverse=True,
        )

        # Build Inverted Index Watchers[t_k] for deferred composites
        admitted_ids: Set[str] = set()
        admitted_of: Dict[str, Set[str]] = collections.defaultdict(set)
        watchers: Dict[str, List[str]] = collections.defaultdict(list)
        fires_count: Dict[str, int] = collections.Counter()
        current_tokens = 0

        for cid, wrapper in composites_map.items():
            c_ids = wrapper.constituent_tool_ids or [s.tool_id for s in wrapper.steps]
            for tk in c_ids:
                watchers[tk].append(cid)

        def _can_backward_replace(w_id: str) -> Tuple[bool, Set[str], int, float]:
            wrapper = composites_map[w_id]
            w_tool = tool_by_id.get(w_id)
            if w_tool is None:
                return False, set(), 0, 0.0
            # Intent-eligibility guard: query must intersect wrapper intent tokens or have >= 2 constituent seeds
            intent_toks = set(t.lower() for t in wrapper.intent_tokens)
            c_ids = wrapper.constituent_tool_ids or [s.tool_id for s in wrapper.steps]
            admitted_c = set(c_ids).intersection(admitted_ids)
            if not admitted_c:
                return False, set(), 0, 0.0
            if not (query_tokens.intersection(intent_toks) or len(admitted_c) >= 2):
                return False, set(), 0, 0.0

            evict_toks = sum(tool_by_id[tk].estimated_tokens for tk in admitted_c if tk in tool_by_id)
            evict_score = sum(propagated_scores.get(tk, 0.0) for tk in admitted_c)
            new_tokens = current_tokens - evict_toks + w_tool.estimated_tokens
            w_score = propagated_scores.get(w_id, 0.0)
            if new_tokens <= self.token_budget and w_score > evict_score:
                return True, admitted_c, evict_toks, evict_score
            return False, set(), 0, 0.0

        for tool, score in scored_tools:
            tid = tool.tool_id
            # Rule 1 (Forward Blocking): if tid is a constituent of an already-admitted composite W, skip
            if tid not in composites_map:
                blocked = False
                for w_id in watchers.get(tid, []):
                    if w_id in admitted_ids:
                        blocked = True
                        break
                if blocked:
                    continue

            # If tid is a composite wrapper W: check direct admission or Rule 2 Backward Replacement
            if tid in composites_map:
                wrapper = composites_map[tid]
                c_ids = wrapper.constituent_tool_ids or [s.tool_id for s in wrapper.steps]
                admitted_c = set(c_ids).intersection(admitted_ids)
                if admitted_c:
                    ok, to_evict, evict_toks, _ = _can_backward_replace(tid)
                    if ok:
                        for tk in to_evict:
                            admitted_ids.discard(tk)
                            for w_watch in watchers.get(tk, []):
                                admitted_of[w_watch].discard(tk)
                        admitted_ids.add(tid)
                        current_tokens = current_tokens - evict_toks + tool.estimated_tokens
                    continue

            if current_tokens + tool.estimated_tokens <= self.token_budget:
                admitted_ids.add(tid)
                current_tokens += tool.estimated_tokens
                # Update Watchers[tid] for deferred composites with heuristic cutoff fires(W) <= 2m
                for w_id in watchers.get(tid, []):
                    if w_id not in admitted_ids and w_id in composites_map:
                        admitted_of[w_id].add(tid)
                        m_len = max(len(composites_map[w_id].constituent_tool_ids or composites_map[w_id].steps), 1)
                        if fires_count[w_id] < 2 * m_len:
                            fires_count[w_id] += 1
                            ok, to_evict, evict_toks, _ = _can_backward_replace(w_id)
                            if ok:
                                w_tool = tool_by_id[w_id]
                                for tk in to_evict:
                                    admitted_ids.discard(tk)
                                    for w_watch in watchers.get(tk, []):
                                        admitted_of[w_watch].discard(tk)
                                admitted_ids.add(w_id)
                                current_tokens = current_tokens - evict_toks + w_tool.estimated_tokens

        # Rule 3 (Liveness Override): if an un-admitted composite W was skipped due to budget B,
        # ensure its highest-scoring atomic constituent tools are admitted within B
        for w_id, wrapper in composites_map.items():
            if w_id not in admitted_ids:
                c_ids = wrapper.constituent_tool_ids or [s.tool_id for s in wrapper.steps]
                c_tools = [tool_by_id[tk] for tk in c_ids if tk in tool_by_id and tk not in admitted_ids]
                c_tools.sort(key=lambda t: propagated_scores.get(t.tool_id, 0.0), reverse=True)
                for ct in c_tools:
                    if current_tokens + ct.estimated_tokens <= self.token_budget:
                        admitted_ids.add(ct.tool_id)
                        current_tokens += ct.estimated_tokens

        if not admitted_ids and scored_tools:
            # Guarantee top-1 tool if budget is extremely tight
            top_tool = scored_tools[0][0]
            admitted_ids.add(top_tool.tool_id)
            current_tokens += top_tool.estimated_tokens

        # Preserve descending score order among admitted tools
        projected: List[ToolDefinition] = [
            tool for tool, _ in scored_tools if tool.tool_id in admitted_ids
        ]

        baseline_tokens = (
            initial_monolithic_tokens
            if initial_monolithic_tokens is not None
            else sum(t.estimated_tokens for t in authorized_tools)
        )
        if baseline_tokens > 0:
            reduction_pct = max(0.0, (1.0 - (current_tokens / baseline_tokens)) * 100.0)
        else:
            reduction_pct = 0.0

        latency_ms = (time.perf_counter() - start_time) * 1000.0

        return JITProjectionContext(
            session_id=session_id,
            principal_id=principal.principal_id,
            user_goal=user_goal,
            initial_catalog_size=initial_monolithic_count,
            role_filtered_size=len(authorized_tools),
            projected_tools=projected,
            token_budget=self.token_budget,
            actual_projected_tokens=current_tokens,
            token_reduction_pct=round(reduction_pct, 2),
            synthesis_applied=any(t.tool_id in composites_map for t in projected),
            synthesized_tool=next(
                (composites_map[t.tool_id] for t in projected if t.tool_id in composites_map),
                None,
            ),
            projection_latency_ms=round(latency_ms, 3),
        )

