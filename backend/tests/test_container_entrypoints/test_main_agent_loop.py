"""Unit tests for main-agent decision loop."""
from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from bridle.container_entrypoints.decider import DeepSeekDecider
from bridle.container_entrypoints.main_agent_loop import Decision, MainAgentLoop


class TestMainAgentLoop:
    def test_select_node_on_user_message(self) -> None:
        client = MagicMock()
        client.get_session.return_value = {"status": "active"}
        client.poll_messages.return_value = [{"role": "user", "content": "go", "created_at": "2026-01-01T00:00:01Z"}]
        client.get_eligible_snapshot.return_value = {
            "eligible_nodes": [
                {"node_id": "n1", "plan_node_id": "n1", "status": "ready", "title": "x"},
            ],
            "blocked_nodes": [],
        }
        client.current_plan.return_value = {"nodes": []}
        decider = MagicMock()
        decider.decide.return_value = Decision(action="select_node", node_id="n1", reply="", reason="ready")

        loop = MainAgentLoop(client, decider, session_id="s1", plan_id="p1", poll_interval_seconds=0)
        loop._idle_tick = lambda: False  # type: ignore[method-assign]
        code = loop.run_once()

        assert code == 0
        client.select_node.assert_called_once_with("s1", "n1")
        client.post_assistant.assert_called()

    def test_reply_without_node(self) -> None:
        client = MagicMock()
        client.get_session.return_value = {"status": "active"}
        client.poll_messages.return_value = [{"role": "user", "content": "hi", "created_at": "2026-01-01T00:00:02Z"}]
        client.get_eligible_snapshot.return_value = {
            "eligible_nodes": [],
            "blocked_nodes": [],
        }
        client.current_plan.return_value = {"nodes": []}
        decider = MagicMock()
        decider.decide.return_value = Decision(action="reply", reply="hello", reason="chat")

        loop = MainAgentLoop(client, decider, session_id="s1", plan_id="p1", poll_interval_seconds=0)
        loop._idle_tick = lambda: False  # type: ignore[method-assign]
        loop.run_once()

        client.select_node.assert_not_called()
        client.post_assistant.assert_called_once()

    def test_exits_when_session_cancelled(self) -> None:
        client = MagicMock()
        client.get_session.return_value = {"status": "cancelled"}
        decider = MagicMock()

        loop = MainAgentLoop(client, decider, session_id="s1", plan_id="p1", poll_interval_seconds=0)
        assert loop.run_once() == 0
        decider.decide.assert_not_called()

    def test_exits_after_repeated_5xx(self) -> None:
        client = MagicMock()
        client.get_session.side_effect = RuntimeError("http_500")
        decider = MagicMock()

        loop = MainAgentLoop(client, decider, session_id="s1", plan_id="p1", poll_interval_seconds=0)
        loop._max_http_failures = 5  # type: ignore[attr-defined]
        assert loop.run_forever(max_iterations=10) == 2

    def test_survives_transport_error(self) -> None:
        client = MagicMock()
        client.get_session.side_effect = httpx.ConnectError("Network is unreachable")
        decider = MagicMock()

        loop = MainAgentLoop(client, decider, session_id="s1", plan_id="p1", poll_interval_seconds=0)
        exit_code = loop.run_forever(max_iterations=2)
        assert exit_code == 0
        assert loop._http_failures > 0

    def test_exits_after_max_transport_failures(self) -> None:
        client = MagicMock()
        client.get_session.side_effect = httpx.ConnectError("Network is unreachable")
        decider = MagicMock()

        loop = MainAgentLoop(client, decider, session_id="s1", plan_id="p1", poll_interval_seconds=0)
        assert loop.run_forever(max_iterations=10) == 2
        assert loop._http_failures >= loop._max_http_failures

    def test_recovers_after_transient_transport_error(self) -> None:
        client = MagicMock()
        client.get_session.side_effect = [
            httpx.ConnectError("Network is unreachable"),
            {"status": "active"},
        ]
        client.poll_messages.return_value = []
        decider = MagicMock()

        loop = MainAgentLoop(client, decider, session_id="s1", plan_id="p1", poll_interval_seconds=0)
        exit_code = loop.run_forever(max_iterations=2)
        assert exit_code == 0
        assert loop._http_failures == 0

    def test_survives_4xx_business_conflict(self) -> None:
        """4xx 业务冲突（如 409 node_not_eligible）不应杀掉 main-agent 进程。

        必须：吞掉异常、调 post_assistant 上报、下一轮继续轮询。
        """
        client = MagicMock()
        client.get_session.return_value = {"status": "active"}
        client.poll_messages.return_value = [
            {"role": "user", "content": "go", "created_at": "2026-01-01T00:00:01Z"}
        ]
        client.get_eligible_snapshot.return_value = {
            "eligible_nodes": [
                {"node_id": "n1", "plan_node_id": "n1", "status": "ready", "title": "x"},
            ],
            "blocked_nodes": [],
        }
        client.current_plan.return_value = {"nodes": []}

        request = httpx.Request("POST", "http://x/select-node")
        response = httpx.Response(
            409,
            request=request,
            text='{"code":"node_not_eligible","details":{"node_id":"n1"}}',
        )
        client.select_node.side_effect = httpx.HTTPStatusError(
            "409 ...", request=request, response=response
        )

        decider = MagicMock()
        decider.decide.return_value = Decision(action="select_node", node_id="n1")

        loop = MainAgentLoop(client, decider, session_id="s1", plan_id="p1", poll_interval_seconds=0)
        loop._idle_tick = lambda: False  # type: ignore[method-assign]
        # 只跑一轮：第一轮 4xx 被吞 + post_assistant 报错；要求返回 0（不退出）。
        exit_code = loop.run_forever(max_iterations=1)
        assert exit_code == 0
        assert client.post_assistant.called
        reported = client.post_assistant.call_args.args[1]
        assert "409" in reported
        assert "node_not_eligible" in reported

    def test_runtime_negotiation_triggered_when_eligible_empty(self) -> None:
        client = MagicMock()
        decider = MagicMock()
        client.get_session.return_value = {"status": "active"}
        client.get_eligible_snapshot.return_value = {
            "eligible_nodes": [],
            "blocked_nodes": [
                {
                    "node_id": "db1",
                    "plan_node_id": "n1",
                    "reason": "node_too_complex",
                    "blocked_by": ["node_too_granular:estimated_minutes_too_low"],
                }
            ],
        }
        client.current_plan.return_value = {"nodes": []}
        client.poll_messages.return_value = [
            {"role": "user", "content": "go", "created_at": "2026-01-01T00:00:00"},
        ]
        decider.decide.return_value = Decision(action="reply", reply="ok", reason="")
        loop = MainAgentLoop(client, decider, session_id="s1", plan_id="p1")
        loop.run_once()
        client.negotiate_complexity.assert_called_once_with("p1")

    def test_runtime_negotiation_triggered_for_node_too_granular(self) -> None:
        client = MagicMock()
        decider = MagicMock()
        client.get_session.return_value = {"status": "active"}
        client.get_eligible_snapshot.return_value = {
            "eligible_nodes": [],
            "blocked_nodes": [
                {
                    "node_id": "db1",
                    "plan_node_id": "n1",
                    "reason": "node_too_granular",
                    "blocked_by": ["node_too_granular:estimated_minutes_too_low"],
                }
            ],
        }
        client.current_plan.return_value = {"nodes": []}
        client.poll_messages.return_value = [
            {"role": "user", "content": "go", "created_at": "2026-01-01T00:00:01"},
        ]
        decider.decide.return_value = Decision(action="reply", reply="ok", reason="")
        loop = MainAgentLoop(client, decider, session_id="s1", plan_id="p1")
        loop.run_once()
        client.negotiate_complexity.assert_called_once_with("p1")

    def test_runtime_negotiation_triggered_for_node_blocked(self) -> None:
        client = MagicMock()
        decider = MagicMock()
        client.get_session.return_value = {"status": "active"}
        client.get_eligible_snapshot.return_value = {
            "eligible_nodes": [],
            "blocked_nodes": [
                {
                    "node_id": "db1",
                    "plan_node_id": "n1",
                    "reason": "node_blocked",
                    "blocked_by": ["code_change node missing tests"],
                }
            ],
        }
        client.current_plan.return_value = {"nodes": []}
        client.poll_messages.return_value = [
            {"role": "user", "content": "go", "created_at": "2026-01-01T00:00:03Z"},
        ]
        decider.decide.return_value = Decision(action="reply", reply="ok", reason="")
        loop = MainAgentLoop(client, decider, session_id="s1", plan_id="p1")
        loop.run_once()
        client.negotiate_complexity.assert_called_once_with("p1")

    def test_runtime_negotiation_triggered_for_node_incomplete(self) -> None:
        client = MagicMock()
        decider = MagicMock()
        client.get_session.return_value = {"status": "active"}
        client.get_eligible_snapshot.return_value = {
            "eligible_nodes": [],
            "blocked_nodes": [
                {
                    "node_id": "db1",
                    "plan_node_id": "n1",
                    "reason": "node_incomplete",
                    "blocked_by": ["node_incomplete:missing_tests"],
                }
            ],
        }
        client.current_plan.return_value = {"nodes": []}
        client.poll_messages.return_value = [
            {"role": "user", "content": "go", "created_at": "2026-01-01T00:00:02"},
        ]
        decider.decide.return_value = Decision(action="reply", reply="ok", reason="")
        loop = MainAgentLoop(client, decider, session_id="s1", plan_id="p1")
        loop.run_once()
        client.negotiate_complexity.assert_called_once_with("p1")

    def test_runtime_negotiation_422_fails_session_and_reports(self) -> None:
        client = MagicMock()
        decider = MagicMock()
        client.get_session.return_value = {"status": "active"}
        client.get_eligible_snapshot.return_value = {
            "eligible_nodes": [],
            "blocked_nodes": [
                {
                    "node_id": "db1",
                    "plan_node_id": "n1",
                    "reason": "node_too_granular",
                    "blocked_by": [],
                }
            ],
        }
        client.current_plan.return_value = {"nodes": []}
        client.poll_messages.return_value = [
            {"role": "user", "content": "go", "created_at": "2026-01-01T00:00:02"},
        ]
        request = httpx.Request("POST", "http://x/negotiate")
        response = httpx.Response(422, request=request, text='{"code":"plan_not_executable"}')
        client.negotiate_complexity.side_effect = httpx.HTTPStatusError(
            "422", request=request, response=response
        )
        decider.decide.return_value = Decision(action="reply", reply="ok", reason="")
        loop = MainAgentLoop(client, decider, session_id="s1", plan_id="p1")
        loop.run_once()
        client.fail_session.assert_called_once()
        client.post_assistant.assert_called_once()
        assert client.post_assistant.call_count == 1


