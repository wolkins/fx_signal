"""本の体系を任意の 3 時間軸（環境/トレード/エントリー）でロング・ショート両方向に検証する。
ロングは価格を上下反転（鏡像）してショート規則をそのまま適用する。

  python mtf_both.py                 # 既定の変種を全ペア
  python mtf_both.py USDJPY          # ペア指定
"""
import sys, numpy as np, pandas as pd
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fx_signal as fx, dow_guard as dg

CACHE = str(Path(__file__).resolve().parents[1] / ".cache"); PIP = 0.01
SPREAD = {"USDJPY": 0.3, "AUDJPY": 0.7, "NZDJPY": 1.0}
SWAP = {"USDJPY": 1.3, "AUDJPY": 0.8, "NZDJPY": 0.7}   # pips/日。ショートは支払い、ロングは受取（同額と仮定）
LR, MW = dg.PIVOT_LR, dg.MIN_WAVE
BARS_PER_DAY = {"5min": 288, "15min": 96, "1h": 24, "4h": 6}
_cache = {}


def load(code):
    if code not in _cache:
        df = pd.read_csv(f"{CACHE}/dukascopy_{code}_5m_2023-06-05_2026-06-04.csv", index_col=0, parse_dates=True).dropna(subset=["Close"])
        if df.index.tz is None: df.index = df.index.tz_localize("UTC")
        _cache[code] = df[["Open", "High", "Low", "Close"]]
    return _cache[code]


def mirror(df):
    return pd.DataFrame({"Open": -df["Open"], "High": -df["Low"], "Low": -df["High"], "Close": -df["Close"]}, index=df.index)


def rs(df5, rule):
    d = df5.tz_convert("UTC").tz_localize(None)
    if rule == "5min":
        out = d.copy()
    else:
        out = d.resample(rule).agg({"Open": "first", "High": "max", "Low": "min", "Close": "last"}).dropna(subset=["Close"])
    out = out[out.index.dayofweek != 6]
    out["end"] = out.index + pd.Timedelta(rule)
    return out


def swing_track(bars, lr=LR, mw=MW, keep_list=False):
    """各バー確定時点の採用済み山谷（leg ルール）。逐次更新で O(n)。"""
    piv = dg.find_pivots(bars, lr); n = len(bars); sw = []; pi = 0
    lastH = [None] * n; prevH = [None] * n; lastL = [None] * n; prevL = [None] * n; lists = []
    hs, ls = [], []
    def adopt(p):
        if sw and p.kind == sw[-1].kind:
            last = sw[-1]
            if (p.price > last.price) if p.kind == "H" else (p.price < last.price):
                sw[-1] = p
                (hs if p.kind == "H" else ls)[-1] = p
            return
        if sw and p.pos - sw[-1].pos < mw: return
        sw.append(p); (hs if p.kind == "H" else ls).append(p)
    for i in range(n):
        while pi < len(piv) and piv[pi].pos + lr <= i:
            adopt(piv[pi]); pi += 1
        if hs: lastH[i] = hs[-1]
        if len(hs) > 1: prevH[i] = hs[-2]
        if ls: lastL[i] = ls[-1]
        if len(ls) > 1: prevL[i] = ls[-2]
        if keep_list: lists.append(sw[-300:])
    return dict(lastH=lastH, prevH=prevH, lastL=lastL, prevL=prevL, lists=lists)


def short_dow(bars, st):
    c = bars["Close"].to_numpy(); n = len(c)
    down = np.zeros(n, bool); kh = np.full(n, np.nan); pend_low = None; key_high = None; d = False
    for i in range(n):
        L = st["lastL"][i]
        if L is not None and (pend_low is None or L.pos != pend_low.pos): pend_low = L
        if pend_low is not None and c[i] < pend_low.price and pend_low.pos < i:
            d = True; H = st["lastH"][i]; key_high = H.price if H is not None else key_high; pend_low = None
        elif d and key_high is not None and c[i] > key_high:
            d = False; key_high = None
        down[i] = d; kh[i] = key_high if key_high is not None else np.nan
    return down, kh


