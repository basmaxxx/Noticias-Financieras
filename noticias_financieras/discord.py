"""Envío al canal de Discord mediante un webhook, respetando los límites de la API."""

import logging
import time

import requests

from .render import Section

log = logging.getLogger(__name__)

MAX_EMBEDS_PER_MESSAGE = 10
MAX_CHARS_PER_MESSAGE = 5800  # Discord admite 6000 caracteres sumando todos los embeds


def _embed_size(embed: dict) -> int:
    size = len(embed.get("title", "")) + len(embed.get("description", ""))
    size += len(embed.get("footer", {}).get("text", ""))
    size += sum(len(f["name"]) + len(f["value"]) for f in embed.get("fields", []))
    return size


def _messages(sections: list[Section]) -> list[dict]:
    messages = []
    for section in sections:
        content, batch, size = section.header, [], 0
        for embed in section.embeds:
            embed_size = _embed_size(embed)
            if batch and (len(batch) == MAX_EMBEDS_PER_MESSAGE or size + embed_size > MAX_CHARS_PER_MESSAGE):
                messages.append({"content": content, "embeds": batch})
                content, batch, size = "", [], 0
            batch.append(embed)
            size += embed_size
        messages.append({"content": content, "embeds": batch})
    return [m for m in messages if m["content"] or m["embeds"]]


def _post(webhook_url: str, payload: dict, attempts: int = 5) -> None:
    payload = {**payload, "allowed_mentions": {"parse": []}}
    for attempt in range(1, attempts + 1):
        resp = requests.post(webhook_url, json=payload, params={"wait": "true"}, timeout=30)
        if resp.status_code == 429:
            delay = float(resp.json().get("retry_after", 2)) + 0.5
            log.warning("Discord limitó el envío (HTTP 429): intento=%d/%d, espera=%.1fs",
                        attempt, attempts, delay)
            time.sleep(delay)
            continue
        if resp.status_code >= 400:
            raise RuntimeError(f"Discord respondió {resp.status_code}: {resp.text[:500]}")
        return
    raise RuntimeError("Discord: demasiados reintentos por rate limit")


def send(webhook_url: str, sections: list[Section]) -> None:
    messages = _messages(sections)
    for index, message in enumerate(messages, start=1):
        log.info("Enviando mensaje %d/%d a Discord", index, len(messages))
        _post(webhook_url, message)
        time.sleep(1)  # mantiene el orden y evita el rate limit del webhook
    log.info("Publicados %d mensajes en Discord", len(messages))


def send_text(webhook_url: str, text: str) -> None:
    _post(webhook_url, {"content": text[:2000]})
