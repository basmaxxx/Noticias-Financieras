"""Datos de mercado y fundamentals desde Yahoo Finance.

Todas las cifras que se publican salen de acá, nunca del modelo de lenguaje.
"""

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date

import pandas as pd
import yfinance as yf

log = logging.getLogger(__name__)

FUNDAMENTAL_FIELDS = [
    "currency", "sector", "industry", "marketCap", "trailingPE", "forwardPE", "priceToBook",
    "enterpriseToEbitda", "revenueGrowth", "earningsGrowth", "grossMargins", "operatingMargins",
    "profitMargins", "returnOnEquity", "debtToEquity", "freeCashflow", "dividendYield",
    "targetMeanPrice", "recommendationKey", "numberOfAnalystOpinions",
    "fiftyTwoWeekHigh", "fiftyTwoWeekLow",
]


@dataclass
class Quote:
    name: str
    ticker: str
    last: float
    ref_1w: float | None
    ref_1m: float | None
    as_of: date

    @property
    def chg_1w(self) -> float | None:
        return self.last / self.ref_1w - 1 if self.ref_1w else None

    @property
    def chg_1m(self) -> float | None:
        return self.last / self.ref_1m - 1 if self.ref_1m else None


def _closes(tickers: list[str]) -> pd.DataFrame:
    if not tickers:
        return pd.DataFrame()
    data = yf.download(tickers, period="3mo", interval="1d", auto_adjust=True,
                       progress=False, threads=True)
    if data is None or data.empty:
        return pd.DataFrame()
    close = data["Close"]
    if isinstance(close, pd.Series):
        close = close.to_frame(tickers[0])
    if close.index.tz is not None:
        close.index = close.index.tz_localize(None)
    return close


def _reference(series: pd.Series, days: int) -> float | None:
    older = series[series.index <= series.index[-1] - pd.Timedelta(days=days)]
    return float(older.iloc[-1]) if not older.empty else None


def _quote(name: str, ticker: str, series: pd.Series, today: date, max_age_days: int) -> Quote | None:
    s = series.dropna()
    if s.empty:
        log.warning("Sin datos para %s (%s)", name, ticker)
        return None
    as_of = s.index[-1].date()
    if (today - as_of).days > max_age_days:
        log.warning("Dato desactualizado para %s (%s): último %s, se omite", name, ticker, as_of)
        return None
    return Quote(name, ticker, float(s.iloc[-1]), _reference(s, 7), _reference(s, 30), as_of)


def snapshot(groups: dict[str, dict[str, str]], today: date, max_age_days: int) -> dict[str, list[Quote]]:
    tickers = list(dict.fromkeys(t for group in groups.values() for t in group.values()))
    close = _closes(tickers)
    result = {}
    for group, mapping in groups.items():
        quotes = [
            _quote(name, ticker, close[ticker], today, max_age_days)
            for name, ticker in mapping.items() if ticker in close
        ]
        result[group] = [q for q in quotes if q]
    return result


def quotes_for(tickers: list[str], today: date, max_age_days: int) -> dict[str, Quote]:
    close = _closes(tickers)
    quotes = {t: _quote(t, t, close[t], today, max_age_days) for t in tickers if t in close}
    return {t: q for t, q in quotes.items() if q}


def ccl(cfg: dict, today: date, max_age_days: int) -> Quote | None:
    """Dólar contado con liquidación implícito: precio local * ratio / precio del ADR."""
    close = _closes([cfg["local"], cfg["adr"]])
    if cfg["local"] not in close or cfg["adr"] not in close:
        return None
    implied = (close[cfg["local"]] * cfg["ratio"] / close[cfg["adr"]]).dropna()
    return _quote(cfg["name"], "CCL", implied, today, max_age_days)


def movers(quotes: list[Quote], n: int = 5) -> tuple[list[Quote], list[Quote]]:
    ranked = sorted((q for q in quotes if q.chg_1w is not None), key=lambda q: q.chg_1w, reverse=True)
    return ranked[:n], ranked[::-1][:n]


def fundamentals(tickers: list[str]) -> dict[str, dict]:
    def one(ticker):
        try:
            info = yf.Ticker(ticker).info or {}
        except Exception as exc:
            log.warning("No se pudieron obtener fundamentals de %s: %s", ticker, exc)
            return ticker, None
        price = info.get("currentPrice") or info.get("regularMarketPrice")
        name = info.get("shortName") or info.get("longName")
        if not price or not name:
            log.warning("Ticker sin datos válidos en Yahoo: %s", ticker)
            return ticker, None
        data = {k: info.get(k) for k in FUNDAMENTAL_FIELDS}
        data.update(name=name, price=price)
        target, high = info.get("targetMeanPrice"), info.get("fiftyTwoWeekHigh")
        data["upside"] = target / price - 1 if target else None
        data["vs_52w_high"] = price / high - 1 if high else None
        return ticker, data

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = dict(pool.map(one, tickers))
    return {t: d for t, d in results.items() if d}


def earnings_between(tickers: list[str], start: date, end: date) -> list[tuple[date, str]]:
    def one(ticker):
        try:
            cal = yf.Ticker(ticker).calendar or {}
        except Exception:
            return []
        dates = (cal.get("Earnings Date") or []) if isinstance(cal, dict) else []
        return [(d, ticker) for d in dates if isinstance(d, date) and start <= d <= end]

    with ThreadPoolExecutor(max_workers=4) as pool:
        found = [item for items in pool.map(one, tickers) for item in items]
    return sorted(set(found))
