"""Cache and concurrency shortcuts must preserve complete answers and mutation boundaries."""
import asyncio
from contextlib import asynccontextmanager
import threading
from types import SimpleNamespace

import httpx
import pytest

from test_views import env as env
from app.deps import book_lifecycle


def test_answers_are_frontier_and_reset_scoped(env, monkeypatch):
    api, bid, app, _ = env
    client = app.state.client
    # Keep the real offline route's deterministic validation. Jev is unused for stub generation.
    client.jev = SimpleNamespace(model="jev-1.13.0", close=lambda: None)
    calls = []
    original = client.complete
    def count(*args, **kwargs):
        calls.append(1)
        fields = getattr(kwargs.get("schema"), "model_fields", {})
        if "insufficient_evidence" in fields:
            fact = "Berenice met Aldric at the forge and they spoke."
            return {"insufficient_evidence": fact not in args[1],
                    "claims": ([{"text": fact, "citation_ids": [1]}]
                               if fact in args[1] else [])}, {"in": 20, "out": 12}
        if "references_future" in fields:
            return {"references_future": False, "unsupported_claims": []}, {"in": 12, "out": 3}
        return original(*args, **kwargs)
    monkeypatch.setattr(client, "complete", count)
    question = {"question": "Where did Berenice meet Aldric?", "bookmark": 999}
    first = api.post(f"/api/books/{bid}/ask", json=question)
    assert first.status_code == 200, first.text
    assert first.json()["claims"] and not first.json()["insufficient_evidence"]
    paid_calls = len(calls)
    contextual = api.post(f"/api/books/{bid}/ask", json={**question, "history": [
        {"role": "user", "content": "Tell me about Berenice", "as_of_chapter": 2},
    ]})
    assert contextual.status_code == 200, contextual.text
    assert not contextual.json().get("cached") and len(calls) > paid_calls
    paid_calls = len(calls)
    earlier = api.post(f"/api/books/{bid}/ask", json={**question, "bookmark": 1}).json()
    assert earlier["as_of_chapter"] == 1 and "Berenice" not in str(earlier.get("claims"))
    assert len(calls) > paid_calls
    api.post(f"/api/books/{bid}/position/reset", json={"position_epoch": 0})
    reset = api.post(f"/api/books/{bid}/ask", json=question).json()
    assert reset["as_of_chapter"] == 0 and reset["claims"] == []


@pytest.mark.parametrize("assist_path", ["/ask", "/chapter-closeout", "/selection-action", "/people", "/ingest/retry"])
def test_slow_recap_and_answers_share_reads_but_reset_waits_and_cancel_releases(assist_path):
    async def scenario():
        app = SimpleNamespace(state=SimpleNamespace(book_request_locks={}))
        gate = asynccontextmanager(book_lifecycle)
        def request(method, path):
            return SimpleNamespace(app=app, method=method, url=SimpleNamespace(path=path))
        # A slow catch-up summary: a shared read, as the removed background preparation was.
        recap = request("GET", "/api/books/book/catch-me-up")
        warm_entered, release_warm = asyncio.Event(), asyncio.Event()
        answer_entered, release_answer = asyncio.Event(), asyncio.Event()
        reset_entered = asyncio.Event()
        async def warm():
            async with gate("book", recap):
                warm_entered.set()
                await release_warm.wait()
        async def ask():
            async with gate("book", request("POST", "/api/books/book" + assist_path)):
                answer_entered.set()
                await release_answer.wait()
        async def reset():
            async with gate("book", request("POST", "/api/books/book/position/reset")):
                reset_entered.set()
        warmer = asyncio.create_task(warm())
        await warm_entered.wait()
        answer = asyncio.create_task(ask())
        # A whole slow recap is deliberately still in flight; the answer must already be admitted.
        await asyncio.wait_for(answer_entered.wait(), timeout=1)
        mutation = asyncio.create_task(reset())
        await asyncio.sleep(0)
        assert not reset_entered.is_set()
        answer.cancel()
        try:
            await answer
        except asyncio.CancelledError:
            pass
        assert not reset_entered.is_set()  # The recap still protects its snapshot.
        release_warm.set()
        await asyncio.gather(warmer, mutation)
        assert reset_entered.is_set() and not app.state.book_request_locks
    asyncio.run(scenario())


