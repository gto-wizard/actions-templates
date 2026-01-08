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
```
