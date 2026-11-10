"""Controller: login, cadastro e logout."""

from controllers.router import PRE_CSRF_COOKIE, SESSION_COOKIE, route
from models import session as session_model
from models import user as user_model
from views.renderer import render_landing, render_login, render_register


@route("GET", "/", auth=False)
def home(req):
    if req.session:
        req.redirect("/lists")
        return
    req.send_html(render_landing())


@route("GET", "/login", auth=False)
def login_form(req):
    if req.session:
        req.redirect("/lists")
        return
    csrf, set_cookie = req.pre_session_csrf()
    req.send_html(render_login(csrf, notice=req.query.get("conta", "")),
                  set_cookies=[set_cookie] if set_cookie else None)


@route("POST", "/login", auth=False)
def login_submit(req):
    req.require_pre_session_csrf()
    form = req.form()
    username = form.get("username", "")
    csrf = req.cookie(PRE_CSRF_COOKIE)

    result = user_model.authenticate(username, form.get("password", ""), req.client_ip)
    if result.locked:
        req.send_html(render_login(
            csrf, "Muitas tentativas erradas. Aguarde 15 minutos e tente de novo.", username), status=429)
        return
    if result.user_id is None:
        req.send_html(render_login(csrf, "Usuário ou senha inválidos.", username), status=401)
        return
    _start_session(req, result.user_id)


@route("GET", "/register", auth=False)
def register_form(req):
    if req.session:
        req.redirect("/lists")
        return
    csrf, set_cookie = req.pre_session_csrf()
    req.send_html(render_register(csrf), set_cookies=[set_cookie] if set_cookie else None)


@route("POST", "/register", auth=False)
def register_submit(req):
    req.require_pre_session_csrf()
    form = req.form()
    username = form.get("username", "")
    password = form.get("password", "")
    csrf = req.cookie(PRE_CSRF_COOKIE)

    if password != form.get("confirm_password", ""):
        req.send_html(render_register(csrf, "As senhas não coincidem.", username), status=400)
        return
    ok, message = user_model.create_user(username, password)
    if not ok:
        req.send_html(render_register(csrf, message, username), status=400)
        return

    result = user_model.authenticate(username, password, req.client_ip)
    _start_session(req, result.user_id)


@route("POST", "/logout")
def logout(req):
    token = req.cookie(SESSION_COOKIE)
    if token:
        session_model.delete_session(token)
    req.redirect("/login", set_cookies=[req.clear_cookie(SESSION_COOKIE)])


def _start_session(req, user_id: int) -> None:
    # Sessão nova a cada login (evita session fixation) e derruba a anterior, se houver.
    old_token = req.cookie(SESSION_COOKIE)
    if old_token:
        session_model.delete_session(old_token)
    token = session_model.create_session(user_id)
    req.redirect("/lists", set_cookies=[req.session_cookie(token), req.clear_cookie(PRE_CSRF_COOKIE)])