@pytest.mark.parametrize(("method", "suffix"), [
    ("POST", "/position/reset"), ("DELETE", ""), ("POST", "/memory-corrections"),
])
@pytest.mark.parametrize(("reader_method", "reader_suffix"), [
    ("GET", "/catch-me-up"), ("POST", "/people"),
])
def test_recap_and_progress_share_lifecycle_but_mutations_wait_and_cancellation_cleans_up(
    method, suffix, reader_method, reader_suffix,
):
    async def scenario():
        app = SimpleNamespace(state=SimpleNamespace(book_request_locks={}))
        gate = asynccontextmanager(book_lifecycle)

        def request(method, suffix):
            return SimpleNamespace(app=app, method=method,
                                   url=SimpleNamespace(path=f"/api/books/book{suffix}"))

        recap_entered, position_entered = asyncio.Event(), asyncio.Event()
        release_recap, release_position = asyncio.Event(), asyncio.Event()
        second_position_entered, mutation_entered = asyncio.Event(), asyncio.Event()

        async def hold(method, suffix, entered, release):
            async with gate("book", request(method, suffix)):
                entered.set()
                await release.wait()

        async def mutate():
            async with gate("book", request(method, suffix)):
                mutation_entered.set()

        tasks = []
        try:
            recap = asyncio.create_task(hold(reader_method, reader_suffix, recap_entered, release_recap))
            tasks.append(recap)
            await asyncio.wait_for(recap_entered.wait(), timeout=1)
            position = asyncio.create_task(hold("PUT", "/position", position_entered, release_position))
            tasks.append(position)
            await asyncio.wait_for(position_entered.wait(), timeout=1)
            second_position = asyncio.create_task(hold(
                "PUT", "/position", second_position_entered, release_position,
            ))
            tasks.append(second_position)
            await asyncio.sleep(0)
            entry = app.state.book_request_locks["book"]
            assert entry.readers == entry.refcount == 3
            assert not second_position_entered.is_set()  # Progress transitions serialize independently.

            for endpoint in ("/position", "/ingest", "/graph", "/manifest"):
                async with asyncio.timeout(1):
                    async with gate("book", request("GET", endpoint)):
                        assert not release_recap.is_set() and not release_position.is_set()

            second_position.cancel()  # Cancellation while waiting for the progress lock drops its lease.
            await asyncio.gather(second_position, return_exceptions=True)
            assert entry.readers == entry.refcount == 2
            mutation = asyncio.create_task(mutate())
            tasks.append(mutation)
            await asyncio.sleep(0)
            assert entry.lock.locked() and not mutation_entered.is_set()

            # A queued mutation keeps writer preference. Cancel a reader queued behind it without
            # leaking the registry entry or allowing the mutation to cross an active recap.
            queued_reader = asyncio.create_task(hold(
                "GET", "/ingest", asyncio.Event(), asyncio.Event(),
            ))
            tasks.append(queued_reader)
            await asyncio.sleep(0)
            queued_reader.cancel()
            await asyncio.gather(queued_reader, return_exceptions=True)
            assert entry.refcount == 3  # recap + position + mutation
            release_position.set()
            await position
            assert not mutation_entered.is_set()
            recap.cancel()  # Cancellation of an admitted reader also releases the exclusive waiter.
            await asyncio.gather(recap, return_exceptions=True)
            await asyncio.wait_for(mutation, timeout=1)
            assert mutation_entered.is_set() and not app.state.book_request_locks
        finally:
            release_recap.set()
            release_position.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("mutation", ["reset", "delete"])
