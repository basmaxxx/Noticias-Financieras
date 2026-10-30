"""Arma los mensajes de Discord (embeds) y una vista previa en Markdown."""

from dataclasses import dataclass, field
from datetime import date

from .market import Quote
from .sources import Story
from .summarizer import AnalisisSemanal, Punto, ResumenNoticias

BLUE, GREEN, RED, ORANGE, PURPLE, GREY, GOLD = 0x1F6FEB, 0x2EA043, 0xDA3633, 0xD29922, 0x8957E5, 0x6E7681, 0xE3B341
DESCRIPTION_LIMIT = 4000
RECOMMENDATION = {
    "strong_buy": "Compra fuerte", "buy": "Compra", "hold": "Mantener",
    "underperform": "Bajo rendimiento", "sell": "Venta", "strong_sell": "Venta fuerte",
}
VALUATION_COLOR = {"atractiva": GREEN, "razonable": BLUE, "exigente": ORANGE, "sin datos suficientes": GREY}
WEEKDAYS = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]
SENTIMENT_ICON ={"positivo": "🟢", "negativo": "🔴", "mixto": "🟡"}
DISCLAIMER = (
    "⚠️ *Resumen generado automáticamente a partir de medios financieros y datos de Yahoo Finance. "
    "Es información, no asesoramiento financiero: verificá las fuentes y hacé tu propio análisis "
    "antes de invertir.*"
)


@dataclass
class Section:
    header: str
    embeds: list[dict] = field(default_factory=list)


@dataclass
class Report:
    start: date
    end: date
    market: dict[str, list[Quote]]
    stories: list[Story]
    sources_ok: list[str]
    sources_failed: list[str]
    articles_count: int
    digest: ResumenNoticias | None = None
    analysis: AnalisisSemanal | None = None
    fundamentals: dict[str, dict] = field(default_factory=dict)
    fundamental_quotes: dict[str, Quote] = field(default_factory=dict)
    earnings: list[tuple[date, str]] = field(default_factory=list)
    yield_tickers: tuple[str, ...] = ()
    llm_error: str | None = None


# --- Formato de números ------------------------------------------------------------

def pct(value: float | None, digits: int = 1) -> str:
    return "s/d" if value is None else f"{value * 100:+.{digits}f}%"


def plain_pct(value: float | None) -> str:
    return "s/d" if value is None else f"{value * 100:.1f}%"


def money(value: float | None, currency: str | None = "USD") -> str:
    if value is None:
        return "s/d"
    prefix = "$" if currency in (None, "USD") else f"{currency} "
    for div, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
        if abs(value) >= div:
            return f"{prefix}{value / div:,.2f}{suffix}"
    return f"{prefix}{value:,.2f}"


def ratio(value: float | None) -> str:
    return "s/d" if value is None else f"{value:.1f}x"


def dot(value: float | None) -> str:
    if value is None:
        return "⚪"
    return "🟢" if value > 0 else "🔴" if value < 0 else "⚪"


# --- Helpers ----------------------------------------------------------------------

def _chunks(lines: list[str], limit: int = DESCRIPTION_LIMIT) -> list[str]:
    chunks, current = [], ""
    for line in lines:
        if current and len(current) + len(line) + 1 > limit:
            chunks.append(current)
            current = ""
        current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks


def _text_embeds(title: str, lines: list[str], color: int) -> list[dict]:
    embeds = [{"description": chunk, "color": color} for chunk in _chunks(lines)]
    if embeds:
        embeds[0]["title"] = title
    return embeds


def _links(ids: list[int], by_id: dict[int, Story], max_links: int = 3) -> str:
    articles = [a for i in ids if i in by_id for a in by_id[i].articles]
    # Enlaces directos primero (los de Google News son largos y redirigen).
    articles.sort(key=lambda a: "news.google.com" in a.url)
    seen, links = set(), []
    for a in articles:
        if a.outlet not in seen:
            seen.add(a.outlet)
            links.append(f"[{a.outlet}]({a.url})")
    count = len(seen)
    label = f"📰 {count} {'medio' if count == 1 else 'medios'}"
    return f"{label}: " + " · ".join(links[:max_links]) if links else ""


def _tickers(tickers: list[str]) -> str:
    return " ".join(f"`{t}`" for t in tickers)


def _quote_table(quotes: list[Quote], yields: tuple[str, ...]) -> str:
    rows = [f"{'':17}{'Último':>11}{'Semana':>9}{'Mes':>8}"]
    for q in quotes:
        if q.ticker in yields:
            last = f"{q.last:.2f}%"
            week = f"{(q.last - q.ref_1w) * 100:+.0f}pb" if q.ref_1w else "s/d"
            month = f"{(q.last - q.ref_1m) * 100:+.0f}pb" if q.ref_1m else "s/d"
        else:
            last, week, month = f"{q.last:,.2f}", pct(q.chg_1w), pct(q.chg_1m)
        rows.append(f"{q.name[:16]:17}{last:>11}{week:>9}{month:>8}")
    return "```\n" + "\n".join(rows) + "\n```"


