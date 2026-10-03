import json
import os
import subprocess
import sys
import tempfile
import time
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import agent_fallback  # noqa: E402


class AgentFallbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.fake = self.root / "fake_provider.py"
        self.fake.write_text(textwrap.dedent("""\
            import os, sys, time
            name = sys.argv[1]
            if os.environ.get("FAKE_EXPECT_PROMPT_ARG") == "1" and sys.argv[-1] != "prompt":
                print("prompt argument missing", file=sys.stderr)
                raise SystemExit(2)
            behavior = os.environ.get("FAKE_" + name.upper(), "ok")
            _ = sys.stdin.read()
            if behavior == "quota":
                print("quota marker", file=sys.stderr)
            elif behavior == "technical":
                print("Tests failed: expected result was not produced.")
            elif behavior == "empty":
                pass
            elif behavior == "slow":
                time.sleep(2)
                print("too late")
            else:
                print(name + " response")
        """), encoding="utf-8")
        self.config = {
            "timeout_seconds": 1,
            "log_path": str(self.root / "events.jsonl"),
            "providers": {
                name: {"enabled": True, "command": [sys.executable, str(self.fake), name],
                       "patterns": {"quota": ["quota marker"], "temporary": [], "auth": []}}
                for name in ("claude", "codex", "antigravity")
            },
        }

    def tearDown(self):
        os.environ.pop("FAKE_EXPECT_PROMPT_ARG", None)
        for name in ("CLAUDE", "CODEX", "ANTIGRAVITY"):
            os.environ.pop("FAKE_" + name, None)
        self.temp.cleanup()

    def test_role_selection_order(self):
        self.assertEqual(agent_fallback.ROLE_ORDER["thinker"], ("claude", "codex", "antigravity"))
        self.assertEqual(agent_fallback.ROLE_ORDER["coder"], ("codex", "claude", "antigravity"))
        self.assertEqual(agent_fallback.ROLE_ORDER["reviewer"], ("antigravity", "claude", "codex"))

    def test_agy_success_with_denied_action_is_not_an_answer(self):
        payload = json.dumps({"status": "SUCCESS", "response": "", "denied_actions": [{"action": "command"}]})
        kind, answer = agent_fallback.decode_response(0, payload, "", {}, "agy-json")
        self.assertEqual((kind, answer), ("permission_denied", ""))

    def test_codex_echoed_quota_and_negative_conclusion_are_valid(self):
        payload = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "Tests failed: quota reached fixture broken"}})
        kind, answer = agent_fallback.decode_response(0, payload, "prompt: quota reached", {"quota": ["quota reached"]}, "codex-jsonl")
        self.assertEqual(kind, "valid")
        self.assertIn("Tests failed", answer)

    def test_codex_structured_quota_with_zero_exit(self):
        payload = json.dumps({"type": "turn.failed", "error": {"message": "quota reached"}})
        kind, answer = agent_fallback.decode_response(0, payload, "", {"quota": ["quota reached"]}, "codex-jsonl")
        self.assertEqual((kind, answer), ("quota", ""))

    def test_claude_structured_error(self):
        payload = json.dumps({"is_error": True, "result": "usage limit reached"})
        kind, answer = agent_fallback.decode_response(0, payload, "", {"quota": ["usage limit reached"]}, "claude-json")
        self.assertEqual((kind, answer), ("quota", ""))

    def test_custom_role_order_and_disabled_provider(self):
        self.config["role_order"] = {"coder": ["antigravity", "claude", "codex"]}
        self.config["providers"]["antigravity"]["enabled"] = False
        selected, attempts = agent_fallback.dispatch("coder", "prompt", self.config)
        self.assertEqual(selected.provider, "claude")
        self.assertEqual(attempts[0].classification, "disabled")

    def test_claude_quota_falls_back_to_codex(self):
        os.environ["FAKE_CLAUDE"] = "quota"
        selected, attempts = agent_fallback.dispatch("thinker", "prompt", self.config)
        self.assertEqual(selected.provider, "codex")
        self.assertEqual([item.classification for item in attempts], ["quota", "valid"])

    def test_codex_quota_falls_back_to_antigravity(self):
        os.environ["FAKE_CODEX"] = "quota"
        os.environ["FAKE_CLAUDE"] = "quota"
        selected, attempts = agent_fallback.dispatch("coder", "prompt", self.config)
        self.assertEqual(selected.provider, "antigravity")
        self.assertEqual([item.provider for item in attempts], ["codex", "claude", "antigravity"])

    def test_technical_negative_answer_does_not_fallback(self):
        os.environ["FAKE_CODEX"] = "technical"
        selected, attempts = agent_fallback.dispatch("coder", "prompt", self.config)
        self.assertEqual(selected.provider, "codex")
        self.assertEqual(selected.classification, "valid")
        self.assertEqual(len(attempts), 1)

    def test_timeout_falls_back(self):
        os.environ["FAKE_CLAUDE"] = "slow"
        selected, attempts = agent_fallback.dispatch("thinker", "prompt", self.config, timeout_override=0.05)
        self.assertEqual(selected.provider, "codex")
        self.assertEqual(attempts[0].classification, "timeout")

    def test_antigravity_prompt_is_passed_as_safe_argument(self):
        self.config["providers"]["antigravity"]["prompt_transport"] = "argument"
        os.environ["FAKE_EXPECT_PROMPT_ARG"] = "1"
        selected, attempts = agent_fallback.dispatch("reviewer", "prompt", self.config)
        self.assertEqual(selected.provider, "antigravity")
        self.assertEqual(len(attempts), 1)

    def test_configurable_auth_pattern_is_classified_before_answer(self):
        self.assertEqual(
            agent_fallback.classify(0, "respuesta", "SESSION-REAUTH", {"auth": ["session-reauth"]}),
            "auth",
        )

    def test_cli_outputs_only_selected_answer_on_stdout(self):
        config_path = self.root / "config.json"
        config_path.write_text(json.dumps(self.config), encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(ROOT / "agent_fallback.py"), "--role", "reviewer", "--config", str(config_path)],
            input="hello", text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "antigravity response\n")
        self.assertIn("antigravity: valid", result.stderr)

    def test_log_has_no_prompt_or_provider_output(self):
        attempt = agent_fallback.Attempt("claude", "valid", 0, "sensitive answer", "sensitive diagnostic", 1)
        agent_fallback.append_log(Path(self.config["log_path"]), "thinker", attempt)
        event = json.loads(Path(self.config["log_path"]).read_text(encoding="utf-8"))
        self.assertEqual(set(event), {"timestamp", "role", "provider", "classification", "exit_code", "duration_ms"})



