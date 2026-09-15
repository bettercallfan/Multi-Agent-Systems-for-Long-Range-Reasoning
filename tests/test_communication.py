import json
from pathlib import Path
import tempfile
import unittest

from orchestration.communication.communication_models import (
    CommunicationBudget,
    CommunicationPolicy,
    estimate_tokens,
    utf8_size,
)
from orchestration.communication.context_builder import ContextBuilder
from orchestration.communication.context_builder import ContentReferenceError, resolve_content_reference
from orchestration.communication.context_compressors import (
    AutoCompressor,
    DeterministicCompressor,
    LLMLinguaCompressor,
)
from orchestration.execution.node_executor import NodeExecutionContext
from orchestration.graph.task_graph import TaskGraph, TaskNode


def make_node(node_id, dependencies=None):
    return TaskNode(
        node_id=node_id,
        description=f"run {node_id}",
        capability="analysis",
        dependencies=dependencies or [],
        success_criteria=[{"type": "node_result"}],
    )


class BrokenBackendFactory:
    def __init__(self):
        self.calls = 0

    def __call__(self):
        self.calls += 1
        raise ImportError("optional dependency intentionally unavailable")


class VerboseFakeBackend:
    def compress_prompt(self, text, target_token):
        return {"compressed_prompt": text * 2}