def _points(points: list[Punto], by_id: dict[int, Story]) -> list[str]:
    return [f"**• {p.titulo}**\n{p.detalle}\n{_links(p.fuentes, by_id, max_links=2)}\n" for p in points]


# --- Secciones ----------------------------------------------------------------------

def _market_section(r: Report) -> Section:
    section = Section("## 🌎 Mercados en la semana")
    for group, quotes in r.market.items():
        if quotes:
            section.embeds.append({"title": group, "description": _quote_table(quotes, r.yield_tickers),
                                   "color": GREY})
    stocks = [q for group, quotes in r.market.items() if group.startswith(("ADRs", "Grandes"))
              for q in quotes]
    if stocks:
        best = sorted((q for q in stocks if q.chg_1w is not None), key=lambda q: q.chg_1w, reverse=True)
        top, bottom = best[:5], best[::-1][:5]
        fmt = lambda qs: "\n".join(f"{dot(q.chg_1w)} **{q.name}** `{q.ticker}` {pct(q.chg_1w)}" for q in qs)
        section.embeds.append({
            "title": "Mayores movimientos (watchlist)",
            "color": GREY,
            "fields": [{"name": "Suben", "value": fmt(top), "inline": True},
                       {"name": "Bajan", "value": fmt(bottom), "inline": True}],
        })
    return section


def _highlights_section(r: Report, by_id) -> Section:
    section = Section("## 🔥 Highlights de la semana")
    for i, h in enumerate(r.digest.highlights, start=1):
        lines = [h.detalle, f"**Por qué importa:** {h.por_que_importa}"]
        if h.activos:
            lines.append(f"**Activos:** {_tickers(h.activos)}")
        lines.append(_links(h.fuentes, by_id))
        section.embeds.append({"title": f"{i}. {h.titulo}"[:256], "description": "\n".join(lines)[:4096],
                               "color": GOLD})
    return section


def _company_embed(ticker: str, data: dict, quote: Quote | None, r: Report, by_id) -> dict:
    cur = data.get("currency")
    analysis = next((a for a in (r.analysis.analisis_fundamental if r.analysis else [])
                     if a.ticker.upper() == ticker), None)
    news = next((e for e in (r.digest.empresas if r.digest else []) if e.ticker == ticker), None)

    lines = []
    if news:
        lines.append(f"{SENTIMENT_ICON[news.sentimiento]} **Novedad:** {news.motivo}")
    if analysis:
        lines.append(f"**Tesis:** {analysis.tesis}")
        if analysis.fortalezas:
            lines.append("**A favor:** " + "; ".join(analysis.fortalezas))
        if analysis.riesgos:
            lines.append("**Riesgos:** " + "; ".join(analysis.riesgos))
    if news:
        lines.append(_links(news.fuentes, by_id))

    target = data.get("targetMeanPrice")
    analysts = data.get("numberOfAnalystOpinions")
    rec = RECOMMENDATION.get(data.get("recommendationKey") or "", "s/d")
    week = f" ({pct(quote.chg_1w)} sem.)" if quote else ""
    fields = [
        ("Precio", f"{money(data['price'], cur)}{week}"),
        ("Market cap", money(data.get("marketCap"), cur)),
        ("P/E TTM / Fwd", f"{ratio(data.get('trailingPE'))} / {ratio(data.get('forwardPE'))}"),
        ("EV/EBITDA", ratio(data.get("enterpriseToEbitda"))),
        ("Crec. ingresos / ganancias", f"{pct(data.get('revenueGrowth'))} / {pct(data.get('earningsGrowth'))}"),
        ("Margen operativo / neto", f"{plain_pct(data.get('operatingMargins'))} / {plain_pct(data.get('profitMargins'))}"),
        ("ROE", plain_pct(data.get("returnOnEquity"))),
        ("Deuda / Patrimonio", "s/d" if data.get("debtToEquity") is None else f"{data['debtToEquity']:.0f}%"),
        ("Free cash flow", money(data.get("freeCashflow"), cur)),
        ("Objetivo analistas", f"{money(target, cur)} ({pct(data.get('upside'))})" if target else "s/d"),
        ("Consenso", f"{rec} ({analysts} analistas)" if analysts else rec),
        ("vs. máx. 52 sem.", pct(data.get("vs_52w_high"))),
    ]
    title = f"{data['name']} ({ticker})"
    if analysis:
        title += f" — valuación {analysis.valuacion}"
    return {
        "title": title[:256],
        "description": "\n".join(lines)[:4096] or None,
        "color": VALUATION_COLOR[analysis.valuacion] if analysis else BLUE,
        "fields": [{"name": n, "value": v, "inline": True} for n, v in fields],
        "footer": {"text": f"Sector: {data.get('sector') or 's/d'} · {data.get('industry') or ''}".strip(" ·")},
    }


