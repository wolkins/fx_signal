#!/usr/bin/env python3
"""fx-signal の損益ベース評価（読み取り専用・スクラッチ）。

本体 fx_signal / dow と同じ判定ロジックを 3年・5分足(Dukascopy キャッシュ)に流し、
「合図どおりに手動で建玉を反転させた場合」の損益を pips で評価する。

ポジションモデル:
  - 合図が出た足の「次の足の Open」で建てる（通知→手動発注の遅延を保守的に見る）
  - 次の合図でドテン（常時建玉）。ゲート変種では抑制時の扱いを切り替える。
  - コスト: スプレッド片道分を往復で控除（JPYクロス 1pip = 0.01）
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parent.parent  # リポジトリ root（analysis_scratch_*/ の親）
sys.path.insert(0, str(REPO))
import fx_signal as fx  # noqa: E402
import dow  # noqa: E402

CACHE = REPO / ".cache"
PAIRS = {"USDJPY": 0.3, "AUDJPY": 0.7, "NZDJPY": 1.0}  # 往復スプレッド想定(pips)
PIP = 0.01
SPLIT = pd.Timestamp("2025-06-05", tz="UTC")  # 前2年=探索 / 後1年=検証


def load(code: str) -> pd.DataFrame:
    df = pd.read_csv(CACHE / f"dukascopy_{code}_5m_2023-06-05_2026-06-04.csv",
                     index_col=0, parse_dates=True)
    return df.dropna(subset=["Close"])


# ── 状態列（fx.evaluate / backtest._states と同じ規則。パラメータ化） ──────────
def states(df: pd.DataFrame, short: int, long: int, mode: str, k: float, pct: float) -> pd.Series:
    close = df["Close"]
    s = close.rolling(short).mean().to_numpy()
    l = close.rolling(long).mean().to_numpy()
    if mode == "atr":
        band = (k * fx.compute_atr(df, fx.ATR_PERIOD)).to_numpy()
    else:
        band = np.abs(l) * (pct / 100.0)
    out = np.empty(len(close), dtype=object)
    prev = None
    for i in range(len(close)):
        if np.isnan(s[i]) or np.isnan(l[i]):
            out[i] = None
            continue
        raw = "LONG" if s[i] > l[i] else "SHORT"
        if prev is not None and abs(s[i] - l[i]) < band[i]:
            st = prev
        else:
            st = raw
        out[i] = st
        prev = st
    return pd.Series(out, index=close.index)


def changes(st: pd.Series) -> pd.DataFrame:
    """転換時刻と新状態。"""
    v = st.dropna()
    chg = v[v != v.shift()].iloc[1:]  # 最初の確定は転換ではない
    return pd.DataFrame({"to": chg})


# ── ゲート（dow.trend_series を asof 参照。先読みなし） ─────────────────────────
def gate_pass(df: pd.DataFrame, chg: pd.DataFrame, range_blocks: bool) -> pd.Series:
    tr4 = dow.trend_series(df, dow.ENV_TF)
    tr1 = dow.trend_series(df, dow.TRADE_TF)
    old = dow.RANGE_BLOCKS
    dow.RANGE_BLOCKS = range_blocks
    try:
        res = []
        for ts, to in chg["to"].items():
            t4, t1 = tr4.asof(ts), tr1.asof(ts)
            r = dow.gate(to, {"4H": t4 if isinstance(t4, str) else "UNKNOWN",
                              "1H": t1 if isinstance(t1, str) else "UNKNOWN"})
            res.append(r["pass"])
    finally:
        dow.RANGE_BLOCKS = old
    return pd.Series(res, index=chg.index)


# ── 損益 ────────────────────────────────────────────────────────────────────────
def pnl(df: pd.DataFrame, chg: pd.DataFrame, pos_rule, spread: float) -> dict:
    """pos_rule(row) -> +1 / -1 / 0 を転換ごとに適用し、セグメント損益を集計。"""
    opens = df["Open"]
    idx = df.index
    trades = []  # (entry_ts, exit_ts, dir, pips)
    cur_dir, cur_entry, cur_ts = 0, None, None
    for ts, row in chg.iterrows():
        pos = pos_rule(row)
        loc = idx.get_loc(ts)
        if loc + 1 >= len(idx):
            break
        px = float(opens.iloc[loc + 1])  # 次足 Open
        ets = idx[loc + 1]
        if pos == cur_dir:
            continue  # ポジション変化なし（抑制で保持など）
        if cur_dir != 0:
            trades.append((cur_ts, ets, cur_dir, cur_dir * (px - cur_entry) / PIP - spread))
        cur_dir, cur_entry, cur_ts = pos, px, ets
    if cur_dir != 0:
        trades.append((cur_ts, idx[-1], cur_dir, cur_dir * (float(df["Close"].iloc[-1]) - cur_entry) / PIP - spread))
    return trades


def summarize(trades, label: str, start=None, end=None) -> dict:
    t = [x for x in trades if (start is None or x[0] >= start) and (end is None or x[0] < end)]
    if not t:
        return {"label": label, "trades": 0}
    p = np.array([x[3] for x in t])
    hold = np.array([(x[1] - x[0]).total_seconds() / 3600 for x in t])
    eq = np.cumsum(p)
    dd = float((np.maximum.accumulate(eq) - eq).max())
    days = (t[-1][1] - t[0][0]).total_seconds() / 86400
    gp, gl = p[p > 0].sum(), -p[p < 0].sum()
    return {
        "label": label,
        "trades": len(t),
        "per_day": len(t) / days,
        "win%": 100 * (p > 0).mean(),
        "net_pips": p.sum(),
        "avg_pips": p.mean(),
        "PF": gp / gl if gl > 0 else float("inf"),
        "maxDD": dd,
        "avg_hold_h": hold.mean(),
        "whipsaw<3h": int((hold < 3).sum()),
        "pips/yr": p.sum() / days * 365,
    }


def fmt(rows: list[dict]) -> str:
    cols = ["label", "trades", "per_day", "win%", "net_pips", "avg_pips", "PF", "maxDD", "avg_hold_h", "whipsaw<3h", "pips/yr"]
    out = ["  ".join(f"{c:>10}" if c != "label" else f"{c:<34}" for c in cols)]
    for r in rows:
        cells = []
        for c in cols:
            v = r.get(c, "")
            if c == "label":
                cells.append(f"{v:<34}")
            elif isinstance(v, float):
                cells.append(f"{v:>10.1f}" if c != "PF" else f"{v:>10.2f}")
            else:
                cells.append(f"{v:>10}")
        out.append("  ".join(cells))
    return "\n".join(out)


def run_config(df, code, short, long, mode, k, pct, with_gate=True):
    st = states(df, short, long, mode, k, pct)
    chg = changes(st)
    spread = PAIRS[code]
    variants = {}
    sign = lambda to: 1 if to == "LONG" else -1  # noqa: E731
    variants["nogate(flip)"] = pnl(df, chg, lambda r: sign(r["to"]), spread)
    if with_gate:
        gp = gate_pass(df, chg, range_blocks=False)
        chg_g = chg.assign(ok=gp)
        # 現行ライブ挙動: 抑制時は通知が無い＝利用者は前の建玉を持ち続ける
        hold_dir = {"d": 0}
        def rule_hold(r):
            if r["ok"]:
                hold_dir["d"] = sign(r["to"])
            return hold_dir["d"]
        variants["gate:blocked=hold(現行)"] = pnl(df, chg_g, rule_hold, spread)
        # 改良候補: 抑制時に「決済のみ」通知してフラット
        variants["gate:blocked=flat"] = pnl(df, chg_g, lambda r: sign(r["to"]) if r["ok"] else 0, spread)
        gp2 = gate_pass(df, chg, range_blocks=True)
        chg_g2 = chg.assign(ok=gp2)
        variants["gate+RANGE:blocked=flat"] = pnl(df, chg_g2, lambda r: sign(r["to"]) if r["ok"] else 0, spread)
    return variants


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "baseline"
    dfs = {c: load(c) for c in PAIRS}

    if mode == "baseline":
        print("### 現行設定 SMA50/100 ATRx1.0 — 全期間(3y) / 探索期(前2y) / 検証期(後1y)")
        for code, df in dfs.items():
            v = run_config(df, code, fx.SHORT_SMA, fx.LONG_SMA, fx.DEADBAND_MODE, fx.ATR_K, fx.HYSTERESIS_PCT)
            print(f"\n■ {code}  (スプレッド往復 {PAIRS[code]} pips)")
            rows = []
            for name, tr in v.items():
                rows.append(summarize(tr, f"{name} [3y]"))
                rows.append(summarize(tr, f"{name} [後1y]", start=SPLIT))
            print(fmt(rows))
        # 参考: 期間中の値幅
        for code, df in dfs.items():
            print(f"  参考 {code}: 始値 {df['Close'].iloc[0]:.3f} → 終値 {df['Close'].iloc[-1]:.3f} "
                  f"(買い持ち {(df['Close'].iloc[-1]-df['Close'].iloc[0])/PIP:+.0f} pips)")

    elif mode == "sweep":
        print("### パラメータ探索（ゲート無し flip）: 前2y で選び、後1y で検証。pips/yr と PF を見る")
        grid = []
        for short, long in [(20, 50), (50, 100), (50, 200), (100, 200), (100, 300), (200, 400)]:
            for k in [0.0, 0.5, 1.0, 1.5, 2.0, 3.0]:
                grid.append((short, long, "atr", k))
        for code, df in dfs.items():
            print(f"\n■ {code}")
            rows = []
            for short, long, m, k in grid:
                st = states(df, short, long, m, k, 0)
                chg = changes(st)
                tr = pnl(df, chg, lambda r: 1 if r["to"] == "LONG" else -1, PAIRS[code])
                a = summarize(tr, f"SMA{short}/{long} ATRx{k} [前2y]", end=SPLIT)
                b = summarize(tr, f"SMA{short}/{long} ATRx{k} [後1y]", start=SPLIT)
                rows.append(a); rows.append(b)
            print(fmt(rows))

    elif mode == "sweep_gate":
        print("### 有望設定にゲートを当てる（blocked=flat / RANGE有無）")
        cfgs = [(50, 100, 1.0), (100, 200, 1.0), (100, 200, 2.0), (50, 200, 1.0), (100, 300, 1.0), (200, 400, 1.0)]
        for code, df in dfs.items():
            print(f"\n■ {code}")
            rows = []
            for short, long, k in cfgs:
                v = run_config(df, code, short, long, "atr", k, 0)
                for name, tr in v.items():
                    rows.append(summarize(tr, f"{short}/{long} k{k} {name} [前2y]", end=SPLIT))
                    rows.append(summarize(tr, f"{short}/{long} k{k} {name} [後1y]", start=SPLIT))
            print(fmt(rows))


if __name__ == "__main__":
    main()
