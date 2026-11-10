"""View: compõe o JPG da want list (grade de cartas) com Pillow.

Mesmo sistema visual do site (LigaWant: fundo grafite, destaque verde-limão, uma cor por site;
títulos em Bricolage Grotesque, texto em Inter). A carta é a protagonista: canto real, sombra de
elevação e a pilha de cópias; os preços vão em mini-chips por site e os totais em chips no cabeçalho.
"""

import io
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from models.want_list import ListCard, WantList

WIDTH = 1080
MIN_WIDTH = 640  # listas de 1–2 cartas encolhem a imagem até aqui em vez de sobrar fundo vazio
PADDING = 56
GAP_X = 24
GAP_Y = 44
CARD_RATIO = 680 / 488  # altura/largura da imagem "normal" do Scryfall
# Canto da carta: o real é ~4,8% da largura, mas os scans antigos (Alpha, Beta) têm o arredondado
# um pouco maior e mostrariam pontos claros nos cantos; 5,8% esconde isso sem parecer cartão.
CARD_CORNER = 0.058
STACK_OFFSET = 7        # deslocamento de cada carta extra da pilha (qtd > 1)
MAX_NAME_LINES = 2
JPEG_QUALITY = 90

# Tokens do app.css (marca LigaWant).
SURFACE = (16, 18, 25)            # Mesa
SURFACE_HIGH = (34, 38, 52)
SURFACE_HIGHEST = (43, 48, 66)
STACK_1 = (47, 52, 70)
STACK_2 = (38, 42, 57)
ON_SURFACE = (236, 238, 243)
ON_SURFACE_VARIANT = (167, 173, 189)
OUTLINE_VARIANT = (51, 56, 73)
PRIMARY = (200, 240, 49)          # Lima
ON_PRIMARY = (16, 18, 25)
SHADOW = (0, 0, 0)
MARKET_COLORS = {"liga": (200, 240, 49), "tcgplayer": (179, 166, 255), "cardmarket": (124, 203, 255)}

FONTS_DIR = Path(__file__).resolve().parent.parent / "static" / "fonts"
TEXT_FONT = FONTS_DIR / "Inter-variable.ttf"
DISPLAY_FONT = FONTS_DIR / "BricolageGrotesque-variable.ttf"
FALLBACK_FONTS = {400: "C:/Windows/Fonts/segoeui.ttf", 500: "C:/Windows/Fonts/seguisb.ttf",
                  700: "C:/Windows/Fonts/segoeuib.ttf"}

MARKETS = (("liga", "R$", "LigaMagic"), ("tcgplayer", "US$", "TCGplayer"), ("cardmarket", "€", "Cardmarket"))
EMPTY_PRICE = {"value": None, "foil": False}
ALL_MARKETS = tuple(m for m, _, _ in MARKETS)
SYMBOLS = {m: s for m, s, _ in MARKETS}
NAMES = {m: n for m, _, n in MARKETS}
# Legenda do rodapé, só dos sites que aparecem na imagem.
CREDITS = {
    "liga": "R$ é o menor preço da LigaMagic (NM, SP… = melhor qualidade em estoque; † = outra edição)",
    "tcgplayer": "US$ é TCGplayer",
    "cardmarket": "€ é Cardmarket",
}


@lru_cache(maxsize=None)
def _font(size: int, weight: int = 400, display: bool = False) -> ImageFont.FreeTypeFont:
    """Inter (texto) ou Bricolage Grotesque (`display`: títulos, marca, valores grandes), variáveis, do
    próprio app. Os eixos são montados pelo nome (cada arquivo tem a sua ordem); o tamanho óptico
    acompanha o corpo. Sem os arquivos, Segoe UI do Windows."""
    path = DISPLAY_FONT if display else TEXT_FONT
    if path.is_file():
        font = ImageFont.truetype(str(path), size)
        values = []
        for axis in font.get_variation_axes():
            name = axis["name"].decode() if isinstance(axis["name"], bytes) else str(axis["name"])
            wanted = {"Weight": weight, "Optical size": size, "Width": 100}.get(name, axis["default"])
            values.append(max(axis["minimum"], min(axis["maximum"], wanted)))
        font.set_variation_by_axes(values)
        return font
    fallback = Path(FALLBACK_FONTS[min(FALLBACK_FONTS, key=lambda w: abs(w - weight))])
    if fallback.is_file():
        return ImageFont.truetype(str(fallback), size)
    return ImageFont.load_default(size=size)


