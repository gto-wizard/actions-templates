#!/usr/bin/env python3
"""Run one Claude Code session on a CI runner. Configuration and outcome, nothing else.

This is the BINARY WRAPPER for Claude, and it is deliberately thin, because Claude Code
already reports itself. It does three things:

    1. configure the CLI's own OpenTelemetry so it exports where we want, labelled with
       who the run was for
    2. install HOOKS for the few facts telemetry does not carry -- chiefly HOW A RUN ENDED
    3. exec the CLI with argv the caller supplies, bounded in time, and report the outcome

It does NOT parse a transcript to reconstruct tokens, cost, or tool calls. Claude Code emits
all of that natively -- `claude_code_token_usage`, `claude_code_cost_usage`,
`claude_code_active_time_total`, plus tool-level spans -- and reconstructing it by hand
produced a second, worse copy of numbers the CLI already publishes. `claude-review` carries
1095 lines; 30 of them are stream parsing and the rest is review plumbing that was never a
runner's job.

WHY NOT claude-code-action
    It owns argv. Its SDK side-channel broke `--resume` on five consecutive runs in
    gto-universe while the same session ids resumed cleanly against the real CLI, which is
    why that repository invokes the binary directly and says so in a comment. A shared
    runner that wraps the SDK cannot honour that, so this execs the CLI: every flag the
    caller passes in `claude-args` reaches the process unaltered.

WHY HOOKS, AND WHY ONLY TWO
    Hooks are the CLI's own callback mechanism and are configurable ONLY through a settings
    file -- no environment variable, no flag -- so this writes one and passes `--settings`.
    `StopFailure` distinguishes `rate_limit`, `overloaded`, `authentication_failed` and
    `billing_error`; `SessionEnd` reports `end_reason`. Those are precisely the distinctions
    the opencode runner has to guess by regex-matching error text, and getting them from the
    CLI is both exact and free. Everything else it can tell us is already a metric.

    The hook is `cat >> file`. A hook that needed a script would need the script installed,
    versioned and made executable on the runner; appending the event JSON costs nothing and
    cannot fail in an interesting way.

WHAT THE WRAPPER STILL OWNS
    Being killed from OUTSIDE. `timeout` sends a signal the CLI may not survive long enough
    to report, so no hook fires and the exit code is the only evidence. That fallback is the
    reason this file exists at all rather than being a step in a workflow.

Standard library only: a runner executes it before any dependency install.
"""

import json
import os
import shlex
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))

from gto_otlp import (  # noqa: E402
    STATUS_CANCELLED,
    STATUS_ERROR,
    STATUS_REJECTED,
    STATUS_SUCCESS,
    STATUS_TIMEOUT,
    agent_attributes,
    litellm_tags,
    resource_attribute_env,
)

TIMEOUT_EXIT_CODE = 124  # `timeout` says so

# `StopFailure` matchers, mapped to the shared outcome vocabulary. These are the CLI's own
# words for why a turn died, which is the whole reason to take them from a hook: the opencode
# runner infers the same distinction by matching phrases in error text, and a refusal that
# reads as a model failure blames the model for an operational problem.
REJECTION_REASONS = frozenset({"rate_limit", "overloaded", "authentication_failed", "billing_error"})

# Events worth a callback. Deliberately short: everything else Claude Code can report, it
# already reports as a metric, a log or a span, and a hook that duplicates one is a second
# source for a fact that already has an owner.
HOOK_EVENTS = ("SessionEnd", "StopFailure")


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def append_output(name: str, value: str) -> None:
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with Path(output).open("a", encoding="utf-8") as stream:
            stream.write(f"{name}={value}\n")


def hook_settings(record_file: Path) -> dict[str, Any]:
    """A settings file whose only content is our callbacks.

    Passed with `--settings`, which is additive to whatever `--setting-sources` admits, so
    this grants the hooks without deciding on the caller's behalf whether the repository's
    own configuration is trusted. That decision is `setting-sources`, one input, made by
    whoever knows where the input came from.
    """
    handler = [{"type": "command", "command": f"cat >> {shlex.quote(str(record_file))}"}]
    return {"hooks": {event: [{"hooks": handler}] for event in HOOK_EVENTS}}


def hook_records(path: Path) -> list[dict[str, Any]]:
    """Whatever the hooks appended, tolerating a file that was never written."""
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            record = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def hook_status(records: list[dict[str, Any]]) -> str:
    """The CLI's own account of how the turn ended, or empty when it never said.

    A `StopFailure` outranks a `SessionEnd`: the session always ends, so `end_reason` alone
    would report a rate-limited run as a clean finish.
    """
    for record in records:
        if record.get("hook_event_name") != "StopFailure":
            continue
        reason = str(record.get("reason") or record.get("error_type") or record.get("type") or "")
        return STATUS_REJECTED if reason in REJECTION_REASONS else STATUS_ERROR
    return ""


def run_status(*, exit_code: int, cancelled: bool, from_hook: str = "") -> str:
    """How the RUN ended -- never whether the answer was any good.

    There is no `unusable` here. Whether an answer satisfies a contract is the task layer's
    judgement, and only it holds the contract; a reviewer wants strict JSON where a
    summarizer wants prose.

    The hook is preferred over the exit code because it is more specific: a gateway refusal
    and a genuine crash both exit non-zero, and calling a dead credential a model failure
    hides an operational problem inside a quality signal. The exit code still wins for
    endings the CLI could not report, which is exactly the case a hook cannot cover --
    being killed from outside.

    `cancelled` is derived from the ABSENCE of an exit code rather than a `cancelled()`
    expression, which a composite action cannot evaluate: GitHub rejects the whole action
    template with "Unrecognized function: 'cancelled'" and every job fails before its first
    step.
    """
    if cancelled:
        return STATUS_CANCELLED
    if exit_code == TIMEOUT_EXIT_CODE:
        return STATUS_TIMEOUT
    if from_hook:
        return from_hook
    return STATUS_SUCCESS if exit_code == 0 else STATUS_ERROR


