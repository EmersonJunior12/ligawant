"""Tradução das falhas da LigaMagic para HTTP, igual em todos os controllers que falam com a Liga."""

from controllers.router import HttpError, route
from models import ligamagic


def liga_http_error(exc: ligamagic.LigaError) -> HttpError:
    """Pausa pedida pela Liga -> 503 + Retry-After; recusa (403) -> 503 sem Retry-After (nos dois o
    navegador para o lote atual); resto -> 502."""
    if isinstance(exc, ligamagic.LigaBlocked):
        until = exc.retry_at.astimezone().strftime("%H:%M:%S")
        return HttpError(
            503,
            f"A LigaMagic pediu uma pausa nas consultas até {until}.",
            [("Retry-After", str(exc.retry_after_seconds))],
        )
    if isinstance(exc, ligamagic.LigaForbidden):
        return HttpError(
            503,
            "A LigaMagic recusou o acesso deste computador (HTTP 403). As consultas param aqui; "
            "tente de novo mais tarde.",
        )
    return HttpError(502, "LigaMagic indisponível agora.")


@route("POST", "/api/liga/resume", json_api=True)
def resume(req):
    """Botão "Tentar agora": libera a pausa antes do prazo (decisão do usuário)."""
    req.send_json({"resumed": ligamagic.resume_now()})
