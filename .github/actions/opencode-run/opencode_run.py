#!/usr/bin/env python3
"""Run one opencode session on a CI runner and report what happened. Nothing more.

This is the BINARY WRAPPER layer. It knows how to install a pinned CLI, point it at the
gateway, hand it a prompt, bound it in time, and describe how the session ended. It does not
know what the prompt was for. No pull request is resolved here, no schema is demanded of the
answer, no comment is posted, no metric is written.

That separation is the whole point. `opencode-review` fused all four together, so the only
way to ask these runners for anything else -- summarize a change, triage an issue, draft a
release note -- was to fork a reviewer and gut it. Worse, the fusion silently lost data: the
review prompt tells the model "change type, complexity and risk are classified by the shared
reporting wrapper, do not produce them", but that wrapper only ever existed inside
`claude-review`, so all 48 opencode runs carry no classification at all while the panels that
group by it look perfectly healthy.

WHAT THIS OWNS
    the CLI, the generated config, the timeout, and the OUTCOME vocabulary
    (success / error / timeout / cancelled / rejected, from `gto_otlp`).

WHAT THIS DOES NOT OWN
    `unusable`. That is a judgement that an answer failed A CONTRACT, and only the task
    layer knows the contract -- a reviewer wants strict JSON, a summarizer wants prose.
    A run that started, answered and exited cleanly is a `success` here even if the caller
    goes on to reject what it said. Keeping that distinction is what lets one runner serve
    tasks whose idea of a good answer disagree.

    Telemetry. `report-agent-run` is the single writer of `gto.ai.agent.*`; this action
    hands it numbers as step outputs. Two producers for one metric family is what made the
    dashboard's cost join ambiguous, and the fix is one writer rather than a cleverer query.

Standard library only: a runner executes it before any dependency install.
"""

import json
import os
import re
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
    litellm_tags,
)

DEFAULT_GATEWAY_URL = "https://llm.gtowiz.com/v1"
DEFAULT_PROVIDER_ID = "gtowizard"
DEFAULT_MODEL = "kimi-k3"

# The env var the generated provider block dereferences. The key never enters the config
# JSON, so the config stays safe to print while the secret stays in the environment.
API_KEY_ENV = "OPENCODE_GATEWAY_API_KEY"

# Our own agent, not the built-in `plan`, whose edits and bash are `ask` -- and an `ask` in a
# non-interactive run is a hang, not a refusal.
RUN_AGENT = "gto-run"

# Config and plugin surfaces the checked-out tree must not be able to introduce. Inline
# config already outranks a repository `opencode.json`, and `--pure` already refuses
# `.opencode/plugin` code, so this list exists to make a pull request that TRIES visible
# rather than quietly ineffective.
FORBIDDEN_CONFIG_PATHS = ("opencode.json", "opencode.jsonc", ".opencode")

