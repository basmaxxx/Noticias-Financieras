"""Logs por ejecución, con trazas de error y credenciales ocultas."""

import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path


def redact_secrets(text: str) -> str:
    for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DISCORD_WEBHOOK_URL"):
        if value := os.environ.get(key):
            text = text.replace(value, "[REDACTADO]")
    text = re.sub(r"https://(?:\w+\.)?discord(?:app)?\.com/api(?:/v\d+)?/webhooks/[^\s\"'<>]+",
                  "[WEBHOOK REDACTADO]", text)
    return re.sub(r"\bsk-[A-Za-z0-9_-]+", "[API KEY REDACTADA]", text)


class RedactingFormatter(logging.Formatter):
    converter = time.gmtime

    def format(self, record: logging.LogRecord) -> str:
        # Aplicar después de formatear también oculta secretos en las excepciones.
        return redact_secrets(super().format(record))


def configure_logging(output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S-%f")
    run_log = output_dir / f"ejecucion-{stamp}.log"
    error_log = output_dir / f"ejecucion-{stamp}.errors.log"
    formatter = RedactingFormatter("%(asctime)sZ %(levelname)s %(name)s: %(message)s")
    console = logging.StreamHandler()
    full = logging.FileHandler(run_log, encoding="utf-8")
    errors = logging.FileHandler(error_log, encoding="utf-8")
    errors.setLevel(logging.WARNING)
    for handler in (console, full, errors):
        handler.setFormatter(formatter)
    logging.basicConfig(level=logging.INFO, handlers=[console, full, errors], force=True)
    for name in ("yfinance", "httpx", "httpcore", "httpx2", "httpcore2", "openai"):
        logging.getLogger(name).setLevel(logging.WARNING)
    logging.captureWarnings(True)
    return run_log, error_log
