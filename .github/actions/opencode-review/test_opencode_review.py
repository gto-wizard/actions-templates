import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

MODULE_PATH = Path(__file__).with_name("opencode_review.py")
SPEC = importlib.util.spec_from_file_location("opencode_review", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

# One real step's counters, verbatim from an `opencode run --format json` stream against the
# gateway (opencode 1.18.16, gtowizard/kimi-k3). opencode reported total=38159 for exactly
# these, which is the only reason this module's arithmetic matches its own.
REAL_STEP_TOKENS = {
    "total": 38159,
    "input": 3374,
    "output": 140,
    "reasoning": 85,
    "cache": {"write": 0, "read": 34560},
}

VALID_REVIEW = {
    "summary": "Reads the permissions claim into Identity.groups.",
    "rationale": "Traced the claim from auth.py to Acl.capabilities_for and read both tests.",
    "verdict": "comment",
    "findings": [
        {"severity": "warning", "path": "services/mcp/mcp_server/auth.py", "line": 114, "message": "x"}
    ],
}

METADATA = {
    "repository": "gto-wizard/gto-brain",
    "pr_number": 181,
    "pr_url": "https://github.com/gto-wizard/gto-brain/pull/181",
    "changed_files": 7,
    "additions": 219,
    "deletions": 10,
}


def events(text="", tokens=None):
    """A minimal but structurally real stream: tool call, answer, step accounting."""
    session = "ses_009660d3affeLljd62940Foqsr"
    return [
        {"type": "step_start", "sessionID": session, "part": {"type": "step-start"}},
        {"type": "tool_use", "sessionID": session, "part": {"type": "tool", "tool": "bash"}},
        {"type": "text", "sessionID": session, "part": {"type": "text", "text": text}},
        {
            "type": "step_finish",
            "sessionID": session,
            "part": {
                "type": "step-finish",
                "reason": "stop",
                "cost": 0,
                "tokens": tokens or REAL_STEP_TOKENS,
            },
        },
    ]


class ExtractionTest(unittest.TestCase):
    """The answer is asked for, not enforced, so extraction is load-bearing."""

    def test_accepts_how_models_actually_answer(self) -> None:
        payload = json.dumps(VALID_REVIEW)
        restated = json.dumps({**VALID_REVIEW, "verdict": "approve"})
        for text, expected in (
            (payload, "comment"),
            (f"```json\n{payload}\n```", "comment"),
            (f"Here is my review.\n\n{payload}\n\nHappy to expand.", "comment"),
            # Narration between tool calls shares this text stream, and a model that restates
            # its answer means the restatement — so the LAST object wins.
            (f"Let me look at auth.py.\n{payload}\nCorrection:\n{restated}", "approve"),
        ):
            with self.subTest(text=text[:40]):
                review = MODULE.extract_review(text)
                self.assertIsNotNone(review)
                self.assertEqual(review["verdict"], expected)

    def test_returns_none_rather_than_a_fragment(self) -> None:
        # A findings entry is an object too; only the one carrying `summary` is the review.
        fragment = json.dumps({"severity": "info", "path": "a.py", "line": 1, "message": "m"})
        for text in ("", "Nothing wrong with this pull request.", "{not json}", fragment):
            with self.subTest(text=text[:40]):
                self.assertIsNone(MODULE.extract_review(text))


class ValidationTest(unittest.TestCase):
    def test_accepts_the_real_shape(self) -> None:
        self.assertEqual(MODULE.validate_review(VALID_REVIEW), [])
        self.assertEqual(MODULE.validate_review({**VALID_REVIEW, "findings": []}), [])

    def test_names_every_departure(self) -> None:
        cases = (
            ({"verdict": "lgtm"}, "verdict"),
            ({"summary": "   "}, "summary is missing or empty"),
            ({"rationale": None}, "rationale is missing or empty"),
            ({"findings": "none"}, "findings is not a list"),
            ({"findings": [{"severity": "nit", "path": "a.py", "line": 1, "message": "m"}]}, "severity"),
            ({"findings": [{"severity": "info", "path": "a.py", "line": "12", "message": "m"}]}, "line"),
            # `True` is an int in Python; a boolean line number is still malformed.
            ({"findings": [{"severity": "info", "path": "a.py", "line": True, "message": "m"}]}, "line"),
            ({"findings": ["blocking: fix it"]}, "findings[0] is not an object"),
        )
        for mutation, expected in cases:
            with self.subTest(mutation=mutation):
                problems = MODULE.validate_review({**VALID_REVIEW, **mutation})
                self.assertTrue(any(expected in problem for problem in problems), problems)


class AccountingTest(unittest.TestCase):
    def test_sums_steps_and_keeps_opencodes_own_total_arithmetic(self) -> None:
        single = MODULE.token_usage(events())
        self.assertEqual(
            single,
            {
                "input": 3374,
                "output": 140,
                "reasoning": 85,
                "cache_read": 34560,
                "cache_write": 0,
                "total": REAL_STEP_TOKENS["total"],
            },
        )
        # Two steps double every counter: reviews are billed per step, so the report must not
        # collapse them into one context size.
        self.assertEqual(MODULE.token_usage(events() + events())["total"], 2 * single["total"])

    def test_tool_histogram_collapses_a_real_reviews_call_order(self) -> None:
        calls = ["bash"] + ["read"] * 6 + ["bash", "grep", "grep", "read", "read"] + ["grep"] * 4 + ["read"]
        self.assertEqual(MODULE.tool_histogram(calls), "read x9, grep x6, bash x2")
        self.assertEqual(MODULE.tool_histogram([]), "none")

    def test_read_events_keeps_what_arrived_when_the_last_line_is_truncated(self) -> None:
        complete = events(text=json.dumps(VALID_REVIEW))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            path.write_text(
                "\n".join(json.dumps(event) for event in complete) + '\n{"type":"step_fin',
                encoding="utf-8",
            )
            # A timeout is exactly when the evidence matters, and it leaves half a line.
            self.assertEqual(len(MODULE.read_events(path)), len(complete))


class ReportTest(unittest.TestCase):
    def test_normalizes_a_successful_review(self) -> None:
        report = MODULE.build_report(
            METADATA,
            events(text=json.dumps(VALID_REVIEW)),
            model="kimi-k3",
            provider="gtowizard",
            exit_code=0,
        )
        session = report["session"]
        self.assertEqual(report["source"], "opencode")
        self.assertEqual(report["runner"]["model"], "kimi-k3")
        self.assertEqual(session["status"], "success")
        self.assertFalse(session["is_error"])
        self.assertEqual(session["id"], "ses_009660d3affeLljd62940Foqsr")
        self.assertEqual(session["tools"], ["bash"])
        self.assertEqual(session["finish_reasons"], ["stop"])
        self.assertEqual(session["review"], VALID_REVIEW)
        self.assertEqual(session["review_problems"], [])
        # The raw answer is dropped once it parsed — it is already in `review`.
        self.assertIsNone(session["raw_answer"])
        # No cost key at all: opencode reports 0 for a custom provider, and a zero reads free.
        self.assertNotIn("cost_usd", session)

    def test_records_an_unusable_answer_instead_of_raising(self) -> None:
        """A model that cannot hold the shape is a result, not a crash."""
        for text, expected in (
            ("I reviewed it and it looks fine.", "no JSON review object in the answer"),
            (json.dumps({**VALID_REVIEW, "verdict": "ship it"}), "verdict"),
        ):
            with self.subTest(text=text[:40]):
                report = MODULE.build_report(
                    METADATA, events(text=text), model="kimi-k3", provider="gtowizard", exit_code=0
                )
                session = report["session"]
                self.assertIsNone(session["review"])
                self.assertTrue(session["is_error"])
                self.assertTrue(any(expected in problem for problem in session["review_problems"]))
                # Kept precisely because somebody now has to read it.
                self.assertEqual(session["raw_answer"], text)

    def test_is_an_error_when_opencode_itself_failed(self) -> None:
        report = MODULE.build_report(
            METADATA,
            events(text=json.dumps(VALID_REVIEW)),
            model="kimi-k3",
            provider="gtowizard",
            exit_code=124,
        )
        self.assertTrue(report["session"]["is_error"])
        self.assertEqual(report["session"]["exit_code"], 124)


class SummaryTest(unittest.TestCase):
    def test_escapes_a_finding_that_would_break_the_table(self) -> None:
        finding = {"severity": "blocking", "path": "a.py", "line": 3, "message": "a | b\nsecond line"}
        report = MODULE.build_report(
            METADATA,
            events(text=json.dumps({**VALID_REVIEW, "findings": [finding]})),
            model="kimi-k3",
            provider="gtowizard",
            exit_code=0,
        )
        row = next(line for line in MODULE.summary_markdown(report).splitlines() if "a.py" in line)
        # Four structural pipes for three cells; the finding's own pipe is escaped and its
        # newline does not start a second row.
        self.assertEqual(row.count("|") - row.count("\\|"), 4)
        self.assertIn("a \\| b second line", row)

    def test_states_an_unusable_answer_and_never_claims_a_cost(self) -> None:
        report = MODULE.build_report(
            METADATA, events(text="no json here"), model="kimi-k3", provider="gtowizard", exit_code=0
        )
        summary = MODULE.summary_markdown(report)
        self.assertIn("No usable review", summary)
        self.assertIn("no JSON review object in the answer", summary)
        self.assertIn("spend log", summary)


class ConfigTest(unittest.TestCase):
    """The generated config is the security boundary."""

    def test_carries_no_credential_and_pins_one_model(self) -> None:
        config = MODULE.review_config("kimi-k2.6", provider_id="gtowizard", gateway_url="https://gw.example/v1")
        provider = config["provider"]["gtowizard"]
        self.assertEqual(provider["options"]["apiKey"], "{env:OPENCODE_GATEWAY_API_KEY}")
        self.assertEqual(provider["options"]["baseURL"], "https://gw.example/v1")
        # Only the requested model, so a typo fails loudly instead of resolving to a default.
        self.assertEqual(list(provider["models"]), ["kimi-k2.6"])
        self.assertEqual(config["model"], "gtowizard/kimi-k2.6")

    def test_denies_by_default_everywhere_it_matters(self) -> None:
        config = MODULE.review_config("kimi-k3")
        for where in ("top", "agent"):
            permissions = (
                config["permission"] if where == "top" else config["agent"][MODULE.REVIEW_AGENT]["permission"]
            )
            with self.subTest(where=where):
                self.assertEqual(permissions["*"], "deny")
                self.assertEqual(permissions["read"], "allow")
                # Writes, egress, and subagents are never named as allowed, so `*` catches them.
                for tool in ("edit", "write", "patch", "webfetch", "websearch", "task"):
                    self.assertEqual(permissions.get(tool, "deny"), "deny")
                # bash is an allowlist of read-only git verbs — the one place an instruction
                # injected through a reviewed diff could otherwise reach a mutating command.
                self.assertEqual(permissions["bash"]["*"], "deny")
                self.assertEqual(
                    set(permissions["bash"]) - {"*"},
                    {"git diff*", "git show*", "git log*", "git status*", "git rev-parse*"},
                )

    def test_prompt_carries_the_schema_the_validator_enforces(self) -> None:
        prompt = MODULE.review_prompt("base123", "head456", "Prefer async paths.")
        self.assertIn("git diff base123...head456", prompt)
        for field in MODULE.REVIEW_SCHEMA["required"]:
            self.assertIn(field, prompt)
        for verdict in MODULE.VERDICTS:
            self.assertIn(verdict, prompt)
        self.assertIn("Prefer async paths.", prompt)
        # Classification belongs to the shared classifier; asking twice gives one question
        # two answers.
        self.assertIn("Do not produce them.", prompt)


class WorkspaceGuardTest(unittest.TestCase):
    def test_refuses_a_checkout_that_configures_its_own_reviewer(self) -> None:
        for name in MODULE.FORBIDDEN_CONFIG_PATHS:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / name).mkdir() if name == ".opencode" else (root / name).write_text("{}")
                with self.assertRaises(ValueError) as caught:
                    MODULE.guard_workspace(root)
                self.assertIn(name, str(caught.exception))

    def test_accepts_a_clean_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            MODULE.guard_workspace(Path(directory))


