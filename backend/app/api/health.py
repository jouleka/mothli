"""Process liveness and provider-aware readiness for the local production wrapper."""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.deps import get_client
from app.llm.client import ProviderUnavailable

router = APIRouter(prefix="/api/health", tags=["health"])


@router.get("/live")
def live():
    return {"status": "ok"}


@router.get("/ready")
def ready(client=Depends(get_client)):
    provider = client.provider_status()
    degraded = any(item["status"] == "degraded" for item in provider.values())
    payload = {"status": "degraded" if degraded else "ready", "provider": provider}
    return JSONResponse(payload, status_code=503 if degraded else 200)


@router.get("/ai")
def ai_runtime(client=Depends(get_client)):
    """Describe local AI truthfully without probing a provider or exposing keys/endpoints.

    Readiness means that the process can serve requests. A deliberately enabled test stub can be
    ready too, so the UI needs this separate distinction before presenting it as working AI.
    """
    health = client.provider_status()
    payload = {
        "mode": "demo" if client.provider == "stub" else "live",
        "provider": client.provider,
        "models": {"extraction": client.version["cheap"], "synthesis": client.version["large"]},
        "completion_status": health["completion"]["status"],
        "embedding_mode": "demo" if client.embed_identity().startswith("stub:") else "live",
        "local_testing": client.provider == "codex-local",
        "reasoning": ({"extraction": "low", "selection": "low", "synthesis": "medium",
                       "review": "medium"} if client._model_for("cheap") == "gpt-6.1-terra"
                      and client._model_for("large") == "gpt-6.1-terra" else None),
    }
    jev = getattr(client, "jev", None)
    if jev is not None:
        payload["decisions"] = {"provider": "typesafe", "model": jev.model,
                                "status": getattr(jev, "status", "configured")}
    return payload


@router.post("/ai/check")
def check_ai(client=Depends(get_client)):
    """An explicit local connection test; never send book text or run this on page load."""
    if client.provider == "codex-local":
        try:
            text, _usage = client.complete(
                "This is a connection test. Reply with exactly OK, with no other text.",
                "Check the connection.", tier="large", max_output_tokens=256,
            )
            if text.strip() != "OK":
                client._record_provider_failure("completion", "invalid_response")
        except ProviderUnavailable as exc:
            client._record_provider_failure("completion", exc.kind)
    else:
        client.probe()
    return ai_runtime(client)
