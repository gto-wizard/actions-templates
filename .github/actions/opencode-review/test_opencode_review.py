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

    def test_prefers_the_review_over_an_echoed_schema(self) -> None:
        """What `deepseek-v4-pro` actually did on its second real run.

        It answered correctly and then echoed the requested schema after it, so the last
        object in the text was the schema's own `properties` block — which has a `summary`
        key and is not a review. Keying on "has a summary" threw away a good review.
        """
        review = json.dumps(VALID_REVIEW)
        schema_echo = json.dumps(MODULE.REVIEW_SCHEMA)
        found = MODULE.extract_review(f"Here is the review.\n{review}\n\nSchema used:\n{schema_echo}")
        self.assertIsNotNone(found)
        self.assertEqual(found["verdict"], VALID_REVIEW["verdict"])
        self.assertEqual(found["summary"], VALID_REVIEW["summary"])

    def test_falls_back_to_an_attempt_so_problems_name_real_content(self) -> None:
        # Nothing validates, so the caller still gets the model's own object and can report
        # what was wrong with it rather than "no review found".
        broken = json.dumps({**VALID_REVIEW, "verdict": "ship it"})
        found = MODULE.extract_review(f"prose {broken} more prose")
        self.assertIsNotNone(found)
        self.assertEqual(found["verdict"], "ship it")
        self.assertTrue(MODULE.validate_review(found))

    def test_returns_none_rather_than_a_fragment(self) -> None:
        # A findings entry is an object too, and it validates as neither a review nor an
        # attempt at one — nothing here should be mistaken for an answer.
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


class ExitCodeWiringTest(unittest.TestCase):
    """`report()` reads the run step's exit code from the environment, and an ABSENT one is
    the cancellation signal -- a composite action cannot call `cancelled()`, so the killed
    step's silence is what carries it."""

    def _status_for(self, reported_exit_code: str) -> str:
        with tempfile.TemporaryDirectory() as directory:
            artifact_dir = Path(directory)
            metadata_file = artifact_dir / "metadata.json"
            metadata_file.write_text(json.dumps(METADATA), encoding="utf-8")
            execution_file = artifact_dir / "events.jsonl"
            execution_file.write_text(
                "\n".join(json.dumps(event) for event in events(text=json.dumps(VALID_REVIEW))),
                encoding="utf-8",
            )
            environment = {
                "INPUT_ARTIFACT_DIR": str(artifact_dir),
                "INPUT_METADATA_FILE": str(metadata_file),
                "INPUT_EXECUTION_FILE": str(execution_file),
                "INPUT_EXIT_CODE": reported_exit_code,
                "INPUT_MODEL": "kimi-k3",
                "INPUT_PROVIDER_ID": "gtowizard",
                "INPUT_METRICS_ENDPOINT": "",
                "INPUT_LOGS_ENDPOINT": "",
                "INPUT_TRACES_ENDPOINT": "",
                "GITHUB_OUTPUT": str(artifact_dir / "outputs"),
                "GITHUB_STEP_SUMMARY": str(artifact_dir / "summary.md"),
            }
            with mock.patch.dict(MODULE.os.environ, environment, clear=False):
                MODULE.report()
            written = json.loads((artifact_dir / "opencode-run-report.json").read_text())
            return written["session"]["status"]

    def test_an_absent_exit_code_reads_as_cancelled_not_as_a_bad_model(self) -> None:
        # Regression: this used to arrive as a defaulted `1`, so every re-push recorded
        # four models as having failed to answer.
        self.assertEqual("cancelled", self._status_for(""))

    def test_a_reported_exit_code_is_honoured_verbatim(self) -> None:
        for reported, expected in (("0", "success"), ("1", "error"), ("124", "timeout")):
            with self.subTest(exit_code=reported):
                self.assertEqual(expected, self._status_for(reported))


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
        """A timed-out run is an error even if a parseable answer arrived earlier in its stream."""
        report = MODULE.build_report(
            METADATA,
            events(text=json.dumps(VALID_REVIEW)),
            model="kimi-k3",
            provider="gtowizard",
            exit_code=124,
        )
        self.assertTrue(report["session"]["is_error"])
        self.assertEqual(report["session"]["exit_code"], 124)


class RunStatusTest(unittest.TestCase):
    """A model failing and a run being taken away from it are different facts."""

    def test_separates_the_four_outcomes(self) -> None:
        cases = (
            ({"exit_code": 0, "valid": True, "cancelled": False}, "success"),
            # The model answered in the wrong shape — its fault, and a distinct outcome from
            # opencode exiting non-zero.
            ({"exit_code": 0, "valid": False, "cancelled": False}, "unusable"),
            ({"exit_code": 1, "valid": False, "cancelled": False}, "error"),
            ({"exit_code": 124, "valid": False, "cancelled": False}, "timeout"),
            # Cancellation wins over the exit code, because the exit code is the default 1 the
            # report falls back to when the review step never got to write one.
            ({"exit_code": 1, "valid": False, "cancelled": True}, "cancelled"),
            ({"exit_code": 0, "valid": True, "cancelled": True}, "cancelled"),
        )
        for kwargs, expected in cases:
            with self.subTest(**kwargs):
                self.assertEqual(MODULE.run_status(**kwargs), expected)

    def test_a_cancelled_run_does_not_blame_the_model(self) -> None:
        """Observed: a re-push cancelled four reviewers and all four recorded `unusable`."""
        report = MODULE.build_report(
            METADATA,
            events(text="I was still working when"),
            model="glm-5.2",
            provider="gtowizard",
            exit_code=1,
            cancelled=True,
        )
        session = report["session"]
        self.assertEqual(session["status"], "cancelled")
        self.assertEqual(session["review_problems"], ["the run was cancelled before the model finished answering"])
        with mock.patch.dict(MODULE.os.environ, {}, clear=False):
            attributes = MODULE.telemetry_attributes(report)
        # Not "unusable": that would be a fake data point in the model comparison.
        self.assertEqual(attributes["gto.review.verdict"], "cancelled")
        self.assertIs(attributes["review.success"], False)


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

    def test_tags_every_request_so_gateway_spend_is_attributable(self) -> None:
        # Without this the run's dollars pool into one `User-Agent: opencode` bucket that
        # cannot be split by pull request, model or run -- measured at $10.96 unattributed.
        config = MODULE.review_config("kimi-k3", provider_id="gtowizard", run_id="31633374870")
        headers = config["provider"]["gtowizard"]["options"]["headers"]
        self.assertEqual(
            "gto-ai-review,runner:opencode,model:gtowizard/kimi-k3,run:31633374870",
            headers["x-litellm-tags"],
        )

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


