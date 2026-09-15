import json
import unittest

from pydantic import ValidationError

from orchestration.graph.task_graph import (
    GraphValidationError,
    InvalidNodeTransitionError,
    NodeStatus,
    TaskGraph,
    TaskNode,
)


def node(node_id, dependencies=None, output=None, capability="analysis"):
    return TaskNode(
        node_id=node_id,
        description=f"execute {node_id}",
        capability=capability,
        dependencies=dependencies or [],
        output_artifacts=[output] if output else [],
        success_criteria=[] if output else [{"type": "node_result"}],
    )


class TaskGraphValidationTests(unittest.TestCase):
    def test_valid_graph_topological_order_and_ready_nodes(self):
        graph = TaskGraph(
            graph_id="g1", goal="test",
            nodes=[node("A"), node("B"), node("C", ["A", "B"]), node("D", ["C"])],
        )
        self.assertEqual(graph.topological_order(), ["A", "B", "C", "D"])
        self.assertEqual([item.node_id for item in graph.ready_nodes()], ["A", "B"])
        graph.mark_running("A", "executor")
        graph.mark_completed("A", {"summary": "A done"})
        self.assertEqual([item.node_id for item in graph.ready_nodes()], ["B"])
        graph.mark_running("B", "executor")
        graph.mark_completed("B", {"summary": "B done"})
        self.assertEqual([item.node_id for item in graph.ready_nodes()], ["C"])

    def test_empty_duplicate_missing_self_cycle_and_output_conflict_rejected(self):
        cases = [
            lambda: TaskGraph(graph_id="g", goal="x", nodes=[]),
            lambda: TaskGraph(graph_id="g", goal="x", nodes=[node("A"), node("A")]),
            lambda: TaskGraph(graph_id="g", goal="x", nodes=[node("A", ["missing"])]),
            lambda: TaskGraph(graph_id="g", goal="x", nodes=[node("A", ["A"])]),
            lambda: TaskGraph(graph_id="g", goal="x", nodes=[node("A", ["B"]), node("B", ["A"])]),
            lambda: TaskGraph(graph_id="g", goal="x", nodes=[node("A", output="artifacts/x.json"), node("B", output="artifacts/x.json")]),
        ]
        for make_graph in cases:
            with self.subTest(case=make_graph):
                with self.assertRaises((GraphValidationError, ValidationError)):
                    make_graph()

    def test_capability_and_verification_contract_required(self):
        with self.assertRaises(ValidationError):
            node("A", capability=" ")
        with self.assertRaises((GraphValidationError, ValidationError)):
            TaskGraph(
                graph_id="g", goal="x",
                nodes=[TaskNode(node_id="A", description="x", capability="analysis")],
            )
        graph = TaskGraph(graph_id="g", goal="x", nodes=[node("A")])
        with self.assertRaises(GraphValidationError):
            graph.validate_graph({"code"})

    def test_dependency_field_contract_only_targets_direct_dependencies(self):
        valid = TaskNode(
            node_id="B", description="consume A", capability="analysis",
            dependencies=["A"],
            dependency_fields={"A": ["/structured_output/amount"]},
            success_criteria=[{"type": "node_result"}],
        )
        self.assertEqual(
            valid.dependency_fields["A"], ["/structured_output/amount"],
        )
        with self.assertRaises(ValidationError):
            TaskNode(
                node_id="B", description="bad", capability="analysis",
                dependencies=["A"], dependency_fields={"C": ["/summary"]},
                success_criteria=[{"type": "node_result"}],
            )

    def test_failed_dependency_blocks_all_descendants(self):
        graph = TaskGraph(
            graph_id="g", goal="x",
            nodes=[node("A"), node("B", ["A"]), node("C", ["B"]), node("D")],
        )
        graph.ready_nodes()
        graph.mark_running("A", "executor")
        graph.mark_failed("A", {"error_type": "test"})
        ready = graph.ready_nodes()
        self.assertEqual([item.node_id for item in ready], ["D"])
        self.assertEqual(graph.get_node("B").status, NodeStatus.BLOCKED)
        self.assertEqual(graph.get_node("C").status, NodeStatus.BLOCKED)

    def test_illegal_state_transition_rejected(self):
        graph = TaskGraph(graph_id="g", goal="x", nodes=[node("A")])
        with self.assertRaises(InvalidNodeTransitionError):
            graph.mark_completed("A", {})
        graph.ready_nodes()
        graph.mark_running("A", "executor")
        graph.mark_failed("A", {"message": "boom"})
        graph.reset_for_retry("A")
        self.assertEqual(graph.get_node("A").status, NodeStatus.PENDING)
        self.assertEqual(graph.get_node("A").attempts, 1)

    def test_json_round_trip_and_runtime_reset(self):
        graph = TaskGraph(graph_id="g", goal="x", nodes=[node("A")])
        graph.ready_nodes()
        graph.mark_running("A", "executor")
        graph.mark_completed("A", {"value": 1})
        loaded = TaskGraph.model_validate_json(graph.model_dump_json())
        self.assertEqual(json.loads(loaded.model_dump_json()), json.loads(graph.model_dump_json()))
        loaded.reset_runtime_state()
        self.assertEqual(loaded.get_node("A").status, NodeStatus.PENDING)
        self.assertIsNone(loaded.get_node("A").result)


if __name__ == "__main__":
    unittest.main()
