#!/usr/bin/env python3
"""日足ダウ理論（実質押し安値・戻り高値・6本のタメ）の規則をロング保護向けに機械化し、損益で検証する（読み取り専用）。

本の規則 → 実装の対応（ユーザー提供の要約に基づく）:
  - 波（山/谷）: 「山全体で6本程度のタメ」→ ピボット(左右 PIVOT 本)で山谷を取り、
    直前の山谷から MIN_WAVE 本未満で反転した小さな波は無視（＝大きく数える）。同種が連続したら極値側を採用。
  - ゾーン抜け: 終値がゾーン外側の端（高値側はヒゲ先 High、安値側はヒゲ先 Low）を抜けて確定した時点。
  - 実質押し安値: 「直近高値を終値で超えて確定した」時にだけ、その高値を付けさせた起点の安値
    （直近の確定した谷）へ切り上げる。高値更新前の押し目の安値では更新しない。
  - ダウ崩れ(EXIT): 終値 < 実質押し安値（ヒゲ先）。実質戻り高値 = 安値を割らせた起点の高値。
  - 復帰(UP再開): 崩れた「後」に形成された一番近い小山の高値を終値で超える。
    （旧い大きな高値ではない）。オプション: ふた山（崩れ後に谷が2つ確定）を要求。
  - 環境足: 同じ機械を環境足に当て、環境足が DOWN の間は在場しない（上位足が絶対優先）。
  - 調整期間: 崩れない限り保有（在場維持）。分割決済は通知文の目安のみ（損益検証では全決済）。

使い方（リポジトリ root で）:
    .venv/bin/python analysis_scratch_20261023/dow_book.py            # 比較表
    .venv/bin/python analysis_scratch_20261023/dow_book.py detail AUDJPY 1D 4h   # 切替履歴
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).parent))
from analyze_pnl import load  # noqa: E402  Dukascopy 5m キャッシュ読み込み

PIP = 0.01
SPREAD = 0.5  # 往復 pips（JPYクロス）
PAIRS = ["USDJPY", "AUDJPY", "NZDJPY"]
# 参考値（python backtest.py --guard --source dukascopy 3y 5m, 帯2%）
REF = {
    "USDJPY": ("買い持ち +1705 / DD 2081", "SMA50帯2% +1864 / DD 942 / 往復5"),
    "AUDJPY": ("買い持ち +1955 / DD 2228", "SMA50帯2% +2385 / DD 1007 / 往復3"),
    "NZDJPY": ("買い持ち +612 / DD 1842", "SMA50帯2% +454 / DD 793 / 往復5"),
}


def resample(df5: pd.DataFrame, tf: str) -> pd.DataFrame:
    agg = {"Open": "first", "High": "max", "Low": "min", "Close": "last"}
    bars = df5[list(agg)].resample(tf, label="left", closed="left").agg(agg).dropna()
    bars["end"] = bars.index + pd.Timedelta(tf) if tf not in ("1D", "1W") else (
        bars.index + (pd.Timedelta(days=1) if tf == "1D" else pd.Timedelta(days=7)))
    return bars


@dataclass
class Pivot:
    pos: int
    kind: str      # "H" / "L"
    price: float   # ヒゲ先（ゾーン外側の端）
    body: float    # 実体側の端


def pivots(bars: pd.DataFrame, lr: int) -> list[Pivot]:
    """左右 lr 本より厳密に高い/安い点（同値タイは不採用）。確定は pos+lr 本目。"""
    h, lo = bars["High"].to_numpy(), bars["Low"].to_numpy()
    o, c = bars["Open"].to_numpy(), bars["Close"].to_numpy()
    out: list[Pivot] = []
    n = len(h)
    for i in range(lr, n - lr):
        hw, lw = h[i - lr:i + lr + 1], lo[i - lr:i + lr + 1]
        if h[i] == hw.max() and (hw == h[i]).sum() == 1:
            out.append(Pivot(i, "H", float(h[i]), float(max(o[i], c[i]))))
        elif lo[i] == lw.min() and (lw == lo[i]).sum() == 1:
            out.append(Pivot(i, "L", float(lo[i]), float(min(o[i], c[i]))))
    return out


class DowMachine:
    """1つの時間足に対する、ダウ理論のロング向け状態機械（先読みなし）。"""

    def __init__(self, bars: pd.DataFrame, lr: int = 3, min_wave: int = 6, double_bottom: bool = False):
        self.bars = bars
        self.lr = lr
        self.min_wave = min_wave
        self.double_bottom = double_bottom
        self.piv = sorted(pivots(bars, lr), key=lambda p: p.pos)

    def run(self) -> tuple[pd.Series, list[tuple]]:
        close = self.bars["Close"].to_numpy()
        n = len(close)
        sw: list[Pivot] = []          # 採用した山谷（交互）
        trend = None                  # None / "UP" / "DOWN"
        key_low = key_high = None     # 実質押し安値 / 実質戻り高値（Pivot）
        pending_high = pending_low = None  # まだ終値で抜かれていない直近の小山/小谷
        break_pos = -1                # 直近の UP→DOWN 崩れの位置
        lows_since_break = 0
        pi = 0
        states = np.empty(n, dtype=object)
        events: list[tuple] = []

        def add_pivot(p: Pivot) -> bool:
            """交互性と「大きく数える」(min_wave) を適用して採用可否を返す。"""
            nonlocal sw
            if sw and sw[-1].kind == p.kind:
                more_extreme = p.price > sw[-1].price if p.kind == "H" else p.price < sw[-1].price
                if more_extreme:
                    sw[-1] = p
                    return True
                return False
            if sw and (p.pos - sw[-1].pos) < self.min_wave:
                return False  # 小さな波は数えない
            sw.append(p)
            return True

        for i in range(n):
            # i 本目で確定した山谷を取り込む
            while pi < len(self.piv) and self.piv[pi].pos + self.lr <= i:
                p = self.piv[pi]
                pi += 1
                if not add_pivot(p):
                    continue
                if p.kind == "H":
                    if p.pos > break_pos:          # 崩れた「後」にできた小山だけが復帰の基準
                        pending_high = p
                else:
                    pending_low = p
                    if trend == "DOWN" and p.pos > break_pos:
                        lows_since_break += 1

            c = close[i]
            last_low = next((s for s in reversed(sw) if s.kind == "L"), None)
            last_high = next((s for s in reversed(sw) if s.kind == "H"), None)

            # 高値超え確定（ゾーン外側＝ヒゲ先を終値で抜く）
            if pending_high is not None and c > pending_high.price:
                if trend != "UP":
                    if self.double_bottom and trend == "DOWN" and lows_since_break < 2:
                        pending_high = None       # ふた山未形成 → 次の小山まで待つ
                    else:
                        trend = "UP"
                        events.append((i, "UP", c))
                if trend == "UP":
                    if last_low is not None:
                        key_low = last_low          # 高値を更新させた起点の安値 → 実質押し安値
                    pending_high = None
            # ダウ崩れ（実質押し安値をヒゲ先で終値割れ）
            elif trend == "UP" and key_low is not None and c < key_low.price:
                trend = "DOWN"
                key_high = last_high                # 安値を割らせた起点の高値 → 実質戻り高値
                break_pos = i
                lows_since_break = 0
                pending_high = None
                events.append((i, "DOWN", c))
            # 下降継続（参考: 実質戻り高値の切り下げ）
            elif trend == "DOWN" and pending_low is not None and c < pending_low.price:
                key_high = last_high
                pending_low = None
            # 初期化: 最初の明確な安値割れ
            elif trend is None and pending_low is not None and c < pending_low.price:
                trend = "DOWN"
                key_high = last_high
                break_pos = i
                pending_low = None
                events.append((i, "DOWN", c))

            states[i] = trend
        return pd.Series(states, index=self.bars.index), events


def long_only_pnl(bars: pd.DataFrame, in_market: pd.Series):
    """in_market[i]（i 本目の終値で確定）を i+1 本目の Open で執行するロング専用損益。"""
    o, c = bars["Open"].to_numpy(), bars["Close"].to_numpy()
    im = in_market.shift(1).fillna(False).to_numpy(dtype=bool)
    trades: list[float] = []
    pnl = np.zeros(len(c))
    pos, entry = False, None
    for i in range(1, len(c)):
        if im[i] != pos:
            if im[i]:
                pos, entry = True, o[i]
            else:
                trades.append((o[i] - entry) / PIP - SPREAD)
                pos, entry = False, None
        if pos:
            pnl[i] = (c[i] - (o[i] if im[i] != im[i - 1] else c[i - 1])) / PIP
    if pos:
        trades.append((c[-1] - entry) / PIP - SPREAD)
    eq = np.cumsum(pnl)
    dd = float((np.maximum.accumulate(eq) - eq).max())
    yr = pd.Series(pnl, index=bars.index).groupby(bars.index.year).sum()
    return sum(trades), dd, len(trades), float(im.mean()), yr


def evaluate(df5, env_tf, trade_tf, lr=3, min_wave=6, double_bottom=False):
    tb = resample(df5, trade_tf)
    t_state, t_events = DowMachine(tb, lr, min_wave, double_bottom).run()
    in_mkt = t_state == "UP"
    if env_tf:
        eb = resample(df5, env_tf)
        e_state, _ = DowMachine(eb, lr, min_wave, False).run()
        # 環境足の状態は、そのバーが「終了」した時刻以降にだけ参照できる（先読み防止）
        e_known = pd.Series(e_state.to_numpy(), index=eb["end"].to_numpy())
        e_at_trade = e_known.reindex(tb["end"].to_numpy(), method="ffill")
        env_block = pd.Series(e_at_trade.to_numpy() == "DOWN", index=tb.index)
        in_mkt = in_mkt & ~env_block
    # 判定可能になった最初の時点以降で評価（それ以前は買い持ちと同条件にならないので除外）
    first = t_state.first_valid_index()
    if first is None:
        return None
    tb2, im2 = tb.loc[first:], in_mkt.loc[first:]
    res = long_only_pnl(tb2, im2)
    bh = long_only_pnl(tb2, pd.Series(True, index=tb2.index))
    return res, bh, t_events, tb


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "detail":
        code, env_tf, trade_tf = sys.argv[2], (None if sys.argv[3] in ("none", "None") else sys.argv[3]), sys.argv[4]
        df5 = load(code)
        res, bh, events, tb = evaluate(df5, env_tf, trade_tf)
        print(f"■ {code} env={env_tf} trade={trade_tf}: 損益 {res[0]:.0f} DD {res[1]:.0f} 往復 {res[2]} 在場 {100*res[3]:.0f}%")
        for i, st, c in events:
            print(f"   {tb.index[i]}  → {st:<4} 終値 {c:.3f}")
        return

    combos = [
        ("4h", "15min", "本のデイトレ構成 4H/15m"),
        ("4h", "1h", "4H/1H"),
        ("1D", "1h", "本のスイング構成 日足/1H"),
        ("1D", "4h", "日足/4H"),
        ("1W", "1D", "本の長期構成 週足/日足"),
        (None, "4h", "環境足なし 4H"),
        (None, "1D", "環境足なし 日足"),
    ]
    print("列: 構成 | 損益pips 最大DD 往復 在場% | 年次(2023/24/25/26)   ※同一期間の買い持ちは各行の右端")
    for code in PAIRS:
        df5 = load(code)
        print(f"\n■ {code}   参考: {REF[code][0]} / {REF[code][1]}")
        for env_tf, trade_tf, name in combos:
            for db in (False, True):
                out = evaluate(df5, env_tf, trade_tf, lr=3, min_wave=6, double_bottom=db)
                if out is None:
                    continue
                res, bh, _, _ = out
                yr = "/".join(f"{v:.0f}" for v in res[4])
                tag = name + (" +ふた山" if db else "")
                print(f"  {tag:<28} | {res[0]:>6.0f} {res[1]:>5.0f} {res[2]:>4} {100*res[3]:>4.0f}% | {yr:<24} | B&H {bh[0]:>6.0f} DD {bh[1]:>5.0f}")
        # ピボット幅の感度（日足/4H のみ）
        for lr, mw in ((2, 0), (6, 6), (3, 12)):
            res, bh, _, _ = evaluate(df5, "1D", "4h", lr=lr, min_wave=mw)
            print(f"  {'日足/4H 感度 pivot'+str(lr)+' wave'+str(mw):<28} | {res[0]:>6.0f} {res[1]:>5.0f} {res[2]:>4} {100*res[3]:>4.0f}% |")


if __name__ == "__main__":
    main()
