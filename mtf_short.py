"""ふた山ショート／ふた山ロング（マルチタイムフレーム・ダウ）シグナル。
`python fx_signal.py --mtf`（両方向）/ `--mtf-short` / `--mtf-long` で起動。

ロング用ダウ理論（dow_guard.py）の鏡像を 4h / 1h / 15m の 3 段で使う戻り売りの合図（ショート）と、
その価格を上下反転して同じ規則を当てた押し目買いの合図（ロング）。判定コードは 1 本で、ロングは mirror() を通すだけ。
5分足ショート監視（short_signal.py）とは独立に動き、状態は mtf_state_<PAIR>.json（ショート）/
mtf_state_long_<PAIR>.json（ロング）。cron は平日 15 分おき。両方向を 1 プロセスで回すので足の取得は 1 回。

規則（決定論的・先読みなし。backtest と本体が同じ関数を使う）
  4h : 鏡像ダウで「下降継続」（採用済み安値を終値で割った後、実質戻り高値を終値で超えていない）
       ＋ 戻り売り直撃エリア: 割った過去の 4h 押し安値のうち現在値のすぐ上の帯（ヒゲ先〜実体下端）
       ＋ 直近下落波（高値→安値）の 80% 戻し水準を 1h 高値が超えていない
  1h : ふた山（2番天井）。採用済みの山 2 つが 4h 直近安値より後にでき、価格差 ≤ TOP_TOL×ATR(1h)、
       直撃エリアに届いている（オーバーシュート許容）。高い方を 1h 終値で超えたら無効
  15m: 2番天井より後にできた 15m 採用済み安値を終値で割ったら SHORT（エントリー）
       手仕舞い: 15m 直近採用済み高値を終値で超える / 2番天井高値 + 0.1×ATR(1h) を High で抜ける（損切り）/
                4h 下降終了。ふた山が有効な間は同じ条件で入り直す（無効になったら次のふた山まで待つ）
  山谷の採用は dow_guard と同じ（左右 PIVOT_LR 本で確定、直前の山谷から MIN_WAVE 本以上の "leg" ルール）。

較正（Dukascopy 5分足 3年 2023-06〜2026-06、翌足始値執行、スプレッド・ショート swap 控除後、pips）
  USD/JPY +1445 / 最大DD 107 / 44往復 / 勝率 50% / PF 3.9（2023 +326, 2024 +38, 2025 +1138, 2026 -24）
    ふた山の許容幅 1.0〜3.0×ATR で +1145〜+1463（すべてプラス）。直撃エリア条件を外すと +765 / DD 424。
  AUD/JPY -398、NZD/JPY -296 → USD/JPY のみ既定。
  「4h 下降継続中に 15m 安値割れで何度でも入り直す」版は全ペア大きくマイナス（USD -573 / AUD -2402 / NZD -1785）。
  件数が少なく（月 1 回強）、利益の半分は上位 3 件。2024・2026 はほぼゼロ。情報提供として使う。
  ロング（鏡像、受取スワップ加算）: USD/JPY +864 / 最大DD 250 / 62往復 / 勝率 47% / PF 2.1
    （2023 -235, 2024 +610, 2025 +308, 2026 +141）。許容幅 1.0〜3.0×ATR で +773〜+1247（すべてプラス）。
    直撃エリアを外すと +702 / DD 519。AUD/JPY -104、NZD/JPY -13 → USD/JPY のみ既定。
  時間軸を 1h/15m/5m に下げた版（本の短期案）はショート USD -528 / AUD -435、ロング USD +428 だが許容幅で
  +428〜+1349 と不安定なため採用しない（2026-11-08 検証）。

データ源: TWELVEDATA_API_KEY があれば Twelve Data（15分足 500 本、1時間足 5000 本を 55 分キャッシュで
short_signal と共用）、4時間足は 1時間足から合成（UTC 0 時起点、backtest と同じ）。無ければ yfinance。
  消費目安: 15分足 96/日 ＋ 1時間足（共用）26/日。
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

import dow_guard as dg
import fx_signal as fx
import twelvedata as td

STATE_SHORT = "SHORT"
STATE_LONG = "LONG"
STATE_FLAT = "FLAT"
STATES = (STATE_SHORT, STATE_LONG, STATE_FLAT)
SIDES = ("short", "long")

DEFAULT_PAIRS = {"short": ["USDJPY=X"], "long": ["USDJPY=X"]}
PAIRS_ENV = {"short": "MTF_SHORT_PAIRS", "long": "MTF_LONG_PAIRS"}
ENTRY_INTERVAL = "15min"
ENTRY_BARS = 500             # 約 5 営業日分（15m の山谷と ATR に十分）
ENV_INTERVAL = "1h"
ENV_BARS = 5000              # 約 7 か月（4h 合成で約 1100 本。鏡像ダウの初期化に十分）
ENV_REFRESH_MIN = 55
STALE_MINUTES = 45           # 最新確定 15分足がこれより古ければ市場クローズ中
LR, MW = dg.PIVOT_LR, dg.MIN_WAVE
TOP_TOL = 1.5                # ふた山の価格差許容（×ATR 1h）
ZONE_TOL_ATR = 1.0           # 直撃エリア上端からの許容（×ATR 1h）
ZONE_LOOKBACK_1H = 24        # 直撃エリア到達・80% 判定に使う 1h 高値の本数
HARD_SL_ATR = 0.1            # 2番天井高値 + これ×ATR(1h) を High で抜けたら損切り
RETRACE_LIMIT = 0.8
MIN_4H_BARS = 60

TOKYO = fx.TOKYO


def pairs(side: str = "short") -> list[str]:
    """監視ペア。環境変数 MTF_SHORT_PAIRS / MTF_LONG_PAIRS（カンマ区切り、空なら既定）。"""
    env = os.environ.get(PAIRS_ENV[side])
    if env is None or not env.strip():
        return list(DEFAULT_PAIRS[side])
    return [p.strip().upper().replace("=X", "") + "=X" for p in env.split(",") if p.strip()]


def state_path(ticker: str, side: str = "short") -> Path:
    """状態ファイル。ショートは従来どおり mtf_state_<PAIR>.json、ロングは mtf_state_long_<PAIR>.json。"""
    tag = "" if side == "short" else f"{side}_"
    return Path(__file__).with_name(f"mtf_state_{tag}{fx.pair_code(ticker)}.json")


# ─────────────────────────────────────────────────────────────
# 山谷・鏡像ダウ（backtest と共用）
# ─────────────────────────────────────────────────────────────
def swing_track(bars: pd.DataFrame, lr: int = LR, mw: int = MW, keep_list: bool = False) -> dict:
    """各バー確定時点の採用済み山谷（dow_guard の "leg" ルール）。
    返り値: lastH/prevH/lastL/prevL（各バーの Pivot or None の list）、keep_list なら lists（採用列のコピー）。"""
    piv = dg.find_pivots(bars, lr)
    n = len(bars)
    sw: list[dg.Pivot] = []
    pi = 0
    lastH, prevH, lastL, prevL, lists = [], [], [], [], []

    def adopt(p: dg.Pivot) -> None:
        if not sw:
            sw.append(p)
            return
        last = sw[-1]
        if p.kind == last.kind:
            if (p.price > last.price) if p.kind == "H" else (p.price < last.price):
                sw[-1] = p
            return
        if p.pos - last.pos < mw:
            return
        sw.append(p)

    for i in range(n):
        while pi < len(piv) and piv[pi].pos + lr <= i:
            adopt(piv[pi])
            pi += 1
        hs = [s for s in sw if s.kind == "H"]
        ls = [s for s in sw if s.kind == "L"]
        lastH.append(hs[-1] if hs else None)
        prevH.append(hs[-2] if len(hs) > 1 else None)
        lastL.append(ls[-1] if ls else None)
        prevL.append(ls[-2] if len(ls) > 1 else None)
        if keep_list:
            lists.append(list(sw))
    return dict(lastH=lastH, prevH=prevH, lastL=lastL, prevL=prevL, lists=lists)


def short_dow(bars: pd.DataFrame, lr: int = LR, mw: int = MW):
    """鏡像ダウ（簡略版）: 採用済み安値を終値で割ったら DOWN、その時点の直近高値が実質戻り高値。
    終値で超えたら終了。返り値: down[bool], key_high[float|nan], last_break[pos], swing_track 結果。"""
    st = swing_track(bars, lr, mw)
    c = bars["Close"].to_numpy()
    n = len(c)
    down = np.zeros(n, bool)
    kh = np.full(n, np.nan)
    last_break = np.full(n, -1)
    pend_low = None
    key_high = None
    d = False
    lb = -1
    for i in range(n):
        L = st["lastL"][i]
        if L is not None and (pend_low is None or L.pos != pend_low.pos):
            pend_low = L
        if pend_low is not None and c[i] < pend_low.price and pend_low.pos < i:
            d = True
            H = st["lastH"][i]
            key_high = H.price if H is not None else key_high
            pend_low = None
            lb = i
        elif d and key_high is not None and c[i] > key_high:
            d = False
            key_high = None
        down[i] = d
        kh[i] = key_high if key_high is not None else np.nan
        last_break[i] = lb
    return down, kh, last_break, st


def h4_context(b4: pd.DataFrame) -> dict:
    """4h 各バー確定時点の: down / key_high / 直撃エリア（zone_lo, zone_hi）/ 80% 水準 / 直近安値の時刻。"""
    down, kh, lb, st = short_dow(b4)
    o, h, l, c = (b4[x].to_numpy() for x in ("Open", "High", "Low", "Close"))
    lists = swing_track(b4, keep_list=True)["lists"]
    n = len(b4)
    zone_lo = np.full(n, np.nan)
    zone_hi = np.full(n, np.nan)
    ret_lim = np.full(n, np.nan)
    lastL_time = np.full(n, np.datetime64("NaT"), dtype="datetime64[ns]")
    for i in range(n):
        if not down[i]:
            continue
        sw = lists[i]
        ls_above = [s for s in sw if s.kind == "L" and s.price > c[i]]
        if ls_above:
            z = min(ls_above, key=lambda s: s.price)
            zone_lo[i] = l[z.pos]
            zone_hi[i] = min(o[z.pos], c[z.pos])
        L = st["lastL"][i]
        if L is not None:
            lastL_time[i] = b4.index[L.pos].to_datetime64()
            hs_before = [s for s in sw if s.kind == "H" and s.pos < L.pos]
            if hs_before:
                H = hs_before[-1]
                ret_lim[i] = L.price + RETRACE_LIMIT * (H.price - L.price)
    return dict(down=down, key_high=kh, zone_lo=zone_lo, zone_hi=zone_hi, ret_lim=ret_lim, lastL_time=lastL_time,
                end=b4["end"].to_numpy())


def h1_setup(b1: pd.DataFrame, ctx4: dict, top_tol: float = TOP_TOL) -> dict:
    """1h 各バー確定時点のふた山判定。valid[bool] / top[float] / top_time[datetime64] / atr[float] / dt_pos."""
    st = swing_track(b1)
    c = b1["Close"].to_numpy()
    h = b1["High"].to_numpy()
    atr = fx.compute_atr(b1[["High", "Low", "Close"]], 14).to_numpy()
    e1 = b1["end"].to_numpy()
    m41 = np.searchsorted(ctx4["end"], e1, side="right") - 1
    n = len(b1)
    valid = np.zeros(n, bool)
    top = np.full(n, np.nan)
    top_time = np.full(n, np.datetime64("NaT"), dtype="datetime64[ns]")
    top1 = np.full(n, np.nan)
    for i in range(n):
        k = m41[i]
        if k < 0 or not ctx4["down"][k]:
            continue
        H2, H1 = st["lastH"][i], st["prevH"][i]
        if H2 is None or H1 is None:
            continue
        t4 = ctx4["lastL_time"][k]
        if np.isnat(t4) or b1.index[H1.pos].to_datetime64() < t4:
            continue
        if np.isnan(atr[i]) or abs(H2.price - H1.price) > top_tol * atr[i]:
            continue
        tp = max(H1.price, H2.price)
        if c[H2.pos + LR:i + 1].max() > tp:
            continue
        zl, zh = ctx4["zone_lo"][k], ctx4["zone_hi"][k]
        if np.isnan(zl):
            continue
        if not (tp >= zl and min(H1.price, H2.price) <= zh + ZONE_TOL_ATR * atr[i]):
            continue
        rl = ctx4["ret_lim"][k]
        if not np.isnan(rl) and tp > rl:
            continue
        valid[i] = True
        top[i] = tp
        top1[i] = min(H1.price, H2.price)
        top_time[i] = b1.index[H2.pos].to_datetime64()
    return dict(valid=valid, top=top, top_low=top1, top_time=top_time, atr=atr, end=e1, m41=m41)


def resample_4h(b1: pd.DataFrame) -> pd.DataFrame:
    """1h（naive UTC）→ 4h（UTC 0 時起点）。backtest の 5m→4h と同じ区切り。"""
    out = b1[["Open", "High", "Low", "Close"]].resample("4h").agg(
        {"Open": "first", "High": "max", "Low": "min", "Close": "last"}).dropna(subset=["Close"])
    out = out[out.index.dayofweek != 6]
    out["end"] = out.index + pd.Timedelta("4h")
    return out


def with_end(df: pd.DataFrame, interval: str) -> pd.DataFrame:
    out = df[["Open", "High", "Low", "Close"]].copy()
    out["end"] = out.index + pd.Timedelta(td.INTERVALS.get(interval, interval))
    return out


# ─────────────────────────────────────────────────────────────
# データ取得
# ─────────────────────────────────────────────────────────────
def _naive_utc(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    out = df[[c for c in ("Open", "High", "Low", "Close") if c in df.columns]].copy()
    if getattr(out.index, "tz", None) is not None:
        out.index = out.index.tz_convert("UTC").tz_localize(None)
    return out


def fetch_entry_bars(ticker: str, now_utc: datetime | None = None) -> pd.DataFrame:
    df = td.fetch_ohlc(ticker, ENTRY_INTERVAL, ENTRY_BARS)
    source = "twelvedata"
    if df is None or df.empty:
        df = _naive_utc(fx.fetch_data(ticker, interval="15m", period="5d"))
        source = "yfinance"
    if df is None or df.empty:
        return pd.DataFrame()
    df = td.confirmed_only(td.drop_weekend(df), ENTRY_INTERVAL, now_utc)
    df.attrs["source"] = source
    return df


def fetch_env_bars(ticker: str, now_utc: datetime | None = None) -> pd.DataFrame:
    df = td.fetch_ohlc(ticker, ENV_INTERVAL, ENV_BARS, cache_min=ENV_REFRESH_MIN, now_utc=now_utc)
    if df is None or df.empty:
        df = _naive_utc(fx.fetch_data(ticker, interval="1h", period="1y"))
    if df is None or df.empty:
        return pd.DataFrame()
    return td.confirmed_only(td.drop_weekend(df), ENV_INTERVAL, now_utc)


# ─────────────────────────────────────────────────────────────
# 判定
# ─────────────────────────────────────────────────────────────
def mirror(df: pd.DataFrame) -> pd.DataFrame:
    """上下反転（ロング用）。High/Low を入れ替えて符号反転する。attrs（source）は引き継ぐ。"""
    out = pd.DataFrame({"Open": -df["Open"], "High": -df["Low"], "Low": -df["High"], "Close": -df["Close"]}, index=df.index)
    out.attrs = dict(df.attrs)
    return out


_NEG_KEYS = ("price", "env_key", "setup_ext", "setup_inner", "trig_15m", "exit_15m", "entry_price", "hard_sl", "stop_hit")


def _unmirror(info: dict) -> dict:
    for k in _NEG_KEYS:
        if info.get(k) is not None:
            info[k] = -float(info[k])
    lo, hi = info.get("zone_lo"), info.get("zone_hi")
    info["zone_lo"], info["zone_hi"] = (None if hi is None else -float(hi)), (None if lo is None else -float(lo))
    return info


def evaluate(b15: pd.DataFrame, b1: pd.DataFrame, prev: dict | None, now_utc: datetime | None = None,
             side: str = "short") -> dict | None:
    """確定 15m / 1h 足と前回状態から今回の状態を返す。データ不足・鮮度不良なら None。

    side="long" は価格を上下反転してショートの規則をそのまま適用し、結果の価格を元に戻す
    （4h 上昇ダウ ＋ 直撃エリア（抜けた戻り高値の帯）＋ 1h ふた山（2番底）＋ 15m 高値超え）。
    返り値の鍵は方向共通: env_ok（環境足が合図方向に継続）/ env_key（実質戻り高値 or 実質押し安値）/
    setup_ext（ふた山の高い方 or 2番底の安い方）/ trig_15m（割る安値 or 超える高値）/ exit_15m（手仕舞い水準）。
    """
    if side not in SIDES:
        raise ValueError(f"side は {SIDES} のいずれか: {side!r}")
    if side == "long":
        prev_m = dict(prev) if prev else prev
        if prev_m and prev_m.get("hard_sl") is not None:
            prev_m["hard_sl"] = -float(prev_m["hard_sl"])
        info = _evaluate(mirror(b15), mirror(b1), prev_m, now_utc, STATE_LONG)
        return None if info is None else _unmirror(info)
    return _evaluate(b15, b1, prev, now_utc, STATE_SHORT)


def _evaluate(b15: pd.DataFrame, b1: pd.DataFrame, prev: dict | None, now_utc: datetime | None, pos_state: str) -> dict | None:
    """ショート空間での判定。prev が建玉中なら建玉後の足で手仕舞い条件を、FLAT ならエントリー条件を見る。"""
    if b15 is None or b15.empty or b1 is None or b1.empty or len(b15) < 2 * LR + MW + 20:
        return None
    now = now_utc or datetime.now(timezone.utc)
    last = b15.index[-1]
    age_min = (pd.Timestamp(now.astimezone(timezone.utc)).tz_localize(None) - last).total_seconds() / 60.0
    if STALE_MINUTES > 0 and age_min > STALE_MINUTES:
        print(f"最新確定 15分足が {age_min:.0f}分前で古いため市場クローズ中と判断しスキップします。")
        return None
    source = b15.attrs.get("source")
    b1 = with_end(b1, ENV_INTERVAL)
    b4 = resample_4h(b1)
    if len(b4) < MIN_4H_BARS:
        return None
    b15 = with_end(b15, ENTRY_INTERVAL)
    ctx4 = h4_context(b4)
    s1 = h1_setup(b1, ctx4)
    i1 = len(b1) - 1
    k4 = int(s1["m41"][i1])
    st15 = swing_track(b15)
    i = len(b15) - 1
    c15 = b15["Close"].to_numpy()
    L15, H15 = st15["lastL"][i], st15["lastH"][i]
    down = bool(ctx4["down"][k4]) if k4 >= 0 else False

    def _f(arr, k):
        return None if (k < 0 or np.isnan(arr[k])) else float(arr[k])
    info = {
        "price": float(c15[i]),
        "time": last.tz_localize("UTC").tz_convert(TOKYO).strftime("%Y-%m-%d %H:%M JST") + " 確定15分足",
        "bar": last.strftime("%Y-%m-%dT%H:%M"),
        "env_ok": down,
        "env_key": _f(ctx4["key_high"], k4),
        "zone_lo": _f(ctx4["zone_lo"], k4),
        "zone_hi": _f(ctx4["zone_hi"], k4),
        "setup_valid": bool(s1["valid"][i1]),
        "setup_ext": _f(s1["top"], i1),
        "setup_inner": _f(s1["top_low"], i1),
        "setup_time": None if np.isnat(s1["top_time"][i1]) else pd.Timestamp(s1["top_time"][i1]).strftime("%Y-%m-%dT%H:%M"),
        "atr_1h": _f(s1["atr"], i1),
        "trig_15m": None if L15 is None else float(L15.price),
        "trig_15m_time": None if L15 is None else b15.index[L15.pos].strftime("%Y-%m-%dT%H:%M"),
        "exit_15m": None if H15 is None else float(H15.price),
        "source": source,
        "state": STATE_FLAT,
        "exit_reason": None,
    }
    prev_state = prev.get("state") if prev and prev.get("state") in STATES else None
    if prev_state in (STATE_SHORT, STATE_LONG):
        for k in ("entry_price", "entry_bar", "hard_sl", "setup_time_entry"):
            info[k] = prev.get(k)
        info["state"] = pos_state
        after = b15[b15.index > pd.Timestamp(prev["entry_bar"])] if prev.get("entry_bar") else b15.iloc[0:0]
        if not down:
            info["state"], info["exit_reason"] = STATE_FLAT, "env_end"
        elif prev.get("hard_sl") is not None and not after.empty and float(after["High"].max()) >= float(prev["hard_sl"]):
            info["state"], info["exit_reason"] = STATE_FLAT, "stop"
            info["stop_hit"] = float(after["High"].max())
        elif H15 is not None and c15[i] > H15.price:
            info["state"], info["exit_reason"] = STATE_FLAT, "exit_15m"
        return info
    # FLAT: エントリー判定
    if down and info["setup_valid"] and L15 is not None and L15.pos < i:
        t2 = pd.Timestamp(info["setup_time"])
        if b15.index[L15.pos] > t2 and c15[i] < L15.price:
            info["state"] = pos_state
            info["entry_price"] = float(c15[i])
            info["entry_bar"] = info["bar"]
            info["hard_sl"] = info["setup_ext"] + HARD_SL_ATR * (info["atr_1h"] or 0.0)
            info["setup_time_entry"] = info["setup_time"]
    return info


# ─────────────────────────────────────────────────────────────
# 通知文
# ─────────────────────────────────────────────────────────────
# 方向ごとの語彙。ロングはショートの鏡像なので文面だけ差し替える。
VOCAB = {
    "short": dict(
        name="ふた山ショート", pt="2番天井", env_ok="4h: 下降ダウ継続 ⬇️", env_key="実質戻り高値",
        env_key_end="4h 終値で超えたら下降終了", env_ng="4h: 下降ダウではない（売り場なし）",
        zone="直撃エリア（割った押し安値の帯）", setup="1h ふた山", ext="高い方", ext_inval="1h 終値で超えたら無効",
        trig="15m 安値", trig_act="15m 終値で割ったら売り", entry_emoji="🔻", entry_how="終値で下抜け",
        sl_how="2番天井 + 0.1×ATR", act="売り建てを検討", exit_lv="15m 直近高値", exit_how="終値超え",
        close_act="売り建ての買い戻しを検討", reenter="次の 15m 安値割れで再度合図", env_end="4h の下降ダウ終了",
        stop_px="高値", exit_15m_how="終値で上抜け", judge="4h 鏡像ダウ ＋ 1h ふた山 ＋ 15m 安値割れ",
    ),
    "long": dict(
        name="ふた山ロング", pt="2番底", env_ok="4h: 上昇ダウ継続 ⬆️", env_key="実質押し安値",
        env_key_end="4h 終値で割ったら上昇終了", env_ng="4h: 上昇ダウではない（買い場なし）",
        zone="直撃エリア（抜けた戻り高値の帯）", setup="1h ふた山（2番底）", ext="安い方", ext_inval="1h 終値で割ったら無効",
        trig="15m 高値", trig_act="15m 終値で超えたら買い", entry_emoji="🔺", entry_how="終値で上抜け",
        sl_how="2番底 − 0.1×ATR", act="買い建てを検討", exit_lv="15m 直近安値", exit_how="終値割れ",
        close_act="買い建ての決済を検討", reenter="次の 15m 高値超えで再度合図", env_end="4h の上昇ダウ終了",
        stop_px="安値", exit_15m_how="終値で下抜け", judge="4h ダウ ＋ 1h ふた山（2番底） ＋ 15m 高値超え",
    ),
}


def _ctx_lines(info: dict, d: int, v: dict) -> str:
    if info["env_ok"]:
        env = v["env_ok"]
        if info.get("env_key") is not None:
            env += f" / {v['env_key']} {info['env_key']:.{d}f}（{v['env_key_end']}）"
        if info.get("zone_lo") is not None:
            env += f"\n{v['zone']}: {info['zone_lo']:.{d}f}〜{info['zone_hi']:.{d}f}"
    else:
        env = v["env_ng"]
    if info["setup_valid"]:
        t = _jst(info["setup_time"])
        setup = f"{v['setup']}: 成立 ✅ {v['ext']} {info['setup_ext']:.{d}f}（{v['pt']} {t}）— {v['ext_inval']}"
    else:
        setup = f"{v['setup']}: 未成立"
    return f"{env}\n{setup}"


def _jst(iso: str | None) -> str:
    if not iso:
        return "-"
    return pd.Timestamp(iso).tz_localize("UTC").tz_convert(TOKYO).strftime("%m/%d %H:%M")


def format_message(info: dict, *, label: str, decimals: int = 3, first_run: bool = False, armed: bool = False,
                   side: str = "short") -> str:
    d = decimals
    v = VOCAB[side]
    ctx = _ctx_lines(info, d, v)
    tail = f"判定: {v['judge']} / {info['time']}"
    if first_run:
        return (f"🔔 {label} {v['name']}監視を開始しました\n現在: *{info['state']}*\n終値 {info['price']:.{d}f}\n{ctx}\n{tail}")
    if armed:
        trig = f"{info['trig_15m']:.{d}f}（{_jst(info.get('trig_15m_time'))}）" if info.get("trig_15m") is not None else "形成待ち"
        return (f"👀 {label} {v['name']}: ふた山が成立しました（エントリー待ち）\n終値 {info['price']:.{d}f}\n{ctx}\n"
                f"→ {v['pt']}より後にできた {v['trig']} {trig} を {v['trig_act']}\n{tail}")
    if info["state"] in (STATE_SHORT, STATE_LONG):
        return (f"{v['entry_emoji']} {label} {v['name']} エントリー（{v['trig']} {info['trig_15m']:.{d}f} を{v['entry_how']}）\n"
                f"終値 {info['price']:.{d}f} / 損切り目安 {info['hard_sl']:.{d}f}（{v['sl_how']}）\n{ctx}\n"
                f"→ {v['act']}。手仕舞いは {v['exit_lv']}（今 {info['exit_15m']:.{d}f}）の{v['exit_how']} / 損切り到達 / {v['env_end']}で通知\n{tail}")
    why = {"exit_15m": f"{v['exit_lv']} {info['exit_15m']:.{d}f} を{v['exit_15m_how']}",
           "env_end": v["env_end"],
           "stop": f"損切りライン {info.get('hard_sl', 0):.{d}f} に{v['stop_px']} {info.get('stop_hit', 0):.{d}f} が到達"}.get(
        info.get("exit_reason"), "条件終了")
    head = "🛑" if info.get("exit_reason") == "stop" else "⚪"
    return (f"{head} {label} {v['name']} 手仕舞い（{why}）\n終値 {info['price']:.{d}f}\n{ctx}\n"
            f"→ {v['close_act']}。ふた山が有効な間は{v['reenter']}\n{tail}")


# ─────────────────────────────────────────────────────────────
# 1ペア処理・main
# ─────────────────────────────────────────────────────────────
def process_pair(ticker: str, side: str = "short", b15: pd.DataFrame | None = None, b1: pd.DataFrame | None = None) -> str:
    """1 ペア 1 方向を処理。b15 / b1 を渡せば再取得しない（ショートとロングで同じ足を共用するため）。"""
    label = fx.pair_label(ticker)
    name = VOCAB[side]["name"]
    d = fx.pair_decimals(ticker)
    path = state_path(ticker, side)
    prev = fx.load_state(path)
    prev_state = prev.get("state") if prev and prev.get("state") in STATES else None

    if b15 is None:
        b15 = fetch_entry_bars(ticker)
    if b15 is None or b15.empty:
        print(f"[{label}] 15分足データ取得に失敗（空の応答）。")
        return "fetch_failed"
    if b1 is None:
        b1 = fetch_env_bars(ticker)
    if b1 is None or b1.empty:
        print(f"[{label}] 1時間足データ取得に失敗（空の応答）。")
        return "fetch_failed"
    info = evaluate(b15, b1, prev, side=side)
    if info is None:
        print(f"[{label}] {name}判定スキップ（本数不足/鮮度不良）。")
        return "no_data"
    info["armed_time"] = info["setup_time"] if info["setup_valid"] else None

    if prev_state is None:
        fx.notify(format_message(info, label=label, decimals=d, first_run=True, side=side))
        _save(path, info, side)
        print(f"[{label}] {name}監視 初回起動: 状態を {info['state']} で保存しました（{info['source']}）。")
        return "first_run"
    if prev_state != info["state"]:
        fx.notify(format_message(info, label=label, decimals=d, side=side))
        _save(path, info, side)
        print(f"[{label}] {name} 状態変化: {prev_state} → {info['state']}（{info.get('exit_reason') or 'entry'}・{info['source']}）")
        return "changed"
    # 状態は同じ。ふた山が新しく成立したら 1 回だけ知らせる
    if info["state"] == STATE_FLAT and info["setup_valid"] and info["armed_time"] != prev.get("armed_time"):
        fx.notify(format_message(info, label=label, decimals=d, armed=True, side=side))
        _save(path, info, side)
        print(f"[{label}] {name} ふた山成立を通知（{info['source']}）")
        return "armed"
    if info["armed_time"] != prev.get("armed_time"):
        _save(path, info, side)
    note = "・ふた山有効" if info["setup_valid"] else ""
    print(f"[{label}] {name} 状態変化なし: {info['state']}（通知スキップ{note}・{info['source']}）")
    return "nochange"


def _save(path: Path, info: dict, side: str = "short") -> None:
    keys = ("state", "price", "bar", "time", "env_ok", "env_key", "zone_lo", "zone_hi", "setup_valid",
            "setup_ext", "setup_time", "armed_time", "trig_15m", "exit_15m", "entry_price", "entry_bar", "hard_sl",
            "setup_time_entry", "exit_reason", "source")
    payload = {k: info.get(k) for k in keys}
    payload["strategy"] = f"mtf_{side}"
    payload["updated_at"] = datetime.now(TOKYO).strftime("%Y-%m-%d %H:%M:%S JST")
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(sides: tuple[str, ...] = ("short",)) -> int:
    """指定方向を処理。同じペアがショート・ロング両方にあれば足は 1 回だけ取得する（クレジット節約）。"""
    want: dict[str, list[str]] = {}
    for side in sides:
        for t in pairs(side):
            want.setdefault(t, []).append(side)
    for ticker, tsides in want.items():
        label = fx.pair_label(ticker)
        try:
            b15 = fetch_entry_bars(ticker)
            b1 = fetch_env_bars(ticker) if (b15 is not None and not b15.empty) else None
        except Exception as exc:  # noqa: BLE001
            print(f"[{label}] ふた山 データ取得中にエラー（スキップ）: {exc}", file=sys.stderr)
            continue
        for side in tsides:
            try:
                process_pair(ticker, side, b15, b1)
            except Exception as exc:  # noqa: BLE001 - 1ペアの失敗で全体を止めない
                print(f"[{label}] {VOCAB[side]['name']}処理中にエラー（スキップ）: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    args = set(sys.argv[1:])
    sides = ("short", "long") if "--long" in args and "--short" in args else ("long",) if "--long" in args else \
        ("short", "long") if "--both" in args else ("short",)
    sys.exit(main(sides))
