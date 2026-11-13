# Runbook: an engagement on a DGX Spark

This runbook takes an NVIDIA DGX Spark from a fresh install to a finished engagement with
`proof`: the four models served with vLLM, Yield and Lens running, a bundle loaded, its runs
measured, every run's Lens result in place, and a checksummed evidence package exported. It then
tears everything down and lists what to do when a step fails.

The walk-through uses the rehearsal bundle in `examples/bundle-rehearsal`. It needs no client
material, and its target is the appliance's own chat model, so it is the right first engagement on
a new appliance. A real engagement follows the same steps with the client's bundle in its place.

Placeholders: `<appliance-host>` is the appliance's address, `<operator>` is your user on it,
`<bundle>` is a bundle's directory, `<client>` is the `client` named in its `bundle.toml`, and
`<run-id>` is a run's `id` in its `matrix.toml`.

Numbers marked as measured come from the DGX Spark load test and rehearsal of 2026-11-12 and 13.

## 1. Prerequisites

On the appliance (DGX OS, aarch64, GB10 with 128 GB of unified memory):

| What | Check | Notes |
|---|---|---|
| NVIDIA driver and CUDA | `nvidia-smi` | Shows the GB10. DGX OS ships both. |
| Docker Engine 29 | `docker version` | Your user in the `docker` group, so `proof` drives the daemon without `sudo`. |
| Docker Compose v2.24 or later | `docker compose version` | v5 was measured on the appliance. |
| NVIDIA container toolkit, or CDI | `docker run --rm --gpus all ubuntu nvidia-smi` | The model containers reserve the GPU through Compose's `driver: nvidia` device request. |
| Disk | `df -h ~`, `docker system df` | Measured with the four models and the stack: 62 GB of weights in `~/.cache/huggingface`, 26.9 GB of images (24.5 GB of them the vLLM 0.32.0 image), 1.6 GB of containers, a few MB in `$PROOF_HOME`: about 90 GB. Keep at least 200 GB free, for evidence exports and a second vLLM image during an upgrade. |
| Outbound HTTPS | | To Hugging Face, GitHub, ghcr.io, Docker Hub, PyPI and astral.sh (uv). |

Credentials, before the day:

- **SSH key access** to `<operator>@<appliance-host>`. Everything below runs in a shell on the
  appliance. Run long commands (`proof models pull`, `proof models up`, `proof run queue`) inside
  `tmux` or `screen`, so a dropped SSH session does not stop them.
- **A Hugging Face token** for the gated Llama Guard weights. On the Hugging Face account that
  owns the token, accept Meta's license on the `meta-llama/Llama-Guard-4-12B` model page and wait
  until access is granted. A fine-grained token also needs the permission "Read access to
  contents of all public gated repos you can access"; without it the download answers 403 even
  with the license accepted. A classic read token works as is.
- **A ghcr.io login able to read the Ethicompass images.** Yield's and Lens's images are private.
  Use a GitHub account with read access to the Ethicompass packages and a personal access token
  with `read:packages`:

  ```sh
  docker login ghcr.io -u <github-user>     # paste the token at the prompt
  ```

- **The bundle's secrets**, one value per name its `bundle.toml` lists under `secrets`.

## 2. Install proof on the appliance

On the appliance:

```sh
curl -fsSL https://raw.githubusercontent.com/Ethicompass/proof-headless/main/install.sh | sh
```

The script downloads the latest release (`PROOF_VERSION=<tag>` for another) into
`~/src/proof-headless`, installs uv if missing and `proof` with `uv tool install` (its own Python
3.12; `proof` lands in `~/.local/bin`), copies `manifests/dgx-spark/models.yaml` to
`~/.proof/models.yaml` and creates an empty `~/.config/proof/secrets.env` at mode 600. Running it
again upgrades `proof` and leaves the manifest and the secrets as they are. On an appliance with no
route to GitHub, copy the repository over from a workstation and run the script from the copy:

```sh
git archive --format=tar.gz --prefix=proof-headless/ <tag> \
  | ssh <operator>@<appliance-host> 'rm -rf ~/src/proof-headless && mkdir -p ~/src && tar -xzf - -C ~/src && sh ~/src/proof-headless/install.sh'
```

`~/.local/bin` must be on your `PATH` (DGX OS adds it at login once the directory exists; log in
again if `proof` is not found). `~/src/proof-headless` also carries `manifests/` and `examples/`,
which the steps below use.

