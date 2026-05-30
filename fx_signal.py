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

# 注: リスク層 risk_filter は「状態変化時」にのみ遅延 import する（main 内）。
# トップレベルで import しないことで、万一リスク層側に不備があっても本体の
# 起動・シグナル通知を絶対に妨げない（グレースフルデグレードの境界を明確化）。

# ─────────────────────────────────────────────────────────────
# 設定（ここを変えれば挙動を調整できる）
# ─────────────────────────────────────────────────────────────
# 監視する通貨ペア（yfinanceティッカー）のリスト。増減はここだけで完結する。
# ラベル・小数桁・カレンダー対象通貨はティッカーから自動導出する（下のヘルパ参照）。
PAIRS = ["USDJPY=X", "AUDJPY=X"]
INTERVAL = "5m"           # 足種（例: "5m", "15m", "1h", "1d"）
PERIOD = "5d"             # 取得期間
SHORT_SMA = 20            # 短期SMAの本数
LONG_SMA = 50             # 長期SMAの本数
RSI_PERIOD = 14           # RSIの計算期間（参考情報）

# ヒステリシス（デッドバンド）: 短期SMAと長期SMAの乖離が長期SMA比でこの%未満の
# 間は、状態を切り替えず前回状態を維持する。SMAが拮抗して LONG/SHORT が交互に
# 揺れる「ダマシ(whipsaw)」を防ぐ。0.0 で無効（純粋なクロスのみ＝従来挙動）。
# バックテスト(3年・5分足・Dukascopy / USD/JPY・AUD/JPY)実測:
#   0.0%→約4.8回/日(1h未満のダマシ多発) / 0.05%→約1.7-2.1回/日 /
#   0.08%→約1.0-1.3回/日(1h未満のダマシ=0・3h未満も約6割減) / 0.10%→約0.7-1.0回/日。
# 0.08% が「ダマシ最小化 × 1日1回程度の実用頻度」のバランス。
HYSTERESIS_PCT = 0.08

# 最新足がこの分数より古ければ「市場クローズ中」とみなして何もせず終了する。
# 土日・祝日（クリスマスや年末年始など）はFX市場が止まり足が更新されないため、
# 無駄な判定をスキップする。yfinance の配信遅延を考慮して余裕を持たせた値。
# 0 で無効（常に判定する）。
STALE_MINUTES = 90

TOKYO = ZoneInfo("Asia/Tokyo")
WEBHOOK_ENV = "SLACK_WEBHOOK_URL"


# ─────────────────────────────────────────────────────────────
# 通貨ペアのメタ情報をティッカーから導出
# ─────────────────────────────────────────────────────────────
def pair_code(ticker: str) -> str:
    """'USDJPY=X' → 'USDJPY'。"""
    return ticker.replace("=X", "").upper()


def pair_label(ticker: str) -> str:
    """'USDJPY=X' → 'USD/JPY'。"""
    code = pair_code(ticker)
    return f"{code[:3]}/{code[3:6]}" if len(code) >= 6 else code


def pair_currencies(ticker: str) -> tuple[str, ...]:
    """'USDJPY=X' → ('USD', 'JPY')。リスク層のカレンダー対象通貨。"""
    code = pair_code(ticker)
    return (code[:3], code[3:6]) if len(code) >= 6 else (code,)


def pair_decimals(ticker: str) -> int:
    """表示小数桁。JPYクオート(例 159.255)は3桁、それ以外(例 1.16591)は5桁。"""
    code = pair_code(ticker)
    return 3 if code[3:6] == "JPY" else 5


def state_path(ticker: str) -> Path:
    """ペアごとの状態ファイル。'USDJPY=X' → state_USDJPY.json。"""
    return Path(__file__).with_name(f"state_{pair_code(ticker)}.json")


