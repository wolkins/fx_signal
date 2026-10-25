#!/usr/bin/env python3
"""ロング保護ガード（日足ダウ理論）.

ダウ理論（実質押し安値・戻り高値・6本のタメ）の規則を、確定日足に対するロング専用の状態機械として実装する。
ドテン(売り)は提案しない。long_guard.py（SMA50・帯）と同じインターフェースを持ち、
fx_signal.py はペアごとにどちらのエンジンを使うかを GUARD_BY_PAIR で選ぶ。

規則（決定論的・先読みなし。本の記述をユーザー要約から機械化）:
  - 山/谷: ピボット（左右 PIVOT_LR 本より厳密に高い/安い。同値タイは不採用）で取り、
    直前の山谷から MIN_WAVE 本未満で反転した小さな波は数えない（＝「大きく数える」）。
    同種が連続したら極値側で置き換える。ピボットは右 PIVOT_LR 本が確定してから採用（先読みなし）。
  - ゾーン抜け: 終値がゾーン外側の端（高値側はヒゲ先 High、安値側はヒゲ先 Low）を抜けて確定。
  - 上昇開始/継続: 直近の小山（まだ抜かれていない山）の High を終値で超えた時に UP。
    その時に「高値を付けさせた起点の安値」＝直近の確定した谷を **実質押し安値** に切り上げる。
    高値更新前にできた押し目の安値では更新しない。
  - ダウ崩れ(EXIT): 終値 < 実質押し安値（ヒゲ先）。実質戻り高値 = 安値を割らせた起点の高値。
    ヒゲだけの一時的な割れ（オーバーシュート）は崩れとみなさない。
  - 復帰(HOLD): 崩れた「後」に新しく形成された一番近い小山の High を終値で超えた時。
    崩れる前の大きな高値ではない（早く入れて損切り幅が小さい、が本の趣旨）。
  - 調整期間（レンジ）: 崩れない限り保有。分割決済（半分）は通知文の目安として添えるだけ。
  - 環境足: 本体では使わない（3年検証で週足環境足は日足単体と結果が同じだったため）。

較正（2026-10-26 更新。ロング専用・翌日始値で執行・往復0.5pips控除・UTC 日足・日曜端数除外。
python backtest.py --guard --engine both）:
  3年 Dukascopy 2023-06〜2026-06: USD/JPY +2132/DD1360, AUD/JPY +2637/DD1311, NZD/JPY -228/DD1764
    （買い持ち +1516/2081, +2008/2228, +722/1842。SMA50帯2%: +1230/974, +2273/1102, +168/968）
  直近2年 yfinance 1h→日足 2024-10〜2026-10: USD/JPY -350/1365, AUD/JPY +986/1542, NZD/JPY -226/1181
    （SMA: -336/980, +1654/419, -662/1099。買い持ち: -46/1735, +1295/1183, +140/873）
  → NZD/JPY（両期間）と AUD/JPY（直近2年）で最大DDを買い持ちから減らせておらず、保護の目的を
    満たさない。日曜の端数足を日足に含めるかどうかで NZD/JPY 3年が +992/674 ⇔ -228/1764 と
    符号ごと変わるほど日足の作り方に敏感で、頑健とは言えない。
  ※ それ以前の較正（AUD/NZD でダウが優位）は yfinance 日足の Close 不具合（前日終値）と日曜端数足を
    含んでおり無効。既定割り当て AUD/JPY・NZD/JPY=ダウ は SMA へ戻すことを推奨（README 参照）。
  WAVE_RULE="hill"（短い戻りの山も数える）やふた山・週足環境・SMA環境の併用も、旧データで SMA を
  安定して上回らなかった（WAVE_RULE の注記参照）。
  本のデイトレ構成（4H環境足・15分足判定）は3年で往復370回超＝スワップ保有には不適。
  MIN_WAVE を 0 にする（大きく数えない）と NZD/JPY は 3年で -632 に崩れる。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import pandas as pd

# ─────────────────────────────────────────────────────────────
# 設定
# ─────────────────────────────────────────────────────────────
PIVOT_LR = 3              # 山谷の確定: 左右この本数より厳密に高い/安い（山全体で約7本）
MIN_WAVE = 6              # 「6本のタメ」の本数（下の WAVE_RULE で測り方を選ぶ）
# 「6本のタメ」の測り方（本の記述は両方に読める。較正で安定したのは "leg"）:
#   "leg" : 直前の谷(山)から新しい山(谷)まで MIN_WAVE 本以上かけて形成された山谷だけを「明確な山谷」
#           として数える。短い戻りの山は数えないので、崩れ後の復帰ラインは「6本以上かけた戻り高値」
#           ができるまで古い戻り高値のまま残る（＝下落中の小さな戻りでは復帰しない。保守的）。
#   "hill": 谷→山→谷の全体幅が MIN_WAVE 本以上なら、短い戻りの山も数える（構造の追従が速い）。
#           3年＋直近2年の較正では NZD/JPY が崩れ、SMA50帯2% を安定して上回らなかった。
WAVE_RULE = "leg"
DOUBLE_BOTTOM = False     # True: 崩れ後の復帰に2番底（谷2つ）を要求する（本の「ふた山」）
GUARD_INTERVAL = "1h"     # 取得する元足。fx_signal.to_daily_utc で UTC 日足にまとめる
                          # （yfinance の "1d" は Close が前日終値になっており使えない）
GUARD_PERIOD = "2y"       # 状態機械の初期化に十分な履歴（最初の明確な高値超え/安値割れが要る）
GUARD_STALE_DAYS = 5      # 最新確定足がこれより古ければスキップ（長期休場/データ源異常）
MIN_BARS = 60             # これ未満の日足本数では判定しない

STATE_HOLD = "HOLD"       # ロング維持（上昇ダウ継続）
STATE_EXIT = "EXIT"       # ロング離脱（ダウ崩れ中・待機）
STATES = (STATE_HOLD, STATE_EXIT)
ENGINE = "dow"


# ─────────────────────────────────────────────────────────────
# 確定日足の抽出（OHLC）
# ─────────────────────────────────────────────────────────────
def confirmed_daily_ohlc(df: pd.DataFrame, now_utc: datetime | None = None) -> pd.DataFrame:
    """日足 DataFrame から「確定済み」の OHLC を返す（index は naive の日付）。

    当日分は進行中バーなので、日付が今日(UTC)より前のバーだけを採用。
    """
    cols = ["Open", "High", "Low", "Close"]
    if df is None or df.empty or any(c not in df.columns for c in cols):
        return pd.DataFrame(columns=cols)
    d = df[cols].dropna()
    if d.empty:
        return d
    idx = pd.to_datetime(d.index)
    if idx.tz is not None:
        idx = idx.tz_convert("UTC").tz_localize(None)
    d = d.copy()
    d.index = idx.normalize()
    d = d[~d.index.duplicated(keep="last")].astype(float)
    today = pd.Timestamp((now_utc or datetime.now(timezone.utc)).date())
    return d[d.index < today]


# ─────────────────────────────────────────────────────────────
# 山谷（ピボット）
# ─────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Pivot:
    pos: int           # バー位置
    kind: str          # "H" / "L"
    price: float       # ゾーン外側の端（H=High, L=Low）
    date: pd.Timestamp


def find_pivots(bars: pd.DataFrame, lr: int = PIVOT_LR) -> list[Pivot]:
    """左右 lr 本より厳密に高い/安い点。pos+lr 本目で確定する（それまで採用しない）。"""
    h, lo = bars["High"].to_numpy(), bars["Low"].to_numpy()
    idx = bars.index
    out: list[Pivot] = []
    for i in range(lr, len(h) - lr):
        hw, lw = h[i - lr:i + lr + 1], lo[i - lr:i + lr + 1]
        if h[i] == hw.max() and (hw == h[i]).sum() == 1:
            out.append(Pivot(i, "H", float(h[i]), idx[i]))
        elif lo[i] == lw.min() and (lw == lo[i]).sum() == 1:
            out.append(Pivot(i, "L", float(lo[i]), idx[i]))
    return out


# ─────────────────────────────────────────────────────────────
# 状態機械
# ─────────────────────────────────────────────────────────────
def analyze(bars: pd.DataFrame, lr: int = PIVOT_LR, min_wave: int = MIN_WAVE,
            double_bottom: bool | None = None, wave_rule: str | None = None) -> dict:
    """確定日足 OHLC を先頭から逐次処理し、各日の状態列と最終コンテキストを返す。

    double_bottom=True なら、崩れ後の復帰に「谷が2つ確定している（ふた山/2番底）」ことを要求する
    （本の「1山目で焦って入らず2番底を確認」）。None なら DOUBLE_BOTTOM 定数に従う。

    返り値:
      states: pd.Series（各日の HOLD/EXIT。初期化前は None）
      trend:  最終状態（HOLD/EXIT/None）
      key_low:  実質押し安値 Pivot（HOLD 中の離脱ライン）
      key_high: 実質戻り高値 Pivot（参考）
      reentry_high: 崩れ後にできた小山 Pivot（EXIT 中の復帰ライン。未形成なら None）
      last_event: (日付, 状態) 直近の切替
    オンライン処理なので、系列を途中で切っても過去の値は変わらない（先読みなし）。
    """
    if double_bottom is None:
        double_bottom = DOUBLE_BOTTOM
    if wave_rule is None:
        wave_rule = WAVE_RULE
    close = bars["Close"].to_numpy()
    n = len(close)
    piv = find_pivots(bars, lr)
    lows_since_break = 0
    sw: list[Pivot] = []
    trend: str | None = None
    key_low: Pivot | None = None
    key_high: Pivot | None = None
    pending_high: Pivot | None = None   # まだ終値で抜かれていない直近の小山
    pending_low: Pivot | None = None
    break_pos = -1
    last_event: tuple | None = None
    pi = 0
    states: list[str | None] = []

    tentative: Pivot | None = None  # 直前の山谷から min_wave 未満で反転した「仮の」山谷

    def more_extreme(a: Pivot, b: Pivot) -> bool:
        return a.price > b.price if a.kind == "H" else a.price < b.price

    def adopt(p: Pivot) -> list[Pivot]:
        """交互性と「6本のタメ」(WAVE_RULE) を適用し、新たに採用した山谷を返す（0〜2個）。

        "leg": 直前の山谷から min_wave 本以上かけてできた山谷だけを採用。同種が連続したら極値側で置換。
        "hill": 直前の山谷から min_wave 未満で反転した山は仮採用し、次の谷までの全体幅が
                min_wave 以上なら山と谷をまとめて採用する。
        """
        nonlocal tentative
        if not sw:
            sw.append(p)
            return [p]
        last = sw[-1]
        if wave_rule == "leg":
            if p.kind == last.kind:
                if more_extreme(p, last):
                    sw[-1] = p
                    return [p]
                return []
            if p.pos - last.pos < min_wave:
                return []                              # 短い波の山谷は「明確」でない → 数えない
            sw.append(p)
            return [p]
        if p.kind == last.kind:
            if tentative is not None:
                if p.pos - last.pos >= min_wave:       # 谷→(仮)山→谷 の全体が十分な幅 → まとめて採用
                    sw.extend([tentative, p])
                    out = [tentative, p]
                    tentative = None
                    return out
                if more_extreme(p, last):              # 小さな波のまま谷が更新 → 谷を置換、仮山は捨てる
                    sw[-1] = p
                    tentative = None
                    return [p]
                return []                              # 小さな波: 何も変えない（仮山は保持）
            if more_extreme(p, last):
                sw[-1] = p
                return [p]
            return []
        # 反対種
        if tentative is not None and tentative.kind == p.kind:
            if p.pos - last.pos >= min_wave:           # 仮山より後にできた山が十分な幅 → 正式採用
                sw.append(p)
                tentative = None
                return [p]
            if more_extreme(p, tentative):
                tentative = p                          # 仮山を更新（より高い/安い）
            return []
        if p.pos - last.pos < min_wave:
            tentative = p                              # 仮採用（次の谷まで保留）
            return []
        sw.append(p)
        return [p]

    for i in range(n):
        while pi < len(piv) and piv[pi].pos + lr <= i:   # i 本目で確定した山谷
            p = piv[pi]
            pi += 1
            for a in adopt(p):
                if a.kind == "H":
                    if a.pos > break_pos:                 # 崩れた後の小山だけが復帰の基準
                        pending_high = a
                else:
                    pending_low = a
                    if trend == STATE_EXIT and a.pos > break_pos:
                        lows_since_break += 1

        c = close[i]
        last_low = next((s for s in reversed(sw) if s.kind == "L"), None)
        last_high = next((s for s in reversed(sw) if s.kind == "H"), None)

        if pending_high is not None and c > pending_high.price:
            # 直近高値超え確定 → UP。高値を付けさせた起点の安値を実質押し安値に切り上げ
            if trend == STATE_EXIT and double_bottom and lows_since_break < 2:
                pending_high = None                       # ふた山未形成: 次の小山まで待つ
            else:
                if trend != STATE_HOLD:
                    trend = STATE_HOLD
                    last_event = (bars.index[i], STATE_HOLD)
                if last_low is not None:
                    key_low = last_low
                pending_high = None
        elif trend == STATE_HOLD and key_low is not None and c < key_low.price:
            # ダウ崩れ: 実質押し安値を終値で割る
            trend = STATE_EXIT
            key_high = last_high
            break_pos = i
            pending_high = None
            lows_since_break = 0
            last_event = (bars.index[i], STATE_EXIT)
        elif trend == STATE_EXIT and pending_low is not None and c < pending_low.price:
            key_high = last_high                          # 実質戻り高値の切り下げ（参考）
            pending_low = None
        elif trend is None and pending_low is not None and c < pending_low.price:
            trend = STATE_EXIT                            # 初期化: 最初の明確な安値割れ
            key_high = last_high
            break_pos = i
            pending_low = None
            last_event = (bars.index[i], STATE_EXIT)
        states.append(trend)

    return {
        "states": pd.Series(states, index=bars.index, dtype=object),
        "trend": trend,
        "key_low": key_low,
        "key_high": key_high,
        "reentry_high": pending_high if trend == STATE_EXIT else None,
        "last_event": last_event,
    }


def guard_states(bars: pd.DataFrame, lr: int | None = None, min_wave: int | None = None) -> pd.Series:
    """backtest 用: 各日の HOLD/EXIT 列（初期化前は None）。"""
    return analyze(bars, lr if lr is not None else PIVOT_LR,
                   min_wave if min_wave is not None else MIN_WAVE)["states"]


def normalize_prev(prev_state: str | None) -> str | None:
    return prev_state if prev_state in STATES else None


def evaluate(df: pd.DataFrame, prev_state: str | None = None,
             now_utc: datetime | None = None) -> dict | None:
    """日足 DataFrame から HOLD/EXIT を判定する。初期化前・データ不足・鮮度不良なら None。

    状態機械は履歴全体から再計算する（prev_state は使わない。オンライン処理なので
    毎回同じ履歴を流せば同じ状態になる）。
    """
    bars = confirmed_daily_ohlc(df, now_utc)
    if len(bars) < MIN_BARS:
        return None
    now = now_utc or datetime.now(timezone.utc)
    last_day = bars.index[-1]
    age_days = (pd.Timestamp(now.date()) - last_day).days
    if GUARD_STALE_DAYS > 0 and age_days > GUARD_STALE_DAYS:
        print(f"最新確定日足が {age_days}日前で古いためスキップします（長期休場/データ源異常）。")
        return None

    res = analyze(bars)
    if res["trend"] is None:
        return None  # まだ明確な高値超え/安値割れが無い（判定不能）

    c = float(bars["Close"].iloc[-1])
    close = bars["Close"]

    def _chg(k: int) -> float | None:
        if len(close) <= k or close.iloc[-1 - k] == 0:
            return None
        return float((close.iloc[-1] - close.iloc[-1 - k]) / close.iloc[-1 - k] * 100.0)

    kl, rh = res["key_low"], res["reentry_high"]
    ev = res["last_event"]
    return {
        "state": res["trend"],
        "price": c,
        "key_low": kl.price if kl else None,
        "key_low_date": kl.date.strftime("%Y-%m-%d") if kl else None,
        "reentry_high": rh.price if rh else None,
        "reentry_high_date": rh.date.strftime("%Y-%m-%d") if rh else None,
        "event_date": ev[0].strftime("%Y-%m-%d") if ev else None,
        # リスク層(assess_risk)は change_1d_pct/5d_pct の存在で日足判定と認識する
        "change_1d_pct": _chg(1),
        "change_5d_pct": _chg(5),
        "time": f"{last_day.strftime('%Y-%m-%d')} 確定日足",
    }


# ─────────────────────────────────────────────────────────────
# 通知文
# ─────────────────────────────────────────────────────────────
def _fmt(v, decimals):
    return f"{v:.{decimals}f}" if v is not None else "未形成"


def format_message(info: dict, *, label: str, decimals: int = 3,
                   first_run: bool = False) -> str:
    st = info["state"]
    rule = f"日足ダウ理論（山谷 左右{PIVOT_LR}本・{MIN_WAVE}本未満の波は無視・終値で判定）"
    kl = _fmt(info.get("key_low"), decimals)
    rh = _fmt(info.get("reentry_high"), decimals)
    if first_run:
        if st == STATE_HOLD:
            line = f"実質押し安値: {kl}（{info.get('key_low_date')}）— ここを終値で割ったら離脱を通知"
            stance = "ロング維持 🟢"
        else:
            line = f"復帰ライン: 崩れ後の明確な戻り高値 {rh}" + (
                f"（{info.get('reentry_high_date')}）— ここを終値で超えたら復帰を通知。"
                f"{MIN_WAVE}本以上かけた新しい戻り高値ができればラインは下がります"
                if info.get("reentry_high") else f"（{MIN_WAVE}本以上かけた戻り高値の形成待ち）")
            stance = "離脱中（待機） 🛡️"
        return (
            f"🔔 {label} ロング保護の監視を開始しました\n"
            f"現在: *{st}*（{stance}）\n"
            f"日足終値: {info['price']:.{decimals}f} / {line}\n"
            f"判定: {rule} / {info['time']}"
        )
    if st == STATE_EXIT:
        return (
            f"🛡️ {label} ロング離脱シグナル（日足ダウ崩れ）\n"
            f"日足終値 {info['price']:.{decimals}f} が実質押し安値 {kl}"
            f"（{info.get('key_low_date')} の安値）を終値で下抜け\n"
            f"→ ロングの縮小/決済を検討（目安: 半分）。復帰は崩れ後に{MIN_WAVE}本以上かけて形成される"
            f"戻り高値の終値超えで通知\n"
            f"{info['time']}"
        )
    return (
        f"🟢 {label} ロング復帰シグナル（上昇ダウ再開）\n"
        f"日足終値 {info['price']:.{decimals}f} が崩れ後の直近高値を終値で上抜け\n"
        f"→ ロングの再構築を検討（スワップ受け取り再開）。新たな実質押し安値: {kl}"
        f"（{info.get('key_low_date')}）\n"
        f"{info['time']}"
    )


def stance_label(state: str | None) -> str:
    if state == STATE_HOLD:
        return "ロング維持🟢"
    if state == STATE_EXIT:
        return "離脱中🛡️"
    return "不明"