class PartialWorkTests(unittest.TestCase):
    def _repo(self):
        import tempfile
        d = Path(tempfile.mkdtemp())
        for cmd in (["git", "init", "-q"], ["git", "config", "user.email", "t@t"], ["git", "config", "user.name", "t"]):
            subprocess.run(cmd, cwd=d, check=True)
        (d / "a.txt").write_text("a\n")
        subprocess.run(["git", "add", "."], cwd=d, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=d, check=True)
        return d

    def test_changes_and_snapshot_keep_worktree_intact(self):
        d = self._repo()
        self.assertEqual(agent_fallback.worktree_changes(d), set())
        (d / "a.txt").write_text("edited\n")
        (d / "new.go").write_text("package x\n")
        self.assertEqual(agent_fallback.worktree_changes(d), {"a.txt", "new.go"})
        branch = agent_fallback.snapshot_worktree(d, "coder-run1")
        self.assertTrue(branch and branch.startswith("salvage/auto/coder-run1-"))
        shown = subprocess.run(["git", "show", f"{branch}:new.go"], cwd=d, capture_output=True, text=True).stdout
        self.assertEqual(shown, "package x\n")
        # El worktree y su indice no cambian: sigue habiendo cambios sin commit.
        self.assertEqual(agent_fallback.worktree_changes(d), {"a.txt", "new.go"})
        self.assertEqual(subprocess.run(["git", "diff", "--cached", "--name-only"], cwd=d,
                                        capture_output=True, text=True).stdout, "")

    def test_clean_worktree_has_no_snapshot(self):
        self.assertIsNone(agent_fallback.snapshot_worktree(self._repo(), "x"))

    def test_continuation_note_names_provider_and_files(self):
        note = agent_fallback.continuation_note([agent_fallback.Attempt("opencode-go-kimi", "quota", 1)], {"core/x.go"})
        self.assertIn("opencode-go-kimi (quota)", note)
        self.assertIn("- core/x.go", note)
        self.assertIn("CONTINUA", note)


class AttemptTrailTests(unittest.TestCase):
    def test_trail_keeps_messages_commands_and_files_in_order(self):
        raw = "\n".join([
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Voy a editar calls.go"}]}}),
            json.dumps({"type": "tool_use", "input": {"command": "go test ./internal/fingerprint"}}),
            json.dumps({"item": {"type": "file_change", "changes": [{"path": "core/internal/fingerprint/calls.go"}]}}),
            "no json",
        ])
        trail = agent_fallback.attempt_trail(raw)
        self.assertLess(trail.index("Voy a editar"), trail.index("go test"))
        self.assertIn("path: core/internal/fingerprint/calls.go", trail)

    def test_plain_text_output_is_kept(self):
        self.assertIn("quota exceeded", agent_fallback.attempt_trail("partial work...\nquota exceeded"))

    def test_note_includes_trail_of_cut_attempt(self):
        cut = agent_fallback.Attempt("opencode-go-glm", "quota", 1, raw=json.dumps({"text": "edite weight.go"}))
        note = agent_fallback.continuation_note([cut], {"core/internal/graph/weight.go"})
        self.assertIn("Bitacora de opencode-go-glm", note)
        self.assertIn("edite weight.go", note)


if __name__ == "__main__":
    unittest.main()


