#!/usr/bin/env python3
"""追加分析: 年次安定性 / ゲートの情報量 / 時間帯・曜日 / ゲート変種。"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from analyze_pnl import PAIRS, PIP, SPLIT, load, states, changes, pnl, summarize, fmt  # noqa: E402
import fx_signal as fx  # noqa: E402
import dow  # noqa: E402

JST = "Asia/Tokyo"


def trades_df(trades) -> pd.DataFrame:
    t = pd.DataFrame(trades, columns=["entry", "exit", "dir", "pips"])
    t["hold_h"] = (t["exit"] - t["entry"]).dt.total_seconds() / 3600
    return t


def gate_info(df, chg):
    tr4 = dow.trend_series(df, dow.ENV_TF)
    tr1 = dow.trend_series(df, dow.TRADE_TF)
    rows = []
    for ts, to in chg["to"].items():
        t4, t1 = tr4.asof(ts), tr1.asof(ts)
        t4 = t4 if isinstance(t4, str) else "UNKNOWN"
        t1 = t1 if isinstance(t1, str) else "UNKNOWN"
        r = dow.gate(to, {"4H": t4, "1H": t1})
        rows.append({"ts": ts, "t4": t4, "t1": t1, "pass": r["pass"], "reason": r["reason"]})
    return pd.DataFrame(rows).set_index("ts")


def main():
    sign = lambda to: 1 if to == "LONG" else -1  # noqa: E731
    for code in PAIRS:
        df = load(code)
        st = states(df, fx.SHORT_SMA, fx.LONG_SMA, fx.DEADBAND_MODE, fx.ATR_K, fx.HYSTERESIS_PCT)
        chg = changes(st)
        tr = pnl(df, chg, lambda r: sign(r["to"]), PAIRS[code])
        t = trades_df(tr)
        print(f"\n\n████ {code}  現行設定 SMA50/100 ATRx1.0 ゲート無し flip  (全 {len(t)} トレード)")

        # (a) 年次
        print("\n(a) 期間別（半年刻み）: 戦略の安定性")
        t["half"] = t["entry"].dt.year.astype(str) + "H" + ((t["entry"].dt.month > 6).astype(int) + 1).astype(str)
        g = t.groupby("half")["pips"]
        for h, s in g:
            print(f"   {h}: n={len(s):>4}  net={s.sum():>8.0f}  avg={s.mean():>6.1f}  win%={100*(s>0).mean():>5.1f}")

        # (b) ゲートの情報量: 抑制された合図は実際に損だったか
        print("\n(b) ゲート判定別の成績（同じ flip トレードを通過/抑制でラベル分け）")
        gi = gate_info(df, chg)
        # 合図時刻 → トレード（entry は次足なので ts+5m）
        gi["entry"] = gi.index + pd.Timedelta(minutes=5)
        m = t.merge(gi.reset_index(drop=True), on="entry", how="left")
        for key in ["pass", "reason"]:
            for k, s in m.groupby(key)["pips"]:
                print(f"   {key}={str(k):<24} n={len(s):>4}  avg={s.mean():>6.1f}  net={s.sum():>8.0f}  win%={100*(s>0).mean():>5.1f}")
        print("   4H×1H トレンド組み合わせ別 avg pips（合図方向=順方向をF、逆=C、RANGE=R、UNKNOWN=U で表記）")
        def rel(trend, to):
            if trend == "RANGE": return "R"
            if trend == "UNKNOWN": return "U"
            return "F" if (trend == "UP") == (to == "LONG") else "C"
        m["to"] = m["dir"].map({1: "LONG", -1: "SHORT"})
        m["combo"] = m.apply(lambda r: f"4H{rel(r['t4'], r['to'])}/1H{rel(r['t1'], r['to'])}", axis=1)
        for k, s in m.groupby("combo")["pips"]:
            print(f"   {k:<10} n={len(s):>4}  avg={s.mean():>6.1f}  net={s.sum():>8.0f}  win%={100*(s>0).mean():>5.1f}")

        # (c) 時間帯（JST）
        print("\n(c) エントリー時間帯(JST)別")
        hr = t["entry"].dt.tz_convert(JST).dt.hour
        sess = pd.cut(hr, bins=[-1, 7, 15, 21, 24], labels=["早朝0-7", "東京8-15", "欧州16-21", "NY22-24"])
        for k, s in t.groupby(sess, observed=True)["pips"]:
            print(f"   {k:<8} n={len(s):>4}  avg={s.mean():>6.1f}  net={s.sum():>8.0f}  win%={100*(s>0).mean():>5.1f}")

        # (d) 曜日
        print("\n(d) エントリー曜日(JST)別 / 週末跨ぎ")
        wd = t["entry"].dt.tz_convert(JST).dt.day_name().str[:3]
        for k, s in t.groupby(wd)["pips"]:
            print(f"   {k} n={len(s):>4}  avg={s.mean():>6.1f}  net={s.sum():>8.0f}")
        span_wk = (t["exit"].dt.tz_convert(JST).dt.to_period("W") != t["entry"].dt.tz_convert(JST).dt.to_period("W"))
        for k, s in t.groupby(span_wk)["pips"]:
            print(f"   週末跨ぎ={k!s:<5} n={len(s):>4}  avg={s.mean():>6.1f}  net={s.sum():>8.0f}")

        # (e) ゲート変種: 1Hのみ / 4Hのみ、blocked=flat
        print("\n(e) ゲート変種（blocked=flat）: 前2y / 後1y")
        rows = []
        def var(name, ok_series):
            c = chg.assign(ok=ok_series.values)
            trv = pnl(df, c, lambda r: sign(r["to"]) if r["ok"] else 0, PAIRS[code])
            rows.append(summarize(trv, f"{name} [前2y]", end=SPLIT))
            rows.append(summarize(trv, f"{name} [後1y]", start=SPLIT))
        rel4 = gi.apply(lambda r: rel(r["t4"], chg.loc[r.name, "to"]), axis=1)
        rel1 = gi.apply(lambda r: rel(r["t1"], chg.loc[r.name, "to"]), axis=1)
        var("現行: 4H&1H 逆行で抑制", gi["pass"])
        var("4Hのみ 逆行で抑制", rel4 != "C")
        var("1Hのみ 逆行で抑制", rel1 != "C")
        var("4H 順方向のみ通す", rel4 == "F")
        var("1H 順方向のみ通す", rel1 == "F")
        var("逆ゲート: 4H逆行の時だけ通す", rel4 == "C")
        print(fmt(rows))


if __name__ == "__main__":
    main()
