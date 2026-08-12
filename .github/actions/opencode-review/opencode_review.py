#!/usr/bin/env python3
"""Portable `opencode` pull-request review against an OpenAI-compatible gateway.

The sibling of `claude-review`, for every model that is not Claude. Review policy
belongs to the caller; this module owns the execution wire: the read-only opencode config,
the prompt, the JSON event stream, and the normalized report.

Three contracts matter more than any feature here:

* **the caller's diff is untrusted input.** opencode merges config from eight sources and a
  repository's own `opencode.json` outranks the `OPENCODE_CONFIG` path, so config is handed
  over as inline content — which outranks the checkout — and permissions deny by default;
* **an unusable answer is a result, not a crash.** opencode cannot constrain output to a
  schema, so the shape is asked for and validated here. A model that cannot hold it yields
  `review: null` plus the problems found and its raw text, and the action still reports;
* **cost is not ours to claim.** opencode prices runs from models.dev, which knows nothing
  about a custom provider, so every event reports `cost: 0`. Tokens are real; dollars are
  not, and the report omits the field rather than publish a zero that reads as free.

This module deliberately does NOT classify a pull request. Change type, complexity, and risk
belong to `claude-review`'s separate classifier pass, which is one model for every
repository on purpose; a second model re-deriving them would answer one question twice.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

# The OTLP wire is shared with `claude-review`: both actions feed one dashboard, so the
# encoding, the transport and the pull-request identifier have a single owner. For a `uses:`
# reference GitHub checks out the whole repository, so this sibling path resolves on a runner
# and in the unit tests alike.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))

from gto_otlp import (  # noqa: E402 - sys.path must be set before this import
    litellm_tags,
    log_envelope,
    metrics_envelope,
    new_trace_ids,
    post_json,
    agent_attributes,
    agent_metrics,
    span_envelope,
)

SCHEMA_VERSION = 1

GITHUB_API = "https://api.github.com"
GITHUB_API_TIMEOUT = 15.0

DEFAULT_GATEWAY_URL = "https://llm.gtowiz.com/v1"
DEFAULT_PROVIDER_ID = "gtowizard"
DEFAULT_MODEL = "kimi-k3"

# The env var the generated provider block dereferences. The key never enters the config
# JSON, so the config stays safe to print while the secret stays in the environment.
API_KEY_ENV = "OPENCODE_GATEWAY_API_KEY"

# Our own agent, not the built-in `plan`, whose edits and bash are `ask` — and an `ask` in a
# non-interactive run is a hang, not a refusal.
REVIEW_AGENT = "gto-review"

# Config and plugin surfaces a reviewed diff must not be able to introduce. Inline config
# already outranks a repository `opencode.json`, and `--pure` already refuses
# `.opencode/plugin` code, so this list exists to make a pull request that TRIES visible
# rather than quietly ineffective.
FORBIDDEN_CONFIG_PATHS = ("opencode.json", "opencode.jsonc", ".opencode")

VERDICTS = ("approve", "comment", "request_changes")
SEVERITIES = ("info", "warning", "blocking")

# What the reviewer is asked for. Printed into the prompt AND enforced by the validator, so
# the two cannot drift: a field added here is demanded and checked in the same commit.
REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "summary": {"type": "string"},
        "rationale": {"type": "string"},
        "verdict": {"type": "string", "enum": list(VERDICTS)},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "severity": {"type": "string", "enum": list(SEVERITIES)},
                    "path": {"type": "string"},
                    "line": {"type": ["integer", "null"]},
                    "message": {"type": "string"},
                },
                "required": ["severity", "path", "line", "message"],
            },
        },
    },
    "required": ["summary", "rationale", "verdict", "findings"],
}

# Every read a diff review needs, and nothing else.
#
# Deny-by-default is the point: `bash` is an allowlist of read-only git verbs, so an
# instruction injected through the diff cannot reach `git push`, and `webfetch`/`websearch`
# cannot carry what the review read back out. `task` stays denied because a subagent escapes
# the turn budget, not because it is unsafe. Verified against a real run: a denial returns an
# explanatory error the model recovers from, it does not hang the session.
REVIEW_PERMISSIONS: dict[str, Any] = {
    "*": "deny",
    "read": "allow",
    "grep": "allow",
    "glob": "allow",
    "list": "allow",
    "todowrite": "allow",
    "todoread": "allow",
    "bash": {
        "*": "deny",
        "git diff*": "allow",
        "git show*": "allow",
        "git log*": "allow",
        "git status*": "allow",
        "git rev-parse*": "allow",
    },
}


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def append_output(name: str, value: str) -> None:
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with Path(output).open("a", encoding="utf-8") as stream:
            stream.write(f"{name}={value}\n")


def safe_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-.")
    return slug[:100] or "unknown"


def review_config(
    model: str,
    *,
    provider_id: str = DEFAULT_PROVIDER_ID,
    gateway_url: str = DEFAULT_GATEWAY_URL,
    api_key_env: str = API_KEY_ENV,
    run_id: object = "",
) -> dict[str, Any]:
    """The inline opencode config for one read-only review run."""
    alias = f"{provider_id}/{model}"
    # Every request carries the run id, so the gateway's spend log can be split by review
    # instead of pooling into one anonymous `User-Agent: opencode` bucket.
    tags = litellm_tags(runner="opencode", model=alias, run_id=run_id)
    agent = {
        "description": "Read-only pull-request reviewer for CI.",
        "mode": "primary",
        "model": alias,
        "permission": REVIEW_PERMISSIONS,
    }
    return {
        "$schema": "https://opencode.ai/config.json",
        "model": alias,
        "provider": {
            provider_id: {
                "npm": "@ai-sdk/openai-compatible",
                "name": "LLM gateway",
                "options": {
                    "baseURL": gateway_url,
                    "apiKey": f"{{env:{api_key_env}}}",
                    "headers": {"x-litellm-tags": tags},
                },
                # One model, not the gateway's whole catalogue: the run is pinned to what the
                # caller asked for, so a typo in the id fails loudly instead of silently
                # resolving to somebody's default.
                "models": {model: {"name": f"{model} ({provider_id})"}},
            }
        },
        "permission": REVIEW_PERMISSIONS,
        "agent": {REVIEW_AGENT: agent},
        "share": "disabled",
        "autoupdate": False,
    }


def review_prompt(base_sha: str, head_sha: str, extra: str = "") -> str:
    """The review instruction, carrying the schema the validator enforces.

    No reasoning effort is requested anywhere: a gateway that does not accept
    `reasoning_effort` answers 400 to every request that sends one.
    """
    schema = json.dumps(REVIEW_SCHEMA, separators=(",", ":"))
    caller = f"\n{extra.strip()}\n" if extra.strip() else ""
    return f"""Review this pull request. It is the diff between {base_sha} and {head_sha}.

