#!/usr/bin/env python3
"""Publish one AI agent run to OTLP. The ONLY thing that writes `gto.ai.agent.*`.

Single writer, on purpose. While each runner emitted its own copy of this metric family they
drifted: `vcs.change.ref` reached the opencode reviewer and never reached the Claude one, so
filtering a dashboard by pull request silently dropped every Claude run. The shared attribute
builder in `gto_otlp` fixed the SPELLING; this action fixes the number of authors.

It is also what makes the runner layer generic. `opencode-run` produces text and numbers and
knows nothing about telemetry; a task layer knows what the run meant. Both hand their facts
here, and one series comes out with both sets of labels on it — rather than two series that a
dashboard has to join and, as happened, cannot join unambiguously.

    runner facts    status, duration, tokens        (from `opencode-run`)
    task facts      verdict, findings, complexity   (from whatever asked for the run)
    CI facts        repository, run id, actor       (from the environment)
    change facts    pull request title, author      (resolved here, from `pr-number`)

`extra-attributes` is how a task layer adds its own dimensions without this action having to
learn about them. Keep them low-cardinality: every distinct combination is a time series.

CREDENTIAL: none. This reads a public-ish PR through the caller's `github-token` and writes
to a collector. It never sees a gateway key.

Standard library only: a runner executes it before any dependency install.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))

from gto_otlp import (  # noqa: E402
    STATUS_SUCCESS,
    agent_attributes,
    agent_metrics,
    metrics_envelope,
    post_json,
)

GITHUB_API = "https://api.github.com"
GITHUB_API_TIMEOUT = 15.0
SCOPE_NAME = "gto.actions.report_agent_run"


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def github_get(path: str, token: str) -> Any:
    request = urllib.request.Request(  # noqa: S310 - host is a constant
        f"{GITHUB_API}{path}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=GITHUB_API_TIMEOUT) as response:  # noqa: S310
        return json.loads(response.read().decode())


def change_facts(*, repository: str, pr_number: str, token: str) -> dict[str, object]:
    """What the run was about, resolved once here rather than by every task layer.

    Best-effort by design. A failure to describe the change must not cost the measurement of
    the run: the numbers are the point, the title is decoration. Losing telemetry because
    the API was briefly unavailable is the failure mode this whole module exists to prevent.
    """
    if not (pr_number and token and repository):
        return {}
    try:
        pull = github_get(f"/repos/{repository}/pulls/{pr_number}", token)
    except (urllib.error.URLError, OSError, ValueError, KeyError) as error:
        print(f"::warning title=Change details unavailable::{error}", flush=True)
        return {}
    head = pull.get("head") or {}
    base = pull.get("base") or {}
    user = pull.get("user") or {}
    return {
        "change_title": str(pull.get("title") or ""),
        "change_url": str(pull.get("html_url") or ""),
        "change_author": str(user.get("login") or ""),
        "changed_files": pull.get("changed_files") or 0,
        "head_ref": str(head.get("ref") or ""),
        "head_revision": str(head.get("sha") or ""),
        "base_revision": str(base.get("sha") or ""),
    }


def parse_json_input(name: str, raw: str) -> dict[str, Any]:
    """Tolerate an unset or malformed JSON input rather than lose the whole report."""
    if not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        print(f"::warning title=Ignoring malformed {name}::{error}", flush=True)
        return {}
    if not isinstance(value, dict):
        print(f"::warning title=Ignoring {name}::expected a JSON object", flush=True)
        return {}
    return value


def int_or_none(raw: str) -> int | None:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def float_or_none(raw: str) -> float | None:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def token_counts(raw: str) -> dict[str, int] | None:
    """`opencode-run`'s token object, keeping only integer counters."""
    parsed = parse_json_input("tokens", raw)
    counts = {key: value for key, value in parsed.items() if isinstance(value, int)}
    return counts or None


def main() -> int:
    status = env("INPUT_STATUS") or "unknown"
    task = env("INPUT_TASK") or "unknown"
    runner = env("INPUT_RUNNER") or "unknown"
    repository = env("GITHUB_REPOSITORY")
    pr_number = env("INPUT_PR_NUMBER")
    endpoint = env("INPUT_METRICS_ENDPOINT")

    if not endpoint:
        print("[report] no metrics endpoint; nothing published", flush=True)
        return 0

    # `success` is stated by the caller, not inferred from the status word. Only the task
    # layer knows whether an answer it received was usable, and that judgement is exactly
    # what distinguishes "the model is bad at this" from "the key was out of budget".
    raw_success = env("INPUT_SUCCESS").lower()
    success = raw_success == "true" if raw_success else status == STATUS_SUCCESS

    attributes = agent_attributes(
        runner=runner,
        model=env("INPUT_MODEL") or "unknown",
        task=task,
        repository=repository,
        change_number=pr_number,
        status=status,
        success=success,
        run_id=env("GITHUB_RUN_ID"),
        run_attempt=env("GITHUB_RUN_ATTEMPT"),
        actor=env("GITHUB_ACTOR"),
        api_key_alias=env("INPUT_API_KEY_ALIAS"),
        code_areas=env("INPUT_CODE_AREAS"),
        department=env("INPUT_DEPARTMENT"),
        team_id=env("INPUT_TEAM_ID"),
        **change_facts(repository=repository, pr_number=pr_number, token=env("INPUT_GITHUB_TOKEN")),
    )
    # Merged last so a task layer can add dimensions, but the shared set above is what a
    # dashboard groups by and a task must not be able to redefine it by accident.
    for key, value in parse_json_input("extra-attributes", env("INPUT_EXTRA_ATTRIBUTES")).items():
        if key in attributes:
            print(f"::warning title=Ignoring extra attribute::{key} is part of the shared set", flush=True)
            continue
        attributes[key] = value

    metrics = agent_metrics(
        attributes,
        observed_at_unix_nano=time.time_ns(),
        tokens=token_counts(env("INPUT_TOKENS")),
        findings=int_or_none(env("INPUT_FINDINGS")),
        duration_seconds=float_or_none(env("INPUT_DURATION_SECONDS")),
    )

    # `service.name` names the PRODUCER, and there is now exactly one of them whatever CLI
    # ran. Naming it per runner would put `gto-claude-review` and `gto-opencode-review` on
    # series that are otherwise identical, which is a label nobody asked for and a second
    # thing to keep in sync.
    resource = {"service.name": "gto-agent-run", "service.namespace": "gto-ai"}
    try:
        post_json(endpoint, metrics_envelope(resource, SCOPE_NAME, metrics))
    except (urllib.error.URLError, OSError, ValueError, RuntimeError) as error:
        # A warning, never a failure. A job that did its work and could not phone home has
        # still done its work; failing it here would turn an observability outage into a
        # broken pipeline.
        print(f"::warning title=Telemetry not delivered::{error}", flush=True)
        return 0

    print(f"[report] task={task} runner={runner} status={status} success={success}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
