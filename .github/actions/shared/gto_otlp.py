#!/usr/bin/env python3
"""OTLP/HTTP encoding and transport, plus the schema every AI agent run in CI reports.

What these actions actually do is run an LLM agent on a GitHub runner. Reviewing a pull
request is the first TASK built on that, not the thing itself, so nothing here is named for
it: `gto.ai.agent.*` with a `gto.ai.task` label, rather than `gto.ai.review.*`, which would
have to be renamed the day a second task exists. The task is a dimension; the run is the
concept.

Every producer must agree on the wire or a dashboard comparing them is comparing two things
that only look alike, so the encoding, the transport, the attribute shape and the change
identifier live here once rather than being copied per action.

Consumed by adding this directory to `sys.path`: for a `uses:` reference GitHub checks out
the whole repository into `_actions/<owner>/<repo>/<ref>/`, so a sibling path resolves both
on a runner and in the unit tests.

Standard library only: a runner executes it before any dependency install.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

OTLP_TIMEOUT_SECONDS = 15


def change_ref(repository: str, number: object) -> str:
    """``gto-brain#182`` — a pull-request identifier that survives being put in a dropdown.

    A bare number is ambiguous the moment a second repository runs a review, and that is the
    dimension a dashboard filters on: `pr=182` silently merges gto-brain#182 with
    gto-universe#182 and reports the sum as one pull request's cost. The repository is already
    its own attribute, so this exists purely to be *selectable* — short name, because a
    dropdown of `gto-wizard/gto-brain#182` is mostly the same nine characters over and over.
    """
    return f"{repository.split('/')[-1] or repository}#{number}"


def otel_value(value: object) -> dict[str, object]:
    """One OTLP AnyValue. Booleans before ints, because `bool` is an `int` in Python."""
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    return {"stringValue": str(value)}


def otel_attributes(values: dict[str, object]) -> list[dict[str, object]]:
    return [{"key": key, "value": otel_value(value)} for key, value in values.items()]


def resource_attribute_env(values: dict[str, object]) -> str:
    """Encode `OTEL_RESOURCE_ATTRIBUTES` per the env-var grammar.

    Empty values are dropped rather than sent blank: an attribute that exists with no value
    is worse than an absent one, because it looks like an answer in a dashboard.
    """
    return ",".join(
        f"{key}={quote(str(value), safe='-._~')}" for key, value in values.items() if str(value)
    )


def post_json(endpoint: str, payload: dict[str, Any]) -> None:
    """POST one OTLP/HTTP JSON envelope, raising with the collector's own words on refusal."""
    parsed = urlparse(endpoint)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"refusing to post telemetry to {endpoint!r}")
    request = Request(  # noqa: S310 - scheme asserted above
        endpoint,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=OTLP_TIMEOUT_SECONDS) as response:  # noqa: S310 - same
            if response.status >= 300:
                raise RuntimeError(f"OTLP endpoint {parsed.netloc} returned HTTP {response.status}")
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:500].strip()
        suffix = f": {detail}" if detail else ""
        raise RuntimeError(f"OTLP endpoint {parsed.netloc} returned HTTP {error.code}{suffix}") from error


def gauge_metric(
    name: str,
    *,
    description: str,
    unit: str,
    value: float | int,
    attributes: dict[str, object],
    observed_at_unix_nano: int,
) -> dict[str, Any]:
    """A single-sample gauge. One run is one point; a counter would imply a rate nobody wants."""
    point: dict[str, Any] = {
        "timeUnixNano": str(observed_at_unix_nano),
        "attributes": otel_attributes(attributes),
    }
    if isinstance(value, float):
        point["asDouble"] = value
    else:
        point["asInt"] = str(value)
    return {
        "name": name,
        "description": description,
        "unit": unit,
        "gauge": {"dataPoints": [point]},
    }


def metrics_envelope(
    resource: dict[str, object], scope_name: str, metrics: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "resourceMetrics": [
            {
                "resource": {"attributes": otel_attributes(resource)},
                "scopeMetrics": [{"scope": {"name": scope_name, "version": "1"}, "metrics": metrics}],
            }
        ]
    }


def log_envelope(
    resource: dict[str, object],
    scope_name: str,
    *,
    body: str,
    attributes: dict[str, object],
    observed_at_unix_nano: int,
    trace_id: str = "",
    span_id: str = "",
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "timeUnixNano": str(observed_at_unix_nano),
        "observedTimeUnixNano": str(observed_at_unix_nano),
        "severityNumber": 9,
        "severityText": "INFO",
        "body": {"stringValue": body},
        "attributes": otel_attributes(attributes),
    }
    if trace_id:
        record["traceId"] = trace_id
    if span_id:
        record["spanId"] = span_id
    return {
        "resourceLogs": [
            {
                "resource": {"attributes": otel_attributes(resource)},
                "scopeLogs": [{"scope": {"name": scope_name, "version": "1"}, "logRecords": [record]}],
            }
        ]
    }


