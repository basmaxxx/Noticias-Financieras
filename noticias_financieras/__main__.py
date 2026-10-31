"""Genera el resumen financiero semanal y lo publica en Discord.

Uso:
    python -m noticias_financieras              # genera, espera a la hora configurada y publica
    python -m noticias_financieras --no-wait    # publica apenas termina
    python -m noticias_financieras --dry-run    # no publica: guarda la vista previa en output/
    python -m noticias_financieras --dry-run --no-llm   # sin llamar a OpenAI (solo datos y titulares)
"""

import argparse
import json
import logging
import os
import sys
import time
import tomllib
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import discord, market, render, sources
from .logging_setup import configure_logging, redact_secrets
from .summarizer import Summarizer, format_fundamentals, format_market, pick_companies, validate_analysis, validate_digest

log = logging.getLogger("noticias_financieras")
ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition("=")
        if sep and not line.lstrip().startswith("#"):
            os.environ.setdefault(key.strip(), value.strip().strip('"'))


def wait_until(post_time: str, tz: ZoneInfo, max_wait: timedelta) -> None:
    now = datetime.now(tz)
    hour, minute = map(int, post_time.split(":"))
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    delay = target - now
    if timedelta(0) < delay <= max_wait:
        log.info("Esperando hasta las %s (%d min)", post_time, delay.total_seconds() // 60)
        time.sleep(delay.total_seconds())


def build_report(cfg: dict, use_llm: bool) -> render.Report:
    tz = ZoneInfo(cfg["general"]["timezone"])
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=cfg["general"]["lookback_days"])
    today = now.astimezone(tz).date()
    mcfg, lcfg = cfg["market"], cfg["llm"]

    log.info("Recolectando noticias: %s al %s, %d feeds", since, now, len(cfg["sources"]))
    articles, fetch_report = sources.fetch_all(cfg["sources"], since, now)
    stories = sources.cluster(articles, lcfg["max_stories"])
    log.info("%d notas -> %d historias; medios OK: %d, con error: %s",
             len(articles), len(stories), len(fetch_report.ok), fetch_report.failed or "ninguno")
    if len(stories) < 20:
        raise RuntimeError(f"Muy pocas noticias recolectadas ({len(stories)}); revisar las fuentes")

    log.info("Descargando cotizaciones de %d grupos", len(mcfg["groups"]))
    snapshot = market.snapshot(mcfg["groups"], today, mcfg["max_age_days"])
    extra = []
    try:
        if ccl := market.ccl(mcfg["ccl"], today, mcfg["max_age_days"]):
            extra.append(ccl)
    except Exception:
        log.warning("No se pudo calcular el CCL", exc_info=True)
    for group, quotes in snapshot.items():
        if any(q.ticker == "ARS=X" for q in quotes):
            quotes.extend(extra)
            extra = []
    if extra:
        snapshot["Argentina"] = extra
    quoted = {q.ticker for quotes in snapshot.values() for q in quotes}

    report = render.Report(
        start=(now - timedelta(days=cfg["general"]["lookback_days"])).astimezone(tz).date(),
        end=today,
        market=snapshot,
        stories=stories,
        sources_ok=fetch_report.ok,
        sources_failed=fetch_report.failed,
        articles_count=len(articles),
        yield_tickers=tuple(mcfg.get("yield_tickers", [])),
    )

    companies: list[str] = []
    if use_llm:
        stage = "inicialización"
        try:
            summarizer = Summarizer(lcfg["model"], lcfg["effort"],
                                    max_output_tokens=lcfg.get("max_output_tokens", 64000),
                                    timeout_seconds=lcfg.get("timeout_seconds", 600))
            market_text = format_market(snapshot, [])
            stage = "resumen de noticias"
            report.digest = validate_digest(
                summarizer.news(stories, market_text, report.start, report.end), stories)
            companies = pick_companies(report.digest, stories, lcfg["max_companies"])
            log.info("Empresas para análisis fundamental: %s", companies)
            stage = "fundamentals"
            report.fundamentals = market.fundamentals(companies)
            report.fundamental_quotes = market.quotes_for(list(report.fundamentals), today, mcfg["max_age_days"])

            cited = {i for item in [*report.digest.highlights, *report.digest.macro_global,
                                    *report.digest.argentina, *report.digest.empresas] for i in item.fuentes}
            cited_stories = [s for s in stories if s.id in cited] or stories[:60]
            stage = "análisis semanal"
            analysis = summarizer.analysis(report.digest, cited_stories, market_text,
                                           format_fundamentals(report.fundamentals))
            report.analysis = validate_analysis(analysis, stories, report.fundamentals,
                                                quoted | set(report.fundamental_quotes))
        except Exception as exc:
            # Si falla la IA se publica igual con datos de mercado y titulares.
            log.exception("Informe parcial: falló la etapa %s, modelo=%s", stage, lcfg["model"])
            report.llm_error = redact_secrets(f"{type(exc).__name__}: {exc}")
    else:
        log.info("IA desactivada por --no-llm")

    watchlist = [t for group, m in mcfg["groups"].items() if group.startswith(("ADRs", "Grandes"))
                 for t in m.values()]
    tickers = list(dict.fromkeys([*watchlist, *report.fundamentals]))
    report.earnings = market.earnings_between(tickers, today + timedelta(days=1), today + timedelta(days=7))
    return report


