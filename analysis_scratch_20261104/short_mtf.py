"""本のショート体系（ロングの鏡像）のマルチタイムフレーム版を 3 年の Dukascopy 5 分足で検証する。

  4h : 下降ダウ継続（安値更新後、実質戻り高値を終値で超えていない）
       ＋ 戻り売り直撃エリア（割った過去の押し安値の実体〜ヒゲ帯）に戻している
       ＋ 直近下落波の戻しが 80% 未満
  1h : ふた山（2番天井）。採用済みの山 2 つが 4h 直近安値より後にでき、価格差 ≤ TOP_TOL×ATR(1h)、
       直撃エリアに届いている（オーバーシュート許容）。2 山の高い方を 1h 終値で超えたら無効
  15m: 直近の採用済み安値（2番天井より後にできたもの）を終値で割ったらショート（翌足始値）
       手仕舞い: 直近の採用済み高値を終値で超えたら全決済（翌足始値）。安全策で 2番天井高値 + 0.1ATR を High で抜けたら決済
       再エントリー: 4h 下降継続中、手仕舞い後に新たにできた 15m 安値を割ったら復帰
  共通: 4h 下降終了で全決済

山谷の採用は dow_guard と同じ（左右 PIVOT_LR 本で確定、"leg" ルールで直前の山谷から MIN_WAVE 本以上）。
"""
import sys, numpy as np, pandas as pd
sys.path.insert(0, "/Users/fukuhara/work/fx-signal")
import fx_signal as fx, dow_guard as dg

CACHE = "/Users/fukuhara/work/fx-signal/.cache"; PIP = 0.01
SPREAD = {"USDJPY": 0.3, "AUDJPY": 0.7, "NZDJPY": 1.0}
SWAP = {"USDJPY": 1.3, "AUDJPY": 0.8, "NZDJPY": 0.7}
LR, MW = dg.PIVOT_LR, dg.MIN_WAVE


def load(code):
    df = pd.read_csv(f"{CACHE}/dukascopy_{code}_5m_2023-06-05_2026-06-04.csv", index_col=0, parse_dates=True).dropna(subset=["Close"])
    if df.index.tz is None: df.index = df.index.tz_localize("UTC")
    return df


def rs(df5, rule):
    d = df5.tz_convert("UTC").tz_localize(None)
    out = d[["Open","High","Low","Close"]].resample(rule).agg({"Open":"first","High":"max","Low":"min","Close":"last"}).dropna(subset=["Close"])
    out = out[out.index.dayofweek != 6]
    out["end"] = out.index + pd.Timedelta(rule)
    return out


def swing_track(bars, lr=LR, mw=MW, keep_list=False):
    """各バー確定時点の採用済み山谷（leg ルール）。返り値: 配列 lastH/prevH/lastL/prevL（Pivot or None）、必要なら sw のコピー。"""
    piv = dg.find_pivots(bars, lr); n = len(bars); sw = []; pi = 0
    lastH, prevH, lastL, prevL, lists = [], [], [], [], []
    def adopt(p):
        if not sw: sw.append(p); return
        last = sw[-1]
        if p.kind == last.kind:
            if (p.price > last.price) if p.kind == "H" else (p.price < last.price): sw[-1] = p
            return
        if p.pos - last.pos < mw: return
        sw.append(p)
    for i in range(n):
        while pi < len(piv) and piv[pi].pos + lr <= i:
            adopt(piv[pi]); pi += 1
        hs = [s for s in sw if s.kind == "H"]; ls = [s for s in sw if s.kind == "L"]
        lastH.append(hs[-1] if hs else None); prevH.append(hs[-2] if len(hs) > 1 else None)
        lastL.append(ls[-1] if ls else None); prevL.append(ls[-2] if len(ls) > 1 else None)
        if keep_list: lists.append(list(sw))
    return dict(lastH=lastH, prevH=prevH, lastL=lastL, prevL=prevL, lists=lists)


