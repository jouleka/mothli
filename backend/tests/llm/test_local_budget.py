from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from app.cost import CostCeilingExceeded
from app.llm.local_budget import SessionBudget


def test_subclass_keeps_configured_output_cap_on_the_actual_wire(tmp_path):
    import json
    import httpx
    from pydantic import BaseModel
    from app.llm.local_budget import CappedLocalClient
    from app.catalog.catalog import Catalog
    from app.config import Settings
    from app.cost import budgeted_completion

    class Result(BaseModel):
        text: str

    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "test",
                "object": "chat.completion",
                "created": 0,
                "model": "gpt-6.1-terra",
                "service_tier": "default",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": '{"text":"OK."}'},
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
            },
        )

    client = CappedLocalClient(
        session_budget=SessionBudget(),
        env={},
        provider="openai-compatible",
        api_key="synthetic",
        embed_provider="stub",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    cat = Catalog(str(tmp_path / "catalog.db"))
    cat.add_book("b", title="Cap control")
    settings = Settings(_env_file=None, cost_max_output_tokens_per_call=128)
    try:
        budgeted_completion(
            cat,
            settings,
            client,
            "b",
            system="Write.",
            user="Ready.",
            tier="cheap",
            schema=Result,
            phase="test",
        )
        assert requests[0]["max_completion_tokens"] == 128
    finally:
        cat.close()
        client._oai.close()


def test_parallel_reservations_block_before_billable_work():
    budget = SessionBudget(0.05)
    barrier = Barrier(2)
    calls = []

    def run():
        barrier.wait()

        def paid():
            calls.append(1)
            raise RuntimeError("ambiguous provider failure")

        try:
            budget.run(0.04, paid, "gpt-6.1-terra")
        except (CostCeilingExceeded, RuntimeError):
            pass

    with ThreadPoolExecutor(2) as pool:
        list(pool.map(lambda _: run(), range(2)))
    assert len(calls) == 1 and budget.charged == 0.04


def test_success_releases_unused_reservation_but_failure_does_not():
    budget = SessionBudget(0.05)
    assert budget.run(0.04, lambda: ("ok", {"in": 1000, "out": 100}), "gpt-6.1-terra")[0] == "ok"
    assert budget.charged == pytest.approx(0.0015)

    def fail():
        raise RuntimeError("timeout")

    with pytest.raises(RuntimeError):
        budget.run(0.04, fail, "gpt-6.1-terra")
    with pytest.raises(CostCeilingExceeded):
        budget.run(0.01, lambda: pytest.fail("must not call provider"), "gpt-6.1-terra")