def _clamp(value: float, low: int, high: int) -> int:
    return max(low, min(high, round(value)))


def _fit_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: float) -> str:
    if draw.textlength(text, font=font) <= max_width:
        return text
    while text and draw.textlength(text + "…", font=font) > max_width:
        text = text[:-1]
    return text.rstrip() + "…"


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: float) -> list[str]:
    lines, current = [], ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if draw.textlength(candidate, font=font) <= max_width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _wrap_limited(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont,
                  max_width: float, max_lines: int) -> list[str]:
    """Quebra em até `max_lines`; o que sobrar vai para a última linha, cortada com reticências."""
    lines = _wrap(draw, text, font, max_width) or [""]
    if len(lines) > max_lines:
        lines = lines[:max_lines - 1] + [" ".join(lines[max_lines - 1:])]
    return [_fit_text(draw, line, font, max_width) for line in lines]


def _money(value: float, symbol: str) -> str:
    """Formato pt-BR: US$ 1.234,56."""
    formatted = f"{value:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
    return f"{symbol} {formatted}"


def _join_pt(parts: list[str]) -> str:
    """["a"] -> "a"; ["a", "b", "c"] -> "a, b e c"."""
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " e " + parts[-1]


def _choose_cols(count: int) -> int:
    """Colunas que deixam menos buraco na última linha (3 cartas = 3 grandes, 9 = página 3×3 de fichário)."""
    if count <= 3:
        return max(count, 1)
    if count > 20:
        return 5
    candidates = (4, 3, 5) if count <= 12 else (4, 5)
    return min(candidates, key=lambda cols: -count % cols)  # empate: vale a ordem da tupla


def _totals(cards: list[ListCard], prices: dict[str, dict], market: str) -> tuple[float, int]:
    total, missing = 0.0, 0
    for card in cards:
        value = prices.get(card.scryfall_id, {}).get(market, {}).get("value")
        if value is None:
            missing += card.quantity
        else:
            total += value * card.quantity
    return total, missing


# ---------- peças ----------

def _rounded_mask(size: tuple[int, int], radius: int) -> Image.Image:
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), radius=radius, fill=255)
    return mask


@lru_cache(maxsize=8)
def _shadow(size: tuple[int, int], radius: int, blur: int, opacity: int) -> Image.Image:
    """Máscara de sombra (elevação MD3) do tamanho da carta, já desfocada e com margem para o blur."""
    pad = blur * 3
    mask = Image.new("L", (size[0] + 2 * pad, size[1] + 2 * pad), 0)
    ImageDraw.Draw(mask).rounded_rectangle((pad, pad, pad + size[0], pad + size[1]), radius=radius, fill=opacity)
    return mask.filter(ImageFilter.GaussianBlur(blur))


def _drop_shadow(canvas: Image.Image, box: tuple[int, int, int, int], radius: int) -> None:
    """Duas sombras como no Material: uma curta e marcada, outra longa e difusa."""
    x0, y0, x1, y1 = box
    size = (x1 - x0, y1 - y0)
    for offset, blur, opacity in ((3, 3, 150), (14, 16, 120)):
        mask = _shadow(size, radius, blur, opacity)
        pad = blur * 3
        canvas.paste(SHADOW, (x0 - pad, y0 - pad + offset), mask)


