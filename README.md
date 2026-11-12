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
| `proof bundle` | Validates and loads a client bundle (see `examples/bundle-acme`) | available |
| `proof spec render` | Renders Yield run specs from a bundle's run matrix and role bindings | available |
| `proof run` | Queues, watches and re-measures a matrix of runs | planned (#7) |
| `proof lens` | Makes sure every Yield run has its Lens result | available |
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

## Bring up the stack

`proof stack` runs Yield, Lens and a brain registry from the images in an `appliance.lock`, which
names every image by digest. The repository's `appliance.lock` is pinned to the `develop` images,
published for amd64 and arm64. Copy it to `$PROOF_HOME/appliance.lock`, or write a fresh one:

```sh
docker login ghcr.io                      # Yield's and Lens's images are private
proof stack pin                           # develop by default; refuses images this host cannot run
proof secrets set ACME_TARGET_KEY         # hidden prompt, or a value on stdin; once per secret
proof secrets list                        # names only
proof stack up --bundle <bundle>          # render, pull, start, seed, wait until healthy
proof stack status                        # image digest, state, health and ports per service
proof doctor --no-models --stack          # each service's health on its loopback port
proof stack down                          # stop; the stores and the registry keep their volumes
```

`up` reads the models manifest (`$PROOF_HOME/models.yaml`, or `--manifest`) for two things: the
models' network every job joins, and the chat model Lens's Reader and Curator ask, with its
reasoning switch off. It refuses a manifest with no chat model. It renders compose under
`$PROOF_HOME/stack` with Yield's and Lens's seed assets beside it, so the host needs no checkout of
either repository, and `status`, `logs` and `down` act on that file without rendering it again.

No container loads the secrets file. Yield's API and compute get exactly the secrets the bundles
the stack serves declare, and `up` refuses one the file does not define; Lens gets none. `up
--bundle <dir>` adds a bundle to `$PROOF_HOME/stack/bundles.json`, every later `up` keeps serving
it, and `proof stack forget-bundle <client>` removes it. Without a bundle every run is refused at
Yield's gate, which requires the connector's secret. `proof stack render` prints a preview and
writes the running stack's file only with `--write`.
Compose also substitutes the store credentials from the file when they are set there
(`YIELD_S3_ACCESS_KEY`, `YIELD_S3_SECRET_KEY`, `LENS_S3_ACCESS_KEY`, `LENS_S3_SECRET_KEY`,
`LENS_WEBHOOK_SIGNING_SECRET`), else the development defaults.

| From the host | Port | From a job container |
|---|---|---|
| Yield API | `127.0.0.1:8080` | `http://yield-api:8080` |
| Yield compute | `127.0.0.1:8081` | `http://yield-compute:8080` |
| Lens API | `127.0.0.1:8090` | `http://lens-api:8080` (a run's `webhook_url` is `http://lens-api:8080/webhooks/yield`) |
| Brain registry | `localhost:5051` (`vitruvio dist push --insecure`) | `brain-registry:5000` (a run's `kb_ref`) |
| Models | `127.0.0.1:<port>` | `http://<name>:8000/v1` |

Every job Yield and Lens launch joins `proof-stack` (the hops between the two) and the models'
network.
[ADR 0002](docs/adr/0002-the-stack-is-one-compose-project-rendered-from-the-lock.md) says why the
stack is rendered here rather than taken from each repository's compose file.

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
nothing is written, when any value of the secrets file appears in any file, as written,
JSON-escaped, percent-encoded or base64-encoded; zstd traces are scanned decompressed, and one that
does not decompress refuses the package too. The refusal names the file and the secret, never the
value. Values shorter than 8 characters are too short to match without refusing ordinary text:
they are not looked for, and the export names those secrets on stderr and in `index.json`.

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