class TestDeepSeekDeciderParse:
    def _decider(self) -> DeepSeekDecider:
        return DeepSeekDecider(api_key="", model="x")

    def test_select_node_with_unknown_id_is_downgraded_to_reply(self) -> None:
        """LLM 返回的 node_id 不在 eligible 列表里时，必须降级为 reply，
        否则会扔给 backend 触发确定性 409 并杀死容器。"""
        d = self._decider()
        text = '{"action":"select_node","node_id":"bogus","reply":""}'
        eligible = [{"node_id": "n1"}, {"node_id": "n2"}]
        decision = d._parse_decision(text, eligible)
        assert decision.action == "reply"
        assert decision.reason == "select_node_not_in_eligible"

    def test_select_node_when_eligible_empty_is_downgraded_to_reply(self) -> None:
        d = self._decider()
        text = '{"action":"select_node","node_id":"n1"}'
        decision = d._parse_decision(text, eligible=[])
        assert decision.action == "reply"
        assert decision.reason == "select_node_not_in_eligible"

    def test_select_node_with_valid_id_passes(self) -> None:
        d = self._decider()
        text = '{"action":"select_node","node_id":"n1"}'
        decision = d._parse_decision(text, eligible=[{"node_id": "n1"}])
        assert decision.action == "select_node"
        assert decision.node_id == "n1"

    def test_select_node_without_id_picks_first_eligible(self) -> None:
        """保留原有行为：LLM 没给 node_id 但 eligible 非空时，挑第一个。"""
        d = self._decider()
        text = '{"action":"select_node","node_id":""}'
        decision = d._parse_decision(text, eligible=[{"node_id": "n1"}, {"node_id": "n2"}])
        assert decision.action == "select_node"
        assert decision.node_id == "n1"

    def test_select_node_bad_id_does_not_leak_llm_reply(self) -> None:
        d = self._decider()
        text = '{"action":"select_node","node_id":"node-001","reply":"开始执行节点 node-001"}'
        eligible = [{"node_id": "uuid1"}]
        decision = d._parse_decision(text, eligible)
        assert decision.action == "reply"
        assert "开始执行" not in decision.reply
        assert "不在可执行列表" in decision.reply
        assert decision.reason == "select_node_not_in_eligible"


