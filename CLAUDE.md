# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Repo Is

Shared GitHub Actions reusable workflows and composite actions used across all GTO Wizard repositories. Consumers reference these templates via `uses: gto-wizard/actions-templates/.github/workflows/<name>@main` or `uses: gto-wizard/actions-templates/.github/actions/<name>@main`.

## Repository Structure

- `.github/workflows/` — Reusable workflows (called via `workflow_call`)
  - `build-multi.yaml` — Multi-arch Docker build using native ARM64/AMD64 runners (no QEMU)
  - `build-multi-qemu.yaml` — Multi-arch Docker build using QEMU emulation
  - `deploy.yaml` — Deploy to K8s via k8s-resources patching + ArgoCD sync
  - `frontend-build.yaml` — Frontend build with npm
  - `frontend-build-with-pnpm.yaml` — Frontend build with pnpm (uses `pnpm nx run`)
  - `frontend-deployment.yaml` — Frontend deploy to Cloudflare Pages
- `.github/actions/` — Composite actions
  - `argocd-sync-wait/` — ArgoCD sync + health wait (shell-based composite action)
  - `slack-release-payload-builder/` — Builds Slack Block Kit payloads (Python script)

## Linting and Validation

Pre-commit hooks handle all linting. Install with:
```bash
pre-commit install
pre-commit run --all-files
```

Hooks that run:
- **prettier** — formats YAML files (2-space indent, 120 char width, no semis, double quotes)
- **yamllint** — lints YAML (120 char lines, 2-space indent, comments/truthy checks disabled)
- **end-of-file-fixer** and **trailing-whitespace**

There are no tests or build steps — this is a YAML-only repo (plus one Python script in `slack-release-payload-builder`).

## Key Conventions

- **Pin all GitHub Actions by full SHA**, not tags. Add a comment with the version: `uses: actions/checkout@<sha> # pin@v4.2.2`
- **All workflows use `workflow_call` trigger** — they are reusable templates, not standalone workflows
- Docker images are pushed to **AWS ECR**. Auth uses OIDC role assumption (`role-to-assume` with `id-token: write`)
- Image tags: `type=sha,prefix=` (short commit SHA) + `type=raw,value=latest` on default branch
- Build cache uses a separate ECR `cache` repository with registry-backed cache
- Dependabot builds skip cache-to writes (`github.actor != 'dependabot[bot]'` guard)
- The deploy workflow triggers a `patch.yaml` workflow in the `k8s-resources` repo via GitHub App token, then optionally runs ArgoCD sync