def result_event(path: Path) -> dict[str, Any]:
    """The terminal `result` object from a `stream-json` run.

    The ONLY thing read out of the transcript, and only because the caller needs the answer
    text. Tokens, cost and tool counts are deliberately not taken from here: the CLI exports
    them itself, and a second copy computed from the same stream is a second number to
    disagree with.
    """
    if not path.is_file():
        return {}
    result: dict[str, Any] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            result = event
    return result


def prepare() -> int:
    """Write the settings, the prompt, and the resource attributes the CLI will export with."""
    prompt = os.environ.get("INPUT_PROMPT", "")
    if not prompt.strip():
        print("::error title=No prompt::this runner has no built-in instructions; pass `prompt`", flush=True)
        return 2

    runner_temp = Path(env("RUNNER_TEMP", "/tmp"))  # noqa: S108 - GitHub always sets RUNNER_TEMP
    artifact_dir = runner_temp / "gto-claude-run"
    home_dir = runner_temp / "gto-claude-home"
    for directory in (artifact_dir, home_dir):
        directory.mkdir(parents=True, exist_ok=True)

    record_file = artifact_dir / "hooks.jsonl"
    settings_file = artifact_dir / "settings.json"
    settings_file.write_text(json.dumps(hook_settings(record_file), indent=2), encoding="utf-8")

    prompt_file = artifact_dir / "prompt.txt"
    prompt_file.write_text(prompt, encoding="utf-8")

    model = env("INPUT_MODEL") or "sonnet"
    # The same attribute set `report-agent-run` publishes, so the CLI's native signals and
    # our own land on one vocabulary. Built here rather than by the caller because resource
    # attributes must exist BEFORE the process starts, and a schema with two builders is a
    # schema that drifts -- `vcs.change.ref` once reached one runner and not the other.
    attributes = agent_attributes(
        runner="claude",
        model=model,
        task=env("INPUT_TASK") or "unknown",
        repository=env("GITHUB_REPOSITORY"),
        change_number=env("INPUT_PR_NUMBER"),
        status="running",
        success=False,
        run_id=env("GITHUB_RUN_ID"),
        run_attempt=env("GITHUB_RUN_ATTEMPT"),
        actor=env("GITHUB_ACTOR"),
        api_key_alias=env("INPUT_API_KEY_ALIAS"),
        code_areas=env("INPUT_CODE_AREAS"),
        department=env("INPUT_DEPARTMENT"),
        team_id=env("INPUT_TEAM_ID"),
    )
    # `review.status` is not knowable before the run and would be a lie on every native
    # signal; the terminal status is published by `report-agent-run` afterwards.
    for absent in ("review.status", "review.success"):
        attributes.pop(absent, None)
    attributes["service.name"] = "claude-code"
    attributes["service.namespace"] = "gto-ai"

    for name, value in (
        ("artifact-dir", str(artifact_dir)),
        ("home-dir", str(home_dir)),
        ("settings-file", str(settings_file)),
        ("prompt-file", str(prompt_file)),
        ("record-file", str(record_file)),
        ("model", model),
        ("resource-attributes", resource_attribute_env(attributes)),
        ("litellm-tags", litellm_tags(runner="claude", model=model, run_id=env("GITHUB_RUN_ID"))),
    ):
        append_output(name, value)
    return 0


def collect() -> int:
    """Turn the run into step outputs. Always succeeds: reporting never fails a job."""
    artifact_dir = Path(env("INPUT_ARTIFACT_DIR"))
    raw_exit = env("INPUT_EXIT_CODE")
    cancelled = raw_exit == ""
    exit_code = int(raw_exit) if raw_exit.lstrip("-").isdigit() else 1

    records = hook_records(artifact_dir / "hooks.jsonl")
    status = run_status(exit_code=exit_code, cancelled=cancelled, from_hook=hook_status(records))

    result = result_event(artifact_dir / "transcript.jsonl")
    text = str(result.get("result") or "")
    text_file = artifact_dir / "answer.txt"
    text_file.write_text(text, encoding="utf-8")

    end_reasons = ",".join(
        str(r.get("end_reason") or r.get("reason") or "") for r in records if r.get("hook_event_name")
    ).strip(",")

    for name, value in (
        ("status", status),
        ("success", "true" if status == STATUS_SUCCESS else "false"),
        ("exit-code", "" if cancelled else str(exit_code)),
        ("text-file", str(text_file)),
        ("text-bytes", str(len(text.encode("utf-8")))),
        ("session-id", str(result.get("session_id") or "")),
        ("end-reasons", end_reasons),
        ("hook-events", str(len(records))),
    ):
        append_output(name, value)

    print(f"[claude-run] status={status} hooks={len(records)} answer={len(text)} chars", flush=True)
    return 0


COMMANDS = {"prepare": prepare, "collect": collect}


def main() -> int:
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command not in COMMANDS:
        print(f"usage: {sys.argv[0]} {{{'|'.join(COMMANDS)}}}", flush=True)
        return 2
    return COMMANDS[command]()


if __name__ == "__main__":
    sys.exit(main())