class StreamDecoderTests(unittest.TestCase):
    """Shapes captured from the real CLIs on 2026-09-14, not invented."""

    def test_claude_stream_extracts_final_result(self):
        payload = "\n".join([
            '{"type":"system","subtype":"init"}',
            '{"type":"assistant","message":{"content":[{"type":"text","text":"OK"}]}}',
            '{"type":"result","subtype":"success","is_error":false,"result":"OK",'
            '"permission_denials":[],"total_cost_usd":0.22}',
        ])
        kind, answer = agent_fallback.decode_response(0, payload, "", {}, "claude-stream")
        self.assertEqual(kind, "valid")
        self.assertEqual(answer, "OK")

    def test_claude_stream_without_result_event_is_provider_error(self):
        payload = '{"type":"system","subtype":"init"}'
        kind, answer = agent_fallback.decode_response(0, payload, "", {}, "claude-stream")
        self.assertEqual(kind, "provider_error")
        self.assertEqual(answer, "")

    def test_claude_stream_empty_answer_with_denials_is_permission_denied(self):
        payload = ('{"type":"result","subtype":"success","is_error":false,"result":"",'
                   '"permission_denials":[{"tool":"Bash"}]}')
        kind, _ = agent_fallback.decode_response(0, payload, "", {}, "claude-stream")
        self.assertEqual(kind, "permission_denied")

    def test_agy_stream_extracts_response(self):
        payload = "\n".join([
            '{"event":"init","conversation_id":"x"}',
            '{"event":"step_update","step_update":{}}',
            '{"event":"result","result":{"status":"SUCCESS","response":"OK\\n",'
            '"usage":{"total_tokens":21242}}}',
        ])
        kind, answer = agent_fallback.decode_response(0, payload, "", {}, "agy-stream")
        self.assertEqual(kind, "valid")
        self.assertEqual(answer.strip(), "OK")

    def test_agy_stream_denied_actions_is_permission_denied(self):
        payload = ('{"event":"result","result":{"status":"SUCCESS","response":"",'
                   '"denied_actions":["RunCommand"]}}')
        kind, _ = agent_fallback.decode_response(0, payload, "", {}, "agy-stream")
        self.assertEqual(kind, "permission_denied")

    def test_quota_pattern_still_wins_over_stream_shape(self):
        payload = ('{"event":"result","result":{"status":"ERROR",'
                   '"error":"quota exceeded, try later"}}')
        kind, _ = agent_fallback.decode_response(
            0, payload, "", {"quota": ["quota exceeded"]}, "agy-stream")
        self.assertEqual(kind, "quota")

    def test_provider_error_carries_the_providers_words(self):
        payload = ('{"event":"result","result":{"status":"ERROR",'
                   '"error":{"code":13,"message":"internal error while streaming"}}}')
        kind, answer, detail = agent_fallback.decode_response_detail(0, payload, "", {}, "agy-stream")
        self.assertEqual(kind, "provider_error")
        self.assertEqual(answer, "")
        self.assertIn("internal error while streaming", detail)

    def test_empty_answer_detail_says_nothing_was_written(self):
        payload = '{"event":"result","result":{"status":"SUCCESS","response":""}}'
        kind, _, detail = agent_fallback.decode_response_detail(0, payload, "", {}, "agy-stream")
        self.assertEqual(kind, "no_valid_response")
        self.assertIn("ningun archivo", detail)

    def test_opencode_stream_collects_text_and_written_files(self):
        payload = "\n".join([
            '{"type":"step_start","part":{"type":"step-start"}}',
            '{"type":"tool_use","part":{"type":"tool","tool":"write","state":{"status":"completed","input":{"path":"/w/hola.txt"}}}}',
            '{"type":"text","part":{"id":"p1","type":"text","text":"DONE"}}',
            '{"type":"step_finish","part":{"type":"step-finish","tokens":{"input":10,"output":2}}}',
        ])
        kind, answer = agent_fallback.decode_response(0, payload, "", {}, "opencode-json")
        self.assertEqual((kind, answer), ("valid", "DONE"))
        silent = payload.replace('"text":"DONE"', '"text":""')
        kind, answer = agent_fallback.decode_response(0, silent, "", {}, "opencode-json")
        self.assertEqual(kind, "valid"); self.assertIn("/w/hola.txt", answer)

    def test_opencode_quota_and_no_route_are_classified(self):
        quota = '{"type":"error","error":{"type":"provider.quota","message":"Upstream request failed: Insufficient account funds","status":402}}'
        kind, _, detail = agent_fallback.decode_response_detail(1, quota, "", {"quota": ["provider.quota"], "auth": ["provider.no-route"]}, "opencode-json")
        self.assertEqual(kind, "quota"); self.assertIn("Insufficient", detail)
        route = '{"type":"error","error":{"type":"provider.no-route","message":"Model unavailable: opencode-go/kimi-k3"}}'
        kind, _, _ = agent_fallback.decode_response_detail(1, route, "", {"quota": ["provider.quota"], "auth": ["provider.no-route"]}, "opencode-json")
        self.assertEqual(kind, "auth")

    def test_opencode_progress_label_names_the_tool(self):
        line = '{"type":"tool_use","part":{"type":"tool","tool":"bash","state":{"status":"running"}}}'
        self.assertEqual(agent_fallback.summarize_progress(line), "tool_use:bash:running")

    def test_stream_tokens_per_format(self):
        oc = [{"type": "step_finish", "part": {"tokens": {"input": 100, "output": 10, "reasoning": 5, "cache": {"read": 50, "write": 0}}}}] * 2
        self.assertEqual(agent_fallback.stream_tokens(oc, "opencode-json"), {"in": 200, "out": 30, "cache": 100})
        cx = [{"type": "turn.completed", "usage": {"input_tokens": 1000, "cached_input_tokens": 900, "output_tokens": 20}}]
        self.assertEqual(agent_fallback.stream_tokens(cx, "codex-jsonl"), {"in": 1000, "out": 20, "cache": 900})
        cl = [{"type": "result", "usage": {"input_tokens": 5, "output_tokens": 7, "cache_read_input_tokens": 300, "cache_creation_input_tokens": 20}}]
        self.assertEqual(agent_fallback.stream_tokens(cl, "claude-stream"), {"in": 5, "out": 7, "cache": 320})
        ag = [{"event": "result", "result": {"status": "SUCCESS", "usage": {"total_tokens": 21242}}}]
        self.assertEqual(agent_fallback.stream_tokens(ag, "agy-stream"), {"in": 21242, "out": 0, "cache": 0})
        self.assertIsNone(agent_fallback.stream_tokens([{"type": "text"}], "opencode-json"))

    def test_non_json_body_is_invalid_not_empty_answer(self):
        payload = "command not found: agy"
        kind, answer = agent_fallback.decode_response(127, payload, "", {}, "claude-stream")
        self.assertEqual(kind, "invalid_response")
        self.assertEqual(answer, "")

    def test_partial_trailing_line_does_not_lose_earlier_events(self):
        payload = '{"type":"result","subtype":"success","result":"OK"}\n{"type":"assi'
        kind, answer = agent_fallback.decode_response(0, payload, "", {}, "claude-stream")
        self.assertEqual(kind, "valid")
        self.assertEqual(answer, "OK")


