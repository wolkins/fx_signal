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

データ源（--source）:
    yfinance（既定）… キー不要。ただし5分足は約60日が上限（Yahooの仕様）。
    dukascopy        … キー不要・約2003年〜。5分足のまま長期を遡れる。
                       要 `pip install -r requirements-backtest.txt`。取得は重いので
                       .cache/ にCSVキャッシュして2回目以降は即時。
    例: python backtest.py --source dukascopy USDJPY=X 1y 5m

ロング保護ガード（--guard）:
    python backtest.py --guard 1y 1d                       # 日足で全ペア（買い持ちと比較。エンジンは本体のペア別設定）
    python backtest.py --guard --source dukascopy 3y 5m    # 5分足→日足に集約して3年
    python backtest.py --guard AUDJPY=X 1y 1d              # 単一ペアを切替履歴つきで
    python backtest.py --guard --engine both 2y 1d         # SMA とダウの両エンジンを並べて比較（sma/dow/both）
"""

from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import yfinance as yf

import fx_signal as fx  # 本体の定数とロジックを再利用

CACHE_DIR = Path(__file__).with_name(".cache")


# ─────────────────────────────────────────────────────────────
# データ取得（ソース切替）。返すのは "Close" 列を持つ DataFrame。
# ─────────────────────────────────────────────────────────────
def fetch_history(ticker: str, period: str, interval: str, source: str):
    if source == "dukascopy":
        return _fetch_dukascopy(ticker, period, interval)
    if source == "yfinance":
        return _fetch_yfinance(ticker, period, interval)
    raise ValueError(f"未対応の --source: {source}（yfinance / dukascopy）")


def _fetch_yfinance(ticker: str, period: str, interval: str):
    df = yf.download(
        ticker, period=period, interval=interval, progress=False, auto_adjust=True
    )
    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df


def _parse_period(period: str) -> timedelta:
    """'60d'/'2y'/'6mo'/'3wk' などを timedelta に変換。"""
    m = re.fullmatch(r"\s*(\d+)\s*(d|wk|mo|y)\s*", period)
    if not m:
        raise ValueError(f"未対応の period: {period}（例 60d / 1y / 6mo / 3wk）")
    n = int(m.group(1))
    days = {"d": 1, "wk": 7, "mo": 30, "y": 365}[m.group(2)]
    return timedelta(days=n * days)


def _fetch_dukascopy(ticker: str, period: str, interval: str):
    """Dukascopy から OHLC を取得（キー不要・深い履歴）。CSVキャッシュ付き。"""
    try:
        import logging

        import dukascopy_python as dk

        # 取得の進捗ログが煩いので抑える（ERROR以上のみ・親へ伝播させない）
        _dklog = logging.getLogger("DUKASCRIPT")
        _dklog.setLevel(logging.ERROR)
        _dklog.propagate = False
    except ImportError as exc:
        raise SystemExit(
            "dukascopy-python が必要です: pip install -r requirements-backtest.txt"
        ) from exc

    interval_map = {
        "1m": dk.INTERVAL_MIN_1,
        "5m": dk.INTERVAL_MIN_5,
        "15m": dk.INTERVAL_MIN_15,
        "30m": dk.INTERVAL_MIN_30,
        "1h": dk.INTERVAL_HOUR_1,
        "4h": dk.INTERVAL_HOUR_4,
        "1d": dk.INTERVAL_DAY_1,
        "1wk": dk.INTERVAL_WEEK_1,
    }
    if interval not in interval_map:
        raise ValueError(f"dukascopy 未対応の足種: {interval}")

    instrument = fx.pair_label(ticker)  # 'USD/JPY' 等（Dukascopyの銘柄名と一致）
    end = datetime.now(timezone.utc)
    start = end - _parse_period(period)

    code = fx.pair_code(ticker)
    cache = CACHE_DIR / f"dukascopy_{code}_{interval}_{start.date()}_{end.date()}.csv"
    if cache.exists():
        return pd.read_csv(cache, index_col=0, parse_dates=True)

    raw = dk.fetch(instrument, interval_map[interval], dk.OFFER_SIDE_BID, start, end)
    if raw is None or len(raw) == 0:
        return None
    # close/high/... → Close/High/...（既存ロジックは "Close" を参照）
    df = raw.rename(columns=str.capitalize)
    CACHE_DIR.mkdir(exist_ok=True)
    df.to_csv(cache)
    return df


def _states(df: pd.DataFrame) -> list[tuple[pd.Timestamp, str, float]]:
    """本体(evaluate)と同じ条件（デッドバンド含む）で各時点の状態列を作る。

    デッドバンド判定は fx.deadband_threshold を共用し、本体と必ず一致させる。
    """
    close = df["Close"].dropna()
    short = close.rolling(fx.SHORT_SMA).mean()
    long = close.rolling(fx.LONG_SMA).mean()
    use_atr = fx.DEADBAND_MODE == "atr" and {"High", "Low"}.issubset(df.columns)
    atr = fx.compute_atr(df, fx.ATR_PERIOD).reindex(close.index) if use_atr else None
    out: list[tuple[pd.Timestamp, str, float]] = []
    prev: str | None = None
    for ts in close.index:
        s, l = short.loc[ts], long.loc[ts]
        if pd.isna(s) or pd.isna(l):
            continue
        raw = "LONG" if s > l else "SHORT"
        av = atr.loc[ts] if atr is not None else None
        if prev in ("LONG", "SHORT") and abs(s - l) < fx.deadband_threshold(l, av):
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


def backtest_pair(
    ticker: str, period: str, interval: str, detail: bool, source: str, gate: bool = False
) -> int:
    label = fx.pair_label(ticker)
    dec = fx.pair_decimals(ticker)
    df = fetch_history(ticker, period, interval, source)
    if df is None or df.empty or "Close" not in df.columns:
        print(f"[{label}] データを取得できませんでした。")
        return 1
    close = df["Close"].dropna()
    if len(close) < fx.LONG_SMA + 1:
        print(f"[{label}] 本数不足: {len(close)}本（長期SMA {fx.LONG_SMA} に届かず）")
        return 1

    states = _states(df)
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
        f"  / {len(states)}本 / SMA{fx.SHORT_SMA}x{fx.LONG_SMA}"
        f" / 不感帯 {('ATRx'+str(fx.ATR_K)) if fx.DEADBAND_MODE=='atr' else (str(fx.HYSTERESIS_PCT)+'%')}"
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

    if gate and changes:
        _print_gate_comparison(df, changes)
    return 0


def _print_gate_comparison(df: pd.DataFrame, changes: list) -> None:
    """各SMA転換に MTFゲートを当て、通過/抑制の内訳を出す（before/after比較）。"""
    import dow

    from collections import Counter

    tr4 = dow.trend_series(df, dow.ENV_TF)
    tr1 = dow.trend_series(df, dow.TRADE_TF)
    reasons: Counter = Counter()
    passed = 0
    for ts, _frm, to, _price in changes:
        # 先読みなし: その時刻までに確定した上位足トレンドを asof 参照
        t4 = tr4.asof(ts)
        t1 = tr1.asof(ts)
        res = dow.gate(to, {"4H": t4 if pd.notna(t4) else "UNKNOWN",
                            "1H": t1 if pd.notna(t1) else "UNKNOWN"})
        if res["pass"]:
            passed += 1
        reasons[res["reason"]] += 1
    blocked = len(changes) - passed
    detail = " / ".join(f"{k}:{v}" for k, v in sorted(reasons.items()))
    print(
        f"  ゲート(SWING {dow.SWING_LEFT}/{dow.SWING_RIGHT}): "
        f"転換 {len(changes)} → 通過 {passed} / 抑制 {blocked}"
    )
    print(f"     内訳: {detail}")


# ─────────────────────────────────────────────────────────────
# ロング保護ガード（long_guard.py）の損益評価: --guard
# ─────────────────────────────────────────────────────────────
def _to_daily(df: pd.DataFrame, interval: str) -> pd.DataFrame:
    """日中足なら本番と同じ fx.to_daily_utc で UTC 日足に（日曜の端数は除外）。
    日足ならそのまま（index は naive 日付に正規化）。"""
    if interval != "1d":
        return fx.to_daily_utc(df)
    d = df.copy()
    idx = pd.to_datetime(d.index)
    if idx.tz is not None:
        idx = idx.tz_convert("UTC").tz_localize(None)
    d.index = idx.normalize()
    return d


def _guard_pnl(daily: pd.DataFrame, in_market: pd.Series, spread_pips: float, pip: float):
    """in_market[i]（i日の確定後に決まる）を i+1 日の Open で執行する、ロング専用の損益。

    返り値: (トレード損益pips のリスト, 日次損益pips Series, 在場率)
    """
    o = daily["Open"].to_numpy(dtype=float)
    c = daily["Close"].to_numpy(dtype=float)
    im = in_market.shift(1).fillna(False).to_numpy(dtype=bool)  # 前日の判定を当日に適用
    trades: list[float] = []
    daily_pnl = pd.Series(0.0, index=daily.index)
    pos = False
    entry = None
    for i in range(1, len(c)):
        if im[i] != pos:
            if im[i]:
                pos, entry = True, o[i]
            else:
                trades.append((o[i] - entry) / pip - spread_pips)
                pos, entry = False, None
        if pos:
            # 建てた日は Open→Close、以降は前日Close→当日Close
            daily_pnl.iloc[i] = (c[i] - (o[i] if im[i] != im[i - 1] else c[i - 1])) / pip
    if pos:
        trades.append((c[-1] - entry) / pip - spread_pips)
    return trades, daily_pnl, float(im.mean())


def _max_dd(daily_pnl: pd.Series) -> float:
    eq = daily_pnl.cumsum()
    return float((eq.cummax() - eq).max())


def backtest_guard(ticker: str, period: str, interval: str, detail: bool, source: str,
                   engine: str | None = None) -> int:
    """ロング保護ガードを過去データに流し、買い持ちとの比較を出す。

    engine: "sma"（long_guard）/ "dow"（dow_guard）/ None（本体の guard_engine() に従う）。
    """
    engine = engine or fx.guard_engine(ticker)
    lg = fx.guard_module(engine)

    label = fx.pair_label(ticker)
    dec = fx.pair_decimals(ticker)
    pip = 10 ** (-(dec - 1))  # JPYクロス(3桁)=0.01, それ以外(5桁)=0.0001
    spread = {3: 0.5, 5: 1.0}.get(dec, 1.0)  # 往復スプレッドの保守的な想定(pips)
    if source == "yfinance" and interval == "1d":
        # yfinance の FX 日足は Close が前日終値（00:00 UTC の値）で使えない。本番と同じく
        # 1時間足を UTC 日足にまとめる（yfinance の 1h は約2年ぶんまで）。
        print(f"[{label}] yfinance の 1d は FX の終値が前日値になるため、1h→UTC日足に切り替えます。")
        interval = "1h"
    df = fetch_history(ticker, period, interval, source)
    if df is None or df.empty or "Close" not in df.columns or "Open" not in df.columns:
        print(f"[{label}] データを取得できませんでした（Open/Close が必要）。")
        return 1
    daily = _to_daily(df, interval)
    if engine == "dow":
        need = lg.MIN_BARS
        desc = f"日足ダウ理論（山谷 左右{lg.PIVOT_LR}本・{lg.MIN_WAVE}本未満の波は無視）"
        if {"High", "Low"} - set(daily.columns):
            print(f"[{label}] dow エンジンには High/Low が必要です。")
            return 1
    else:
        need = lg.GUARD_SMA_DAYS + 5
        desc = f"日足SMA{lg.GUARD_SMA_DAYS}・帯±{lg.GUARD_BAND_PCT:g}%"
    if len(daily) < need:
        print(f"[{label}] 日足本数不足: {len(daily)}本（{need}本必要）")
        return 1

    st = lg.guard_states(daily) if engine == "dow" else lg.guard_states(daily["Close"])
    hold = st == lg.STATE_HOLD
    valid = st.notna()
    # 比較対象の買い持ちは、ガードが判定可能になった日以降で揃える
    bh_trades, bh_daily, _ = _guard_pnl(daily, valid, spread, pip)
    g_trades, g_daily, in_rate = _guard_pnl(daily, hold & valid, spread, pip)
    flips = st[valid][st[valid] != st[valid].shift()].iloc[1:]

    print(
        f"\n■ {label}  期間 {daily.index[0].date()}〜{daily.index[-1].date()} / 日足 {len(daily)}本"
        f" / ガード[{engine}]: {desc} / スプレッド往復 {spread}pips"
    )
    print(
        f"  買い持ち : 損益 {sum(bh_trades):>7.0f} pips / 最大DD {_max_dd(bh_daily):>6.0f} / 在場 100%"
    )
    print(
        f"  ガード   : 損益 {sum(g_trades):>7.0f} pips / 最大DD {_max_dd(g_daily):>6.0f}"
        f" / 在場 {100*in_rate:.0f}% / 往復 {len(g_trades)}回（切替 {len(flips)}回）"
        f" / 現在 {st.iloc[-1]}（{lg.stance_label(st.iloc[-1])}）"
    )
    years = sorted(set(daily.index.year))
    yr = "  ".join(
        f"{y}: {g_daily[g_daily.index.year == y].sum():>5.0f}/{bh_daily[bh_daily.index.year == y].sum():>5.0f}"
        for y in years
    )
    print(f"  年次 pips（ガード/買い持ち）: {yr}")
    if detail and len(flips):
        print("  ―― 切替履歴 ――")
        for ts, s in flips.items():
            arrow = "🛡️離脱" if s == lg.STATE_EXIT else "🟢復帰"
            print(f"  {ts.date()}  → {s:<4} {arrow}  終値 {daily.loc[ts, 'Close']:.{dec}f}")
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
    # 引数解析: --source <yfinance|dukascopy>、先頭が "XXX=X" ならそのペアを詳細表示。
    # 残りの位置引数は period, interval。
    args: list[str] = []
    source = "yfinance"
    gate = False
    guard = False
    engine: str | None = None
    it = iter(argv)
    for a in it:
        if a == "--source":
            source = next(it, "yfinance")
        elif a.startswith("--source="):
            source = a.split("=", 1)[1]
        elif a == "--gate":  # MTFゲート有無の比較出力を追加
            gate = True
        elif a == "--guard":  # ロング保護ガード（long_guard / dow_guard）の損益評価
            guard = True
        elif a == "--engine":  # --guard のエンジン強制: sma / dow / both（既定: 本体のペア別設定）
            engine = next(it, None)
        elif a.startswith("--engine="):
            engine = a.split("=", 1)[1]
        else:
            args.append(a)

    single = args.pop(0) if args and args[0].endswith("=X") else None
    period = args[0] if len(args) > 0 else "60d"
    interval = args[1] if len(args) > 1 else fx.INTERVAL

    tickers = [single] if single else fx.PAIRS
    print(f"バックテスト: {', '.join(fx.pair_label(t) for t in tickers)}"
          f"  / source={source} / period={period} / interval={interval}"
          f"{' / gate比較' if gate else ''}{' / ロング保護ガード' if guard else ''}")
    rc = 0
    for t in tickers:
        try:
            if guard:
                engines = ["sma", "dow"] if engine == "both" else [engine]
                for e in engines:
                    rc |= backtest_guard(t, period, interval, detail=bool(single), source=source, engine=e)
                continue
            rc |= backtest_pair(
                t, period, interval, detail=bool(single), source=source, gate=gate
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[{fx.pair_label(t)}] エラー: {exc}")
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
