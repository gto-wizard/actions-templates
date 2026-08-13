#!/usr/bin/env python3
"""Publish what the LLM gateway ACTUALLY billed for each AI agent run in CI.

Every agent reports plenty about itself; only Claude's CLI reports a price, and the
price it reports is list, not what we are billed -- measured at 2.1x the booked figure
across two runs. opencode runs report `cost: 0`, because opencode prices from
models.dev which does not know a custom provider, and deriving dollars from their token
counts lands ~4x low (those counts are per-message, not per-session).

So no runner can be trusted to price itself, and the gateway is the only authority. It
already knows: every review request carries
`x-litellm-tags: gto-ai-review,runner:...,model:...,run:<github run id>`, so its spend log
splits per run. This module reads that and republishes it as a metric.

Nothing here is review-shaped: it answers "what did the gateway bill for this CI run",
which is true of any task these runners perform.

WHAT IT EMITS, and why it is a separate name:

    gto.ai.agent.gateway_cost_usd{github_run_id, model, gto_model_provider_id, gto_review_runner}

`model` is the RUNNER's own name for the model -- the same string `gto.ai.agent.runs` reports
-- so the two are one dimension and a dashboard can join on it directly. The gateway's own id
for what it billed rides along as `gto_model_provider_id`, because they genuinely differ
(`gtowizard/kimi-k3` is billed as `moonshotai/kimi-k3`) and both are worth having. Emitting only
the gateway's id, as this did first, split every "by model" panel into two namespaces that
looked like one: the cost column of any table keyed on the runner's name silently came back
empty.

It deliberately does NOT re-emit the reviewers' own label set. This module knows what a run
cost; it does not know what the run was for, and inventing those labels here would give
two owners to the same facts. A dashboard joins on run and model, which is unique on both
sides -- a Claude run bills the reviewer and the classifier separately:

    sum by (vcs_change_ref) (
      gto_ai_agent_gateway_cost_usd
        * on (github_run_id, model) group_left(vcs_change_ref) gto_ai_agent_runs
    )

WHY THE COST IS STAMPED AT EXPORT TIME, NOT RUN TIME:

Attributing each run's cost to when the run happened would make "spend over time" a curve
rather than a spike at each export. It is not possible: Mimir's out-of-order window on this
tenant is 5 minutes, and a back-dated sample is refused outright --

    the sample has been rejected because another sample with a more recent timestamp has
    already been ingested and this sample is beyond the out-of-order time window of 5m
    (err-mimir-sample-out-of-order)

-- so spend necessarily appears at the next export after the run, and the schedule is what
bounds that lag. Do not re-attempt without first widening the window server-side.

CREDENTIAL: a LiteLLM `proxy_admin_viewer` key. Verified: it reads /spend/logs/ui and
/spend/tags, and is refused (403) when it tries to mint a key. The master key must never
be used here -- this job only ever reads.

Runs on in-cluster runners because the OTLP collector is a cluster-local service.
Standard library only, same as the agent actions. The metric NAME comes from
`shared/gto_otlp.py`, so the producer of the number and the producers it is joined
against cannot drift apart.
"""

import contextlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))

from gto_otlp import AGENT_METRIC_GATEWAY_COST, post_json  # noqa: E402

GATEWAY_TIMEOUT = 30
PAGE_SIZE = 100
MAX_PAGES = 50

# LiteLLM stores one row per distinct tag; these are the ones this exporter reads back.
RUN_TAG = "run:"
RUNNER_TAG = "runner:"
MODEL_TAG = "model:"
REVIEW_TAG = "gto-ai-review"


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def get_json(url: str, api_key: str) -> object:
    request = urllib.request.Request(  # noqa: S310 - scheme fixed by caller
        url, headers={"Authorization": f"Bearer {api_key}"}, method="GET"
    )
    with urllib.request.urlopen(request, timeout=GATEWAY_TIMEOUT) as response:  # noqa: S310
        return json.loads(response.read().decode())


