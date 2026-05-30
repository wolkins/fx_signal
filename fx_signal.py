#!/usr/bin/env python3
"""USD/JPY 個人用シグナル監視ツール.

短期SMAと長期SMAのクロスで買い/売りの状態を判定し、状態が変わった瞬間だけ
Slack に通知する。自動売買はしない（発注は手動）。

設定はファイル冒頭の定数で変更できる。
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf

# ─────────────────────────────────────────────────────────────
# 設定（ここを変えれば挙動を調整できる）
# ─────────────────────────────────────────────────────────────
TICKER = "USDJPY=X"      # yfinance のティッカー
INTERVAL = "5m"           # 足種（例: "5m", "15m", "1h", "1d"）
PERIOD = "5d"             # 取得期間
SHORT_SMA = 20            # 短期SMAの本数
LONG_SMA = 50             # 長期SMAの本数
RSI_PERIOD = 14           # RSIの計算期間（参考情報）

# ヒステリシス（デッドバンド）: 短期SMAと長期SMAの乖離が長期SMA比でこの%未満の
# 間は、状態を切り替えず前回状態を維持する。SMAが拮抗して LONG/SHORT が交互に
# 揺れるのを防ぐ。0.0 で無効（純粋なクロスのみで判定 = 従来挙動）。
# 例: 0.02 にすると約0.02%（USD/JPY 159円で約3pips）の余裕を持たせる。
HYSTERESIS_PCT = 0.0

STATE_FILE = Path(__file__).with_name("state.json")
TOKYO = ZoneInfo("Asia/Tokyo")
WEBHOOK_ENV = "SLACK_WEBHOOK_URL"


# ─────────────────────────────────────────────────────────────
# データ取得
# ─────────────────────────────────────────────────────────────
def fetch_data() -> pd.DataFrame:
    """yfinance から価格データを取得し、列を平坦化して返す。

    通信失敗や yfinance 側のエラーで例外が出てもジョブを落とさず、空の
    DataFrame を返して呼び出し側に「何もせず正常終了」させる。
    """
    try:
        df = yf.download(
            TICKER,
            interval=INTERVAL,
            period=PERIOD,
            progress=False,
            auto_adjust=True,
        )
    except Exception as exc:  # noqa: BLE001 - 取得失敗は握りつぶして正常終了させる
        print(f"データ取得に失敗しました（スキップ）: {exc}", file=sys.stderr)
        return pd.DataFrame()

    if df is None or df.empty:
        return pd.DataFrame()

    # 単一ティッカーでも列が MultiIndex になることがあるので平坦化する
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    return df


# ─────────────────────────────────────────────────────────────
# 指標計算
# ─────────────────────────────────────────────────────────────
def compute_rsi(close: pd.Series, period: int = RSI_PERIOD) -> float | None:
    """RSI(14) を計算して最新値を返す。計算不能なら None。"""
    if len(close) < period + 1:
        return None

    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)

    # Wilder の平滑移動平均
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

    last_gain = avg_gain.iloc[-1]
    last_loss = avg_loss.iloc[-1]

    if pd.isna(last_gain) or pd.isna(last_loss):
        return None

    # 除算ゼロ（loss=0）で落ちないようにする
    if last_loss == 0:
        return 100.0 if last_gain > 0 else 50.0

    rs = last_gain / last_loss
    return float(100.0 - (100.0 / (1.0 + rs)))


def evaluate(df: pd.DataFrame, prev_state: str | None = None) -> dict | None:
    """SMAクロスから状態を判定する。データ不足なら None。

    prev_state を渡すと、SMA乖離が HYSTERESIS_PCT 未満のデッドバンド内では
    状態を切り替えず前回状態を維持する（境界でのチャタリング防止）。
    """
    if df.empty or "Close" not in df.columns:
        return None

    close = df["Close"].dropna()

    # 長期SMAに足りない本数しか無ければ判定不能（市場クローズ中など）
    if len(close) < LONG_SMA:
        return None

    short_ma = close.rolling(SHORT_SMA).mean().iloc[-1]
    long_ma = close.rolling(LONG_SMA).mean().iloc[-1]

    if pd.isna(short_ma) or pd.isna(long_ma):
        return None

    raw_state = "LONG" if short_ma > long_ma else "SHORT"

    # デッドバンド内（SMAが拮抗）なら前回状態を維持してチャタリングを防ぐ
    if (
        HYSTERESIS_PCT > 0.0
        and prev_state in ("LONG", "SHORT")
        and long_ma != 0
        and abs(short_ma - long_ma) / abs(long_ma) * 100.0 < HYSTERESIS_PCT
    ):
        state = prev_state
    else:
        state = raw_state

    # 最新足の時刻を日本時間に変換（tz-naive の場合も考慮）
    last_ts = close.index[-1]
    if isinstance(last_ts, pd.Timestamp):
        if last_ts.tzinfo is None:
            last_ts = last_ts.tz_localize("UTC")
        last_ts = last_ts.tz_convert(TOKYO)
        ts_str = last_ts.strftime("%Y-%m-%d %H:%M JST")
    else:
        ts_str = str(last_ts)

    return {
        "state": state,
        "price": float(close.iloc[-1]),
        "short_ma": float(short_ma),
        "long_ma": float(long_ma),
        "rsi": compute_rsi(close),
        "time": ts_str,
    }


# ─────────────────────────────────────────────────────────────
# 状態の保存・読み込み
# ─────────────────────────────────────────────────────────────
def load_state() -> dict | None:
    if not STATE_FILE.exists():
        return None
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"state.json の読み込みに失敗: {exc}", file=sys.stderr)
        return None


def save_state(info: dict) -> None:
    payload = {
        "state": info["state"],
        "price": info["price"],
        "time": info["time"],
        "updated_at": datetime.now(TOKYO).strftime("%Y-%m-%d %H:%M:%S JST"),
    }
    STATE_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


# ─────────────────────────────────────────────────────────────
# 通知
# ─────────────────────────────────────────────────────────────
def notify(text: str) -> None:
    """Slack へ通知する。未設定なら標準エラー出力に出すだけ。"""
    url = os.environ.get(WEBHOOK_ENV)
    if not url:
        print(
            f"[{WEBHOOK_ENV} 未設定 — 通知内容を表示]\n{text}",
            file=sys.stderr,
        )
        return

    try:
        resp = requests.post(url, json={"text": text}, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f"Slack通知に失敗: {exc}\n本文:\n{text}", file=sys.stderr)


def format_message(info: dict, *, first_run: bool = False) -> str:
    label = "買い目線 📈" if info["state"] == "LONG" else "売り目線 📉"
    rsi = info["rsi"]
    rsi_str = f"{rsi:.1f}" if rsi is not None else "N/A"

    header = "🔔 USD/JPY 監視を開始しました" if first_run else "⚡ USD/JPY シグナル変化"

    return (
        f"{header}\n"
        f"状態: *{info['state']}*（{label}）\n"
        f"価格: {info['price']:.3f}\n"
        f"SMA{SHORT_SMA}: {info['short_ma']:.3f} / SMA{LONG_SMA}: {info['long_ma']:.3f}\n"
        f"RSI{RSI_PERIOD}: {rsi_str}\n"
        f"最新足: {info['time']}（{INTERVAL}）"
    )


# ─────────────────────────────────────────────────────────────
# メイン
# ─────────────────────────────────────────────────────────────
def main() -> int:
    prev = load_state()
    prev_state = prev.get("state") if prev else None

    df = fetch_data()
    info = evaluate(df, prev_state=prev_state)

    if info is None:
        # データが空 or 本数不足（市場クローズ中など）はクラッシュさせず正常終了
        print("データ不足のため判定をスキップしました（市場クローズ中など）。")
        return 0

    if prev is None:
        # 初回起動: 監視開始を1回だけ通知して状態を保存
        notify(format_message(info, first_run=True))
        save_state(info)
        print(f"初回起動: 状態を {info['state']} で保存しました。")
        return 0

    if prev.get("state") != info["state"]:
        # 状態が変わった瞬間だけ通知
        notify(format_message(info))
        save_state(info)
        print(f"状態変化: {prev.get('state')} → {info['state']}")
    else:
        # 同じ状態が続く間は通知しない（チャタリング防止）
        print(f"状態変化なし: {info['state']}（通知スキップ）")

    return 0


if __name__ == "__main__":
    sys.exit(main())
