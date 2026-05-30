#!/usr/bin/env python3
"""過去データでの SMA クロスシグナルのバックテスト（読み取り専用）.

本体 fx_signal.py と同じ決定論的ロジック（短期SMA vs 長期SMAのクロス）を、過去の
ヒストリカルデータ全体に対して走らせ、「いつ買い(LONG)/売り(SHORT)シグナルが
切り替わっていたか」を一覧表示する。

- Slack送信・state.json保存・LLM(リスク層)呼び出しは一切しない。完全に読み取り専用。
- 本体の定数(SHORT_SMA/LONG_SMA/HYSTERESIS_PCT 等)を import して同じ条件で再現する。

使い方:
    python backtest.py                # 既定: 60日・5分足
    python backtest.py 60d 5m
    python backtest.py 2y 1d          # 2年・日足など
"""

from __future__ import annotations

import sys

import pandas as pd
import yfinance as yf

import fx_signal as fx  # 本体の定数とロジックを再利用


def run_backtest(period: str, interval: str) -> int:
    print(f"取得: {fx.TICKER} / period={period} / interval={interval}")
    df = yf.download(
        fx.TICKER, period=period, interval=interval, progress=False, auto_adjust=True
    )
    if df is None or df.empty:
        print("データを取得できませんでした。")
        return 1
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    close = df["Close"].dropna()
    if len(close) < fx.LONG_SMA + 1:
        print(f"本数不足: {len(close)}本（長期SMA {fx.LONG_SMA} に届かず）")
        return 1

    short = close.rolling(fx.SHORT_SMA).mean()
    long = close.rolling(fx.LONG_SMA).mean()

    # 各時点の状態を決定論的に算出（本体と同じ: ヒステリシスも反映）
    states: list[tuple[pd.Timestamp, str, float]] = []
    prev_state: str | None = None
    for ts in close.index:
        s, l = short.loc[ts], long.loc[ts]
        if pd.isna(s) or pd.isna(l):
            continue
        raw = "LONG" if s > l else "SHORT"
        if (
            fx.HYSTERESIS_PCT > 0.0
            and prev_state in ("LONG", "SHORT")
            and l != 0
            and abs(s - l) / abs(l) * 100.0 < fx.HYSTERESIS_PCT
        ):
            state = prev_state
        else:
            state = raw
        states.append((ts, state, float(close.loc[ts])))
        prev_state = state

    if not states:
        print("SMAが算出できる区間がありませんでした。")
        return 1

    # 状態が切り替わった瞬間（=通知が飛ぶ瞬間）だけ抽出
    changes: list[tuple[pd.Timestamp, str, str, float]] = []
    for i in range(1, len(states)):
        if states[i][1] != states[i - 1][1]:
            ts, st, price = states[i]
            changes.append((ts, states[i - 1][1], st, price))

    first_ts = _to_jst(states[0][0])
    last_ts = _to_jst(states[-1][0])
    print(
        f"\n期間: {first_ts} 〜 {last_ts}  /  評価本数: {len(states)}本"
        f"  /  SMA{fx.SHORT_SMA}x{fx.LONG_SMA}"
        f"  /  ヒステリシス: {fx.HYSTERESIS_PCT}%"
    )
    print(f"初期状態: {states[0][1]}（{_label(states[0][1])}）")
    print(f"シグナル転換回数: {len(changes)} 回\n")

    if changes:
        print("―― 転換履歴（この瞬間に通知が飛ぶ）――")
        print(f"{'日時(JST)':<20} {'転換':<16} {'価格':>10}  経過")
        prev_change_ts = states[0][0]
        for ts, frm, to, price in changes:
            held = ts - prev_change_ts
            arrow = "📈買い" if to == "LONG" else "📉売り"
            print(
                f"{_to_jst(ts):<20} {frm}→{to:<5} {arrow:<6} {price:>10.3f}"
                f"  （前回転換から {_fmt_td(held)}）"
            )
            prev_change_ts = ts

    # 簡易サマリー（チャタリング傾向の把握）
    if len(changes) >= 2:
        spans = [
            changes[i][0] - changes[i - 1][0] for i in range(1, len(changes))
        ]
        avg = sum(spans, pd.Timedelta(0)) / len(spans)
        shortest = min(spans)
        print(
            f"\n平均保有: {_fmt_td(avg)}  /  最短保有: {_fmt_td(shortest)}"
            f"  /  最新状態: {states[-1][1]}（{_label(states[-1][1])}）"
        )
        if shortest < pd.Timedelta(hours=1):
            print(
                "※ 1時間未満の短命な転換があります。HYSTERESIS_PCT を上げると"
                "境界での往復を減らせます。"
            )
    return 0


def _to_jst(ts: pd.Timestamp) -> str:
    if isinstance(ts, pd.Timestamp):
        t = ts.tz_localize("UTC") if ts.tzinfo is None else ts
        return t.tz_convert(fx.TOKYO).strftime("%Y-%m-%d %H:%M")
    return str(ts)


def _label(state: str) -> str:
    return "買い目線" if state == "LONG" else "売り目線"


def _fmt_td(td: pd.Timedelta) -> str:
    total_min = int(td.total_seconds() // 60)
    days, rem = divmod(total_min, 1440)
    hours, mins = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}日")
    if hours:
        parts.append(f"{hours}時間")
    parts.append(f"{mins}分")
    return "".join(parts)


if __name__ == "__main__":
    period = sys.argv[1] if len(sys.argv) > 1 else "60d"
    interval = sys.argv[2] if len(sys.argv) > 2 else fx.INTERVAL
    sys.exit(run_backtest(period, interval))
