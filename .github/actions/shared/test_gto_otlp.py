import importlib.util
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("gto_otlp.py")
SPEC = importlib.util.spec_from_file_location("gto_otlp", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

# The contract both reviewers owe a dashboard. Asserted as an exact set, not a subset: a
# label silently reaching one runner and not the other is the specific defect this module
# exists to prevent -- `vcs.change.ref` shipped on the opencode reviewer and never on the
# Claude one, so filtering by pull request dropped every Claude run without any error.
SHARED_KEYS = {
    "github.repository",
    "github.run.id",
    "github.run.attempt",
    "github.actor",
    "vcs.change.number",
    "vcs.change.ref",
    "vcs.change.title",
    "vcs.change.url",
    "vcs.change.author",
    "vcs.change.files",
    "vcs.ref.head.name",
    "vcs.ref.head.revision",
    "vcs.ref.base.revision",
    "gto.review.runner",
    "gto.ai.task",
    "gto.api_key.alias",
    "gto.code.areas",
    "department",
    "team.id",
    "model",
    "review.status",
    "review.success",
}


def attributes(**overrides):
    base = dict(
        runner="opencode",
        model="gtowizard/kimi-k3",
        repository="gto-wizard/gto-brain",
        change_number=182,
        status="success",
        success=True,
        task="pr_review",
    )
    base.update(overrides)
    return MODULE.agent_attributes(**base)


class ReviewAttributesTest(unittest.TestCase):
    def test_shared_key_set_is_exact(self) -> None:
        self.assertEqual(SHARED_KEYS, set(attributes()))

    def test_change_ref_is_qualified_and_short(self) -> None:
        self.assertEqual("gto-brain#182", attributes()["vcs.change.ref"])

    def test_code_areas_defaults_rather_than_going_blank(self) -> None:
        # An attribute that exists with no value reads as an answer in a dashboard.
        self.assertEqual("repository", attributes(code_areas="")["gto.code.areas"])

    def test_optional_fields_are_typed_not_absent(self) -> None:
        self.assertEqual(0, attributes()["vcs.change.files"])


class ReviewMetricsTest(unittest.TestCase):
    def _names(self, **kwargs):
        metrics = MODULE.agent_metrics(
            attributes(), observed_at_unix_nano=1_700_000_000_000_000_000, **kwargs
        )
        return [m["name"] for m in metrics]

    def test_a_run_always_counts_even_with_nothing_else_to_report(self) -> None:
        # Counting runs must not mean "runs whose runner happened to know its own price".
        self.assertEqual([MODULE.AGENT_METRIC_RUNS], self._names())

    def test_cost_is_absent_rather_than_zero_when_unknown(self) -> None:
        self.assertNotIn(MODULE.AGENT_METRIC_COST, self._names(tokens={"input": 5}))

    def test_zero_cost_is_still_emitted_when_the_runner_reports_it(self) -> None:
        # None means "cannot say"; 0.0 is a claim, and a claim gets published.
        self.assertIn(MODULE.AGENT_METRIC_COST, self._names(cost_usd=0.0))

    def test_both_runners_reach_the_same_names(self) -> None:
        claude = self._names(cost_usd=1.25)
        opencode = self._names(tokens={"input": 1, "output": 2}, findings=3)
        self.assertEqual([MODULE.AGENT_METRIC_RUNS, MODULE.AGENT_METRIC_COST], claude)
        self.assertEqual(
            [MODULE.AGENT_METRIC_RUNS] + [MODULE.AGENT_METRIC_TOKENS] * 2 + [MODULE.AGENT_METRIC_FINDINGS],
            opencode,
        )
        self.assertEqual(MODULE.AGENT_METRIC_RUNS, claude[0])

    def test_duration_is_reported_by_every_runner(self) -> None:
        # The one economic axis on which all five reviewers are comparable today, since
        # only Claude can report a price.
        for kwargs in ({"cost_usd": 1.0}, {"tokens": {"input": 1}, "findings": 0}):
            with self.subTest(**kwargs):
                self.assertIn(MODULE.AGENT_METRIC_DURATION, self._names(duration_seconds=42.5, **kwargs))

    def test_duration_is_a_double_so_sub_second_runs_are_not_floored(self) -> None:
        metrics = MODULE.agent_metrics(attributes(), observed_at_unix_nano=1, duration_seconds=0.75)
        point = next(m for m in metrics if m["name"] == MODULE.AGENT_METRIC_DURATION)
        self.assertEqual(0.75, point["gauge"]["dataPoints"][0]["asDouble"])

    def test_token_kind_rides_as_a_label_not_a_metric_name(self) -> None:
        metrics = MODULE.agent_metrics(
            attributes(),
            observed_at_unix_nano=1,
            tokens={"input": 10, "cache_read": 20},
        )
        kinds = {
            attribute["value"]["stringValue"]
            for metric in metrics
            if metric["name"] == MODULE.AGENT_METRIC_TOKENS
            for point in metric["gauge"]["dataPoints"]
            for attribute in point["attributes"]
            if attribute["key"] == "kind"
        }
        self.assertEqual({"input", "cache_read"}, kinds)

    def test_cost_is_a_double_and_counts_are_ints(self) -> None:
        metrics = MODULE.agent_metrics(
            attributes(), observed_at_unix_nano=1, cost_usd=1.5, findings=2
        )
        by_name = {m["name"]: m["gauge"]["dataPoints"][0] for m in metrics}
        self.assertEqual(1.5, by_name[MODULE.AGENT_METRIC_COST]["asDouble"])
        self.assertEqual("2", by_name[MODULE.AGENT_METRIC_FINDINGS]["asInt"])
        self.assertEqual("1", by_name[MODULE.AGENT_METRIC_RUNS]["asInt"])


class LitellmTagsTest(unittest.TestCase):
    def test_carries_the_run_id_as_the_join_key(self) -> None:
        self.assertEqual(
            "gto-ai-review,runner:opencode,model:gtowizard/kimi-k3,run:99",
            MODULE.litellm_tags(runner="opencode", model="gtowizard/kimi-k3", run_id="99"),
        )

    def test_only_one_tag_family_is_unbounded(self) -> None:
        # LiteLLM stores a row per distinct tag, so anything per-PR or per-attempt here
        # grows that table for reach `run:` already provides.
        tags = MODULE.litellm_tags(runner="claude", model="claude", run_id=1).split(",")
        unbounded = [tag for tag in tags if tag.startswith("run:")]
        self.assertEqual(1, len(unbounded))
        self.assertEqual(4, len(tags))


if __name__ == "__main__":
    unittest.main()