class CodexQuotaPatternTests(unittest.TestCase):
    def test_observed_codex_quota_message_classifies_as_quota(self):
        """Wording captured from the codex CLI on 2026-09-14."""
        message = ("You've hit your usage limit. Upgrade to Pro, visit "
                   "https://chatgpt.com/codex/settings/usage to purchase more "
                   "credits or try again at 8:17 PM.")
        payload = "\n".join([
            '{"type":"thread.started"}',
            json.dumps({"type": "error", "message": message}),
            json.dumps({"type": "turn.failed", "error": {"message": message}}),
        ])
        config = json.loads(
            Path(__file__).resolve().parents[1].joinpath(
                "config/agent-fallback.json").read_text())
        patterns = config["providers"]["codex"]["patterns"]
        kind, answer = agent_fallback.decode_response(1, payload, "", patterns, "codex-jsonl")
        self.assertEqual(kind, "quota")
        self.assertEqual(answer, "")


class CooldownTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / "cooldowns.json"

    def test_reset_seconds_reads_codex_date(self):
        now = time.mktime((2026, 9, 19, 14, 26, 0, 0, 0, -1))
        detail = "You've hit your usage limit ... or try again at Sep 20th, 2026 4:33 PM."
        self.assertAlmostEqual(agent_fallback.reset_seconds(detail, now), 26 * 3600 + 7 * 60, delta=1)

    def test_reset_seconds_reads_clock_and_epoch(self):
        now = time.mktime((2026, 9, 19, 14, 0, 0, 0, 0, -1))
        self.assertAlmostEqual(agent_fallback.reset_seconds("limit reached · resets 3pm", now), 3600, delta=1)
        self.assertAlmostEqual(agent_fallback.reset_seconds("resets 1pm", now), 23 * 3600, delta=1)
        self.assertAlmostEqual(agent_fallback.reset_seconds(f"usage limit reached|{int(now) + 500}", now), 500, delta=1)
        self.assertIsNone(agent_fallback.reset_seconds("quota exceeded", now))
        self.assertEqual(agent_fallback.reset_seconds("Individual quota reached. Resets in 2h30m42s.", now), 2 * 3600 + 30 * 60 + 42)
        self.assertEqual(agent_fallback.reset_seconds("Resets in 45m", now), 45 * 60)
        self.assertEqual(agent_fallback.reset_seconds("5-hour usage limit reached. Resets in 4hr 22min.", now), 4 * 3600 + 22 * 60)
        self.assertEqual(agent_fallback.reset_seconds("purchase more credits or try again at 3:14 PM.", now), 74 * 60)

    def test_recent_invocations_counts_only_real_calls_in_window(self):
        import tempfile
        from pathlib import Path as P
        log = P(tempfile.mkdtemp()) / "log.jsonl"
        now = time.time()
        def stamp(delta): return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - delta))
        log.write_text("\n".join(json.dumps(e) for e in [
            {"provider": "claude", "classification": "valid", "timestamp": stamp(60)},
            {"provider": "claude", "classification": "cooling_down", "timestamp": stamp(60)},
            {"provider": "claude", "classification": "valid", "timestamp": stamp(7200)},
            {"provider": "codex", "classification": "valid", "timestamp": stamp(60)},
        ]) + "\n")
        self.assertEqual(agent_fallback.recent_invocations(log, "claude", 3600), 1)
        self.assertEqual(agent_fallback.reset_seconds("try again at Jan 1st, 2031 1:00 AM", now), 48 * 3600)

    def test_quota_pool_siblings_share_the_cooldown(self):
        providers = {"codex": {"quota_pool": "codex"}, "codex-terra": {"quota_pool": "codex"},
                     "antigravity": {"quota_pool": "agy-gemini"}, "claude": {}}
        self.assertEqual(sorted(agent_fallback.quota_pool_siblings(providers, "codex")), ["codex", "codex-terra"])
        self.assertEqual(agent_fallback.quota_pool_siblings(providers, "antigravity"), ["antigravity"])
        self.assertEqual(agent_fallback.quota_pool_siblings(providers, "claude"), ["claude"])

    def test_worktree_links_symlink_missing_dirs_only(self):
        import tempfile
        from pathlib import Path as P
        src = P(tempfile.mkdtemp()); (src / "node_modules").mkdir()
        wt = P(tempfile.mkdtemp()); (wt / "mcp").mkdir(); (wt / "dashboard").mkdir(); (wt / "dashboard/node_modules/real").mkdir(parents=True)
        made = agent_fallback.link_worktree_dirs(
            {"mcp/node_modules": str(src / "node_modules"), "dashboard/node_modules": str(src / "node_modules"),
             "other/node_modules": str(src / "missing")}, wt)
        self.assertEqual(made, ["mcp/node_modules"])
        self.assertTrue((wt / "mcp/node_modules").is_dir() and not (wt / "mcp/node_modules").is_symlink())
        (src / "node_modules/pkg").mkdir(); (src / "node_modules/.bin").mkdir()
        agent_fallback.link_worktree_dirs({"mcp/node_modules": str(src / "node_modules")}, wt)  # idempotente y rellena
        self.assertTrue((wt / "mcp/node_modules/pkg").is_symlink()); self.assertTrue((wt / "mcp/node_modules/.bin").is_symlink())
        self.assertFalse((wt / "other/node_modules").exists())

    def test_rotate_first_spreads_consecutive_calls(self):
        import tempfile
        from pathlib import Path as P
        path = P(tempfile.mkdtemp()) / "rotation.json"
        order = ["a", "b", "c", "d"]
        self.assertEqual(agent_fallback.rotate_first(order, 3, path, "coder"), ["a", "b", "c", "d"])
        self.assertEqual(agent_fallback.rotate_first(order, 3, path, "coder"), ["b", "c", "a", "d"])
        self.assertEqual(agent_fallback.rotate_first(order, 3, path, "coder"), ["c", "a", "b", "d"])
        self.assertEqual(agent_fallback.rotate_first(order, 3, path, "coder"), ["a", "b", "c", "d"])
        self.assertEqual(agent_fallback.rotate_first(order, 0, path, "coder"), order)

    def test_missing_file_means_no_cooldowns(self):
        self.assertEqual(agent_fallback.read_cooldowns(self.path), {})

    def test_corrupt_file_never_blocks_a_provider(self):
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(agent_fallback.read_cooldowns(self.path), {})

    def test_write_then_read_roundtrip(self):
        agent_fallback.write_cooldown(self.path, "codex", "quota", 600)
        cooldowns = agent_fallback.read_cooldowns(self.path)
        self.assertIn("codex", cooldowns)
        self.assertGreater(cooldowns["codex"], time.time())

    def test_write_preserves_other_providers(self):
        agent_fallback.write_cooldown(self.path, "codex", "quota", 600)
        agent_fallback.write_cooldown(self.path, "claude", "auth", 600)
        self.assertEqual(set(agent_fallback.read_cooldowns(self.path)), {"codex", "claude"})

    def test_expired_cooldown_is_not_skipped(self):
        now = time.time()
        cooling = agent_fallback.cooling_providers(
            ["codex", "claude"], {"codex": now - 1}, now)
        self.assertEqual(cooling, set())

    def test_active_cooldown_is_skipped(self):
        now = time.time()
        cooling = agent_fallback.cooling_providers(
            ["codex", "claude"], {"codex": now + 600}, now)
        self.assertEqual(cooling, {"codex"})

    def test_all_cooling_tries_only_the_one_about_to_reset(self):
        """Within ten minutes of a reset the estimate may be stale: try that one."""
        now = time.time()
        cooling = agent_fallback.cooling_providers(
            ["codex", "claude"], {"codex": now + 3600, "claude": now + 300}, now)
        self.assertEqual(cooling, {"codex"})

    def test_all_cooling_far_away_skips_everyone(self):
        """Hammering exhausted providers every retry costs quota and time."""
        now = time.time()
        cooling = agent_fallback.cooling_providers(
            ["codex", "claude"], {"codex": now + 3600, "claude": now + 7200}, now)
        self.assertEqual(cooling, {"codex", "claude"})

    def test_quota_attempt_records_cooldown_and_next_run_skips_it(self):
        config = {
            "timeout_seconds": 10,
            "cooldown_path": str(self.path),
            "cooldown_seconds": 600,
            "role_order": {"coder": ["codex", "claude"]},
            "providers": {
                "codex": {"enabled": True, "output_format": "text",
                          "command": [sys.executable, "-c",
                                      "import sys; sys.stdin.read(); "
                                      "print('quota reached'); sys.exit(1)"],
                          "patterns": {"quota": ["quota reached"]}},
                "claude": {"enabled": True, "output_format": "text",
                           "command": [sys.executable, "-c",
                                       "import sys; sys.stdin.read(); print('respuesta')"],
                           "patterns": {}},
            },
        }
        selected, attempts = agent_fallback.dispatch("coder", "prompt", config)
        self.assertEqual([a.classification for a in attempts], ["quota", "valid"])
        self.assertEqual(selected.stdout.strip(), "respuesta")

        selected, attempts = agent_fallback.dispatch("coder", "prompt", config)
        self.assertEqual([a.classification for a in attempts], ["cooling_down", "valid"])
        self.assertEqual(selected.stdout.strip(), "respuesta")