A bundle with brains needs the `vitruvio` CLI on the appliance's `PATH` too, because
`proof bundle load` pushes brains with `vitruvio dist push`. The rehearsal bundle has no brain.

## 3. Where proof keeps things

| Path | What |
|---|---|
| `$PROOF_HOME` (default `~/.proof`) | Everything `proof` renders and records. |
| `$PROOF_HOME/models.yaml` | The models manifest every command reads unless given `--manifest`. |
| `$PROOF_HOME/appliance.lock` | The stack's image digests, written by `proof stack pin`. |
| `$PROOF_HOME/models/`, `$PROOF_HOME/stack/` | The rendered compose files. `stack/bundles.json` lists the bundles the stack serves. |
| `$PROOF_HOME/bundles/<client>/` | The receipt, the rendered specs, the queue state (`queue.json`, `events.jsonl`, `submitted/`) and Lens records. |
| `$PROOF_HOME/evidence/` | The doctor's JSON reports. |
| `$PROOF_HOME/exports/` | Evidence packages. |
| `~/.config/proof/secrets.env` (or `$PROOF_SECRETS_FILE`) | Credentials, mode 600. No container loads it. |
| `~/.cache/huggingface` | Model weights (the manifest's `hf_cache`). |

## 4. Secrets

`proof secrets set NAME` reads the value from a hidden prompt, or from standard input when piped.
It never prints a value; `proof secrets list` shows names only.

```sh
proof secrets set HF_TOKEN
printf 'not-a-real-key' | proof secrets set REHEARSAL_TARGET_KEY
proof secrets list
```

`HF_TOKEN` goes to the model containers only. Each bundle secret goes to Yield's API and compute
only, and only while a bundle that declares it is served. For a real bundle, set each name its
`bundle.toml` lists; `proof bundle validate` and `proof stack up` refuse a declared secret the
file does not define.

Do not set the stack's own credentials (`YIELD_S3_ACCESS_KEY`, `YIELD_S3_SECRET_KEY`,
`LENS_S3_ACCESS_KEY`, `LENS_S3_SECRET_KEY`, `LENS_WEBHOOK_SIGNING_SECRET`): the first
`proof stack up` generates each one the file lacks and never replaces one it has.

The evidence export refuses a package that contains any secret's value. Values shorter than 8
characters are not scanned (the export names them), so give every secret at least 8 characters.

## 5. Serve the models

The DGX Spark manifest serves four models:

| Name | Model | Port | Memory share | Roles |
|---|---|---|---|---|
| `chat` | Nemotron 3.5 Lightning 30B-A3B NVFP4 | 8001 | 0.40 | chat: judge, metric judge, generator, attacker, Lens's Reader and Curator |
| `guardian` | Llama Guard 4 12B (gated, online FP8) | 8002 | 0.15 | guardian |
| `rerank` | Nemotron 3.5 Rerank 8B | 8003 | 0.16 | rerank |
| `embed` | Nemotron 3.5 Embed 1B | 8004 | 0.05 | embed |

The shares add up to 0.76 against a budget of 0.80.

The install script copied this manifest to `~/.proof/models.yaml`. Check it:

```sh
proof models validate
```

`validate` checks the manifest and its memory arithmetic and prints what it declares. It exits 2
on an invalid manifest, with the shares table when the budget is the problem.

### Download the weights first

```sh
proof models pull                # all four; --only guardian for one
```

Recommended before the first `up`. Measured: a first start that also downloads took 27 to 47
minutes per model. `pull` runs `hf download` inside each model's own image, against the same cache
and the same `HF_TOKEN` the server gets, so `up` then only loads. Running it again only checks
what is already cached.

### Start them

```sh
proof models up
```

`up` checks the memory budget, the memory the host has available for the models not yet running,
and that `HF_TOKEN` is set when a model is gated. It then starts the models one at a time, each
after the previous reports healthy. A model still downloading or loading past its health check's
start period (20 minutes by default) reads `unhealthy` but is waited on while its container runs
and has not restarted, up to the start period plus `--grace` (3600 seconds by default); its status
line shows the bytes in its cache directory and the rate they arrive at. A model that exits, dies
or restarts stops the sequence at once and prints its log. `--only NAME` starts some models;
`--force` starts even when memory looks short.

