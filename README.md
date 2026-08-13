# actions-templates

GitHub Actions templates developed by and used by GTO Wizard

## Workflows

### `build-multi.yaml`
Multi-arch Docker build using native ARM64/AMD64 runners. Outputs: `ecr-registry`

```yaml
build:
  uses: gto-wizard/actions-templates/.github/workflows/build-multi.yaml@main
  with:
    IMAGE_NAME: my-app
    DOCKER_FILE: ./Dockerfile
    RUNS_ON_LABEL_AMD64: 4core-16gb-amd
    RUNS_ON_LABEL_ARM64: 4core-16gb-arm
```

### `build-multi-qemu.yaml`
Multi-arch Docker build using QEMU emulation.

### `deploy.yaml`
Deploys to Kubernetes via k8s-resources patching and ArgoCD sync.

```yaml
deploy:
  needs: build
  uses: gto-wizard/actions-templates/.github/workflows/deploy.yaml@main
  secrets: inherit
  with:
    APP_NAME: my-app
    APP_ENVIRONMENT: prod
    TARGET_REPOSITORY_FILES: applications/my-app/prod/values.my-app.image.yaml
    IMAGE_REPOSITORY: ${{ needs.build.outputs.ecr-registry }}/my-app
```

### `argocd-sync-wait`
Kubernetes ArgoCD sync and wait utility. Waits for application sync and healthy state in ArgoCD after deployment.

```yaml
- uses: gto-wizard/actions-templates/.github/actions/argocd-sync-wait@main
  with:
    ARGOCD_APP_NAMES: "my-app-dev"
    ARGOCD_AUTH_TOKEN: ${{ secrets.ARGOCD_AUTH_TOKEN }}
    WAIT_TIMEOUT_SECONDS: 300
```

The health wait **tolerates a transient `Degraded → Healthy` recovery**. `argocd app wait --health` fails fast on the first `Degraded` observation, so apps that legitimately flap through `Degraded` on first sync (e.g. KEDA scale-to-zero deployments creating the HPA / scaling to 0 while ExternalSecrets populate the trigger secret) are re-waited within a single wall-clock deadline. `WAIT_TIMEOUT_SECONDS` is the **total** budget per app — it is not multiplied by retries. If an app never reaches `Healthy` within the budget, the in-flight sync operation is terminated (`terminate-op`) and the step fails.

### `setup-warp`
Installs Cloudflare WARP and connects it in **proxy mode** so subsequent steps can reach internal services (e.g. `grafana.gtowiz.com`) over an authenticated tunnel. The proxy listens on `localhost:<proxy-port>` (default `40000`) — point `curl --proxy` or `HTTP_PROXY` / `HTTPS_PROXY` env vars at it.

```yaml
- uses: gto-wizard/actions-templates/.github/actions/setup-warp@main
  with:
    organization: gtowizard
    auth-client-id: ${{ vars.CF_GITHUB_ACTIONS_DIND_ACCESS_CLIENT_ID }}
    auth-client-secret: ${{ secrets.CF_GITHUB_ACTIONS_DIND_ACCESS_CLIENT_SECRET }}

# Then route requests through the proxy:
- run: curl --proxy http://localhost:40000 https://internal.gtowiz.com/...
```

### `claude-review`

Wraps **one** existing Claude Code invocation without owning its review prompt,
tools, model, verdict, or retry strategy. Those stay with the calling repository.
The action owns only the execution wire, so five repositories can answer "what
did we spend, on what, and why" without five implementations of it.

The invocation itself runs on **`anthropics/claude-code-action@v1`** — this action
does not exec the CLI. It hands the official action `claude_args` and `settings`,
then consumes its `execution_file`, `structured_output`, `session_id`, and
`conclusion` outputs. What it adds around that:

