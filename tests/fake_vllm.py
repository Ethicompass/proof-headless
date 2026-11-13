"""A vLLM server as an `httpx.MockTransport`: answers shaped the way vLLM shapes them."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any

import httpx

from proof_headless.models import probes


@dataclass
class FakeVllm:
    """One served model. Flip a flag to break the behaviour a probe checks."""

    widest: int = 20
    structured: str = '{"answer": "YES"}'
    reasoning_obeys_switch: bool = True
    guard: str | None = None
    """What the guard answers to everything; `None` answers like Llama Guard."""
    always_safe: bool = False
    reasoning_eats_budget: bool = False
    same_text_cosine: float = 1.0
    """The cosine between the first and the last vector, which embed the same text."""
    relevant_first: bool = True
    status: int = 200
    seen: list[dict[str, Any]] = field(default_factory=list)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.status != 200:
            return httpx.Response(self.status, text="model not loaded")
        body = json.loads(request.content or b"{}")
        self.seen.append({"path": request.url.path, **body})
        path = request.url.path
        if path == "/v1/embeddings":
            vectors = [[float(len(text)), 1.0, 0.0] for text in body["input"]]
            first = vectors[0]
            norm = math.hypot(*first)
            tilt = math.sqrt(1.0 - self.same_text_cosine**2) * norm
            vectors[-1] = [first[0] * self.same_text_cosine, self.same_text_cosine, tilt]
            return httpx.Response(200, json={"data": [{"embedding": v} for v in vectors]})
        if path == "/v1/rerank":
            high, low = (-1.8, -9.2) if self.relevant_first else (-9.2, -1.8)
            results = [{"index": 0, "relevance_score": high}, {"index": 1, "relevance_score": low}]
            return httpx.Response(200, json={"results": results})
        if path == "/classify":
            passage = body["messages"][1]["content"]
            score = 0.9 if (passage == probes.RELEVANT) == self.relevant_first else 0.1
            return httpx.Response(200, json={"data": [{"probs": [score]}]})
        return httpx.Response(200, json=self._chat(body))

    def _chat(self, body: dict[str, Any]) -> dict[str, Any]:
        thinking = (body.get("chat_template_kwargs") or {}).get("enable_thinking", True)
        message: dict[str, Any] = {"role": "assistant", "content": "READY"}
        if "response_format" in body:
            message["content"] = self.structured
        if "guard" in body["model"]:
            harmful = body["messages"][-1]["content"] == probes.HARMFUL
            default = "unsafe\nS1" if harmful and not self.always_safe else "safe"
            message["content"] = self.guard if self.guard is not None else default
        if thinking or not self.reasoning_obeys_switch:
            message["reasoning"] = "Let me think. 17 times 23 is 391."
        if self.reasoning_eats_budget and "READY" in str(body["messages"][-1]["content"]):
            message["content"] = None
            message["reasoning"] = "Let me think about how to say READY..."
        choice: dict[str, Any] = {"index": 0, "message": message, "finish_reason": "stop"}
        if body.get("logprobs"):
            alternatives = [{"token": f"t{i}", "logprob": -i} for i in range(self.widest)]
            choice["logprobs"] = {
                "content": [{"token": "YES", "logprob": -0.1, "top_logprobs": alternatives}]
            }
        return {"id": "x", "object": "chat.completion", "model": body["model"], "choices": [choice]}
