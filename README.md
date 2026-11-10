# LigaWant

Want list para Magic: The Gathering. Você monta a lista das cartas que faltam, vê o preço de cada uma na **LigaMagic (R$)**, no **TCGplayer (US$)** e no **Cardmarket (€)** lado a lado, descobre quais estão na coleção pública de alguém na Liga e gera uma imagem da lista para mandar no grupo.

Servidor em Python puro (biblioteca padrão + Pillow), SQLite, sem framework e sem build de front-end.

---

## Sumário

- [Funcionalidades](#funcionalidades)
- [Como rodar](#como-rodar)
- [Arquitetura](#arquitetura)
- [Fontes de dados](#fontes-de-dados)
- [Banco de dados](#banco-de-dados)
- [Rotas](#rotas)
- [Segurança](#segurança)
- [Design system](#design-system)
- [Limites e parâmetros](#limites-e-parâmetros)

---

## Funcionalidades

| Tela | O que faz |
|---|---|
| **Landing** (`/`) | Apresentação e chamada para criar conta. |
| **Minhas listas** (`/lists`) | Várias want lists por usuário (ex.: uma por deck), com total por site. |
| **Editor** (`/lists/<id>`) | Busca por nome em português ou inglês, escolha da edição exata (galeria de impressões), quantidade de 1 a 99, salvamento automático e preço por carta e da lista nos três sites. |
| **Buscar em coleção** (`/collection`) | Cola o link de uma coleção pública da LigaMagic e cruza com as suas wants: o que está lá, se é **a mesma edição** ou outra, e por quanto. |
| **Exportar JPG** (`/lists/<id>/want.jpg`) | Imagem da lista (grade de cartas, quantidade como pilha, preços por site) pronta para WhatsApp. |
| **Perfil** (`/profile`) | Trocar usuário e senha, escolher quais sites de preço aparecem e excluir a conta. |

Contas sem acesso há cerca de 6 meses são apagadas, com as listas, pela limpeza automática.

## Como rodar

Requisitos: Python 3.10+ (desenvolvido em 3.12).

```bash
git clone https://github.com/EmersonJunior12/ligawant.git
cd ligawant
python -m venv .venv
# Windows: .venv\Scripts\activate    Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

O servidor sobe em `http://127.0.0.1:8001/`. Se a porta estiver ocupada, ele usa a próxima livre. O banco `data/app.db` é criado na primeira execução e fica fora do git.

Ao subir, e depois a cada 12 h, o app limpa sessões vencidas, tentativas de login antigas, o cache velho da Liga e as contas inativas.

## Arquitetura

MVC sem framework: um roteador próprio sobre `http.server.ThreadingHTTPServer`.

```
app.py                     ponto de entrada: cria o schema, faz a limpeza periódica e sobe o servidor
controllers/
  router.py                handler HTTP, tabela de rotas (@route), sessão, CSRF, headers de segurança, estáticos
  auth_controller.py       landing, login, cadastro e logout
  list_controller.py       listas: CRUD, resumos e exportação do JPG
  collection_controller.py cruzamento coleção da Liga × wants
  api_controller.py        busca no Scryfall, detalhes e impressões, preços da Liga, sites ativos
  profile_controller.py    usuário, senha e exclusão de conta
  liga_errors.py           tradução das pausas e recusas da Liga em HTTP, e a rota "Tentar agora"
models/
  db.py                    conexão SQLite (transação por contexto), schema e migrações
  user.py / session.py     contas (scrypt), bloqueio por tentativas, sessões server-side
  want_list.py             listas e cartas; toda consulta filtra pelo dono
  scryfall.py              cliente da API do Scryfall (busca, impressões, preços USD/EUR, imagens)
  ligamagic.py             preço da Liga por impressão (raspagem, cache, ritmo e disjuntor)
  liga_collection.py       leitura de coleção pública da Liga e o cruzamento com as wants
views/
  renderer.py              monta o HTML a partir dos templates (__PLACEHOLDER__, sempre com escape)
  image_renderer.py        compõe o JPG da want list com Pillow
  templates/               páginas e partials (topbar, barra de sites, diálogo de exportação, ícones SVG)
static/
  css/app.css              design system inteiro (tokens + componentes)
  js/                      um script por tela, sem bundler; ui.js tem os utilitários comuns
  fonts/ img/              fontes servidas pelo próprio app (licença OFL) e favicon
```

Algumas decisões do código:

- **Sem dependências no servidor web.** A única dependência é o Pillow, usado só para o JPG.
- **As rotas JSON exigem sessão e CSRF por padrão.** Para abrir uma rota é preciso marcar `auth=False` de forma explícita.
- **O HTML é renderizado no servidor**, e o JavaScript de cada tela cuida só da interação (autosave, busca, preços sob demanda).
- **Templates com placeholders `__NOME__` substituídos numa passada só.** Assim, texto de usuário que contenha `__ALGO__` não é reinterpretado.

## Fontes de dados

### Scryfall (API oficial)

Usado para a busca de nomes (PT/EN), as impressões de cada carta, as imagens e os preços do TCGplayer (USD) e do Cardmarket (EUR). O cliente segue as regras da API: User-Agent e Accept explícitos, no máximo cerca de 10 requisições por segundo e cache de 10 min em memória.

### LigaMagic (raspagem da página pública)

A Liga não tem API pública. O app baixa a página da carta (`?view=cards/card&card=<nome>`) e lê três variáveis JavaScript que já vêm na página, em vez de interpretar o layout:

- `cards_editions`: o resumo Menor/Médio/Maior de cada edição e variante (normal, foil, etched…);
- `cards_stock`: as ofertas, das quais só se lê a qualidade (M/NM/SP…), para mostrar a melhor disponível;
- `dataQuality`: os nomes das qualidades.

O preço mostrado é o **menor da edição**, em qualquer qualidade e idioma. O preço de cada oferta vem ofuscado como sprite de dígitos, uma proteção deliberada da Liga, e não é decodificado.

**Regras de convivência com o site** (`models/ligamagic.py`):

- User-Agent honesto. Nada de se passar por navegador, trocar de IP ou resolver desafio.
- No máximo **2 páginas abertas ao mesmo tempo, no app inteiro**, e pelo menos **1 s entre o início de cada uma**. As cartas de uma lista são buscadas em paralelo dentro desse limite.
- Cache no SQLite: **12 h** para carta encontrada e **1 h** para não encontrada. Só o resumo é guardado, nunca o HTML.
- **Disjuntor:** um HTTP 429 ou 503, ou uma página de desafio, pausa todas as chamadas. A pausa dura até o `Retry-After` ou por um backoff de 30 s que dobra até 15 min, e sobrevive a reinício. O usuário pode liberar antes com "Tentar agora". HTTP 403 é tratado como recusa e vira aviso.

A **coleção pública** (`?view=colecao/colecao`) vem numa tabela HTML de 80 linhas por página. O `robots.txt` da Liga desaconselha robôs nesse caminho, então a leitura é conservadora:

- só acontece quando o usuário clica;
- lê uma página a cada 5 s, até 15 páginas (1.200 linhas);
- guarda o resultado 1 h em cache.

> Se a Liga mudar o nome ou o formato dessas variáveis, os preços da Liga param de chegar. O app avisa com uma mensagem de erro de formato em vez de mostrar dado errado.

## Banco de dados

SQLite em `data/app.db`, no modo WAL e com chaves estrangeiras ligadas.

| Tabela | Conteúdo |
|---|---|
| `users` | usuário (único, sem diferenciar maiúsculas), sal e hash scrypt, sites de preço ativos, último acesso |
| `sessions` | **hash** do token de sessão, token CSRF, validade (12 h) |
| `login_attempts` | tentativas por usuário e por IP, para o bloqueio temporário |
| `want_lists` | listas por usuário (nome único por dono) |
| `want_list_cards` | cartas da lista: impressão do Scryfall, nomes EN/PT, edição, número, quantidade e posição |
| `liga_cache` | resumo de preços da Liga por nome de carta (JSON) |
| `liga_collection_cache` | páginas de coleção já reduzidas às linhas (JSON) |
| `liga_state` | estado do disjuntor da Liga (pausa e backoff) |

Apagar um usuário apaga em cascata as sessões, as listas e as cartas dele.

## Rotas

**Páginas**

- `GET /`, `GET/POST /login`, `GET/POST /register`, `POST /logout`
- `GET /lists`, `GET /lists/new`, `GET /lists/<id>`, `POST /lists/<id>/delete`, `GET /lists/<id>/want.jpg`
- `GET /collection`
- `GET /profile`, `POST /profile/username`, `POST /profile/password`, `POST /profile/delete`

**API JSON** (exige sessão; os POSTs exigem o header `X-CSRF-Token`)

| Rota | Uso |
|---|---|
| `GET /api/cards/search?q=` | autocomplete de nomes (Scryfall) |
| `GET /api/cards/prints?name=` | impressões de uma carta |
| `POST /api/cards/details` | imagens e preços USD/EUR de até 200 impressões |
| `POST /api/cards/liga` | preços da Liga (até 600 itens, 10 nomes distintos por chamada) |
| `POST /api/liga/resume` | "Tentar agora": libera a pausa da Liga |
| `GET /api/lists/summaries` | totais das listas |
| `POST /api/lists`, `POST /api/lists/<id>` | criar e salvar lista (autosave) |
| `GET /api/collection/page` | uma página da coleção pública da Liga |
| `POST /api/collection/compare` | cruzamento coleção × wants |
| `POST /api/settings/markets` | sites de preço ativos |

## Segurança

- **Senhas:** scrypt (N=2¹⁴, r=8, p=1) com sal por usuário, no mínimo 8 caracteres.
- **Força bruta:** depois de 5 falhas por usuário ou 20 por IP em 15 min, o login fica bloqueado temporariamente.
- **Sessão:** server-side, com cookie `HttpOnly; SameSite=Strict` e validade de 12 h. O banco guarda só o SHA-256 do token.
- **CSRF:** token por sessão nos formulários e no header `X-CSRF-Token`. Antes do login, os formulários usam um token separado guardado em cookie.
- **Headers de segurança:**
  - CSP restrita (`script-src 'self'`, `style-src 'self'`, imagens só do próprio app e de `cards.scryfall.io`, `frame-ancestors 'none'`);
  - `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy: same-origin` e `Cross-Origin-Opener-Policy: same-origin`.
- **Entrada:**
  - corpo das requisições limitado a 256 KB;
  - todo texto passa por escape na view;
  - toda consulta a lista filtra pelo dono;
  - ids do Scryfall validados por formato.
- **Escopo:** o app escuta só em `127.0.0.1`. Publicar exige um proxy reverso com HTTPS na frente.

## Design system

A identidade é a de uma **mesa de jogo à noite**: fundo grafite azulado, destaque **verde-limão** e uma cor por site de preço. Os componentes seguem a estrutura do Material Design 3 (papéis de cor, state layers, ripple, elevação), com a forma e a paleta próprias do LigaWant. Tudo está em [`static/css/app.css`](static/css/app.css), e o JPG exportado (`views/image_renderer.py`) usa o mesmo sistema.

### Cor

Os tokens usam nomes de papel do MD3 e valores da marca.

| Token | Valor | Papel |
|---|---|---|
| `--surface` | `#101219` | fundo, a "Mesa" |
| `--surface-container` | `#191c26` | papéis e cards, o "Feltro" |
| `--surface-high` / `--surface-highest` | `#222634` / `#2b3042` | camadas elevadas |
| `--on-surface` / `--on-surface-variant` | `#eceef3` / `#a7adbd` | texto principal e secundário |
| `--outline` / `--outline-variant` | `#4a5063` / `#333849` | bordas e divisores |
| `--primary` | `#c8f031` | ação principal e "mesma edição", a "Lima" |
| `--error` | `#ff7a6b` | erro e destrutivo |

**Cores por site:** cada preço aparece sempre com a cor do site **e** o símbolo da moeda. A cor nunca é a única pista, por acessibilidade.

| Site | Token | Cor |
|---|---|---|
| LigaMagic (R$) | `--market-liga` | `#c8f031` Lima |
| TCGplayer (US$) | `--market-tcgplayer` | `#b3a6ff` Lilás |
| Cardmarket (€) | `--market-cardmarket` | `#7ccbff` Céu |

### Tipografia

- **Bricolage Grotesque** (`--font-display`) para títulos e marca.
- **Inter** (`--font`) para texto e preços, com algarismos tabulares para os valores ficarem alinhados nas colunas.
- As duas são **servidas pelo próprio app** (`static/fonts/`, recortes latin e latin-ext em woff2), porque a CSP não aceita CDN. A licença é a SIL OFL, em `static/fonts/OFL.txt`.

### Forma e elevação

| Token | Raio | Uso |
|---|---|---|
| `--radius-xs` | 6px | tags |
| `--radius-sm` | 8px | chips, menus |
| `--radius-md` | 12px | botões, campos |
| `--radius-lg` | 20px | papéis |
| `--radius-xl` | 28px | diálogos |
| `--card-radius` | 4,8% | carta de Magic: o canto real, proporcional à largura |

Há quatro níveis de elevação MD3 para tema escuro (`--elev-1` a `--elev-5`) e uma curva *emphasized* (`--ease: cubic-bezier(.2, 0, 0, 1)`).

### Componentes e padrões

- Botões (`btn-primary`, `btn-ghost`, `btn-text`, `icon-btn`) com state layer de 8% no hover e ripple no clique.
- Chips de site (`market-chip`), que ligam e desligam cada site na barra de sites.
- **A carta é a protagonista:** miniatura com o canto real, sombra de elevação e **pilha** atrás (`--stack-1`, `--stack-2`) indicando a quantidade.
- Galeria de impressões para escolher a edição, com zoom; carta ampliada em tela cheia ao clicar (sempre a imagem em inglês, com as duas faces quando houver); skeleton com shimmer enquanto os preços carregam.
- Toasts para confirmação e erro; aviso de pausa da Liga com o botão "Tentar agora".
- Ícones em sprite SVG inline (`views/templates/partials/icons.svg`).

### Acessibilidade e responsividade

- `:focus-visible` com contorno de 2px na cor primária em todos os elementos interativos.
- As animações só rodam com `prefers-reduced-motion: no-preference`.
- Nenhuma informação depende só da cor (moeda e texto sempre acompanham).
- No toque (`hover: none`), os controles que só apareceriam no hover ficam visíveis.
- Layout com quebra em 720px.
- Barra de rolagem fina no tom do tema, com espaço reservado, para a página não pular quando um diálogo trava a rolagem.

### JPG exportado

`views/image_renderer.py` compõe a imagem com Pillow, no mesmo sistema visual do site:

- 1.080 px de largura, encolhendo até 640 px em listas de 1 ou 2 cartas;
- cartas com o canto arredondado e a pilha de cópias;
- mini-chips de preço por site e os totais no cabeçalho.

O usuário escolhe no diálogo de exportação quais sites entram na imagem.

## Limites e parâmetros

| Parâmetro | Valor | Onde |
|---|---|---|
| Cartas por lista | 200 | `models/want_list.py` |
| Nome da lista | 80 caracteres | `models/want_list.py` |
| Quantidade por carta | 1–99 | schema |
| Sessão | 12 h | `models/session.py` |
| Inatividade até apagar a conta | ~6 meses (183 dias) | `models/user.py` |
| Liga: concorrência e intervalo | 2 simultâneas, 1 s entre inícios | `models/ligamagic.py` |
| Liga: cache | 12 h (não encontrada: 1 h) | `models/ligamagic.py` |
| Coleção: páginas e intervalo | 15 páginas, 5 s, cache de 1 h | `models/liga_collection.py` |
| Scryfall | ~10 req/s, cache de 10 min | `models/scryfall.py` |

---

LigaWant não tem vínculo com a LigaMagic, o TCGplayer, o Cardmarket, o Scryfall nem a Wizards of the Coast. Magic: The Gathering é marca da Wizards of the Coast.
