import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from thunder_os.agents import AgentJob
from thunder_os.hermes_text_adapter import HermesTextAdapter, text_deadline


class TextBudgetTest(unittest.TestCase):
    def test_research_has_finite_deadline_without_expanding_copy(self):
        self.assertEqual(text_deadline('research'), 240)
        self.assertEqual(text_deadline('copy'), 120)

    def test_explicit_reasoning_keeps_model_tools_and_retry_bounds(self):
        create = Mock(return_value=iter([
            {"type": "response.output_text.delta", "delta": '{"copy":"draft"}'},
            {"type": "response.completed", "response": {"status": "completed", "output": []}},
        ]))
        client = SimpleNamespace(responses=SimpleNamespace(create=create), close=Mock())
        factory = Mock(return_value=client)
        adapter = HermesTextAdapter(
            config_loader=lambda: {"model": {"provider": "openai-codex", "default": "configured-model"}},
            runtime_loader=lambda **_: {"provider": "openai-codex", "api_mode": "codex_responses",
                                       "api_key": "fixture", "base_url": "https://fixture.invalid"},
            client_factory=factory,
            context_loader=lambda _: {"catalog_hash": "hash", "not_a_send_authorization": True,
                                      "restrictions": ["no effects"], "capabilities": ["copy"]},
        )
        adapter(AgentJob("bounded", "copy", {"catalog_hash": "hash"}), 120)
        create.assert_called_once()
        request = create.call_args.kwargs
        self.assertEqual(request["reasoning"], {"effort": "low"})
        self.assertEqual(request["model"], "configured-model")
        self.assertEqual(request["tools"], [])
        self.assertFalse(request["store"])
        self.assertEqual(request["timeout"], 120)
        self.assertEqual(factory.call_args.kwargs["max_retries"], 0)
        client.close.assert_called_once()
