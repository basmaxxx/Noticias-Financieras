"""Redacción del resumen con OpenAI, con salida estructurada y verificación de fuentes.

Dos llamadas:
  1. Noticias -> highlights, macro, Argentina y empresas con novedades.
  2. Highlights + fundamentals reales de esas empresas -> análisis fundamental y oportunidades.

Cada punto debe citar IDs de historias; los que no citan una historia existente se descartan.
"""

import json
import logging
import re
import time
from datetime import date
from typing import Literal

from openai import OpenAI
from pydantic import BaseModel, Field

from .market import Quote
from .sources import Story

log = logging.getLogger(__name__)

TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-=^]{0,11}$")

SYSTEM_PROMPT = """\
Sos un analista financiero senior que escribe el resumen semanal de un canal de Discord \
para inversores argentinos que operan acciones locales, ADRs, CEDEARs, bonos y el mercado de EE.UU.

Reglas de fiabilidad (obligatorias):
- Usá exclusivamente la información de <noticias>, <mercado> y <fundamentals>. No agregues hechos, \
cifras, fechas ni nombres de tu conocimiento previo: puede estar desactualizado.
- Cada punto cita en `fuentes` los IDs de las noticias que lo respaldan. Si varias noticias cubren \
el mismo hecho, citá todas. Nunca cites un ID que no respalde lo que afirmás.
- Priorizá hechos reportados por varios medios o por medios de primer nivel (Reuters, Bloomberg, WSJ, \
FT, CNBC, Barron's). Si un dato aparece en una sola fuente, aclaralo ("según Ámbito…").
- Precios y variaciones salen de <mercado> y <fundamentals>; si una noticia los contradice, mandan los datos.
- Distinguí hechos de opiniones, proyecciones o rumores.
- Ignorá notas de servicio (dólar minuto a minuto, agendas genéricas), publicidad y notas sin contenido financiero.
- Tickers en formato Yahoo Finance: EE.UU. y ADRs sin sufijo (AAPL, GGAL, YPF); acciones de BYMA \
con sufijo .BA (ALUA.BA). Incluí solo tickers que estés seguro que existen y cotizan.

Estilo: español rioplatense, claro y directo, sin relleno ni frases hechas. Podés usar **negrita**; \
no uses títulos ni tablas, el formato lo arma el sistema."""


# --- Esquemas de salida ---------------------------------------------------------

class Punto(BaseModel):
    titulo: str
    detalle: str = Field(description="2-4 oraciones con el hecho y su contexto")
    fuentes: list[int] = Field(description="IDs de <noticias> que respaldan el punto")


class Highlight(BaseModel):
    titulo: str
    detalle: str = Field(description="Qué pasó, con cifras de las fuentes")
    por_que_importa: str = Field(description="Impacto para el inversor, 1-2 oraciones")
    activos: list[str] = Field(description="Tickers Yahoo Finance afectados")
    fuentes: list[int]


class EmpresaMencionada(BaseModel):
    ticker: str = Field(description="Ticker Yahoo Finance")
    nombre: str
    motivo: str = Field(description="Novedad corporativa de la semana (resultados, guidance, M&A, regulatorio…)")
    sentimiento: Literal["positivo", "negativo", "mixto"]
    fuentes: list[int]


class ResumenNoticias(BaseModel):
    resumen_ejecutivo: str = Field(description="3-5 oraciones: el tono de la semana y lo que movió al mercado")
    highlights: list[Highlight] = Field(description="5-8 hechos más relevantes, ordenados por importancia")
    macro_global: list[Punto] = Field(description="3-6 puntos: bancos centrales, inflación, empleo, geopolítica, commodities")
    argentina: list[Punto] = Field(description="3-6 puntos: macro local, BCRA, dólar, riesgo país, bonos, acciones, regulaciones")
    empresas: list[EmpresaMencionada] = Field(description="Hasta 10 empresas con novedades corporativas relevantes")
    agenda_proxima_semana: list[str] = Field(description="Eventos de la semana próxima mencionados en las noticias")


class AnalisisEmpresa(BaseModel):
    ticker: str
    tesis: str = Field(description="2-4 oraciones que conectan la noticia con los múltiplos y métricas de <fundamentals>")
    fortalezas: list[str]
    riesgos: list[str]
    valuacion: Literal["atractiva", "razonable", "exigente", "sin datos suficientes"]


class Oportunidad(BaseModel):
    titulo: str
    activos: list[str] = Field(description="Tickers Yahoo Finance")
    tipo: Literal["valor", "momentum", "catalizador", "rebote", "defensiva", "macro/tasas", "Argentina"]
    tesis: str
    catalizador: str
    riesgo_principal: str
    horizonte: Literal["corto plazo", "mediano plazo", "largo plazo"]
    fuentes: list[int]