def run(code, direction="short", env="4h", trade="1h", entry="15m", *, need_zone=True, need_retrace=True,
        top_tol=1.5, reentry=False, session=None, lookback=24, label=""):
    """session: (start_h, end_h) JST。エントリーのみ制限（end_h <= start_h なら日跨ぎ）。"""
    df5 = load(code)
    if direction == "long": df5 = mirror(df5)
    bE = rs(df5, env); bT = rs(df5, trade); bX = rs(df5, entry)
    sp = SPREAD[code]; swap_bar = SWAP[code] / BARS_PER_DAY[entry] * (-1 if direction == "long" else 1)
    # ── 環境足 ──
    stE_full = swing_track(bE, keep_list=True); downE, khE = short_dow(bE, stE_full)
    oE, hE, lE, cE = (bE[x].to_numpy() for x in ["Open", "High", "Low", "Close"])
    nE = len(bE)
    zone_lo = np.full(nE, np.nan); zone_hi = np.full(nE, np.nan); ret_lim = np.full(nE, np.nan)
    lastL_time = np.full(nE, np.datetime64("NaT"), dtype="datetime64[ns]")
    for i in range(nE):
        if not downE[i]: continue
        sw = stE_full["lists"][i]
        ls_above = [s for s in sw if s.kind == "L" and s.price > cE[i]]
        if ls_above:
            z = min(ls_above, key=lambda s: s.price); j = z.pos
            zone_lo[i] = lE[j]; zone_hi[i] = min(oE[j], cE[j])
        L = stE_full["lastL"][i]
        if L is not None:
            lastL_time[i] = bE.index[L.pos].to_datetime64()
            hs_before = [s for s in sw if s.kind == "H" and s.pos < L.pos]
            if hs_before: ret_lim[i] = L.price + 0.8 * (hs_before[-1].price - L.price)
    # ── トレード足（ふた山）──
    stT = swing_track(bT); cT = bT["Close"].to_numpy(); hT = bT["High"].to_numpy()
    atrT = fx.compute_atr(bT[["High", "Low", "Close"]], 14).to_numpy()
    eE = bE["end"].to_numpy(); eT = bT["end"].to_numpy(); eX = bX["end"].to_numpy()
    mET = np.searchsorted(eE, eT, side="right") - 1
    nT = len(bT); dt_valid = np.zeros(nT, bool); dt_top = np.full(nT, np.nan); dt_time = np.full(nT, np.datetime64("NaT"), dtype="datetime64[ns]")
    for i in range(nT):
        k = mET[i]
        if k < 0 or not downE[k]: continue
        H2, H1 = stT["lastH"][i], stT["prevH"][i]
        if H2 is None or H1 is None: continue
        tE = lastL_time[k]
        if np.isnat(tE) or bT.index[H1.pos].to_datetime64() < tE: continue
        if abs(H2.price - H1.price) > top_tol * atrT[i]: continue
        top = max(H1.price, H2.price)
        if cT[H2.pos + LR:i + 1].max() > top: continue
        if need_zone:
            if np.isnan(zone_lo[k]): continue
            if not (top >= zone_lo[k] and min(H1.price, H2.price) <= zone_hi[k] + 1.0 * atrT[i]): continue
        if need_retrace and not np.isnan(ret_lim[k]) and top > ret_lim[k]: continue
        dt_valid[i] = True; dt_top[i] = top; dt_time[i] = bT.index[H2.pos].to_datetime64()
    # ── エントリー足 ──
    stX = swing_track(bX); o, h, c = (bX[x].to_numpy() for x in ["Open", "High", "Close"])
    mEX = np.searchsorted(eE, eX, side="right") - 1; mTX = np.searchsorted(eT, eX, side="right") - 1
    jst_h = ((bX.index + pd.Timedelta("9h")).hour).to_numpy()
    def in_session(i):
        if session is None: return True
        s, e = session
        return (s <= jst_h[i] < e) if s < e else (jst_h[i] >= s or jst_h[i] < e)
    n = len(bX); pos = False; trades = []; bars_in = 0; signal = None; exit_bar = -1; pending = None; entry_px = None
    for i in range(1, n):
        if signal == "enter": pos = True; entry_px = o[i]; entry_i = i; hard_sl = pending; signal = None
        elif signal == "exit": trades.append(dict(pips=-(o[i] - entry_px) / PIP - sp, t=bX.index[i], hold=i - entry_i)); pos = False; exit_bar = i; signal = None
        kE, kT = mEX[i], mTX[i]
        if kE < 0 or kT < 0: continue
        if pos:
            bars_in += 1
            if not downE[kE]: signal = "exit"; continue
            if hard_sl is not None and h[i] >= hard_sl:
                trades.append(dict(pips=-(hard_sl - entry_px) / PIP - sp, t=bX.index[i], hold=i - entry_i)); pos = False; exit_bar = i; continue
            HX = stX["lastH"][i]
            if HX is not None and c[i] > HX.price: signal = "exit"
            continue
        if not downE[kE]: continue
        LX = stX["lastL"][i]
        if LX is None or LX.pos >= i or not in_session(i): continue
        if reentry and exit_bar > 0 and not np.isnan(khE[kE]) and cT[kT] < khE[kE]:
            if LX.pos > exit_bar and c[i] < LX.price: signal = "enter"; pending = None; continue
        if dt_valid[kT] and bX.index[LX.pos].to_datetime64() > dt_time[kT] and c[i] < LX.price:
            signal = "enter"; pending = dt_top[kT] + 0.1 * atrT[kT]
    if pos: trades.append(dict(pips=-(c[-1] - entry_px) / PIP - sp, t=bX.index[-1], hold=n - entry_i))
    tr = pd.DataFrame(trades) if trades else pd.DataFrame(columns=["pips", "t", "hold"])
    total = tr["pips"].sum() - swap_bar * bars_in
    if len(tr):
        eq = tr["pips"].cumsum().to_numpy(); dd = float((np.maximum.accumulate(eq) - eq).max())
        yr = tr.groupby(tr["t"].dt.year)["pips"].sum(); yrs = " ".join(f"{int(y)}:{v:+.0f}" for y, v in yr.items())
        win = round(100 * (tr["pips"] > 0).mean()); pf = tr.loc[tr.pips > 0, "pips"].sum() / max(-tr.loc[tr.pips < 0, "pips"].sum(), 1e-9)
        hold_h = tr["hold"].mean() * 24 / BARS_PER_DAY[entry]
    else:
        dd = 0; yrs = "-"; win = 0; pf = 0; hold_h = 0
    print(f"  {label:<34} 損益 {total:>6.0f} DD {dd:>5.0f} 往復 {len(tr):>3} 勝率 {win:>3}% PF {pf:.2f} 平均保有 {hold_h:.1f}h | {yrs}", flush=True)
    return tr


if __name__ == "__main__":
    codes = sys.argv[1:] or ["USDJPY", "AUDJPY", "NZDJPY"]
    for code in codes:
        for d in ("short", "long"):
            print(f"\n■ {code} {d.upper()}  3年（スプレッド・swap 控除後、pips）")
            run(code, d, "4h", "1h", "15min", label="4h/1h/15m（現行ふた山の時間軸）")
            run(code, d, "1h", "15min", "5min", label="1h/15m/5m 本方針")
            run(code, d, "1h", "15min", "5min", session=(16, 24), label="1h/15m/5m 入口 16-24 JST")
            run(code, d, "1h", "15min", "5min", session=(15, 2), label="1h/15m/5m 入口 15-02 JST")
            run(code, d, "1h", "15min", "5min", need_zone=False, label="1h/15m/5m 直撃エリア無し")
            run(code, d, "1h", "15min", "5min", reentry=True, label="1h/15m/5m 本仕様（再エントリー有）")