class TelemetryTest(unittest.TestCase):
    """Four of five reviewers were invisible in Grafana until this existed."""

    def _report(self, **overrides):
        report = MODULE.build_report(
            {**METADATA, "run_id": "31626101260", "run_attempt": "2", "head_ref": "feat/x"},
            events(text=json.dumps(VALID_REVIEW)),
            model="kimi-k3",
            provider="gtowizard",
            exit_code=0,
        )
        report["session"].update(overrides)
        return report

    def test_carries_a_pull_request_reference_that_cannot_collide(self) -> None:
        attributes = MODULE.telemetry_attributes(self._report())
        # The whole point: `181` alone would merge two repositories in one dashboard filter.
        self.assertEqual(attributes["vcs.change.ref"], "gto-brain#181")
        self.assertEqual(attributes["vcs.change.number"], 181)
        self.assertNotEqual("gto-universe#181", attributes["vcs.change.ref"])

    def test_dimensions_match_the_claude_action_so_runners_are_comparable(self) -> None:
        attributes = MODULE.telemetry_attributes(self._report())
        for shared in (
            "github.repository",
            "github.run.id",
            "vcs.change.ref",
            "vcs.change.author",
            "gto.api_key.alias",
            "department",
            "team.id",
            "model",
            "review.status",
        ):
            self.assertIn(shared, attributes)
        # Runner is what tells the two apart in a shared panel.
        self.assertEqual(attributes["gto.review.runner"], "opencode")
        # And cost is absent on purpose: opencode reports 0 for a custom provider.
        self.assertNotIn("gto.review.cost_usd", attributes)

    def test_an_unusable_review_is_still_reported_as_a_dimension(self) -> None:
        # Built from a real unusable answer rather than by hand-setting the status, so the
        # fallback chain review -> status is actually exercised.
        report = MODULE.build_report(
            {**METADATA, "run_id": "1", "run_attempt": "1"},
            events(text="I looked at it and it seems fine to me."),
            model="kimi-k3",
            provider="gtowizard",
            exit_code=0,
        )
        attributes = MODULE.telemetry_attributes(report)
        self.assertEqual(report["session"]["status"], "unusable")
        self.assertEqual(attributes["gto.review.verdict"], "unusable")
        self.assertIs(attributes["review.success"], False)

    def test_a_dead_collector_warns_and_never_raises(self) -> None:
        environment = {
            "INPUT_METRICS_ENDPOINT": "http://127.0.0.1:1/v1/metrics",
            "INPUT_LOGS_ENDPOINT": "",
            "INPUT_TRACES_ENDPOINT": "",
        }
        with mock.patch.dict(MODULE.os.environ, environment, clear=False):
            failures = MODULE.emit_telemetry(
                self._report(), observed_at_unix_nano=1_700_000_000_000_000_000, duration_nanos=1
            )
        # Reporting never decides whether a review passed.
        self.assertEqual(len(failures), 1)
        self.assertTrue(failures[0].startswith("metrics:"))

    def test_skips_a_signal_whose_endpoint_is_empty(self) -> None:
        with mock.patch.dict(
            MODULE.os.environ,
            {"INPUT_METRICS_ENDPOINT": "", "INPUT_LOGS_ENDPOINT": "", "INPUT_TRACES_ENDPOINT": ""},
            clear=False,
        ):
            self.assertEqual(
                MODULE.emit_telemetry(
                    self._report(), observed_at_unix_nano=1_700_000_000_000_000_000, duration_nanos=1
                ),
                [],
            )

    def test_tokens_are_one_metric_with_a_kind_rather_than_four_names(self) -> None:
        posted = []
        with (
            mock.patch.dict(
                MODULE.os.environ,
                {
                    "INPUT_METRICS_ENDPOINT": "http://collector/v1/metrics",
                    "INPUT_LOGS_ENDPOINT": "",
                    "INPUT_TRACES_ENDPOINT": "",
                },
                clear=False,
            ),
            mock.patch.object(MODULE, "post_json", lambda endpoint, payload: posted.append(payload)),
        ):
            MODULE.emit_telemetry(
                self._report(), observed_at_unix_nano=1_700_000_000_000_000_000, duration_nanos=1
            )
        metrics = posted[0]["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]
        names = {m["name"] for m in metrics}
        # Runner-neutral names, shared with the Claude action: the runner is a label, so
        # counting reviews never needs to union two metric families again.
        self.assertEqual(
            names,
            {"gto.ai.review.runs", "gto.ai.review.tokens", "gto.ai.review.findings"},
        )
        kinds = {
            attribute["value"]["stringValue"]
            for m in metrics
            if m["name"] == "gto.ai.review.tokens"
            for point in m["gauge"]["dataPoints"]
            for attribute in point["attributes"]
            if attribute["key"] == "kind"
        }
        self.assertEqual(kinds, {"input", "output", "reasoning", "cache_read"})


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