class ProvenanceTest(unittest.TestCase):
    """A caller handed only a number cannot check provenance in an `if:`, so prepare does."""

    def _pull_request(self, head_repository="gto-wizard/gto-brain"):
        return {
            "number": 181,
            "title": "feat(mcp): resolve capabilities from the permissions claim",
            "html_url": "https://github.com/gto-wizard/gto-brain/pull/181",
            "user": {"login": "MilosMosovsky"},
            "head": {"ref": "feat/x", "sha": "4c94b1d", "repo": {"full_name": head_repository}},
            "base": {"ref": "main", "sha": "4f3f9d8"},
            "changed_files": 7,
            "additions": 219,
            "deletions": 10,
        }

    def test_metadata_flattens_the_api_shape(self) -> None:
        metadata = MODULE.pull_request_metadata(
            self._pull_request(), repository="gto-wizard/gto-brain", model="kimi-k3", provider="gtowizard"
        )
        self.assertEqual(metadata["pr_number"], 181)
        self.assertEqual(metadata["base_sha"], "4f3f9d8")
        self.assertEqual(metadata["head_repository"], "gto-wizard/gto-brain")
        self.assertEqual(metadata["model"], "gtowizard/kimi-k3")
        self.assertEqual(metadata["changed_files"], 7)

    def test_prepare_refuses_a_fork(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            environment = {
                "INPUT_PR_NUMBER": "181",
                "INPUT_GITHUB_TOKEN": "t",
                "GITHUB_REPOSITORY": "gto-wizard/gto-brain",
                "RUNNER_TEMP": directory,
                "GITHUB_OUTPUT": str(Path(directory) / "outputs"),
            }
            fork = self._pull_request(head_repository="someone-else/gto-brain")
            with (
                mock.patch.dict(MODULE.os.environ, environment, clear=False),
                mock.patch.object(MODULE, "github_get", return_value=fork),
                mock.patch.object(MODULE.Path, "cwd", return_value=Path(directory)),
                self.assertRaises(ValueError) as caught,
            ):
                MODULE.prepare()
            self.assertIn("someone-else/gto-brain", str(caught.exception))

    def test_main_reports_a_refusal_as_exit_2_rather_than_a_traceback(self) -> None:
        with (
            mock.patch.object(MODULE.sys, "argv", ["opencode_review.py", "prepare"]),
            mock.patch.object(MODULE, "prepare", side_effect=ValueError("nope")),
        ):
            self.assertEqual(MODULE.main(), 2)


if __name__ == "__main__":
    unittest.main()
