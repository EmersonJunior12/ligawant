#!/usr/bin/env python3
"""Entry point: sobe o LigaWant (MTG Want List) em localhost (padrão MVC)."""

import socketserver
import threading
from http.server import ThreadingHTTPServer

# Importar os controllers registra as rotas no router.
from controllers import (  # noqa: F401
    api_controller, auth_controller, collection_controller, list_controller, profile_controller,
)
from controllers.router import AppRequestHandler
from models import db, liga_collection, ligamagic, session, user

HOST = "127.0.0.1"
PORT = 8001
CLEANUP_INTERVAL_S = 12 * 60 * 60


def find_free_port(start_port: int) -> int:
    port = start_port
    while True:
        try:
            with socketserver.TCPServer((HOST, port), None, bind_and_activate=False) as test:
                test.server_bind()
                return port
        except OSError:
            port += 1


def cleanup() -> None:
    """Sessões vencidas, tentativas de login antigas, cache velho da Liga e contas sem acesso há ~6 meses."""
    try:
        session.purge_expired()
        user.purge_old_attempts()
        ligamagic.purge_cache()
        liga_collection.purge_cache()
        removed = user.purge_inactive_users()
        if removed:
            print(f"[limpeza] {removed} {'conta inativa excluída' if removed == 1 else 'contas inativas excluídas'}")
    except Exception as exc:  # noqa: BLE001 - limpeza falhar não pode derrubar o servidor
        print(f"[limpeza] erro: {exc!r}")


def cleanup_loop(stop: threading.Event) -> None:
    while not stop.wait(CLEANUP_INTERVAL_S):
        cleanup()


def main():
    db.init_db()
    cleanup()
    # Só roda com o app ligado: uma conta vencida sai na próxima subida ou no próximo ciclo.
    threading.Thread(target=cleanup_loop, args=(threading.Event(),), daemon=True).start()
    port = find_free_port(PORT)
    url = f"http://{HOST}:{port}/"

    with ThreadingHTTPServer((HOST, port), AppRequestHandler) as httpd:
        print(f"Servindo em {url} (MVC: models/ views/ controllers/)")
        print("Abra esse endereço no navegador. Ctrl+C para parar.")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nEncerrando servidor.")


if __name__ == "__main__":
    main()