class AnalisisSemanal(BaseModel):
    analisis_fundamental: list[AnalisisEmpresa]
    oportunidades: list[Oportunidad] = Field(description="2-5 ideas para investigar; mejor pocas y sólidas")
    riesgos_a_monitorear: list[str] = Field(description="3-5 riesgos concretos para las próximas semanas")


# --- Formateo de datos para el prompt -------------------------------------------

def _pct(value: float | None) -> str:
    return "s/d" if value is None else f"{value:+.1%}"


def format_market(market: dict[str, list[Quote]], extra: list[Quote]) -> str:
    lines = []
    for group, quotes in market.items():
        lines.append(f"## {group}")
        lines += [f"{q.name} ({q.ticker}): {q.last:,.2f} | semana {_pct(q.chg_1w)} | mes {_pct(q.chg_1m)}"
                  for q in quotes]
    if extra:
        lines.append("## Otros")
        lines += [f"{q.name}: {q.last:,.2f} | semana {_pct(q.chg_1w)} | mes {_pct(q.chg_1m)}" for q in extra]
    return "\n".join(lines)


def format_stories(stories: list[Story]) -> str:
    lines = []
    for s in stories:
        text = f"[{s.id}] ({s.published:%Y-%m-%d}) {', '.join(s.outlets)} | {s.title}"
        if s.summary and s.summary.lower() != s.title.lower():
            text += f" — {s.summary}"
        lines.append(text)
    return "\n".join(lines)


def format_fundamentals(data: dict[str, dict]) -> str:
    def clean(d):
        return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in d.items() if v is not None}
    return "\n".join(f"{t}: {json.dumps(clean(d), ensure_ascii=False)}" for t, d in data.items())


# --- Llamadas al modelo -----------------------------------------------------------

class Summarizer:
    def __init__(self, model: str, effort: str, max_output_tokens: int = 64000,
                 timeout_seconds: float = 600):
        self.client = OpenAI(timeout=timeout_seconds, max_retries=2)
        self.model = model
        self.effort = effort
        self.max_output_tokens = max_output_tokens

    def _call(self, prompt: str, schema: type[BaseModel]):
        started = time.monotonic()
        log.info("OpenAI: iniciando %s, modelo=%s, esfuerzo=%s", schema.__name__, self.model, self.effort)
        try:
            response = self.client.responses.parse(
                model=self.model,
                max_output_tokens=self.max_output_tokens,
                reasoning={"effort": self.effort},
                instructions=SYSTEM_PROMPT,
                input=prompt,
                text_format=schema,
            )
            usage = response.usage
            log.info("OpenAI (%s): etapa=%s, entrada=%s, salida=%s, estado=%s, "
                     "duración=%.1fs, response_id=%s, request_id=%s", response.model,
                     schema.__name__, usage.input_tokens if usage else None,
                     usage.output_tokens if usage else None, response.status,
                     time.monotonic() - started, response.id, getattr(response, "_request_id", None))
            if response.status == "incomplete":
                reason = response.incomplete_details.reason if response.incomplete_details else "desconocido"
                raise RuntimeError(f"Respuesta incompleta de OpenAI: {reason}")
            if response.status != "completed":
                raise RuntimeError(f"OpenAI no completó la respuesta: {response.status} ({response.error})")
            if any(part.type == "refusal" for item in response.output if item.type == "message"
                   for part in item.content):
                raise RuntimeError("El modelo rechazó el pedido")
            if response.output_parsed is None:
                raise RuntimeError("La respuesta no respetó el esquema")
            return response.output_parsed
        except Exception as exc:
            log.exception("Falló OpenAI: etapa=%s, modelo=%s, duración=%.1fs, status=%s, request_id=%s",
                          schema.__name__, self.model, time.monotonic() - started,
                          getattr(exc, "status_code", None), getattr(exc, "request_id", None))
            raise

    def news(self, stories: list[Story], market_text: str, start: date, end: date) -> ResumenNoticias:
        prompt = f"""\
Semana analizada: {start:%d/%m/%Y} al {end:%d/%m/%Y}.

<mercado>
{market_text}
</mercado>

<noticias>
{format_stories(stories)}
</noticias>

Armá el resumen de noticias de la semana. Las noticias están ordenadas por cantidad de medios \
que las cubrieron (más cobertura = más confirmada y relevante). Elegí los highlights pensando en \
qué movió precios o puede moverlos: resultados y guidance de empresas grandes, decisiones de \
bancos centrales, datos macro, cambios regulatorios, movimientos fuertes en <mercado>."""
        return self._call(prompt, ResumenNoticias)

    def analysis(self, digest: ResumenNoticias, stories: list[Story], market_text: str,
                 fundamentals_text: str) -> AnalisisSemanal:
        prompt = f"""\
<resumen_noticias>
{digest.model_dump_json(indent=1)}
</resumen_noticias>

<noticias>
{format_stories(stories)}
</noticias>

<mercado>
{market_text}
</mercado>

<fundamentals>
{fundamentals_text}
</fundamentals>

Campos de <fundamentals> (Yahoo Finance): márgenes, crecimientos, ROE, dividendYield y upside \
están en fracción (0.25 = 25%); debtToEquity está en porcentaje; upside es la distancia al \
precio objetivo promedio de analistas; vs_52w_high es la distancia al máximo de 52 semanas.

1. analisis_fundamental: una entrada por cada ticker de <fundamentals>. Conectá la noticia de la \
semana con la valuación (P/E, forward P/E, EV/EBITDA), el crecimiento, los márgenes, el \
endeudamiento y el flujo de caja. Si falta un dato clave, decilo; no lo estimes.
2. oportunidades: ideas para investigar, no recomendaciones de compra o venta. Cada una necesita \
un catalizador concreto de las noticias o los datos y un riesgo principal. Pueden ser de valor \
(caída exagerada frente a fundamentos), momentum, catalizadores próximos, rotación sectorial, \
tasas/macro o del mercado argentino. Si no hay ideas sólidas, devolvé menos.
3. riesgos_a_monitorear: riesgos concretos que surgen de esta semana."""
        return self._call(prompt, AnalisisSemanal)