class CwdPlaceholderTests(unittest.TestCase):
    def test_cwd_placeholder_is_replaced_with_working_directory(self):
        with tempfile.TemporaryDirectory() as workdir:
            script = ("import sys; sys.stdin.read(); "
                      "print(sys.argv[1])")
            attempt = agent_fallback.run_provider(
                "probe",
                [sys.executable, "-c", script, "{cwd}"],
                "prompt", 30, {}, "stdin", "text")
            self.assertEqual(attempt.classification, "valid")
            self.assertEqual(attempt.stdout.strip(), os.getcwd())
            del workdir

    def test_command_without_placeholder_is_untouched(self):
        attempt = agent_fallback.run_provider(
            "probe",
            [sys.executable, "-c", "import sys; sys.stdin.read(); print(sys.argv[1])", "literal"],
            "prompt", 30, {}, "stdin", "text")
        self.assertEqual(attempt.stdout.strip(), "literal")

    def test_placeholder_in_prompt_is_not_substituted(self):
        """Only the configured command is templated, never model input."""
        attempt = agent_fallback.run_provider(
            "probe",
            [sys.executable, "-c", "import sys; print(sys.stdin.read())"],
            "texto con {cwd} dentro", 30, {}, "stdin", "text")
        self.assertIn("{cwd}", attempt.stdout)


