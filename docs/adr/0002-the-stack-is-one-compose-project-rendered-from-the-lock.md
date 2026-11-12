# 0002. The stack is one compose project rendered from the lock

Status: proposed (2026-11-12)

## Context

Yield and Lens each ship a compose file for a laptop: services built from source, seed assets
mounted from `../seed`, mock targets and consumers, and a store named `minio` in both. The first
engagements ran them with per-engagement overrides that swapped builds for released images
(`pull_policy: never`, a patched compute), replaced unpullable MinIO images, and joined the two
stacks through `host.docker.internal` and host ports. Yield's and Lens's co-location changes
(`YIELD_EXTRA_NETWORKS`, `LENS_EXTRA_NETWORKS`, the `lens-minio` alias, the secrets `env_file`)
make a shared network possible, but their compose files still need a checkout to run.

## Decision

`proof stack` renders one compose project, `proof-stack`, from `appliance.lock`, and runs it on
the appliance with no checkout of Yield or Lens.

- **Services** follow Yield's and Lens's compose files with their co-location changes, with
  every image taken from the lock by digest, each store and API under a name of its own
  (`yield-minio`, `lens-minio`), host ports on loopback, and no mock target or consumer. Lens's
  Valkey is dropped: Lens's compose runs its jobs registry in memory.
- **Networks**: `proof-yield` and `proof-lens` (each stack's own, handed to its dispatcher),
  `proof-stack` (Yield's MinIO, `lens-api` and the brain registry, the hops between the two), and
  `proof-models`, external to both the stack and the models' project and created by whichever
  starts first. Every job joins `proof-stack` and `proof-models` through the extra-networks
  settings.
- **Seeds** are vendored: Yield's `deploy/seed` (minus domain catalogues, which arrive with a
  bundle) and Lens's calibration, rule set and score-card weights, each with the commit it was
  copied from in `seed/SOURCE.yaml`. They run in the locked `python` image. Lens's seed checks
  that each asset lands on the digest the stack pins, so a drifted copy fails `up`, not a run.
- **Secrets**: the operator's secrets file is the `env_file` of the three services that resolve a
  `secret_ref`, and the `--env-file` compose interpolates the store credentials from. `proof`
  writes a value only when the operator sets one, and never prints one.
- **Pinning** resolves tags with `docker buildx imagetools inspect`, which uses the registry login
  docker already holds, and records each image's platforms so `up` refuses an image the host
  cannot run.

## Consequences

- A change to Yield's or Lens's compose file does not reach the appliance until this template
  follows it. The golden file `tests/golden/stack-compose.yaml` makes that change reviewable.
- A refresh of a seed is a re-copy from a named commit and a diff, and a change to Lens's
  seed digests is a change of `SOURCE.yaml`.
- Fetching seeds from the repositories at `up` would avoid the copy, but the repositories are
  private and the appliance would need a GitHub token for it; the copy needs nothing.
