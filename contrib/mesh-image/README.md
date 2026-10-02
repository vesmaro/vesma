# Mesh-enabled release image (ghcr.io/vesmaro/vesma)

Builds the release image with the gencode the `vesma._mesh_gen` shim
expects. This institutionalizes recipe #354 and the verification gate
added after the `v4.3.4-mesh.1` regression (vesma-mesh#44: the build
copied stale gitignored stubs — the image's gencode lacked
`ListMemoriesRequest.resume_cursor` and every pull to the cluster died
on `AttributeError`).

The image ships `HEALTHCHECK` on `/health` (auth-exempt endpoint; port
read from the runtime env — see the Dockerfile comment).

## Release naming

From the **next** release the git tag carries **no `-mesh` suffix**:
the mesh is part of the system, not a variant of it. The rule (already
in effect on the registry side):

- git tag: `vX.Y.Z` (core) / `vA.B.C` (mesh) — bare semver;
- **ghcr image tag == git tag, exactly**: `ghcr.io/vesmaro/vesma:<core tag>`,
  `ghcr.io/vesmaro/vesma-mesh:<mesh tag>`;
- historical `-mesh.N` tags (…`v4.3.7-mesh.2`) stay on ghcr and in
  `contrib/node-install/compatibility.tsv` as append-only history — do
  not rewrite them. New ledger rows use bare tags; the ledger parser
  accepts both formats.

## Full release flow (tag → build same tag → gate → push, one block)

Run from the vesma repo on the dev laptop (host-built — GH Actions
billing-locked, recipe #354/#374; podman is reached through
`distrobox-host-exec` when working from the dev box):

```bash
set -e
TAG=v4.3.8                                   # bare tag, NO -mesh suffix
# 1) the commit is on origin/main and tagged — tag == future image tag
git tag "$TAG" && git push origin "$TAG"
# 2) clean build context from the TAG (never the working tree)
rm -rf "/tmp/mesh-img-$TAG" && mkdir -p "/tmp/mesh-img-$TAG"
git archive "$TAG" | tar -x -C "/tmp/mesh-img-$TAG"
#    Dockerfile.mesh + gate.py ship IN the repo (contrib/mesh-image/):
#    the archive already carries them — nothing to copy.
# 3) build the image with the SAME tag as the git tag
distrobox-host-exec podman build -f "/tmp/mesh-img-$TAG/contrib/mesh-image/Dockerfile.mesh" \
  -t "ghcr.io/vesmaro/vesma:$TAG" "/tmp/mesh-img-$TAG"
# 4) GATE — mandatory, fail = abort (never push an ungated image)
distrobox-host-exec podman run --rm --entrypoint python "ghcr.io/vesmaro/vesma:$TAG" /gate.py
# 5) push (private registry; login as korrnals first)
distrobox-host-exec podman push "ghcr.io/vesmaro/vesma:$TAG"
```

Pair it with the mesh release the same way: tag `vesma-mesh`
(`vA.B.C`), build/push `ghcr.io/vesmaro/vesma-mesh:<tag>`, then
append the pair to `contrib/node-install/compatibility.tsv` (see the
node-install README — "Version cycle").

## Gate (mandatory, fails the build)

`gate.py` imports the shipped stubs and asserts the presence of the
newest proto fields (`resume_cursor`, `ListMemoriesResponse.cursor`,
`PullRequest.cursor`, `MetadataRecord.origin_peer` when present on the
branch) plus `import vesma.mesh_server`. A missing field = stale
gencode = build failure. Extend the field list whenever the proto gains
load-bearing fields.

## Rotation reminder

Core-identity cert leaves (`CN=*-mnemos-core`) rotate manually every 90
days via the mesh pki directory; see
`~/.config/mnemos-mesh/pki/fingerprints.txt` on the operator host.