def spend_rows(base_url: str, api_key: str, *, since_hours: int) -> list[dict]:
    """Every gateway request in the window, newest pages first, stopping when one is short.

    The window is deliberately wider than the schedule: a review that lands between two
    runs of this job must still be picked up, and re-emitting a gauge with the same labels
    and a later timestamp is idempotent.
    """
    end = datetime.now(UTC)
    start = end - timedelta(hours=since_hours)
    rows: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        query = urllib.parse.urlencode({
            "start_date": start.strftime("%Y-%m-%d %H:%M:%S"),
            "end_date": end.strftime("%Y-%m-%d %H:%M:%S"),
            "page_size": PAGE_SIZE,
            "page": page,
        })
        payload = get_json(f"{base_url}/spend/logs/ui?{query}", api_key)
        batch = (payload or {}).get("data") or [] if isinstance(payload, dict) else []
        rows.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
    return rows


def tags_of(row: dict) -> list[str]:
    tags = row.get("request_tags") or []
    return [t for t in tags if isinstance(t, str)] if isinstance(tags, list) else []


def key_alias(row: dict) -> str:
    """Which API key paid for this request."""
    metadata = row.get("metadata")
    if isinstance(metadata, dict) and metadata.get("user_api_key_alias"):
        return str(metadata["user_api_key_alias"])
    return str(row.get("user") or "")


def coverage_gap(rows: list[dict]) -> tuple[int, float]:
    """Spend on a CI key that no run will ever claim.

    This is the failure mode that would otherwise be invisible. A request whose header did
    not survive is not misattributed -- it is simply absent, so the cost silently reads low
    and every panel still looks plausible.

    Looking for rows tagged `gto-ai-review` but missing `run:` does not find it, because
    both tags come from the same header: when it is absent, so are both, and the row is
    indistinguishable from someone's laptop. What separates them is the KEY -- a CI key is
    used by nothing else -- so the CI keys are learned from the rows that did carry a run id
    rather than hardcoded, and anything else billed to those keys is the gap. Measured at
    $4.83 over three days when this was written, against $24.35 attributed.
    """
    ci_keys = {key_alias(row) for row in rows if any(t.startswith(RUN_TAG) for t in tags_of(row))}
    ci_keys.discard("")
    missing, spend = 0, 0.0
    for row in rows:
        if key_alias(row) not in ci_keys or any(t.startswith(RUN_TAG) for t in tags_of(row)):
            continue
        missing += 1
        with contextlib.suppress(TypeError, ValueError):
            spend += float(row.get("spend") or 0.0)
    return missing, spend


def model_alias(row: dict, tags: list[str], runner: str) -> str:
    """The runner's own name for the model, so this joins to `gto.ai.agent.runs` on `model`.

    Two sources, because one runner can name it per-request and the other cannot:

      opencode  sends one header per review, so `model:gtowizard/kimi-k3` IS the alias.
      claude    sends one header for the whole process, and that process calls two models
                (the reviewer and the classifier). Its tag degrades to `model:claude` --
                the runner, not a model -- and the per-request answer is the gateway's
                `model_group`, which for these is already the runner's spelling
                (`claude-sonnet-5`, `claude-haiku-4.5`).

    So: the tag when it names a model, `model_group` when it only names the runner. Falling
    back to the gateway's billed id keeps a row attributable if both are missing, at the cost
    of it landing in the provider's namespace -- visible, rather than dropped.
    """
    tagged = next((t[len(MODEL_TAG):] for t in tags if t.startswith(MODEL_TAG)), "")
    if tagged and tagged != runner:
        return tagged
    return str(row.get("model_group") or row.get("model") or "unknown")


