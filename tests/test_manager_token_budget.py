"""Manager-facing context budget: compact tool views, short guidance, and a
blocking wait that replaces model-driven polling. No network or service."""

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from agent_captain.core.management_policy import MANAGER_ROLE, MCP_INSTRUCTIONS
from agent_captain.integrations import mcp_bridge


def size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


AGENT = {
    "host": "spark", "thread_id": "ceohub-1", "alias": "gb10-agent", "active": True, "mode": "terminal",
    "terminal_id": "ceohub-1", "cwd": "/home/u/exp/nvidia-gb10", "pinned": False, "management_policy": "connected_workers",
    "host_label": "Spark · DGX", "agent_id": "spark::ceohub-1", "source": "terminal",
    "output_binding": {"state": "recorded", "session_id": "01a1-session", "method": None, "note": "上次关联；读取时重新核验当前进程。"},
    "source_thread_id": None, "connected": True, "status": "agent", "state": "agent", "command": "node", "agent_ready": True,
    "human_busy": False, "blocked_reason": None, "human_input": {"state": "idle", "reason": None},
    "recovery": {"code": "reconnected", "message": "已重新附着原终端并核验相同 tmux/pane；未重启 agent、未输入任务。",
                 "next_action": "open_terminal", "request_key": "auto-reconnect-1", "request_origin": "ui"},
    "auto_reconnect": {"enabled": True, "interval_seconds": 600, "status": "connected", "next_attempt_at": None,
                       "last_attempt_at": 1794496013.6, "last_result": {"code": "reconnected", "message": "已自动接回原终端。"}},
    "management": {"status": "ready", "label": "Agent 可交互", "tone": "positive", "primary_action": "open_terminal", "needs_attention": False},
}
SCREEN = "\n".join(f"line {index}" for index in range(140))


def rollout_read(messages, status):
    return {**AGENT, "cwd": "/.__pronhub_existing__/ceohub-1/eyJtYXJrZXIiOiB0cnVlfQ", "screen": SCREEN,
            "ansi": "\u001b[38;5;151m" + SCREEN, "display_identity": {"birth": "x", "executable": "ssh.exe"},
            "id": "01a1-session", "source": "codex_rollout", "messages": messages,
            "recent_signals": [{"time": "t", "type": "tool_output", "text": "x" * 2500, "text_complete": False}] * 12,
            "conversation_status": status, "status": status, "content_complete": True, "history_complete": False,
            "next_cursor": None, "after_cursor": "after-1", "has_more": False, "read_issues": {"invalid_lines": 0},
            "note": "Selected-session persisted output, not terminal pixels.",
            "read_transport": {"kind": "ssh", "attempts": 1, "elapsed_ms": 900}}


def message(role, text, time="2026-11-16T03:00:00Z", complete=True):
    return {"role": role, "time": time, "channel": "final", "text": text, "message_id": "m-" + str(hash(text)),
            "byte_offset": 1, "chunk_offset": 0, "total_chars": len(text) * (1 if complete else 3),
            "content_complete": complete, "has_more_content": not complete}


class GuidanceBudgetTests(unittest.TestCase):
    def test_guidance_and_tool_descriptions_stay_small(self):
        self.assertLess(len(MCP_INSTRUCTIONS), 2600)
        self.assertLess(len(MANAGER_ROLE), 3400)
        self.assertLess(sum(len(item["description"]) for item in mcp_bridge.TOOLS), 3600)
        skill = (ROOT / "plugins" / "agent-captain" / "skills" / "agent-captain" / "SKILL.md").read_text(encoding="utf-8")
        self.assertLess(len(skill), 2200)

    def test_guidance_asks_for_english_user_voice_briefs_without_manager_vocabulary(self):
        self.assertIn("Write in English, in the first person", MCP_INSTRUCTIONS)
        self.assertIn("never mention a manager or captain", MCP_INSTRUCTIONS)
        self.assertIn("wait_for_agent", MCP_INSTRUCTIONS)


