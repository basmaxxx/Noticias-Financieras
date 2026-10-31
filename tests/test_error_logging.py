import io
import logging
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from noticias_financieras import __main__ as cli
from noticias_financieras.logging_setup import configure_logging


class LoggingTests(unittest.TestCase):
    def setUp(self):
        root = logging.getLogger()
        self.handlers, self.level = root.handlers[:], root.level
        root.handlers.clear()
        self.warning_handler = logging._showwarning

    def tearDown(self):
        root = logging.getLogger()
        for handler in root.handlers[:]:
            handler.close()
            root.removeHandler(handler)
        root.handlers[:] = self.handlers
        root.setLevel(self.level)
        logging.captureWarnings(False)
        if self.warning_handler is not None:
            logging.captureWarnings(True)

    def test_files_include_tracebacks_but_hide_credentials(self):
        secret = "sk-test-super-secret"
        webhook = "https://discord.com/api/webhooks/123/secret-token"
        console = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, redirect_stderr(console), patch.dict(
            os.environ, {"OPENAI_API_KEY": secret, "DISCORD_WEBHOOK_URL": webhook}
        ):
            full, errors = configure_logging(Path(tmp))
            logger = logging.getLogger("noticias_financieras.tests")
            logger.info("Inicio de ejecución")
            try:
                raise RuntimeError(f"Error usando {secret} y {webhook}")
            except RuntimeError:
                logger.warning("Fallo recuperable", exc_info=True)
            for handler in logging.getLogger().handlers:
                handler.flush()
            full_text, error_text = full.read_text(encoding="utf-8"), errors.read_text(encoding="utf-8")
            self.assertIn("Inicio de ejecución", full_text)
            self.assertNotIn("Inicio de ejecución", error_text)
            for text in (full_text, error_text, console.getvalue()):
                self.assertIn("Traceback", text)
                self.assertNotIn(secret, text)
                self.assertNotIn(webhook, text)
                self.assertIn("REDACTADO", text)
            logging.shutdown()  # Windows requiere cerrar los archivos antes de borrar el temporal.

    def test_invalid_config_is_logged_before_report_generation(self):
        with tempfile.TemporaryDirectory() as tmp, redirect_stderr(io.StringIO()):
            root = Path(tmp)
            (root / "config.toml").write_text("[config inválida", encoding="utf-8")
            with patch.object(cli, "ROOT", root), patch("sys.argv", ["noticias", "--dry-run"]), \
                    patch.object(cli, "build_report") as build:
                self.assertEqual(cli.main(), 1)
            build.assert_not_called()
            error_file, = (root / "output").glob("*.errors.log")
            self.assertIn("TOMLDecodeError", error_file.read_text(encoding="utf-8"))
            logging.shutdown()

    def test_missing_webhook_has_persistent_log_and_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp, redirect_stderr(io.StringIO()):
            root = Path(tmp)
            (root / "config.toml").write_text("", encoding="utf-8")
            with patch.object(cli, "ROOT", root), patch("sys.argv", ["noticias"]), \
                    patch.dict(os.environ, {}, clear=True):
                self.assertEqual(cli.main(), 2)
            error_file, = (root / "output").glob("*.errors.log")
            self.assertIn("DISCORD_WEBHOOK_URL", error_file.read_text(encoding="utf-8"))
            logging.shutdown()
