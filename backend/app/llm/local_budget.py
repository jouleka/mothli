"""Opt-in local-session ceiling shared across copied reader clients and Jev requests."""

import json
import threading

from app.cost import CostCeilingExceeded, estimate_input_tokens, usd_of
from app.cost.limits import reserved_usd_of
from app.llm.client import LLMClient


class SessionBudget:
    def __init__(self, limit=0.25):
        if not 0 < limit <= 0.50:
            raise ValueError("Local session limit must be at most $0.50")
        self.limit = limit
        self.charged = 0.0
        self.lock = threading.Lock()

    def run(self, reserve, call, model):
        with self.lock:
            if not 0 <= reserve <= self.limit - self.charged:
                raise CostCeilingExceeded("local API session spend ceiling reached")
            self.charged += reserve
        # Ambiguous failures and interrupted calls retain the full reservation.
        result, usage = call()
        actual = usd_of(model, usage)
        with self.lock:
            self.charged += actual - reserve
        if actual > reserve:
            raise CostCeilingExceeded("provider exceeded local session reservation")
        return result, usage


class CappedLocalClient(LLMClient):
    def __init__(self, *, session_budget, **kwargs):
        super().__init__(**kwargs)
        if self.provider != "openai-compatible" or not self._is_native_openai:
            raise ValueError("Local cap requires native OpenAI")
        self.session_budget = session_budget

    def complete(self, system, user, tier="cheap", schema=None, max_output_tokens=None):
        model = self.billing_model(tier)
        if model != "gpt-6.1-terra":
            raise ValueError("Local experiment is priced for standard Terra only")
        reserve = reserved_usd_of(
            model,
            {
                "in": estimate_input_tokens(system, user, schema) + 2048,
                "out": max_output_tokens if max_output_tokens is not None else 4096,
            },
        )
        complete = super().complete
        return self.session_budget.run(
            reserve, lambda: complete(system, user, tier, schema, max_output_tokens), model
        )


class CappedJev:
    def __init__(self, client, budget):
        self.client, self.budget = client, budget

    def __getattr__(self, name):
        return getattr(self.client, name)

    def evaluate(self, state, questions, **kwargs):
        reserve = usd_of(
            self.model,
            {
                "in": len(
                    json.dumps({"state": state, "questions": questions}, ensure_ascii=False).encode(
                        "utf-8"
                    )
                )
                + 2048,
                "out": 0,
            },
        )
        return self.budget.run(
            reserve, lambda: self.client.evaluate(state, questions, **kwargs), self.model
        )