```sh
proof models status              # container state and health per model
proof models logs chat --tail 100
```

Each model answers on `127.0.0.1:<port>` from the host, and on `http://<name>:8000/v1` from
containers on the `proof-models` network.

### Probe them

```sh
proof doctor
```

`doctor` calls each model the way a run will, prints a table, writes a JSON report under
`$PROOF_HOME/evidence/` and exits 1 on any failure. The probes:

| Probe | Model | Passes when |
|---|---|---|
| `chat completion` | chat | The model answers with content with its reasoning switched off. |
| `logprobs` | chat | Every answer token carries at least 20 logprob alternatives, which the judge's grader reads. |
| `structured output` | chat | A JSON-schema-constrained answer parses and fits the schema. |
| `reasoning switch` | chat | Reasoning appears with the switch on and disappears with it off. |
| `guardian verdict` | guardian | `safe` for a benign request, `unsafe` with an S-category for a harmful one. |
| `embeddings` | embed | One vector per input, one dimension, and the same text embeds to a cosine of at least 0.9999. |
| `rerank` | rerank | The relevant passage outranks the irrelevant one; the score scale (probability or logit) is reported. |

`--only NAME` probes some models; `--timeout` sets the seconds per request (300 by default).

Measured memory: with the four models and the stack running, the appliance used about 106 of the
121 GiB the host reports. Leave the rest alone: no other GPU work on the appliance during an
engagement.

## 6. Bring up the stack

The stack is Yield (API, compute, driver), Lens (API, run), two MinIO stores, Valkey and a brain
registry, all from the images an `appliance.lock` names by digest.

```sh
proof stack pin --channel release     # or --channel develop for unreleased fixes
```

`pin` resolves every image of the channel to a digest with the ghcr.io login docker holds and
writes `$PROOF_HOME/appliance.lock`. It refuses an image the appliance's platform (arm64) cannot
run. `develop` follows Yield's and Lens's integration branches; `release` names the released
versions. Review a change of the lock like a change of code: it decides which Yield and Lens
measure the engagement. A run resumed after a failure replays its recorded grades with
`yield-driver` 0.1.11 or later, which both channels now pin (`release` names yield api 0.1.14,
driver 0.1.12 and compute 0.1.16, lens api 0.1.11 and run 0.1.13). Compute 0.1.16 reads a
reasoning metric judge's label from its answer (yield #168). `--set NAME=REF` resolves one image
from another reference.

```sh
proof stack up --bundle ~/src/proof-headless/examples/bundle-rehearsal
proof stack status
proof doctor --no-models --stack
```

`up` renders the compose project under `$PROOF_HOME/stack`, generates the stack's credentials the
secrets file lacks, pulls the images, starts and seeds the services, and waits until every one is
healthy. It needs the models manifest (the stack joins its network, and Lens asks its chat model),
so start the models first. `--bundle` adds a bundle to the ones the stack serves; every later `up`
keeps serving it, with or without `--bundle`. Serving a bundle is what hands its secrets to
Yield; without one, Yield refuses every run.

`proof stack status` shows each service's image digest, state, health and ports.
`proof stack logs <service> --tail 200` prints one service's log.

`proof doctor --stack` adds a health probe per service on its loopback port: `yield-api`,
`yield-compute` and `lens-api` on `/healthz`, `yield-minio` and `lens-minio` on
`/minio/health/live`, the brain registry on `/v2/`, and a Valkey `PING`. `--no-models` skips the
model probes.

| From the host | Port |
|---|---|
| Yield API | `127.0.0.1:8080` |
| Yield compute | `127.0.0.1:8081` |
| Lens API | `127.0.0.1:8090` |
| Yield's MinIO | `127.0.0.1:9000` |
| Brain registry | `localhost:5051` |

To stop serving a bundle, for example after an engagement so its secrets leave Yield:

```sh
proof stack forget-bundle <client>
proof stack up                        # applies it
```

## 7. Load a bundle

A bundle is a directory, kept outside this repository next to the client's material:

```
<bundle>/
  bundle.toml        client, language, secrets, context, and the pieces below
  matrix.toml        the runs: connector, budget, metrics and one [[runs]] table per run
  roles.toml         which model plays each role, and each model's requests per second
  catalogues/        strategy catalogues, one directory each
  profiles/          target profiles (how the target is reached)
  priors/            realism priors (optional)
  mappings/          metric-to-dimension mappings (optional)
  brain/             vitruvio.toml for each brain (optional)
  declarations/      Lens declarations, each named by its canonical digest
```

