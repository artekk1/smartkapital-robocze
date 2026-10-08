"""Wskaźniki techniczne z dziennych notowań (Yahoo Finance chart API)."""
from __future__ import annotations

import io
from datetime import datetime, timezone

import numpy as np
import pandas as pd


def from_yahoo_chart(js: dict) -> tuple[pd.DataFrame, dict]:
    res = (js.get("chart") or {}).get("result") or []
    if not res:
        raise ValueError("pusta odpowiedź Yahoo chart")
    r = res[0]
    ts = r.get("timestamp") or []
    q = r["indicators"]["quote"][0]
    adj = (r["indicators"].get("adjclose") or [{}])[0].get("adjclose") or q["close"]
    df = pd.DataFrame({
        "open": q["open"], "high": q["high"], "low": q["low"], "close": q["close"],
        "adjclose": adj, "volume": q["volume"],
    }, index=pd.to_datetime([datetime.fromtimestamp(t, tz=timezone.utc).date() for t in ts]))
    df = df.dropna(subset=["close", "high", "low", "volume"])
    df = df[~df.index.duplicated(keep="last")]
    return df.astype(float), r.get("meta", {})


def from_stooq_csv(text: str, sessions: int = 520) -> pd.DataFrame:
    """CSV ze Stooq (Date,Open,High,Low,Close,Volume); ceny skorygowane o splity i dywidendy."""
    if not text.startswith("Date,"):
        raise ValueError(f"Stooq: {text[:80].strip()!r}")
    df = pd.read_csv(io.StringIO(text), parse_dates=["Date"], index_col="Date")
    df.columns = [c.lower() for c in df.columns]
    df = df.dropna(subset=["close", "high", "low", "volume"]).tail(sessions)
    df["adjclose"] = df["close"]
    return df[["open", "high", "low", "close", "adjclose", "volume"]].astype(float)


def pivot_lows(lows: pd.Series, k: int) -> list[tuple[pd.Timestamp, float]]:
    """Dołki potwierdzone k świecami z obu stron."""
    vals = lows.to_numpy()
    out = []
    for i in range(k, len(vals) - k):
        window = vals[i - k:i + k + 1]
        if vals[i] == window.min() and (window == vals[i]).sum() == 1:
            out.append((lows.index[i], float(vals[i])))
    return out


def higher_low(lows: pd.Series, k: int, lookback: int) -> dict:
    piv = [p for p in pivot_lows(lows, k) if p[0] >= lows.index[-min(lookback, len(lows))]]
    if len(piv) < 2:
        return {"ok": False, "pivots": piv}
    return {"ok": piv[-1][1] > piv[-2][1], "pivots": piv[-2:]}


def beta(stock: pd.DataFrame, bench: pd.DataFrame, weeks: int = 104) -> tuple[float | None, int]:
    """Beta z tygodniowych stóp zwrotu (cena skorygowana) względem benchmarku."""
    s = stock["adjclose"].resample("W-FRI").last().pct_change()
    b = bench["adjclose"].resample("W-FRI").last().pct_change()
    both = pd.concat([s, b], axis=1, keys=["s", "b"]).dropna().tail(weeks)
    if len(both) < 52:
        return None, len(both)
    var = both["b"].var()
    if var == 0:
        return None, len(both)
    return float(both["s"].cov(both["b"]) / var), len(both)


def technicals(df: pd.DataFrame, bench: pd.DataFrame) -> dict:
    close, high, low, vol = df["close"], df["high"], df["low"], df["volume"]
    last = df.index[-1]
    price = float(close.iloc[-1])
    hi_252 = high.tail(252)
    high_52w = float(hi_252.max())
    sma50 = close.rolling(50).mean()
    sma_now = float(sma50.iloc[-1]) if len(close) >= 50 else float("nan")

    # SMA50 "zaczyna rosnąć": rośnie przez ostatnie 5 sesji, a w ciągu 60 sesji wcześniej spadała.
    slope = sma50.diff(5)
    slope_now = float(slope.iloc[-1]) if len(slope.dropna()) else float("nan")
    was_falling = bool((slope.iloc[-65:-5] < 0).any()) if len(slope.dropna()) > 65 else False
    sma_turning_up = bool(slope_now > 0 and was_falling)

    hl_daily = higher_low(low, k=5, lookback=126)
    weekly_low = low.resample("W-FRI").min().dropna()
    hl_weekly = higher_low(weekly_low, k=2, lookback=52)

    # Wybicie: w ostatnich 10 sesjach zamknięcie powyżej maksimum z poprzednich 63 sesji,
    # przy wolumenie >= 1,5x średniej z 50 sesji przed tym dniem.
    breakout = None
    n = len(df)
    for i in range(max(n - 10, 64), n):
        prior_high = float(high.iloc[i - 63:i].max())
        avg_vol = float(vol.iloc[max(0, i - 50):i].mean())
        if close.iloc[i] > prior_high:
            ratio = float(vol.iloc[i] / avg_vol) if avg_vol else float("nan")
            cand = {"date": df.index[i].date().isoformat(), "close": float(close.iloc[i]),
                    "level_3m": prior_high, "vol_ratio": ratio, "ok": ratio >= 1.5}
            if breakout is None or (cand["ok"] and not breakout["ok"]):
                breakout = cand

    dollar_vol = float((close * vol).tail(50).mean())
    b, b_n = beta(df, bench)
    return {
        "date": last.date().isoformat(),
        "price": price,
        "high_52w": high_52w,
        "high_52w_date": hi_252.idxmax().date().isoformat(),
        "dist_from_high": price / high_52w - 1 if high_52w else float("nan"),
        "sma50": sma_now,
        "above_sma50": bool(price > sma_now) if not np.isnan(sma_now) else False,
        "sma50_slope5": slope_now,
        "sma50_turning_up": sma_turning_up,
        "higher_low_daily": hl_daily,
        "higher_low_weekly": hl_weekly,
        "higher_low": bool(hl_daily["ok"] or hl_weekly["ok"]),
        "breakout": breakout,
        "breakout_volume": bool(breakout and breakout["ok"]),
        "avg_dollar_volume_50d": dollar_vol,
        "beta": b,
        "beta_weeks": b_n,
        "sessions": n,
    }
