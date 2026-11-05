"""本番規則（5分足 SMA50/100+ATR帯 × 1h ダウ鏡像ゲート）に「エントリー時の乖離フィルタ」を足した検証。
  skip: クロス確定足で (SMA50 - Close) > k*ATR なら、そのエピソードは丸ごと見送り
  wait: 同条件なら、SHORT 状態が続く間に (SMA50 - Close) <= k*ATR に戻った足で入る
"""
import sys, numpy as np, pandas as pd
sys.path.insert(0, "/Users/fukuhara/work/fx-signal")
import fx_signal as fx, dow_guard as dg, short_signal as ss

CACHE = "/Users/fukuhara/work/fx-signal/.cache"
PIP = 0.01
SPREAD = {"USDJPY": 0.3, "AUDJPY": 0.7}
SWAP_SHORT = {"USDJPY": 1.3, "AUDJPY": 0.8}

def load(code):
    df = pd.read_csv(f"{CACHE}/dukascopy_{code}_5m_2023-06-05_2026-06-04.csv", index_col=0, parse_dates=True)
    df = df.dropna(subset=["Close"])
    if df.index.tz is None: df.index = df.index.tz_localize("UTC")
    return df

def rs(df5, rule):
    d = df5.tz_convert("UTC").tz_localize(None)
    out = d[["Open","High","Low","Close"]].resample(rule).agg({"Open":"first","High":"max","Low":"min","Close":"last"}).dropna(subset=["Close"])
    out = out[out.index.dayofweek != 6]
    out["end"] = out.index + pd.Timedelta(rule)
    return out

def env_gate(tb, eb):
    e_in = ss.env_downtrend_series(eb[["Open","High","Low","Close"]])
    known = pd.Series(e_in.to_numpy(), index=eb["end"].to_numpy())
    return pd.Series(known.reindex(tb["end"].to_numpy(), method="ffill").fillna(False).to_numpy(bool), index=tb.index)

def pnl(bars, in_mkt, spread, swap_per_bar):
    o, c = bars["Open"].to_numpy(), bars["Close"].to_numpy()
    im = in_mkt.shift(1).fillna(False).to_numpy(dtype=bool)
    p = np.zeros(len(c)); trades = []; pos = False; entry = None
    for i in range(1, len(c)):
        if im[i] != pos:
            if im[i]: pos, entry = True, o[i]
            else: trades.append(-(o[i]-entry)/PIP - spread); pos, entry = False, None
        if pos:
            p[i] = -(c[i] - (o[i] if im[i] != im[i-1] else c[i-1]))/PIP - swap_per_bar
    if pos: trades.append(-(c[-1]-entry)/PIP - spread)
    eq = np.cumsum(p); dd = float((np.maximum.accumulate(eq)-eq).max())
    yr = pd.Series(p, index=bars.index).groupby(bars.index.year).sum()
    wins = sum(1 for t in trades if t > 0)
    return dict(pnl=round(sum(trades)-swap_per_bar*im.sum()), dd=round(dd), n=len(trades), win=wins,
                avg=round(np.mean(trades),1) if trades else 0,
                yr=" ".join(f"{int(y)}:{v:+.0f}" for y, v in yr.items()))

def apply_filter(base, dist, thr, mode):
    """base: bool Series（本番の在場）。dist: SMA50-Close（>0 で価格が下に乖離）。thr: 許容幅の Series。"""
    b = base.to_numpy(bool); d = dist.to_numpy(); t = thr.to_numpy()
    out = np.zeros(len(b), bool); pos = False; blocked = False
    for i in range(len(b)):
        if not b[i]:
            pos = False; blocked = False; continue
        if pos:
            out[i] = True; continue
        # base が在場だがまだ入っていない
        ok = not (d[i] > t[i])
        if ok and not blocked:
            pos = True; out[i] = True
        elif mode == "skip" and not ok:
            blocked = True
        # wait モードは ok になった足で入る（blocked を立てない）
    return pd.Series(out, index=base.index)

def fmt(name, r):
    return f"  {name:<34} 損益 {r['pnl']:>6} DD {r['dd']:>5} 往復 {r['n']:>4} 勝率 {100*r['win']//max(r['n'],1):>3}% 平均 {r['avg']:>6} | {r['yr']}"

for code in ["USDJPY", "AUDJPY"]:
    df5 = load(code); tb = rs(df5, "5min"); hb = rs(df5, "1h")
    st = ss.short_states(tb[["Open","High","Low","Close"]])
    base = (st == ss.STATE_SHORT) & env_gate(tb, hb)
    c = tb["Close"]; s50 = c.rolling(50).mean(); atr = fx.compute_atr(tb[["High","Low","Close"]], 14)
    dist = s50 - c
    print(f"\n■ {code}  本番規則 + エントリー乖離フィルタ（3年、スプレッド {SPREAD[code]}・swap 控除後）")
    print(fmt("フィルタなし（本番）", pnl(tb, base, SPREAD[code], SWAP_SHORT[code]/288)))
    # エントリー時の乖離の分布
    ent = base & ~base.shift(1).fillna(False)
    ratio = (dist / atr)[ent]
    print(f"  エントリー時 (SMA50-Close)/ATR 分布: 中央値 {ratio.median():.1f} / 75% {ratio.quantile(.75):.1f} / 90% {ratio.quantile(.9):.1f} / 最大 {ratio.max():.1f}  (n={ent.sum()})")
    for mode in ["skip", "wait"]:
        for k in [1, 2, 3, 4, 6]:
            r = pnl(tb, apply_filter(base, dist, k*atr, mode), SPREAD[code], SWAP_SHORT[code]/288)
            print(fmt(f"{mode} k={k} ×ATR", r))
        for pips in [10, 20, 30, 40]:
            thr = pd.Series(pips*PIP, index=tb.index)
            r = pnl(tb, apply_filter(base, dist, thr, mode), SPREAD[code], SWAP_SHORT[code]/288)
            print(fmt(f"{mode} {pips}pips 固定", r))
