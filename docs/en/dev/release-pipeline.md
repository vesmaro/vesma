# Release pipeline (internal operator runbook)

**🌐 Language / Язык:** English · [Русский](../../ru/dev/release-pipeline.md)

> Internal operator documentation: how Vesma releases ride the shared cluster
> release pipeline. Contributors and end users do not need this page — for the
> public contribution and release surfaces see
> [CONTRIBUTING.md](../../../CONTRIBUTING.md) and the
> [PyPI publish runbook](../admin/runbooks/pypi-publish.md).

The project releases through the shared cluster pipeline
[Korrnals/release-pipeline](https://github.com/Korrnals/release-pipeline)
(K3s cluster `abyss-ai-agent`, namespace `release-pipeline`). GitHub Actions is
not used (the account's billing is locked) — the pipeline is the sanctioned
release path. A release artifact is signed in three layers:
SHA256 → SBOM → cosign → GPG.

## Releasing a new version

1. Bump `VERSION` → release commit (repo convention) → tag `vX.Y.Z` → push.
2. Update the project version in `projects[]` of
   `~/.cache/release-pipeline-values.yaml` and apply:

   ```bash
   helm upgrade --install release-pipeline \
     ~/LABs/Projects/Project-Umbra/release-pipeline/chart/release-pipeline \
     --kube-context abyss-ai-agent -n release-pipeline \
     -f ~/.cache/release-pipeline-values.yaml
   ```

3. Observe: `kubectl --context abyss-ai-agent -n release-pipeline get jobs`;
   logs: `kubectl ... logs -f job/release-<proj>-<ver>`.

Details (project types, kaniko containers, teardown) live in the pipeline's own
README.