def _card_tile(card: ListCard, image_bytes: Optional[bytes], size: tuple[int, int]) -> Image.Image:
    if image_bytes:
        try:
            with Image.open(io.BytesIO(image_bytes)) as src:
                return ImageOps.fit(src.convert("RGB"), size, Image.Resampling.LANCZOS)
        except OSError:
            pass
    # Placeholder quando a imagem não veio do Scryfall: nome da carta centralizado.
    tile = Image.new("RGB", size, SURFACE_HIGHEST)
    draw = ImageDraw.Draw(tile)
    font = _font(_clamp(size[0] / 11, 16, 26), 500)
    lines = _wrap(draw, card.name_en.split(" // ")[0], font, size[0] - 32)[:5]
    line_h = round(font.size * 1.3)
    y = (size[1] - line_h * len(lines)) // 2
    for line in lines:
        draw.text((size[0] / 2, y), line, font=font, fill=ON_SURFACE_VARIANT, anchor="mt")
        y += line_h
    return tile


def _brand_mark(draw: ImageDraw.ImageDraw, x: int, y: int, h: int) -> int:
    """O símbolo LigaWant (o mesmo do sprite, viewBox 48): duas cartas sobrepostas, o encontro em lima.
    `h` é a altura do quadro de 48; devolve a largura ocupada."""
    k = h / 48
    stroke = max(2, round(3 * k))

    def box(bx: float, by: float, bw: float, bh: float) -> tuple[float, float, float, float]:
        return (x + bx * k, y + by * k, x + (bx + bw) * k, y + (by + bh) * k)

    draw.rectangle(box(20, 9, 8, 30), fill=PRIMARY)
    for bx, by in ((4, 5), (20, 9)):
        draw.rounded_rectangle(box(bx, by, 24, 34), radius=round(4 * k), outline=ON_SURFACE, width=stroke)
    return round(46 * k)


def _wordmark(draw: ImageDraw.ImageDraw, x: float, y: float, font: ImageFont.FreeTypeFont,
              anchor: str = "lm") -> None:
    """"Liga" no tom do texto + "Want" em lima. `anchor` "lm" (a partir de x) ou "rm" (termina em x)."""
    liga_w = draw.textlength("Liga", font=font)
    if anchor == "rm":
        x -= liga_w + draw.textlength("Want", font=font)
    draw.text((x, y), "Liga", font=font, fill=ON_SURFACE, anchor="lm")
    draw.text((x + liga_w, y), "Want", font=font, fill=PRIMARY, anchor="lm")


# ---------- chips de preço (mini, por carta) ----------

class _Chip:
    """Mini-chip de preço: símbolo em tom variante, valor em destaque e, na Liga, a tag de qualidade."""

    def __init__(self, draw: ImageDraw.ImageDraw, price: dict, symbol: str, font: ImageFont.FreeTypeFont,
                 tag_font: ImageFont.FreeTypeFont, color: tuple[int, int, int] = ON_SURFACE_VARIANT):
        self.symbol = symbol
        self.color = color  # cor do site no símbolo da moeda
        value = price.get("value")
        self.value = "—" if value is None else _money(value, "").strip()
        if value is not None and price.get("foil"):
            self.value += "*"
        if value is not None and price.get("other_edition"):
            self.value += "†"
        self.quality = price.get("quality") if value is not None else None
        self.font, self.tag_font = font, tag_font
        self.pad_x, self.pad_y = round(font.size * .55), round(font.size * .32)
        self.gap = round(font.size * .3)
        self.sym_w = draw.textlength(symbol, font=font)
        self.val_w = draw.textlength(self.value, font=font)
        self.tag_w = draw.textlength(self.quality, font=tag_font) + 2 * round(tag_font.size * .45) if self.quality else 0
        self.width = round(self.pad_x * 2 + self.sym_w + self.gap + self.val_w + (self.gap + self.tag_w if self.quality else 0))
        self.height = round(font.size + 2 * self.pad_y)

    def draw(self, draw: ImageDraw.ImageDraw, x: int, y: int) -> None:
        draw.rounded_rectangle((x, y, x + self.width, y + self.height), radius=round(self.height * .3), fill=SURFACE_HIGH)
        cy = y + self.height / 2
        tx = x + self.pad_x
        draw.text((tx, cy), self.symbol, font=self.font, fill=self.color, anchor="lm")
        tx += self.sym_w + self.gap
        draw.text((tx, cy), self.value, font=self.font, fill=ON_SURFACE if self.value != "—" else ON_SURFACE_VARIANT,
                  anchor="lm")
        if self.quality:
            tx += self.val_w + self.gap
            th = round(self.tag_font.size * 1.35)
            draw.rounded_rectangle((tx, cy - th / 2, tx + self.tag_w, cy + th / 2), radius=3, outline=PRIMARY, width=1)
            draw.text((tx + self.tag_w / 2, cy), self.quality, font=self.tag_font, fill=PRIMARY, anchor="mm")