- the pinned runtime, when you point `claude-executable` at a cached binary;
- exports native Claude OTel under one PR-scoped trace;
- captures both the action's `execution_file` message log and Claude's own session
  JSONL, copied into the evidence directory (the action writes to a fixed name in
  `RUNNER_TEMP`, so the classifier pass would otherwise overwrite the review's);
- captures the pull request's timeline — commits, comments, review comments,
  reviews, labels, force pushes, participants;
- runs a portable structured classifier over the immutable diff plus that timeline;
- emits one exact-cost metric, log record, and root span;
- uploads private evidence for 14 days;
- **returns Claude's original exit code, always** — the wrapped step runs with
  `continue-on-error`, every reporting step runs `if: always()`, and one final step
  re-fails on the review's own outcome.

That last line is the contract that matters. Classification and telemetry are
reporting; a failed classifier or an unreachable collector warns and never
changes whether a review passed.

The shared classifier owns these reporting dimensions for every repository:

- change type and domain;
- optional testing/documentation concerns;
- implementation complexity (`light`, `easy`, `hard`);
- operational/business risk (`safe`, `medium`, `risky`).

Complexity and risk are deliberately orthogonal, and risk is max-by-dimension
rather than an average. A one-line authorization or payment change is `light` and
`risky`; a broad internal refactor can be `hard` and `safe`.

Everything the classifier reads — titles, code, comments, review threads — is
labelled untrusted in the prompt, because a pull request can otherwise ask to be
classified as `safe`.

```yaml
- name: Run repository-owned review with shared evidence and OTel
  id: review
  continue-on-error: true
  uses: gto-wizard/actions-templates/.github/actions/claude-review@<full-commit-sha>
  env:
    # Isolating HOME keeps a pull request from replacing Claude's settings or
    # hooks while the API credential is in the environment.
    HOME: ${{ runner.temp }}/claude-home
    ANTHROPIC_BASE_URL: https://llm.gtowiz.com
    ANTHROPIC_AUTH_TOKEN: ${{ secrets.REVIEW_KEY }}
  with:
    prompt: ${{ steps.prompt.outputs.text }}
    model: claude-sonnet-5
    max-turns: 60
    allowed-tools: Read,Glob,Grep,Bash(git diff *)
    invocation: primary
    pr-number: ${{ github.event.pull_request.number }}
    pr-title: ${{ github.event.pull_request.title }}
    pr-url: ${{ github.event.pull_request.html_url }}
    pr-author: ${{ github.event.pull_request.user.login }}
    base-sha: ${{ github.event.pull_request.base.sha }}
    head-sha: ${{ github.event.pull_request.head.sha }}
    api-key-alias: my-repository-review-bot
    department: DEVELOPMENT
    team-id: <litellm-team-id>
```

Pass credentials either as `anthropic-api-key`, or as step `env:` that the
official action inherits — `ANTHROPIC_AUTH_TOKEN` + `ANTHROPIC_BASE_URL` for the
LiteLLM gateway.

`model` and `classifier-model` default to the Anthropic-direct aliases `sonnet`
and `haiku`. Callers routing through the LiteLLM gateway must pass the names that
gateway exposes (`claude-sonnet-5`, `claude-haiku-4.5`, `claude-opus-5`) — the
short aliases do not resolve there.

Caller requirements:

- `actions/checkout` with `fetch-depth: 0`, so `base...head` can be diffed
  locally. Pass `diff-file` instead if the caller already has the diff.
- `permissions: pull-requests: read` for timeline capture. Pass
  `github-token: ""` to skip it; capture failures only degrade reporting.
- A runner able to reach the OTLP endpoints. The defaults are in-cluster service
  DNS, so an ARC runner in the CI cluster needs no collector credential.
- Nothing for Python — the action provisions its own unless you pass
  `python-version: ""`. Nothing for the CLI either: `claude-code-action` installs
  it. Pass `claude-executable` only to pin a version or reuse a cached binary.

Retries and multi-pass reviews stay the caller's business: give each Claude call
its own `invocation` name (`primary`, `retry-1`) and the telemetry separates
attempts per PR while still summing cost.

Outputs include `change-type`, `complexity`, `risk`, `classification-status`,
`total-cost-usd`, `session-id`, and paths to every evidence file.

Every signal carries **`vcs.change.ref`** — `gto-brain#182` — alongside the bare
`vcs.change.number`. Filter dashboards on the qualified one: a `pr=182` variable
silently merges `gto-brain#182` with `gto-universe#182` and presents the sum as one
pull request's cost, which is a wrong number rather than a missing one.

### `opencode-review`

The sibling of `claude-review`, for every model that is **not** Claude. It
runs one read-only [`opencode`](https://opencode.ai) review of one pull request on
one model served by any OpenAI-compatible gateway, and normalizes the result into a
report, a job summary, and step outputs.

Callers hardcode one model per workflow, so `kimi-pr-review.yml` and
`deepseek-pr-review.yml` are each a trigger, a gate, a checkout, and this:

```yaml
- name: Review with Kimi
  uses: gto-wizard/actions-templates/.github/actions/opencode-review@<full-commit-sha>
  with:
    pr-number: ${{ github.event.pull_request.number }}
    model: kimi-k3
    api-key: ${{ secrets.LITELLM_OPENCODE_REVIEW_KEY }}
```

It **deliberately does not classify.** Change type, complexity, and risk belong to
`claude-review`'s classifier pass, which is one model for every repository on
purpose; a second model re-deriving them would answer one question twice. This
action asks only for what a different *reading* of the diff produces: summary,
rationale, verdict (`approve` / `comment` / `request_changes`), and findings
anchored to path and line.

Three things are load-bearing, because a reviewed diff is untrusted input and
opencode is far more configurable than Claude Code:

- **Config arrives inline.** opencode merges config from eight sources, and a
  repository's own `opencode.json` outranks the `OPENCODE_CONFIG` path — a file in
  the diff could otherwise set `permission: allow`, or repoint the provider at
  another endpoint with the gateway key attached. The action passes its config
  through `OPENCODE_CONFIG_CONTENT`, which outranks the checkout.
- **`--pure`**, because `.opencode/plugin/*` is executable code loaded from the
  working directory. The action additionally *refuses* a checkout containing
  `opencode.json`, `opencode.jsonc`, or `.opencode/`, so a pull request that tries
  is visible rather than merely inert.
- **Deny-by-default permissions**, with `bash` reduced to an allowlist of read-only
  git verbs. Writes, `webfetch`, `websearch`, and subagents are never named, so `*`
  catches them. Verified against a real run: a denial returns an explanatory error
  the model recovers from, it does not hang the session.

The key is never written to a config file — the provider block dereferences
`{env:OPENCODE_GATEWAY_API_KEY}`, which the run step sets from `api-key`. The action
log shows tool *names* and per-step token counts only; tool inputs, tool results,
and the review text stay out of it and go to the private artifact instead.

Two limits are recorded rather than papered over:

- **The output shape is asked for, not enforced.** opencode has no `--json-schema`,
  so the schema goes in the prompt and is validated afterwards. A model that cannot
  hold it yields `review: null` plus the problems found and its raw text — reported
  as `status: error` with a warning, **not** as a failed job. For a comparison
  between models that verdict is a result, and one model's bad answer must not paint
  a panel red. The action fails only when `opencode` itself exits non-zero.
- **Cost is not self-reported.** opencode prices runs from models.dev, which knows
  nothing about a custom provider, so every event reports `cost: 0`. Tokens are
  real; the report omits cost rather than publish a zero that reads as free, and the
  gateway's own spend log is the source of truth. Token *fidelity* also varies by
  route — some report no output tokens at all.

`timeout-seconds` (default 900) exists because opencode documents no step,
iteration, or tool-call limit: without it the only bound is the caller's job
timeout, which would kill the reporting steps too. On expiry the run is interrupted
and the partial stream is still reported — a truncated final JSONL line is tolerated.

Caller requirements:

- `actions/checkout` with `fetch-depth: 0` and `ref: refs/pull/<n>/head`, so
  `base...head` can be diffed locally;
- `permissions: pull-requests: read`, to resolve the pull request. A fork's head is
  refused: a caller handed only a number cannot check provenance in an `if:`;
- nothing for Python or the CLI — the action provisions Python (unless
  `python-version: ""`) and installs the pinned opencode itself.

Evidence artifact (14 days): `pr-metadata.json`, `pr.diff.patch`,
`review-prompt.txt`, `opencode-run-report.json`, and the complete
`opencode-events.jsonl` stream. It contains code, so it stays private and short-lived.

Outputs: `verdict`, `findings`, `status`, `session-id`, `tokens-input`,
`tokens-output`, `report-file`, `artifact-name`.

**Telemetry.** What these actions do is run an LLM agent on a runner; reviewing a pull
request is the first *task* built on that. So the metrics are named for the run, not the
task, and both the runner and the task are labels — `gto.ai.task="pr_review"`,
`gto.review.runner="opencode"`. Built by `shared/gto_otlp.py`:

| metric | emitted when | notes |
| --- | --- | --- |
| `gto_ai_agent_runs` | always | the series to count. A run that could report nothing else still ran. **One LLM invocation is one run**, so counting reviews is `{task="pr_review"}` — a Claude review also emits a `task="classification"` run for its classifier pass. |
| `gto_ai_agent_cost_usd` | the runner knows its price | Claude today. Absent, never zero — a zero reads as free. |
| `gto_ai_agent_tokens` | the runner reports tokens | `kind` = input/output/reasoning/cache_read. |
| `gto_ai_agent_findings` | the runner reports findings | opencode today. |
| `gto_ai_agent_duration_seconds` | always | wall clock. EVERY runner reports it, so it is the one axis on which all reviewers compare directly today. |

Plus a `gto.opencode.pr_review.completed` event to Loki and a `gto.opencode.pr_review`
root span to Tempo. The shared dimensions come from `review_attributes()` so they cannot
drift between runners — they previously did, and `vcs.change.ref` reached one reviewer
and not the other, silently dropping every Claude run from any per-PR filter.

opencode still has no cost of its own: it reports `cost: 0` for a custom provider, and
its token counts are per-message rather than per-session, so deriving dollars from them
reconciles ~4x low. Instead every request carries
`x-litellm-tags: gto-ai-review,runner:…,model:…,run:<github run id>`, so the gateway's
own spend log can be split per review and joined back to `gto_ai_agent_runs` on
`github_run_id`. Filter on `vcs.change.ref` (`gto-brain#182`), never the bare number,
which collides across repositories. Any endpoint set to an empty string skips that
signal, and an unreachable collector warns without changing the review's verdict.

`review.status` is one of **`success`**, **`unusable`** (the model answered in the
wrong shape — its fault), **`error`** (opencode exited non-zero), **`timeout`**, or
**`cancelled`**. Cancellation cannot be read from the exit code — the review is killed
before it writes one — so it is passed in separately. That matters because
`concurrency: cancel-in-progress` means every re-push cancels the previous panel, and
without the distinction each push would manufacture a fake "this model cannot answer"
data point.

### `gateway-spend-export`

What the LLM gateway **actually billed** for each agent run, republished as
`gto_ai_agent_gateway_cost_usd{github_run_id, model, gto_review_runner}`.

It exists because no agent can price itself. opencode reports `cost: 0` for a custom
provider. Claude's CLI reports public list rates — measured at **2.1x** the booked figure.
Deriving cost from token counts is exact for some models and off by −16% / +37% for others,
which is the worst kind of wrong: plausible. The gateway's own log is the figure that
decrements the key's budget, so it is not a calculation at all.

It is a separate action rather than part of the agents because the spend log is admin-only:
an agent's own CI key is refused (401), and a per-key delta cannot work when several agents
share a key and run in parallel.

Give it a LiteLLM `proxy_admin_viewer` key restricted to a non-existent model list, so it can
read spend but cannot mint keys (403) or run inference (403). Never the master key.

```yaml
on:
  schedule: [{cron: "*/15 * * * *"}]
jobs:
  export:
    runs-on: <an in-cluster runner>   # the OTLP collector is cluster-local
    steps:
      - uses: gto-wizard/actions-templates/.github/actions/gateway-spend-export@<full-commit-sha>
        with:
          api-key: ${{ secrets.LITELLM_SPEND_VIEWER_KEY }}
```

It warns when tagged agent traffic arrives without a `run:<id>`, because that is the one
failure that hides: a missing request reads as a lower bill rather than an error.
