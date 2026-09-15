"""Deterministic boundary adapter for model-authored PlanIR JSON.

The internal PlanIR stays strict.  This module only repairs common transport
shapes produced by otherwise usable model plans (string input references,
object-shaped edges, and harmless field aliases) before Pydantic validation.
It never invents business nodes or changes the task goal.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from orchestration.planning.plan_ir import PlanIR, PlanPatch


class PlanFormatReport(BaseModel):
    repairs: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def repair_count(self) -> int:
        return len(self.repairs)


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return deepcopy(value)
    if not isinstance(value, str):
        raise ValueError(f"规划输出必须是 JSON 对象或文本，实际为 {type(value).__name__}")
    candidates: list[str] = []
    candidates.extend(
        match.group(1) for match in re.finditer(
            r"```(?:json)?\s*(\{.*?\})\s*```", value, re.DOTALL | re.IGNORECASE,
        )
    )
    start = value.find("{")
    if start >= 0:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(value)):
            char = value[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(value[start:index + 1])
                    break
    errors: list[str] = []
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError as exc:
            errors.append(str(exc))
    raise ValueError("规划输出中没有可解析的 JSON 对象" + (f": {errors[-1]}" if errors else ""))


def _looks_like_artifact(value: str) -> bool:
    path = Path(value)
    return (
        value.startswith("artifacts/")
        or value in {"normalized_input", "normalized_input.json"}
        or bool(path.suffix)
    )


def _normalise_nodes(
    raw_nodes: Any,
    task_spec: dict[str, Any],
    report: PlanFormatReport,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    if not isinstance(raw_nodes, list):
        raise ValueError("PlanIR.nodes 必须是数组")
    nodes: list[dict[str, Any]] = []
    for index, item in enumerate(raw_nodes):
        if not isinstance(item, dict):
            raise ValueError(f"PlanIR.nodes[{index}] 必须是对象")
        node = deepcopy(item)
        aliases = {
            "node_id": ("id",),
            "objective": ("description", "task"),
            "node_type": ("type",),
            "primary_capability": ("capability",),
            "dependencies": ("depends_on",),
        }
        for target, sources in aliases.items():
            if node.get(target) is None:
                for source in sources:
                    if node.get(source) is not None:
                        node[target] = node[source]
                        report.repairs.append({
                            "action": "field_alias", "node_index": index,
                            "from": source, "to": target,
                        })
                        break
        node.setdefault("dependencies", [])
        node.setdefault("supporting_capabilities", [])
        node.setdefault("required_inputs", [])
        node.setdefault("output_artifacts", [])
        node.setdefault("acceptance_criteria", [])
        forbidden_artifacts = set(
            task_spec.get("planning_contract", {}).get("postprocess_deliverables", [])
        ) | set(task_spec.get("planning_contract", {}).get("framework_artifacts", []))
        if forbidden_artifacts and node.get("output_artifacts"):
            original_outputs = list(node.get("output_artifacts") or [])
            node["output_artifacts"] = [
                artifact for artifact in original_outputs
                if artifact not in forbidden_artifacts
            ]
            removed = sorted(set(original_outputs) - set(node["output_artifacts"]))
            if removed:
                report.repairs.append({
                    "action": "outer_artifacts_removed_from_plan",
                    "node_id": node.get("node_id"), "artifacts": removed,
                })
        if node.get("node_type") == "compound" and not node.get("primary_capability"):
            node["primary_capability"] = "reasoning"
            report.repairs.append({
                "action": "compound_capability_defaulted",
                "node_id": node.get("node_id"), "capability": "reasoning",
            })
        expected = node.get("expected_output")
        if isinstance(expected, str):
            node["expected_output"] = {
                "output_id": expected,
                "output_type": "structured_result",
                "description": expected,
            }
            report.repairs.append({
                "action": "logical_output_from_string", "node_id": node.get("node_id"),
            })
        elif isinstance(expected, list):
            # Some compatible model endpoints wrap a single logical output in
            # a list.  PlanIR intentionally models one independently
            # verifiable state transition per primitive, so repair only the
            # transport shape and leave business validation to PlanValidator.
            values = [str(item).strip() for item in expected if str(item).strip()]
            output_id = (
                Path(values[0]).stem
                if len(values) == 1
                else f"{node.get('node_id', 'node')}_result"
            )
            node["expected_output"] = (
                {
                    "output_id": output_id,
                    "output_type": "structured_result",
                    "description": "、".join(values),
                }
                if values and node.get("node_type") != "compound"
                else None
            )
            report.repairs.append({
                "action": "logical_output_from_list",
                "node_id": node.get("node_id"),
                "values": values,
            })
        elif isinstance(expected, dict) and not expected.get("output_id") and node.get("node_type") == "compound":
            node["expected_output"] = None
            report.repairs.append({
                "action": "compound_logical_output_removed",
                "node_id": node.get("node_id"),
            })
        elif isinstance(expected, dict):
            expected["output_type"] = expected.get("output_type") or expected.get("type") or "structured_result"
            if not expected.get("output_id"):
                path_value = expected.get("path") or expected.get("file")
                expected["output_id"] = (
                    Path(str(path_value)).stem if path_value else f"{node.get('node_id', 'node')}_result"
                )
                report.repairs.append({
                    "action": "logical_output_id_defaulted", "node_id": node.get("node_id"),
                })
            if not expected.get("description"):
                expected["description"] = str(expected["output_id"])
                report.repairs.append({
                    "action": "logical_output_description_defaulted",
                    "node_id": node.get("node_id"),
                })
        elif expected is None and node.get("node_type") == "primitive":
            output_id = Path(str((node.get("output_artifacts") or [""])[0])).stem
            node["expected_output"] = {
                "output_id": output_id or f"{node.get('node_id', 'node')}_result",
                "output_type": "structured_result",
                "description": str(node.get("objective") or "primitive result"),
            }
            report.repairs.append({
                "action": "primitive_logical_output_defaulted", "node_id": node.get("node_id"),
            })
        criteria = []
        for criterion in node.get("acceptance_criteria") or []:
            if isinstance(criterion, str):
                criteria.append({"type": "semantic_check", "description": criterion})
                report.repairs.append({
                    "action": "acceptance_criterion_from_string",
                    "node_id": node.get("node_id"),
                })
            elif isinstance(criterion, dict):
                criteria.append(criterion)
        node["acceptance_criteria"] = criteria
        nodes.append(node)

    output_producers: dict[str, str] = {}
    for node in nodes:
        node_id = str(node.get("node_id") or "")
        expected = node.get("expected_output")
        if node_id and isinstance(expected, dict) and expected.get("output_id"):
            output_producers[str(expected["output_id"])] = node_id
        for artifact in node.get("output_artifacts") or []:
            if node_id:
                output_producers[str(artifact)] = node_id

    task_files = {
        str(path) for path in task_spec.get("input", {}).get("files", [])
    }
    task_basenames = {Path(path).name for path in task_files}
    node_ids = {str(node.get("node_id")) for node in nodes if node.get("node_id")}
    for node in nodes:
        node_id = str(node.get("node_id") or "")
        normalised_inputs: list[dict[str, Any]] = []
        dependencies = list(node.get("dependencies") or [])
        for raw_input in node.get("required_inputs") or []:
            if isinstance(raw_input, dict):
                ref = deepcopy(raw_input)
                ref.setdefault("name", ref.get("path") or ref.get("output_id") or "")
                producer = ref.get("producer_node_id")
                if producer and ref.get("source_type") != "user_input":
                    # A producer_node_id is authoritative evidence that this
                    # is a node-output reference, even if the model labels it
                    # as an artifact.
                    if ref.get("source_type") != "node_output":
                        report.repairs.append({
                            "action": "producer_ref_retyped_as_node_output",
                            "node_id": node_id, "producer_node_id": producer,
                        })
                    ref["source_type"] = "node_output"
                if ref.get("source_type") not in {"user_input", "artifact", "node_output"}:
                    previous = ref.get("source_type")
                    ref["source_type"] = "node_output" if producer else "artifact"
                    report.repairs.append({
                        "action": "input_ref_source_type_normalized",
                        "node_id": node_id, "from": previous,
                        "to": ref["source_type"],
                    })
                ref.setdefault("fields", [])
            elif isinstance(raw_input, str):
                value = raw_input.strip()
                producer = output_producers.get(value)
                if producer and producer != node_id:
                    ref = {
                        "source_type": "node_output", "name": value,
                        "producer_node_id": producer, "fields": [],
                    }
                    if producer not in dependencies:
                        dependencies.append(producer)
                elif value in node_ids and value != node_id:
                    ref = {
                        "source_type": "node_output", "name": value,
                        "producer_node_id": value, "fields": [],
                    }
                    if value not in dependencies:
                        dependencies.append(value)
                elif value in task_files or Path(value).name in task_basenames or "/inputs/" in value:
                    ref = {"source_type": "user_input", "name": value, "fields": []}
                else:
                    artifact_name = "normalized_input.json" if value == "normalized_input" else value
                    ref = {"source_type": "artifact", "name": artifact_name, "fields": []}
                report.repairs.append({
                    "action": "input_ref_from_string", "node_id": node_id,
                    "input": value, "source_type": ref["source_type"],
                })
            else:
                raise ValueError(f"节点 {node_id} 的 required_inputs 包含不支持类型")
            producer = ref.get("producer_node_id")
            if ref.get("source_type") == "node_output" and producer and producer not in dependencies:
                dependencies.append(producer)
            normalised_inputs.append(ref)
        node["required_inputs"] = normalised_inputs
        node["dependencies"] = list(dict.fromkeys(str(dep) for dep in dependencies if dep))
    # A field allowlist is safe only when the producer exposes a declared
    # schema.  Models frequently guess names such as ``variables`` for a
    # generic structured Agent result; retaining that guess would make the
    # runtime reject an otherwise valid dependency.  With no schema_ref, pass
    # the direct structured result as a whole and let the consumer interpret
    # it.  Explicit schema-bound fields remain available for low-entropy
    # routing.
    for node in nodes:
        for ref in node.get("required_inputs", []):
            if ref.get("source_type") != "node_output" or not ref.get("fields"):
                continue
            producer = next(
                (item for item in nodes if item.get("node_id") == ref.get("producer_node_id")),
                None,
            )
            expected = producer.get("expected_output") if producer else None
            if not isinstance(expected, dict) or not expected.get("schema_ref"):
                report.repairs.append({
                    "action": "unbound_dependency_fields_cleared",
                    "node_id": node.get("node_id"),
                    "producer_node_id": ref.get("producer_node_id"),
                    "fields": list(ref.get("fields") or []),
                })
                ref["fields"] = []
    return nodes, output_producers


def _normalise_edges(
    raw_edges: Any,
    nodes: list[dict[str, Any]],
    report: PlanFormatReport,
) -> list[tuple[str, str]]:
    if raw_edges is None:
        raw_edges = []
    if not isinstance(raw_edges, list):
        raise ValueError("PlanIR.edges 必须是数组")
    by_id = {str(node.get("node_id")): node for node in nodes if node.get("node_id")}

    def is_ancestor(ancestor_id: str, descendant_id: str) -> bool:
        """Return whether two known nodes have a containment relationship."""
        current_id = str(by_id.get(descendant_id, {}).get("parent_node_id") or "")
        seen: set[str] = set()
        while current_id and current_id not in seen:
            if current_id == ancestor_id:
                return True
            seen.add(current_id)
            current_id = str(by_id.get(current_id, {}).get("parent_node_id") or "")
        return False

    def is_containment_pair(source: str, target: str) -> bool:
        return is_ancestor(source, target) or is_ancestor(target, source)

    edges: list[tuple[str, str]] = []
    for index, item in enumerate(raw_edges):
        relationship = ""
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            source, target = str(item[0]), str(item[1])
        elif isinstance(item, dict):
            source = str(item.get("from_node") or item.get("source") or item.get("from") or "")
            target = str(item.get("to_node") or item.get("target") or item.get("to") or "")
            relationship = str(item.get("relationship") or item.get("type") or "").lower()
            report.repairs.append({
                "action": "edge_from_object", "edge_index": index,
                "relationship": relationship,
            })
        else:
            raise ValueError(f"PlanIR.edges[{index}] 必须是二元数组或边对象")
        if not source or not target:
            raise ValueError(f"PlanIR.edges[{index}] 缺少起点或终点")
        if relationship in {"subtask_of", "contains", "parent_of", "child_of"}:
            source_node, target_node = by_id.get(source), by_id.get(target)
            if source_node and target_node:
                source_compound = source_node.get("node_type") == "compound"
                target_compound = target_node.get("node_type") == "compound"
                if source_compound and not target_compound:
                    if not target_node.get("parent_node_id"):
                        target_node["parent_node_id"] = source
                elif target_compound and not source_compound:
                    if not source_node.get("parent_node_id"):
                        source_node["parent_node_id"] = target
                elif relationship == "child_of":
                    if not source_node.get("parent_node_id"):
                        source_node["parent_node_id"] = target
                else:
                    if not target_node.get("parent_node_id"):
                        target_node["parent_node_id"] = source
            continue
        edges.append((source, target))

    # Models sometimes repeat an already declared ``parent_node_id`` as an
    # untyped two-element edge.  In either direction this is containment, not
    # executable data flow.  Apply this after the first pass because typed
    # relationship edges above may have populated parent_node_id.
    filtered_edges: list[tuple[str, str]] = []
    for source, target in edges:
        if is_containment_pair(source, target):
            report.repairs.append({
                "action": "containment_edge_removed",
                "source": source,
                "target": target,
            })
            continue
        filtered_edges.append((source, target))
    edges = filtered_edges

    # The same malformed relationship can arrive through a node's explicit
    # dependencies rather than PlanIR.edges.  Removing only hierarchy-related
    # references is deterministic and does not invent or reorder business
    # dependencies.
    for node in nodes:
        node_id = str(node.get("node_id") or "")
        dependencies = list(node.get("dependencies") or [])
        retained_dependencies = [
            dependency for dependency in dependencies
            if not is_containment_pair(str(dependency), node_id)
        ]
        for dependency in dependencies:
            if dependency not in retained_dependencies:
                report.repairs.append({
                    "action": "containment_dependency_removed",
                    "source": str(dependency),
                    "target": node_id,
                })
        node["dependencies"] = retained_dependencies

    for source, target in edges:
        target_node = by_id.get(target)
        if target_node is not None:
            dependencies = list(target_node.get("dependencies") or [])
            if source not in dependencies:
                dependencies.append(source)
                target_node["dependencies"] = dependencies
    return list(dict.fromkeys(edges))


def _set_depths(nodes: list[dict[str, Any]], report: PlanFormatReport) -> None:
    by_id = {str(node.get("node_id")): node for node in nodes if node.get("node_id")}
    for node in nodes:
        depth = 0
        parent = node.get("parent_node_id")
        seen: set[str] = set()
        while parent and parent in by_id and parent not in seen:
            seen.add(parent)
            depth += 1
            parent = by_id[parent].get("parent_node_id")
        if node.get("decomposition_depth") != depth:
            node["decomposition_depth"] = depth
            report.repairs.append({
                "action": "decomposition_depth_recomputed",
                "node_id": node.get("node_id"), "depth": depth,
            })


def _normalise_deliverables(
    value: Any,
    report: PlanFormatReport,
) -> list[str]:
    """Convert harmless model transport shapes to PlanIR's strict strings.

    Some providers return ``{"name": "artifacts/result.json", ...}`` even
    when the schema requests a string array.  Selecting an explicitly supplied
    path does not invent or weaken a deliverable; descriptions remain model
    commentary and are deliberately not copied into the execution contract.
    """

    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("PlanIR.deliverables 必须是数组")
    deliverables: list[str] = []
    for index, item in enumerate(value):
        if isinstance(item, str):
            path = item.strip()
        elif isinstance(item, dict):
            path = str(
                item.get("name")
                or item.get("artifact_name")
                or item.get("path")
                or item.get("artifact")
                or item.get("file")
                or item.get("output")
                or ""
            ).strip()
            report.repairs.append({
                "action": "deliverable_from_object",
                "deliverable_index": index,
                "path": path,
            })
        else:
            raise ValueError(
                f"PlanIR.deliverables[{index}] 必须是字符串或包含路径的对象"
            )
        if not path:
            raise ValueError(f"PlanIR.deliverables[{index}] 缺少交付物路径")
        if path not in deliverables:
            deliverables.append(path)
    return deliverables


def adapt_plan_ir(value: Any, task_spec: dict[str, Any]) -> tuple[PlanIR, PlanFormatReport, dict[str, Any]]:
    payload = _json_object(value)
    if isinstance(payload.get("plan_ir"), dict):
        payload = payload["plan_ir"]
    report = PlanFormatReport()
    nodes, _ = _normalise_nodes(payload.get("nodes"), task_spec, report)
    edges = _normalise_edges(payload.get("edges", []), nodes, report)
    _set_depths(nodes, report)
    payload["nodes"] = nodes
    payload["edges"] = edges
    payload["deliverables"] = _normalise_deliverables(
        payload.get("deliverables", []), report,
    )
    return PlanIR.model_validate(payload), report, payload


def adapt_plan_patch(value: Any, task_spec: dict[str, Any]) -> tuple[PlanPatch, PlanFormatReport, dict[str, Any]]:
    payload = _json_object(value)
    if isinstance(payload.get("plan_patch"), dict):
        payload = payload["plan_patch"]
    report = PlanFormatReport()
    nodes, _ = _normalise_nodes(payload.get("replacement_nodes", []), task_spec, report)
    edges = _normalise_edges(payload.get("replacement_edges", []), nodes, report)
    _set_depths(nodes, report)
    payload["replacement_nodes"] = nodes
    payload["replacement_edges"] = edges
    return PlanPatch.model_validate(payload), report, payload
