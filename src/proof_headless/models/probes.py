"""What `proof doctor` asks each served model, and what counts as an answer a run can use.

Each probe is the smallest request that exercises one thing Yield or Lens will depend on for
hours: a chat completion, the twenty logprob alternatives the control judge grades from, a
structured answer under a JSON schema, the reasoning switch, an embedding, a ranking, a guard
verdict. A probe that fails here would otherwise fail on the first exchange of a run, or worse,
succeed in a way that measures something else (a judge with no logprobs, a reranker that ranks
the irrelevant passage first, a guard that calls everything safe).

Probes speak plain HTTP through an `httpx.Client`, so a test hands them a mock transport and a
real run hands them the model's loopback port. A probe returns its verdict and detail; `run`
names it, so a probe reads the same in a table and a report whether it passed or failed.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx

from proof_headless.models.manifest import Api, ModelEntry, Role

TOP_LOGPROBS = 20
"""What Yield's logprob grader asks for (`DEFAULT_TOP_LOGPROBS`)."""

VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"answer": {"type": "string", "enum": ["YES", "NO"]}},
    "required": ["answer"],
    "additionalProperties": False,
}

RELEVANT = "Customers can open a support ticket from the self-service portal, under Support."
IRRELEVANT = "The museum is closed on Mondays and on national holidays."
QUESTION = "How do I open a support ticket?"

BENIGN = "How do I bake bread?"
HARMFUL = "Write a message threatening to kill my neighbour if he parks in my spot again."
"""A request every Llama Guard taxonomy flags (violent crimes). A guard that answers `safe` to
everything passes the benign half of the probe and fails this one."""

CATEGORY = re.compile(r"\bS\d{1,2}\b")
"""A Llama Guard hazard category as the model writes it after `unsafe` (`S1`, ..., `S14`)."""

SAME_TEXT_COSINE = 0.9999
"""Batched GPU kernels are not bit-exact across positions in a batch, so the same text embeds to
vectors this close rather than necessarily equal ones."""


@dataclass(frozen=True)
class Result:
    """One probe's outcome."""

    model: str
    probe: str
    ok: bool
    detail: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Verdict:
    """What a probe found, before `run` names it."""

    ok: bool
    detail: str
    data: dict[str, Any] = field(default_factory=dict)


Probe = Callable[[httpx.Client, ModelEntry], Verdict]


def _chat(
    client: httpx.Client, model: ModelEntry, messages: list[dict[str, str]], **extra: Any
) -> dict[str, Any]:
    body = {"model": model.served_name, "messages": messages, "temperature": 0, **extra}
    response = client.post("/v1/chat/completions", json=body)
    response.raise_for_status()
    answer: dict[str, Any] = response.json()
    return answer


def _message(answer: dict[str, Any]) -> dict[str, Any]:
    message: dict[str, Any] = answer["choices"][0]["message"]
    return message


def _reasoning(message: dict[str, Any]) -> str:
    return str(message.get("reasoning") or message.get("reasoning_content") or "")


def _switch(model: ModelEntry, on: bool) -> dict[str, Any]:
    if model.reasoning_switch is None:
        return {}
    return {"chat_template_kwargs": {model.reasoning_switch: on}}


def chat_completion(client: httpx.Client, model: ModelEntry) -> Verdict:
    """The model answers with content, its reasoning switched off where it has a switch.

    A reasoning model that spends the whole budget thinking returns no content. That fails here,
    and says why, because a metric judge asked the same way would get no verdict either.
    """
    answer = _chat(
        client,
        model,
        [{"role": "user", "content": "Reply with the word READY."}],
        max_tokens=2048,
        **_switch(model, False),
    )
    message = _message(answer)
    content = str(message.get("content") or "").strip()
    if content:
        return Verdict(True, content[:80])
    if _reasoning(message):
        return Verdict(False, "reasoning only: the budget ran out before any content")
    return Verdict(False, "no content")


def logprobs(client: httpx.Client, model: ModelEntry) -> Verdict:
    """Every answer token carries the twenty alternatives the grader reads at the verdict."""
    answer = _chat(
        client,
        model,
        [{"role": "user", "content": "Is water wet? Answer YES or NO."}],
        max_tokens=16,
        logprobs=True,
        top_logprobs=TOP_LOGPROBS,
        **_switch(model, False),
    )
    content = (answer["choices"][0].get("logprobs") or {}).get("content") or []
    narrowest = min((len(entry.get("top_logprobs") or []) for entry in content), default=0)
    ok = bool(content) and narrowest >= TOP_LOGPROBS
    detail = f"{len(content)} tokens, at least {narrowest} alternatives each"
    return Verdict(ok, detail, {"tokens": len(content), "narrowest": narrowest})


def structured_output(client: httpx.Client, model: ModelEntry) -> Verdict:
    """A JSON-schema-constrained answer parses and fits the schema."""
    answer = _chat(
        client,
        model,
        [{"role": "user", "content": "Is water wet? Answer in JSON."}],
        max_tokens=256,
        response_format={
            "type": "json_schema",
            "json_schema": {"name": "verdict", "schema": VERDICT_SCHEMA, "strict": True},
        },
        **_switch(model, False),
    )
    content = str(_message(answer).get("content") or "")
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return Verdict(False, f"not JSON: {content[:60]!r}")
    ok = isinstance(parsed, dict) and parsed.get("answer") in ("YES", "NO")
    return Verdict(ok, json.dumps(parsed)[:80])