`examples/bundle-acme` shows every kind of piece; `examples/bundle-rehearsal` is the one that
runs.

```sh
B=~/src/proof-headless/examples/bundle-rehearsal
proof bundle validate $B
proof bundle load $B
```

`validate` checks files, paths, sidecars, priors, declarations and that every declared secret is
set, without contacting any service. `load` validates, then publishes the catalogues, profiles,
priors and mappings to Yield compute, pushes brains to the appliance's registry, stages the
declarations, and writes `$PROOF_HOME/bundles/<client>/receipt.json` with each piece's version
and each brain's digest. A load that stops writes an incomplete receipt; fix the cause and load
again. `--skip-brains` keeps the last receipt's brain digests instead of pushing again.

## 8. Render the run specs

```sh
proof spec render $B
```

One JSON spec per run lands in `$PROOF_HOME/bundles/<client>/specs/`, pinned to the last load's
receipt, with each role bound to a model of the manifest by `roles.toml` and results delivered to
the stack's Lens. `--bind ROLE=MODEL` rebinds a role for this render; `--json` prints the specs.

Render again after every `bundle load`. Do not render again between a run's first submission and
its end: Yield freezes a run's spec, and a different spec under the same run id is refused (409).

## 9. Run the matrix

```sh
proof run queue $B --concurrency 2
```

The queue submits the specs to Yield, keeps `--concurrency` runs in flight (2 by default), follows
each until it closes, and then follows each complete run to its Lens result. It asks Yield about a
run before posting it, so it never posts a closed or measuring run again. A run Yield reads as
failed is posted again up to `--retries` times (1 by default); the relaunched driver resumes from
what the store holds without calling the target for closed units. `--only <run-id>` queues some
runs. Ctrl-C stops the queue, not the runs; continue with:

```sh
proof run queue $B --resume
```

From a second shell:

```sh
proof run watch $B                # the dashboard
proof run watch $B --plain        # lines, for a plain SSH terminal
proof run watch $B --json         # one sample as JSON
proof run status $B               # each run's place in the queue and in Yield
```

`watch` only reads. It shows the runs, the models, the stack and the anomalies the queue looks
for: `stalled`, `driver_silent`, `liveness`, `failed_traces`, `rate_limited`, `timeout`,
`target_error`, `measure_idle`, `container_exited`, `model_down`, `stack_down` and
`unreachable`. The queue appends everything it does and every anomaly to
`$PROOF_HOME/bundles/<client>/events.jsonl`.

A run whose measure failed but whose dataset is stored (no manifest, Yield reads it as failed) can
be measured again without attacking again:

```sh
proof run remeasure $B <run-id>                    # every metric of the run
proof run remeasure $B <run-id> --metric bias      # one metric
```

A complete run cannot be re-measured: Yield closes a run once. Measuring it differently needs a
new run id.

### Sizing a real engagement

Measured in the load test (three concurrent runs of 8 replicas, 16 to 32 conversations each,
against the appliance's chat model): Lightning served 936 requests in 1 h 48 min with none
errored, a mean latency of 16 s, about 120 generated tokens per second, 2 to 3 requests in flight
and its KV cache under 1%. The GPU had headroom. The bottleneck was the judge grading each unit
in sequence with reasoning on, at 22 to 41 s per grade.

- Start with `--concurrency 4` to `6`, and watch latency and anomalies before raising it. This is
  a recommendation from the headroom above, not a measurement: the load test ran 3 at once.
- Size each run's `budget.max_wall_seconds` in `matrix.toml` so the whole run fits in one
  attempt: count its conversations times about 40 s of grading each, plus the attack, with a
  margin. A run that outlives its budget fails and needs a resume.
- A resume replays the recorded grades (yield driver 0.1.11 or later, on both channels), so it
  does not grade closed units again.
- The rehearsal's two runs (12 and 18 target calls) fit well inside their 3600 s budget.

## 10. Lens results

The queue launches Lens's job for a complete run whose result has not appeared after
`--lens-wait-minutes` (10 by default). Check every run once the queue ends:

```sh
proof lens status <client>                 # exits 1 while any run has no result
proof lens run <client>                    # every run without a result
proof lens run <client> <run-id>           # one run
```

`lens status` lists each run's derived Lens project (`decl-` plus the first 20 hex of the
declaration) and run id, whether Lens received and admitted the delivery, its stage and its
result. `lens run` asks lens-api to relaunch an admitted run with no result and no live job, and
launches `lens-run` by hand when the relaunch cannot; it is safe to run twice. `--manual` goes
straight to the hand launch, `--no-fallback` forbids it, `--timeout` sets how long to wait for
each result (3600 s by default).

