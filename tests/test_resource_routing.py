import unittest
from unittest.mock import patch

from orchestration.execution.capability_registry import CapabilityRegistry, CapabilityNotFoundError
from orchestration.execution.node_executor import ExecutorDescriptor
from orchestration.execution.resource_registry import default_resource_registry
from orchestration.graph.task_graph import TaskNode
from config import model_route_config


class _Executor:
    def __init__(self, executor_id, location, security=("internal",), quality=.8):
        self.descriptor = ExecutorDescriptor(
            executor_id=executor_id, executor_type="test", capabilities=["reasoning"],
            resource_location=location, security_levels=list(security), quality_score=quality,
        )
    async def execute(self, context):  # pragma: no cover
        raise NotImplementedError


class ResourceRoutingTests(unittest.TestCase):
    def setUp(self):
        self.registry = CapabilityRegistry(resource_registry=default_resource_registry())
        self.registry.register(_Executor("device-reasoner", "local", ("internal", "confidential"), .7))
        self.registry.register(_Executor("cloud-reasoner", "cloud", ("internal",), 1.0))

    def test_confidential_task_filters_cloud(self):
        node = TaskNode(node_id="secure", description="secure", capability="reasoning", success_criteria=[{"type": "node_result"}], resource_requirements={"data_sensitivity": "confidential"})
        selected, decision = self.registry.select_executor(node)
        self.assertEqual(selected.descriptor.executor_id, "device-reasoner")
        self.assertEqual(decision.resource_location, "local")

    def test_allowed_location_is_hard_filter(self):
        node = TaskNode(node_id="edge_task", description="edge", capability="reasoning", success_criteria=[{"type": "node_result"}], resource_requirements={"allowed_locations": ["edge"]})
        with self.assertRaises(CapabilityNotFoundError):
            self.registry.select_executor(node)

    def test_default_registry_supports_resource_failover_state(self):
        resources = default_resource_registry()
        resources.set_available("cloud-qwen", False)
        self.assertFalse(resources.get("cloud-qwen").can_accept())
        resources.set_available("cloud-qwen", True)
        self.assertTrue(resources.get("cloud-qwen").can_accept())

    def test_model_routes_are_bound_to_resources_without_exposing_keys(self):
        with patch.dict("os.environ", {
            "MODEL_BASE_URL": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "MODEL_NAME": "qwen3.5-35b-a3b",
            "DEVICE_MODEL": "qwen3.5-flash",
            "EDGE_MODEL": "qwen3.5-flash",
            "CLOUD_MODEL": "qwen3.5-35b-a3b",
        }, clear=False):
            resources = default_resource_registry()
            self.assertEqual(resources.get("device-local").model_name, "qwen3.5-flash")
            self.assertEqual(resources.get("cloud-qwen").model_name, "qwen3.5-35b-a3b")
            registry = CapabilityRegistry(resource_registry=resources)
            registry.register(_Executor("cloud-route-reasoner", "cloud", ("internal",), 1.0))
            node = TaskNode(node_id="reason", description="reason", capability="reasoning",
                            success_criteria=[{"type": "node_result"}],
                            resource_requirements={"allowed_locations": ["cloud"]})
            selected, decision = registry.select_executor(node)
            self.assertEqual(decision.model_name, "qwen3.5-35b-a3b")

    def test_model_route_config_defaults_to_flash_and_cloud_models(self):
        with patch.dict("os.environ", {}, clear=True):
            routes = model_route_config()
        self.assertEqual(routes["device"]["model"], "qwen3.5-flash")
        self.assertEqual(routes["edge"]["model"], "qwen3.5-flash")
        self.assertEqual(routes["cloud"]["model"], "qwen3.5-35b-a3b")


if __name__ == "__main__":
    unittest.main()
