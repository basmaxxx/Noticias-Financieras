import json
import tempfile
import tomllib
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from noticias_financieras import __main__ as cli, discord, market, render, sources
from noticias_financieras.summarizer import Punto, ResumenNoticias


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.cfg = tomllib.loads((cli.ROOT / "config.toml").read_text(encoding="utf-8"))
        now = datetime.now(timezone.utc)
        self.stories = [sources.Story(i, [sources.Article(
            "Reuters", "global", f"Noticia financiera {i}", "", f"https://example.com/{i}", now
        )]) for i in range(1, 21)]
        self.client = Mock()
        self.earnings = Mock(return_value=[])
        patches = [
            patch.object(cli.sources, "fetch_all", return_value=([], sources.FetchReport(["Reuters"], []))),
            patch.object(cli.sources, "cluster", return_value=self.stories),
            patch.object(cli.market, "snapshot", return_value={}),
            patch.object(cli.market, "ccl", return_value=None),
            patch.object(cli.market, "fundamentals", return_value={}),
            patch.object(cli.market, "quotes_for", return_value={}),
            patch.object(cli.market, "earnings_between", self.earnings),
            patch.object(cli, "Summarizer", return_value=self.client),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_news_failure_still_saves_market_and_headlines(self):
        self.client.news.side_effect = RuntimeError("API caída")
        with self.assertLogs("noticias_financieras", level="ERROR"):
            report = cli.build_report(self.cfg, use_llm=True)
        self.assertIsNone(report.digest)
        self.assertIn("API caída", report.llm_error)
        self.earnings.assert_called_once()
        self.client.analysis.assert_not_called()
        with tempfile.TemporaryDirectory() as tmp, patch.object(cli, "ROOT", Path(tmp)):
            preview = cli.save(report, render.build(report))
            self.assertIn("Noticia financiera", preview.read_text(encoding="utf-8"))
            self.assertIn("API caída", json.loads(preview.with_suffix(".json").read_text(encoding="utf-8"))["llm_error"])

    def test_analysis_failure_preserves_completed_digest(self):
        self.client.news.return_value = ResumenNoticias(
            resumen_ejecutivo="Resumen verificado", highlights=[], empresas=[], argentina=[],
            macro_global=[Punto(titulo="Inflación", detalle="Bajó esta semana", fuentes=[1])],
            agenda_proxima_semana=[])
        self.client.analysis.side_effect = RuntimeError("Análisis incompleto")
        with self.assertLogs("noticias_financieras", level="ERROR"):
            report = cli.build_report(self.cfg, use_llm=True)
        self.assertIsNotNone(report.digest)
        self.assertIsNone(report.analysis)
        preview = render.to_markdown(render.build(report))
        self.assertIn("Resumen verificado", preview)
        self.assertIn("se conservan las secciones completadas", preview)

    def test_no_llm_does_not_create_api_client(self):
        cli.build_report(self.cfg, use_llm=False)
        cli.Summarizer.assert_not_called()


class RecoverableErrorsTests(unittest.TestCase):
    def test_calendar_errors_are_logged_and_do_not_stop_report(self):
        with patch.object(market.yf, "Ticker", side_effect=RuntimeError("Yahoo caído")), \
                self.assertLogs("noticias_financieras.market", level="WARNING") as logs:
            self.assertEqual(market.earnings_between(["AAPL"], datetime.now().date(), datetime.now().date()), [])
        self.assertIn("AAPL", "\n".join(logs.output))
        self.assertIn("Traceback", "\n".join(logs.output))

    def test_feed_errors_include_traceback(self):
        with patch.object(sources, "fetch_source", side_effect=ValueError("Feed inválido")), \
                self.assertLogs("noticias_financieras.sources", level="WARNING") as logs:
            now = datetime.now(timezone.utc)
            articles, report = sources.fetch_all([{"name": "Reuters", "url": "https://example.com/rss"}], now, now)
        self.assertEqual(articles, [])
        self.assertEqual(report.failed, ["Reuters"])
        self.assertIn("Traceback", "\n".join(logs.output))

    def test_discord_rate_limit_logs_attempt_and_retries(self):
        limited = Mock(status_code=429)
        limited.json.return_value = {"retry_after": 1}
        with patch.object(discord.requests, "post", side_effect=[limited, Mock(status_code=204)]) as post, \
                patch.object(discord.time, "sleep") as sleep, \
                self.assertLogs("noticias_financieras.discord", level="WARNING") as logs:
            discord._post("https://example.com/fake-webhook", {"content": "Prueba"})
        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once_with(1.5)
        self.assertIn("intento=1/5", "\n".join(logs.output))
