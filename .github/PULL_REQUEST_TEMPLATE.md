## Summary

<!--
2-3 bullets describing what infrastructure changed and why.
-->
-

## Linear Ticket

Refs: [TICKET-ID](https://linear.app/gto-wizard/issue/TICKET-ID)

## Environments Affected

- [ ] dev
- [ ] prod
- [ ] Both
- [ ] N/A (module/template — no direct environment impact)

## Changes

<!--
Resources added, modified, or removed.
For Terraform: paste or link the relevant tg-apply plan output.
-->

## Validation

- [ ] tg-apply plan reviewed (Terraform repos)
- [ ] kubectl apply --dry-run=client passed (K8s repos)
- [ ] No hardcoded secrets — all sensitive values via AWS Secrets Manager / env refs

## Rollback Plan

<!-- How would you revert this if it causes an incident? -->

## Checklist

- [ ] PR title follows `type(module/env): [ISSUE-ID] description` where module is an area within the repo (e.g. `s3/dev`, `iam/prod`, `gto-wizard/all`) and env is the environment affected
- [ ] Linear ticket referenced above
- [ ] `review-devops` label added to this PR