Start with `git diff {base_sha}...{head_sha}`, then read whatever files you need for
context. You have read-only access: no edits, no commits, no network.

Report:
- a concise factual summary of the change,
- an evidence-based review rationale — what you checked and what convinced you, not
  private chain-of-thought,
- concrete findings, each anchored to a path and (where you can) a line. Return an empty
  list when the change is sound. Do not invent findings to look thorough; a wrong finding
  costs more review time than a missed one.

Change type, complexity, and risk are classified separately and identically for every
repository by the shared reporting wrapper. Do not produce them.
{caller}
Answer with a SINGLE JSON object and nothing else — no prose around it, no markdown fence.
It must validate against this schema:

{schema}

`verdict` is `approve` when you would merge it as-is, `comment` when your findings are
worth reading but none block, and `request_changes` when at least one finding does."""


def github_get(path: str, token: str) -> Any:
    url = f"{GITHUB_API}{path}"
    if not url.startswith(f"{GITHUB_API}/"):
        raise ValueError(f"refusing to fetch {url}")
    request = Request(  # noqa: S310 - api.github.com by construction, asserted above
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "gto-opencode-review",
        },
    )
    with urlopen(request, timeout=GITHUB_API_TIMEOUT) as response:  # noqa: S310 - same
        return json.load(response)


def pull_request_metadata(pull_request: dict[str, Any], *, repository: str, model: str, provider: str) -> dict[str, Any]:
    head = pull_request.get("head") or {}
    base = pull_request.get("base") or {}
    user = pull_request.get("user") or {}
    return {
        "schema_version": SCHEMA_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "repository": repository,
        "workflow": env("GITHUB_WORKFLOW"),
        "run_id": env("GITHUB_RUN_ID", "local"),
        "run_attempt": env("GITHUB_RUN_ATTEMPT", "1"),
        "actor": env("GITHUB_ACTOR"),
        "event_name": env("GITHUB_EVENT_NAME"),
        "model": f"{provider}/{model}",
        "pr_number": pull_request.get("number"),
        "pr_title": pull_request.get("title", ""),
        "pr_url": pull_request.get("html_url", ""),
        "pr_author": user.get("login", ""),
        "head_ref": head.get("ref", ""),
        "head_sha": head.get("sha", ""),
        "head_repository": ((head.get("repo") or {}).get("full_name")) or "",
        "base_ref": base.get("ref", ""),
        "base_sha": base.get("sha", ""),
        # Diff size travels with the review so "12 findings" can be read against "3 files"
        # rather than in a vacuum when two models are compared.
        "changed_files": pull_request.get("changed_files"),
        "additions": pull_request.get("additions"),
        "deletions": pull_request.get("deletions"),
    }


def guard_workspace(root: Path) -> None:
    """Refuse a checkout that carries its own opencode configuration or plugins."""
    for name in FORBIDDEN_CONFIG_PATHS:
        if (root / name).exists():
            raise ValueError(
                f"{name} is present in this checkout; a reviewed diff must not be able to "
                "configure its own reviewer. Review this pull request by hand."
            )


def prepare() -> int:
    """Resolve the pull request, refuse what must not be reviewed, and write the inputs."""
    number = env("INPUT_PR_NUMBER")
    if not number.isdigit():
        raise ValueError("a numeric pr-number is required")
    repository = env("GITHUB_REPOSITORY")
    if "/" not in repository:
        raise ValueError("GITHUB_REPOSITORY must be owner/repository")
    token = env("INPUT_GITHUB_TOKEN")
    if not token:
        raise ValueError("github-token is required to resolve the pull request")

    model = env("INPUT_MODEL") or DEFAULT_MODEL
    provider = env("INPUT_PROVIDER_ID") or DEFAULT_PROVIDER_ID
    gateway = env("INPUT_GATEWAY_URL") or DEFAULT_GATEWAY_URL

    guard_workspace(Path.cwd())

    pull_request = github_get(f"/repos/{repository}/pulls/{number}", token)
    if not isinstance(pull_request, dict):
        raise ValueError(f"pull request {number} did not return an object")
    metadata = pull_request_metadata(pull_request, repository=repository, model=model, provider=provider)

    # A caller's `if:` cannot check provenance when all it was handed is a number, so the
    # check lives here: a fork's head must never be reviewed by a job holding a gateway key.
    if metadata["head_repository"] != repository:
        raise ValueError(
            f"pull request {number} head is {metadata['head_repository'] or 'unknown'}, not {repository}"
        )
    base_sha = env("INPUT_BASE_SHA") or str(metadata["base_sha"])
    head_sha = env("INPUT_HEAD_SHA") or str(metadata["head_sha"])
    if not base_sha or not head_sha:
        raise ValueError("could not resolve both base and head revisions")

    runner_temp = Path(env("RUNNER_TEMP", "/tmp"))  # noqa: S108 - RUNNER_TEMP is always set on a runner
    slug = safe_slug(model)
    artifact_dir = runner_temp / "gto-opencode-review" / slug
    artifact_dir.mkdir(parents=True, exist_ok=True)
    # Deliberately outside artifact_dir: HOME holds opencode's session state and the resolved
    # config, and neither belongs in a build artifact.
    home_dir = runner_temp / "gto-opencode-home" / slug
    home_dir.mkdir(parents=True, exist_ok=True)

    (artifact_dir / "pr-metadata.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    # The immutable diff, for the same reason the Claude action captures one: the evidence
    # should show what was reviewed, not require re-deriving it from two revisions later.
    diff = subprocess.run(  # noqa: S603 - fixed executable, revisions are not shell-interpreted
        ["git", "diff", "--binary", f"{base_sha}...{head_sha}"],
        check=True,
        capture_output=True,
    )
    (artifact_dir / "pr.diff.patch").write_bytes(diff.stdout)

    prompt_file = artifact_dir / "review-prompt.txt"
    prompt_file.write_text(review_prompt(base_sha, head_sha, env("INPUT_EXTRA_INSTRUCTIONS")), encoding="utf-8")
    # Outside the artifact: it is not secret — the key is an `{env:…}` reference — but a
    # config file in a downloadable artifact invites somebody to reuse it as one.
    config_file = home_dir / "opencode-config.json"
    config_file.write_text(
        json.dumps(
            review_config(
                model, provider_id=provider, gateway_url=gateway, run_id=metadata["run_id"]
            ),
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )

    artifact_name = safe_slug(f"opencode-{model}-pr-{number}-{metadata['run_id']}-{metadata['run_attempt']}")
    for name, value in (
        ("artifact-dir", str(artifact_dir)),
        ("artifact-name", artifact_name),
        ("home-dir", str(home_dir)),
        ("metadata-file", str(artifact_dir / "pr-metadata.json")),
        ("prompt-file", str(prompt_file)),
        ("config-file", str(config_file)),
        ("base-sha", base_sha),
        ("head-sha", head_sha),
        ("model", model),
        ("model-alias", f"{provider}/{model}"),
    ):
        append_output(name, value)
    print(
        f"[prepare] pr=#{number} {base_sha[:12]}...{head_sha[:12]} model={provider}/{model} "
        f"files={metadata['changed_files']} +{metadata['additions']}/-{metadata['deletions']}",
        flush=True,
    )
    return 0


def parts(events: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    """``(event type, part)`` pairs, tolerating events without a part object."""
    pairs: list[tuple[str, dict[str, Any]]] = []
    for event in events:
        part = event.get("part")
        pairs.append((str(event.get("type") or ""), part if isinstance(part, dict) else {}))
    return pairs


def review_text(events: list[dict[str, Any]]) -> str:
    """Every text part in arrival order.

    NOT just the final answer: models narrate between tool calls, so this interleaves
    commentary with the answer. `extract_review` is what separates them.
    """
    return "".join(
        part["text"] for event_type, part in parts(events) if event_type == "text" and isinstance(part.get("text"), str)
    )


def session_id(events: list[dict[str, Any]]) -> str:
    for event in events:
        value = event.get("sessionID")
        if isinstance(value, str) and value:
            return value
    return ""


def tool_names(events: list[dict[str, Any]]) -> list[str]:
    """Tool names in call order — enough for a progress view that prints no tool input."""
    return [part["tool"] for _, part in parts(events) if isinstance(part.get("tool"), str) and part["tool"]]


def tool_histogram(names: list[str]) -> str:
    """``read x9, grep x6, bash x2`` — how the reviewer spent its steps, at a glance."""
    counts: dict[str, int] = {}
    for name in names:
        counts[name] = counts.get(name, 0) + 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return ", ".join(f"{name} x{count}" for name, count in ranked) or "none"


def token_usage(events: list[dict[str, Any]]) -> dict[str, int]:
    """Sum every step's token counts.

    Per-step, not per-conversation: a multi-step review resends its history each step, so
    these are billable totals rather than a context size. `input` excludes what came from
    cache; `cache_read` is that part. `total` follows opencode's own arithmetic — every
    counter added, reasoning included and separate from output — verified against a run whose
    reported per-step total equalled exactly that sum. Inventing a third definition of
    "tokens" would make two runs incomparable.
    """
    totals = dict.fromkeys(("input", "output", "reasoning", "cache_read", "cache_write"), 0)
    for _, part in parts(events):
        tokens = part.get("tokens")
        if not isinstance(tokens, dict):
            continue
        for field in ("input", "output", "reasoning"):
            value = tokens.get(field)
            if isinstance(value, int):
                totals[field] += value
        cache = tokens.get("cache")
        if isinstance(cache, dict):
            for field, target in (("read", "cache_read"), ("write", "cache_write")):
                value = cache.get(field)
                if isinstance(value, int):
                    totals[target] += value
    totals["total"] = sum(totals.values())
    return totals


def finish_reasons(events: list[dict[str, Any]]) -> list[str]:
    """Why each step ended — `stop` is clean; `length` or `tool-calls` explains a stub."""
    return [
        part["reason"]
        for event_type, part in parts(events)
        if event_type == "step_finish" and isinstance(part.get("reason"), str)
    ]


def finding_problems(findings: object) -> list[str]:
    if not isinstance(findings, list):
        return ["findings is not a list"]
    problems: list[str] = []
    for position, finding in enumerate(findings):
        if not isinstance(finding, dict):
            problems.append(f"findings[{position}] is not an object")
            continue
        if finding.get("severity") not in SEVERITIES:
            problems.append(f"findings[{position}].severity is not one of {', '.join(SEVERITIES)}")
        if not isinstance(finding.get("path"), str):
            problems.append(f"findings[{position}].path is not a string")
        if not isinstance(finding.get("message"), str):
            problems.append(f"findings[{position}].message is not a string")
        line = finding.get("line")
        if isinstance(line, bool) or not isinstance(line, (int, type(None))):
            problems.append(f"findings[{position}].line is neither an integer nor null")
    return problems


def validate_review(review: dict[str, Any]) -> list[str]:
    """Every way this object departs from `REVIEW_SCHEMA`."""
    problems: list[str] = []
    for field in ("summary", "rationale"):
        value = review.get(field)
        if not isinstance(value, str) or not value.strip():
            problems.append(f"{field} is missing or empty")
    if review.get("verdict") not in VERDICTS:
        problems.append(f"verdict={review.get('verdict')!r} is not one of {', '.join(VERDICTS)}")
    problems.extend(finding_problems(review.get("findings")))
    return problems


def json_objects(text: str) -> list[dict[str, Any]]:
    """Every JSON object embedded anywhere in a free-text answer, in order of appearance."""
    decoder = json.JSONDecoder()
    objects: list[dict[str, Any]] = []
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            candidate, _ = decoder.raw_decode(text, index)
        except ValueError:
            continue
        if isinstance(candidate, dict):
            objects.append(candidate)
    return objects


def extract_review(text: str) -> dict[str, Any] | None:
    """The review object embedded in a free-text answer, or ``None``.

    With no schema enforcement to lean on, this accepts what models actually emit — a bare
    object, a fenced one, or an object after a paragraph of preamble — and prefers the LAST
    one that actually VALIDATES. Last, because a model that restates its answer means the
    restatement; validating, because of what `deepseek-v4-pro` did on its second real run: it
    echoed the requested schema back around its answer, so the text ended with the schema's
    own `properties` block — an object that has a `summary` key and is not a review. Keying
    on "has a summary" picked that, reported three validation problems, and threw away a
    perfectly good review that was sitting earlier in the same string.

    When nothing validates, the last object that at least looks like an attempt is returned
    so the caller can report real problems against real content instead of "no review found".
    """
    objects = json_objects(text)
    for candidate in reversed(objects):
        if not validate_review(candidate):
            return candidate
    for candidate in reversed(objects):
        if isinstance(candidate.get("summary"), str):
            return candidate
    return None


def read_events(path: Path) -> list[dict[str, Any]]:
    """opencode's JSONL stream, tolerating a truncated final line.

    A killed or timed-out run leaves half a line behind. Failing the whole report on it would
    discard every event that did arrive, which is the opposite of useful — and a timeout is
    exactly when the evidence matters.
    """
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


# Why a run ended, as a dimension. Not a free-text field: this is what a dashboard groups by,
# and it is the difference between "this model cannot hold the format" and "somebody pushed
# again while it was thinking".
STATUS_SUCCESS = "success"
# "the model answered, but not in the shape asked for" is a fact about the model. "opencode
# exited non-zero" is a fact about the run. Collapsing them into one word throws away the
# distinction a model comparison is built on.
STATUS_UNUSABLE = "unusable"
STATUS_ERROR = "error"
STATUS_TIMEOUT = "timeout"
STATUS_CANCELLED = "cancelled"

TIMEOUT_EXIT_CODE = 124  # `timeout` says so


def run_status(*, exit_code: int, valid: bool, cancelled: bool) -> str:
    """Distinguish the model failing from the run being taken away from it.

    A cancelled run cannot be detected from the exit code: the review step is killed before it
    writes one, so the report would see a default and record the model as having failed. Since
    `concurrency: cancel-in-progress` means every re-push cancels the previous panel, that
    would manufacture a fake "this model cannot answer" data point on every push — corrupting
    exactly the comparison the telemetry exists for. Observed: four reviewers cancelled
    mid-flight, all four recorded `verdict: unusable`.

    `cancelled` is derived from the ABSENCE of an exit code rather than from a `cancelled()`
    expression, which a composite action cannot evaluate — GitHub rejects the whole action
    template with "Unrecognized function: 'cancelled'" and every review fails before its first
    step. The run step disables errexit precisely so it always records an exit code on any
    normal end, including a timeout; no exit code therefore means the step never reached its
    last line, which is what being killed looks like.
    """
    if cancelled:
        return STATUS_CANCELLED
    if exit_code == TIMEOUT_EXIT_CODE:
        return STATUS_TIMEOUT
    if exit_code != 0:
        return STATUS_ERROR
    return STATUS_SUCCESS if valid else STATUS_UNUSABLE


def build_report(
    metadata: dict[str, Any],
    events: list[dict[str, Any]],
    *,
    model: str,
    provider: str,
    exit_code: int,
    cancelled: bool = False,
) -> dict[str, Any]:
    """One opencode run, normalized into a report a human or an ingest can read."""
    text = review_text(events)
    review = extract_review(text)
    if cancelled:
        problems = ["the run was cancelled before the model finished answering"]
    elif review is None:
        problems = ["no JSON review object in the answer"]
    else:
        problems = validate_review(review)
    valid = not cancelled and review is not None and not problems
    status = run_status(exit_code=exit_code, valid=valid, cancelled=cancelled)
    return {
        "schema_version": SCHEMA_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "source": "opencode",
        "runner": {"cli": "opencode", "provider": provider, "model": model, "agent": REVIEW_AGENT},
        "metadata": metadata,
        "session": {
            "id": session_id(events),
            "status": status,
            "is_error": status != STATUS_SUCCESS,
            "exit_code": exit_code,
            # No `cost_usd`, deliberately — see the module docstring. Its absence is the claim.
            "tokens": token_usage(events),
            "tools": tool_names(events),
            "finish_reasons": finish_reasons(events),
            "review": review if status == STATUS_SUCCESS else None,
            "review_problems": problems,
            # The raw answer is kept only when it did not parse — exactly when somebody has
            # to read it. A valid review is already in `review`.
            "raw_answer": None if status == STATUS_SUCCESS else text,
        },
        "artifact": {"format": "jsonl", "event_count": len(events)},
    }


def finding_row(finding: dict[str, Any]) -> str:
    """One summary-table row. Pipes are escaped so a finding cannot break the table."""
    line = finding.get("line")
    location = f"`{finding.get('path', '?')}`" + (
        f":{line}" if isinstance(line, int) and not isinstance(line, bool) else ""
    )
    message = str(finding.get("message", "")).replace("|", "\\|").replace("\n", " ")
    return f"| {finding.get('severity', '?')} | {location} | {message} |"


def summary_markdown(report: dict[str, Any]) -> str:
    """The job summary — the only place most people will read this review."""
    metadata = report["metadata"]
    session = report["session"]
    runner = report["runner"]
    review = session.get("review") or {}
    tokens = session["tokens"]
    findings = review.get("findings") or []
    number = metadata.get("pr_number", "?")
    url = metadata.get("pr_url", "")
    lines = [
        f"### opencode review — `{runner['provider']}/{runner['model']}`",
        "",
        "| dimension | value |",
        "| --- | --- |",
        f"| pull request | {f'[#{number}]({url})' if url else f'#{number}'} |",
        f"| verdict | {review.get('verdict', '**unusable**')} |",
        f"| findings | {len(findings)} |",
        f"| diff | {metadata.get('changed_files', '?')} files, "
        f"+{metadata.get('additions', '?')}/-{metadata.get('deletions', '?')} |",
        f"| tokens (in / out / reasoning) | {tokens['input']} / {tokens['output']} / {tokens['reasoning']} |",
        f"| tools | {tool_histogram(session['tools'])} |",
        f"| session | `{session.get('id') or 'unavailable'}` |",
        f"| result | {session['status']} (exit {session['exit_code']}) |",
    ]
    if review.get("summary"):
        lines.extend(["", "**Summary.** " + str(review["summary"])])
    if review.get("rationale"):
        lines.extend(["", "**Rationale.** " + str(review["rationale"])])
    if findings:
        lines.extend(["", "| severity | location | finding |", "| --- | --- | --- |"])
        lines.extend(finding_row(finding) for finding in findings)
    if session["review"] is None:
        problems = "\n".join(f"- {problem}" for problem in session["review_problems"])
        lines.extend(
            [
                "",
                "**No usable review.** opencode cannot constrain an answer to a schema, so this is a",
                "reportable outcome of the run rather than an infrastructure failure:",
                "",
                problems,
            ]
        )
    lines.extend(
        [
            "",
            f"Cost is absent by design: opencode prices runs from models.dev, which does not know the "
            f"`{runner['provider']}` provider, so it reports `cost: 0` for every request. The gateway's "
            f"spend log for this run's key is the cost source of truth.",
        ]
    )
    return "\n".join(lines) + "\n"


SCOPE_NAME = "gto.actions.opencode_review"


def telemetry_attributes(report: dict[str, Any]) -> dict[str, object]:
    """The shared review dimensions, plus the facts only this runner produces.

    The common set comes from `review_attributes` so it cannot drift from the Claude
    action's — the two used to keep private copies, and a label added to one quietly did
    not exist on the other.
    """
    metadata = report["metadata"]
    runner = report["runner"]
    session = report["session"]
    review = session.get("review") or {}
    return {
        **agent_attributes(
            runner="opencode",
            task="pr_review",
            model=f"{runner['provider']}/{runner['model']}",
            repository=str(metadata.get("repository", "")),
            change_number=metadata.get("pr_number", ""),
            status=session["status"],
            success=not session["is_error"],
            change_title=metadata.get("pr_title", ""),
            change_url=metadata.get("pr_url", ""),
            change_author=metadata.get("pr_author", ""),
            changed_files=metadata.get("changed_files") or 0,
            head_ref=metadata.get("head_ref", ""),
            head_revision=metadata.get("head_sha", ""),
            base_revision=metadata.get("base_sha", ""),
            run_id=metadata.get("run_id", ""),
            run_attempt=metadata.get("run_attempt", ""),
            actor=metadata.get("actor", ""),
            api_key_alias=env("INPUT_API_KEY_ALIAS"),
            code_areas=env("INPUT_CODE_AREAS"),
            department=env("INPUT_DEPARTMENT"),
            team_id=env("INPUT_TEAM_ID"),
        ),
        # Falls back to the run's status, not to "unusable": a cancelled or timed-out run
        # never got to answer, and recording that as a model verdict is a lie in a dashboard.
        "gto.review.verdict": review.get("verdict") or session["status"],
        "gto.review.findings": len(review.get("findings") or []),
        "gto.review.steps": len(session.get("finish_reasons") or []),
        "gto.review.tool_calls": len(session.get("tools") or []),
    }


def emit_telemetry(report: dict[str, Any], *, observed_at_unix_nano: int, duration_nanos: int) -> list[str]:
    """Ship one metric, one event and one root span. Returns the failures, never raises.

    Reporting never decides whether a review passed — the same contract the Claude action
    holds. An unreachable collector warns; the verdict stands.
    """
    attributes = telemetry_attributes(report)
    session = report["session"]
    runner = report["runner"]
    resource = {
        "service.name": "gto-opencode-review",
        "service.namespace": "gto-ai",
        "service.instance.id": safe_slug(
            f"github-run-{attributes['github.run.id']}-{attributes['github.run.attempt']}-{runner['model']}"
        ),
    }
    trace_id, span_id = new_trace_ids(secrets.token_bytes)
    tokens = session["tokens"]

    # No cost: opencode prices from models.dev, which does not know this provider, so it
    # reports `cost: 0` for every request. Deriving dollars from the tokens below does not
    # work either — they are per-message, not per-session, and reconcile ~4x low against the
    # gateway's own booking. The gateway is the authority; `x-litellm-tags` carries the run
    # id so it can be joined back to these series.
    metrics = agent_metrics(
        attributes,
        observed_at_unix_nano=observed_at_unix_nano,
        tokens={kind: tokens[kind] for kind in ("input", "output", "reasoning", "cache_read")},
        findings=int(attributes["gto.review.findings"]),
        duration_seconds=duration_nanos / 1_000_000_000,
    )

    event_attributes = {
        **attributes,
        "gto.review.problems": "; ".join(session["review_problems"])[:900] or "none",
        "gto.review.tools": tool_histogram(session["tools"]),
        "gto.review.session_id": session.get("id") or "",
        "opencode.tokens.input": tokens["input"],
        "opencode.tokens.output": tokens["output"],
        "opencode.tokens.reasoning": tokens["reasoning"],
        "opencode.tokens.cache_read": tokens["cache_read"],
    }
    signals = (
        (
            "metrics",
            env("INPUT_METRICS_ENDPOINT"),
            metrics_envelope(resource, SCOPE_NAME, metrics),
        ),
        (
            "logs",
            env("INPUT_LOGS_ENDPOINT"),
            log_envelope(
                resource,
                SCOPE_NAME,
                body="gto.opencode.pr_review.completed",
                attributes=event_attributes,
                observed_at_unix_nano=observed_at_unix_nano,
                trace_id=trace_id,
                span_id=span_id,
            ),
        ),
        (
            "traces",
            env("INPUT_TRACES_ENDPOINT"),
            span_envelope(
                resource,
                SCOPE_NAME,
                name="gto.opencode.pr_review",
                trace_id=trace_id,
                span_id=span_id,
                start_unix_nano=observed_at_unix_nano - duration_nanos,
                end_unix_nano=observed_at_unix_nano,
                attributes=event_attributes,
                failed=session["is_error"],
            ),
        ),
    )
    failures: list[str] = []
    for signal, endpoint, payload in signals:
        if not endpoint:
            continue
        try:
            post_json(endpoint, payload)
        except (OSError, RuntimeError, ValueError) as error:
            failures.append(f"{signal}: {error}")
    return failures


def report() -> int:
    """Normalize the event stream, write the artifact copy, the summary, and the outputs."""
    artifact_dir = Path(env("INPUT_ARTIFACT_DIR"))
    if not artifact_dir.is_dir():
        raise ValueError(f"artifact directory does not exist: {artifact_dir}")
    metadata_path = Path(env("INPUT_METADATA_FILE"))
    metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.is_file() else {}
    # Empty means the run step never wrote one, i.e. it was killed rather than finished.
    reported_exit_code = env("INPUT_EXIT_CODE")
    exit_code = int(reported_exit_code or "1")

    execution = Path(env("INPUT_EXECUTION_FILE"))
    events = read_events(execution) if execution.is_file() else []
    if not events:
        print("::warning title=No opencode events::the run produced no event stream", flush=True)

    built = build_report(
        metadata,
        events,
        model=env("INPUT_MODEL") or DEFAULT_MODEL,
        provider=env("INPUT_PROVIDER_ID") or DEFAULT_PROVIDER_ID,
        exit_code=exit_code,
        cancelled=reported_exit_code == "",
    )
    report_file = artifact_dir / "opencode-run-report.json"
    report_file.write_text(json.dumps(built, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with Path(step_summary).open("a", encoding="utf-8") as stream:
            stream.write(summary_markdown(built))

    duration_nanos = max(int(float(env("INPUT_DURATION_SECONDS", "0") or 0) * 1_000_000_000), 1)
    failures = emit_telemetry(built, observed_at_unix_nano=time.time_ns(), duration_nanos=duration_nanos)
    if failures:
        # Reporting never decides whether a review passed: an unreachable collector warns.
        print("::warning title=Telemetry not fully delivered::" + "; ".join(failures)[:400], flush=True)

    session = built["session"]
    review = session["review"] or {}
    append_output("report-file", str(report_file))
    append_output("verdict", str(review.get("verdict", "")))
    append_output("findings", str(len(review.get("findings") or [])))
    append_output("status", str(session["status"]))
    append_output("session-id", str(session["id"]))
    append_output("tokens-input", str(session["tokens"]["input"]))
    append_output("tokens-output", str(session["tokens"]["output"]))
    print(
        f"[report] model={built['runner']['model']} status={session['status']} "
        f"verdict={review.get('verdict', 'none')} findings={len(review.get('findings') or [])} "
        f"tokens_in={session['tokens']['input']} tokens_out={session['tokens']['output']}",
        flush=True,
    )
    if session["review"] is None:
        print(
            "::warning title=No usable opencode review::" + "; ".join(session["review_problems"])[:400],
            flush=True,
        )
    return 0


def render_stream() -> int:
    """A content-free progress view, while the raw JSONL is teed to the artifact.

    Tool inputs, tool results, and the answer itself never reach the action log — the same
    redaction the Claude action applies, for the same reason: the log is visible to everyone
    with repository read access, and a diff review quotes the diff.
    """
    seen_session = False
    for line in sys.stdin:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if not seen_session and isinstance(event.get("sessionID"), str):
            print(f"[init] session={event['sessionID']}", flush=True)
            seen_session = True
        raw_part = event.get("part")
        part = raw_part if isinstance(raw_part, dict) else {}
        if isinstance(part.get("tool"), str):
            print(f"[tool] {part['tool']}", flush=True)
        elif event.get("type") == "step_finish":
            raw_tokens = part.get("tokens")
            tokens = raw_tokens if isinstance(raw_tokens, dict) else {}
            print(
                f"[step] reason={part.get('reason', '?')} "
                f"tokens_in={tokens.get('input', '?')} tokens_out={tokens.get('output', '?')}",
                flush=True,
            )
    return 0


def main() -> int:
    try:
        command = sys.argv[1]
        if command == "prepare":
            return prepare()
        if command == "report":
            return report()
        if command == "render-stream":
            return render_stream()
        raise ValueError("usage: opencode_review.py <prepare|report|render-stream>")
    except (KeyError, IndexError, ValueError, RuntimeError, OSError, subprocess.CalledProcessError) as error:
        print(f"opencode-review: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
