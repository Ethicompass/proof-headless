# 0002. The stack is one compose project rendered from the lock

Status: proposed (2026-11-12)

## Context

Yield and Lens each ship a compose file for a laptop: services built from source, seed assets
mounted from `../seed`, mock targets and consumers, and a store named `minio` in both. The first
engagements ran them with per-engagement overrides that swapped builds for released images
(`pull_policy: never`, a patched compute), replaced unpullable MinIO images, and joined the two
stacks through `host.docker.internal` and host ports. Yield's and Lens's co-location changes
(`YIELD_EXTRA_NETWORKS`, `LENS_EXTRA_NETWORKS`, Lens's `lens-minio` alias, Yield's secrets
`env_file`)
make a shared network possible, but their compose files still need a checkout to run.

## Decision

`proof stack` renders one compose project, `proof-stack`, from `appliance.lock` and the models
manifest, and runs it on the appliance with no checkout of Yield or Lens. Only `up` and `render`
render; `status`, `logs` and `down` act on the file the stack was started from.

- **Services** follow Yield's and Lens's compose files with their co-location changes, with
  every image taken from the lock by digest, each store and API under a name of its own
  (`yield-minio`, `lens-minio`), host ports on loopback, and no mock target or consumer. Lens's
  Valkey is dropped: Lens's compose runs its jobs registry in memory.
- **Networks**: `proof-yield` and `proof-lens` (each stack's own, handed to its dispatcher),
  `proof-stack` (Yield's MinIO, `lens-api` and the brain registry, the hops between the two), and
  the models manifest's network (`proof-models` by default), external to both the stack and the
  models' project and created by whichever starts first. Every job joins `proof-stack` and the
  models' network through the extra-networks settings.
- **Lens's Reader and Curator** ask the manifest's chat model (`http://<name>:8000/v1`, its served
  name, `json_schema`, no credential), with its reasoning switch off when it has one. A manifest
  with no chat model is refused: Lens would report `not_configured` on every run while the stack
  looked healthy.
- **Seeds** are vendored: Yield's `deploy/seed` (minus domain catalogues, which arrive with a
  bundle) and Lens's calibration, rule set and score-card weights, each with the commit it was
  copied from in `seed/SOURCE.yaml`. They run in the locked `python` image. Lens's seed checks
  that each asset lands on the digest the stack pins, so a drifted copy fails `up`, not a run.
- **Secrets, least privilege**: no service loads the operator's secrets file. It is compose's
  `--env-file`, and the template names what reaches a container: the store credentials, and for
  Yield's API and compute exactly the secrets the bundles passed to `up` declare (a `YIELD_*` or
  `LENS_*` name is refused, being a setting). Lens gets none, because its only `*_REF` is the
  signing secret, mapped by name; `HF_TOKEN` stays with the models. `proof` writes a value only
  when the operator sets one, and never prints one.
- **Pinning** resolves tags with `docker buildx imagetools inspect`, which uses the registry login
  docker already holds, and records each image's platforms. `pin` refuses to write a lock with an
  image the target platform (this host's by default) cannot run, and `up` checks again. `pin`
  defaults to `develop` until a promotion publishes arm64 release images that carry the
  co-location settings; then the default becomes `release`.

## Consequences

- A change to Yield's or Lens's compose file does not reach the appliance until this template
  follows it. The golden file `tests/golden/stack-compose.yaml` makes that change reviewable.
- A refresh of a seed is a re-copy from a named commit and a diff, and a change to Lens's
  seed digests is a change of `SOURCE.yaml`.
- Fetching seeds from the repositories at `up` would avoid the copy, but the repositories are
  private and the appliance would need a GitHub token for it; the copy needs nothing.