class CompactViewTests(unittest.TestCase):
    def test_overview_is_one_small_row_per_agent(self):
        payload = {"agents": [AGENT] * 6, "manager_role_policy": {"version": "x"}, "scope_note": "长说明" * 20,
                   "connected_hosts": ["spark"]}
        compact = mcp_bridge.compact_result("list_active_agents", payload)
        self.assertEqual(compact["agents"][0], {"agent_id": "spark::ceohub-1", "name": "gb10-agent", "host": "Spark · DGX",
                                                "cwd": "/home/u/exp/nvidia-gb10", "status": "ready", "ready": True,
                                                "auto_reconnect": "connected"})
        self.assertLess(size(compact), size(payload) * 0.25)

    def test_attention_is_kept_for_disconnected_workers(self):
        down = {**AGENT, "agent_ready": False, "management": {"status": "disconnected", "needs_attention": True},
                "recovery": {"code": "route_unavailable", "message": "SSH 路由暂不可达", "next_action": "check_connection",
                             "request_key": "k-1", "retryable": True}}
        row = mcp_bridge.compact_agent(down, live=True)
        self.assertEqual(row["attention"], "SSH 路由暂不可达")
        self.assertEqual(row["recovery"], {"code": "route_unavailable", "next_action": "check_connection", "request_key": "k-1", "retryable": True})
        self.assertEqual(row["terminal_id"], "ceohub-1")

    def test_read_drops_terminal_bulk_and_keeps_messages_and_cursors(self):
        payload = rollout_read([message("user", "Please check the D256 source."), message("assistant", "Done: commit abc.")],
                               "completed_in_history")
        compact = mcp_bridge.compact_result("read_agent", payload)
        for dropped in ("ansi", "screen", "recent_signals", "display_identity", "read_transport", "note", "cwd"):
            self.assertNotIn(dropped, compact)
        self.assertEqual(compact["messages"][1], {"role": "assistant", "time": "2026-11-16T03:00:00Z", "text": "Done: commit abc."})
        self.assertEqual((compact["session_id"], compact["status"], compact["after_cursor"]), ("01a1-session", "completed_in_history", "after-1"))
        self.assertNotIn("read_issues", compact)  # all-zero issue counters carry no information
        self.assertLess(size(compact), size(payload) * 0.05)

    def test_split_message_is_marked_and_cursor_kept(self):
        payload = rollout_read([message("assistant", "part one", complete=False)], "completed_in_history")
        payload.update(content_complete=False, next_cursor="next-1")
        compact = mcp_bridge.compact_read(payload)
        self.assertEqual(compact["messages"][0]["chunk"], "0-8 of 24 chars")
        self.assertEqual((compact["content_complete"], compact["next_cursor"]), (False, "next-1"))

    def test_unbound_read_returns_screen_tail_and_reason(self):
        payload = {**AGENT, "screen": SCREEN, "ansi": "raw", "messages": [], "history_complete": False,
                   "output_binding": {"state": "unavailable", "reason": "SSH 握手失败"}}
        compact = mcp_bridge.compact_read(payload)
        self.assertEqual(compact["unavailable_reason"], "SSH 握手失败")
        self.assertEqual(compact["screen_tail"].splitlines()[-1], "line 139")
        self.assertEqual(len(compact["screen_tail"].splitlines()), mcp_bridge.SCREEN_TAIL_LINES)

    def test_delivery_never_echoes_the_sent_prompt(self):
        record = {"id": "r-1", "request_id": "r-1", "host": "spark", "thread_id": "ceohub-1", "text": "long brief " * 300,
                  "state": "submitted", "delivery": "submitted", "origin": "plugin", "submitted_at": 1.0, "error": None}
        self.assertEqual(mcp_bridge.compact_result("send_agent_message", record),
                         {"request_id": "r-1", "delivery": "submitted", "agent_id": "spark::ceohub-1", "submitted_at": 1.0})

    def test_events_summarize_terminal_output(self):
        events = [{"id": index, "kind": "terminal_output", "time": 1.0,
                   "data": {"host": "spark", "thread_id": "ceohub-1", "terminal_id": "ceohub-1", "current_seq": index}} for index in range(40)]
        events.append({"id": 41, "kind": "terminal_state", "time": 2.0,
                       "data": {"host": "spark", "thread_id": "ceohub-1", "connected": False, "state": "exited", "command": ""}})
        compact = mcp_bridge.compact_result("poll_events", {"events": events, "cursor": 41})
        self.assertEqual(compact, {"events": [{"id": 41, "kind": "terminal_state", "time": 2.0, "agent_id": "spark::ceohub-1",
                                               "state": "exited", "connected": False, "command": ""}],
                                   "output_activity": {"spark::ceohub-1": 40}, "cursor": 41})

    def test_verbose_returns_full_record_without_raw_ansi(self):
        payload = {**AGENT, "screen": "s", "ansi": "raw"}
        full = mcp_bridge.compact_result("get_agent", payload, verbose=True)
        self.assertNotIn("ansi", full)
        self.assertEqual(full["recovery"], AGENT["recovery"])

    def test_empty_screen_is_recovered_from_the_serialized_buffer(self):
        # PowerShell left in a ">>" continuation draft: rendered screen empty,
        # serialized buffer still holds the lines.
        ansi = ("\u001b[?25l\u001b[0mPS D:\\ceo_hub> \u001b[93m&\u001b[0m python -u .runtime\\live_wait_test.py\r\n"
                "\u001b[0m>>\u001b[3C\r\n>>\u001b[1B\u001b]0;Windows PowerShell\u0007\u001b[?25h")
        payload = {**AGENT, "command": "powershell.exe", "screen": "", "ansi": ansi}
        compact = mcp_bridge.compact_result("get_agent", payload)
        self.assertEqual(compact["screen_source"], "ansi_text")
        self.assertEqual(compact["screen_tail"], "PS D:\\ceo_hub> & python -u .runtime\\live_wait_test.py\n>>\n>>")
        self.assertNotIn("ansi", compact)
        verbose = mcp_bridge.compact_result("get_agent", payload, verbose=True)
        self.assertEqual(verbose["screen_source"], "ansi_text")
        self.assertNotIn("ansi", verbose)

    def test_rendered_screen_is_never_replaced(self):
        payload = {**AGENT, "screen": "real screen", "ansi": "\u001b[0mother"}
        self.assertEqual(mcp_bridge.compact_result("get_agent", payload)["screen_tail"], "real screen")
        self.assertNotIn("screen_source", mcp_bridge.compact_result("get_agent", payload))
        blank = {**AGENT, "screen": "", "ansi": "\u001b[?1049h\u001b[H\u001b[2J"}
        self.assertNotIn("screen_tail", mcp_bridge.compact_result("get_agent", blank))

    def test_terminal_line_breaks_are_sent_as_enter(self):
        with patch.object(mcp_bridge, "call_service", return_value={"request_id": "r", "delivery": "submitted"}) as call:
            mcp_bridge.dispatch("send_terminal_input", {"agent_id": "local::t", "data": "python x.py\n", "request_key": "k"})
            mcp_bridge.dispatch("send_terminal_input", {"agent_id": "local::t", "data": "a\r\nb\r", "request_key": "k2"})
            mcp_bridge.dispatch("send_terminal_input", {"agent_id": "local::t", "data": "\u0003", "request_key": "k3"})
        self.assertEqual([c.args[1]["data"] for c in call.call_args_list], ["python x.py\r", "a\rb\r", "\u0003"])

    def test_dispatch_defaults_read_limit_and_strips_verbose(self):
        with patch.object(mcp_bridge, "call_service", return_value=rollout_read([], "completed_in_history")) as call:
            mcp_bridge.dispatch("read_agent", {"agent_id": "spark::ceohub-1", "verbose": False})
        call.assert_called_once_with("read_agent", {"agent_id": "spark::ceohub-1", "limit": mcp_bridge.DEFAULT_READ_LIMIT})


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class WaitForAgentTests(unittest.TestCase):
    BRIEF = "Can you check where the FA forward D256 result came from?\nRecords only."

    def setUp(self):
        mcp_bridge._SEEN_WORKING.clear()

    def run_wait(self, responses, arguments):
        clock = FakeClock()
        calls = []

        def fake(name, args):
            calls.append((name, args))
            item = responses[name].pop(0) if len(responses[name]) > 1 else responses[name][0]
            if isinstance(item, Exception):
                raise item
            return item

        with patch.object(mcp_bridge, "call_service", side_effect=fake):
            result = mcp_bridge.wait_for_agent(arguments, sleep=clock.sleep, clock=clock)
        return result, calls

    def delivery(self, state="submitted"):
        return {"request_id": "r-1", "host": "spark", "thread_id": "ceohub-1", "state": state, "delivery": state,
                "text": self.BRIEF, "submitted_at": 1794900000.0}

    def test_returns_final_reply_after_the_matching_turn_completes(self):
        old = [message("user", "Earlier task"), message("assistant", "Earlier result")]
        mine = message("user", self.BRIEF)
        responses = {"get_delivery": [self.delivery()],
                     "read_agent": [rollout_read(old, "completed_in_history"),
                                    rollout_read(old + [mine], "working_in_history"),
                                    rollout_read(old + [mine, message("assistant", "Looking at the archive."),
                                                        message("assistant", "Found it: fa_nine256.py, no CUDA run.")], "completed_in_history")]}
        result, calls = self.run_wait(responses, {"agent_id": "spark::ceohub-1", "request_id": "r-1", "timeout_seconds": 300})
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["reply"], "Found it: fa_nine256.py, no CUDA run.")
        self.assertEqual(result["earlier_assistant_messages"], 1)
        self.assertEqual(result["waited_seconds"], 2 * mcp_bridge.WAIT_INTERVAL_SECONDS)
        self.assertEqual([name for name, _ in calls].count("get_delivery"), 1)
        self.assertTrue(all(args.get("limit") == mcp_bridge.WAIT_READ_LIMIT for name, args in calls if name == "read_agent"))

    def test_previous_completed_turn_is_not_mistaken_for_the_new_one(self):
        old = [message("user", "Earlier task", time="2026-01-01T00:00:00Z"), message("assistant", "Earlier result")]
        responses = {"get_delivery": [self.delivery()], "read_agent": [rollout_read(old, "completed_in_history")]}
        result, _ = self.run_wait(responses, {"agent_id": "spark::ceohub-1", "request_id": "r-1", "timeout_seconds": 60})
        self.assertEqual(result["state"], "timed_out")
        self.assertEqual(result["delivery"], "submitted")
        self.assertIn("again", result["hint"])

    def test_waits_through_pending_delivery_and_reports_failed_delivery(self):
        responses = {"get_delivery": [self.delivery("human_busy"), self.delivery("not_sent")], "read_agent": [{}]}
        result, calls = self.run_wait(responses, {"agent_id": "spark::ceohub-1", "request_id": "r-1"})
        self.assertEqual((result["state"], result["delivery"]), ("not_submitted", "not_sent"))
        self.assertNotIn("read_agent", [name for name, _ in calls])

    def test_request_for_another_agent_is_rejected(self):
        responses = {"get_delivery": [{**self.delivery(), "thread_id": "other"}], "read_agent": [{}]}
        result, calls = self.run_wait(responses, {"agent_id": "spark::ceohub-1", "request_id": "r-1"})
        self.assertEqual(result["state"], "invalid_request")
        self.assertIn("different agent", result["error"])
        self.assertEqual(len(calls), 1)

    def test_without_request_id_an_idle_worker_returns_immediately(self):
        responses = {"read_agent": [rollout_read([message("assistant", "Idle reply")], "completed_in_history")]}
        result, calls = self.run_wait(responses, {"agent_id": "spark::ceohub-1"})
        self.assertEqual((result["state"], result["reply"], result["waited_seconds"]), ("completed", "Idle reply", 0))
        self.assertEqual(len(calls), 1)

    def test_transient_read_errors_keep_waiting(self):
        mine = message("user", self.BRIEF)
        responses = {"get_delivery": [self.delivery()],
                     "read_agent": [mcp_bridge.BridgeError("ssh_read_in_progress"),
                                    rollout_read([mine, message("assistant", "ok")], "interrupted_in_history")]}
        result, _ = self.run_wait(responses, {"agent_id": "spark::ceohub-1", "request_id": "r-1"})
        self.assertEqual((result["state"], result["reply"]), ("interrupted", "ok"))

    def screen_read(self, screen, ready=True):
        return {**AGENT, "source": "terminal", "messages": [], "agent_ready": ready, "screen": screen,
                "output_binding": {"state": "unbound", "reason": "no_exact_session_evidence"}}

    def test_unlinked_session_falls_back_to_the_screen(self):
        old = "  Worked for 6s\n\n› Ask Codex to do anything\n\n  model · ~/workspace"
        busy = "› Can you check where the FA forward D256 result came from?\n\n• Working (5s • esc to interrupt)"
        done = ("› Can you check where the FA forward D256 result came from?\n  Records only.\n\n"
                "• It came from fa_nine256.py; no CUDA run exists.\n\n  Worked for 40s\n\n"
                "› Ask Codex to do anything\n\n  model · ~/workspace")
        mcp_bridge._SEEN_WORKING.clear()
        responses = {"get_delivery": [self.delivery()],
                     "read_agent": [self.screen_read(old), self.screen_read(busy), self.screen_read(done)]}
        result, _ = self.run_wait(responses, {"agent_id": "spark::ceohub-1", "request_id": "r-1"})
        self.assertEqual((result["state"], result["reply_source"], result["reply_complete"]), ("completed", "screen", False))
        self.assertEqual(result["reply"], "Records only.\n\n• It came from fa_nine256.py; no CUDA run exists.\n\n  Worked for 40s")

    def test_previous_screen_turn_is_not_mistaken_for_the_new_one(self):
        mcp_bridge._SEEN_WORKING.clear()
        old = "  Worked for 6s\n\n› Ask Codex to do anything"
        responses = {"get_delivery": [self.delivery()], "read_agent": [self.screen_read(old)]}
        result, _ = self.run_wait(responses, {"agent_id": "spark::ceohub-1", "request_id": "r-1", "timeout_seconds": 30})
        self.assertEqual((result["state"], result["status"]), ("timed_out", "screen_only"))

    def test_progress_seen_in_an_earlier_call_is_remembered(self):
        mcp_bridge._SEEN_WORKING.clear()
        busy = self.screen_read("• Working (5s • esc to interrupt)")
        first, _ = self.run_wait({"get_delivery": [self.delivery()], "read_agent": [busy]},
                                 {"agent_id": "spark::ceohub-1", "request_id": "r-1", "timeout_seconds": 20})
        self.assertEqual(first["state"], "timed_out")
        scrolled = self.screen_read("  result table\n  Worked for 9m\n\n› Ask Codex to do anything")
        second, _ = self.run_wait({"get_delivery": [self.delivery()], "read_agent": [scrolled]},
                                  {"agent_id": "spark::ceohub-1", "request_id": "r-1"})
        self.assertEqual(second["state"], "completed")
        self.assertNotIn("r-1", mcp_bridge._SEEN_WORKING)

    def test_one_call_never_exceeds_the_client_tool_timeout(self):
        mcp_bridge._SEEN_WORKING.clear()
        busy = self.screen_read("• Working (5s • esc to interrupt)")
        result, _ = self.run_wait({"get_delivery": [self.delivery()], "read_agent": [busy]},
                                  {"agent_id": "spark::ceohub-1", "request_id": "r-1", "timeout_seconds": 900})
        self.assertEqual(result["waited_seconds"], mcp_bridge.WAIT_MAX_SECONDS)
        self.assertLess(mcp_bridge.WAIT_MAX_SECONDS, 180)

    def test_wait_never_sends_input(self):
        self.assertTrue(mcp_bridge.TOOLS_BY_NAME["wait_for_agent"]["annotations"]["readOnlyHint"])
        responses = {"read_agent": [rollout_read([], "completed_in_history")]}
        _, calls = self.run_wait(responses, {"agent_id": "spark::ceohub-1"})
        self.assertEqual({name for name, _ in calls}, {"read_agent"})


if __name__ == "__main__":
    unittest.main()
