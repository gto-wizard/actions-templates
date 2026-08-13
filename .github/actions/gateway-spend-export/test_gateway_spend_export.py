import importlib.util
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))

MODULE_PATH = Path(__file__).with_name("gateway_spend_export.py")
SPEC = importlib.util.spec_from_file_location("gateway_spend_export", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

CI_KEY = "gto-brain-opencode-review-tmp"


def row(*, tags: list[str], model: str, model_group: str = "", spend: float = 1.0, key: str = CI_KEY) -> dict:
    return {
        "request_tags": tags,
        "model": model,
        "model_group": model_group,
        "spend": spend,
        "metadata": {"user_api_key_alias": key},
    }


class ModelAliasTests(unittest.TestCase):
    """The label that decides whether cost joins to a run, or silently does not.

    Emitting only the gateway's billed id is what left every runner-keyed cost column empty:
    `gto_ai_agent_runs` says `gtowizard/kimi-k3`, the gateway says `moonshotai/kimi-k3`, and a
    join on `model` matches neither way round while both panels still render.
    """

    def test_opencode_tag_is_the_alias(self) -> None:
        alias = MODULE.model_alias(
            row(tags=["model:gtowizard/kimi-k3"], model="moonshotai/kimi-k3", model_group="kimi-k3"),
            ["model:gtowizard/kimi-k3"],
            "opencode",
        )
        self.assertEqual(alias, "gtowizard/kimi-k3")

    def test_claude_tag_names_the_runner_so_model_group_wins(self) -> None:
        # One header covers the whole Claude process, which calls two models; its `model:` tag
        # degrades to the runner's name. Trusting it would label the classifier's spend
        # "claude" and merge it with the reviewer's.
        for group, billed in (
            ("claude-sonnet-5", "anthropic/claude-sonnet-5"),
            ("claude-haiku-4.5", "anthropic/claude-haiku-4-5-20251001"),
        ):
            with self.subTest(group):
                alias = MODULE.model_alias(
                    row(tags=["model:claude"], model=billed, model_group=group),
                    ["model:claude"],
                    "claude",
                )
                self.assertEqual(alias, group)

    def test_a_run_bills_its_two_models_separately(self) -> None:
        # (run, model) must be unique on this side for the dashboard's join to be legal.
        totals = MODULE.by_run_and_model([
            row(tags=["run:1", "runner:claude", "model:claude"],
                model="anthropic/claude-sonnet-5", model_group="claude-sonnet-5", spend=0.40),
            row(tags=["run:1", "runner:claude", "model:claude"],
                model="anthropic/claude-haiku-4-5-20251001", model_group="claude-haiku-4.5", spend=0.08),
        ])
        self.assertEqual(
            {(run, alias): spend for (run, alias, _, _), spend in totals.items()},
            {("1", "claude-sonnet-5"): 0.40, ("1", "claude-haiku-4.5"): 0.08},
        )

    def test_missing_both_sources_still_attributes_the_row(self) -> None:
        alias = MODULE.model_alias(row(tags=[], model="moonshotai/kimi-k3"), [], "opencode")
        self.assertEqual(alias, "moonshotai/kimi-k3")


class CoverageGapTests(unittest.TestCase):
    """Under-counted spend is invisible by construction -- every panel still looks plausible."""

    def test_untagged_spend_on_a_ci_key_is_counted(self) -> None:
        # Both tags come from one header, so a request that loses it has neither. Only the
        # key tells it apart from someone's laptop.
        count, spend = MODULE.coverage_gap([
            row(tags=["gto-ai-review", "runner:opencode", "run:31686385051"], model="m", spend=2.0),
            row(tags=["User-Agent: opencode"], model="m", spend=0.75),
        ])
        self.assertEqual((count, spend), (1, 0.75))

    def test_other_keys_are_not_our_gap(self) -> None:
        count, spend = MODULE.coverage_gap([
            row(tags=["run:1", "runner:opencode"], model="m", spend=2.0),
            row(tags=["User-Agent: opencode"], model="m", spend=9.0, key="someones-laptop"),
        ])
        self.assertEqual((count, spend), (0, 0.0))

    def test_no_ci_traffic_means_no_gap(self) -> None:
        count, spend = MODULE.coverage_gap([row(tags=["User-Agent: opencode"], model="m", spend=5.0)])
        self.assertEqual((count, spend), (0, 0.0))


class PayloadTests(unittest.TestCase):
    def test_both_model_names_are_emitted(self) -> None:
        payload = MODULE.metrics_payload(
            {("31686385051", "gtowizard/kimi-k3", "moonshotai/kimi-k3", "opencode"): 0.07},
            observed_at_unix_nano=1,
        )
        points = payload["resourceMetrics"][0]["scopeMetrics"][0]["metrics"][0]["gauge"]["dataPoints"]
        attributes = {a["key"]: a["value"]["stringValue"] for a in points[0]["attributes"]}
        self.assertEqual(attributes["model"], "gtowizard/kimi-k3")
        self.assertEqual(attributes["gto.model.provider_id"], "moonshotai/kimi-k3")
        self.assertEqual(attributes["github.run.id"], "31686385051")
        self.assertEqual(attributes["gto.review.runner"], "opencode")


if __name__ == "__main__":
    unittest.main()
