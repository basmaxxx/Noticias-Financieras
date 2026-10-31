"""Descarga de noticias desde feeds RSS, filtrado por fecha y agrupación por historia."""

import calendar
import html
import logging
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import feedparser
import requests

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")
# Umbral de similitud de títulos (Jaccard) para considerar dos notas la misma historia.
SAME_STORY_THRESHOLD = 0.6
STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "are", "was", "its", "after",
    "into", "over", "amid", "says", "will", "has", "have", "more", "than", "but",
    "los", "las", "del", "por", "para", "con", "una", "que", "sus", "como", "mas",
    "tras", "ante", "entre", "sobre", "esta", "este", "hoy",
}


@dataclass
class Article:
    outlet: str
    region: str
    title: str
    summary: str
    url: str
    published: datetime


@dataclass
class Story:
    """Una historia puede estar cubierta por varias notas de distintos medios."""

    id: int
    articles: list[Article]

    @property
    def title(self) -> str:
        return self.articles[0].title

    @property
    def summary(self) -> str:
        return next((a.summary for a in self.articles if a.summary), "")

    @property
    def region(self) -> str:
        return self.articles[0].region

    @property
    def published(self) -> datetime:
        return max(a.published for a in self.articles)

    @property
    def outlets(self) -> list[str]:
        return list(dict.fromkeys(a.outlet for a in self.articles))


@dataclass
class FetchReport:
    ok: list[str]
    failed: list[str]


def _clean(text: str, limit: int | None = None) -> str:
    text = WS_RE.sub(" ", TAG_RE.sub(" ", html.unescape(text or ""))).strip()
    if limit and len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "…"
    return text


def _entry_datetime(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        value = entry.get(key)
        if value:
            return datetime.fromtimestamp(calendar.timegm(value), tz=timezone.utc)
    return None


def _download(url: str, attempts: int = 2) -> bytes:
    for attempt in range(attempts):
        try:
            resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
            resp.raise_for_status()
            return resp.content
        except requests.RequestException:
            log.warning("Error descargando RSS: url=%s, intento=%d/%d", url, attempt + 1, attempts,
                        exc_info=True)
            if attempt == attempts - 1:
                raise
    raise AssertionError("unreachable")


def fetch_source(source: dict, since: datetime, until: datetime) -> list[Article]:
    feed = feedparser.parse(_download(source["url"]))
    if not feed.entries:
        raise ValueError(f"feed vacío o inválido ({feed.get('bozo_exception', 'sin entradas')})")

    is_google_news = "news.google.com" in source["url"]
    articles = []
    for entry in feed.entries:
        published = _entry_datetime(entry)
        # Sin fecha no podemos verificar que sea de esta semana: se descarta.
        if not published or not (since <= published <= until + timedelta(hours=1)):
            continue
        title = _clean(entry.get("title", ""))
        summary = _clean(entry.get("summary", ""), limit=400)
        if is_google_news:
            # Google News agrega " - Medio" al título y su resumen es solo el enlace.
            title = re.sub(r"\s+-\s+[^-]+$", "", title)
            summary = ""
        if title and entry.get("link"):
            articles.append(Article(
                outlet=source["name"],
                region=source.get("region", "global"),
                title=title,
                summary=summary,
                url=entry["link"],
                published=published,
            ))
    return articles


def fetch_all(sources: list[dict], since: datetime, until: datetime) -> tuple[list[Article], FetchReport]:
    def run(source):
        try:
            return source, fetch_source(source, since, until), None
        except Exception as exc:  # una fuente caída no debe frenar el resumen
            return source, [], exc

    articles: list[Article] = []
    report = FetchReport(ok=[], failed=[])
    with ThreadPoolExecutor(max_workers=8) as pool:
        for source, items, error in pool.map(run, sources):
            label = f"{source['name']} ({source['url'][:60]}…)"
            if error:
                log.warning("Fuente con error %s: %s", label, error,
                            exc_info=(type(error), error, error.__traceback__))
                report.failed.append(source["name"])
            else:
                log.info("%-20s %3d notas en la ventana", source["name"], len(items))
                report.ok.append(source["name"])
                articles.extend(items)
    # Un medio con varios feeds cuenta como OK si alguno respondió.
    report.failed = sorted(set(report.failed) - set(report.ok))
    report.ok = list(dict.fromkeys(report.ok))
    return articles, report


def _tokens(title: str) -> frozenset[str]:
    norm = unicodedata.normalize("NFKD", title.lower()).encode("ascii", "ignore").decode()
    return frozenset(w for w in re.findall(r"[a-z0-9]+", norm) if len(w) > 2 and w not in STOPWORDS)


def cluster(articles: list[Article], max_stories: int) -> list[Story]:
    """Agrupa notas casi idénticas y ordena por cobertura (cantidad de medios) y recencia."""
    groups: list[list[Article]] = []
    group_tokens: list[frozenset[str]] = []
    for article in articles:
        tokens = _tokens(article.title)
        for i, other in enumerate(group_tokens):
            if tokens and other and len(tokens & other) / len(tokens | other) >= SAME_STORY_THRESHOLD:
                groups[i].append(article)
                break
        else:
            groups.append([article])
            group_tokens.append(tokens)

    stories = [Story(id=0, articles=g) for g in groups]
    stories.sort(key=lambda s: (len(s.outlets), s.published), reverse=True)
    stories = stories[:max_stories]
    for i, story in enumerate(stories, start=1):
        story.id = i
    return stories
