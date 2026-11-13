# 0001. The appliance is generic; a client is a bundle

Status: accepted (2026-11-12)

## Context

The first two engagements (November 2026) were run with scripts written for each one. They
built specs with model ids and an OpenRouter URL written in, queued runs one at a time, polled
logs, launched Lens by hand, and kept keys in plain env files beside the client's material. None of
it carried over to the next engagement. The next one runs every model on a single DGX Spark with
vLLM, which none of those scripts could do.

Yield already treats a client as data (Yield ADR 0016): catalogues, target profiles, priors and
mappings are published to its store, not baked into an image. Lens reads declarations the same way.

## Decision

`proof` is one CLI for running an engagement on one host, and it knows no client.

- **What the host serves** is a models manifest: which models, their images, flags, ports, memory
  shares and the roles they fill. The repository ships example manifests and no defaults in code.
- **What the engagement is about** is a bundle: catalogues, profiles, priors, mappings, brains,
  declarations, the run matrix and the names of the secrets the bundle needs. Bundles live outside
  this repository.
- **What the stack runs** is a lock of image digests, so an engagement records exactly which
  Yield and Lens measured it.
- **Secrets** live in one operator file outside any bundle, are referenced by name, and are never
  written to a bundle, a spec, a log or the evidence.
- `proof` runs on the appliance host and drives its local docker daemon. Remote operation is SSH
  to the host.
- Rich renders every command's output, and Textual renders the interactive views. Read commands
  also take `--json` for scripting.

## Consequences

- A new client needs a new bundle, never a change to this repository.
- A new kind of client material is a change to the bundle schema and its loader, made once for
  every client.
- An engagement's evidence names the manifest, the lock and the bundle it ran with.
