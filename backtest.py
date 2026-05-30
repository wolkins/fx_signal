#!/usr/bin/env python3
"""過去データでの SMA クロスシグナルのバックテスト（読み取り専用）.

本体 fx_signal.py と同じ決定論的ロジック（短期SMA vs 長期SMAのクロス）を、過去の
ヒストリカルデータ全体に対して走らせ、「いつ買い(LONG)/売り(SHORT)シグナルが
切り替わっていたか」を検証する。複数ペアに対応。

- Slack送信・state保存・LLM(リスク層)呼び出しは一切しない。完全に読み取り専用。
- 本体の定数(PAIRS/SHORT_SMA/LONG_SMA/HYSTERESIS_PCT 等)を import して再現する。

使い方:
    python backtest.py                  # PAIRS 全ペアを 60日・5分足で要約
    python backtest.py 60d 5m           # 全ペアを期間/足種指定で要約
    python backtest.py USDJPY=X 60d 5m  # 単一ペアを詳細(転換履歴つき)で
"""

from __future__ import annotations

import sys

import pandas as pd
import yfinance as yf

import fx_signal as fx  # 本体の定数とロジックを再利用


def _states(close: pd.Series) -> list[tuple[pd.Timestamp, str, float]]:
    """本体と同じ条件（ヒステリシス含む）で各時点の状態列を作る。"""
    short = close.rolling(fx.SHORT_SMA).mean()
    long = close.rolling(fx.LONG_SMA).mean()
    out: list[tuple[pd.Timestamp, str, float]] = []
    prev: str | None = None
    for ts in close.index:
        s, l = short.loc[ts], long.loc[ts]
        if pd.isna(s) or pd.isna(l):
            continue
        raw = "LONG" if s > l else "SHORT"
        if (
            fx.HYSTERESIS_PCT > 0.0
            and prev in ("LONG", "SHORT")
            and l != 0
            and abs(s - l) / abs(l) * 100.0 < fx.HYSTERESIS_PCT
        ):
            state = prev
        else:
            state = raw
        out.append((ts, state, float(close.loc[ts])))
        prev = state
    return out


def _changes(states) -> list[tuple[pd.Timestamp, str, str, float]]:
    return [
        (states[i][0], states[i - 1][1], states[i][1], states[i][2])
        for i in range(1, len(states))
        if states[i][1] != states[i - 1][1]
    ]


def backtest_pair(ticker: str, period: str, interval: str, detail: bool) -> int:
    label = fx.pair_label(ticker)
    dec = fx.pair_decimals(ticker)
    df = yf.download(
        ticker, period=period, interval=interval, progress=False, auto_adjust=True
    )
    if df is None or df.empty:
        print(f"[{label}] データを取得できませんでした。")
        return 1
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    close = df["Close"].dropna()
    if len(close) < fx.LONG_SMA + 1:
        print(f"[{label}] 本数不足: {len(close)}本（長期SMA {fx.LONG_SMA} に届かず）")
        return 1

    states = _states(close)
    if not states:
        print(f"[{label}] SMAが算出できる区間がありませんでした。")
        return 1
    changes = _changes(states)

    days = (states[-1][0] - states[0][0]).total_seconds() / 86400 or 1
    per_day = len(changes) / days
    avg = shortest = None
    if len(changes) >= 2:
        spans = [changes[i][0] - changes[i - 1][0] for i in range(1, len(changes))]
        avg = sum(spans, pd.Timedelta(0)) / len(spans)
        shortest = min(spans)

    print(
        f"\n■ {label}  期間 {_jst(states[0][0])}〜{_jst(states[-1][0])}"
        f"  / {len(states)}本 / SMA{fx.SHORT_SMA}x{fx.LONG_SMA} / ヒス {fx.HYSTERESIS_PCT}%"
    )
    print(
        f"  転換 {len(changes)}回（約{per_day:.1f}回/日） / "
        f"平均保有 {_td(avg) if avg else '-'} / 最短 {_td(shortest) if shortest else '-'} / "
        f"最新 {states[-1][1]}（{_label(states[-1][1])}）"
    )
    if shortest is not None and shortest < pd.Timedelta(hours=1):
        print("  ※ 1時間未満の短命な転換あり。HYSTERESIS_PCT を上げると往復が減ります。")

    if detail and changes:
        print(f"  ―― 転換履歴 ――")
        prev_ts = states[0][0]
        for ts, frm, to, price in changes:
            arrow = "📈買い" if to == "LONG" else "📉売り"
            print(
                f"  {_jst(ts):<17} {frm}→{to:<5} {arrow:<6} {price:>11.{dec}f}"
                f"  （前回から {_td(ts - prev_ts)}）"
            )
            prev_ts = ts
    return 0


def _jst(ts: pd.Timestamp) -> str:
    if isinstance(ts, pd.Timestamp):
        t = ts.tz_localize("UTC") if ts.tzinfo is None else ts
        return t.tz_convert(fx.TOKYO).strftime("%Y-%m-%d %H:%M")
    return str(ts)


def _label(state: str) -> str:
    return "買い目線" if state == "LONG" else "売り目線"


def _td(td: pd.Timedelta) -> str:
    total = int(td.total_seconds() // 60)
    d, rem = divmod(total, 1440)
    h, m = divmod(rem, 60)
    return (f"{d}日" if d else "") + (f"{h}時間" if h else "") + f"{m}分"


def main(argv: list[str]) -> int:
    # 引数解析: 先頭が "XXX=X" ならそのペアを詳細表示。残りは period, interval。
    args = list(argv)
    single = None
    if args and args[0].endswith("=X"):
        single = args.pop(0)
    period = args[0] if len(args) > 0 else "60d"
    interval = args[1] if len(args) > 1 else fx.INTERVAL

    tickers = [single] if single else fx.PAIRS
    print(f"バックテスト: {', '.join(fx.pair_label(t) for t in tickers)}"
          f"  / period={period} / interval={interval}")
    rc = 0
    for t in tickers:
        try:
            rc |= backtest_pair(t, period, interval, detail=bool(single))
        except Exception as exc:  # noqa: BLE001
            print(f"[{fx.pair_label(t)}] エラー: {exc}")
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