# ─────────────────────────────────────────────────────────────
# データ取得
# ─────────────────────────────────────────────────────────────
def fetch_data(ticker: str) -> pd.DataFrame:
    """yfinance から価格データを取得し、列を平坦化して返す。

    通信失敗や yfinance 側のエラーで例外が出てもジョブを落とさず、空の
    DataFrame を返して呼び出し側に「何もせず正常終了」させる。
    """
    try:
        df = yf.download(
            ticker,
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


def compute_change_pct(close: pd.Series, bars: int) -> float | None:
    """直近 bars 本前からの価格変化率(%)を返す。算出不能なら None。

    リスク層に渡す「介入リスク判断材料」としての決定論的な参考値。
    """
    if len(close) <= bars:
        return None
    old = close.iloc[-1 - bars]
    new = close.iloc[-1]
    if old == 0 or pd.isna(old) or pd.isna(new):
        return None
    return float((new - old) / old * 100.0)


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

    # 最新足の時刻（tz-aware に正規化）を取得
    last_ts = close.index[-1]
    if isinstance(last_ts, pd.Timestamp):
        last_ts = last_ts.tz_localize("UTC") if last_ts.tzinfo is None else last_ts
        last_ts = last_ts.tz_convert(TOKYO)

        # 最新足が古すぎる＝市場クローズ中（土日・祝日）なら何もせず終了
        if STALE_MINUTES > 0:
            age_min = (datetime.now(TOKYO) - last_ts).total_seconds() / 60.0
            if age_min > STALE_MINUTES:
                print(
                    f"最新足が {age_min:.0f}分前で古いため市場クローズ中と判断しスキップします。"
                )
                return None
        ts_str = last_ts.strftime("%Y-%m-%d %H:%M JST")
    else:
        ts_str = str(last_ts)

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

    return {
        "state": state,
        "price": float(close.iloc[-1]),
        "short_ma": float(short_ma),
        "long_ma": float(long_ma),
        "rsi": compute_rsi(close),
        # 直近の価格変化率（リスク層の介入リスク判断材料。決定論的に算出）
        "change_15m_pct": compute_change_pct(close, 3),
        "change_1h_pct": compute_change_pct(close, 12),
        "time": ts_str,
    }


# ─────────────────────────────────────────────────────────────
# 状態の保存・読み込み
# ─────────────────────────────────────────────────────────────
def load_state(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"{path.name} の読み込みに失敗: {exc}", file=sys.stderr)
        return None


def save_state(path: Path, info: dict) -> None:
    payload = {
        "state": info["state"],
        "price": info["price"],
        "time": info["time"],
        "updated_at": datetime.now(TOKYO).strftime("%Y-%m-%d %H:%M:%S JST"),
    }
    path.write_text(
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
        # exc 文字列には Webhook URL が含まれることがあるため出さない（機密漏洩防止）。
        # 漏れて困らない「種別 / ステータスコード」だけをログに出す。
        status = getattr(getattr(exc, "response", None), "status_code", None)
        detail = f"HTTP {status}" if status else type(exc).__name__
        print(f"Slack通知に失敗: {detail}（URLは伏せています）\n本文:\n{text}", file=sys.stderr)


def _risk_prefix(risk: dict | None) -> str:
    """リスク評価から、通知先頭に付ける警告ブロックを組み立てる。

    risk_level=high もしくは advise_caution=True の時だけ警告行を付ける。
    それ以外（low/medium/unknown/None）は空文字を返し、従来どおりの通知になる。
    """
    if not risk:
        return ""
    if risk.get("risk_level") != "high" and not risk.get("advise_caution"):
        return ""

    lines = [f"⚠️ {risk.get('headline') or 'リスク警戒'}"]
    events = risk.get("events") or []
    if events:
        lines.append("警戒イベント: " + " / ".join(events[:3]))
    reason = risk.get("reason")
    if reason:
        lines.append(reason)
    lines.append(f"（リスク: {risk.get('risk_level', 'unknown')}）")
    return "\n".join(lines) + "\n———\n"


# 「設定上は動くはずなのに失敗した」status。末尾フッターで可視化する。
_RISK_FAILED_STATUSES = (
    "calendar_unavailable",
    "calendar_schema",
    "llm_failed",
    "parse_failed",
)


def _risk_footer(risk: dict | None) -> str:
    """リスク層が想定外に失敗した時だけ、本体通知の末尾に控えめな1行を付ける。

    付ける: calendar_unavailable / calendar_schema / llm_failed / parse_failed
    付けない: ok / disabled / no_api_key（正常・意図的オフはノイズにしない）。
    high リスク用の先頭⚠️ブロック(_risk_prefix)とは別物。
    """
    if not risk:
        return ""
    if risk.get("status") in _RISK_FAILED_STATUSES:
        return "\nℹ️ リスク評価は取得できませんでした（本体シグナルは通常どおり）"
    return ""


def format_message(
    info: dict,
    *,
    label: str,
    decimals: int = 3,
    first_run: bool = False,
    risk: dict | None = None,
    info_only: bool = False,
) -> str:
    stance = "買い目線 📈" if info["state"] == "LONG" else "売り目線 📉"
    rsi = info["rsi"]
    rsi_str = f"{rsi:.1f}" if rsi is not None else "N/A"

    if first_run:
        header = f"🔔 {label} 監視を開始しました"
    elif info_only:
        # 高リスクのためシグナルを「情報のみ」に格下げ（発注は促さない）
        header = f"🛑 {label} シグナル【情報のみ・発注見送り推奨】"
    else:
        header = f"⚡ {label} シグナル変化"

    body = (
        f"{header}\n"
        f"状態: *{info['state']}*（{stance}）\n"
        f"価格: {info['price']:.{decimals}f}\n"
        f"SMA{SHORT_SMA}: {info['short_ma']:.{decimals}f}"
        f" / SMA{LONG_SMA}: {info['long_ma']:.{decimals}f}\n"
        f"RSI{RSI_PERIOD}: {rsi_str}\n"
        f"最新足: {info['time']}（{INTERVAL}）"
    )
    return _risk_prefix(risk) + body + _risk_footer(risk)


# ─────────────────────────────────────────────────────────────
# メイン
# ─────────────────────────────────────────────────────────────
def process_pair(ticker: str) -> None:
    """1通貨ペアを判定し、状態変化時のみ通知する。

    ペアごとに独立した state_<PAIR>.json で状態を管理する。
    1ペアの処理が例外を投げても、呼び出し側で他ペアは止めない。
    """
    label = pair_label(ticker)
    decimals = pair_decimals(ticker)
    currencies = pair_currencies(ticker)
    path = state_path(ticker)

    prev = load_state(path)
    prev_state = prev.get("state") if prev else None

    df = fetch_data(ticker)
    info = evaluate(df, prev_state=prev_state)

    if info is None:
        # データが空 or 本数不足（市場クローズ中など）はクラッシュさせず正常終了
        print(f"[{label}] データ不足のため判定をスキップ（市場クローズ中など）。")
        return

    if prev is None:
        # 初回起動: 監視開始を1回だけ通知して状態を保存
        notify(format_message(info, label=label, decimals=decimals, first_run=True))
        save_state(path, info)
        print(f"[{label}] 初回起動: 状態を {info['state']} で保存しました。")
        return

    if prev.get("state") != info["state"]:
        # 状態が変わった瞬間だけ通知。
        # ここでのみリスク層を呼ぶ（＝LLM呼び出しは状態変化時に限定しコスト最小化）。
        # リスク層が何を返しても/失敗しても、本体のシグナル通知は必ず出す。
        # import 自体も try 内に置き、モジュール不備でも本体を止めない。
        risk = None
        suppress = False
        try:
            import risk_filter  # 遅延 import（付加層）
            risk = risk_filter.assess_risk(
                info, currencies=currencies, pair_label=label
            )
            suppress = risk_filter.RISK_SUPPRESS_SIGNALS
        except Exception as exc:  # noqa: BLE001 - 付加層の失敗で本体を止めない
            print(f"[{label}] リスク層でエラー（リスク不明で続行）: {exc}", file=sys.stderr)
            # モジュール不備等でassess_riskに到達できなくても、末尾フッターで可視化する
            risk = {"status": "llm_failed", "risk_level": "unknown", "available": False}

        info_only = bool(risk and suppress and risk.get("risk_level") == "high")
        notify(format_message(info, label=label, decimals=decimals, risk=risk, info_only=info_only))
        save_state(path, info)
        rlevel = risk.get("risk_level") if risk else "unknown"
        rstatus = risk.get("status") if risk else "unknown"
        print(
            f"[{label}] 状態変化: {prev.get('state')} → {info['state']}"
            f" / リスク: {rlevel} (status={rstatus})"
        )
    else:
        # 同じ状態が続く間は通知しない（チャタリング防止）
        print(f"[{label}] 状態変化なし: {info['state']}（通知スキップ）")


def main() -> int:
    # 各ペアを独立処理。1ペアの失敗が他ペアの監視を止めないようにする。
    for ticker in PAIRS:
        try:
            process_pair(ticker)
        except Exception as exc:  # noqa: BLE001 - 1ペアの失敗で全体を止めない
            print(f"[{pair_label(ticker)}] 処理中にエラー（スキップ）: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