def short_dow(bars, lr=LR, mw=MW):
    """鏡像ダウ（簡略版）: 採用済み安値を終値で割ったら DOWN、その時点の直近高値が実質戻り高値。終値で超えたら終了。
    返り値: down[], key_high[](価格 or nan), 直近安値更新バー pos"""
    st = swing_track(bars, lr, mw); c = bars["Close"].to_numpy(); n = len(c)
    down = np.zeros(n, bool); kh = np.full(n, np.nan); pend_low = None; key_high = None; d = False
    last_break = np.full(n, -1); lb = -1
    for i in range(n):
        L = st["lastL"][i]
        if L is not None and (pend_low is None or L.pos != pend_low.pos): pend_low = L
        if pend_low is not None and c[i] < pend_low.price and pend_low.pos < i:
            if not d: d = True
            H = st["lastH"][i]; key_high = H.price if H is not None else key_high
            pend_low = None; lb = i
        elif d and key_high is not None and c[i] > key_high:
            d = False; key_high = None
        down[i] = d; kh[i] = key_high if key_high is not None else np.nan; last_break[i] = lb
    return down, kh, last_break, st


def run(code, *, need_double_top=True, need_zone=True, need_retrace=True, top_tol=1.5, verbose=True, reentry=True, exit_tf="15m", label=""):
    df5 = load(code); b15 = rs(df5, "15min"); b1 = rs(df5, "1h"); b4 = rs(df5, "4h")
    sp, swap_bar = SPREAD[code], SWAP[code] / 96
    # ── 4h ──
    down4, kh4, lb4, st4 = short_dow(b4)
    o4, h4, l4, c4 = (b4[x].to_numpy() for x in ["Open","High","Low","Close"])
    atr4 = fx.compute_atr(b4[["High","Low","Close"]], 14).to_numpy()
    st4l = swing_track(b4, keep_list=True)["lists"]
    zone_lo = np.full(len(b4), np.nan); zone_hi = np.full(len(b4), np.nan); ret_lim = np.full(len(b4), np.nan); lastL4_time = np.full(len(b4), np.datetime64("NaT"), dtype="datetime64[ns]")
    for i in range(len(b4)):
        if not down4[i]: continue
        sw = st4l[i]
        ls_above = [s for s in sw if s.kind == "L" and s.price > c4[i]]
        if ls_above:
            z = min(ls_above, key=lambda s: s.price); j = z.pos
            zone_lo[i] = l4[j]; zone_hi[i] = min(o4[j], c4[j])
        L = st4["lastL"][i]
        if L is not None:
            lastL4_time[i] = b4.index[L.pos].to_datetime64()
            hs_before = [s for s in sw if s.kind == "H" and s.pos < L.pos]
            if hs_before:
                H = hs_before[-1]; ret_lim[i] = L.price + 0.8 * (H.price - L.price)
    # ── 1h ──
    st1 = swing_track(b1); c1 = b1["Close"].to_numpy(); h1 = b1["High"].to_numpy()
    atr1 = fx.compute_atr(b1[["High","Low","Close"]], 14).to_numpy()
    e4 = b4["end"].to_numpy(); e1 = b1["end"].to_numpy(); e15 = b15["end"].to_numpy()
    # 1h 各バー → 直前に確定した 4h バー
    m41 = np.searchsorted(e4, e1, side="right") - 1
    dt_valid = np.zeros(len(b1), bool); dt_top = np.full(len(b1), np.nan); dt_time = np.full(len(b1), np.datetime64("NaT"), dtype="datetime64[ns]")
    for i in range(len(b1)):
        k = m41[i]
        if k < 0 or not down4[k]: continue
        H2, H1 = st1["lastH"][i], st1["prevH"][i]
        if H2 is None or H1 is None: continue
        if not need_double_top:
            top = H2.price
            if c1[i] > top: continue
            ok = True
            if need_zone and not np.isnan(zone_lo[k]): ok = h1[max(0, i-24):i+1].max() >= zone_lo[k] and top <= zone_hi[k] + 1.0 * atr1[i]
            elif need_zone: ok = False
            if need_retrace and not np.isnan(ret_lim[k]) and h1[max(0, i-24):i+1].max() > ret_lim[k]: ok = False
            if ok: dt_valid[i] = True; dt_top[i] = top; dt_time[i] = b1.index[H2.pos].to_datetime64()
            continue
        t4 = lastL4_time[k]
        if np.isnat(t4) or b1.index[H1.pos].to_datetime64() < t4: continue      # 2 山とも 4h 直近安値より後
        if abs(H2.price - H1.price) > top_tol * atr1[i]: continue
        top = max(H1.price, H2.price)
        if c1[H2.pos + LR:i + 1].max() > top: continue                            # 2 番天井確定後に高い方を終値で超えたら無効
        if need_zone:
            if np.isnan(zone_lo[k]): continue
            if not (top >= zone_lo[k] and min(H1.price, H2.price) <= zone_hi[k] + 1.0 * atr1[i]): continue
        if need_retrace and not np.isnan(ret_lim[k]) and top > ret_lim[k]: continue
        dt_valid[i] = True; dt_top[i] = top; dt_time[i] = b1.index[H2.pos].to_datetime64()
    # ── 15m ──
    st15 = swing_track(b15); o, h, l, c = (b15[x].to_numpy() for x in ["Open","High","Low","Close"])
    m415 = np.searchsorted(e4, e15, side="right") - 1; m115 = np.searchsorted(e1, e15, side="right") - 1
    n = len(b15); pos = False; entry = None; trades = []; bars_in = 0; signal = None  # signal: 次足始値で執行する指示
    exit_bar = -1; armed_after = None; pending = None
    for i in range(1, n):
        # 前足で出た指示を今足始値で執行
        if signal == "enter": pos = True; entry = o[i]; entry_i = i; hard_sl = pending; signal = None
        elif signal == "exit": trades.append(dict(pips=-(o[i]-entry)/PIP - sp, t=b15.index[i], hold=i-entry_i)); pos = False; exit_bar = i; signal = None
        k4, k1 = m415[i], m115[i]
        if k4 < 0 or k1 < 0: continue
        if pos:
            bars_in += 1
            H15 = st15["lastH"][i]
            if not down4[k4]: signal = "exit"; continue
            if hard_sl is not None and h[i] >= hard_sl:
                trades.append(dict(pips=-(hard_sl-entry)/PIP - sp, t=b15.index[i], hold=i-entry_i)); pos = False; exit_bar = i; continue
            if exit_tf == "1h":
                H1x = st1["lastH"][k1]
                if H1x is not None and c1[k1] > H1x.price: signal = "exit"
            elif H15 is not None and c[i] > H15.price: signal = "exit"
            continue
        # ノーポジ: 初回エントリー or 再エントリー
        if not down4[k4]: armed_after = None; continue
        L15 = st15["lastL"][i]
        if L15 is None: continue
        if reentry and exit_bar > 0 and down4[k4] and (not np.isnan(kh4[k4])) and c1[k1] < kh4[k4]:
            # 再エントリー: 手仕舞い後にできた 15m 安値を割る
            if L15.pos > exit_bar and c[i] < L15.price and L15.pos < i:
                signal = "enter"; pending = None; continue
        if dt_valid[k1]:
            t2 = dt_time[k1]
            if b15.index[L15.pos].to_datetime64() > t2 and c[i] < L15.price and L15.pos < i:
                signal = "enter"; pending = dt_top[k1] + 0.1 * atr1[k1]; continue
    if pos: trades.append(dict(pips=-(c[-1]-entry)/PIP - sp, t=b15.index[-1], hold=n-entry_i))
    tr = pd.DataFrame(trades) if trades else pd.DataFrame(columns=["pips","t","hold"])
    total = tr["pips"].sum() - swap_bar * bars_in
    if len(tr):
        eq = tr["pips"].cumsum().to_numpy(); dd = float((np.maximum.accumulate(eq) - eq).max())
        yr = tr.groupby(tr["t"].dt.year)["pips"].sum(); yrs = " ".join(f"{int(y)}:{v:+.0f}" for y, v in yr.items())
        win = round(100 * (tr["pips"] > 0).mean()); pf = tr.loc[tr.pips > 0, "pips"].sum() / max(-tr.loc[tr.pips < 0, "pips"].sum(), 1e-9)
    else:
        dd = 0; yrs = "-"; win = 0; pf = 0
    tag = label or f"ふた山{'有' if need_double_top else '無'}/直撃{'有' if need_zone else '無'}/80%{'有' if need_retrace else '無'}"
    print(f"  {tag:<22} 損益 {total:>6.0f} DD {dd:>5.0f} 往復 {len(tr):>3} 勝率 {win:>3}% PF {pf:.2f} 平均保有 {tr['hold'].mean() if len(tr) else 0:.0f}本(15m) | {yrs}")
    return tr


for code in ["USDJPY", "AUDJPY", "NZDJPY"]:
    print(f"\n■ {code}  3年（スプレッド・swap 控除後、pips）")
    run(code, label="本仕様（再エントリー有・15m高値で手仕舞い）")
    run(code, reentry=False, label="再エントリー無し（ふた山からの初回のみ）")
    run(code, exit_tf="1h", label="手仕舞いを 1h 高値超えに")
    run(code, reentry=False, exit_tf="1h", label="初回のみ ＋ 1h 高値で手仕舞い")