# Deny-by-default, and NOT a free-form input. Anything beyond this comes from a NAMED
# capability below, never from a caller-supplied permission blob: a generic escape hatch is
# flipped in a workflow nobody reads closely, whereas a capability has to be added here, with
# its own review, its own prompt fragment and its own blast radius written down.
#
# `bash` is an allowlist of read-only git verbs, so an instruction injected through the
# repository cannot reach `git push`, and `webfetch`/`websearch` cannot carry what was read
# back out. `task` stays denied because a subagent escapes the turn budget.
READ_ONLY_PERMISSIONS: dict[str, Any] = {
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

# --- capabilities -------------------------------------------------------------------------
#
# A capability is one bundle of three things that MUST ship together: the permission that
# grants it, the prompt fragment that tells the model it exists, and (where there is one) the
# executor that performs it. Splitting them is how a model ends up confidently emitting
# something nothing executes, or -- far worse -- how an executor accepts something the prompt
# never advertised, which is a hole an injected instruction can walk through.
#
# The asymmetry that keeps this safe: ADVERTISING IS NOT GRANTING. The permission block is the
# grant; the fragment is documentation. Advertised-but-not-permitted fails closed. The reverse
# does not, which is why there is no "allow whatever the caller asks for" path.
#
# `comment` hands the agent a real credential and lets it decide, mid-run, to write to the
# pull request. That is a genuinely different trust model from every other run: the diff it is
# reading is untrusted, the model cannot reliably tell that input from its instructions, and
# the token's `permissions:` block is the only remaining boundary. Grant it only where the
# input is trusted, and never together with `contents: write`.
CAPABILITIES: dict[str, dict[str, Any]] = {
    "comment": {
        "permission": {
            # Not `gh*`: that would reach `gh api`, which can POST anywhere on github.com,
            # and `gh auth token`, which prints the credential straight into the transcript.
            "bash": {"gh pr comment*": "allow"},
            # So it can put a long markdown body in a file rather than quoting it through a
            # shell. Writes land in an ephemeral workspace that nothing pushes.
            "write": "allow",
            "edit": "allow",
        },
        "prompt": (
            "You can post a comment to the pull request you are reading, once, by running:\n"
            "\n"
            "    gh pr comment <number> --repo <owner/repo> --body-file <path>\n"
            "\n"
            "Write the body to a file first and pass it with --body-file; do not try to quote a\n"
            "long markdown body on the command line. No other `gh` subcommand is available to\n"
            "you, and there is no network access beyond that one command.\n"
            "\n"
            "Post exactly one comment, and only when you have something to say. If the command\n"
            "fails, say so in your final answer rather than retrying it repeatedly."
        ),
    }
}


def capability_bundle(names: list[str]) -> tuple[dict[str, Any], str]:
    """Merge the named capabilities into one permission overlay and one prompt appendix."""
    permission: dict[str, Any] = {}
    fragments: list[str] = []
    for name in names:
        capability = CAPABILITIES.get(name)
        if capability is None:
            raise SystemExit(
                f"::error title=Unknown capability::{name!r} is not one of {', '.join(CAPABILITIES)}"
            )
        for key, value in capability["permission"].items():
            if isinstance(value, dict):
                merged = dict(permission.get(key) or {})
                merged.update(value)
                permission[key] = merged
            else:
                permission[key] = value
        fragments.append(capability["prompt"])
    return permission, "\n\n".join(fragments)


def permissions_for(names: list[str]) -> dict[str, Any]:
    """Read-only, plus whatever the named capabilities add. `*: deny` always survives."""
    overlay, _ = capability_bundle(names)
    permissions = {key: (dict(value) if isinstance(value, dict) else value) for key, value in READ_ONLY_PERMISSIONS.items()}
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(permissions.get(key), dict):
            permissions[key].update(value)
        else:
            permissions[key] = value
    return permissions


TIMEOUT_EXIT_CODE = 124  # `timeout` says so

# What a gateway refusal looks like in the event stream. opencode surfaces the provider's
# error as text rather than a status code, so this matches LiteLLM's own wording. Anchored
# on distinctive phrases, never a bare "429", which appears in ordinary diffs.
GATEWAY_REJECTION_MARKERS = (
    "budget has been exceeded",
    "exceededbudget",
    "request rejected (429)",
    "rate limit exceeded",
    "authenticationerror",
    "invalid api key",
)


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


def run_config(
    model: str,
    *,
    provider_id: str = DEFAULT_PROVIDER_ID,
    gateway_url: str = DEFAULT_GATEWAY_URL,
    api_key_env: str = API_KEY_ENV,
    run_id: object = "",
    capabilities: list[str] | None = None,
) -> dict[str, Any]:
    """The inline opencode config for one run, read-only unless a capability widens it."""
    permissions = permissions_for(capabilities or [])
    alias = f"{provider_id}/{model}"
    # Every request carries the run id, so the gateway's spend log can be split by run
    # instead of pooling into one anonymous `User-Agent: opencode` bucket.
    tags = litellm_tags(runner="opencode", model=alias, run_id=run_id)
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
        "permission": permissions,
        "agent": {
            RUN_AGENT: {
                "description": "LLM run for CI.",
                "mode": "primary",
                "model": alias,
                "permission": permissions,
            }
        },
        "share": "disabled",
        "autoupdate": False,
    }


def guard_workspace(root: Path) -> None:
    """Refuse a checkout that ships its own opencode configuration."""
    found = [name for name in FORBIDDEN_CONFIG_PATHS if (root / name).exists()]
    if found:
        raise SystemExit(
            f"::error title=Workspace carries opencode config::{', '.join(found)} would attempt "
            "to configure the agent that reads it"
        )


# --- reading back what the session did ---------------------------------------------------


