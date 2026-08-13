import importlib.util
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))

MODULE_PATH = Path(__file__).with_name("opencode_run.py")
SPEC = importlib.util.spec_from_file_location("opencode_run", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def declared_inputs(action_path: Path) -> dict[str, dict[str, object]]:
    """The action's `inputs:` block, by name.

    Hand-parsed rather than via PyYAML: these actions run on a bare runner before any
    dependency install, and a test that needs a package the code deliberately avoids would
    quietly stop running the day someone trims the test environment.
    """
    inputs: dict[str, dict[str, object]] = {}
    current: str | None = None
    in_block = False
    for line in action_path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith(" "):
            in_block = line.startswith("inputs:")
            current = None
            continue
        if not in_block:
            continue
        stripped = line.strip()
        if line.startswith("  ") and not line.startswith("    ") and stripped.endswith(":"):
            current = stripped[:-1]
            inputs[current] = {}
        elif current and stripped.startswith("required:"):
            inputs[current]["required"] = stripped.split(":", 1)[1].strip() == "true"
    return inputs


ACTION_INPUTS = declared_inputs(Path(__file__).with_name("action.yaml"))


class GenericityTests(unittest.TestCase):
    """What separates a binary wrapper from a reviewer that takes a parameter."""

    def test_the_caller_supplies_the_instruction(self) -> None:
        # The failure this layer exists to prevent. `opencode-review` had no `prompt` input
        # at all — only `extra-instructions`, appended INSIDE a hardcoded "Review this pull
        # request", so asking for anything else meant arguing with the paragraph above it.
        # Asserted against the declared contract rather than the prose: comments here
        # legitimately discuss reviews, inputs must not.
        action = ACTION_INPUTS
        self.assertIn("prompt", action)
        self.assertTrue(action["prompt"].get("required"), "a runner with an optional prompt has a default one")
        for leaked in ("pr-number", "extra-instructions", "base-sha", "head-sha", "github-token"):
            self.assertNotIn(leaked, action, f"{leaked!r} is task vocabulary; it does not belong in the runner")

    def test_the_runner_publishes_no_telemetry(self) -> None:
        # Two producers for one metric family is what made the dashboard's cost join
        # ambiguous. `report-agent-run` is the single writer; this action only hands it
        # numbers.
        self.assertNotIn("metrics-endpoint", ACTION_INPUTS)
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertNotIn("agent_metrics", source)
        self.assertNotIn("post_json", source)

    def test_config_pins_one_model_and_no_credential(self) -> None:
        config = MODULE.run_config("kimi-k3", run_id="42")
        provider = config["provider"]["gtowizard"]
        self.assertEqual(list(provider["models"]), ["kimi-k3"])
        self.assertEqual(provider["options"]["apiKey"], "{env:OPENCODE_GATEWAY_API_KEY}")
        self.assertIn("run:42", provider["options"]["headers"]["x-litellm-tags"])
        self.assertEqual(config["permission"]["*"], "deny")

    def test_write_verbs_stay_denied(self) -> None:
        bash = MODULE.run_config("kimi-k3")["permission"]["bash"]
        self.assertEqual(bash["*"], "deny")
        self.assertNotIn("git push*", bash)


class OutcomeTests(unittest.TestCase):
    """The runner reports how the RUN ended, never whether the ANSWER was good."""

    def test_unusable_is_not_a_runner_outcome(self) -> None:
        # A clean exit is success even if a caller's contract later rejects the content.
        # Folding that judgement in here is what made one runner unable to serve a task
        # whose idea of a good answer differs.
        self.assertEqual(MODULE.run_status(exit_code=0, cancelled=False), "success")

    def test_endings_are_told_apart(self) -> None:
        for kwargs, expected in (
            ({"exit_code": 0, "cancelled": False, "rejected": True}, "rejected"),
            ({"exit_code": 0, "cancelled": True}, "cancelled"),
            ({"exit_code": 124, "cancelled": False}, "timeout"),
            ({"exit_code": 1, "cancelled": False}, "error"),
        ):
            with self.subTest(expected):
                self.assertEqual(MODULE.run_status(**kwargs), expected)

    def test_rejection_outranks_a_timeout(self) -> None:
        # An exhausted key hangs until the timeout kills it. Measured: a key one cent over
        # budget produced four fifteen-minute `timeout` rows with zero tool calls, which read
        # as four slow models rather than one dead credential.
        self.assertEqual(MODULE.run_status(exit_code=124, cancelled=False, rejected=True), "rejected")

    def test_budget_refusal_is_recognised(self) -> None:
        self.assertTrue(MODULE.looks_rejected("ExceededBudget: Budget has been exceeded"))
        self.assertFalse(MODULE.looks_rejected("the diff touches a 429 retry path"))


class SessionTests(unittest.TestCase):
    def events(self) -> list[dict]:
        return [
            {"type": "text", "sessionID": "s1", "part": {"text": "thinking. "}},
            {"type": "tool", "part": {"tool": "read"}},
            {"type": "tool", "part": {"tool": "read"}},
            {"type": "tool", "part": {"tool": "grep"}},
            {"type": "step_finish", "part": {"reason": "stop", "tokens": {"input": 10, "output": 5, "cache": {"read": 3}}}},
            {"type": "text", "part": {"text": "the answer"}},
        ]

    def test_text_is_every_part_in_order(self) -> None:
        self.assertEqual(MODULE.answer_text(self.events()), "thinking. the answer")

    def test_tokens_follow_opencode_arithmetic(self) -> None:
        tokens = MODULE.token_usage(self.events())
        self.assertEqual(tokens["input"], 10)
        self.assertEqual(tokens["cache_read"], 3)
        self.assertEqual(tokens["total"], 18)

    def test_histogram_ranks_by_count(self) -> None:
        self.assertEqual(MODULE.tool_histogram(MODULE.tool_names(self.events())), "read x2, grep x1")

    def test_a_truncated_final_line_keeps_the_rest(self) -> None:
        # A timeout is exactly when the evidence matters, and it is exactly when the stream
        # is cut mid-line.
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            path.write_text(json.dumps({"type": "text", "part": {"text": "kept"}}) + '\n{"type": "te', encoding="utf-8")
            events = MODULE.read_events(path)
        self.assertEqual(MODULE.answer_text(events), "kept")


if __name__ == "__main__":
    unittest.main()
