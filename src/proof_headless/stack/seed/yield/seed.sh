#!/usr/bin/env bash
# Publish the business data a run needs into the store, once, so you can start measuring.
#
# Nothing in Yield loads these from a file any more: the contract, the catalogues, the realism
# priors, the dimension mappings and the driver vocabulary are read from the object store at the
# version a run froze. That leaves somebody having to put them there the first time, and this is
# that somebody. Run it against a fresh deployment and the stack has what it needs.
#
# THE ORDER IS NOT A PREFERENCE. Publishing a catalogue validates it against a behavioral contract
# -- the six semantic rejections check that every plugin names a principle the contract carries --
# so a store with catalogues and no contract cannot be built, and compute cannot bootstrap itself
# out of an empty store either. That is why the seeding moved out of the service's boot: the
# service would have needed a contract to validate the catalogues it was seeding. Here the
# dependency is just a line order.
#
# It publishes through the HTTP doors rather than writing keys into S3 directly, on purpose. The
# store's key layout is zero-padded and its payloads are canonically encoded, because digests are
# taken over them; a shell script writing raw objects would be a fourth hand-maintained copy of
# those rules, and this repo already has three copies of one table and a CI guard watching them.
#
#   ./deploy/seed/seed.sh                      # against http://localhost:8081
#   COMPUTE=http://yield-compute:8080 ./deploy/seed/seed.sh
#   ./deploy/seed/seed.sh --dry-run            # say what it would publish and touch nothing
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPUTE="${COMPUTE:-http://localhost:8081}"
DRY_RUN=""
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

say() { printf '  %s\n' "$*"; }

# HTTP through python3's standard library rather than curl, for the reason the compose healthcheck
# gives about itself: a slim Python base does not carry curl, and this has to run both from a
# checkout and inside the compute image.
http() { python3 "$HERE/publish.py" "$COMPUTE" "$@"; }

if ! http healthz >/dev/null 2>&1; then
  echo "compute is not answering at $COMPUTE/healthz" >&2
  echo "set COMPUTE to where it listens, or bring the stack up first" >&2
  exit 1
fi

# Which names are already published. The store only appends, so publishing again would open a
# second version rather than overwrite -- and a version number is supposed to mean "somebody
# changed this". Skipping by name is what makes a re-run a no-op for the assets whose publish is
# not idempotent. Catalogues and mappings are not skipped: compute answers an identical one with
# the version it already holds and writes nothing, and one that changed on disk -- a new strategy,
# a new sidecar, a dimension that now maps to a metric -- becomes the next version, which is
# exactly what "somebody changed this" means. Skipped by name, a stack whose volume outlived its images kept serving the old
# catalogue while the seed said it had nothing to do.
carries() { http carries "$1" "$2"; }

skip_or_note() {  # true when there is nothing to do for this name
  local kind="$1" name="$2" description="$3"
  if carries "$kind" "$name"; then
    say "$description: already published, left alone"
    return 0
  fi
  if [ -n "$DRY_RUN" ]; then
    say "$description: would publish"
    return 0
  fi
  return 1
}

printf '\nseeding %s from %s\n' "$COMPUTE" "$HERE"
[ -n "$DRY_RUN" ] && say "(dry run: nothing will be written)"

# 1. the contract, because everything below is validated against it
if ! skip_or_note contracts pi "the behavioral contract"; then
  http raw /contracts pi "$HERE/contract.yaml"
  say "the behavioral contract: published as pi"
fi

# 2. the vocabulary, which a run's driver reference is frozen from
if ! skip_or_note vocabularies drivers "the driver vocabulary"; then
  http raw /vocabularies drivers "$HERE/vocabulary.yaml"
  say "the driver vocabulary: published as drivers"
fi

# 3. the catalogues, each with the sidecars it declares published at the same version. Always
#    sent: compute dedupes by content, so an unchanged catalogue costs one request and no write.
for directory in "$HERE"/catalogues/*/; do
  name="$(basename "$directory")"
  if [ -n "$DRY_RUN" ]; then
    say "catalogue $name: would publish"
    continue
  fi
  outcome="$(http catalogue "$name" "$directory")"
  say "catalogue $name: $outcome"
done

# 4. the realism priors
for directory in "$HERE"/priors/*/; do
  name="$(basename "$directory")"
  skip_or_note priors "$name" "prior $name" && continue
  http wrapped /priors phrasings "$name" "$directory/prior.json"
  say "prior $name: published"
done

# 5. the dimension mappings. Always sent, like the catalogues: compute answers an unchanged mapping
#    with the version it already holds and writes nothing, and a mapping that changed on disk --
#    a dimension that now maps to a metric -- becomes the next version. Skipped by name, a store
#    that already carried `default` kept the mapping it had, and the gate kept refusing a dimension
#    the shipped mapping had started to cover (issue #108).
for directory in "$HERE"/mappings/*/; do
  name="$(basename "$directory")"
  if [ -n "$DRY_RUN" ]; then
    say "mapping $name: would publish"
    continue
  fi
  outcome="$(http mapping "$name" "$directory/mapping.json")"
  say "mapping $name: $outcome"
done

printf '\nthe store carries:\n'
for kind in contracts vocabularies catalogues priors mappings; do
  printf '  %-14s %s\n' "$kind" "$(http names "$kind")"
done
printf '\ndone. runs submitted to the API can be measured now.\n'
