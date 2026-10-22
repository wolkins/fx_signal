#!/usr/bin/env python3
"""ロング保護ガード（日足 SMA ＋ ヒステリシス帯）.

スワップ狙いで買い持ちしている JPY クロスについて、「持ち続ける(HOLD)」か
「一時的に降りる(EXIT)」かを **確定日足** だけで判定する付加モジュール。
ドテン(売り)は提案しない。ロング専用。

判定（決定論的・先読みなし）:
  - 確定日足の終値 C と SMA(GUARD_SMA_DAYS) を比較する。
  - HOLD 中に C < SMA × (1 − 帯%) となったら EXIT（離脱）。
  - EXIT 中に C > SMA × (1 + 帯%) となったら HOLD（復帰）。
  - 帯の内側では前回状態を維持する（日足レベルのだまし防止）。
  - 初期状態（前回なし）は C > SMA なら HOLD、そうでなければ EXIT。
  - 当日（進行中）の日足は使わない。日付が「今日(UTC)より前」のバーのみ確定とみなす。

較正（Dukascopy 5分足→日足 3年 2023-06〜2026-06 ＋ yfinance 日足 直近2年 2024-10〜2026-10、
翌日始値で執行、往復スプレッド控除、`python backtest.py --guard` で再現）:
  SMA50・帯2%: 3年で 買い持ち比の最大DD USD/JPY 2081→942, AUD/JPY 2228→1007, NZD/JPY 1842→793。
  損益は USD/AUD で買い持ち以上、NZD はやや劣る。往復は3年で 3〜5 回。
  帯1% は3年ではやや良い（AUD DD 513）が、直近2年の持ち合いで往復が 2〜3 倍に増えて
  全ペアで買い持ちに負けた（だまし）。両期間で崩れないのは帯2%。帯0%（単純クロス）は
  往復が 26〜35 回に増えて損益が悪化する。

本体 fx_signal.py は STRATEGY="long_guard" の時にこのモジュールを使う。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

# ─────────────────────────────────────────────────────────────
# 設定
# ─────────────────────────────────────────────────────────────
GUARD_SMA_DAYS = 50       # 日足 SMA の本数
GUARD_BAND_PCT = 2.0      # ヒステリシス帯（SMA に対する%）。0 で単純クロス
GUARD_INTERVAL = "1d"     # yfinance の足種
GUARD_PERIOD = "1y"       # 取得期間（SMA50 + 余裕。祝日欠損を考慮）
GUARD_STALE_DAYS = 5      # 最新確定足がこれより古ければデータ源異常/長期休場としてスキップ

STATE_HOLD = "HOLD"       # ロング維持
STATE_EXIT = "EXIT"       # ロング離脱（待機）
STATES = (STATE_HOLD, STATE_EXIT)


# ─────────────────────────────────────────────────────────────
# 確定日足の抽出
# ─────────────────────────────────────────────────────────────
def confirmed_daily_close(df: pd.DataFrame, now_utc: datetime | None = None) -> pd.Series:
    """日足 DataFrame から「確定済み」の終値列を返す（index は naive の日付）。

    yfinance の 1d は当日分を進行中バーとして含むため、日付が今日(UTC)より前の
    バーだけを採用する。tz-aware index は UTC に揃えてから日付化する。
    """
    if df is None or df.empty or "Close" not in df.columns:
        return pd.Series(dtype=float)
    close = df["Close"].dropna()
    if close.empty:
        return pd.Series(dtype=float)
    idx = pd.to_datetime(close.index)
    if idx.tz is not None:
        idx = idx.tz_convert("UTC").tz_localize(None)
    idx = idx.normalize()
    today = pd.Timestamp((now_utc or datetime.now(timezone.utc)).date())
    s = pd.Series(close.to_numpy(dtype=float), index=idx)
    s = s[~s.index.duplicated(keep="last")]
    return s[s.index < today]


# ─────────────────────────────────────────────────────────────
# 判定（本体と backtest が共用する唯一の規則）
# ─────────────────────────────────────────────────────────────
def next_state(close: float, sma: float, prev_state: str | None,
               band_pct: float | None = None) -> str:
    """1本ぶんの状態遷移。prev_state が None/不明なら単純比較で初期化する。

    band_pct を省略すると **呼び出し時点の** GUARD_BAND_PCT を使う（定義時固定にしない。
    本体 evaluate と backtest が常に同じ定数を見るため）。
    """
    if band_pct is None:
        band_pct = GUARD_BAND_PCT
    if prev_state == STATE_HOLD:
        return STATE_EXIT if close < sma * (1.0 - band_pct / 100.0) else STATE_HOLD
    if prev_state == STATE_EXIT:
        return STATE_HOLD if close > sma * (1.0 + band_pct / 100.0) else STATE_EXIT
    return STATE_HOLD if close > sma else STATE_EXIT


def guard_states(close: pd.Series, n: int | None = None,
                 band_pct: float | None = None) -> pd.Series:
    """確定日足終値列から各日の状態列を返す（SMA 未確定の先頭は NaN）。backtest 用。

    n / band_pct 省略時は呼び出し時点の GUARD_SMA_DAYS / GUARD_BAND_PCT を使う。
    """
    if n is None:
        n = GUARD_SMA_DAYS
    if band_pct is None:
        band_pct = GUARD_BAND_PCT
    sma = close.rolling(n).mean()
    out: list[str | None] = []
    prev: str | None = None
    for c, s in zip(close.to_numpy(dtype=float), sma.to_numpy(dtype=float)):
        if pd.isna(s):
            out.append(None)
            continue
        prev = next_state(float(c), float(s), prev, band_pct)
        out.append(prev)
    return pd.Series(out, index=close.index, dtype=object)


def normalize_prev(prev_state: str | None) -> str | None:
    """保存済み状態を正規化。旧戦略(LONG/SHORT)など未知の値は None（初回扱い）。"""
    return prev_state if prev_state in STATES else None


def evaluate(df: pd.DataFrame, prev_state: str | None = None,
             now_utc: datetime | None = None) -> dict | None:
    """日足 DataFrame から HOLD/EXIT を判定する。データ不足・鮮度不良なら None。"""
    close = confirmed_daily_close(df, now_utc)
    if len(close) < GUARD_SMA_DAYS:
        return None
    now = now_utc or datetime.now(timezone.utc)
    last_day = close.index[-1]
    age_days = (pd.Timestamp(now.date()) - last_day).days
    if GUARD_STALE_DAYS > 0 and age_days > GUARD_STALE_DAYS:
        print(f"最新確定日足が {age_days}日前で古いためスキップします（長期休場/データ源異常）。")
        return None

    sma = float(close.rolling(GUARD_SMA_DAYS).mean().iloc[-1])
    c = float(close.iloc[-1])
    state = next_state(c, sma, normalize_prev(prev_state))
    band_lo = sma * (1.0 - GUARD_BAND_PCT / 100.0)
    band_hi = sma * (1.0 + GUARD_BAND_PCT / 100.0)

    def _chg(bars: int) -> float | None:
        if len(close) <= bars or close.iloc[-1 - bars] == 0:
            return None
        return float((close.iloc[-1] - close.iloc[-1 - bars]) / close.iloc[-1 - bars] * 100.0)

    return {
        "state": state,
        "price": c,
        "sma": sma,
        "band_lo": band_lo,
        "band_hi": band_hi,
        "dist_pct": (c - sma) / sma * 100.0 if sma else None,
        "change_1d_pct": _chg(1),
        "change_5d_pct": _chg(5),
        # リスク層(assess_risk)が参照するキー。日足判定では短期変化率は意味が薄いので
        # 1日/5日の変化率を同じ器に入れて渡す（LLM入力の構造を変えないため）。
        "change_15m_pct": _chg(1),
        "change_1h_pct": _chg(5),
        "time": f"{last_day.strftime('%Y-%m-%d')} 確定日足",
    }


# ─────────────────────────────────────────────────────────────
# 通知文
# ─────────────────────────────────────────────────────────────
def format_message(info: dict, *, label: str, decimals: int = 3,
                   first_run: bool = False) -> str:
    """ロング保護ガードの通知本文（リスク層の前後装飾は本体側で付ける）。"""
    st = info["state"]
    sma_s = f"SMA{GUARD_SMA_DAYS}日 {info['sma']:.{decimals}f}"
    dist = info.get("dist_pct")
    dist_s = f"（SMA比 {dist:+.2f}%）" if dist is not None else ""
    if first_run:
        stance = "ロング維持 🟢" if st == STATE_HOLD else "離脱中（待機） 🛡️"
        return (
            f"🔔 {label} ロング保護の監視を開始しました\n"
            f"現在: *{st}*（{stance}）\n"
            f"日足終値: {info['price']:.{decimals}f} / {sma_s}{dist_s}\n"
            f"判定: 日足SMA{GUARD_SMA_DAYS}・帯±{GUARD_BAND_PCT:g}% / {info['time']}"
        )
    if st == STATE_EXIT:
        return (
            f"🛡️ {label} ロング離脱シグナル\n"
            f"日足終値 {info['price']:.{decimals}f} が {sma_s} の −{GUARD_BAND_PCT:g}% 帯"
            f"（{info['band_lo']:.{decimals}f}）を下抜け{dist_s}\n"
            f"→ ロングの縮小/決済を検討。復帰シグナルまで新規買いは見送り\n"
            f"{info['time']}"
        )
    return (
        f"🟢 {label} ロング復帰シグナル\n"
        f"日足終値 {info['price']:.{decimals}f} が {sma_s} の ＋{GUARD_BAND_PCT:g}% 帯"
        f"（{info['band_hi']:.{decimals}f}）を上抜け{dist_s}\n"
        f"→ ロングの再構築を検討（スワップ受け取り再開）\n"
        f"{info['time']}"
    )


def stance_label(state: str | None) -> str:
    """ハートビート等で使う短い表示。"""
    if state == STATE_HOLD:
        return "ロング維持🟢"
    if state == STATE_EXIT:
        return "離脱中🛡️"
    return "不明"