@pytest.mark.parametrize("experience", ["recap", "reflection", "selection"])
def test_slow_recap_does_not_block_reader_status_or_progress_but_lifecycle_mutation_waits(
    env, monkeypatch, mutation, experience,
):
    api, bid, app, atoms = env
    client = app.state.reader_client
    original_complete = client.complete
    entered, release = threading.Event(), threading.Event()

    def slow_recap(system, user, tier="cheap", schema=None, **kwargs):
        if ((experience == "selection" and "citation_ids" in getattr(schema, "model_fields", {}))
                or (tier == "large" and (schema is None if experience == "recap"
                                        else getattr(schema, "__name__", "") == "AskDraft"))):
            entered.set()
            assert release.wait(5), "test did not release the simulated recap provider"
        return original_complete(system, user, tier=tier, schema=schema, **kwargs)

    monkeypatch.setattr(client, "complete", slow_recap)
    app.state.recaps.invalidate_book(bid)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://testserver") as requests:
            recap = asyncio.create_task(
                requests.get(f"/api/books/{bid}/catch-me-up") if experience == "recap" else
                requests.post(f"/api/books/{bid}/chapter-closeout", json={"chapter": 2})
                if experience == "reflection" else requests.post(
                    f"/api/books/{bid}/selection-action",
                    json={"action": "define", "text": "magistrate", "atom": 3, "cfi": "cfi"}))
            mutation_request = None
            try:
                assert await asyncio.to_thread(entered.wait, 2)
                position, ingest, graph = await asyncio.wait_for(asyncio.gather(*[
                    requests.get(f"/api/books/{bid}/{endpoint}")
                    for endpoint in ("position", "ingest", "graph")
                ]), timeout=1)
                assert all(response.status_code == 200 for response in (position, ingest, graph))
                assert position.json()["bookmark"] == ingest.json()["ingest_progress"] == 2
                assert graph.json()["as_of_chapter"] == 2

                progress = await asyncio.wait_for(requests.put(f"/api/books/{bid}/position", json={
                    "cfi": "responsive-progress", "offset": sum(a["char_len"] for a in atoms[:2]) + 2,
                    "position_epoch": 0,
                }), timeout=1)
                assert progress.status_code == 200 and progress.json()["bookmark"] == 2
                assert app.state.catalog.get_state(bid)["cfi"] == "responsive-progress"
                assert not release.is_set() and not recap.done()

                mutation_request = asyncio.create_task(
                    requests.post(f"/api/books/{bid}/position/reset", json={"position_epoch": 0})
                    if mutation == "reset" else requests.delete(f"/api/books/{bid}")
                )
                # Observe acquisition of the exclusive gate, rather than infer ordering from timing.
                async with asyncio.timeout(1):
                    while not app.state.book_request_locks[bid].lock.locked():
                        await asyncio.sleep(0)
                assert not mutation_request.done()
                assert app.state.catalog.get_state(bid)["bookmark"] == 2
                release.set()
                recap_response, mutation_response = await asyncio.wait_for(
                    asyncio.gather(recap, mutation_request), timeout=3,
                )
                assert recap_response.status_code == 200, recap_response.text
                assert recap_response.json()["as_of_chapter"] == 2
                assert mutation_response.status_code == (200 if mutation == "reset" else 204)
                if mutation == "reset":
                    assert app.state.catalog.get_state(bid)["position_epoch"] == 1
                    assert app.state.catalog.get_state(bid)["bookmark"] == 0
                else:
                    assert app.state.catalog.get_book(bid) is None
                assert not app.state.book_request_locks
            finally:
                release.set()
                await asyncio.gather(recap, *([mutation_request] if mutation_request else []),
                                     return_exceptions=True)

    # Reuse the app's lifespan loop: lifecycle entries must never cross event loops.
    api.portal.call(scenario)
