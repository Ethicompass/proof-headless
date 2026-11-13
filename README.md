# proof-headless

`proof` runs an Ethi Proof engagement on one host. It serves the models with vLLM, brings up Yield
and Lens, loads a client's bundle, runs the engagement and exports its evidence. Everything about
a client arrives in a bundle
([ADR 0001](docs/adr/0001-the-appliance-is-generic-a-client-is-a-bundle.md)).

## Install

On the appliance:

```sh
curl -fsSL https://raw.githubusercontent.com/Ethicompass/proof-headless/main/install.sh | sh
```

Or, with a copy of the repository already on the appliance: `sh install.sh`.

The script installs [uv](https://docs.astral.sh/uv/) if missing and `proof` in `~/.local/bin`,
copies the DGX Spark models manifest to `~/.proof/models.yaml` and creates
`~/.config/proof/secrets.env` (mode 600). Downloaded, the source lands in `~/src/proof-headless`,
with the manifests and the example bundles. Run it again to upgrade; it keeps the manifest and the
secrets. `PROOF_VERSION=<tag>` installs a given release.

## Use

```sh
proof secrets set HF_TOKEN                           # once per secret; values never printed
proof models up && proof doctor                      # serve the models and probe them
docker login ghcr.io                                 # Yield's and Lens's images are private
proof stack pin --channel release                    # pin the released images by digest
proof stack up --bundle <bundle>                     # Yield, Lens and the brain registry
proof bundle load <bundle>                           # catalogues, profile, brains
proof spec render <bundle>                           # one Yield run spec per run
proof spec plan <bundle>                             # probes and traces per run
proof run queue <bundle> && proof run watch <bundle> # run the matrix
proof lens status <client>                           # every run has its Lens result
proof evidence export <client>                       # the checksummed evidence package
```

`proof` with no command opens the interactive shell (`q` quits). Every command takes `--help`.

| Command | What it does |
|---|---|
| `proof version` | Versions of proof and the host |
| `proof models` | Serves the models of a manifest with vLLM, within a memory budget |
| `proof doctor` | Probes every model the way runs use it, and the stack with `--stack` |
| `proof stack` | Brings up Yield, Lens and the brain registry from pinned images |
| `proof secrets` | Sets a credential in the secrets file; lists names, never values |
| `proof bundle` | Validates and loads a client bundle (see `examples/bundle-acme`) |
| `proof spec render` | Renders Yield run specs from a bundle's run matrix and role bindings |
| `proof spec plan` | Counts each spec's probes and traces with Yield's own generation |
| `proof run` | Queues, watches and re-measures the runs |
| `proof lens` | Makes sure every Yield run has its Lens result |
| `proof evidence` | Exports and verifies the evidence package |

## More

- [Operator runbook](docs/runbook-dgx-spark.md): a DGX Spark from a fresh install to an exported
  engagement.
- [Commands in detail](docs/commands.md): ports, credentials, queue anomalies, Lens fallback,
  what the evidence package holds.
- [Decisions](docs/adr/).

## Develop

```sh
uv sync
uv run ruff check . && uv run ruff format .
uv run mypy
uv run pytest -q -m "not appliance"
```

Tests marked `appliance` need the real host: a GPU, a docker daemon and served models. See
[CONTRIBUTING.md](CONTRIBUTING.md) for branches, commits and releases.
