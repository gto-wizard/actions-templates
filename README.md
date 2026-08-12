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

### `claude-observability`

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
  uses: gto-wizard/actions-templates/.github/actions/claude-observability@<full-commit-sha>
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