def _layout_chips(chips: list[_Chip], max_width: int, gap: int) -> list[list[_Chip]]:
    """Chips em linhas, quebrando quando não cabem na largura da carta."""
    rows: list[list[_Chip]] = [[]]
    used = 0
    for chip in chips:
        need = chip.width if not rows[-1] else used + gap + chip.width
        if rows[-1] and need > max_width:
            rows.append([chip])
            used = chip.width
        else:
            rows[-1].append(chip)
            used = need
    return [row for row in rows if row]


# ---------- chips de total (cabeçalho) ----------

def _total_chip_size(draw, value_text, label_text, value_font, label_font) -> tuple[int, int]:
    w = max(draw.textlength(value_text, font=value_font), draw.textlength(label_text, font=label_font))
    return round(w + 2 * 22), round(label_font.size * 1.3 + value_font.size * 1.15 + 2 * 14)


def render_want_list(want_list: WantList, images: dict[str, Optional[bytes]],
                     prices: Optional[dict[str, dict]] = None,
                     markets: tuple[str, ...] = ALL_MARKETS) -> bytes:
    """Monta o JPG. Com `prices` (id -> {mercado: preço}), mostra o preço unitário por carta e o total
    dos sites em `markets`. Sem nenhum site, sai como a versão sem preços."""
    markets = tuple(m for m in ALL_MARKETS if m in markets)
    if not markets:
        prices = None
    cards = want_list.cards
    today = datetime.now().strftime("%d/%m/%Y")

    # Grade: tamanho da carta vem da grade de 1080 px (mínimo 3 colunas, para 1–2 cartas não virarem pôster).
    cols = _choose_cols(len(cards))
    size_cols = max(cols, 3)
    card_w = (WIDTH - 2 * PADDING - (size_cols - 1) * GAP_X) // size_cols
    card_h = round(card_w * CARD_RATIO)
    radius = max(6, round(card_w * CARD_CORNER))
    grid_w = cols * card_w + (cols - 1) * GAP_X
    width = WIDTH if cols >= 3 else max(MIN_WIDTH, grid_w + 2 * PADDING)
    text_w = width - 2 * PADDING
    grid_x = (width - grid_w) // 2

    name_font = _font(_clamp(card_w / 9.5, 17, 28), 600)
    meta_font = _font(_clamp(card_w / 13, 14, 20), 400)
    chip_font = _font(_clamp(card_w / 12.5, 14, 21), 500)
    tag_font = _font(_clamp(card_w / 17, 11, 15), 600)
    badge_font = _font(_clamp(card_w / 8.5, 20, 30), 800, display=True)
    name_lh = round(name_font.size * 1.25)
    meta_lh = round(meta_font.size * 1.4)
    chip_gap = 6

    f = {
        "brand": _font(28, 800, display=True), "date": _font(20, 400),
        "title": _font(60, 800, display=True), "sub": _font(24, 400),
        "total_value": _font(36, 800, display=True), "total_label": _font(18, 500),
        "footer": _font(17, 400), "footer_brand": _font(20, 800, display=True),
    }

    # ---------- medição ----------
    measure = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    title_lines = _wrap_limited(measure, want_list.name, f["title"], text_w, 2)
    total_qty = want_list.total_quantity
    sub = f"Lista de {want_list.owner}, {total_qty} {'carta' if total_qty == 1 else 'cartas'}"
    if len(cards) != total_qty:  # só vale dizer quantas distintas quando há cópias repetidas
        sub += f" ({len(cards)} {'distinta' if len(cards) == 1 else 'distintas'})"
    sub = _fit_text(measure, sub, f["sub"], text_w)

    total_chips = []
    if prices is not None:
        for market in markets:
            total, missing = _totals(cards, prices, market)
            label = NAMES[market] + (f", {missing} sem preço" if missing else "")
            value = _money(total, SYMBOLS[market])
            total_chips.append((value, label, _total_chip_size(measure, value, label, f["total_value"], f["total_label"]),
                                MARKET_COLORS[market]))
    chip_rows_total: list[list] = [[]]
    used = 0
    for item in total_chips:
        w = item[2][0]
        if chip_rows_total[-1] and used + 12 + w > text_w:
            chip_rows_total.append([])
            used = 0
        chip_rows_total[-1].append(item)
        used += (12 if used else 0) + w
    chip_rows_total = [r for r in chip_rows_total if r]
    total_chip_h = total_chips[0][2][1] if total_chips else 0

    name_lines = [_wrap_limited(measure, c.name_en.split(" // ")[0], name_font, card_w, MAX_NAME_LINES) for c in cards]
    card_chip_rows: list[list[list[_Chip]]] = []
    for card in cards:
        if prices is None:
            card_chip_rows.append([])
            continue
        card_prices = prices.get(card.scryfall_id) or {}
        chips = [_Chip(measure, card_prices.get(m, EMPTY_PRICE), SYMBOLS[m], chip_font, tag_font, MARKET_COLORS[m])
                 for m in markets]
        card_chip_rows.append(_layout_chips(chips, card_w, chip_gap))
    chip_h = round(chip_font.size + 2 * round(chip_font.size * .32))

    if prices is not None:
        legend = _join_pt([CREDITS[m] for m in markets])
        credit = f"Imagens do Scryfall. {legend[0].upper()}{legend[1:]}; * = foil. Preços de {today}."
    else:
        credit = f"Imagens do Scryfall, {today}."
    brand_w = measure.textlength("LigaWant", font=f["footer_brand"])
    credit_lines = _wrap_limited(measure, credit, f["footer"], text_w - brand_w - 40, 6)

    # ---------- alturas ----------
    top = PADDING
    brand_h = 34
    title_y = top + brand_h + 36
    title_lh = 68
    sub_y = title_y + title_lh * len(title_lines) + 18
    header_h = sub_y + 36
    if chip_rows_total:
        header_h += 28 + len(chip_rows_total) * (total_chip_h + 12) - 12
    header_h += 48

    rows = [list(range(start, min(start + cols, len(cards)))) for start in range(0, max(len(cards), 1), cols)]
    caption_hs = []
    for row in rows:
        names_h = name_lh * max((len(name_lines[i]) for i in row), default=1)
        chips_h = max((len(card_chip_rows[i]) for i in row), default=0)
        chips_h = chips_h * chip_h + max(0, chips_h - 1) * chip_gap
        caption_hs.append(14 + names_h + 4 + meta_lh + (10 + chips_h if prices is not None else 0))
    grid_h = sum(card_h + h for h in caption_hs) + GAP_Y * (len(rows) - 1)
    footer_h = 28 + 24 * len(credit_lines) + 40
    height = header_h + grid_h + 56 + footer_h

    canvas = Image.new("RGB", (width, height), SURFACE)
    draw = ImageDraw.Draw(canvas)

    # ---------- cabeçalho ----------
    mark_w = _brand_mark(draw, PADDING, top - 2, 42)
    _wordmark(draw, PADDING + mark_w + 10, top + 19, f["brand"])
    draw.text((width - PADDING, top + 19), today, font=f["date"], fill=ON_SURFACE_VARIANT, anchor="rm")
    for i, line in enumerate(title_lines):
        draw.text((PADDING, title_y + title_lh * i), line, font=f["title"], fill=ON_SURFACE)
    draw.text((PADDING, sub_y), sub, font=f["sub"], fill=ON_SURFACE_VARIANT)

    y = sub_y + 36 + 28
    for chip_row in chip_rows_total:
        x = PADDING
        for value, label, (w, h), color in chip_row:
            draw.rounded_rectangle((x, y, x + w, y + h), radius=16, fill=SURFACE_HIGH, outline=OUTLINE_VARIANT, width=1)
            draw.text((x + 22, y + 14), label, font=f["total_label"], fill=ON_SURFACE_VARIANT)
            draw.text((x + 22, y + 14 + round(f["total_label"].size * 1.3)), value, font=f["total_value"], fill=color)
            x += w + 12
        y += total_chip_h + 12

    # ---------- grade de cartas ----------
    mask = _rounded_mask((card_w, card_h), radius)
    y = header_h
    for row, caption_h in zip(rows, caption_hs):
        for col, index in enumerate(row):
            card = cards[index]
            x = grid_x + col * (card_w + GAP_X)

            # Quantidade vira pilha (igual ao editor): 2 cópias = uma carta atrás, 3+ = duas.
            layers = min(card.quantity, 3) - 1
            _drop_shadow(canvas, (x + layers * STACK_OFFSET, y - layers * STACK_OFFSET,
                                  x + layers * STACK_OFFSET + card_w, y - layers * STACK_OFFSET + card_h), radius)
            for layer in range(layers, 0, -1):
                off = layer * STACK_OFFSET
                draw.rounded_rectangle((x + off, y - off, x + off + card_w - 1, y - off + card_h - 1),
                                       radius=radius, fill=STACK_1 if layer == 1 else STACK_2)
            _drop_shadow(canvas, (x, y, x + card_w, y + card_h), radius)
            canvas.paste(_card_tile(card, images.get(card.scryfall_id), (card_w, card_h)), (x, y), mask)

            if card.quantity > 1:
                # Badge MD3: pílula latão com contorno da cor do fundo, presa no canto da carta.
                label = f"{card.quantity}x"
                bh = round(badge_font.size * 1.55)
                bw = max(bh, int(draw.textlength(label, font=badge_font)) + bh // 2 + 6)
                bx, by = x + card_w - bw + 8, y - 10
                draw.rounded_rectangle((bx - 3, by - 3, bx + bw + 3, by + bh + 3), radius=(bh + 6) // 2, fill=SURFACE)
                draw.rounded_rectangle((bx, by, bx + bw, by + bh), radius=bh // 2, fill=PRIMARY)
                draw.text((bx + bw / 2, by + bh / 2), label, font=badge_font, fill=ON_PRIMARY, anchor="mm")

            text_y = y + card_h + 14
            for line in name_lines[index]:
                draw.text((x, text_y), line, font=name_font, fill=ON_SURFACE)
                text_y += name_lh
            text_y += 4
            meta = _fit_text(draw, f"{card.set_code} #{card.collector_number}", meta_font, card_w)
            draw.text((x, text_y), meta, font=meta_font, fill=ON_SURFACE_VARIANT)
            if prices is not None:
                chip_y = text_y + meta_lh + 10
                for chip_row in card_chip_rows[index]:
                    cx = x
                    for chip in chip_row:
                        chip.draw(draw, cx, chip_y)
                        cx += chip.width + chip_gap
                    chip_y += chip_h + chip_gap
        y += card_h + caption_h + GAP_Y

    # ---------- rodapé ----------
    footer_y = height - footer_h
    draw.line((PADDING, footer_y, width - PADDING, footer_y), fill=OUTLINE_VARIANT, width=1)
    for i, line in enumerate(credit_lines):
        draw.text((PADDING, footer_y + 28 + 24 * i), line, font=f["footer"], fill=ON_SURFACE_VARIANT)
    _wordmark(draw, width - PADDING, footer_y + 38, f["footer_brand"], anchor="rm")

    buffer = io.BytesIO()
    canvas.save(buffer, "JPEG", quality=JPEG_QUALITY, optimize=True, progressive=True, subsampling=0)
    return buffer.getvalue()
