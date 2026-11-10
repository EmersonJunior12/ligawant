"""View: monta o HTML a partir dos dados do model. Tudo que vem de usuário ou API passa por escape."""

import json
import re
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Optional

from models.session import Session
from models.want_list import ListSummary, WantList

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
_PLACEHOLDER_RE = re.compile(r"__([A-Z_]+)__")


def _load(name: str) -> str:
    return (TEMPLATES_DIR / name).read_text(encoding="utf-8")


def _fill(template: str, values: dict[str, str]) -> str:
    # Substituição em uma passada só: texto de usuário contendo "__ALGO__" não é reinterpretado.
    return _PLACEHOLDER_RE.sub(lambda m: values.get(m.group(1), m.group(0)), template)


def icon(name: str) -> str:
    """Ícone do Material UI pelo sprite (partials/icons.svg), herdando a cor do texto."""
    return f'<svg class="icon" aria-hidden="true"><use href="#i-{name}"/></svg>'


def _page(title: str, content: str, session: Optional[Session] = None, scripts: tuple[str, ...] = (),
          main_class: str = "container") -> str:
    """`main_class`: "container" centraliza e limita a largura; a página inicial usa faixas de largura total."""
    topbar = ""
    if session:
        topbar = _fill(_load("partials/topbar.html"), {
            "USERNAME": escape(session.username),
            "CSRF": escape(session.csrf_token),
        })
    return _fill(_load("base.html"), {
        "TITLE": escape(title),
        "CSRF_META": escape(session.csrf_token) if session else "",
        "ICONS": _load("partials/icons.svg"),
        "TOPBAR": topbar,
        "MAIN_CLASS": main_class,
        "CONTENT": content,
        # ui.js (ripple, diálogo de confirmação) vai em toda página, antes dos scripts da tela.
        "SCRIPT": "".join(f'<script src="/static/js/{name}" defer></script>' for name in ("ui.js", *scripts)),
    })


def _fmt_datetime(iso_value: str) -> str:
    return datetime.fromisoformat(iso_value).astimezone().strftime("%d/%m/%Y %H:%M")


def _error_html(error: str) -> str:
    return f'<p class="alert alert-error" role="alert">{escape(error)}</p>' if error else ""


def render_error(status: int, message: str, session: Optional[Session] = None) -> str:
    content = f"""
    <section class="empty-state">
      <p class="error-code">{status}</p>
      <p>{escape(message)}</p>
      <a class="btn btn-primary" href="/">Voltar ao início</a>
    </section>"""
    return _page(f"{status} · LigaWant", content, session)


def _notice_html(text: str) -> str:
    return f'<p class="alert alert-success" role="status">{escape(text)}</p>' if text else ""


# Avisos que chegam pela URL: só chaves conhecidas, nunca o texto da query.
_LOGIN_NOTICES = {"excluida": "Conta excluída, com todas as listas."}


def render_login(csrf: str, error: str = "", username: str = "", notice: str = "") -> str:
    content = _fill(_load("login.html"), {
        "CSRF": escape(csrf), "ERROR": _error_html(error), "USERNAME_VALUE": escape(username),
        "NOTICE": _notice_html(_LOGIN_NOTICES.get(notice, "")),
    })
    return _page("Entrar · LigaWant", content)


def render_landing() -> str:
    """Página inicial pública (quem não está logado): apresentação do LigaWant, sem dados de usuário."""
    return _page("LigaWant · sua want ligada às coleções", _load("landing.html"), main_class="landing")


def render_register(csrf: str, error: str = "", username: str = "") -> str:
    content = _fill(_load("register.html"), {
        "CSRF": escape(csrf), "ERROR": _error_html(error), "USERNAME_VALUE": escape(username),
    })
    return _page("Criar conta · LigaWant", content)


_PROFILE_NOTICES = {
    "usuario": "Usuário alterado.",
    "senha": "Senha alterada. As sessões em outros navegadores foram encerradas.",
}


def render_profile(session: Session, notice: str = "", username_error: str = "",
                   password_error: str = "", delete_error: str = "",
                   username_value: Optional[str] = None) -> str:
    content = _fill(_load("profile.html"), {
        "NOTICE": _notice_html(_PROFILE_NOTICES.get(notice, "")),
        "USERNAME_ERROR": _error_html(username_error),
        "PASSWORD_ERROR": _error_html(password_error),
        "DELETE_ERROR": _error_html(delete_error),
        "USERNAME_VALUE": escape(session.username if username_value is None else username_value),
        "CSRF": escape(session.csrf_token),
    })
    return _page("Perfil · LigaWant", content, session)