class IdleTimeoutTests(unittest.TestCase):
    """A silent process is hung; a chatty one is working, however long it takes."""

    def test_silent_process_is_cut_by_idle_timeout(self):
        script = "import sys, time; sys.stdin.read(); time.sleep(30)"
        started = time.monotonic()
        attempt = agent_fallback.run_provider(
            "probe", [sys.executable, "-c", script], "p", 30, {}, "stdin", "text",
            idle_timeout=1.0)
        self.assertEqual(attempt.classification, "idle_timeout")
        self.assertLess(time.monotonic() - started, 10)

    def test_chatty_process_outlives_its_idle_timeout(self):
        """Keeps emitting past the idle window, so it must not be killed."""
        script = ("import sys, time\n"
                  "sys.stdin.read()\n"
                  "for i in range(6):\n"
                  "    print('tick', flush=True)\n"
                  "    time.sleep(0.4)\n")
        attempt = agent_fallback.run_provider(
            "probe", [sys.executable, "-c", script], "p", 30, {}, "stdin", "text",
            idle_timeout=1.0)
        self.assertEqual(attempt.classification, "valid")
        self.assertEqual(attempt.stdout.count("tick"), 6)

    def test_absolute_deadline_still_wins(self):
        script = ("import sys, time\n"
                  "sys.stdin.read()\n"
                  "while True:\n"
                  "    print('tick', flush=True)\n"
                  "    time.sleep(0.2)\n")
        attempt = agent_fallback.run_provider(
            "probe", [sys.executable, "-c", script], "p", 1.5, {}, "stdin", "text",
            idle_timeout=60.0)
        self.assertEqual(attempt.classification, "timeout")


class RoleModelTests(unittest.TestCase):
    CLAUDE = ["claude", "--model", "{model}", "-p", "--output-format", "stream-json"]
    AGY = ["agy", "--model", "{model}", "--mode=accept-edits", "-p"]

    def test_model_fills_the_placeholder_in_place(self):
        self.assertEqual(
            agent_fallback.resolve_model(self.CLAUDE, "opus"),
            ["claude", "--model", "opus", "-p", "--output-format", "stream-json"])

    def test_placeholder_keeps_its_position_before_a_trailing_prompt_flag(self):
        """agy takes the prompt as an argument after -p, so order matters."""
        resolved = agent_fallback.resolve_model(self.AGY, "gemini-3.1-pro-high")
        self.assertEqual(resolved[-1], "-p")
        self.assertEqual(resolved[1:3], ["--model", "gemini-3.1-pro-high"])

    def test_no_model_drops_the_flag_and_its_value(self):
        self.assertEqual(
            agent_fallback.resolve_model(self.CLAUDE, None),
            ["claude", "-p", "--output-format", "stream-json"])

    def test_command_without_placeholder_is_untouched_either_way(self):
        plain = ["codex", "exec", "--json", "-"]
        self.assertEqual(agent_fallback.resolve_model(plain, "x"), plain)
        self.assertEqual(agent_fallback.resolve_model(plain, None), plain)

    def test_role_model_reaches_the_process(self):
        config = {
            "timeout_seconds": 20,
            "cooldown_seconds": 0,
            "role_order": {"thinker": ["probe"]},
            "role_models": {"thinker": {"probe": "modelo-elegido"}},
            "providers": {"probe": {"enabled": True, "output_format": "text",
                                    "command": [sys.executable, "-c",
                                                "import sys; sys.stdin.read(); print(sys.argv[1])",
                                                "{model}"],
                                    "patterns": {}}},
        }
        selected, _ = agent_fallback.dispatch("thinker", "p", config)
        self.assertEqual(selected.stdout.strip(), "modelo-elegido")


