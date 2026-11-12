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
| `proof doctor` | Probes every served model the way runs use it, and the stack with `--stack` | available |
| `proof stack` | Brings up Yield, Lens and the brain registry from pinned images | available |
| `proof secrets` | Sets a credential in the operator's secrets file; lists names, never values | available |
| `proof bundle` | Validates and loads a client bundle | planned (#5) |
| `proof spec render` | Renders Yield run specs from a run matrix and the model roles | planned (#6) |
| `proof run` | Queues, watches and re-measures a matrix of runs | planned (#7) |
| `proof lens` | Makes sure every Yield run has its Lens result | planned (#8) |
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

## Bring up the stack

`proof stack` runs Yield, Lens and a brain registry from the images in an `appliance.lock`, which
names every image by digest. The repository's `appliance.lock` is pinned to the `develop` images,
published for amd64 and arm64. Copy it to `$PROOF_HOME/appliance.lock`, or write a fresh one:

```sh
docker login ghcr.io                      # Yield's and Lens's images are private
proof stack pin --channel develop         # or `release` (amd64 only until the next promotion)
proof secrets set TARGET_KEY              # hidden prompt, or a value on stdin; once per secret
proof secrets list                        # names only
proof stack up                            # pull, start, seed, wait until healthy
proof stack status                        # image digest, state, health and ports per service
proof doctor --no-models --stack          # each service's health on its loopback port
proof stack down                          # stop; the stores and the registry keep their volumes
```

Compose renders under `$PROOF_HOME/stack` with Yield's and Lens's seed assets beside it, so the
host needs no checkout of either repository. `up` refuses an image with no build for the host's
architecture, a secrets file others can read, and a secret named `YIELD_*` or `LENS_*` that would
reconfigure a service (the store credentials are the exception: `YIELD_S3_ACCESS_KEY`,
`YIELD_S3_SECRET_KEY`, `LENS_S3_ACCESS_KEY`, `LENS_S3_SECRET_KEY` and
`LENS_WEBHOOK_SIGNING_SECRET` override the development defaults).

| From the host | Port | From a job container |
|---|---|---|
| Yield API | `127.0.0.1:8080` | `http://yield-api:8080` |
| Yield compute | `127.0.0.1:8081` | `http://yield-compute:8080` |
| Lens API | `127.0.0.1:8090` | `http://lens-api:8080` (a run's `webhook_url` is `http://lens-api:8080/webhooks/yield`) |
| Brain registry | `localhost:5051` (`vitruvio dist push --insecure`) | `brain-registry:5000` (a run's `kb_ref`) |
| Models | `127.0.0.1:<port>` | `http://<name>:8000/v1` |

Every job Yield and Lens launch joins `proof-stack` (the hops between the two) and `proof-models`.
[ADR 0002](docs/adr/0002-the-stack-is-one-compose-project-rendered-from-the-lock.md) says why the
stack is rendered here rather than taken from each repository's compose file.

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
