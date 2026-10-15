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
    Directed bipartite graph connecting Tools to Entities, and Entities
    to related Entities via OData foreign-key navigation properties.
    """

    def __init__(self):
        self.tool_to_entities: Dict[str, Set[str]] = collections.defaultdict(set)
        self.entity_to_tools: Dict[str, Set[str]] = collections.defaultdict(set)
        self.entity_adj: Dict[str, Set[str]] = collections.defaultdict(set)

    def register_tool(self, tool: ToolDefinition):
        self.tool_to_entities[tool.tool_id].add(tool.entity_name)
        self.entity_to_tools[tool.entity_name].add(tool.tool_id)

    def register_entity_relationship(self, source_entity: str, target_entity: str):
        self.entity_adj[source_entity].add(target_entity)
        self.entity_adj[target_entity].add(source_entity)  # Bidirectional traversal

    def get_reachable_entities(self, start_entity: str, max_hops: int = 2) -> Set[str]:
        """BFS exploration of related OData entities within max_hops."""
        visited: Set[str] = {start_entity}
        queue = collections.deque([(start_entity, 0)])

        while queue:
            curr, hops = queue.popleft()
            if hops >= max_hops:
                continue
            for neighbor in self.entity_adj.get(curr, set()):
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append((neighbor, hops + 1))
        return visited


class SchemaPruningEngine:
    """
    Evaluates goal-conditioned semantic relevance, propagates relevance across
    the capability graph, and executes bounded token-budget projection.
    """

    def __init__(self, token_budget: int = 2000, graph_decay_factor: float = 0.5):
        self.token_budget = token_budget
        self.decay_factor = graph_decay_factor
        self.graph = CapabilityGraph()

    def build_graph(self, tools: List[ToolDefinition], entities: Optional[List[ODataEntitySet]] = None):
        """Initializes the reachability graph with tools and relational navigation properties."""
        for t in tools:
            self.graph.register_tool(t)

        if entities:
            for ent in entities:
                for nav in ent.navigation_properties:
                    self.graph.register_entity_relationship(ent.entity_name, nav.target_entity)

    def _tokenize(self, text: str) -> Set[str]:
        """Simple lowercase alphanumeric tokenization."""
        return set(re.findall(r"\b[a-zA-Z0-9_]{2,}\b", text.lower()))

    def _compute_semantic_score(self, query_tokens: Set[str], tool: ToolDefinition) -> float:
        """
        Computes BM25-inspired lexical overlap and tag matching score between
        the user query and the tool definition.
        """
        if not query_tokens:
            return 0.1

        target_text = f"{tool.name} {tool.description} {tool.entity_name} {' '.join(tool.semantic_tags)}".lower()
        target_tokens = self._tokenize(target_text)

        intersection = query_tokens.intersection(target_tokens)
        if not intersection:
            return 0.05

        # Weighted score: exact matches on entity or tags get higher weight
        base_score = len(intersection) / math.sqrt(len(query_tokens) * len(target_tokens) + 1.0)
        
        # Tag bonus
        tag_tokens = set(t.lower() for t in tool.semantic_tags)
        if query_tokens.intersection(tag_tokens):
            base_score += 0.35

        return min(base_score, 1.0)

    def prune_and_project(
        self,
        user_goal: str,
        authorized_tools: List[ToolDefinition],
        initial_monolithic_count: int,
        session_id: str,
        principal: UserPrincipal,
        initial_monolithic_tokens: Optional[int] = None,
    ) -> JITProjectionContext:
        """
        Projects the optimal sub-schema under the strict token budget constraint.
        """
        start_time = time.perf_counter()

        query_tokens = self._tokenize(user_goal)
        initial_scores: Dict[str, float] = {}

        # 1. Base semantic scoring
        for tool in authorized_tools:
            initial_scores[tool.tool_id] = self._compute_semantic_score(query_tokens, tool)

        # 2. Graph reachability propagation
        propagated_scores = dict(initial_scores)
        for tool in authorized_tools:
            if initial_scores[tool.tool_id] > 0.4:
                # Find reachable entities
                reachable_entities = self.graph.get_reachable_entities(tool.entity_name, max_hops=1)
                for r_ent in reachable_entities:
                    related_tool_ids = self.graph.entity_to_tools.get(r_ent, set())
                    for r_tid in related_tool_ids:
                        if r_tid != tool.tool_id and r_tid in propagated_scores:
                            propagated_scores[r_tid] += self.decay_factor * initial_scores[tool.tool_id]

        # 3. Knapsack-style selection bounded by token budget
        # Sort tools by value density: score / estimated_tokens
        scored_tools = [
            (tool, propagated_scores.get(tool.tool_id, 0.0))
            for tool in authorized_tools
        ]
        # Sort descending by score, tiebreak on token efficiency
        scored_tools.sort(key=lambda item: (item[1], item[1] / max(item[0].estimated_tokens, 1)), reverse=True)

        projected: List[ToolDefinition] = []
        current_tokens = 0

        for tool, score in scored_tools:
            if current_tokens + tool.estimated_tokens <= self.token_budget:
                projected.append(tool)
                current_tokens += tool.estimated_tokens
            elif not projected:
                # Guarantee at least the top-1 most relevant tool even if tight budget
                projected.append(tool)
                current_tokens += tool.estimated_tokens
                break

        # Calculate metrics
        # Baseline monolithic catalog tokens (or authorized subset if full not provided)
        baseline_tokens = initial_monolithic_tokens if initial_monolithic_tokens is not None else sum(t.estimated_tokens for t in authorized_tools)
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
            synthesis_applied=False,
            synthesized_tool=None,
            projection_latency_ms=round(latency_ms, 3)
        )