class RoleValidationTests(unittest.TestCase):
    """Roles live in the config file; adding one must not require a code change."""

    def _config(self, path: Path, roles: dict) -> None:
        path.write_text(json.dumps({
            "timeout_seconds": 10,
            "cooldown_seconds": 0,
            "role_order": roles,
            "providers": {"probe": {"enabled": True, "output_format": "text",
                                    "command": [sys.executable, "-c",
                                                "import sys; sys.stdin.read(); print('listo')"],
                                    "patterns": {}}},
        }), encoding="utf-8")

    def test_role_defined_only_in_config_is_accepted(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "c.json"
            self._config(cfg, {"thinker_claude": ["probe"]})
            result = subprocess.run(
                [sys.executable, str(Path(__file__).resolve().parents[1] / "agent_fallback.py"),
                 "--role", "thinker_claude", "--config", str(cfg)],
                input="prompt", text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "listo")

    def test_unknown_role_reports_what_is_defined(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "c.json"
            self._config(cfg, {"thinker_claude": ["probe"]})
            result = subprocess.run(
                [sys.executable, str(Path(__file__).resolve().parents[1] / "agent_fallback.py"),
                 "--role", "inventado", "--config", str(cfg)],
                input="prompt", text=True, capture_output=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn("thinker_claude", result.stderr)


class AgyCommandDenialTests(unittest.TestCase):
    """Shapes captured from the agy CLI on 2026-09-15."""

    def _stream(self, tools, denials, response=""):
        lines = [json.dumps({"event": "init"})]
        for name, target in tools:
            lines.append(json.dumps({"event": "step_update", "step_update": {
                "state": "DONE", "tool_name": name,
                "tool_info": {"parameters": {"TargetFile": target}} if target else {}}}))
        lines.append(json.dumps({"event": "result", "result": {
            "status": "SUCCESS", "response": response, "denied_actions": denials}}))
        return "\n".join(lines)

    def test_command_denial_after_edits_keeps_the_work(self):
        payload = self._stream(
            [("write_to_file", "core/internal/store/policy.go"),
             ("replace_file_content", "core/internal/store/assets.go")],
            [{"action": "command", "display_name": "RunCommand"}])
        kind, answer = agent_fallback.decode_response(0, payload, "", {}, "agy-stream")
        self.assertEqual(kind, "valid")
        self.assertIn("policy.go", answer)
        self.assertIn("assets.go", answer)

    def test_command_denial_without_edits_is_still_permission_denied(self):
        payload = self._stream([("view_file", "README.md")],
                               [{"action": "command", "display_name": "RunCommand"}])
        kind, _ = agent_fallback.decode_response(0, payload, "", {}, "agy-stream")
        self.assertEqual(kind, "permission_denied")

    def test_a_non_command_denial_is_not_excused_by_edits(self):
        """Being blocked from reading is a real failure, edits or not."""
        payload = self._stream([("write_to_file", "a.go")],
                               [{"action": "read_file", "display_name": "ListDir"}])
        kind, _ = agent_fallback.decode_response(0, payload, "", {}, "agy-stream")
        self.assertEqual(kind, "permission_denied")

    def test_real_answer_is_preferred_over_the_synthesized_listing(self):
        payload = self._stream([("write_to_file", "a.go")],
                               [{"action": "command", "display_name": "RunCommand"}],
                               response="Implemente la politica de acceso.")
        kind, answer = agent_fallback.decode_response(0, payload, "", {}, "agy-stream")
        self.assertEqual(kind, "valid")
        self.assertEqual(answer, "Implemente la politica de acceso.")


class AgyProgressLabelTests(unittest.TestCase):
    def test_tool_name_is_surfaced_for_antigravity_steps(self):
        line = json.dumps({"event": "step_update", "step_update": {
            "state": "DONE", "tool_name": "write_to_file"}})
        self.assertEqual(agent_fallback.summarize_progress(line), "step_update:write_to_file")

    def test_running_step_shows_its_state(self):
        line = json.dumps({"event": "step_update", "step_update": {
            "state": "ACTIVE", "tool_name": "run_command"}})
        self.assertEqual(agent_fallback.summarize_progress(line), "step_update:run_command:active")


class AgySilentCompletionTests(unittest.TestCase):
    """Antigravity often ends its single turn without a closing summary."""

    def _stream(self, tools, denials=None, response=""):
        lines = [json.dumps({"event": "init"})]
        for name, target in tools:
            lines.append(json.dumps({"event": "step_update", "step_update": {
                "state": "DONE", "tool_name": name,
                "tool_info": {"parameters": {"TargetFile": target}} if target else {}}}))
        lines.append(json.dumps({"event": "result", "result": {
            "status": "SUCCESS", "response": response, "denied_actions": denials or []}}))
        return "\n".join(lines)

    def test_empty_answer_with_edits_and_no_denials_is_valid(self):
        payload = self._stream([("run_command", None),
                                ("replace_file_content", "core/internal/store/policy.go"),
                                ("write_to_file", "core/internal/store/policy_test.go")])
        kind, answer = agent_fallback.decode_response(0, payload, "", {}, "agy-stream")
        self.assertEqual(kind, "valid")
        self.assertIn("policy.go", answer)
        self.assertIn("policy_test.go", answer)

    def test_empty_answer_without_edits_is_still_no_valid_response(self):
        payload = self._stream([("run_command", None), ("view_file", "README.md")])
        kind, _ = agent_fallback.decode_response(0, payload, "", {}, "agy-stream")
        self.assertEqual(kind, "no_valid_response")

    def test_blocking_denial_is_not_excused_by_edits(self):
        payload = self._stream([("write_to_file", "a.go")],
                               [{"action": "read_file", "display_name": "ListDir"}])
        kind, _ = agent_fallback.decode_response(0, payload, "", {}, "agy-stream")
        self.assertEqual(kind, "permission_denied")


class SilentCompletionAcrossProvidersTests(unittest.TestCase):
    """No provider may lose a worktree just because it skipped its summary."""

    def test_claude_empty_result_with_edits_is_valid(self):
        payload = "\n".join([
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Write",
                 "input": {"file_path": "core/internal/store/policy.go"}}]}}),
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Edit",
                 "input": {"file_path": "core/internal/store/assets.go"}}]}}),
            json.dumps({"type": "result", "subtype": "success", "is_error": False,
                        "result": "", "permission_denials": []}),
        ])
        kind, answer = agent_fallback.decode_response(0, payload, "", {}, "claude-stream")
        self.assertEqual(kind, "valid")
        self.assertIn("policy.go", answer)
        self.assertIn("assets.go", answer)

    def test_claude_empty_result_without_edits_stays_no_valid_response(self):
        payload = "\n".join([
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Read", "input": {"file_path": "README.md"}}]}}),
            json.dumps({"type": "result", "subtype": "success", "is_error": False,
                        "result": "", "permission_denials": []}),
        ])
        kind, _ = agent_fallback.decode_response(0, payload, "", {}, "claude-stream")
        self.assertEqual(kind, "no_valid_response")

    def test_codex_without_agent_message_but_with_file_change_is_valid(self):
        payload = "\n".join([
            json.dumps({"type": "item.completed", "item": {
                "type": "file_change",
                "changes": [{"path": "core/internal/store/policy.go"}]}}),
            json.dumps({"type": "turn.completed"}),
        ])
        kind, answer = agent_fallback.decode_response(0, payload, "", {}, "codex-jsonl")
        self.assertEqual(kind, "valid")
        self.assertIn("policy.go", answer)

    def test_codex_error_still_wins_over_file_changes(self):
        payload = "\n".join([
            json.dumps({"type": "item.completed", "item": {
                "type": "file_change", "changes": [{"path": "a.go"}]}}),
            json.dumps({"type": "turn.failed", "error": {"message": "boom"}}),
        ])
        kind, _ = agent_fallback.decode_response(1, payload, "", {}, "codex-jsonl")
        self.assertEqual(kind, "provider_error")