def span_envelope(
    resource: dict[str, object],
    scope_name: str,
    *,
    name: str,
    trace_id: str,
    span_id: str,
    start_unix_nano: int,
    end_unix_nano: int,
    attributes: dict[str, object],
    failed: bool,
) -> dict[str, Any]:
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": otel_attributes(resource)},
                "scopeSpans": [
                    {
                        "scope": {"name": scope_name, "version": "1"},
                        "spans": [
                            {
                                "traceId": trace_id,
                                "spanId": span_id,
                                "name": name,
                                "kind": 1,
                                "startTimeUnixNano": str(start_unix_nano),
                                "endTimeUnixNano": str(end_unix_nano),
                                "attributes": otel_attributes(attributes),
                                # 2 is ERROR, 1 is OK. A failed review is a failed span, so a
                                # trace search for errors finds it without reading attributes.
                                "status": {"code": 2 if failed else 1},
                            }
                        ],
                    }
                ],
            }
        ]
    }


# --- the agent-run schema ------------------------------------------------------------------
#
# One name per concept, emitted by every agent run whatever CLI it wraps and whatever task
# it performs. Before this, the
# Claude action emitted `gto.claude.pr_review.cost_usd` and the opencode action emitted
# `gto.opencode.pr_review.findings`, which meant "a review ran" had two answers and a
# dashboard could only count runs by unioning two metric names — and any label added to one
# family silently failed to appear on the other. The runner is a *label*, not a name.

AGENT_METRIC_RUNS = "gto.ai.agent.runs"
AGENT_METRIC_COST = "gto.ai.agent.cost_usd"
AGENT_METRIC_TOKENS = "gto.ai.agent.tokens"
AGENT_METRIC_FINDINGS = "gto.ai.agent.findings"
AGENT_METRIC_DURATION = "gto.ai.agent.duration_seconds"
# Owned here rather than by the exporter that emits it: the exporter publishes this
# name, these actions publish the rest, and one module deciding all of them is the
# only reason a dashboard can join them.
AGENT_METRIC_GATEWAY_COST = "gto.ai.agent.gateway_cost_usd"

# --- the outcome vocabulary -------------------------------------------------------------
#
# Closed and shared, so two runners cannot describe the same ending with different words.
# The distinction that matters is WHOSE fault an ending is, because a dashboard comparing
# models must exclude the endings that say nothing about a model:
#
#   success   the agent ran and produced a usable answer
#   unusable  it answered, in the wrong shape          <- the model's fault
#   error     it ran and broke                         <- the run's fault
#   timeout   it ran and never converged               <- the run's fault
#   cancelled it was taken away mid-flight             <- nobody's fault; a re-push
#   rejected  it was never admitted at all             <- nobody's fault; the gateway said no
#
# `rejected` exists because a 429 for an exhausted key was being recorded as a review that
# failed. No model was involved: the request did not reach one. Recording that as a failure
# defames every model on the panel and hides an operational problem (a dead key) inside a
# quality signal.
STATUS_SUCCESS = "success"
STATUS_UNUSABLE = "unusable"
STATUS_ERROR = "error"
STATUS_TIMEOUT = "timeout"
STATUS_CANCELLED = "cancelled"
STATUS_REJECTED = "rejected"

# HTTP statuses where the gateway refused to serve the request rather than a model failing
# it: auth, payment/budget, forbidden, and rate/quota limits.
GATEWAY_REJECTION_STATUSES = frozenset({401, 402, 403, 429})


def is_gateway_rejection(http_status: object) -> bool:
    """True when the gateway refused the request outright, so no model ever saw it."""
    try:
        return int(http_status) in GATEWAY_REJECTION_STATUSES
    except (TypeError, ValueError):
        return False


