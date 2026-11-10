"""Controller: handler HTTP, tabela de rotas, sessão/CSRF e headers de segurança."""

import json
import mimetypes
import re
import secrets
from dataclasses import dataclass
from http import cookies
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import parse_qs, urlparse

from models import session as session_model
from models.session import Session
from views.renderer import render_error

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
SESSION_COOKIE = "session_id"
PRE_CSRF_COOKIE = "pre_csrf"
MAX_BODY_BYTES = 256 * 1024
STATIC_TYPES = {
    ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8",
    ".woff2": "font/woff2", ".ttf": "font/ttf", ".txt": "text/plain; charset=utf-8",
    ".svg": "image/svg+xml",
}

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' https://cards.scryfall.io; script-src 'self'; "
        "style-src 'self'; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "Cross-Origin-Opener-Policy": "same-origin",
}


class HttpError(Exception):
    def __init__(self, status: int, message: str, headers: Optional[list[tuple[str, str]]] = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.headers = headers or []  # ex.: Retry-After num 503


@dataclass(frozen=True)
class Route:
    method: str
    pattern: re.Pattern
    action: Callable
    auth: bool  # exige sessão
    json_api: bool = False  # erros saem em JSON em vez de redirect/HTML


ROUTES: list[Route] = []


def route(method: str, path: str, auth: bool = True, json_api: bool = False):
    """Registra uma action. Grupos nomeados do path viram kwargs (ex.: /lists/(?P<list_id>\\d+))."""
    def decorator(action: Callable) -> Callable:
        ROUTES.append(Route(method, re.compile(f"^{path}$"), action, auth, json_api))
        return action
    return decorator


class AppRequestHandler(BaseHTTPRequestHandler):
    server_version = "MtgWantList"
    sys_version = ""

    def log_message(self, format, *args):
        print(f"{self.address_string()} - {format % args}")

    # ---------- dados da requisição ----------

    @property
    def client_ip(self) -> str:
        return self.client_address[0]

    def cookie(self, name: str) -> Optional[str]:
        jar = cookies.SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie", ""))
        except cookies.CookieError:
            return None
        return jar[name].value if name in jar else None

    @property
    def session(self) -> Optional[Session]:
        if not hasattr(self, "_session"):
            self._session = session_model.get_session(self.cookie(SESSION_COOKIE) or "")
        return self._session

    @property
    def query(self) -> dict[str, str]:
        return {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}

    def _read_body(self) -> bytes:
        if not hasattr(self, "_body"):
            try:
                length = int(self.headers.get("Content-Length", 0))
            except ValueError:
                raise HttpError(400, "Content-Length inválido") from None
            if length > MAX_BODY_BYTES:
                raise HttpError(413, "Requisição grande demais")
            self._body = self.rfile.read(length) if length > 0 else b""
        return self._body

    def form(self) -> dict[str, str]:
        parsed = parse_qs(self._read_body().decode("utf-8", errors="replace"))
        return {k: v[0] for k, v in parsed.items()}

    def json_body(self) -> dict:
        try:
            data = json.loads(self._read_body() or b"{}")
        except json.JSONDecodeError:
            raise HttpError(400, "JSON inválido") from None
        if not isinstance(data, dict):
            raise HttpError(400, "JSON inválido")
        return data

    # ---------- CSRF ----------

    def _check_origin(self) -> None:
        origin = self.headers.get("Origin")
        if origin and origin != "null":
            if urlparse(origin).netloc != self.headers.get("Host", ""):
                raise HttpError(403, "Origem não permitida")

    def require_csrf(self) -> None:
        """POST autenticado: token da sessão no form (csrf_token) ou no header X-CSRF-Token."""
        self._check_origin()
        sent = self.headers.get("X-CSRF-Token") or self.form().get("csrf_token", "")
        if not self.session or not secrets.compare_digest(sent, self.session.csrf_token):
            raise HttpError(403, "Token CSRF inválido. Recarregue a página e tente de novo.")

    def pre_session_csrf(self) -> tuple[str, Optional[str]]:
        """Token do login/registro (double-submit cookie). Retorna (token, Set-Cookie ou None)."""
        token = self.cookie(PRE_CSRF_COOKIE)
        if token and len(token) >= 32:
            return token, None
        token = secrets.token_urlsafe(32)
        return token, f"{PRE_CSRF_COOKIE}={token}; HttpOnly; Path=/; SameSite=Strict"

    def require_pre_session_csrf(self) -> None:
        self._check_origin()
        cookie_token = self.cookie(PRE_CSRF_COOKIE) or ""
        sent = self.form().get("csrf_token", "")
        if not cookie_token or not secrets.compare_digest(sent, cookie_token):
            raise HttpError(403, "Formulário expirado. Recarregue a página e tente de novo.")

    # ---------- respostas ----------

    def end_headers(self):
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        super().end_headers()

    def send_body(self, body: bytes, content_type: str, status: int = 200,
                  extra_headers: Optional[list[tuple[str, str]]] = None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in extra_headers or []:
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_html(self, html: str, status: int = 200, set_cookies: Optional[list[str]] = None):
        headers = [("Cache-Control", "no-store")] + [("Set-Cookie", c) for c in set_cookies or []]
        self.send_body(html.encode("utf-8"), "text/html; charset=utf-8", status, headers)

    def send_json(self, data, status: int = 200, extra_headers: Optional[list[tuple[str, str]]] = None):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_body(body, "application/json; charset=utf-8", status,
                       [("Cache-Control", "no-store"), *(extra_headers or [])])

    def redirect(self, location: str, set_cookies: Optional[list[str]] = None):
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        for c in set_cookies or []:
            self.send_header("Set-Cookie", c)
        self.end_headers()

    @staticmethod
    def session_cookie(token: str) -> str:
        # Sem "Secure" porque o app roda em http://127.0.0.1. Atrás de HTTPS, adicionar "; Secure".
        return f"{SESSION_COOKIE}={token}; HttpOnly; Path=/; SameSite=Strict; Max-Age=43200"

    @staticmethod
    def clear_cookie(name: str) -> str:
        return f"{name}=; HttpOnly; Path=/; SameSite=Strict; Max-Age=0"

    # ---------- dispatch ----------

    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method: str):
        path = urlparse(self.path).path

        if method == "GET" and path.startswith("/static/"):
            self._serve_static(path)
            return

        matched_path = False
        for r in ROUTES:
            m = r.pattern.match(path)
            if not m:
                continue
            matched_path = True
            if r.method != method:
                continue
            try:
                if r.auth and not self.session:
                    if r.json_api:
                        raise HttpError(401, "Sessão expirada. Faça login de novo.")
                    self.redirect("/login")
                    return
                if method == "POST" and r.auth:
                    self.require_csrf()
                r.action(self, **m.groupdict())
            except HttpError as exc:
                self._send_error_page(exc.status, exc.message, r.json_api, exc.headers)
            except Exception as exc:  # noqa: BLE001 - nunca vazar stack trace pro cliente
                print(f"[erro] {method} {path}: {exc!r}")
                self._send_error_page(500, "Erro interno.", r.json_api)
            return

        if matched_path:
            self._send_error_page(405, "Método não permitido", path.startswith("/api/"))
        else:
            self._send_error_page(404, "Página não encontrada", path.startswith("/api/"))

    def _send_error_page(self, status: int, message: str, as_json: bool,
                         headers: Optional[list[tuple[str, str]]] = None):
        if as_json:
            self.send_json({"error": message}, status, headers)
        else:
            self.send_html(render_error(status, message, self.session), status)

    # ---------- static ----------

    def _serve_static(self, path: str):
        relative = path[len("/static/"):]
        static_root = STATIC_DIR.resolve()
        requested = (static_root / relative).resolve()

        if static_root not in requested.parents:
            self._send_error_page(403, "Acesso negado", False)
            return
        if not requested.is_file():
            self._send_error_page(404, "Arquivo não encontrado", False)
            return

        # O registro do Windows às vezes mapeia .js/.css errado; com nosniff isso quebraria o script.
        content_type = STATIC_TYPES.get(requested.suffix) or mimetypes.guess_type(str(requested))[0]
        self.send_body(requested.read_bytes(), content_type or "application/octet-stream",
                       extra_headers=[("Cache-Control", "no-cache")])
