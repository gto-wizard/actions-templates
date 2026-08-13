import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))

MODULE_PATH = Path(__file__).with_name("report_agent_run.py")
SPEC = importlib.util.spec_from_file_location("report_agent_run", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

BASE_ENV = {
    "INPUT_TASK": "summarize",
    "INPUT_RUNNER": "opencode",
    "INPUT_MODEL": "gtowizard/kimi-k3",
    "INPUT_STATUS": "success",
    "INPUT_METRICS_ENDPOINT": "http://collector.invalid/v1/metrics",
    "GITHUB_REPOSITORY": "gto-wizard/gto-brain",
    "GITHUB_RUN_ID": "31692337826",
    "INPUT_PR_NUMBER": "182",
    "INPUT_GITHUB_TOKEN": "",
}


def run_with(env: dict[str, str]) -> dict:
    """Run main() with a stubbed transport and return the payload it would have sent."""
    sent: dict = {}
    with mock.patch.dict(os.environ, {**BASE_ENV, **env}, clear=True):
        with mock.patch.object(MODULE, "post_json", side_effect=lambda _e, p: sent.update(p)):
            assert MODULE.main() == 0
    return sent


def points(payload: dict, metric: str) -> list[dict]:
    metrics = payload["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]
    return next((m["gauge"]["dataPoints"] for m in metrics if m["name"] == metric), [])


def labels(payload: dict) -> dict[str, str]:
    point = points(payload, "gto.ai.agent.runs")[0]
    return {a["key"]: list(a["value"].values())[0] for a in point["attributes"]}


class SingleWriterTests(unittest.TestCase):
    def test_task_and_model_land_as_labels(self) -> None:
        got = labels(run_with({}))
        self.assertEqual(got["gto.ai.task"], "summarize")
        self.assertEqual(got["model"], "gtowizard/kimi-k3")
        self.assertEqual(got["gto.review.runner"], "opencode")

    def test_model_keeps_the_runners_spelling(self) -> None:
        # This label is the join key against gateway spend. Rewriting it here to the
        # provider's name is what left every runner-keyed cost column empty.
        self.assertEqual(labels(run_with({"INPUT_MODEL": "gtowizard/kimi-k3"}))["model"], "gtowizard/kimi-k3")

    def test_a_run_is_always_counted(self) -> None:
        # No cost, no tokens, no findings — it still happened. A dashboard that counts a
        # cost metric is really counting runs whose runner knew its own price.
        payload = run_with({"INPUT_STATUS": "timeout", "INPUT_TOKENS": "", "INPUT_DURATION_SECONDS": ""})
        self.assertEqual(len(points(payload, "gto.ai.agent.runs")), 1)
        self.assertEqual(points(payload, "gto.ai.agent.tokens"), [])

    def test_success_is_the_callers_judgement(self) -> None:
        # A run can exit cleanly and answer uselessly; only the task layer knows.
        self.assertEqual(labels(run_with({"INPUT_SUCCESS": "false"}))["review.success"], False)
        self.assertEqual(labels(run_with({}))["review.success"], True)

    def test_unusable_is_reportable_by_a_task_layer(self) -> None:
        got = labels(run_with({"INPUT_STATUS": "unusable", "INPUT_SUCCESS": "false"}))
        self.assertEqual(got["review.status"], "unusable")


class ExtraAttributeTests(unittest.TestCase):
    def test_task_dimensions_are_merged(self) -> None:
        payload = run_with({"INPUT_EXTRA_ATTRIBUTES": json.dumps({"gto.review.verdict": "approve"})})
        self.assertEqual(labels(payload)["gto.review.verdict"], "approve")

    def test_a_task_cannot_redefine_the_shared_set(self) -> None:
        # The shared attributes are what every dashboard groups by. A task layer quietly
        # overwriting `model` or `gto.ai.task` would make two runners incomparable, which is
        # the exact drift the shared builder exists to stop.
        payload = run_with({"INPUT_EXTRA_ATTRIBUTES": json.dumps({"model": "something-else"})})
        self.assertEqual(labels(payload)["model"], "gtowizard/kimi-k3")

    def test_malformed_json_does_not_lose_the_report(self) -> None:
        self.assertEqual(labels(run_with({"INPUT_EXTRA_ATTRIBUTES": "{not json"}))["gto.ai.task"], "summarize")


class DeliveryTests(unittest.TestCase):
    def test_no_endpoint_publishes_nothing_and_still_succeeds(self) -> None:
        with mock.patch.dict(os.environ, {**BASE_ENV, "INPUT_METRICS_ENDPOINT": ""}, clear=True):
            with mock.patch.object(MODULE, "post_json", side_effect=AssertionError("must not send")):
                self.assertEqual(MODULE.main(), 0)

    def test_a_collector_outage_warns_rather_than_fails(self) -> None:
        # A job that did its work and could not phone home has still done its work.
        with mock.patch.dict(os.environ, BASE_ENV, clear=True):
            with mock.patch.object(MODULE, "post_json", side_effect=OSError("connection refused")):
                self.assertEqual(MODULE.main(), 0)


if __name__ == "__main__":
    unittest.main()