def agent_attributes(
    *,
    runner: str,
    model: str,
    task: str,
    repository: str,
    change_number: object,
    status: str,
    success: bool,
    change_title: str = "",
    change_url: str = "",
    change_author: str = "",
    changed_files: object = 0,
    head_ref: str = "",
    head_revision: str = "",
    base_revision: str = "",
    run_id: object = "",
    run_attempt: object = "",
    actor: str = "",
    api_key_alias: str = "",
    code_areas: str = "",
    department: str = "",
    team_id: str = "",
) -> dict[str, object]:
    """The dimensions EVERY agent run carries, whichever CLI produced it and whatever it did.

    This is the set a dashboard filters and groups on, so it lives in exactly one place.
    A runner with extra facts (a classification, a verdict) merges them on top of this —
    what it must not do is spell one of *these* differently, which is precisely what
    happened while each action owned its own copy: `vcs.change.ref` reached the opencode
    reviewer and never reached the Claude one, so filtering by pull request dropped every
    Claude run without saying so.
    """
    return {
        # The job this agent was asked to do. A label, never part of a metric name: the
        # runners are general LLM inference in CI and pr_review is simply the first task,
        # so baking it into a name guarantees a rename the day a second one lands.
        "gto.ai.task": task,
        "github.repository": repository,
        "github.run.id": run_id,
        "github.run.attempt": run_attempt,
        "github.actor": actor,
        "vcs.change.number": change_number,
        "vcs.change.ref": change_ref(repository, change_number),
        "vcs.change.title": change_title,
        "vcs.change.url": change_url,
        "vcs.change.author": change_author,
        "vcs.change.files": changed_files or 0,
        "vcs.ref.head.name": head_ref,
        "vcs.ref.head.revision": head_revision,
        "vcs.ref.base.revision": base_revision,
        "gto.review.runner": runner,
        "gto.api_key.alias": api_key_alias,
        "gto.code.areas": code_areas or "repository",
        "department": department,
        "team.id": team_id,
        "model": model,
        "review.status": status,
        "review.success": success,
    }


def agent_metrics(
    attributes: dict[str, object],
    *,
    observed_at_unix_nano: int,
    cost_usd: float | None = None,
    tokens: dict[str, int] | None = None,
    findings: int | None = None,
    duration_seconds: float | None = None,
) -> list[dict[str, Any]]:
    """Every gauge one completed review contributes.

    `runs` is unconditional and is the series to count: a review that produced no cost, no
    tokens and no findings still happened, and a dashboard that counts a *cost* metric is
    really counting "reviews whose runner happened to know its own price".

    The other three are absent when the runner cannot report them. Absent, not zero — a
    zero dollar review reads as free, which is the specific lie this whole exercise exists
    to avoid.
    """
    metrics = [
        gauge_metric(
            AGENT_METRIC_RUNS,
            description="One completed AI pull-request review",
            unit="{run}",
            value=1,
            attributes=attributes,
            observed_at_unix_nano=observed_at_unix_nano,
        )
    ]
    if cost_usd is not None:
        metrics.append(
            gauge_metric(
                AGENT_METRIC_COST,
                description="Exact cost of one AI pull-request review, as reported by its runner",
                unit="USD",
                value=float(cost_usd),
                attributes=attributes,
                observed_at_unix_nano=observed_at_unix_nano,
            )
        )
    for kind, value in (tokens or {}).items():
        metrics.append(
            gauge_metric(
                AGENT_METRIC_TOKENS,
                description="Tokens billed by one AI pull-request review, by kind",
                unit="{token}",
                value=int(value),
                attributes={**attributes, "kind": kind},
                observed_at_unix_nano=observed_at_unix_nano,
            )
        )
    if duration_seconds is not None:
        # Wall clock for the whole wrapped review. Unlike cost, EVERY runner can report this,
        # so it is the one economic axis on which all five reviewers are directly comparable
        # today -- which is exactly why it is worth emitting rather than leaving in a span.
        metrics.append(
            gauge_metric(
                AGENT_METRIC_DURATION,
                description="Wall-clock seconds of one AI pull-request review",
                unit="s",
                value=float(duration_seconds),
                attributes=attributes,
                observed_at_unix_nano=observed_at_unix_nano,
            )
        )
    if findings is not None:
        metrics.append(
            gauge_metric(
                AGENT_METRIC_FINDINGS,
                description="Findings reported by one AI pull-request review",
                unit="{finding}",
                value=int(findings),
                attributes=attributes,
                observed_at_unix_nano=observed_at_unix_nano,
            )
        )
    return metrics


def litellm_tags(*, runner: str, model: str, run_id: object) -> str:
    """The `x-litellm-tags` value that makes gateway spend attributable to this run.

    Without it every reviewer's requests land in one `User-Agent: opencode` bucket and the
    money cannot be split by pull request, model or run — measured: $10.96 of real spend,
    unattributable. `run:<id>` is the join key back to the metrics above.

    Deliberately four tags, only one of them unbounded: LiteLLM stores a row per distinct
    tag, so `ref:<repo#number>` would add a second unbounded family for no extra reach that
    `run:<id>` does not already give via the run's own telemetry.
    """
    return ",".join(("gto-ai-review", f"runner:{runner}", f"model:{model}", f"run:{run_id}"))


def new_trace_ids(random_bytes) -> tuple[str, str]:
    """A fresh ``(trace_id, span_id)`` pair, hex, OTLP-sized.

    `random_bytes` is injected so a test can be deterministic without patching a module. The
    Claude action keeps its own trace context builder: it must also render a `traceparent` to
    hand to the CLI so Claude's native spans join the same trace, which is a job this pair
    does not do.
    """
    return random_bytes(16).hex(), random_bytes(8).hex()
