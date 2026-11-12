# Rehearsal bundle (fictitious)

A functional rehearsal of the whole appliance: models, stack, bundle load, spec render, the run
queue with two runs in flight, the Lens result and the evidence export. Its target is the
appliance's own chat model (`chat` in `manifests/dgx-spark/models.yaml`, served as
`nemotron-3.5-lightning`), reached through Yield's `openai_chat` connector at
`http://chat:8000/v1/chat/completions`, playing "ACME Home Appliances support", an assistant that
does not exist. No client's data is in it.

**Its numbers are not for reporting.** One model attacks, answers and judges: the generator, the
target and both judges are the same `chat` model, so every score measures that model against
itself. A rehearsal passes when every step completes, not on any value it reports.

What is in it:

- `catalogues/rehearsal`: the five strategies of Yield's `universal` catalogue whose phrasing
  carries no `{premise}` (`ask-identity`, `ask-scope`, `ask-system-prompt`, `act-for-another`,
  `refuse-escalation`), copied verbatim with the four plugins they belong to. They need no
  knowledge base, so the bundle has no brain. A run names a catalogue its bundle loaded, so the
  copy is published under its own name and the seeded `universal` is left alone.
- `matrix.toml`: `r1-disclosure` (12 target calls) and `r2-injection` (18), one replica each,
  measured by `ai_disclosure`, `context`, `toxicity` and `bias`. Yield keeps the control
  (`ask-scope`) in every run it narrows.
- `roles.toml`: judge, metric judge, generator and attacker on `chat`, the embedder on `embed`,
  the reranker on `rerank`, the guardian on `guardian`.
- `target/system-prompt.txt` and its Lens declaration under `declarations/`, named by the canonical
  digest. `openai_chat` sends the model name and the conversation and nothing else, so the served
  model never receives this prompt: the declaration says what the assistant should have been told,
  which is all Lens reads it for.
- One secret, `REHEARSAL_TARGET_KEY`. Yield's gate refuses a connector without a secret reference;
  vLLM is served without an API key and ignores it, so any value of 8 characters or more works
  (shorter values are not scanned by the evidence export, which says so).

On the appliance, from a checkout of this repository:

```sh
cp manifests/dgx-spark/models.yaml ~/.proof/models.yaml
proof models up
proof doctor
printf 'not-a-real-key' | proof secrets set REHEARSAL_TARGET_KEY
proof stack up --bundle examples/bundle-rehearsal
proof doctor --no-models --stack
proof bundle validate examples/bundle-rehearsal
proof bundle load examples/bundle-rehearsal
proof spec render examples/bundle-rehearsal
proof run queue examples/bundle-rehearsal --concurrency 2
proof run watch examples/bundle-rehearsal --plain     # from a second shell, while the queue runs
proof lens status rehearsal
proof lens run rehearsal                              # only if a run still has no result
proof evidence export rehearsal
proof evidence verify <package-dir>
proof stack forget-bundle rehearsal                   # when done, so its secret leaves Yield
```
