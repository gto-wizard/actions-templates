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

    gto.ai.agent.gateway_cost_usd{github_run_id, model, gto_review_runner}

It deliberately does NOT re-emit the reviewers' own label set. This module knows what a run
cost; it does not know what the run was for, and inventing those labels here would give
two owners to the same facts. A dashboard joins on `github_run_id`:

    sum by (vcs_change_ref) (
      gto_ai_agent_gateway_cost_usd
        * on (github_run_id) group_left(vcs_change_ref) gto_ai_agent_runs
    )

CREDENTIAL: a LiteLLM `proxy_admin_viewer` key. Verified: it reads /spend/logs/ui and
/spend/tags, and is refused (403) when it tries to mint a key. The master key must never
be used here -- this job only ever reads.

Runs on in-cluster runners because the OTLP collector is a cluster-local service.
Standard library only, same as the agent actions. The metric NAME comes from
`shared/gto_otlp.py`, so the producer of the number and the producers it is joined
against cannot drift apart.
"""

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


def coverage_gap(rows: list[dict]) -> tuple[int, float]:
    """Review traffic that lost its run id: rows tagged `gto-ai-review` but not `run:`.

    This is the failure mode that would otherwise be invisible. A request whose header
    did not survive is not misattributed -- it is simply absent, so the cost silently
    reads low and every panel still looks plausible. Counting it here turns a silent
    under-count into a warning.
    """
    missing, spend = 0, 0.0
    for row in rows:
        tags = row.get("request_tags") or []
        if not isinstance(tags, list) or REVIEW_TAG not in tags:
            continue
        if any(isinstance(t, str) and t.startswith(RUN_TAG) for t in tags):
            continue
        missing += 1
        try:
            spend += float(row.get("spend") or 0.0)
        except (TypeError, ValueError):
            pass
    return missing, spend


def by_run_and_model(rows: list[dict]) -> dict[tuple[str, str, str], float]:
    """Sum spend per (run, model, runner), keeping only tagged review traffic.

    Untagged rows are skipped rather than bucketed as "unknown": this repository's own
    history contains $1.10 of Claude spend from before tagging existed, and folding that
    into any run would silently overstate it.
    """
    totals: dict[tuple[str, str, str], float] = defaultdict(float)
    for row in rows:
        tags = row.get("request_tags") or []
        if not isinstance(tags, list):
            continue
        run_id = next((t[len(RUN_TAG):] for t in tags if isinstance(t, str) and t.startswith(RUN_TAG)), "")
        if not run_id:
            continue
        runner = next(
            (t[len(RUNNER_TAG):] for t in tags if isinstance(t, str) and t.startswith(RUNNER_TAG)), ""
        )
        # The gateway's own model id, which is what it priced -- not the reviewer's alias.
        model = str(row.get("model") or "unknown")
        try:
            spend = float(row.get("spend") or 0.0)
        except (TypeError, ValueError):
            continue
        totals[(run_id, model, runner)] += spend
    return totals


def metrics_payload(totals: dict[tuple[str, str, str], float], *, observed_at_unix_nano: int) -> dict:
    points = [
        {
            "timeUnixNano": str(observed_at_unix_nano),
            "asDouble": spend,
            "attributes": [
                {"key": "github.run.id", "value": {"stringValue": run_id}},
                {"key": "model", "value": {"stringValue": model}},
                {"key": "gto.review.runner", "value": {"stringValue": runner or "unknown"}},
            ],
        }
        for (run_id, model, runner), spend in sorted(totals.items())
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
            f"::warning title=Agent spend missing a run id::{orphans} tagged agent "
            f"requests (${orphan_spend:.4f}) carry no run:<id>, so their cost cannot be "
            f"attributed and every cost panel reads low by that amount",
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
    runs = len({run_id for run_id, _, _ in totals})
    print(f"[export] {runs} runs, {len(totals)} run/model pairs, ${total:.4f} across {len(rows)} rows",
          flush=True)
    for (run_id, model, runner), spend in sorted(totals.items(), key=lambda item: -item[1])[:20]:
        print(f"[export]   run={run_id} runner={runner or '-'} model={model} spend=${spend:.4f}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