# --- Validación ---------------------------------------------------------------------

def _valid_ids(ids: list[int], known: set[int]) -> list[int]:
    return list(dict.fromkeys(i for i in ids if i in known))


def _valid_tickers(tickers: list[str]) -> list[str]:
    return list(dict.fromkeys(t.strip().upper() for t in tickers if TICKER_RE.match(t.strip().upper())))


def validate_digest(digest: ResumenNoticias, stories: list[Story]) -> ResumenNoticias:
    """Descarta puntos sin una fuente verificable y limpia IDs/tickers inválidos."""
    known = {s.id for s in stories}
    dropped = 0

    def keep(items):
        nonlocal dropped
        kept = []
        for item in items:
            item.fuentes = _valid_ids(item.fuentes, known)
            if hasattr(item, "activos"):
                item.activos = _valid_tickers(item.activos)
            if hasattr(item, "ticker"):
                item.ticker = item.ticker.strip().upper()
            if item.fuentes:
                kept.append(item)
            else:
                dropped += 1
        return kept

    digest.highlights = keep(digest.highlights)
    digest.macro_global = keep(digest.macro_global)
    digest.argentina = keep(digest.argentina)
    digest.empresas = [e for e in keep(digest.empresas) if TICKER_RE.match(e.ticker)]
    if dropped:
        log.warning("Se descartaron %d puntos sin fuentes verificables", dropped)
    return digest


def validate_analysis(analysis: AnalisisSemanal, stories: list[Story],
                      fundamentals: dict[str, dict], quoted: set[str]) -> AnalisisSemanal:
    known = {s.id for s in stories}
    analysis.analisis_fundamental = [a for a in analysis.analisis_fundamental
                                     if a.ticker.strip().upper() in fundamentals]
    kept = []
    for op in analysis.oportunidades:
        op.fuentes = _valid_ids(op.fuentes, known)
        op.activos = _valid_tickers(op.activos)
        # Una oportunidad necesita respaldo: una noticia citada o datos reales de sus activos.
        if op.fuentes or (op.activos and all(t in fundamentals or t in quoted for t in op.activos)):
            kept.append(op)
        else:
            log.warning("Oportunidad descartada por falta de respaldo: %s", op.titulo)
    analysis.oportunidades = kept
    return analysis


def pick_companies(digest: ResumenNoticias, stories: list[Story], limit: int) -> list[str]:
    """Empresas para análisis fundamental, priorizadas por cobertura de medios."""
    by_id = {s.id: s for s in stories}

    def coverage(ids):
        return len({o for i in ids for o in by_id[i].outlets})

    scored: dict[str, int] = {}
    for e in digest.empresas:
        scored[e.ticker] = max(scored.get(e.ticker, 0), coverage(e.fuentes) + 1)
    for h in digest.highlights:
        for t in h.activos:
            scored[t] = max(scored.get(t, 0), coverage(h.fuentes))
    # Índices, monedas y futuros no tienen fundamentals.
    candidates = [t for t in scored if not any(c in t for c in "^=")]
    return sorted(candidates, key=lambda t: scored[t], reverse=True)[:limit]