def save(report: render.Report, sections: list[render.Section]) -> Path:
    out_dir = ROOT / "output"
    out_dir.mkdir(exist_ok=True)
    stem = out_dir / f"resumen-{report.end:%Y-%m-%d}"
    stem.with_suffix(".md").write_text(render.to_markdown(sections), encoding="utf-8")
    data = {
        "digest": report.digest.model_dump() if report.digest else None,
        "analysis": report.analysis.model_dump() if report.analysis else None,
        "fundamentals": report.fundamentals,
        "market": {g: [asdict(q) for q in qs] for g, qs in report.market.items()},
        "stories": [{"id": s.id, "title": s.title, "outlets": s.outlets,
                     "urls": [a.url for a in s.articles]} for s in report.stories],
        "llm_error": report.llm_error,
    }
    stem.with_suffix(".json").write_text(json.dumps(data, ensure_ascii=False, indent=1, default=str),
                                         encoding="utf-8")
    return stem.with_suffix(".md")


def main() -> int:
    parser = argparse.ArgumentParser(description="Resumen financiero semanal para Discord")
    parser.add_argument("--config", default=str(ROOT / "config.toml"))
    parser.add_argument("--dry-run", action="store_true", help="no publica en Discord")
    parser.add_argument("--no-llm", action="store_true", help="no llama a OpenAI")
    parser.add_argument("--no-wait", action="store_true", help="publica sin esperar a post_time")
    args = parser.parse_args()

    started = time.monotonic()
    webhook = None
    try:
        run_log, error_log = configure_logging(ROOT / "output")
        log.info("Inicio: dry_run=%s, use_llm=%s, config=%s", args.dry_run, not args.no_llm, args.config)
        log.info("Logs: %s; advertencias y errores: %s", run_log, error_log)
        load_dotenv(ROOT / ".env")
        webhook = os.environ.get("DISCORD_WEBHOOK_URL")
        cfg = tomllib.loads(Path(args.config).read_text(encoding="utf-8"))
        if not args.dry_run and not webhook:
            log.error("Falta la variable DISCORD_WEBHOOK_URL")
            return 2
        if not args.no_llm and not os.environ.get("OPENAI_API_KEY"):
            log.warning("Falta OPENAI_API_KEY; se generará el informe con datos y titulares")
        report = build_report(cfg, use_llm=not args.no_llm)
        sections = render.build(report)
        preview = save(report, sections)
        log.info("Vista previa guardada en %s", preview)
        if args.dry_run:
            log.info("Dry run completado en %.1fs; informe parcial=%s", time.monotonic() - started,
                     bool(report.llm_error))
            return 0
        if not args.no_wait:
            tz = ZoneInfo(cfg["general"]["timezone"])
            wait_until(cfg["general"]["post_time"], tz, timedelta(minutes=cfg["general"]["max_wait_minutes"]))
        discord.send(webhook, sections)
        log.info("Publicación completada en %.1fs; informe parcial=%s", time.monotonic() - started,
                 bool(report.llm_error))
        return 0
    except Exception as exc:
        log.exception("No se pudo generar o publicar el resumen")
        if webhook and not args.dry_run:
            try:
                detail = redact_secrets(f"{type(exc).__name__}: {exc}")[:300]
                discord.send_text(webhook, f"⚠️ No se pudo generar el resumen semanal: `{detail}`")
            except Exception:
                log.exception("Tampoco se pudo avisar en Discord")
        return 1


if __name__ == "__main__":
    sys.exit(main())