class ShortFakeBackend:
    def compress_prompt(self, text, target_token):
        return {"compressed_prompt": text[:max(1, len(text) // 3)]}


class ContractFakeBackend:
    def __init__(self):
        self.kwargs = None

    def compress_prompt(self, text, target_token, **kwargs):
        self.kwargs = kwargs
        return {"compressed_prompt": text[:max(1, len(text) // 3)]}


class CommunicationModelTests(unittest.TestCase):
    def test_policy_accepts_flat_task_spec_budget(self):
        policy = CommunicationPolicy.from_task_spec({
            "communication_policy": {
                "compressor": "deterministic",
                "max_message_tokens": 321,
                "max_message_bytes": 1234,
                "max_node_context_tokens": 777,
                "max_control_prompt_tokens": 888,
                "max_summary_chars": 99,
                "llmlingua_model": "example/test-model",
                "llmlingua_allow_download": True,
            }
        })

        self.assertEqual(policy.compressor, "deterministic")
        self.assertEqual(policy.budget.max_message_tokens, 321)
        self.assertEqual(policy.budget.max_message_bytes, 1234)
        self.assertEqual(policy.budget.max_node_context_tokens, 777)
        self.assertEqual(policy.budget.max_control_prompt_tokens, 888)
        self.assertEqual(policy.budget.max_summary_chars, 99)
        self.assertEqual(policy.llmlingua_model, "example/test-model")
        self.assertTrue(policy.llmlingua_allow_download)

    def test_mixed_language_token_estimate_is_deterministic_and_conservative(self):
        text = "结构化结果包含 1485 RMB, validated=true"
        self.assertEqual(estimate_tokens(text), estimate_tokens(text))
        self.assertGreaterEqual(estimate_tokens(text), len("结构化结果"))


class CompressorTests(unittest.TestCase):
    def setUp(self):
        self.budget = CommunicationBudget(
            max_message_bytes=120,
            max_message_tokens=30,
            max_summary_chars=40,
        )

    def test_deterministic_compressor_enforces_every_text_budget(self):
        original = ("这是一段需要压缩的中文说明 with English details. " * 20).strip()
        result = DeterministicCompressor().compress(original, self.budget)

        self.assertTrue(result.within_budget)
        self.assertTrue(result.compressed)
        self.assertLessEqual(utf8_size(result.text), self.budget.max_message_bytes)
        self.assertLessEqual(estimate_tokens(result.text), self.budget.max_message_tokens)
        self.assertLessEqual(len(result.text), self.budget.max_summary_chars)

    def test_llmlingua_load_failure_stably_falls_back(self):
        factory = BrokenBackendFactory()
        compressor = LLMLinguaCompressor(backend_factory=factory)
        result = compressor.compress("长文本" * 100, self.budget)
        second = compressor.compress("另一段长文本" * 100, self.budget)

        self.assertTrue(result.fallback_used)
        self.assertTrue(result.within_budget)
        self.assertEqual(result.compressor, "deterministic")
        self.assertTrue(any("LLMLingua 回退" in item for item in result.warnings))
        self.assertTrue(second.fallback_used)
        self.assertEqual(factory.calls, 1)

    def test_llmlingua_output_is_guarded_by_deterministic_budget(self):
        compressor = LLMLinguaCompressor(backend_factory=VerboseFakeBackend)
        result = compressor.compress("内容" * 100, self.budget)

        self.assertEqual(result.compressor, "llmlingua")
        self.assertTrue(result.within_budget)
        self.assertLessEqual(utf8_size(result.text), self.budget.max_message_bytes)
        self.assertLessEqual(estimate_tokens(result.text), self.budget.max_message_tokens)

    def test_llmlingua_preserves_numeric_evidence_and_chinese_boundaries(self):
        backend = ContractFakeBackend()
        result = LLMLinguaCompressor(backend_factory=lambda: backend).compress(
            "节点1证据E1。" * 100,
            self.budget,
        )

        self.assertTrue(result.within_budget)
        self.assertTrue(backend.kwargs["force_reserve_digit"])
        self.assertIn("。", backend.kwargs["chunk_end_tokens"])

    def test_auto_compressor_semantically_compresses_text_that_still_fits_hard_budget(self):
        hard_budget = CommunicationBudget(
            max_message_bytes=4_000,
            max_message_tokens=1_000,
            max_summary_chars=2_000,
        )
        semantic = LLMLinguaCompressor(
            backend_factory=ShortFakeBackend,
            strict=True,
        )
        original = "长上下文事实与解释" * 100

        result = AutoCompressor(
            threshold_tokens=32,
            llmlingua_target_ratio=0.6,
            semantic=semantic,
        ).compress(original, hard_budget)

        self.assertEqual(result.compressor, "llmlingua")
        self.assertEqual(result.attempted_compressor, "llmlingua")
        self.assertTrue(result.compressed)
        self.assertTrue(result.within_budget)

    def test_auto_compressor_missing_backend_preserves_text_within_hard_budget(self):
        hard_budget = CommunicationBudget(
            max_message_bytes=4_000,
            max_message_tokens=1_000,
            max_summary_chars=2_000,
        )
        original = "仍应完整保留的上下文" * 40
        unavailable = LLMLinguaCompressor(
            backend_factory=BrokenBackendFactory(),
            strict=True,
        )

        result = AutoCompressor(
            threshold_tokens=32,
            semantic=unavailable,
        ).compress(original, hard_budget)

        self.assertEqual(result.text, original)
        self.assertTrue(result.fallback_used)
        self.assertEqual(result.attempted_compressor, "llmlingua")


class ContextBuilderTests(unittest.TestCase):
    def _graph(self):
        graph = TaskGraph(
            graph_id="communication-test",
            goal="verify sparse routing",
            nodes=[
                make_node("A"),
                make_node("B"),
                make_node("C", ["A"]),
            ],
        )
        graph.get_node("A").result = {
            "node_id": "A",
            "executor_id": "analysis-agent",
            "status": "completed",
            "summary": "只向直接下游发送这个结论",
            "structured_output": {"amount": 1485, "approved": True},
            "output_artifacts": ["artifacts/result.json"],
            "evidence_refs": ["normalized_input.json#record-1"],
            "token_usage": {"prompt": 9999},
            "internal_trace": "不应进入通信消息",
        }
        graph.get_node("B").result = {
            "summary": "独立分支信息",
            "structured_output": {"should_not_be_seen": True},
        }
        return graph

    def test_only_direct_dependencies_and_allowlisted_fields_are_delivered(self):
        graph = self._graph()
        with tempfile.TemporaryDirectory() as directory:
            built = ContextBuilder({"compressor": "deterministic"}).build(
                graph, graph.get_node("C"), {}, directory,
            )

            self.assertEqual(set(built.dependency_results), {"A"})
            self.assertNotIn("B", built.dependency_results)
            delivered = built.dependency_results["A"]
            self.assertEqual(
                set(delivered),
                {"summary", "structured_output", "output_artifacts", "evidence_refs"},
            )
            self.assertEqual(delivered["structured_output"]["amount"], 1485)
            self.assertNotIn("internal_trace", json.dumps(delivered, ensure_ascii=False))
            self.assertLess(
                built.message_metrics[0].projected_bytes,
                built.message_metrics[0].raw_bytes,
            )
            self.assertTrue(built.aggregate_metrics.within_node_budget)

    def test_large_structured_json_is_persisted_and_referenced_without_corruption(self):
        graph = self._graph()
        large_structured = {"records": [{"id": i, "amount": i * 10} for i in range(200)]}
        graph.get_node("A").result["structured_output"] = large_structured
        policy = CommunicationPolicy.model_validate({
            "compressor": "deterministic",
            "max_inline_structured_bytes": 100,
            "max_message_bytes": 2_000,
            "max_message_tokens": 600,
        })

        with tempfile.TemporaryDirectory() as directory:
            built = ContextBuilder(policy).build(graph, graph.get_node("C"), {}, directory)
            message = built.messages[0]
            reference = message.structured_output["$ref"]
            persisted = json.loads((Path(directory) / reference).read_text(encoding="utf-8"))

            self.assertEqual(persisted, large_structured)
            self.assertEqual(message.structured_output["media_type"], "application/json")
            self.assertEqual(
                message.structured_output["preview"]["fields"]["/records/0/amount"],
                0,
            )
            self.assertTrue(built.message_metrics[0].structured_payload_externalized)
            self.assertEqual(
                message.output_artifacts,
                graph.get_node("A").result["output_artifacts"],
            )
            self.assertEqual(
                message.evidence_refs,
                graph.get_node("A").result["evidence_refs"],
            )

    def test_large_evidence_catalog_is_bounded_inline_and_preserved_by_reference(self):
        graph = self._graph()
        complete_refs = [f"inputs/{index:08d}.json" for index in range(600)]
        graph.get_node("A").result["evidence_refs"] = complete_refs
        policy = CommunicationPolicy.model_validate({
            "compressor": "deterministic",
            "max_message_bytes": 2_000,
            "max_message_tokens": 600,
        })

        with tempfile.TemporaryDirectory() as directory:
            built = ContextBuilder(policy).build(
                graph, graph.get_node("C"), {}, directory,
            )
            message = built.messages[0]
            resolved = resolve_content_reference(
                directory, message.structured_output,
            )

            self.assertTrue(message.reference_only)
            self.assertEqual(len(message.evidence_refs), 16)
            self.assertEqual(
                message.structured_output["omitted_evidence_ref_count"], 584,
            )
            self.assertEqual(resolved["evidence_refs"], complete_refs)
            self.assertTrue(built.aggregate_metrics.within_node_budget)

    def test_exact_duplicate_is_replaced_by_content_addressed_reference(self):
        graph = self._graph()
        builder = ContextBuilder({"compressor": "deterministic"})

        with tempfile.TemporaryDirectory() as directory:
            first = builder.build(graph, graph.get_node("C"), {}, directory)
            second = builder.build(graph, graph.get_node("C"), {}, directory)
            duplicate = second.messages[0]

            self.assertFalse(first.messages[0].reference_only)
            self.assertTrue(duplicate.reference_only)
            self.assertTrue(second.message_metrics[0].exact_duplicate)
            self.assertEqual(second.aggregate_metrics.redundancy_rate, 1.0)
            self.assertTrue(duplicate.summary)
            self.assertEqual(
                duplicate.structured_output["preview"]["fields"]["/amount"],
                1485,
            )
            self.assertEqual(
                duplicate.output_artifacts,
                first.messages[0].output_artifacts,
            )
            self.assertEqual(duplicate.evidence_refs, first.messages[0].evidence_refs)
            self.assertTrue((Path(directory) / duplicate.payload_ref).is_file())

    def test_complete_payload_to_new_receiver_uses_verified_run_reference(self):
        graph = self._graph()
        graph.nodes.append(make_node("D", ["A"]))
        graph.validate_graph()
        builder = ContextBuilder({"compressor": "deterministic"})

        with tempfile.TemporaryDirectory() as directory:
            first = builder.build(graph, graph.get_node("C"), {}, directory)
            other = builder.build(graph, graph.get_node("D"), {}, directory)

            self.assertFalse(first.messages[0].reference_only)
            self.assertTrue(other.message_metrics[0].exact_duplicate)
            self.assertEqual(other.message_metrics[0].duplicate_scope, "run")
            self.assertTrue(other.messages[0].reference_only)
            self.assertTrue(other.message_metrics[0].global_duplicate_observation)
            resolved = resolve_content_reference(directory, other.messages[0].structured_output)
            self.assertEqual(resolved["structured_output"]["amount"], 1485)

    def test_reference_hash_mismatch_is_rejected(self):
        graph = self._graph()
        builder = ContextBuilder({"compressor": "deterministic"})

        with tempfile.TemporaryDirectory() as directory:
            builder.build(graph, graph.get_node("C"), {}, directory)
            duplicate = builder.build(graph, graph.get_node("C"), {}, directory).messages[0]
            target = Path(directory) / duplicate.payload_ref
            target.write_text('{"tampered": true}', encoding="utf-8")

            with self.assertRaises(ContentReferenceError):
                resolve_content_reference(directory, duplicate.structured_output)

    def test_declared_dependency_fields_remain_inline_in_large_reference(self):
        graph = self._graph()
        graph.get_node("A").result["structured_output"] = {
            "records": [{"id": index, "amount": index * 10} for index in range(100)]
        }
        graph.nodes.append(TaskNode(
            node_id="D", description="consume one protected fact", capability="analysis",
            dependencies=["A"],
            dependency_fields={"A": ["/structured_output/records/42/amount"]},
            success_criteria=[{"type": "node_result"}],
        ))
        graph.validate_graph()
        builder = ContextBuilder({
            "compressor": "deterministic",
            "max_inline_structured_bytes": 100,
        })

        with tempfile.TemporaryDirectory() as directory:
            builder.build(graph, graph.get_node("C"), {}, directory)
            protected = builder.build(graph, graph.get_node("D"), {}, directory)
            reference = protected.messages[0].structured_output

            self.assertTrue(protected.messages[0].reference_only)
            self.assertEqual(
                reference["required_fields"]
                ["/structured_output/records/42/amount"],
                420,
            )

    def test_missing_planner_declared_field_is_audited_without_blocking(self):
        graph = self._graph()
        graph.nodes.append(TaskNode(
            node_id="D",
            description="consume planner-declared field",
            capability="analysis",
            dependencies=["A"],
            dependency_fields={"A": ["/structured_output/not_actually_produced"]},
            success_criteria=[{"type": "node_result"}],
        ))
        graph.validate_graph()

        with tempfile.TemporaryDirectory() as directory:
            built = ContextBuilder({
                "compressor": "deterministic",
                "max_inline_structured_bytes": 1,
            }).build(graph, graph.get_node("D"), {}, directory)

            self.assertIn("A", built.dependency_results)
            self.assertTrue(
                built.message_metrics[0].structured_payload_externalized
            )
            self.assertTrue(
                any(
                    "缺少规划声明字段" in warning
                    for warning in built.message_metrics[0].compression_warnings
                )
            )
            resolved = resolve_content_reference(
                directory,
                built.messages[0].structured_output,
            )
            self.assertEqual(resolved["amount"], 1485)

    def test_builder_can_construct_existing_node_execution_context(self):
        graph = self._graph()
        with tempfile.TemporaryDirectory() as directory:
            context, built = ContextBuilder({"compressor": "deterministic"}).build_execution_context(
                graph,
                graph.get_node("C"),
                {"task_id": "test"},
                directory,
            )

            self.assertIsInstance(context, NodeExecutionContext)
            self.assertEqual(context.dependency_results, built.dependency_results)
            self.assertEqual(set(context.dependency_results), {"A"})

    def test_many_dependency_references_drop_redundant_previews_not_summaries(self):
        sources = [make_node(f"S{index:02d}") for index in range(15)]
        sink = make_node("VERIFY", [node.node_id for node in sources])
        graph = TaskGraph(
            graph_id="aggregate-reference-test",
            goal="fit a global verifier fan-in",
            nodes=[*sources, sink],
        )
        for index, node in enumerate(sources):
            node.result = {
                "summary": f"上游结论 {index} 已完成并保留责任边界",
                "structured_output": {
                    "records": [
                        {"id": item, "detail": "x" * 160}
                        for item in range(40)
                    ],
                },
                "output_artifacts": [f"artifacts/source-{index}.json"],
                "evidence_refs": ["normalized_input.json"],
            }

        with tempfile.TemporaryDirectory() as directory:
            built = ContextBuilder({
                "compressor": "deterministic",
                "max_message_bytes": 4_000,
                "max_message_tokens": 900,
                "max_node_context_bytes": 30_000,
                "max_node_context_tokens": 4_000,
            }).build(graph, sink, {}, directory)

        self.assertTrue(built.aggregate_metrics.within_node_budget)
        self.assertEqual(len(built.messages), 15)
        self.assertTrue(all(message.summary for message in built.messages))
        collapsed = [
            message for message in built.messages
            if message.reference_only
            and message.structured_output.get("preview", {}).get("fields") == {}
        ]
        self.assertTrue(collapsed)


if __name__ == "__main__":
    unittest.main()
