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
| `proof lens` | Makes sure every Yield run has its Lens result | available |
| `proof evidence` | Exports a checksummed evidence package | planned (#9) |

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

## Lens results

Every finished Yield run should end with a Lens result. `proof lens status <client>` lists the
client's runs (from the queue state, else the rendered specs), the Lens project and run each
delivery derives (`decl-<first 20 hex of the declaration>`, the Yield run id), whether Lens
received and admitted it, its stage and its result. It exits non-zero while any run has none.

```sh
proof lens status acme
proof lens run acme             # every run of the client without a result
proof lens run acme <run-id>    # one run
```

`proof lens run` launches only an admitted run with no result and no live job, so running it twice
is harmless. It asks lens-api to relaunch first (`POST /projects/{p}/runs/{r}/launch`); when that
cannot launch, it starts `lens-run` by hand with the environment the running lens-api container
gives its jobs, through a 0600 env file deleted right after (`--manual` skips the relaunch,
`--no-fallback` forbids the hand launch). A relaunch Lens refuses (404, 409) is reported and never
launched by hand; a lens-api without the relaunch route says so and points at `--manual`. The hand
launch uses lens-api's `LENS_DOCKER_NETWORK`, as its dispatcher does. A
dead container holding the run's name is recorded first (exit code, last log lines with every
environment value of 8 or more characters redacted) under
`$PROOF_HOME/bundles/<client>/lens/kills/`, then removed. It then waits for the result
(`--timeout`).

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