def by_run_and_model(rows: list[dict]) -> dict[tuple[str, str, str, str], float]:
    """Sum spend per (run, alias, provider model, runner), keeping only tagged review traffic.

    Untagged rows are skipped rather than bucketed as "unknown": this repository's own
    history contains $1.10 of Claude spend from before tagging existed, and folding that
    into any run would silently overstate it. `coverage_gap` reports what that skipping costs.
    """
    totals: dict[tuple[str, str, str, str], float] = defaultdict(float)
    for row in rows:
        tags = tags_of(row)
        run_id = next((t[len(RUN_TAG):] for t in tags if t.startswith(RUN_TAG)), "")
        if not run_id:
            continue
        runner = next((t[len(RUNNER_TAG):] for t in tags if t.startswith(RUNNER_TAG)), "")
        # The gateway's own model id, which is what it priced -- not the runner's alias.
        provider_model = str(row.get("model") or "unknown")
        try:
            spend = float(row.get("spend") or 0.0)
        except (TypeError, ValueError):
            continue
        totals[(run_id, model_alias(row, tags, runner), provider_model, runner)] += spend
    return totals


def metrics_payload(totals: dict[tuple[str, str, str, str], float], *, observed_at_unix_nano: int) -> dict:
    points = [
        {
            "timeUnixNano": str(observed_at_unix_nano),
            "asDouble": spend,
            "attributes": [
                {"key": "github.run.id", "value": {"stringValue": run_id}},
                {"key": "model", "value": {"stringValue": alias}},
                {"key": "gto.model.provider_id", "value": {"stringValue": provider_model}},
                {"key": "gto.review.runner", "value": {"stringValue": runner or "unknown"}},
            ],
        }
        for (run_id, alias, provider_model, runner), spend in sorted(totals.items())
    ]
    return {
        "resourceMetrics": [{
            "resource": {"attributes": [
                {"key": "service.name", "value": {"stringValue": "gto-gateway-spend-export"}},
                {"key": "service.namespace", "value": {"stringValue": "gto-ai"}},
            ]},
            "scopeMetrics": [{
                "scope": {"name": "gto.actions.gateway_spend_export", "version": "1"},
                "metrics": [{
                    "name": AGENT_METRIC_GATEWAY_COST,
                    "description": "What the LLM gateway billed for one AI agent run in CI",
                    "unit": "USD",
                    "gauge": {"dataPoints": points},
                }],
            }],
        }]
    }


def main() -> int:
    api_key = env("LITELLM_SPEND_VIEWER_KEY")
    base_url = env("LITELLM_ADMIN_URL", "https://llm-admin.services.gtowiz.com").rstrip("/")
    endpoint = env("METRICS_ENDPOINT", "http://alloy-alloy-general.alloy.svc.cluster.local:4318/v1/metrics")
    since_hours = int(env("SINCE_HOURS", "48") or "48")
    if not api_key:
        print("::error title=No gateway credential::LITELLM_SPEND_VIEWER_KEY is empty", flush=True)
        return 2

    try:
        rows = spend_rows(base_url, api_key, since_hours=since_hours)
    except (urllib.error.URLError, OSError, ValueError) as error:
        print(f"::error title=Gateway unreachable::{error}", flush=True)
        return 1

    orphans, orphan_spend = coverage_gap(rows)
    if orphans:
        print(
            f"::warning title=Agent spend missing a run id::{orphans} requests "
            f"(${orphan_spend:.4f}) billed to a CI key carry no run:<id>, so their cost "
            f"cannot be attributed and every cost panel reads low by that amount",
            flush=True,
        )

    totals = by_run_and_model(rows)
    if not totals:
        # Not an error: a window with no reviews in it is the normal overnight case.
        print(f"[export] {len(rows)} gateway rows, no tagged agent runs in the last {since_hours}h", flush=True)
        return 0

    payload = metrics_payload(totals, observed_at_unix_nano=time.time_ns())
    try:
        post_json(endpoint, payload)
    except (urllib.error.URLError, OSError, ValueError, RuntimeError) as error:
        print(f"::error title=Telemetry not delivered::{error}", flush=True)
        return 1

    total = sum(totals.values())
    runs = len({run_id for run_id, _, _, _ in totals})
    print(f"[export] {runs} runs, {len(totals)} run/model pairs, ${total:.4f} across {len(rows)} rows",
          flush=True)
    for (run_id, alias, provider_model, runner), spend in sorted(totals.items(), key=lambda item: -item[1])[:20]:
        print(
            f"[export]   run={run_id} runner={runner or '-'} model={alias} "
            f"billed_as={provider_model} spend=${spend:.4f}",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