def read_events(path: Path) -> list[dict[str, Any]]:
    """opencode's JSONL stream, tolerating a truncated final line.

    A killed or timed-out run leaves half a line behind. Failing the whole report on it would
    discard every event that did arrive, which is the opposite of useful -- and a timeout is
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


def parts(events: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    """``(event type, part)`` pairs, tolerating events without a part object."""
    pairs: list[tuple[str, dict[str, Any]]] = []
    for event in events:
        part = event.get("part")
        pairs.append((str(event.get("type") or ""), part if isinstance(part, dict) else {}))
    return pairs


def answer_text(events: list[dict[str, Any]]) -> str:
    """Every text part in arrival order.

    NOT just the final message: models narrate between tool calls, so this interleaves
    commentary with the answer. Separating the two is the task layer's job, because only it
    knows what an answer is supposed to look like.
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
    """Tool names in call order -- enough for a progress view that prints no tool input."""
    return [part["tool"] for _, part in parts(events) if isinstance(part.get("tool"), str) and part["tool"]]


def tool_histogram(names: list[str]) -> str:
    """``read x9, grep x6, bash x2`` -- how the run spent its steps, at a glance."""
    counts: dict[str, int] = {}
    for name in names:
        counts[name] = counts.get(name, 0) + 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return ", ".join(f"{name} x{count}" for name, count in ranked) or "none"


