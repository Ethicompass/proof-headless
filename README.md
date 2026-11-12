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
| `proof models` | Serves the models in a manifest with vLLM, within a memory budget | planned (#2) |
| `proof doctor` | Probes every model and service a run depends on | planned (#3) |
| `proof stack` | Brings up Yield, Lens and the brain registry from pinned images | planned (#4) |
| `proof bundle` | Validates and loads a client bundle | planned (#5) |
| `proof spec render` | Renders Yield run specs from a run matrix and the model roles | planned (#6) |
| `proof run` | Queues, watches and re-measures a matrix of runs | planned (#7) |
| `proof lens` | Makes sure every Yield run has its Lens result | planned (#8) |
| `proof evidence` | Exports a checksummed evidence package | planned (#9) |

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
