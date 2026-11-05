"""本番規則（5分足 SMA50/100+帯 × 1h ダウ鏡像）の入口は固定し、出口・時間帯だけ変えた検証。
  SL: 建値から k×ATR 逆行で損切り（高値ベース）
  TP: 建値から m×ATR 順行で利確（安値ベース）
  TS: 建玉後の最安値から k×ATR 戻したら手仕舞い（トレーリング）
  時間帯: エントリーを UTC の指定時間帯に限定（出口は本番のまま）
"""
import sys, numpy as np, pandas as pd
sys.path.insert(0, "/Users/fukuhara/work/fx-signal")
import fx_signal as fx, short_signal as ss

CACHE = "/Users/fukuhara/work/fx-signal/.cache"; PIP = 0.01
SPREAD = {"USDJPY": 0.3, "AUDJPY": 0.7}; SWAP = {"USDJPY": 1.3, "AUDJPY": 0.8}

def rs(df5, rule):
    d = df5.tz_convert("UTC").tz_localize(None)
    out = d[["Open","High","Low","Close"]].resample(rule).agg({"Open":"first","High":"max","Low":"min","Close":"last"}).dropna(subset=["Close"])
    out = out[out.index.dayofweek != 6]; out["end"] = out.index + pd.Timedelta(rule); return out

def base_series(code):
    df = pd.read_csv(f"{CACHE}/dukascopy_{code}_5m_2023-06-05_2026-06-04.csv", index_col=0, parse_dates=True).dropna(subset=["Close"])
    if df.index.tz is None: df.index = df.index.tz_localize("UTC")
    tb = rs(df, "5min"); hb = rs(df, "1h")
    st = ss.short_states(tb[["Open","High","Low","Close"]])
    e_in = ss.env_downtrend_series(hb[["Open","High","Low","Close"]])
    known = pd.Series(e_in.to_numpy(), index=hb["end"].to_numpy())
    gate = pd.Series(known.reindex(tb["end"].to_numpy(), method="ffill").fillna(False).to_numpy(bool), index=tb.index)
    return tb, ((st == ss.STATE_SHORT) & gate).to_numpy(bool)

def simulate(tb, base, spread, swap_bar, *, sl=None, tp=None, ts=None, hours=None):
    """base[i] は i 本目終値で確定 → i+1 Open で執行。SL/TP/TS は足の High/Low で判定し、その足で約定（保守的に水準で）。
    base が False になったら本番どおり次足 Open で手仕舞い。SL/TP/TS で出た後は base が一度 False→True になるまで再入場しない。"""
    o, h, l, c = (tb[x].to_numpy() for x in ["Open","High","Low","Close"])
    atr = fx.compute_atr(tb[["High","Low","Close"]], 14).to_numpy()
    hr = tb.index.hour.to_numpy()
    n = len(c); pos = False; entry = None; low = None; a0 = None; blocked = False
    trades = []; bars_in = 0; pnl_bar = np.zeros(n)
    for i in range(1, n):
        if pos:
            # 1) base 終了 → 今足 Open で手仕舞い
            if not base[i-1]:
                trades.append(-(o[i]-entry)/PIP - spread); pos = False; continue
            exit_px = None
            if sl is not None and h[i] >= entry + sl*a0: exit_px = entry + sl*a0
            if tp is not None and l[i] <= entry - tp*a0: exit_px = entry - tp*a0 if exit_px is None else exit_px
            if ts is not None:
                if h[i] >= low + ts*a0 and exit_px is None: exit_px = low + ts*a0
            if exit_px is not None:
                trades.append(-(exit_px-entry)/PIP - spread); pos = False; blocked = True
                bars_in += 1; continue
            low = min(low, l[i]); bars_in += 1
            pnl_bar[i] = -(c[i]-c[i-1])/PIP
        else:
            if not base[i-1]: blocked = False; continue
            if blocked: continue
            if hours is not None and hr[i-1] not in hours: continue
            pos = True; entry = o[i]; low = l[i]; a0 = atr[i-1] if not np.isnan(atr[i-1]) else 0.05
            bars_in += 1
    if pos: trades.append(-(c[-1]-entry)/PIP - spread)
    tr = np.array(trades) if trades else np.zeros(1)
    eq = np.cumsum(tr); dd = float((np.maximum.accumulate(eq)-eq).max())
    total = round(tr.sum() - swap_bar*bars_in)
    return dict(pnl=total, dd=round(dd), n=len(trades), win=round(100*(tr>0).mean()), avg=round(tr.mean(),1),
                pf=round(tr[tr>0].sum()/max(-tr[tr<0].sum(),1e-9),2))

def fmt(name, r): return f"  {name:<30} 損益 {r['pnl']:>6} DD(往復) {r['dd']:>5} 往復 {r['n']:>4} 勝率 {r['win']:>3}% 平均 {r['avg']:>6} PF {r['pf']}"

for code in ["USDJPY", "AUDJPY"]:
    tb, base = base_series(code); sp, sw = SPREAD[code], SWAP[code]/288
    print(f"\n■ {code}  入口固定・出口/時間帯の比較（スプレッド {sp}・swap 控除後、pips）")
    print(fmt("本番（SMA 再クロスのみ）", simulate(tb, base, sp, sw)))
    for k in [1, 2, 3, 5]: print(fmt(f"損切り SL {k}×ATR", simulate(tb, base, sp, sw, sl=k)))
    for m in [1, 2, 3, 5]: print(fmt(f"利確 TP {m}×ATR", simulate(tb, base, sp, sw, tp=m)))
    for k in [1, 2, 3, 5]: print(fmt(f"トレーリング TS {k}×ATR", simulate(tb, base, sp, sw, ts=k)))
    for k, m in [(2, 3), (3, 3), (3, 5), (2, 5)]: print(fmt(f"SL {k} + TP {m}", simulate(tb, base, sp, sw, sl=k, tp=m)))
    for name, hrs in [("東京 0-7 UTC", range(0, 8)), ("欧州 7-13 UTC", range(7, 14)), ("NY 13-21 UTC", range(13, 22)), ("欧州+NY 7-21", range(7, 22))]:
        print(fmt(f"入口 {name}", simulate(tb, base, sp, sw, hours=set(hrs))))
