"""mtf_short.py の判定関数（h4_context / h1_setup / swing_track）で 3 年を再生し、
検証（short_mtf.py「初回のみ」）と同じ損益になることを確認する。

  python analysis_scratch_20261104/short_mtf_replay.py            # USDJPY
  python analysis_scratch_20261104/short_mtf_replay.py AUDJPY NZDJPY
  python analysis_scratch_20261104/short_mtf_replay.py --long          # ロング（鏡像）。期待値 USDJPY +864
"""
import sys
from pathlib import Path
import numpy as np, pandas as pd
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mtf_short as ms

CACHE = Path(__file__).resolve().parents[1] / ".cache"
PIP = 0.01
SPREAD = {"USDJPY": 0.3, "AUDJPY": 0.7, "NZDJPY": 1.0}
SWAP = {"USDJPY": 1.3, "AUDJPY": 0.8, "NZDJPY": 0.7}


def rs(df5, rule):
    d = df5.tz_convert("UTC").tz_localize(None)
    out = d[["Open", "High", "Low", "Close"]].resample(rule).agg({"Open": "first", "High": "max", "Low": "min", "Close": "last"}).dropna(subset=["Close"])
    out = out[out.index.dayofweek != 6]
    out["end"] = out.index + pd.Timedelta(rule)
    return out


def replay(code, side="short"):
    df5 = pd.read_csv(CACHE / f"dukascopy_{code}_5m_2023-06-05_2026-06-04.csv", index_col=0, parse_dates=True).dropna(subset=["Close"])
    if df5.index.tz is None:
        df5.index = df5.index.tz_localize("UTC")
    b15, b1 = rs(df5, "15min"), rs(df5, "1h")
    if side == "long":   # 鏡像で判定。pips の式はそのまま（鏡像の売り損益 = 実際の買い損益）、スワップは受取
        b15, b1 = ms.mirror(b15), ms.mirror(b1)
        b15["end"], b1["end"] = b15.index + pd.Timedelta("15min"), b1.index + pd.Timedelta("1h")
    b4 = ms.resample_4h(b1)
    ctx4 = ms.h4_context(b4)
    s1 = ms.h1_setup(b1, ctx4)
    st15 = ms.swing_track(b15)
    o, h, c = (b15[x].to_numpy() for x in ("Open", "High", "Close"))
    m115 = np.searchsorted(s1["end"], b15["end"].to_numpy(), side="right") - 1
    n = len(b15); pos = False; signal = None; trades = []; bars_in = 0; sp, swap_bar = SPREAD[code], SWAP[code] / 96 * (-1 if side == "long" else 1)
    for i in range(1, n):
        if signal == "enter":
            pos, entry, entry_i, hard_sl, signal = True, o[i], i, pending, None
        elif signal == "exit":
            trades.append((b15.index[i], -(o[i] - entry) / PIP - sp)); pos = False; signal = None
        k1 = m115[i]
        if k1 < 0:
            continue
        k4 = s1["m41"][k1]
        if k4 < 0:
            continue
        down = ctx4["down"][k4]
        if pos:
            bars_in += 1
            if not down:
                signal = "exit"; continue
            if hard_sl is not None and h[i] >= hard_sl:
                trades.append((b15.index[i], -(hard_sl - entry) / PIP - sp)); pos = False; continue
            H15 = st15["lastH"][i]
            if H15 is not None and c[i] > H15.price:
                signal = "exit"
            continue
        if not down or not s1["valid"][k1]:
            continue
        L15 = st15["lastL"][i]
        if L15 is None or L15.pos >= i:
            continue
        if b15.index[L15.pos].to_datetime64() > s1["top_time"][k1] and c[i] < L15.price:
            signal = "enter"; pending = s1["top"][k1] + ms.HARD_SL_ATR * s1["atr"][k1]
    if pos:
        trades.append((b15.index[-1], -(c[-1] - entry) / PIP - sp))
    tr = pd.DataFrame(trades, columns=["t", "pips"])
    total = tr["pips"].sum() - swap_bar * bars_in
    eq = tr["pips"].cumsum().to_numpy(); dd = float((np.maximum.accumulate(eq) - eq).max()) if len(tr) else 0
    yr = " ".join(f"{int(y)}:{v:+.0f}" for y, v in tr.groupby(tr.t.dt.year)["pips"].sum().items())
    print(f"{code} {side}: 損益 {total:.0f} DD {dd:.0f} 往復 {len(tr)} 勝率 {100 * (tr.pips > 0).mean():.0f}% | {yr}")


_side = "long" if "--long" in sys.argv else "short"
for code in ([a for a in sys.argv[1:] if not a.startswith("--")] or ["USDJPY"]):
    replay(code, _side)