_MARKET_LABELS = (("liga", "LigaMagic", "R$"), ("tcgplayer", "TCGplayer", "US$"), ("cardmarket", "Cardmarket", "€"))


def _market_bar(session: Session) -> str:
    """Barra "Preços de": um botão liga/desliga por site, estado inicial da conta."""
    chips = "".join(
        f'<button type="button" class="market-chip" data-market="{key}" '
        f'aria-pressed="{"true" if key in session.markets else "false"}">'
        f'{icon("check")}<span>{label}</span><small>{symbol}</small></button>'
        for key, label, symbol in _MARKET_LABELS
    )
    return _fill(_load("partials/market_bar.html"), {"CHIPS": chips})


def _export_dialog(session: Session) -> str:
    """Diálogo "Gerar imagem"; os sites marcados de início são os da barra "Preços de" da conta."""
    return _fill(_load("partials/export_dialog.html"), {"MARKETS": escape(",".join(session.markets))})


def _list_card(summary: ListSummary, csrf: str) -> str:
    plural = "carta" if summary.total_quantity == 1 else "cartas"
    distinct = "distinta" if summary.card_count == 1 else "distintas"
    confirm = f'Excluir a lista "{summary.name}"? Isso não pode ser desfeito.'
    return f"""
      <article class="list-card">
        <a class="list-card-main" href="/lists/{summary.id}">
          <h2>{escape(summary.name)}</h2>
          <p class="list-summary" data-list-id="{summary.id}">
            <span class="list-count">{summary.total_quantity} {plural} ({summary.card_count} {distinct})</span><span class="list-prices" data-list-prices></span>
          </p>
          <p class="small">Atualizada em {_fmt_datetime(summary.updated_at)}</p>
        </a>
        <div class="list-card-actions">
          <button type="button" class="icon-btn" data-export-url="/lists/{summary.id}/want.jpg"
                  aria-label="Gerar imagem de {escape(summary.name)}" title="Gerar imagem">{icon("download")}</button>
          <form method="post" action="/lists/{summary.id}/delete" data-confirm="{escape(confirm)}" data-confirm-action="Excluir lista">
            <input type="hidden" name="csrf_token" value="{escape(csrf)}">
            <button type="submit" class="icon-btn is-danger" aria-label="Excluir {escape(summary.name)}" title="Excluir">{icon("delete")}</button>
          </form>
        </div>
      </article>"""


def render_lists(session: Session, summaries: list[ListSummary]) -> str:
    if summaries:
        body = '<div class="list-grid">' + "".join(_list_card(s, session.csrf_token) for s in summaries) + "</div>"
    else:
        body = f"""
        <div class="empty-state">
          <p>Você ainda não tem nenhuma want list.</p>
          <a class="btn btn-primary" href="/lists/new">{icon("add")}Criar a primeira</a>
        </div>"""
    content = _fill(_load("lists.html"), {"LISTS": body, "MARKET_BAR": _market_bar(session)})
    content += _export_dialog(session)
    return _page("Minhas listas · LigaWant", content, session,
                 ("export-dialog.js", "market-bar.js", "list-summaries.js"))


def render_collection(session: Session) -> str:
    """Página estática: o ?collection= da URL é lido pelo JS, que preenche o campo e dispara a busca."""
    content = _load("collection.html")
    return _page("Buscar em coleção · LigaWant", content, session, ("card-preview.js", "collection-match.js"))


def _json_script(data) -> str:
    # "<" vira < para o JSON nunca fechar a tag <script> em que está embutido.
    return json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")


def render_editor(session: Session, want_list: Optional[WantList]) -> str:
    data = {
        "id": want_list.id if want_list else None,
        "name": want_list.name if want_list else "",
        "markets": list(session.markets),
        "cards": [
            {
                "scryfall_id": c.scryfall_id, "quantity": c.quantity, "name_en": c.name_en,
                "name_pt": c.name_pt, "set_code": c.set_code, "set_name": c.set_name,
                "collector_number": c.collector_number, "image": None, "prices": None,
            }
            for c in (want_list.cards if want_list else [])
        ],
    }
    json_data = _json_script(data)
    content = _fill(_load("list_editor.html"), {
        "HEADING": "Editar lista" if want_list else "Nova lista",
        "LIST_DATA": json_data,
        "MARKET_BAR": _market_bar(session),
    }) + _export_dialog(session)
    title = f"{want_list.name} · LigaWant" if want_list else "Nova lista · LigaWant"
    return _page(title, content, session, ("market-bar.js", "card-preview.js", "list-editor.js", "export-dialog.js"))
