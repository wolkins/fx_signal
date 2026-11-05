"""Twelve Data（https://twelvedata.com）から FX の OHLC を取得する薄いラッパー。

- `time_series` は 1 回 1 通貨ペア = 1 クレジット（outputsize や interval に依らない）。
  無料の Basic は 8 クレジット/分・800 クレジット/日（UTC 0 時リセット）。
- 返る足は要求タイムゾーン（ここでは UTC）の naive index、列は Open/High/Low/Close。
- **土日にも足が返る**（ほぼ横ばいの気配に大きなヒゲが混ざる）。市場が閉まっている
  金曜 21:00 UTC 〜 日曜 22:00 UTC の足は判定に使ってはいけないので drop_weekend で捨てる。
  （実測 2026-10-31〜11-01: 横ばい 157.57 の中に 56〜72 pips のヒゲ）
- 進行中の足（開始時刻 + 足幅 > 今）は confirmed_only で落とす。

API キーは環境変数 TWELVEDATA_API_KEY（fx-signal.env に書く）。未設定なら空を返し、
呼び出し側が yfinance 等にフォールバックする。
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

API_KEY_ENV = "TWELVEDATA_API_KEY"
BASE_URL = "https://api.twelvedata.com/time_series"
TIMEOUT_SEC = 30
RETRIES = 2
RETRY_BASE_SEC = 3

# 足幅（Twelve Data の interval 文字列 → pandas Timedelta）
INTERVALS = {
    "1min": "1min", "5min": "5min", "15min": "15min", "30min": "30min",
    "45min": "45min", "1h": "1h", "2h": "2h", "4h": "4h", "1day": "1D",
}

# 週末除外: 金曜 21:00 UTC 以降（NY クローズは夏 21:00 / 冬 22:00 UTC。保守的に 21:00）
# 〜 日曜 22:00 UTC 未満（シドニー・ウェリントン開始）。
WEEKEND_CLOSE_HOUR_UTC = 21
WEEKEND_OPEN_HOUR_UTC = 22


def symbol_for(ticker: str) -> str:
    """'USDJPY=X' / 'USDJPY' → 'USD/JPY'。"""
    code = ticker.replace("=X", "").upper()
    return f"{code[:3]}/{code[3:]}" if "/" not in code and len(code) == 6 else code


def api_key() -> str | None:
    return os.environ.get(API_KEY_ENV) or None


def drop_weekend(df: pd.DataFrame) -> pd.DataFrame:
    """市場クローズ中（金 21:00 UTC 〜 日 22:00 UTC）の足を捨てる。index は naive UTC。"""
    if df is None or df.empty:
        return df
    idx = df.index
    dow, hour = idx.dayofweek, idx.hour
    closed = (
        (dow == 5)
        | ((dow == 4) & (hour >= WEEKEND_CLOSE_HOUR_UTC))
        | ((dow == 6) & (hour < WEEKEND_OPEN_HOUR_UTC))
    )
    return df[~closed]


def confirmed_only(df: pd.DataFrame, interval: str, now_utc: datetime | None = None) -> pd.DataFrame:
    """開始時刻 + 足幅 が今より後（＝進行中）の足を落とす。"""
    if df is None or df.empty:
        return df
    now = now_utc or datetime.now(timezone.utc)
    now_naive = pd.Timestamp(now.astimezone(timezone.utc)).tz_localize(None)
    width = pd.Timedelta(INTERVALS.get(interval, interval))
    return df[df.index + width <= now_naive]


def parse_values(values: list[dict]) -> pd.DataFrame:
    """API の values（新しい順）→ 古い順の OHLC DataFrame（naive UTC index）。"""
    if not values:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close"])
    rows = pd.DataFrame(values)
    out = pd.DataFrame({
        "Open": pd.to_numeric(rows["open"], errors="coerce"),
        "High": pd.to_numeric(rows["high"], errors="coerce"),
        "Low": pd.to_numeric(rows["low"], errors="coerce"),
        "Close": pd.to_numeric(rows["close"], errors="coerce"),
    })
    out.index = pd.to_datetime(rows["datetime"])
    out.index.name = "Datetime"
    return out.dropna(subset=["Close"]).sort_index()


def cache_path(ticker: str, interval: str) -> Path:
    code = ticker.replace("=X", "").replace("/", "").upper()
    return Path(__file__).with_name(f"td_cache_{code}_{interval}.json")


def _read_cache(path: Path, max_age_min: float, now: datetime) -> pd.DataFrame | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        at = datetime.fromisoformat(raw["fetched_at"])
        if (now - at).total_seconds() > max_age_min * 60:
            return None
        return parse_values(raw["values"])
    except (FileNotFoundError, KeyError, ValueError, OSError, json.JSONDecodeError):
        return None


def fetch_ohlc(ticker: str, interval: str = "5min", outputsize: int = 500, *,
               key: str | None = None, weekend: bool = False, cache_min: float = 0,
               now_utc: datetime | None = None) -> pd.DataFrame:
    """Twelve Data から足を取る。失敗・キー未設定なら空の DataFrame（例外は投げない）。

    weekend=False（既定）で市場クローズ中の足を捨てる。進行中の足は呼び出し側で confirmed_only。
    cache_min > 0 なら取得結果を td_cache_<PAIR>_<interval>.json に保存し、その分数以内は再取得しない
    （1時間足など更新の遅い足を複数の監視で共用してクレジットを節約する）。
    """
    key = key or api_key()
    if not key:
        return pd.DataFrame()
    now = now_utc or datetime.now(timezone.utc)
    cpath = cache_path(ticker, interval)
    if cache_min > 0:
        cached = _read_cache(cpath, cache_min, now)
        if cached is not None and not cached.empty:
            return cached if weekend else drop_weekend(cached)
    params = {
        "symbol": symbol_for(ticker), "interval": interval, "outputsize": int(outputsize),
        "timezone": "UTC", "order": "desc", "apikey": key,
    }
    last_err: str | None = None
    for attempt in range(RETRIES + 1):
        try:
            resp = requests.get(BASE_URL, params=params, timeout=TIMEOUT_SEC)
            data = resp.json()
        except (requests.RequestException, ValueError) as exc:
            last_err = type(exc).__name__
            data = None
        if isinstance(data, dict) and data.get("status") != "error" and data.get("values"):
            df = parse_values(data["values"])
            if cache_min > 0:
                try:
                    cpath.write_text(json.dumps({"fetched_at": now.isoformat(), "values": data["values"]}) + "\n",
                                     encoding="utf-8")
                except OSError as exc:
                    print(f"Twelve Data キャッシュ保存に失敗: {exc}", file=sys.stderr)
            return df if weekend else drop_weekend(df)
        if isinstance(data, dict):
            last_err = f"{data.get('code')}: {data.get('message')}"
            # クレジット超過(429)やキー不正(401)はリトライしても無駄
            if data.get("code") in (401, 403, 429):
                break
        if attempt < RETRIES:
            time.sleep(RETRY_BASE_SEC * (attempt + 1))
    print(f"Twelve Data 取得失敗 {symbol_for(ticker)} {interval}: {last_err}", file=sys.stderr)
    return pd.DataFrame()
