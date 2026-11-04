"""5分足ショート売買シグナル（USD/JPY・AUD/JPY）。`python fx_signal.py --short` で起動。

ロング保護（日足）とは独立に動く短期ショート専用の合図。SHORT（売り建て）と FLAT（買い戻し）の
切り替わりを Slack に通知する。cron は平日 5 分おきを想定（deploy/crontab.example）。

規則（決定論的・先読みなし。backtest と本体が同じ short_states を使う）
  - 5分足終値の SMA50 < SMA100 で SHORT、SMA50 > SMA100 で FLAT。
  - 乖離が ATR(14)×ATR_K 未満のデッドバンド内では前回状態を維持（fx_signal と同じ deadband_threshold）。
  - 環境足ゲート（ENV_GATE="1h"）: 1時間足にダウ理論（dow_guard）を上下反転して流し、
    「下降ダウ継続（安値更新後、実質戻り高値を終値で超えていない）」の間だけ SHORT を許す。
    環境足が下降でなければ FLAT（買い戻し）。実質戻り高値は損切りの目安として通知に載せる。
  - 最新の確定 5分足が STALE_MINUTES より古ければ市場クローズ中とみなしスキップ。
  - 損切りライン（SHORT_STOP_PAIRS のペアのみ、既定 USDJPY）: ショート開始足の終値 + SHORT_STOP_ATR×ATR(14)
    を建値基準の損切りラインとして通知に載せ、以後の確定 5分足の高値がラインに達したら FLAT（損切り）を通知する。
    損切り後は SMA/環境足の条件が一度 FLAT に戻るまで再エントリーしない（backtest と同じ）。

較正（Dukascopy 5分足 3年 2023-06〜2026-06、翌足始値執行、往復スプレッド USD 0.3 / AUD 0.7 pips、
ショートの支払いスワップ USD 1.3 / AUD 0.8 pips/日 を保有時間按分で控除）
  USD/JPY  ゲートなし +1618 / DD 946 / 往復 697、 1hゲート +1228 / DD 824 / 往復 477（4年すべてプラス）
  AUD/JPY  ゲートなし -1059 / DD 1927 / 往復 673、 1hゲート  -590 / DD 1770 / 往復 471（3年中2年マイナス）
  → AUD/JPY は検証上マイナス。ユーザー判断で両ペアに適用（情報提供として使う）。
  ダウ鏡像を 5分/15分足の判定に使う構成は両ペアとも大きくマイナス（-561〜-4614）だった。
  出口の追加検証（入口固定、2026-11-04）: USD/JPY は損切り 3×ATR で +1553（4年すべてプラス、年ごとの偏りが減る）、
  5×ATR で +1639。利確・トレーリング・時間帯限定は改善しないか不安定。AUD/JPY はどの出口でもマイナスのまま
  （損切り 3×ATR は -1095 に悪化）なので損切りは USD/JPY のみ既定で有効。
  エントリー時の SMA50 乖離フィルタ（見送り/待ち、1〜6×ATR・10〜40pips）は両ペアとも効果なし。

データ源: TWELVEDATA_API_KEY があれば Twelve Data（5分足・1時間足、週末の偽足は除外）。
無ければ yfinance（5分足 5d、1時間足 1y）。無料枠（800/日）に収めるため、1時間足は
ENV_REFRESH_MIN 分に 1 回だけ取得し short_env_<PAIR>.json にキャッシュする。
  消費目安: 5分足 2ペア×288回 = 576 + 1時間足 2ペア×24回 = 48 → 624/日（日足ガード分 +36）。
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

import fx_signal as fx
import twelvedata as td

STATE_SHORT = "SHORT"
STATE_FLAT = "FLAT"
STATES = (STATE_SHORT, STATE_FLAT)

DEFAULT_PAIRS = ["USDJPY=X", "AUDJPY=X"]
SHORT_INTERVAL = "5min"      # 判定足
SHORT_BARS = 400             # 取得本数（SMA100 + ATR14 に十分。1 クレジット）
ENV_INTERVAL = "1h"          # 環境足
ENV_BARS = 5000              # 1時間足の取得本数（約 7 か月。ダウの初期化に十分）
ENV_GATE_DEFAULT = "1h"      # "1h" or "none"（環境変数 SHORT_ENV_GATE で上書き）
ENV_REFRESH_MIN = 55         # 環境足の再取得間隔（分）。1時間足は 1 時間に 1 本しか増えない
STALE_MINUTES = 30           # 最新確定 5分足がこれより古ければ市場クローズ中とみなす
STOP_ATR_DEFAULT = 3.0       # 損切り幅（ATR 倍率）。環境変数 SHORT_STOP_ATR で上書き
STOP_PAIRS_DEFAULT = ["USDJPY=X"]   # 損切りを適用するペア。環境変数 SHORT_STOP_PAIRS（空なら無効）
SMA_SHORT = fx.SHORT_SMA     # 50
SMA_LONG = fx.LONG_SMA       # 100
ATR_PERIOD = fx.ATR_PERIOD   # 14

TOKYO = fx.TOKYO


def short_pairs() -> list[str]:
    env = os.environ.get("SHORT_PAIRS")
    if env is None or not env.strip():
        return list(DEFAULT_PAIRS)
    return [p.strip().upper().replace("=X", "") + "=X" for p in env.split(",") if p.strip()]


def env_gate() -> str:
    v = (os.environ.get("SHORT_ENV_GATE") or ENV_GATE_DEFAULT).strip().lower()
    return "none" if v in ("none", "off", "0", "") else "1h"


def stop_atr_for(ticker: str) -> float | None:
    """このペアの損切り幅（ATR 倍率）。対象外・0 以下なら None（損切りなし）。"""
    env = os.environ.get("SHORT_STOP_PAIRS")
    if env is None:
        pairs = list(STOP_PAIRS_DEFAULT)
    else:
        pairs = [p.strip().upper().replace("=X", "") + "=X" for p in env.split(",") if p.strip()]
    if ticker.upper() not in pairs:
        return None
    try:
        k = float(os.environ.get("SHORT_STOP_ATR") or STOP_ATR_DEFAULT)
    except ValueError:
        k = STOP_ATR_DEFAULT
    return k if k > 0 else None


def state_path(ticker: str) -> Path:
    return Path(__file__).with_name(f"short_state_{fx.pair_code(ticker)}.json")


def env_cache_path(ticker: str) -> Path:
    return Path(__file__).with_name(f"short_env_{fx.pair_code(ticker)}.json")


# ─────────────────────────────────────────────────────────────
# 規則本体（backtest と共用）
# ─────────────────────────────────────────────────────────────
def short_states(df: pd.DataFrame) -> pd.Series:
    """各足の SHORT/FLAT（判定不能は None）。SMA50/100 + ATR デッドバンドを逐次処理する。"""
    close = df["Close"]
    s = close.rolling(SMA_SHORT).mean().to_numpy()
    l = close.rolling(SMA_LONG).mean().to_numpy()
    atr = fx.compute_atr(df, ATR_PERIOD).to_numpy() if {"High", "Low"}.issubset(df.columns) else None
    out: list[str | None] = []
    prev: str | None = None
    for i in range(len(close)):
        if pd.isna(s[i]) or pd.isna(l[i]):
            out.append(None)
            continue
        raw = STATE_SHORT if s[i] < l[i] else STATE_FLAT
        a = None if atr is None or pd.isna(atr[i]) else float(atr[i])
        if prev in STATES and abs(s[i] - l[i]) < fx.deadband_threshold(float(l[i]), a):
            st = prev
        else:
            st = raw
        out.append(st)
        prev = st
    return pd.Series(out, index=df.index, dtype=object)


def mirror(bars: pd.DataFrame) -> pd.DataFrame:
    """上下反転（ダウ理論の鏡像用）。High/Low を入れ替えて符号反転する。"""
    return pd.DataFrame({
        "Open": -bars["Open"], "High": -bars["Low"], "Low": -bars["High"], "Close": -bars["Close"],
    }, index=bars.index)


def env_downtrend(bars_1h: pd.DataFrame) -> dict | None:
    """1時間足の鏡像ダウ判定。返り値 {"down": bool, "reentry_high": float|None, "date": str|None}。
    初期化前・本数不足なら None。"""
    import dow_guard as dg
    if bars_1h is None or len(bars_1h) < dg.MIN_BARS:
        return None
    res = dg.analyze(mirror(bars_1h))
    if res["trend"] is None:
        return None
    down = res["trend"] == dg.STATE_HOLD
    kl = res["key_low"]        # 鏡像の実質押し安値 = 実際の実質戻り高値（下降中の終了ライン）
    rh = res["reentry_high"]   # 鏡像の復帰ライン = 実際の「下降再開ライン」（この安値を終値で割ると下降再開）
    return {
        "down": down,
        "reentry_high": (-kl.price) if (down and kl) else None,
        "date": kl.date.strftime("%Y-%m-%d %H:%M") if (down and kl) else None,
        "resume_low": (-rh.price) if (not down and rh) else None,
        "resume_date": rh.date.strftime("%Y-%m-%d %H:%M") if (not down and rh) else None,
        "bar": bars_1h.index[-1].strftime("%Y-%m-%d %H:%M"),
    }


def env_downtrend_series(bars_1h: pd.DataFrame) -> pd.Series:
    """backtest 用: 各 1時間足終了時点の「下降ダウ継続」bool 列。"""
    import dow_guard as dg
    st = dg.analyze(mirror(bars_1h))["states"]
    return st == dg.STATE_HOLD


# ─────────────────────────────────────────────────────────────
# データ取得
# ─────────────────────────────────────────────────────────────
def _naive_utc(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    out = df[[c for c in ("Open", "High", "Low", "Close") if c in df.columns]].copy()
    idx = out.index
    if getattr(idx, "tz", None) is not None:
        out.index = idx.tz_convert("UTC").tz_localize(None)
    return out


def fetch_bars(ticker: str, now_utc: datetime | None = None) -> pd.DataFrame:
    """確定済み 5分足（naive UTC）。Twelve Data → 無ければ yfinance。"""
    df = td.fetch_ohlc(ticker, SHORT_INTERVAL, SHORT_BARS)
    source = "twelvedata"
    if df is None or df.empty:
        df = _naive_utc(fx.fetch_data(ticker, interval="5m", period="5d"))
        source = "yfinance"
    if df is None or df.empty:
        return pd.DataFrame()
    df = td.drop_weekend(df)
    df = td.confirmed_only(df, SHORT_INTERVAL, now_utc)
    df.attrs["source"] = source
    return df


def fetch_env_bars(ticker: str) -> pd.DataFrame:
    df = td.fetch_ohlc(ticker, ENV_INTERVAL, ENV_BARS)
    if df is None or df.empty:
        df = _naive_utc(fx.fetch_data(ticker, interval="1h", period="1y"))
    if df is None or df.empty:
        return pd.DataFrame()
    df = td.drop_weekend(df)
    return td.confirmed_only(df, ENV_INTERVAL)


def load_env(ticker: str, now_utc: datetime | None = None) -> dict | None:
    """環境足判定をキャッシュ付きで返す。ENV_REFRESH_MIN 分以内のキャッシュがあれば再取得しない。"""
    now = now_utc or datetime.now(timezone.utc)
    path = env_cache_path(ticker)
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
        at = datetime.fromisoformat(cached["fetched_at"])
        if (now - at).total_seconds() < ENV_REFRESH_MIN * 60:
            return cached.get("env")
    except (FileNotFoundError, KeyError, ValueError, OSError, json.JSONDecodeError):
        pass
    bars = fetch_env_bars(ticker)
    env = env_downtrend(bars) if bars is not None and not bars.empty else None
    try:
        path.write_text(json.dumps({"fetched_at": now.isoformat(), "env": env}, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    except OSError as exc:
        print(f"環境足キャッシュの保存に失敗: {exc}", file=sys.stderr)
    return env


# ─────────────────────────────────────────────────────────────
# 判定
# ─────────────────────────────────────────────────────────────
def evaluate(df: pd.DataFrame, env: dict | None, gate: str = "1h",
             now_utc: datetime | None = None) -> dict | None:
    """確定 5分足と環境足判定から SHORT/FLAT を返す。データ不足・鮮度不良・ゲート判定不能なら None。"""
    if df is None or df.empty or len(df) < SMA_LONG:
        return None
    now = now_utc or datetime.now(timezone.utc)
    last = df.index[-1]
    age_min = (pd.Timestamp(now.astimezone(timezone.utc)).tz_localize(None) - last).total_seconds() / 60.0
    if STALE_MINUTES > 0 and age_min > STALE_MINUTES:
        print(f"最新確定 5分足が {age_min:.0f}分前で古いため市場クローズ中と判断しスキップします。")
        return None
    st = short_states(df)
    raw = st.iloc[-1]
    if raw not in STATES:
        return None
    gated = raw
    if gate == "1h":
        if env is None:
            return None  # 環境足が判定不能なら合図を出さない
        if not env.get("down"):
            gated = STATE_FLAT
    close = df["Close"]
    sma_s = float(close.rolling(SMA_SHORT).mean().iloc[-1])
    sma_l = float(close.rolling(SMA_LONG).mean().iloc[-1])
    return {
        "state": gated,
        "raw_state": raw,
        "price": float(close.iloc[-1]),
        "sma_short": sma_s,
        "sma_long": sma_l,
        "atr": float(fx.compute_atr(df, ATR_PERIOD).iloc[-1]),
        "env_down": None if env is None else bool(env.get("down")),
        "env_reentry_high": None if env is None else env.get("reentry_high"),
        "env_reentry_date": None if env is None else env.get("date"),
        "env_resume_low": None if env is None else env.get("resume_low"),
        "env_resume_date": None if env is None else env.get("resume_date"),
        "gate": gate,
        "source": df.attrs.get("source"),
        "time": last.tz_localize("UTC").tz_convert(TOKYO).strftime("%Y-%m-%d %H:%M JST") + " 確定5分足",
    }


def apply_stop(info: dict, prev: dict | None, df: pd.DataFrame, stop_atr: float | None) -> dict:
    """損切りの状態遷移を info に反映して返す（prev は前回保存状態。None/初回なら何もしない）。

    - 前回 SHORT で損切りライン保持中: 建玉足より後の確定 5分足の高値がラインに達していれば
      FLAT（exit_reason="stop"、stopped=True）。達していなければ建値・ラインを引き継ぐ。
    - 前回が損切り後の待機（stopped=True）: 規則がまだ SHORT なら FLAT のまま（再エントリー禁止）、
      規則が FLAT に戻れば待機を解除する。
    - 新規 SHORT（前回 FLAT → 今回 SHORT）: 建値＝開始足の終値、ライン＝建値 + stop_atr×ATR。
    """
    info.setdefault("stopped", False)
    if not prev or prev.get("state") not in STATES:
        return info
    prev_state = prev["state"]
    if info["state"] == STATE_SHORT and prev_state == STATE_SHORT:
        line, entry_bar = prev.get("stop_line"), prev.get("entry_bar")
        for k in ("entry_price", "entry_bar", "stop_line", "stop_atr"):
            info[k] = prev.get(k)
        if line is not None and entry_bar:
            after = df[df.index > pd.Timestamp(entry_bar)]
            if not after.empty and float(after["High"].max()) >= float(line):
                info["state"] = STATE_FLAT
                info["exit_reason"] = "stop"
                info["stop_hit_high"] = float(after["High"].max())
                info["stopped"] = True
        return info
    if info["state"] == STATE_SHORT and prev_state == STATE_FLAT:
        if prev.get("stopped"):
            info["state"] = STATE_FLAT          # 損切り後の待機中は再エントリーしない
            info["stopped"] = True
            return info
        if stop_atr is not None and info.get("atr") is not None:
            info["entry_price"] = info["price"]
            info["entry_bar"] = df.index[-1].strftime("%Y-%m-%dT%H:%M")
            info["stop_atr"] = stop_atr
            info["stop_line"] = info["price"] + stop_atr * info["atr"]
        return info
    # 今回 FLAT（規則による手仕舞い、または待機継続）
    info["stopped"] = False
    if prev_state == STATE_SHORT:
        info["exit_reason"] = "rule"
    return info


# ─────────────────────────────────────────────────────────────
# 通知文
# ─────────────────────────────────────────────────────────────
def _env_line(info: dict, decimals: int) -> str:
    if info.get("gate") != "1h":
        return "環境足: 使用しない"
    if info.get("env_down"):
        line = "環境足 1h: 下降ダウ継続 ⬇️"
        rh = info.get("env_reentry_high")
        if rh is not None:
            line += f" / 実質戻り高値 {rh:.{decimals}f}（{info.get('env_reentry_date')}）— ここを 1h 終値で超えたら下降終了"
        return line
    line = "環境足 1h: 下降ダウではない（売り禁止）"
    rl = info.get("env_resume_low")
    if rl is not None:
        line += f" / 下降再開ライン {rl:.{decimals}f}（{info.get('env_resume_date')}）— ここを 1h 終値で割ったら売り許可"
    else:
        line += "（崩れ後の戻り安値の形成待ち）"
    return line


def format_message(info: dict, *, label: str, decimals: int = 3, first_run: bool = False) -> str:
    st = info["state"]
    rule = f"5分足 SMA{SMA_SHORT}/{SMA_LONG}・ATR帯 ＋ 1h ダウ鏡像ゲート" if info.get("gate") == "1h" \
        else f"5分足 SMA{SMA_SHORT}/{SMA_LONG}・ATR帯"
    sma = (f"終値 {info['price']:.{decimals}f} / SMA{SMA_SHORT} {info['sma_short']:.{decimals}f}"
           f" / SMA{SMA_LONG} {info['sma_long']:.{decimals}f} / ATR {info['atr']:.{decimals}f}")
    env = _env_line(info, decimals)
    if first_run:
        stance = "売り建て中 🔻" if st == STATE_SHORT else "待機（ポジションなし） ⚪"
        return (f"🔔 {label} 5分足ショート監視を開始しました\n現在: *{st}*（{stance}）\n{sma}\n{env}\n"
                f"判定: {rule} / {info['time']}")
    if st == STATE_SHORT:
        stop = ""
        exits = "SMA 再クロス、または環境足の下降終了で通知"
        if info.get("stop_line") is not None:
            stop = (f"損切り目安 {info['stop_line']:.{decimals}f}（建値 {info['entry_price']:.{decimals}f}"
                    f" + {info.get('stop_atr', STOP_ATR_DEFAULT):g}×ATR）— 確定5分足の高値が達したら通知\n")
            exits = "SMA 再クロス・環境足の下降終了・損切りライン到達で通知"
        return (f"🔻 {label} ショート開始シグナル（5分足 SMA{SMA_SHORT} が SMA{SMA_LONG} を下抜け）\n{sma}\n{env}\n"
                f"{stop}→ 売り建てを検討。買い戻しは {exits}\n{info['time']}")
    if info.get("exit_reason") == "stop":
        return (f"🛑 {label} ショート損切りシグナル（損切りライン {info['stop_line']:.{decimals}f} に高値"
                f" {info['stop_hit_high']:.{decimals}f} が到達）\n{sma}\n{env}\n"
                f"→ 売り建ての買い戻しを検討。SMA/環境足が一度 FLAT に戻るまで再エントリーの合図は出しません\n{info['time']}")
    why = "SMA 再クロス" if info.get("raw_state") == STATE_FLAT else "環境足 1h の下降ダウ終了"
    return (f"⚪ {label} ショート手仕舞いシグナル（{why}）\n{sma}\n{env}\n"
            f"→ 売り建ての買い戻しを検討\n{info['time']}")


# ─────────────────────────────────────────────────────────────
# 1ペア処理・main
# ─────────────────────────────────────────────────────────────
def process_pair(ticker: str) -> str:
    label = fx.pair_label(ticker)
    decimals = fx.pair_decimals(ticker)
    path = state_path(ticker)
    gate = env_gate()

    prev = fx.load_state(path)
    prev_state = prev.get("state") if prev and prev.get("state") in STATES else None

    df = fetch_bars(ticker)
    if df is None or df.empty:
        print(f"[{label}] 5分足データ取得に失敗（空の応答）。")
        return "fetch_failed"
    env = load_env(ticker) if gate == "1h" else None
    info = evaluate(df, env, gate)
    if info is None:
        print(f"[{label}] ショート判定スキップ（本数不足/鮮度不良/環境足判定不能）。")
        return "no_data"
    info["strategy"] = "short_5m"

    if prev_state is None:
        info.setdefault("stopped", False)
        fx.notify(format_message(info, label=label, decimals=decimals, first_run=True))
        _save(path, info)
        print(f"[{label}] ショート監視 初回起動: 状態を {info['state']} で保存しました（{info['source']}）。")
        return "first_run"
    apply_stop(info, prev, df, stop_atr_for(ticker))
    if prev_state == info["state"]:
        if bool(prev.get("stopped")) != bool(info.get("stopped")):
            _save(path, info)   # 損切り後の待機解除（通知なし）
        note = "・損切り後の待機中" if info.get("stopped") else ""
        print(f"[{label}] ショート 状態変化なし: {info['state']}（通知スキップ{note}・{info['source']}）")
        return "nochange"
    fx.notify(format_message(info, label=label, decimals=decimals))
    _save(path, info)
    why = "損切り" if info.get("exit_reason") == "stop" else ""
    print(f"[{label}] ショート 状態変化: {prev_state} → {info['state']}{why}（{info['source']}）")
    return "changed"


def _save(path: Path, info: dict) -> None:
    payload = {k: info.get(k) for k in ("state", "raw_state", "price", "sma_short", "sma_long",
                                        "env_down", "env_reentry_high", "env_resume_low", "gate", "source", "time",
                                        "entry_price", "entry_bar", "stop_line", "stop_atr", "stopped", "exit_reason")}
    payload["strategy"] = "short_5m"
    payload["updated_at"] = datetime.now(TOKYO).strftime("%Y-%m-%d %H:%M:%S JST")
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    outcomes: list[str] = []
    for ticker in short_pairs():
        try:
            outcomes.append(process_pair(ticker))
        except Exception as exc:  # noqa: BLE001 - 1ペアの失敗で全体を止めない
            print(f"[{fx.pair_label(ticker)}] ショート処理中にエラー（スキップ）: {exc}", file=sys.stderr)
            outcomes.append("error")
    return 0


if __name__ == "__main__":
    sys.exit(main())