class TestMainAgentDispatch:
    def _loop(self, client: MagicMock) -> MainAgentLoop:
        decider = MagicMock()
        return MainAgentLoop(client, decider, session_id="s1", plan_id="p1", poll_interval_seconds=0)

    def test_dispatch_empty_reply_writes_fallback(self) -> None:
        client = MagicMock()
        loop = self._loop(client)
        loop._dispatch(Decision(action="reply", reply="", reason="empty"))
        client.post_assistant.assert_called_once()
        assert "模型未返回内容" in client.post_assistant.call_args.args[1]

    def test_dispatch_wait_writes_indicator(self) -> None:
        client = MagicMock()
        loop = self._loop(client)
        loop._dispatch(Decision(action="wait", reason="no eligible"))
        client.post_assistant.assert_called_once()
        assert "等待中" in client.post_assistant.call_args.args[1]

    def test_dispatch_unknown_action_writes_warning(self) -> None:
        client = MagicMock()
        loop = self._loop(client)
        loop._dispatch(Decision(action="acknowledge", reason=""))
        client.post_assistant.assert_called_once()
        assert "未识别" in client.post_assistant.call_args.args[1]

    def test_decider_receives_failed_runs_context(self) -> None:
        client = MagicMock()
        client.get_session.return_value = {"status": "active"}
        client.poll_messages.return_value = [
            {"role": "user", "content": "retry?", "created_at": "2026-01-01T00:00:03Z"},
        ]
        client.get_eligible_snapshot.return_value = {
            "eligible_nodes": [{"node_id": "n1", "plan_node_id": "n1", "status": "ready", "title": "x"}],
            "blocked_nodes": [],
        }
        client.current_plan.return_value = {"nodes": []}
        failed = [
            {
                "run_id": "run-1",
                "node_id": "n1",
                "plan_node_id": "node-001",
                "title": "Roman",
                "status": "failed",
                "blocked_reason": "tests_failed",
                "result_summary": "Tests failed (exit=4): pytest test_roman.py",
                "result_type": "tests_failed",
                "recommended_next_action": "needs_test_files_or_fix",
                "finished_at": "2026-01-01T00:00:00Z",
            }
        ]
        client.get_recent_failed_runs.return_value = failed
        decider = MagicMock()
        decider.decide.return_value = Decision(action="reply", reply="will retry", reason="ack_fail")

        loop = MainAgentLoop(client, decider, session_id="s1", plan_id="p1", poll_interval_seconds=0)
        loop._idle_tick = lambda: False  # type: ignore[method-assign]
        loop.run_once()

        client.get_recent_failed_runs.assert_called_once_with("s1", 3)
        decider.decide.assert_called_once()
        _args, kwargs = decider.decide.call_args
        assert kwargs.get("failed_runs") == failed