def token_usage(events: list[dict[str, Any]]) -> dict[str, int]:
    """Sum every step's token counts.

    Per-step, not per-conversation: a multi-step run resends its history each step, so these
    are billable totals rather than a context size. `input` excludes what came from cache;
    `cache_read` is that part. `total` follows opencode's own arithmetic -- every counter
    added, reasoning included and separate from output. Inventing a third definition of
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
    """Why each step ended -- `stop` is clean; `length` or `tool-calls` explains a stub."""
    return [
        part["reason"]
        for event_type, part in parts(events)
        if event_type == "step_finish" and isinstance(part.get("reason"), str)
    ]


def looks_rejected(text: str) -> bool:
    """True when the gateway refused the request, so no model ever answered it.

    A refusal is not a model failing: recording an exhausted key as a bad answer blames the
    model for an operational problem. Measured: a key one cent over its budget produced four
    fifteen-minute `timeout` rows with zero tool calls, which read as four slow models.
    """
    haystack = (text or "").lower()
    return any(marker in haystack for marker in GATEWAY_REJECTION_MARKERS)


def run_status(*, exit_code: int, cancelled: bool, rejected: bool = False) -> str:
    """How the RUN ended -- never whether the answer was any good.

    There is no `unusable` here, deliberately: see the module docstring. A clean exit is
    `success` even if the caller later rejects the content.

    `cancelled` is derived from the ABSENCE of an exit code rather than from a `cancelled()`
    expression, which a composite action cannot evaluate -- GitHub rejects the whole action
    template with "Unrecognized function: 'cancelled'" and every job fails before its first
    step. The run step disables errexit precisely so it always records an exit code on any
    normal end, including a timeout; no exit code therefore means the step never reached its
    last line, which is what being killed looks like.
    """
    if rejected:
        return STATUS_REJECTED
    if cancelled:
        return STATUS_CANCELLED
    if exit_code == TIMEOUT_EXIT_CODE:
        return STATUS_TIMEOUT
    if exit_code != 0:
        return STATUS_ERROR
    return STATUS_SUCCESS


# --- subcommands -------------------------------------------------------------------------


def prepare() -> int:
    """Write the config and the prompt, and refuse a workspace that fights back."""
    workspace = Path(env("GITHUB_WORKSPACE", ".")).resolve()
    guard_workspace(workspace)

    prompt = os.environ.get("INPUT_PROMPT", "")
    if not prompt.strip():
        print("::error title=No prompt::this runner has no built-in instructions; pass `prompt`", flush=True)
        return 2

    capabilities = [name.strip() for name in env("INPUT_CAPABILITIES").split(",") if name.strip()]
    if capabilities and not os.environ.get("GH_TOKEN", "").strip():
        print("::error title=Capability without a credential::`comment` needs `github-token`", flush=True)
        return 2
    _, appendix = capability_bundle(capabilities)
    if appendix:
        # Appended AFTER the caller's instruction so the capability reads as an affordance
        # rather than as the task. The task is what the consuming repository asked for.
        prompt = f"{prompt.rstrip()}\n\n{appendix}\n"

    model = env("INPUT_MODEL", DEFAULT_MODEL) or DEFAULT_MODEL
    provider_id = env("INPUT_PROVIDER_ID", DEFAULT_PROVIDER_ID) or DEFAULT_PROVIDER_ID
    gateway_url = env("INPUT_GATEWAY_URL", DEFAULT_GATEWAY_URL) or DEFAULT_GATEWAY_URL

    runner_temp = Path(env("RUNNER_TEMP", "/tmp"))  # noqa: S108 - GitHub always sets RUNNER_TEMP
    artifact_dir = runner_temp / f"gto-agent-run-{safe_slug(model)}"
    home_dir = runner_temp / f"gto-agent-home-{safe_slug(model)}"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    home_dir.mkdir(parents=True, exist_ok=True)

    config = run_config(
        model,
        provider_id=provider_id,
        gateway_url=gateway_url,
        run_id=env("GITHUB_RUN_ID"),
        capabilities=capabilities,
    )
    config_file = artifact_dir / "opencode-config.json"
    config_file.write_text(json.dumps(config, indent=2), encoding="utf-8")

    prompt_file = artifact_dir / "prompt.txt"
    prompt_file.write_text(prompt, encoding="utf-8")

    for name, value in (
        ("artifact-dir", str(artifact_dir)),
        ("home-dir", str(home_dir)),
        ("config-file", str(config_file)),
        ("prompt-file", str(prompt_file)),
        ("model", model),
        ("model-alias", f"{provider_id}/{model}"),
    ):
        append_output(name, value)
    return 0


def render_stream() -> int:
    """Echo tool names and step tokens as they arrive, never tool input or file contents."""
    for line in sys.stdin:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        part = event.get("part") if isinstance(event.get("part"), dict) else {}
        tool = part.get("tool")
        if isinstance(tool, str) and tool:
            print(f"  tool {tool}", flush=True)
        if str(event.get("type")) == "step_finish":
            tokens = part.get("tokens") if isinstance(part.get("tokens"), dict) else {}
            print(f"  step end ({part.get('reason', '?')}) tokens={tokens.get('output', '?')}", flush=True)
    return 0


def collect() -> int:
    """Turn the session into step outputs. Always succeeds: reporting never fails a job."""
    artifact_dir = Path(env("INPUT_ARTIFACT_DIR"))
    events_file = Path(env("INPUT_EVENTS_FILE") or (artifact_dir / "opencode-events.jsonl"))
    events = read_events(events_file) if events_file.exists() else []

    raw_exit = env("INPUT_EXIT_CODE")
    cancelled = raw_exit == ""
    exit_code = int(raw_exit) if raw_exit.isdigit() else 1

    text = answer_text(events)
    status = run_status(exit_code=exit_code, cancelled=cancelled, rejected=looks_rejected(text))

    text_file = artifact_dir / "answer.txt"
    text_file.write_text(text, encoding="utf-8")

    tokens = token_usage(events)
    names = tool_names(events)
    reasons = finish_reasons(events)

    for name, value in (
        ("status", status),
        ("success", "true" if status == STATUS_SUCCESS else "false"),
        ("exit-code", str(exit_code) if not cancelled else ""),
        ("text-file", str(text_file)),
        ("text-bytes", str(len(text.encode("utf-8")))),
        ("events-file", str(events_file)),
        ("session-id", session_id(events)),
        ("steps", str(len(reasons))),
        ("tool-calls", str(len(names))),
        ("tool-histogram", tool_histogram(names)),
        ("finish-reasons", ",".join(reasons)),
        ("tokens", json.dumps(tokens, separators=(",", ":"))),
    ):
        append_output(name, value)

    print(
        f"[run] status={status} steps={len(reasons)} tools={len(names)} "
        f"tokens={tokens['total']} answer={len(text)} chars",
        flush=True,
    )
    return 0


COMMANDS = {"prepare": prepare, "render-stream": render_stream, "collect": collect}


def main() -> int:
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command not in COMMANDS:
        print(f"usage: {sys.argv[0]} {{{'|'.join(COMMANDS)}}}", flush=True)
        return 2
    return COMMANDS[command]()


if __name__ == "__main__":
    sys.exit(main())
