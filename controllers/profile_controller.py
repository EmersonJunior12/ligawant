"""Controller: perfil (trocar usuário e senha). As duas trocas exigem a senha atual."""

from typing import Optional

from controllers.router import SESSION_COOKIE, route
from models import session as session_model
from models import user as user_model
from views.renderer import render_profile


def _check_current_password(req, password: str) -> tuple[Optional[str], int]:
    """Confere a senha atual via authenticate (herda o bloqueio por tentativas). Retorna (erro, status)."""
    result = user_model.authenticate(req.session.username, password, req.client_ip)
    if result.locked:
        return "Muitas tentativas erradas. Aguarde 15 minutos e tente de novo.", 429
    if result.user_id != req.session.user_id:
        return "Senha atual incorreta.", 401
    return None, 200


@route("GET", "/profile")
def show(req):
    req.send_html(render_profile(req.session, notice=req.query.get("ok", "")))


@route("POST", "/profile/username")
def update_username(req):
    form = req.form()
    username = form.get("username", "")

    error, status = _check_current_password(req, form.get("current_password", ""))
    if error is None:
        ok, error = user_model.change_username(req.session.user_id, username)
        status = 400
        if ok:
            req.redirect("/profile?ok=usuario")
            return
    req.send_html(render_profile(req.session, username_error=error, username_value=username), status=status)


@route("POST", "/profile/password")
def update_password(req):
    form = req.form()
    new_password = form.get("new_password", "")

    if new_password != form.get("confirm_password", ""):
        req.send_html(render_profile(req.session, password_error="As senhas novas não coincidem."), status=400)
        return
    error, status = _check_current_password(req, form.get("current_password", ""))
    if error is None:
        ok, error = user_model.change_password(req.session.user_id, new_password)
        status = 400
        if ok:
            # Senha nova encerra as sessões de outros navegadores e troca o token desta.
            session_model.delete_user_sessions(req.session.user_id)
            token = session_model.create_session(req.session.user_id)
            req.redirect("/profile?ok=senha", set_cookies=[req.session_cookie(token)])
            return
    req.send_html(render_profile(req.session, password_error=error), status=status)


@route("POST", "/profile/delete")
def delete_account(req):
    error, status = _check_current_password(req, req.form().get("current_password", ""))
    if error is not None:
        req.send_html(render_profile(req.session, delete_error=error), status=status)
        return
    user_model.delete_user(req.session.user_id)
    req.redirect("/login?conta=excluida", set_cookies=[req.clear_cookie(SESSION_COOKIE)])