For the rehearsal, `<client>` is `rehearsal`.

## 11. Export the evidence

```sh
proof evidence export <client>             # to $PROOF_HOME/exports/<client>-<time>
proof evidence verify <package-dir>
```

The package holds the receipt and specs, the models manifest and compose files, the lock, every
running container's image digest, each model server's version, the doctor's reports, and per run
Yield's status and manifest, its objects in Yield's store (traces, dataset) and its Lens result.
Its `README.md` lists the layout and every gap: a failed run without a manifest or Lens result
shows as a gap rather than stopping the export. `verify` re-checks every file against
`SHA256SUMS` (`sha256sum -c SHA256SUMS` from inside the package does the same) and exits 1 on any
difference. `--out` chooses the directory; `--no-store` skips the store's objects.

Measured: the rehearsal's package had no gaps, traces included, and passed its secret scan.

Copy the package off the appliance, for example `scp -r <operator>@<appliance-host>:<package-dir> .`
from the workstation.

## 12. Teardown

```sh
proof stack forget-bundle <client>         # the bundle's secrets leave Yield on the next up
proof stack down
proof models down
```

| Command | Removes | Keeps |
|---|---|---|
| `proof stack down` | The stack's containers. | The volumes of both stores and the registry (`proof-stack_yield_minio`, `proof-stack_lens_minio`, `proof-stack_brain_registry`): runs, traces, Lens results, brains. |
| `proof models down` | The model containers. | The weights in `~/.cache/huggingface`. |

Neither touches `$PROOF_HOME` (receipts, specs, queue state, doctor reports, exports) or the
secrets file. The next `proof stack up` and `proof models up` start from what was kept. Remove a
volume (`docker volume rm <name>`) or the bundle's `$PROOF_HOME/bundles/<client>` only after its
evidence package is exported, verified and copied off the appliance, and only when the engagement's
retention terms ask for it. Remove the bundle's secrets with an editor on the secrets file.

## 13. Troubleshooting

### `proof models validate` or `up` refuses the memory budget

- **"Invalid manifest" with a shares table** (exit 2): the shares add up to more than
  `memory_budget`. Lower a share. Do not raise the budget past 0.80: the OS, the stack and the
  runs' containers live in the rest (measured: about 106 of 121 GiB in use with everything up).
- **"Refused. The models to start need more memory than is available"**: something else holds
  memory. Check `free -g` and `docker ps` for a stray container (an old model project, another
  vLLM). Stop it, or pass `--force` to try anyway.
- **The check passed but vLLM refused at start** (its log says it cannot get the memory it was
  asked for): on the GB10 the page cache the kernel counts as available is not always handed back
  to the CUDA allocator. Free it with `sync; echo 3 | sudo tee /proc/sys/vm/drop_caches`, then run
  `proof models up` again; models already healthy are not restarted.

### A model reads `unhealthy` on its first start

Usually it is still downloading. The status line under `up` shows the bytes in its cache directory
and the rate; as long as the bytes grow and the container has not restarted, `up` keeps waiting
until the start period plus `--grace`. `proof models logs <name>` shows the download or the load.
If `up` gave up on a slow link, run `proof models pull --only <name>` and then `proof models up`
again. A model that restarts or exits is a real failure: read its log.

### A doctor probe fails

