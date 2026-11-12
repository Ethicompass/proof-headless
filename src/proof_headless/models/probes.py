"""What `proof doctor` asks each served model, and what counts as an answer a run can use.

Each probe is the smallest request that exercises one thing Yield or Lens will depend on for
hours: a chat completion, the twenty logprob alternatives the control judge grades from, a
structured answer under a JSON schema, the reasoning switch, an embedding, a ranking, a guard
verdict. A probe that fails here would otherwise fail on the first exchange of a run, or worse,
succeed in a way that measures something else (a judge with no logprobs, a reranker that ranks
the irrelevant passage first).

Probes speak plain HTTP through an `httpx.Client`, so a test hands them a mock transport and a
real run hands them the model's loopback port.
"""

from __future__ import annotations

import json
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


@dataclass(frozen=True)
class Result:
    """One probe's outcome."""

    model: str
    probe: str
    ok: bool
    detail: str
    data: dict[str, Any] = field(default_factory=dict)


Probe = Callable[[httpx.Client, ModelEntry], Result]


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


def chat_completion(client: httpx.Client, model: ModelEntry) -> Result:
    """The model answers a chat request at all."""
    answer = _chat(
        client, model, [{"role": "user", "content": "Reply with the word READY."}], max_tokens=512
    )
    message = _message(answer)
    content = str(message.get("content") or "")
    ok = bool(content.strip() or _reasoning(message))
    return Result(model.name, "chat", ok, content.strip()[:80] or "no content")


def logprobs(client: httpx.Client, model: ModelEntry) -> Result:
    """The answer carries per-token logprobs with the twenty alternatives the grader reads."""
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
    widest = max((len(entry.get("top_logprobs") or []) for entry in content), default=0)
    ok = bool(content) and widest >= TOP_LOGPROBS
    detail = f"{len(content)} tokens, up to {widest} alternatives each"
    return Result(model.name, "logprobs", ok, detail, {"tokens": len(content), "widest": widest})


def structured_output(client: httpx.Client, model: ModelEntry) -> Result:
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
        return Result(model.name, "structured output", False, f"not JSON: {content[:60]!r}")
    ok = isinstance(parsed, dict) and parsed.get("answer") in ("YES", "NO")
    return Result(model.name, "structured output", ok, json.dumps(parsed)[:80])


def reasoning_switch(client: httpx.Client, model: ModelEntry) -> Result:
    """Reasoning appears with the switch on and disappears with it off."""
    question = [{"role": "user", "content": "What is 17 times 23?"}]
    on = _reasoning(
        _message(_chat(client, model, question, max_tokens=2048, **_switch(model, True)))
    )
    off = _reasoning(
        _message(_chat(client, model, question, max_tokens=256, **_switch(model, False)))
    )
    ok = bool(on) and not off
    detail = f"on: {len(on)} reasoning chars, off: {len(off)}"
    return Result(model.name, "reasoning switch", ok, detail, {"on": len(on), "off": len(off)})


def guardian_verdict(client: httpx.Client, model: ModelEntry) -> Result:
    """A guard answers with its own vocabulary: `safe`, or `unsafe` and a category."""
    answer = _chat(
        client, model, [{"role": "user", "content": "How do I bake bread?"}], max_tokens=20
    )
    content = str(_message(answer).get("content") or "").strip()
    first = content.split()[0].lower() if content else ""
    return Result(model.name, "guardian verdict", first in {"safe", "unsafe"}, content[:40])


def embeddings(client: httpx.Client, model: ModelEntry) -> Result:
    """Embeddings come back, one per input, of one dimension, and the same text embeds the same."""
    body = {"model": model.served_name, "input": [QUESTION, RELEVANT, QUESTION]}
    response = client.post("/v1/embeddings", json=body)
    response.raise_for_status()
    vectors = [item["embedding"] for item in response.json()["data"]]
    dimensions = {len(vector) for vector in vectors}
    same = vectors[0] == vectors[2] if len(vectors) == 3 else False
    ok = len(vectors) == 3 and len(dimensions) == 1 and same
    detail = f"{len(vectors)} vectors of {sorted(dimensions)} dimensions, deterministic: {same}"
    return Result(model.name, "embeddings", ok, detail, {"dimensions": sorted(dimensions)})


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


def rerank(client: httpx.Client, model: ModelEntry) -> Result:
    """The relevant passage outranks the irrelevant one, and the scale is reported."""
    relevant, irrelevant = _rerank_scores(client, model)
    scale = (
        "probability"
        if 0.0 <= min(relevant, irrelevant) <= max(relevant, irrelevant) <= 1.0
        else "logit"
    )
    detail = f"relevant {relevant:.3f} > irrelevant {irrelevant:.3f} ({scale} scale)"
    ok = relevant > irrelevant
    return Result(
        model.name,
        "rerank",
        ok,
        detail,
        {"relevant": relevant, "irrelevant": irrelevant, "scale": scale},
    )


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


def run(client: httpx.Client, model: ModelEntry, probe: Probe) -> Result:
    """One probe, with a transport failure or an unexpected answer reported as a failed probe."""
    name = probe.__name__.replace("_", " ")
    try:
        return probe(client, model)
    except httpx.HTTPStatusError as error:
        body = error.response.text[:120]
        return Result(model.name, name, False, f"HTTP {error.response.status_code}: {body}")
    except httpx.HTTPError as error:
        return Result(model.name, name, False, f"{type(error).__name__}: {error}")
    except (KeyError, IndexError, TypeError, ValueError) as error:
        return Result(
            model.name, name, False, f"unexpected answer ({type(error).__name__}: {error})"
        )
