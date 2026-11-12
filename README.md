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
| `proof run` | Queues, watches and re-measures a matrix of runs | available |
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

Read commands take `--json`. Results go to standard output, and messages for the operator go to
standard error.

## Run the matrix

`proof spec render` writes one spec per run to `$PROOF_HOME/bundles/<client>/specs/`, each
delivering its results to the stack's Lens (`http://lens-api:8080/webhooks/yield`) unless the
matrix sets `webhook_url`. `proof run` submits them to Yield and follows them until each one closes.

```sh
proof run queue <bundle>                  # two runs in flight at a time; --concurrency to change
proof run queue <bundle> --only r1-disclosure
proof run queue <bundle> --resume         # continue after a crash or a Ctrl-C
proof run status <bundle> --json          # each run's place in the queue and in Yield
proof run watch <bundle>                  # the dashboard; --plain over SSH, --json for one sample
proof run remeasure <bundle> <run> --metric bias   # a failed run that holds its dataset
```

The queue asks Yield about a run before posting it: a run Yield closed or is measuring is never
posted again, and anything else is posted with the bytes Yield first accepted, which Yield answers
with 202 while a driver runs and relaunches when none does. A run Yield reads as failed is posted
again (`--retries`, default 1) and keeps its slot until Yield shows the relaunched attempt. A run
whose attack stops advancing for `--stall-minutes` is posted again up to twice, then closed as
`stalled` so the queue ends. Its state is `queue.json` beside the specs, the accepted specs are
kept in `submitted/`, and everything it did, including the anomalies it found, is appended to
`events.jsonl`:

| Anomaly | When |
|---|---|
| `stalled` | a run's phase and counts (or, while measuring, the measure's note) stop moving |
| `liveness` | Yield reads the driver or the measure as `stalled` or `dead` |
| `failed_traces` | the run's failed units grow |
| `rate_limited`, `timeout`, `target_error` | new 429, timeout or target error lines in `yield-gen-<run>`, `yield-driver-<run>` or `yield-meas-<run>` |
| `measure_idle` | a running measure has made no model call for six minutes |
| `container_exited` | a job container exited non-zero, died or is restarting |
| `model_down` | a model of the manifest is not running, or its health check fails |
| `stack_down` | a service of the `proof-stack` project is not running or unhealthy, or a one-shot one exited non-zero |
| `unreachable` | Yield, the docker daemon or the models project cannot be read |

`proof run watch` shows the same, and only reads: it never posts, writes the queue or appends to
its log.

`proof run remeasure` measures a run Yield reads as failed and that has no manifest, such as one
whose measure died on a rate limit. A complete run cannot be re-measured: Yield closes a run once
and refuses a different result under the same key, so measuring differently needs a new run id.
It also refuses while compute holds an earlier `measure-<run>` record, which would answer in place
of the new request, and it never reports a job older than its own request.

## Develop

```sh
uv sync
uv run ruff check . && uv run ruff format .
uv run mypy
uv run pytest -q -m "not appliance"
```

Tests marked `appliance` need the real host: a GPU, a docker daemon and served models.

See [CONTRIBUTING.md](CONTRIBUTING.md) for branches, commits and releases.
