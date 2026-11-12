# proof-headless

`proof` runs an Ethi Proof engagement on one host. It serves the models with vLLM, brings up Yield
and Lens, loads a client's bundle, runs the engagement and exports its evidence. The appliance
is generic, and everything about a client arrives in a bundle (see
[ADR 0001](docs/adr/0001-the-appliance-is-generic-a-client-is-a-bundle.md)).

## Install

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run proof --help
```

`uv run proof` with no command opens the interactive shell. Press `q` to quit.

## What it does

| Command | What it does | Status |
|---|---|---|
| `proof version` | Versions of proof and the host | available |
| `proof models` | Serves the models in a manifest with vLLM, within a memory budget | available |
| `proof doctor` | Probes every served model the way runs use it | available (models; stack probes come with #4) |
| `proof stack` | Brings up Yield, Lens and the brain registry from pinned images | planned (#4) |
| `proof bundle` | Validates and loads a client bundle (see `examples/bundle-acme`) | available |
| `proof spec render` | Renders Yield run specs from a bundle's run matrix and role bindings | available |
| `proof run` | Queues, watches and re-measures a matrix of runs | planned (#7) |
| `proof lens` | Makes sure every Yield run has its Lens result | planned (#8) |
| `proof evidence` | Exports a checksummed evidence package | available |

## Serve the models

The models a host serves are declared in a manifest (`manifests/dgx-spark/models.yaml` is the one
for a DGX Spark). Copy it to `$PROOF_HOME/models.yaml` (default `~/.proof/models.yaml`) or pass
`--manifest`. Gated weights need `HF_TOKEN` in the secrets file
(`~/.config/proof/secrets.env`, mode 600).

```sh
proof models validate      # check the manifest and its memory arithmetic
proof models up            # start each model after the previous is healthy
proof models status        # container state and health per model
proof models logs chat     # the last lines of one model's log
proof models down          # stop them; downloaded weights stay cached
```

`proof doctor` then probes each model the way a run will use it: chat models for logprobs with
twenty alternatives, JSON-schema output and their reasoning switch, the guard for its verdict, the
embedder and the reranker for sane answers. It exits non-zero on any failure and writes a JSON
report under `$PROOF_HOME/evidence/`.

Each model is reachable from other containers as `http://<name>:8000/v1` on the
`proof-models` network, and from the host on `127.0.0.1:<port>` only.

## Export the evidence

```sh
proof evidence export acme              # to $PROOF_HOME/exports/acme-<time>
proof evidence verify <package-dir>     # or: cd <package-dir> && sha256sum -c SHA256SUMS
```

The package holds the bundle's receipt and rendered specs, the models manifest and compose file,
`appliance.lock` when present, every running container's image digests, what each model server
reports at `/version`, the doctor's reports, and per run Yield's status and manifest, the run's
objects in Yield's store (traces, dataset) and the Lens result. Its README lists the layout and
every gap. The store is read over its S3 API with `YIELD_S3_ACCESS_KEY` and `YIELD_S3_SECRET_KEY`
from the secrets file; without them the traces are a recorded gap. The export is refused, and
nothing is written, when any value of the secrets file appears in any file; the refusal names the
file and the secret, never the value.

Read commands take `--json`. Results go to standard output, and messages for the operator go to
standard error.

## Develop

```sh
uv sync
uv run ruff check . && uv run ruff format .
uv run mypy
uv run pytest -q -m "not appliance"
```

Tests marked `appliance` need the real host: a GPU, a docker daemon and served models.

See [CONTRIBUTING.md](CONTRIBUTING.md) for branches, commits and releases.