def _companies_section(r: Report, by_id) -> Section:
    section = Section("## 🏢 Empresas de la semana — análisis fundamental")
    for ticker, data in r.fundamentals.items():
        embed = _company_embed(ticker, data, r.fundamental_quotes.get(ticker), r, by_id)
        if embed["description"] is None:
            del embed["description"]
        section.embeds.append(embed)
    return section


def _opportunities_section(r: Report, by_id) -> Section:
    section = Section("## 💡 Oportunidades para investigar")
    for op in r.analysis.oportunidades:
        lines = [
            f"**Tipo:** {op.tipo} · **Horizonte:** {op.horizonte}",
            f"**Activos:** {_tickers(op.activos)}" if op.activos else "",
            f"**Tesis:** {op.tesis}",
            f"**Catalizador:** {op.catalizador}",
            f"**Riesgo principal:** {op.riesgo_principal}",
            _links(op.fuentes, by_id),
        ]
        section.embeds.append({"title": op.titulo[:256], "description": "\n".join(l for l in lines if l)[:4096],
                               "color": GREEN})
    return section


def _risks_and_agenda(r: Report) -> Section:
    section = Section("## 📅 Riesgos y agenda")
    if r.analysis and r.analysis.riesgos_a_monitorear:
        section.embeds += _text_embeds("Riesgos a monitorear", [f"• {x}" for x in r.analysis.riesgos_a_monitorear], RED)
    agenda = [f"• {x}" for x in (r.digest.agenda_proxima_semana if r.digest else [])]
    if r.earnings:
        agenda.append("\n**Balances de la watchlist (según Yahoo Finance):**")
        agenda += [f"• {WEEKDAYS[d.weekday()]} {d:%d/%m} — `{t}`" for d, t in r.earnings]
    if agenda:
        section.embeds += _text_embeds("Agenda de la próxima semana", agenda, PURPLE)
    return section


def _fallback_section(r: Report, by_id) -> Section:
    lines = [f"• **{s.title}**\n{_links([s.id], by_id, max_links=2)}" for s in r.stories[:15]]
    section = Section("## 📰 Noticias con más cobertura")
    section.embeds += _text_embeds("Sin análisis de IA esta semana — titulares más cubiertos", lines, GREY)
    return section


def build(r: Report) -> list[Section]:
    by_id = {s.id: s for s in r.stories}
    header = Section(f"# 📊 Resumen financiero semanal\n**{r.start:%d/%m} – {r.end:%d/%m/%Y}**")
    if r.digest:
        header.embeds.append({"title": "Resumen ejecutivo", "description": r.digest.resumen_ejecutivo,
                              "color": BLUE})
    sections = [header, _market_section(r)]
    if r.digest:
        sections.append(_highlights_section(r, by_id))
        if r.digest.macro_global:
            sections.append(Section("## 🏛️ Macro global", _text_embeds("Macro global", _points(r.digest.macro_global, by_id), BLUE)))
        if r.digest.argentina:
            sections.append(Section("## 🇦🇷 Argentina", _text_embeds("Argentina", _points(r.digest.argentina, by_id), 0x74ACDF)))
    else:
        sections.append(_fallback_section(r, by_id))
    if r.fundamentals:
        sections.append(_companies_section(r, by_id))
    if r.analysis and r.analysis.oportunidades:
        sections.append(_opportunities_section(r, by_id))
    sections.append(_risks_and_agenda(r))

    footer = [DISCLAIMER,
              f"-# {r.articles_count} notas analizadas de {len(r.sources_ok)} medios: {', '.join(r.sources_ok)}."]
    if r.sources_failed:
        footer.append(f"-# Sin respuesta esta semana: {', '.join(r.sources_failed)}.")
    if r.llm_error:
        footer.append(f"-# El análisis con IA falló ({r.llm_error[:150]}); se publicaron solo datos y titulares.")
    sections.append(Section("\n".join(footer)))
    return [s for s in sections if s.embeds or not s.header.startswith("##")]


def to_markdown(sections: list[Section]) -> str:
    out = []
    for s in sections:
        out.append(s.header)
        for e in s.embeds:
            if e.get("title"):
                out.append(f"\n### {e['title']}")
            if e.get("description"):
                out.append(e["description"])
            for f in e.get("fields", []):
                out.append(f"- **{f['name']}:** {f['value']}")
            if e.get("footer"):
                out.append(f"_{e['footer']['text']}_")
        out.append("")
    return "\n".join(out)
