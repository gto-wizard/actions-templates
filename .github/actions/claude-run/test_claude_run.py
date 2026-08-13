import importlib.util
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))

MODULE_PATH = Path(__file__).with_name("claude_run.py")
SPEC = importlib.util.spec_from_file_location("claude_run", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

ACTION = Path(__file__).with_name("action.yaml").read_text(encoding="utf-8")


class ThinnessTests(unittest.TestCase):
    """The wrapper configures and reports. Anything it reconstructs, it owns a copy of."""

    def test_it_does_not_recompute_what_the_cli_exports(self) -> None:
        # Claude Code emits claude_code_token_usage / _cost_usage / _active_time_total plus
        # tool spans natively. Deriving those here produces a second, worse copy of numbers
        # that already have an owner — which is how `gto.ai.agent.tokens` ended up measuring
        # the same runs as `claude_code_token_usage` with nearly the same labels.
        source = MODULE_PATH.read_text(encoding="utf-8")
        body = source.split('"""', 2)[-1]
        for recomputed in ("def token_usage", "def tool_histogram", "def tool_names", "cache_read"):
            self.assertNotIn(recomputed, body, f"{recomputed} duplicates a native signal")

    def test_it_execs_the_cli_rather_than_the_sdk_action(self) -> None:
        # The property gto-universe's review depends on: argv reaches the process unaltered.
        # claude-code-action's side-channel broke `--resume` on five consecutive runs.
        self.assertNotIn("anthropics/claude-code-action", ACTION)
        self.assertIn("$CLAUDE_ARGS", ACTION)

    def test_telemetry_is_configured_not_intercepted(self) -> None:
        for required in (
            "CLAUDE_CODE_ENABLE_TELEMETRY",
            "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
            "OTEL_RESOURCE_ATTRIBUTES",
            "OTEL_METRIC_EXPORT_INTERVAL",
        ):
            self.assertIn(required, ACTION)

    def test_prompts_and_tool_content_never_leave_the_runner(self) -> None:
        for redaction in ("OTEL_LOG_USER_PROMPTS", "OTEL_LOG_TOOL_CONTENT", "OTEL_LOG_RAW_API_BODIES"):
            self.assertIn(f'{redaction}: "0"', ACTION)


class HookTests(unittest.TestCase):
    """Hooks exist for the facts telemetry does not carry, and for nothing else."""

    def test_only_ending_events_are_hooked(self) -> None:
        # Every other event Claude Code can report, it already reports as a metric, a log or
        # a span. A hook duplicating one is a second source for a fact that has an owner.
        self.assertEqual(set(MODULE.HOOK_EVENTS), {"SessionEnd", "StopFailure"})

    def test_the_hook_needs_nothing_installed(self) -> None:
        settings = MODULE.hook_settings(Path("/tmp/rec.jsonl"))  # noqa: S108
        command = settings["hooks"]["SessionEnd"][0]["hooks"][0]["command"]
        self.assertTrue(command.startswith("cat >>"), command)

    def test_a_path_with_spaces_is_quoted(self) -> None:
        settings = MODULE.hook_settings(Path("/tmp/a dir/rec.jsonl"))  # noqa: S108
        self.assertIn("'/tmp/a dir/rec.jsonl'", settings["hooks"]["StopFailure"][0]["hooks"][0]["command"])

    def test_the_clis_own_words_decide_rejected_versus_error(self) -> None:
        # The distinction the opencode runner has to guess by regex-matching error text.
        for reason, expected in (
            ("rate_limit", "rejected"),
            ("billing_error", "rejected"),
            ("authentication_failed", "rejected"),
            ("overloaded", "rejected"),
            ("some_crash", "error"),
        ):
            with self.subTest(reason):
                records = [{"hook_event_name": "StopFailure", "reason": reason}]
                self.assertEqual(MODULE.hook_status(records), expected)

    def test_a_failure_outranks_the_session_ending(self) -> None:
        # The session always ends, so `end_reason` alone reports a rate-limited run as clean.
        records = [
            {"hook_event_name": "StopFailure", "reason": "rate_limit"},
            {"hook_event_name": "SessionEnd", "end_reason": "other"},
        ]
        self.assertEqual(MODULE.run_status(exit_code=1, cancelled=False, from_hook=MODULE.hook_status(records)),
                         "rejected")

    def test_no_hooks_is_not_an_error(self) -> None:
        self.assertEqual(MODULE.hook_status([]), "")
        with TemporaryDirectory() as tmp:
            self.assertEqual(MODULE.hook_records(Path(tmp) / "absent.jsonl"), [])


class OutcomeTests(unittest.TestCase):
    def test_unusable_is_not_a_runner_outcome(self) -> None:
        self.assertEqual(MODULE.run_status(exit_code=0, cancelled=False), "success")

    def test_being_killed_from_outside_is_what_the_wrapper_still_owns(self) -> None:
        # No hook fires when `timeout` signals the process, so the exit code is the only
        # evidence — and it outranks any hook that did manage to write.
        self.assertEqual(MODULE.run_status(exit_code=124, cancelled=False, from_hook="rejected"), "timeout")
        self.assertEqual(MODULE.run_status(exit_code=0, cancelled=True), "cancelled")

    def test_a_bare_non_zero_exit_is_an_error(self) -> None:
        self.assertEqual(MODULE.run_status(exit_code=1, cancelled=False), "error")


class TranscriptTests(unittest.TestCase):
    def test_only_the_answer_is_read_back(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "transcript.jsonl"
            path.write_text(
                "\n".join(
                    json.dumps(e)
                    for e in (
                        {"type": "assistant", "message": {"content": "noise"}},
                        {"type": "result", "result": "the answer", "session_id": "abc", "total_cost_usd": 0.42},
                    )
                ),
                encoding="utf-8",
            )
            result = MODULE.result_event(path)
        self.assertEqual(result["result"], "the answer")
        self.assertEqual(result["session_id"], "abc")

    def test_a_truncated_transcript_still_yields_what_arrived(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "transcript.jsonl"
            path.write_text(json.dumps({"type": "result", "result": "kept"}) + '\n{"type": "res', encoding="utf-8")
            self.assertEqual(MODULE.result_event(path)["result"], "kept")

    def test_no_transcript_is_not_an_error(self) -> None:
        with TemporaryDirectory() as tmp:
            self.assertEqual(MODULE.result_event(Path(tmp) / "absent.jsonl"), {})


class TrustTests(unittest.TestCase):
    def test_the_checkout_is_distrusted_by_default(self) -> None:
        # A pull request must not be able to configure the agent reviewing it. Widening this
        # is a caller's decision because only the caller knows where the input came from.
        self.assertIn("    default: user", ACTION)


if __name__ == "__main__":
    unittest.main()