| Probe | Likely cause |
|---|---|
| `chat completion` "reasoning only" | The reasoning switch did not turn reasoning off: the manifest's `reasoning_switch` is not the key the chat template reads. |
| `logprobs` fewer than 20 alternatives | The server lacks `--max-logprobs=20` in the manifest's `args`. |
| `structured output` "not JSON" | Reasoning leaked into the content, or guided decoding is not active in this vLLM build. |
| `reasoning switch` | Wrong `reasoning_switch` key, or the `--reasoning-parser` argument is missing, so reasoning is not split from the content. |
| `guardian verdict` | The guard is not answering in its `safe`/`unsafe` format: wrong weights, or the FP8 quantization changed its labels. Compare with BF16 before an engagement. |
| `embeddings` | The model is not the embedder, or the cosine of the same text drifted: check `--dtype` and that the server is the one the manifest names. |
| `rerank` irrelevant above relevant | The reranker is served without its chat template, or the API (`rerank` or `classify`) does not match the model. |
| Any probe: connection refused or timeout | The model is not up, or still loading: `proof models status`, then `proof models logs <name>`. Raise `--timeout` on a cold model. |
| Any `--stack` probe | The service is down or restarting: `proof stack status`, then `proof stack logs <service>`. |

### A run stalls, or shows `driver_silent`

`driver_silent` means the run's counts are flat for `--stall-minutes` (45 by default, capped by the
run's `max_wall_seconds`) while its driver container still runs, or cannot be read. The queue
never closes such a run: the last conversations of a run can keep its counts flat for many minutes
while the driver keeps calling the model. Check:

```sh
docker logs --tail 50 yield-driver-<run-id>
proof run watch $B --plain
```

A driver that logs model calls is busy; leave it. A driver that logs nothing for long, with
`model_down` or `stack_down` in `watch`, waits on a dead dependency: bring the model or the stack
back (`proof models up`, `proof stack up`). A run whose driver is gone is posted again up to twice,
then closed as `stalled`; see the next section to run it again.

### A run failed on its budget

The run outlived `budget.max_wall_seconds`. The queue posts it again up to `--retries` times, and
the relaunched driver resumes from the store without calling the target for closed units. If the
queue ended with the run failed, start a queue for that run alone, without rendering again:

```sh
proof run queue $B --only <run-id> --retries 2
```

The resume replays the recorded grades (yield driver 0.1.11 or later, on both channels) and finishes
quickly.
A run that cannot fit its budget even so needs a new run id with a larger `max_wall_seconds`:
add it to `matrix.toml`, then `proof spec render` and queue it.

### A run has no Lens result

```sh
proof lens status <client>
proof lens run <client> <run-id>
```

If Lens never received the delivery, check the spec's `webhook_url`
(`http://lens-api:8080/webhooks/yield` unless the matrix sets another) and that `lens-api` is
healthy. If Lens admitted it and `lens run` reports the relaunch refused (404 or 409), read
`proof stack logs lens-api`. A dead `lens-run` container is recorded under
`$PROOF_HOME/bundles/<client>/lens/kills/` before it is removed; its log tail says why it died.
A run that failed in Yield has no manifest, so Lens has nothing to measure: re-measure or rerun it
first.

### `proof evidence export` is refused by the secret scan

The refusal names the file and the secret, never the value, and nothing is written. Find why the
value is in that file: a target that echoes its API key into answers, a secret pasted into a
bundle file, or a placeholder secret whose value is an ordinary word that also appears in a trace.
Fix the source, or set the secret to a value that cannot occur in text (at least 8 random
characters), then export again. Do not edit the trace to get past the scan.

### Hugging Face answers 403 on a gated model

The token cannot read `meta-llama/Llama-Guard-4-12B`. Either the license is not accepted (or not
yet granted) on the token's account, or the token is fine-grained without "Read access to contents
of all public gated repos you can access". Fix it on huggingface.co, set the token again with
`proof secrets set HF_TOKEN`, then `proof models pull --only guardian`.

### The ghcr.io pull is denied

`proof stack pin` or `proof stack up` reports `denied` or `unauthorized` for a
`ghcr.io/ethicompass/...` image: docker holds no login, or one without read access to the
Ethicompass packages. Run `docker login ghcr.io` again with a token that has `read:packages` and
an account the organization grants package access to (authorize the token for the organization's
SSO when it enforces one). Check with `docker pull` of one image of the lock.

### Docker Hub hiccups

The vLLM, MinIO, Valkey, registry and Python images come from Docker Hub. A pull that fails with a
timeout, a TLS error, or `toomanyrequests` is usually transient or an anonymous rate limit. Run
the same `proof models up` or `proof stack up` again (both are safe to repeat), or `docker pull`
the image by hand first. `docker login` with a Docker Hub account raises the rate limit.
