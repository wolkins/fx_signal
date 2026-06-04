#!/usr/bin/env python3
"""ダウ理論ベースの「マルチタイムフレーム(MTF)コンフルエンス・ゲート」.

決定論的な SMA クロス判定（fx_signal.evaluate）には一切手を入れず、SMAの合図が出た
瞬間に「上位足のダウ・トレンドと一致した時だけ通す」フィルター層。

役割分担と絶対原則:
- 5m系列(既存)から 1H / 4H をリサンプルして作る。新データ源・APIキーは追加しない。
- すべて決定論的・OHLCのみ・バックテスト可能。先読み(lookahead)を絶対にしない。
- ゲートが失敗（データ不足・判定不能・例外）しても本体シグナルは止めない。
  失敗時は degrade open（素通し）＝従来どおり合図を出す。
  重要: RANGE(=判定した上で揉み合い→通さない) と 判定不能(UNKNOWN→素通し) を取り違えない。

trend の値: "UP" / "DOWN" / "RANGE" / "UNKNOWN"
  UP   … 高値切り上げ(HH) かつ 安値切り上げ(HL)
  DOWN … 高値切り下げ(LH) かつ 安値切り下げ(LL)
  RANGE… 確定スイングはあるが上記いずれでもない（揉み合い）
  UNKNOWN… 判定材料不足（スイング不足・バー不足）。ゲートは degrade open にする。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

# ─────────────────────────────────────────────────────────────
# パラメータ（Dukascopy較正前提のたたき台）
# ─────────────────────────────────────────────────────────────
DOW_GATE_ENABLED = True   # False でゲートを丸ごと無効化（完全に従来挙動）
SWING_LEFT = 2            # スイング確定: 左 left 本より高い/低い
SWING_RIGHT = 2           # スイング確定: 右 right 本より高い/低い（後続が要る＝先読みしない）
ENV_TF = "4h"            # 環境足
TRADE_TF = "1h"          # トレード足
RANGE_BLOCKS = True       # True=上位足RANGEは通さない（保守的）/ False=RANGEは許容

GATE_LOG_ENABLED = True
GATE_LOG_FILE = Path(__file__).with_name("gate_log.jsonl")
TOKYO = ZoneInfo("Asia/Tokyo")


# ─────────────────────────────────────────────────────────────
# スイング検出（決定論的・先読みなし）
# ─────────────────────────────────────────────────────────────
def find_swings(
    high: pd.Series, low: pd.Series, left: int = SWING_LEFT, right: int = SWING_RIGHT
) -> list[dict]:
    """スイング高値/安値を確定順で返す。

    左 left 本・右 right 本より厳密に高い(安い)点を確定スイングとする。確定には right 本の
    後続が必要なため、最新 right 本ぶんは未確定として採用しない（lookahead を一切しない）。
    返り値: [{"pos": int, "ts": Timestamp, "kind": "H"|"L", "price": float}, ...]
    """
    h = high.to_numpy()
    lo = low.to_numpy()
    idx = list(high.index)
    n = len(h)
    out: list[dict] = []
    for i in range(left, n - right):  # i+right<=n-1 を保証＝末尾 right 本は未確定
        hw = h[i - left : i + right + 1]
        lw = lo[i - left : i + right + 1]
        # 厳密な唯一の極値のみ採用（同値タイは不採用）
        if h[i] == hw.max() and (hw == h[i]).sum() == 1:
            out.append({"pos": i, "ts": idx[i], "kind": "H", "price": float(h[i])})
        elif lo[i] == lw.min() and (lw == lo[i]).sum() == 1:
            out.append({"pos": i, "ts": idx[i], "kind": "L", "price": float(lo[i])})
    return out


def dow_trend(swings: list[dict]) -> str:
    """確定スイング列から UP/DOWN/RANGE/UNKNOWN を返す。

    直近2つのスイング高値・安値を比較。材料不足(各2未満)は UNKNOWN（RANGEと区別）。
    """
    highs = [s for s in swings if s["kind"] == "H"]
    lows = [s for s in swings if s["kind"] == "L"]
    if len(highs) < 2 or len(lows) < 2:
        return "UNKNOWN"
    hh = highs[-1]["price"] > highs[-2]["price"]
    hl = lows[-1]["price"] > lows[-2]["price"]
    lh = highs[-1]["price"] < highs[-2]["price"]
    ll = lows[-1]["price"] < lows[-2]["price"]
    if hh and hl:
        return "UP"
    if lh and ll:
        return "DOWN"
    return "RANGE"


# ─────────────────────────────────────────────────────────────
# リサンプル（確定済みバーのみ）
# ─────────────────────────────────────────────────────────────
def _resample(df_5m: pd.DataFrame, tf: str) -> pd.DataFrame:
    """5m OHLC を上位足にリサンプル。進行中(未確定)の最終バーは落とす。"""
    agg = {"Open": "first", "High": "max", "Low": "min", "Close": "last"}
    cols = [c for c in agg if c in df_5m.columns]
    bars = df_5m[cols].resample(tf).agg({c: agg[c] for c in cols}).dropna()
    # 直近の足は進行中＝未確定なので捨てる（先読み防止）
    return bars.iloc[:-1] if len(bars) > 0 else bars


def mtf_trends(df_5m: pd.DataFrame) -> dict:
    """5m から 1H・4H のダウ・トレンドを返す。失敗/不足は UNKNOWN。

    返り値: {"4H": "...", "1H": "..."}
    """
    trends: dict[str, str] = {}
    for name, tf in (("4H", ENV_TF), ("1H", TRADE_TF)):
        try:
            bars = _resample(df_5m, tf)
            if len(bars) < SWING_LEFT + SWING_RIGHT + 1 or "High" not in bars.columns:
                trends[name] = "UNKNOWN"
                continue
            trends[name] = dow_trend(find_swings(bars["High"], bars["Low"]))
        except Exception:  # noqa: BLE001 - 上位足が作れない等は判定不能扱い
            trends[name] = "UNKNOWN"
    return trends


# ─────────────────────────────────────────────────────────────
# ゲート判定
# ─────────────────────────────────────────────────────────────
def gate(entry_state: str, trends: dict) -> dict:
    """SMAの合図を上位足トレンドと突き合わせ、通すか抑制するかを返す。

    返り値: {"pass": bool, "reason": str, "trend_4h": str, "trend_1h": str}
    reason: "ok" / "gate_disabled" / "degrade_open_unknown" /
            "blocked_4h_counter" / "blocked_1h_counter" /
            "blocked_4h_range" / "blocked_1h_range"
    """
    t4 = trends.get("4H", "UNKNOWN")
    t1 = trends.get("1H", "UNKNOWN")
    base = {"trend_4h": t4, "trend_1h": t1}

    if not DOW_GATE_ENABLED:
        return {**base, "pass": True, "reason": "gate_disabled"}
    # 判定不能は degrade open（素通し）。本体を止めない。
    if t4 == "UNKNOWN" or t1 == "UNKNOWN" or entry_state not in ("LONG", "SHORT"):
        return {**base, "pass": True, "reason": "degrade_open_unknown"}

    counter = "DOWN" if entry_state == "LONG" else "UP"
    for tf_name, t in (("4h", t4), ("1h", t1)):
        if t == counter:  # 逆方向は常に抑制
            return {**base, "pass": False, "reason": f"blocked_{tf_name}_counter"}
        if t == "RANGE" and RANGE_BLOCKS:  # RANGEは設定により抑制
            return {**base, "pass": False, "reason": f"blocked_{tf_name}_range"}
    # ここまで来たら 4H・1H とも順方向（または RANGE許容）→ 通す
    return {**base, "pass": True, "reason": "ok"}


# ─────────────────────────────────────────────────────────────
# 抑制ログ（事後検証用・機密は記録しない）
# ─────────────────────────────────────────────────────────────
def log_suppression(pair: str, entry_state: str, result: dict, price) -> None:
    if not GATE_LOG_ENABLED:
        return
    try:
        with GATE_LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "timestamp": datetime.now(TOKYO).strftime("%Y-%m-%d %H:%M:%S JST"),
                        "pair": pair,
                        "entry_state": entry_state,
                        "trend_4h": result.get("trend_4h"),
                        "trend_1h": result.get("trend_1h"),
                        "reason": result.get("reason"),
                        "price": price,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    except OSError:
        pass


# ─────────────────────────────────────────────────────────────
# バックテスト用: 各上位足バー時点のトレンド列（先読みなし）
# ─────────────────────────────────────────────────────────────
def trend_series(df_5m: pd.DataFrame, tf: str) -> pd.Series:
    """各確定バー時点での dow_trend を Series で返す（asof 参照用）。

    スイングは pos+SWING_RIGHT 本目で確定する。確定済みスイングだけを使うので、
    後続バーを足しても過去の値は変わらない（lookahead しない）。
    """
    bars = _resample(df_5m, tf)
    swings = sorted(find_swings(bars["High"], bars["Low"]), key=lambda s: s["pos"])
    res: dict = {}
    confirmed: list[dict] = []
    si = 0
    for bi, ts in enumerate(bars.index):
        while si < len(swings) and swings[si]["pos"] + SWING_RIGHT <= bi:
            confirmed.append(swings[si])
            si += 1
        res[ts] = dow_trend(confirmed)
    s = pd.Series(res)
    # 重要(先読み防止): resample の index はバー「開始」時刻。そのバーの情報が利用可能に
    # なるのはバー「終了」時刻なので、index を +tf ずらす。これにより asof(t) は
    # 「t時点までに確定した上位足」だけを参照し、進行中バーを先取りしない。
    if len(s) > 0:
        s.index = s.index + pd.Timedelta(tf)
    return s