class ExhaustedBehaviourTests(unittest.TestCase):
    """A developer that produced nothing is an empty candidate, not a broken run."""

    def _config(self, path: Path) -> None:
        path.write_text(json.dumps({
            "timeout_seconds": 10, "cooldown_seconds": 0,
            "role_order": {"coder_x": ["probe"]},
            "providers": {"probe": {"enabled": True, "output_format": "text",
                                    "command": [sys.executable, "-c",
                                                "import sys; sys.stdin.read(); sys.exit(3)"],
                                    "patterns": {}}},
        }), encoding="utf-8")

    def _run(self, extra):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "c.json"
            self._config(cfg)
            return subprocess.run(
                [sys.executable, str(Path(__file__).resolve().parents[1] / "agent_fallback.py"),
                 "--role", "coder_x", "--config", str(cfg), *extra],
                input="prompt", text=True, capture_output=True)

    def test_default_still_fails_loudly(self):
        result = self._run([])
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "")

    def test_report_mode_succeeds_with_an_explicit_empty_result(self):
        result = self._run(["--on-exhausted", "report"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Sin resultado", result.stdout)
        self.assertIn("No hay cambios", result.stdout)
        self.assertIn("todos los proveedores fallaron", result.stderr)


class RoleEffortTests(unittest.TestCase):
    CODEX = ["codex", "exec", "--model", "{model}", "-c", "model_reasoning_effort={effort}", "--json", "-"]

    def test_effort_fills_its_placeholder(self):
        out = agent_fallback.resolve_placeholder(self.CODEX, "{effort}", "high")
        self.assertIn("model_reasoning_effort=high", out)

    def test_missing_effort_drops_the_flag_pair(self):
        out = agent_fallback.resolve_placeholder(self.CODEX, "{effort}", None)
        self.assertNotIn("-c", out)
        self.assertFalse(any("{effort}" in a for a in out))
        self.assertEqual(out[-2:], ["--json", "-"])

    def test_role_effort_reaches_the_process(self):
        config = {
            "timeout_seconds": 20, "cooldown_seconds": 0,
            "role_order": {"reviewer": ["probe"]},
            "role_efforts": {"reviewer": {"probe": "alto"}},
            "providers": {"probe": {"enabled": True, "output_format": "text",
                                    "command": [sys.executable, "-c",
                                                "import sys; sys.stdin.read(); print(sys.argv[1])",
                                                "{effort}"],
                                    "patterns": {}}},
        }
        selected, _ = agent_fallback.dispatch("reviewer", "p", config)
        self.assertEqual(selected.stdout.strip(), "alto")


class QuotaWordingTests(unittest.TestCase):
    """Real-world exhaustion messages must classify as quota, not as a failed answer."""

    def _patterns(self, provider):
        cfg = json.loads(Path(__file__).resolve().parents[1].joinpath(
            "config/agent-fallback-cortex-read.json").read_text())
        return cfg["providers"][provider]["patterns"]

    def test_claude_usage_limit_message(self):
        payload = json.dumps({"type": "result", "subtype": "error", "is_error": True,
                              "result": "Claude AI usage limit reached|1789500000"})
        kind, _ = agent_fallback.decode_response(1, payload, "", self._patterns("claude"), "claude-stream")
        self.assertEqual(kind, "quota")

    def test_gemini_resource_exhausted_event(self):
        payload = "\n".join([
            json.dumps({"event": "init"}),
            json.dumps({"event": "error", "error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                                                    "message": "Quota exceeded for quota metric"}}),
            json.dumps({"event": "result", "result": {"status": "ERROR", "response": ""}}),
        ])
        kind, _ = agent_fallback.decode_response(1, payload, "", self._patterns("antigravity"), "agy-stream")
        self.assertEqual(kind, "quota")

    def test_codex_rate_limit_wording(self):
        payload = json.dumps({"type": "turn.failed", "error": {"message": "Rate limit reached for gpt-5.6-sol"}})
        kind, _ = agent_fallback.decode_response(1, payload, "", self._patterns("codex"), "codex-jsonl")
        self.assertEqual(kind, "quota")


class EscalationTests(unittest.TestCase):
    def test_escalates_after_two_hard_failures_only(self):
        config = {"providers": {"sonnet": {}, "cheap": {}}, "escalation": {"coder": {"after_failures": 2, "providers": ["sonnet"]}}}
        one = "plan\n## Fallo de `make test` sobre x\n## Estado del intento anterior (corrida `a`)\n"
        two = one + "## Hallazgos de la revision anterior (corrida `b`)\n"
        self.assertEqual(agent_fallback.escalated_order(["cheap"], "coder", one, config), ["cheap"])
        self.assertEqual(agent_fallback.escalated_order(["cheap"], "coder", two, config), ["sonnet", "cheap"])
        self.assertEqual(agent_fallback.escalated_order(["cheap"], "reviewer", two, config), ["cheap"])

