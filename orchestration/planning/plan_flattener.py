"""Deterministically flatten compound PlanIR nodes into SemanticGraph leaves."""

from __future__ import annotations

from collections import defaultdict

from orchestration.graph.semantic_graph import SemanticGraph, SemanticNode
from orchestration.planning.plan_ir import FlattenResult, PlanIR, PlanNode, PlanNodeType


def flatten_plan_ir_to_semantic_graph(
    plan: PlanIR,
    *,
    preserve_metadata: bool = True,
) -> FlattenResult:
    by_id = {node.node_id: node for node in plan.nodes}
    if len(by_id) != len(plan.nodes):
        raise ValueError("PlanIR.node_id 必须唯一")
    children: dict[str, list[str]] = defaultdict(list)
    for node in plan.nodes:
        if node.parent_node_id:
            if node.parent_node_id not in by_id:
                raise ValueError(f"父节点不存在: {node.parent_node_id}")
            children[node.parent_node_id].append(node.node_id)

    # Parent/child containment is hierarchy metadata, not an execution edge.
    # Some model responses include both in ``edges``; retaining containment
    # edges would incorrectly turn a compound's exit back into its entry.
    def is_descendant(candidate: str, ancestor: str) -> bool:
        current = by_id.get(candidate)
        seen: set[str] = set()
        while current and current.parent_node_id and current.parent_node_id not in seen:
            if current.parent_node_id == ancestor:
                return True
            seen.add(current.parent_node_id)
            current = by_id.get(current.parent_node_id)
        return False

    for source, target in plan.edges:
        if source not in by_id or target not in by_id:
            raise ValueError(f"PlanIR 边引用不存在节点: {source}->{target}")
    def is_containment_pair(source: str, target: str) -> bool:
        return is_descendant(target, source) or is_descendant(source, target)

    candidate_edges = set(plan.edges)
    candidate_edges.update(
        (dep, node.node_id) for node in plan.nodes for dep in node.dependencies
    )
    # Containment is represented exclusively by parent_node_id.  Ignore both
    # ancestor->descendant and descendant->ancestor variants defensively so a
    # malformed model edge can never become sibling-to-sibling cycles when a
    # compound node is replaced by its primitive entries/exits.
    raw_edges = {
        edge for edge in candidate_edges
        if edge[0] in by_id and edge[1] in by_id
        and not is_containment_pair(edge[0], edge[1])
    }

    entries: dict[str, set[str]] = {}
    exits: dict[str, set[str]] = {}
    visiting: set[str] = set()

    def descendants(node_id: str) -> set[str]:
        found: set[str] = set()
        stack = list(children.get(node_id, []))
        while stack:
            current = stack.pop()
            if current in found:
                continue
            found.add(current)
            stack.extend(children.get(current, []))
        return found

    def calculate(node_id: str) -> tuple[set[str], set[str]]:
        if node_id in entries:
            return entries[node_id], exits[node_id]
        if node_id in visiting:
            raise ValueError("compound 层级存在循环")
        visiting.add(node_id)
        node = by_id[node_id]
        if node.node_type == PlanNodeType.PRIMITIVE:
            entry, exit_ = {node_id}, {node_id}
        else:
            direct_children = children.get(node_id, [])
            if not direct_children:
                raise ValueError(f"compound 节点没有子节点: {node_id}")
            all_leaves: set[str] = set()
            for child in direct_children:
                child_entry, child_exit = calculate(child)
                all_leaves.update(child_entry)
                all_leaves.update(child_exit)
            descendants_ids = descendants(node_id)
            leaf_ids = {
                item for item in descendants_ids
                if by_id[item].node_type == PlanNodeType.PRIMITIVE
            }
            internal_edges = {
                (source, target) for source, target in raw_edges
                if source in leaf_ids and target in leaf_ids
            }
            incoming = {target for _, target in internal_edges}
            outgoing = {source for source, _ in internal_edges}
            entry = leaf_ids - incoming
            exit_ = leaf_ids - outgoing
            if not entry or not exit_:
                raise ValueError(f"compound 节点入口或出口为空: {node_id}")
        visiting.remove(node_id)
        entries[node_id], exits[node_id] = entry, exit_
        return entry, exit_

    top_level = [node.node_id for node in plan.nodes if not node.parent_node_id]
    for node_id in top_level:
        calculate(node_id)
    for node in plan.nodes:
        calculate(node.node_id)

    flattened_edges: set[tuple[str, str]] = set()
    for source, target in raw_edges:
        if source not in by_id or target not in by_id:
            raise ValueError(f"PlanIR 边引用不存在节点: {source}->{target}")
        for source_leaf in exits[source]:
            for target_leaf in entries[target]:
                if source_leaf != target_leaf:
                    flattened_edges.add((source_leaf, target_leaf))

    primitive_nodes = [node for node in plan.nodes if node.node_type == PlanNodeType.PRIMITIVE]
    semantic_nodes: list[SemanticNode] = []
    for node in primitive_nodes:
        dependencies = sorted(source for source, target in flattened_edges if target == node.node_id)
        required_inputs: dict[str, list[str]] = {}
        artifact_inputs: list[str] = []
        user_inputs: list[str] = []
        for input_ref in node.required_inputs:
            if input_ref.source_type == "node_output" and input_ref.producer_node_id:
                declared_producer = input_ref.producer_node_id
                # Compound nodes disappear from the executable graph.  Their
                # outgoing data contract must therefore be carried by their
                # primitive exit nodes, which are exactly the direct
                # dependencies introduced by edge flattening.
                producer_ids = sorted(
                    producer_id
                    for producer_id in exits.get(
                        declared_producer,
                        {declared_producer},
                    )
                    if producer_id in dependencies
                )
                if not producer_ids and declared_producer in dependencies:
                    producer_ids = [declared_producer]
                fields = [
                    field if str(field).startswith("/") else "/" + str(field)
                    for field in input_ref.fields
                ]
                for producer_id in producer_ids:
                    required_inputs[producer_id] = list(dict.fromkeys([
                        *required_inputs.get(producer_id, []),
                        *fields,
                    ]))
            elif input_ref.source_type == "artifact":
                artifact_inputs.append(input_ref.name)
            else:
                user_inputs.append(input_ref.name)
        hierarchy_path = []
        current: PlanNode | None = node
        while current is not None:
            hierarchy_path.append(current.node_id)
            current = by_id.get(current.parent_node_id) if current.parent_node_id else None
        metadata = dict(node.metadata)
        metadata.update({
            "supporting_capabilities": list(node.supporting_capabilities),
            "hierarchy_path": list(reversed(hierarchy_path)),
            "output_artifacts": list(node.output_artifacts),
            "user_input_refs": user_inputs,
            "requirement_ids": list(node.requirement_ids),
            "acceptance_refs": list(node.acceptance_refs),
        })
        if preserve_metadata:
            metadata.update({
                "parent_node_id": node.parent_node_id,
                "decomposition_depth": node.decomposition_depth,
            })
        semantic_nodes.append(SemanticNode(
            node_id=node.node_id,
            objective=node.objective,
            capability=node.primary_capability,
            dependencies=dependencies,
            required_inputs=required_inputs,
            input_artifacts=artifact_inputs,
            output_artifacts=list(node.output_artifacts),
            success_criteria=list(node.acceptance_criteria),
            expected_output=(node.expected_output.model_dump(mode="json") if node.expected_output else None),
            acceptance_criteria=list(node.acceptance_criteria),
            metadata=metadata,
        ))
    semantic = SemanticGraph(
        graph_id=plan.global_goal[:64],
        version=plan.version,
        goal=plan.global_goal,
        nodes=semantic_nodes,
    )
    # SemanticGraph intentionally has no cycle validator today; a simple
    # topological check here keeps flattening deterministic and safe.
    remaining = {node.node_id: len(node.dependencies) for node in semantic_nodes}
    ready = [node_id for node_id, degree in remaining.items() if degree == 0]
    visited = 0
    children_map: dict[str, list[str]] = defaultdict(list)
    for source, target in flattened_edges:
        children_map[source].append(target)
    while ready:
        current_id = ready.pop()
        visited += 1
        for target in children_map[current_id]:
            remaining[target] -= 1
            if remaining[target] == 0:
                ready.append(target)
    if visited != len(semantic_nodes):
        raise ValueError("展平后的 primitive 图存在循环依赖")
    return FlattenResult(
        semantic_graph=semantic,
        removed_compound_nodes=[node.node_id for node in plan.nodes if node.node_type == PlanNodeType.COMPOUND],
        entry_nodes={key: sorted(value) for key, value in entries.items()},
        exit_nodes={key: sorted(value) for key, value in exits.items()},
        flattened_edges=sorted(flattened_edges),
    )