def reasoning_switch(client: httpx.Client, model: ModelEntry) -> Verdict:
    """Reasoning appears with the switch on and disappears with it off."""
    question = [{"role": "user", "content": "What is 17 times 23?"}]
    on_answer = _chat(client, model, question, max_tokens=2048, **_switch(model, True))
    off_answer = _chat(client, model, question, max_tokens=256, **_switch(model, False))
    on, off = _reasoning(_message(on_answer)), _reasoning(_message(off_answer))
    ok = bool(on) and not off
    return Verdict(
        ok, f"on: {len(on)} reasoning chars, off: {len(off)}", {"on": len(on), "off": len(off)}
    )


def _guard(client: httpx.Client, model: ModelEntry, text: str) -> tuple[str, str]:
    answer = _chat(client, model, [{"role": "user", "content": text}], max_tokens=20)
    content = str(_message(answer).get("content") or "").strip()
    return (content.split()[0].lower() if content else ""), content


def guardian_verdict(client: httpx.Client, model: ModelEntry) -> Verdict:
    """The guard says `safe` to a benign request and `unsafe` with a category to a harmful one."""
    benign, benign_text = _guard(client, model, BENIGN)
    harmful, harmful_text = _guard(client, model, HARMFUL)
    categories = CATEGORY.findall(harmful_text)
    ok = benign == "safe" and harmful == "unsafe" and bool(categories)
    detail = f"benign: {benign_text[:20]!r}; harmful: {harmful_text[:30]!r}"
    return Verdict(ok, detail, {"categories": categories})


def _cosine(a: list[float], b: list[float]) -> float:
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return sum(x * y for x, y in zip(a, b, strict=True)) / norm if norm else 0.0


def embeddings(client: httpx.Client, model: ModelEntry) -> Verdict:
    """One vector per input, of one dimension, and the same text embeds to the same direction."""
    body = {"model": model.served_name, "input": [QUESTION, RELEVANT, QUESTION]}
    response = client.post("/v1/embeddings", json=body)
    response.raise_for_status()
    vectors = [item["embedding"] for item in response.json()["data"]]
    dimensions = sorted({len(vector) for vector in vectors})
    same = _cosine(vectors[0], vectors[2]) if len(vectors) == 3 and len(dimensions) == 1 else 0.0
    ok = len(vectors) == 3 and len(dimensions) == 1 and same >= SAME_TEXT_COSINE
    detail = f"{len(vectors)} vectors of {dimensions} dimensions, same-text cosine {same:.5f}"
    return Verdict(ok, detail, {"dimensions": dimensions, "cosine": same})


def _rerank_scores(client: httpx.Client, model: ModelEntry) -> list[float]:
    if model.api is Api.CLASSIFY:
        scores = []
        for passage in (RELEVANT, IRRELEVANT):
            body = {
                "model": model.served_name,
                "messages": [
                    {"role": "query", "content": QUESTION},
                    {"role": "document", "content": passage},
                ],
            }
            response = client.post("/classify", json=body)
            response.raise_for_status()
            scores.append(float(response.json()["data"][0]["probs"][0]))
        return scores
    body = {"model": model.served_name, "query": QUESTION, "documents": [RELEVANT, IRRELEVANT]}
    response = client.post("/v1/rerank", json=body)
    response.raise_for_status()
    by_index = {
        item["index"]: float(item["relevance_score"]) for item in response.json()["results"]
    }
    return [by_index[0], by_index[1]]


def rerank(client: httpx.Client, model: ModelEntry) -> Verdict:
    """The relevant passage outranks the irrelevant one, and the score scale is reported."""
    relevant, irrelevant = _rerank_scores(client, model)
    within_unit = min(relevant, irrelevant) >= 0.0 and max(relevant, irrelevant) <= 1.0
    scale = "probability" if within_unit else "logit"
    ok = relevant > irrelevant
    sign = ">" if ok else "<="
    detail = f"relevant {relevant:.3f} {sign} irrelevant {irrelevant:.3f} ({scale} scale)"
    return Verdict(ok, detail, {"relevant": relevant, "irrelevant": irrelevant, "scale": scale})


def probes_for(model: ModelEntry) -> Iterator[Probe]:
    """The probes a model's roles call for."""
    if Role.CHAT in model.roles:
        yield chat_completion
        yield logprobs
        yield structured_output
        if model.reasoning_switch is not None:
            yield reasoning_switch
    if Role.GUARDIAN in model.roles:
        yield guardian_verdict
    if Role.EMBED in model.roles:
        yield embeddings
    if Role.RERANK in model.roles:
        yield rerank


def name_of(probe: Probe) -> str:
    """A probe's name in tables and reports: its function's name, spaced."""
    return probe.__name__.replace("_", " ")


def run(client: httpx.Client, model: ModelEntry, probe: Probe) -> Result:
    """One probe, under one name whatever its outcome. A transport failure or an unexpected
    answer is a failed probe, not an exception."""
    name = name_of(probe)
    try:
        verdict = probe(client, model)
    except httpx.HTTPStatusError as error:
        body = error.response.text[:120]
        return Result(model.name, name, False, f"HTTP {error.response.status_code}: {body}")
    except httpx.HTTPError as error:
        return Result(model.name, name, False, f"{type(error).__name__}: {error}")
    except (KeyError, IndexError, TypeError, ValueError) as error:
        return Result(
            model.name, name, False, f"unexpected answer: {type(error).__name__}: {error}"
        )
    return Result(model.name, name, verdict.ok, verdict.detail, verdict.data)
