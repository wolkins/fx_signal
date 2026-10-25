#!/usr/bin/env python3
"""回帰テスト（ネット・APIキー不要・決定論的）.

今日のレビューで手検証した「壊れやすい所」を固定する:
- リスク層の status 分岐（改善1）と available の整合
- カレンダーのスキーマ崩れ検知 vs 正常0件の撃ち分け（改善2）
- 通知の末尾フッター条件 / 先頭⚠️ブロック条件
- RISK_SKIP_LLM_WHEN_QUIET（改善5）の短絡
- notify が Slack 失敗時に Webhook URL を漏らさないこと
- backtest.py と fx_signal.py の状態判定が一致すること（決定論コアの不変）

実行方法:
    python test_fx_signal.py     # pytest 無しでも全テストを実行
    pytest test_fx_signal.py     # pytest があればそれでも可

すべてモック/合成データで完結し、yfinance も Anthropic API も呼ばない。
"""

from __future__ import annotations

import contextlib
import io
import math
import os
import tempfile
from pathlib import Path

import pandas as pd

import backtest as bt
import dow
import fx_signal as fx
import risk_filter as rf

# テストは作業ツリーを汚さない（監査ログ・健全性・ゲートログを一時パスへ退避）。
rf.RISK_LOG_FILE = Path(tempfile.gettempdir()) / "fx_signal_test_risk_log.jsonl"
fx.HEALTH_FILE = Path(tempfile.gettempdir()) / "fx_signal_test_health.json"
dow.GATE_LOG_FILE = Path(tempfile.gettempdir()) / "fx_signal_test_gate.jsonl"


# ─────────────────────────────────────────────────────────────
# テスト用ヘルパ: 一時的に属性/環境を差し替えて復元する
# ─────────────────────────────────────────────────────────────
@contextlib.contextmanager
def patched(obj, **attrs):
    saved = {k: getattr(obj, k) for k in attrs}
    for k, v in attrs.items():
        setattr(obj, k, v)
    try:
        yield
    finally:
        for k, v in saved.items():
            setattr(obj, k, v)


@contextlib.contextmanager
def no_env(key):
    saved = os.environ.pop(key, None)
    try:
        yield
    finally:
        if saved is not None:
            os.environ[key] = saved


SIG = {"state": "LONG", "price": 159.2, "change_15m_pct": 0.0, "change_1h_pct": 0.1}
OK_RAW = {
    "risk_level": "low",
    "advise_caution": False,
    "headline": "h",
    "reason": "r",
    "events": [],
}


# ─────────────────────────────────────────────────────────────
# 改善1: status 分岐と available
# ─────────────────────────────────────────────────────────────
def test_status_disabled():
    with patched(rf, RISK_FILTER_ENABLED=False):
        r = rf.assess_risk(SIG)
    assert r["status"] == "disabled" and r["available"] is False


def test_status_no_api_key():
    with patched(rf, _fetch_calendar=lambda cur: []), no_env(rf.API_KEY_ENV):
        r = rf.assess_risk(SIG)
    assert r["status"] == "no_api_key" and r["available"] is False


def test_status_llm_failed():
    with patched(
        rf, _fetch_calendar=lambda cur: [], _call_llm=lambda p: ("llm_failed", None)
    ):
        r = rf.assess_risk(SIG)
    assert r["status"] == "llm_failed" and r["available"] is False


def test_status_parse_failed():
    with patched(
        rf,
        _fetch_calendar=lambda cur: [],
        _call_llm=lambda p: ("got_text", {"not": "schema"}),
    ):
        r = rf.assess_risk(SIG)
    assert r["status"] == "parse_failed" and r["available"] is False


def test_status_ok():
    with patched(
        rf, _fetch_calendar=lambda cur: [], _call_llm=lambda p: ("got_text", OK_RAW)
    ):
        r = rf.assess_risk(SIG)
    assert r["status"] == "ok" and r["available"] is True and r["risk_level"] == "low"


def test_status_calendar_unavailable():
    def boom(cur):
        raise rf.CalendarUnavailable("net down")

    with patched(rf, _fetch_calendar=boom):
        r = rf.assess_risk(SIG)
    assert r["status"] == "calendar_unavailable" and r["available"] is False


def test_status_calendar_schema():
    def boom(cur):
        raise rf.CalendarSchemaError("schema")

    with patched(rf, _fetch_calendar=boom):
        r = rf.assess_risk(SIG)
    assert r["status"] == "calendar_schema" and r["available"] is False


# ─────────────────────────────────────────────────────────────
# 改善2: スキーマ崩れ検知 vs 正常0件
# ─────────────────────────────────────────────────────────────
def _ff_item(**over):
    base = {
        "country": "USD",
        "impact": "High",
        "date": "2026-05-28T12:30:00+00:00",
        "title": "CPI",
    }
    base.update(over)
    return base


def test_schema_valid_high():
    assert len(rf._parse_ff([_ff_item()])) == 1


def test_schema_valid_zero_high_is_normal():
    # 正常スキーマだが今週は High が0件 → 例外を出さず空リスト（=正常な low）
    assert rf._parse_ff([_ff_item(impact="Low")]) == []


def test_schema_empty_payload_is_normal():
    assert rf._parse_ff([]) == []


def test_schema_field_renamed_raises():
    # 単一フィールド改名でも検知できること（偽の0件を防ぐ）
    for drop in ("country", "impact", "date", "title"):
        item = {k: v for k, v in _ff_item().items() if k != drop}
        try:
            rf._parse_ff([item])
            raise AssertionError(f"{drop} 欠落で例外が出なかった")
        except rf.CalendarSchemaError:
            pass


def test_schema_non_list_raises():
    try:
        rf._parse_ff({"not": "a list"})
        raise AssertionError("非配列で例外が出なかった")
    except rf.CalendarSchemaError:
        pass


# ─────────────────────────────────────────────────────────────
# 通知フッター / 先頭⚠️ブロック条件
# ─────────────────────────────────────────────────────────────
def test_footer_only_on_failures():
    show = ("calendar_unavailable", "calendar_schema", "llm_failed", "parse_failed")
    hide = ("ok", "disabled", "no_api_key")
    for st in show:
        assert fx._risk_footer({"status": st}) != ""
    for st in hide:
        assert fx._risk_footer({"status": st}) == ""
    assert fx._risk_footer(None) == ""


def test_prefix_only_on_high_or_caution():
    assert fx._risk_prefix({"risk_level": "high"}) != ""
    assert fx._risk_prefix({"risk_level": "low", "advise_caution": True}) != ""
    assert fx._risk_prefix({"risk_level": "low", "advise_caution": False}) == ""
    assert fx._risk_prefix(None) == ""


def test_coerce_bool():
    assert rf._coerce_bool(True) is True
    assert rf._coerce_bool("false") is False  # 文字列 "false" を True にしない
    assert rf._coerce_bool("true") is True
    assert rf._coerce_bool(0) is False


# ─────────────────────────────────────────────────────────────
# 改善5: 静穏時の LLM スキップ（フラグ）
# ─────────────────────────────────────────────────────────────
def test_skip_llm_when_quiet():
    calls = {"n": 0}

    def spy(p):
        calls["n"] += 1
        return ("got_text", OK_RAW)

    quiet = {**SIG, "change_1h_pct": 0.05}
    # OFF（既定）→ 呼ぶ
    with patched(rf, _fetch_calendar=lambda cur: [], _call_llm=spy,
                 RISK_SKIP_LLM_WHEN_QUIET=False):
        rf.assess_risk(quiet)
    assert calls["n"] == 1
    # ON かつ静穏 → 呼ばない・決定論的に low
    calls["n"] = 0
    with patched(rf, _fetch_calendar=lambda cur: [], _call_llm=spy,
                 RISK_SKIP_LLM_WHEN_QUIET=True):
        r = rf.assess_risk(quiet)
    assert calls["n"] == 0 and r["status"] == "ok" and r["risk_level"] == "low"


def test_daily_signal_payload_uses_daily_keys_and_threshold():
    """日足判定(long_guard)の info は timeframe=daily・前日比/5日比で LLM に渡り、
    静穏判定は前日比と RISK_QUIET_PCT_DAILY で行う（1h キーは出さない）。"""
    seen = {}

    def spy(p):
        seen.update(p)
        return ("got_text", OK_RAW)

    daily = {"state": "HOLD", "price": 111.1, "change_1d_pct": 0.84, "change_5d_pct": 2.1}
    with patched(rf, _fetch_calendar=lambda cur: [], _call_llm=spy,
                 RISK_SKIP_LLM_WHEN_QUIET=False):
        rf.assess_risk(daily, pair_label="AUD/JPY")
    sig = seen["signal"]
    assert sig["timeframe"] == "daily"
    assert sig["change_1d_pct"] == 0.84 and sig["change_5d_pct"] == 2.1
    assert "change_1h_pct" not in sig and "change_15m_pct" not in sig
    # 前日比 0.84% は日足の閾値(1.0%)未満 → 静穏スキップ ON なら LLM を呼ばない
    calls = {"n": 0}

    def spy2(p):
        calls["n"] += 1
        return ("got_text", OK_RAW)

    with patched(rf, _fetch_calendar=lambda cur: [], _call_llm=spy2,
                 RISK_SKIP_LLM_WHEN_QUIET=True):
        r = rf.assess_risk(daily)
    assert calls["n"] == 0 and r["risk_level"] == "low"
    # 前日比 1.5% は閾値以上 → 呼ぶ
    with patched(rf, _fetch_calendar=lambda cur: [], _call_llm=spy2,
                 RISK_SKIP_LLM_WHEN_QUIET=True):
        rf.assess_risk({**daily, "change_1d_pct": 1.5})
    assert calls["n"] == 1
    # 5分足判定の info は従来どおり intraday
    with patched(rf, _fetch_calendar=lambda cur: [], _call_llm=spy,
                 RISK_SKIP_LLM_WHEN_QUIET=False):
        rf.assess_risk(SIG)
    assert seen["signal"]["timeframe"] == "intraday" and "change_1h_pct" in seen["signal"]


# ─────────────────────────────────────────────────────────────
# 機密: Slack 失敗ログに Webhook URL を出さない
# ─────────────────────────────────────────────────────────────
def test_notify_does_not_leak_url():
    import requests

    secret_url = "https://hooks.slack.com/services/SECRET123/TOKEN456/zzz"

    class FakeResp:
        status_code = 404

    def fake_post(*a, **k):
        err = requests.exceptions.HTTPError(f"404 Client Error for url: {secret_url}")
        err.response = FakeResp()
        raise err

    buf = io.StringIO()
    with patched(os.environ, **{}), patched(fx.requests, post=fake_post):
        os.environ["SLACK_WEBHOOK_URL"] = secret_url
        try:
            with contextlib.redirect_stderr(buf):
                fx.notify("本文テスト")
        finally:
            os.environ.pop("SLACK_WEBHOOK_URL", None)
    err = buf.getvalue()
    assert "SECRET123" not in err and "hooks.slack.com" not in err
    assert "HTTP 404" in err  # ステータスコードは出る


# ─────────────────────────────────────────────────────────────
# 改善C: カレンダー複数フィードの部分成功（thisweek成功＋nextweek404）
# ─────────────────────────────────────────────────────────────
def test_calendar_multifeed_partial_success():
    import requests

    def fake_one(url):
        if "thisweek" in url:
            return [
                {"country": "USD", "impact": "High",
                 "date": "2026-05-28T12:30:00+00:00", "title": "CPI"}
            ]
        if "nextweek" in url:
            raise requests.HTTPError("404")  # 来週分は未提供
        raise requests.HTTPError("401")  # jblanked

    with patched(rf, _fetch_one=fake_one):
        evs = rf._fetch_calendar(("USD", "JPY"))
    assert [e["title"] for e in evs] == ["CPI"]  # 404でも今週分を採用


def test_calendar_all_feeds_fail_raises():
    import requests

    def boom(url):
        raise requests.HTTPError("down")

    with patched(rf, _fetch_one=boom):
        try:
            rf._fetch_calendar(("USD", "JPY"))
            raise AssertionError("全滅で例外が出なかった")
        except rf.CalendarUnavailable:
            pass


def test_calendar_primary_schema_error_surfaces():
    # 主フィード(thisweek)のスキーマ崩れは握りつぶさず calendar_schema として表に出す。
    import requests

    def fake_one(url):
        if "thisweek" in url:
            return [{"ccy": "USD", "strength": "High"}]  # 想定フィールド無し
        raise requests.HTTPError("404")  # nextweek / jblanked

    with patched(rf, _fetch_one=fake_one):
        try:
            rf._fetch_calendar(("USD", "JPY"))
            raise AssertionError("主フィードのスキーマ崩れで例外が出なかった")
        except rf.CalendarSchemaError:
            pass


# ─────────────────────────────────────────────────────────────
# 改善A: データ源サイレント故障の検知（警告→回復、市場クローズは中立）
# ─────────────────────────────────────────────────────────────
def test_data_health_alert_recovery_and_neutral():
    fx.HEALTH_FILE.unlink(missing_ok=True)
    sent: list[str] = []
    with patched(fx, notify=sent.append):
        fx.update_data_health(True, False)   # streak1: 閾値未満→無音
        assert not sent
        fx.update_data_health(True, False)   # streak2: 警告
        assert sent and "失敗" in sent[-1]
        n = len(sent)
        fx.update_data_health(True, False)   # 既に警告済→無音
        assert len(sent) == n
        fx.update_data_health(False, False)  # 市場クローズ(no_data)→中立・無音
        assert len(sent) == n
        fx.update_data_health(False, True)   # データ回復→回復通知
        assert "回復" in sent[-1]
    fx.HEALTH_FILE.unlink(missing_ok=True)


def test_data_health_counts_exceptions_as_failure():
    # process_pair が例外を投げ続ける故障も「全滅」として警告対象にする。
    fx.HEALTH_FILE.unlink(missing_ok=True)
    sent: list[str] = []

    def boom(ticker):
        raise RuntimeError("boom")

    with patched(fx, PAIRS=["USDJPY=X", "AUDJPY=X"], process_pair=boom,
                 notify=sent.append):
        fx.main()  # 1回目: streak1
        assert not sent
        fx.main()  # 2回目: 閾値到達で警告
    assert sent and "失敗" in sent[-1]
    fx.HEALTH_FILE.unlink(missing_ok=True)


# ─────────────────────────────────────────────────────────────
# 改善B: 週次ハートビート
# ─────────────────────────────────────────────────────────────
def test_heartbeat_message():
    sent: list[str] = []
    with patched(fx, PAIRS=["USDJPY=X"],
                 load_state=lambda p: {"state": "LONG", "updated_at": "2026-05-30 08:00 JST"},
                 fetch_data=lambda t, **k: None,  # 鮮度チェックはスキップ（取得しない）
                 notify=sent.append):
        fx.heartbeat()
    assert sent and "稼働中" in sent[0] and "USD/JPY" in sent[0] and "LONG" in sent[0]
    assert "古い" not in sent[0]  # データ無し（None）なら鮮度警告は出さない


def test_heartbeat_stale_feed_warns():
    """5分足モード(sma_cross): 最新足が STALE_MINUTES より古ければ警告。"""
    import pandas as pd
    sent: list[str] = []
    # 最新足が STALE_MINUTES より十分古い df を返す
    old = pd.date_range(end=pd.Timestamp("2026-05-26 00:00", tz="UTC"), periods=3, freq="5min")
    stale_df = pd.DataFrame({"Close": [1.0, 2.0, 3.0]}, index=old)
    with patched(fx, PAIRS=["USDJPY=X"], STRATEGY="sma_cross",
                 load_state=lambda p: {"state": "LONG", "updated_at": "x"},
                 fetch_data=lambda t, **k: stale_df,
                 STALE_MINUTES=90,
                 notify=sent.append):
        fx.heartbeat()
    assert "古い" in sent[0]  # 全ペア古い → 固まり疑いの警告


def test_heartbeat_daily_mode_uses_confirmed_daily_age():
    """日足モード(long_guard): 鮮度は「最新の確定日足が GUARD_STALE_DAYS より古いか」で判定。
    週末（金曜足が最新・日曜深夜に実行）では警告せず、数週間止まっていれば警告する。"""
    import pandas as pd
    import long_guard as lg

    def run(last_day: str, now: datetime) -> str:
        sent: list[str] = []
        idx = pd.date_range(end=pd.Timestamp(last_day), periods=10, freq="B")
        df = pd.DataFrame({"Close": range(10)}, index=idx)
        calls: list[dict] = []

        def fetch(t, **k):
            calls.append(k)
            return df

        # 「今」を固定するため、_latest_age_days に now_utc を注入する薄い wrapper で検証
        orig = fx._latest_age_days
        with patched(fx, PAIRS=["USDJPY=X", "AUDJPY=X"], STRATEGY="long_guard",
                     load_state=lambda p: {"state": "HOLD", "guard": "sma", "updated_at": "x"},
                     fetch_data=fetch, notify=sent.append,
                     _latest_age_days=lambda d, now_utc=None: orig(d, now)):
            fx.heartbeat()
        # 日足モードでは日足("1d")を取得している
        assert calls and all(k.get("interval") == lg.GUARD_INTERVAL for k in calls)
        return sent[0]

    # 金曜 2026-10-23 が最新確定足、日曜深夜(JST 月曜 02:30 = 日曜 17:30 UTC) → 2日古いだけ。警告なし
    m = run("2026-10-23", datetime(2026, 10, 25, 17, 30, tzinfo=timezone.utc))
    assert "ロング保護・日足" in m and "古い" not in m
    # 最新確定足が 2 週間以上前 → 警告
    m = run("2026-10-09", datetime(2026, 10, 25, 17, 30, tzinfo=timezone.utc))
    assert "確定日足が" in m and "古い" in m


# ─────────────────────────────────────────────────────────────
# ダウMTFゲート: スイング検出・トレンド判定・ゲート分岐・degrade open・先読みなし
# ─────────────────────────────────────────────────────────────
def test_find_swings_no_lookahead():
    idx = pd.date_range("2026-01-01", periods=11, freq="1h", tz="UTC")
    high = pd.Series([1, 2, 5, 2, 1, 2, 6, 2, 1, 2, 7], index=idx)
    low = pd.Series([1, 0, 1, 1, 0, 1, 1, 1, 0, 1, 1], index=idx)
    sw = dow.find_swings(high, low, 2, 2)
    kinds = {(s["pos"], s["kind"]) for s in sw}
    assert {(2, "H"), (6, "H"), (4, "L"), (8, "L")} <= kinds
    # 末尾 right(=2) 本(pos9,10)は未確定 → 含まれない（先読みしない）
    assert all(s["pos"] <= 8 for s in sw)
    # no-lookahead: バーを足しても過去の確定スイングは完全に不変
    idx2 = pd.date_range("2026-01-01", periods=12, freq="1h", tz="UTC")
    high2 = pd.Series([1, 2, 5, 2, 1, 2, 6, 2, 1, 2, 7, 3], index=idx2)
    low2 = pd.Series([1, 0, 1, 1, 0, 1, 1, 1, 0, 1, 1, 1], index=idx2)
    sw2 = dow.find_swings(high2, low2, 2, 2)
    old = [(s["pos"], s["kind"], s["price"]) for s in sw]
    new_sub = [(s["pos"], s["kind"], s["price"]) for s in sw2 if s["pos"] <= 8]
    assert old == new_sub


def _swings(highs, lows):
    return [{"kind": "H", "price": p, "pos": i} for i, p in enumerate(highs)] + [
        {"kind": "L", "price": p, "pos": 100 + i} for i, p in enumerate(lows)
    ]


def test_dow_trend():
    assert dow.dow_trend(_swings([5, 6], [1, 2])) == "UP"
    assert dow.dow_trend(_swings([6, 5], [2, 1])) == "DOWN"
    assert dow.dow_trend(_swings([5, 6], [2, 1])) == "RANGE"
    assert dow.dow_trend(_swings([5], [1])) == "UNKNOWN"


def test_gate_branches():
    # 方向一致は通過 / 逆行は常に抑制 / 判定不能は素通し（RANGE_BLOCKS 非依存）
    assert dow.gate("LONG", {"4H": "UP", "1H": "UP"})["pass"] is True
    assert dow.gate("SHORT", {"4H": "DOWN", "1H": "UP"})["pass"] is False  # 1H逆行
    assert dow.gate("SHORT", {"4H": "DOWN", "1H": "DOWN"})["pass"] is True
    assert dow.gate("LONG", {"4H": "UNKNOWN", "1H": "UP"})["pass"] is True  # degrade open
    # RANGE の扱いは RANGE_BLOCKS 次第（デフォルト値に依存せず明示して検証）
    with patched(dow, RANGE_BLOCKS=True):
        r = dow.gate("LONG", {"4H": "RANGE", "1H": "UP"})
        assert r["pass"] is False and r["reason"] == "blocked_4h_range"
    with patched(dow, RANGE_BLOCKS=False):
        r = dow.gate("LONG", {"4H": "RANGE", "1H": "UP"})
        assert r["pass"] is True and r["reason"] == "ok"


def test_env_bool_override():
    # 未設定 → default
    with no_env("RANGE_BLOCKS"):
        assert dow._env_bool("RANGE_BLOCKS", False) is False
        assert dow._env_bool("RANGE_BLOCKS", True) is True
    # env があれば優先。"false" 文字列を正しく False に（罠回避）
    for val, expect in (("true", True), ("false", False), ("True", True),
                        ("1", True), ("no", False), ("", True)):
        if val == "":
            with no_env("DOW_GATE_ENABLED"):
                assert dow._env_bool("DOW_GATE_ENABLED", True) is True
            continue
        os.environ["DOW_GATE_ENABLED"] = val
        try:
            assert dow._env_bool("DOW_GATE_ENABLED", True) is expect, val
        finally:
            os.environ.pop("DOW_GATE_ENABLED", None)


def test_gate_degrade_open_on_insufficient_data():
    tiny = pd.DataFrame(
        {"Open": [1, 2], "High": [1, 2], "Low": [1, 2], "Close": [1, 2]},
        index=pd.date_range("2026-01-01", periods=2, freq="5min", tz="UTC"),
    )
    trends = dow.mtf_trends(tiny)
    assert trends["4H"] == "UNKNOWN" and trends["1H"] == "UNKNOWN"
    assert dow.gate("LONG", trends)["pass"] is True  # 素通し


def test_gate_disabled_passes_all():
    with patched(dow, DOW_GATE_ENABLED=False):
        assert dow.gate("LONG", {"4H": "DOWN", "1H": "DOWN"})["pass"] is True


def test_trend_series_no_future_leak():
    # backtest用 trend_series が未来を先取りしないこと（prefix不変性で検証）。
    def make(n):
        idx = pd.date_range("2026-01-01", periods=n, freq="5min", tz="UTC")
        c = [100.0 + 5.0 * math.sin(i / 30.0) + i * 0.001 for i in range(n)]
        return pd.DataFrame(
            {"Open": c, "High": [x + 0.1 for x in c], "Low": [x - 0.1 for x in c], "Close": c},
            index=idx,
        )
    df1 = make(1500)
    df2 = make(1900)  # df1 は df2 の先頭プレフィックス（同じデータ＋未来分）
    s1 = dow.trend_series(df1, "1h")
    s2 = dow.trend_series(df2, "1h")
    # df1 範囲内の時刻で asof 値が一致するはず（未来漏れがあれば食い違う）
    for ts in df1.index[1000:1400:50]:
        assert s1.asof(ts) == s2.asof(ts), f"未来漏れ: {ts}"


# ─────────────────────────────────────────────────────────────
# 決定論: backtest._states と fx.evaluate の状態判定が一致
# ─────────────────────────────────────────────────────────────
def _synthetic_df(n=260):
    # 振動＋微トレンドでクロスが何度も起きる合成OHLC。tz-aware の直近インデックス。
    # ATRデッドバンドの検証のため High/Low も持たせる。
    end = pd.Timestamp("2026-05-29 12:00", tz="UTC")
    idx = pd.date_range(end=end, periods=n, freq="5min")
    close = [100.0 + 2.0 * math.sin(i / 6.0) + i * 0.01 for i in range(n)]
    high = [c + 0.05 for c in close]
    low = [c - 0.05 for c in close]
    return pd.DataFrame({"Open": close, "High": high, "Low": low, "Close": close}, index=idx)


def test_determinism_backtest_matches_evaluate():
    df = _synthetic_df()
    # pct と atr の両モードで、backtest._states と fx.evaluate の判定が完全一致するか。
    cases = [
        dict(DEADBAND_MODE="pct", HYSTERESIS_PCT=0.0),
        dict(DEADBAND_MODE="pct", HYSTERESIS_PCT=0.05),
        dict(DEADBAND_MODE="atr", ATR_K=1.0),
    ]
    for case in cases:
        with patched(fx, STALE_MINUTES=0, **case):
            states = bt._states(df)
            assert len(states) > 50, "SMAが算出できる区間が少なすぎる"
            mism = 0
            for k in range(1, len(states)):
                ts, st, _ = states[k]
                prev = states[k - 1][1]
                ev = fx.evaluate(df.loc[:ts], prev_state=prev)
                if ev is None or ev["state"] != st:
                    mism += 1
            assert mism == 0, f"{case}: {mism}バーで不一致"


# ─────────────────────────────────────────────────────────────
# ロング保護ガード（long_guard.py / STRATEGY="long_guard"）
# ─────────────────────────────────────────────────────────────
import long_guard as lg  # noqa: E402
from datetime import datetime, timezone  # noqa: E402


def _daily_df(closes, start="2025-01-01"):
    idx = pd.date_range(start, periods=len(closes), freq="D")
    c = pd.Series(closes, index=idx, dtype=float)
    return pd.DataFrame({"Open": c, "High": c + 0.1, "Low": c - 0.1, "Close": c})


def test_guard_drops_unconfirmed_today_bar():
    df = _daily_df([100.0] * 60, start="2025-01-01")
    last = df.index[-1]  # 2025-03-01
    # 「今日」が最終バーの日付なら、そのバーは進行中として落とされる
    s = lg.confirmed_daily_close(df, now_utc=datetime(2025, 3, 1, 12, tzinfo=timezone.utc))
    assert s.index[-1] == last - pd.Timedelta(days=1)
    # 翌日になれば採用される
    s2 = lg.confirmed_daily_close(df, now_utc=datetime(2025, 3, 2, 0, 5, tzinfo=timezone.utc))
    assert s2.index[-1] == last
    # tz-aware index でも同じ日付に正規化される
    dft = df.copy()
    dft.index = dft.index.tz_localize("UTC")
    s3 = lg.confirmed_daily_close(dft, now_utc=datetime(2025, 3, 2, tzinfo=timezone.utc))
    assert s3.index[-1] == last and s3.index.tz is None


def test_guard_band_hysteresis():
    # SMA=100 相当の平坦な系列。帯1%: 99.5 では HOLD 維持、98.9 で EXIT。
    assert lg.next_state(99.5, 100.0, lg.STATE_HOLD, 1.0) == lg.STATE_HOLD
    assert lg.next_state(98.9, 100.0, lg.STATE_HOLD, 1.0) == lg.STATE_EXIT
    # EXIT 中: 100.5 では EXIT 維持、101.1 で HOLD 復帰
    assert lg.next_state(100.5, 100.0, lg.STATE_EXIT, 1.0) == lg.STATE_EXIT
    assert lg.next_state(101.1, 100.0, lg.STATE_EXIT, 1.0) == lg.STATE_HOLD
    # 初期化は単純比較
    assert lg.next_state(100.01, 100.0, None, 1.0) == lg.STATE_HOLD
    assert lg.next_state(99.99, 100.0, None, 1.0) == lg.STATE_EXIT
    # 帯0なら単純クロス
    assert lg.next_state(99.99, 100.0, lg.STATE_HOLD, 0.0) == lg.STATE_EXIT


def test_guard_states_matches_sequential_evaluate():
    # ランダムウォーク風の決定論的系列で、一括(guard_states)と逐次(evaluate)が一致すること
    import math as _m
    closes = [100.0]
    for i in range(1, 200):
        closes.append(closes[-1] * (1 + 0.012 * _m.sin(i / 7.0) + 0.004 * _m.cos(i / 3.0)))
    df = _daily_df(closes, start="2024-01-01")
    now = datetime(2024, 12, 31, tzinfo=timezone.utc)  # 全バーが確定済み扱い
    states = lg.guard_states(lg.confirmed_daily_close(df, now))
    valid = states.dropna()
    assert len(valid) > 100 and set(valid.unique()) == {lg.STATE_HOLD, lg.STATE_EXIT}, "切替が発生する系列であること"
    prev = None
    mism = 0
    for ts, st in valid.items():
        # 逐次評価は「その翌日に実行された」想定（鮮度ガードに掛からないよう now を揃える）
        nxt = (ts + pd.Timedelta(days=1)).to_pydatetime().replace(tzinfo=timezone.utc)
        ev = lg.evaluate(df.loc[:ts], prev_state=prev, now_utc=nxt)
        if ev is None or ev["state"] != st:
            mism += 1
        prev = st
    assert mism == 0, f"{mism}日で不一致"


def test_guard_evaluate_derives_state_from_history_not_prev():
    """evaluate は履歴全体に規則を通して状態を求め、prev_state や単純比較に依存しない。
    （state ファイル削除や初回起動で、帯を無視した判定が混ざらないこと）"""
    closes = [100.0] * 60 + [85.0] * 5          # 急落 → 規則上は EXIT
    for _ in range(10):                          # その後 SMA の +1% に張り付く: 単純比較なら HOLD、
        sma = sum(closes[-50:]) / 50.0           # 帯(+2%)未満なので規則上は EXIT のまま
        closes.append(sma * 1.01)
    df = _daily_df(closes, start="2025-01-01")
    now = (df.index[-1] + pd.Timedelta(days=1)).to_pydatetime().replace(tzinfo=timezone.utc)
    info = lg.evaluate(df, prev_state=None, now_utc=now)
    assert info is not None
    assert info["price"] > info["sma"], "終値は SMA より上（単純比較なら HOLD になる局面）"
    assert lg.next_state(info["price"], info["sma"], None) == lg.STATE_HOLD
    assert info["state"] == lg.STATE_EXIT, "履歴に規則を通せば EXIT 継続"
    # prev_state が何であっても判定は変わらない（通知の差分検出にだけ使う）
    for prev in (None, "HOLD", "EXIT", "LONG"):
        assert lg.evaluate(df, prev_state=prev, now_utc=now)["state"] == lg.STATE_EXIT
    # 一括計算(backtest)と一致
    assert lg.guard_states(lg.confirmed_daily_close(df, now)).iloc[-1] == lg.STATE_EXIT


def test_guard_evaluate_guards_and_legacy_prev():
    df = _daily_df([100.0] * 30, start="2025-01-01")  # SMA50 に届かない
    assert lg.evaluate(df, now_utc=datetime(2025, 3, 1, tzinfo=timezone.utc)) is None
    df2 = _daily_df([100.0] * 60, start="2025-01-01")
    # 最新確定足が GUARD_STALE_DAYS より古ければ None
    assert lg.evaluate(df2, now_utc=datetime(2025, 3, 20, tzinfo=timezone.utc)) is None
    # 旧戦略の状態(LONG/SHORT)は None に正規化＝初回扱い
    assert lg.normalize_prev("LONG") is None and lg.normalize_prev("HOLD") == "HOLD"
    info = lg.evaluate(df2, prev_state="LONG", now_utc=datetime(2025, 3, 2, tzinfo=timezone.utc))
    assert info is not None and info["state"] in lg.STATES
    assert "確定日足" in info["time"]
    # リスク層が日足判定と認識するキーが存在し、5分足用のキーは渡さない
    assert "change_1d_pct" in info and "change_5d_pct" in info
    assert "change_15m_pct" not in info and "change_1h_pct" not in info


def test_guard_message_format():
    info = {"state": lg.STATE_EXIT, "price": 98.5, "sma": 100.0, "band_lo": 99.0,
            "band_hi": 101.0, "dist_pct": -1.5, "time": "2025-03-01 確定日足"}
    m = lg.format_message(info, label="AUD/JPY")
    assert "離脱" in m and "下抜け" in m and "AUD/JPY" in m and "99.000" in m
    info["state"] = lg.STATE_HOLD
    m2 = lg.format_message(info, label="AUD/JPY")
    assert "復帰" in m2 and "上抜け" in m2
    m3 = lg.format_message(info, label="AUD/JPY", first_run=True)
    assert "監視を開始" in m3 and "HOLD" in m3
    # ハートビート表示
    assert lg.stance_label("HOLD") != lg.stance_label("EXIT") != lg.stance_label(None)


def test_guard_backtest_consistent_with_module():
    # backtest.backtest_guard が long_guard の判定をそのまま使い、落ちずに走ること
    import math as _m
    closes = [100.0]
    for i in range(1, 160):
        closes.append(closes[-1] * (1 + 0.01 * _m.sin(i / 9.0)))
    df = _daily_df(closes, start="2024-01-01")
    with patched(bt, fetch_history=lambda t, p, i, s: df):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = bt.backtest_guard("AUDJPY=X", "1y", "1d", detail=True, source="x", engine="sma")
    out = buf.getvalue()
    assert rc == 0 and "買い持ち" in out and "ガード[sma]" in out and "切替履歴" in out


def test_strategy_default_and_env_override():
    import importlib
    import os as _os
    saved = _os.environ.get("STRATEGY")
    try:
        _os.environ.pop("STRATEGY", None)
        importlib.reload(fx)
        assert fx.STRATEGY == "long_guard"
        _os.environ["STRATEGY"] = "SMA_CROSS"
        importlib.reload(fx)
        assert fx.STRATEGY == "sma_cross"
    finally:
        if saved is None:
            _os.environ.pop("STRATEGY", None)
        else:
            _os.environ["STRATEGY"] = saved
        importlib.reload(fx)
        fx.HEALTH_FILE = Path(tempfile.gettempdir()) / "fx_signal_test_health.json"


# ─────────────────────────────────────────────────────────────
# ロング保護ガード（dow_guard.py）とペア別エンジン選択
# ─────────────────────────────────────────────────────────────
import dow_guard as dg  # noqa: E402


def _ohlc_df(closes, start="2024-01-01", wick=0.05):
    # High/Low は終値±ヒゲ（始値基準にすると山頂の High が隣と同値になりピボットが立たない）
    idx = pd.date_range(start, periods=len(closes), freq="D")
    c = pd.Series(closes, index=idx, dtype=float)
    o = c.shift(1).fillna(c.iloc[0])
    return pd.DataFrame({"Open": o, "High": c + wick, "Low": c - wick, "Close": c})


def _wave_closes():
    # 上昇(谷→山)・調整・高値更新・大きな崩れ・底固め・復帰、を日足で描く決定論的な系列
    seg = []
    seg += [100 + 0.5 * i for i in range(12)]          # 上昇 → 山 A(105.5)
    seg += [105.5 - 0.3 * i for i in range(1, 9)]      # 調整 → 谷 B(103.1)  ※8本=大きい波
    seg += [103.1 + 0.6 * i for i in range(1, 10)]     # 高値更新 → 山 C(108.5)。ここで押し安値=B
    seg += [108.5 - 0.2 * i for i in range(1, 8)]      # 浅い調整 → 谷 D(107.1)（押し安値はまだ B）
    seg += [107.1 + 0.4 * i for i in range(1, 8)]      # 再上昇 → 山 E(109.9)。C 超えで押し安値=D
    seg += [109.9 - 0.9 * i for i in range(1, 12)]     # 大崩れ: D(107.1) を終値で割る → EXIT
    seg += [100.0 + 0.2 * i for i in range(1, 9)]      # 底固め → 小山 F(101.6)
    seg += [101.6 - 0.1 * i for i in range(1, 8)]      # 小さな押し
    seg += [100.9 + 0.5 * i for i in range(1, 10)]     # F を終値で超える → HOLD 復帰
    return seg


def test_dow_pivots_confirm_without_lookahead():
    df = _ohlc_df(_wave_closes())
    piv = dg.find_pivots(df, 3)
    assert piv and all(p.kind in ("H", "L") for p in piv)
    # 山は左右3本より厳密に高い: 末尾3本は確定できない
    assert all(p.pos + 3 <= len(df) - 1 for p in piv)
    kinds = [p.kind for p in piv]
    assert "H" in kinds and "L" in kinds


def test_dow_state_machine_rules():
    df = _ohlc_df(_wave_closes())
    res = dg.analyze(df)
    st = res["states"].dropna()
    assert set(st.unique()) == {dg.STATE_HOLD, dg.STATE_EXIT}, "HOLD/EXIT の両方が出る系列"
    flips = st[st != st.shift()]
    seq = list(flips)
    # 上昇(HOLD) → 崩れ(EXIT) → 復帰(HOLD) の順で遷移する
    assert dg.STATE_HOLD in seq and dg.STATE_EXIT in seq
    i_h = seq.index(dg.STATE_HOLD)
    assert dg.STATE_EXIT in seq[i_h + 1:] and dg.STATE_HOLD in seq[seq.index(dg.STATE_EXIT, i_h + 1) + 1:]
    # 崩れの直前の実質押し安値は「浅い調整の谷 D(≈107.1)」= 高値更新で切り上がっている
    exit_day = flips[flips == dg.STATE_EXIT].index[0]
    before = dg.analyze(df.loc[:exit_day - pd.Timedelta(days=1)])
    assert before["trend"] == dg.STATE_HOLD and before["key_low"] is not None
    assert 106.5 < before["key_low"].price < 107.5, before["key_low"]
    # 崩れた日は終値 < 実質押し安値（ヒゲ先）
    assert float(df.loc[exit_day, "Close"]) < before["key_low"].price
    # 最終状態は復帰(HOLD)で、押し安値は底固めの谷に更新されている
    assert res["trend"] == dg.STATE_HOLD and res["key_low"].price < 103.0


def test_dow_online_prefix_consistency():
    # 系列を途中で切っても過去の状態が変わらない（先読みなし）
    df = _ohlc_df(_wave_closes())
    full = dg.analyze(df)["states"]
    for cut in range(20, len(df), 7):
        part = dg.analyze(df.iloc[:cut])["states"]
        assert (full.iloc[:cut].fillna("-") == part.fillna("-")).all(), f"cut={cut} で不一致"


def test_dow_min_wave_ignores_small_waves():
    df = _ohlc_df(_wave_closes())
    big = dg.analyze(df, lr=3, min_wave=6)["states"].dropna()
    small = dg.analyze(df, lr=3, min_wave=0)["states"].dropna()
    n_big = int((big != big.shift()).sum())
    n_small = int((small != small.shift()).sum())
    assert n_big <= n_small, "大きく数える方が切替は少ないか同数"


def test_dow_evaluate_guards_and_message():
    df = _ohlc_df(_wave_closes())
    last = df.index[-1]
    # 本数不足
    assert dg.evaluate(df.iloc[:30], now_utc=datetime(2024, 3, 1, tzinfo=timezone.utc)) is None
    # 鮮度不良
    assert dg.evaluate(df, now_utc=(last + pd.Timedelta(days=30)).to_pydatetime().replace(tzinfo=timezone.utc)) is None
    # 正常: 当日バーは落ちる → 最終確定日は last-1
    now = last.to_pydatetime().replace(tzinfo=timezone.utc)
    info = dg.evaluate(df, now_utc=now)
    assert info is not None and info["state"] in dg.STATES
    assert (last - pd.Timedelta(days=1)).strftime("%Y-%m-%d") in info["time"]
    assert "change_1d_pct" in info and "change_5d_pct" in info
    assert "change_15m_pct" not in info and "change_1h_pct" not in info
    m = dg.format_message(info, label="AUD/JPY", first_run=True)
    assert "監視を開始" in m and info["state"] in m
    ex = {"state": dg.STATE_EXIT, "price": 100.0, "key_low": 101.2, "key_low_date": "2024-03-01",
          "reentry_high": None, "time": "2024-03-10 確定日足"}
    m2 = dg.format_message(ex, label="NZD/JPY")
    assert "離脱" in m2 and "101.200" in m2 and "半分" in m2
    ho = {**ex, "state": dg.STATE_HOLD, "key_low": 99.5}
    m3 = dg.format_message(ho, label="NZD/JPY")
    assert "復帰" in m3 and "99.500" in m3


def test_guard_engine_mapping_and_env_override():
    import os as _os
    saved = _os.environ.pop("GUARD_DOW_PAIRS", None)
    try:
        assert fx.guard_engine("USDJPY=X") == "sma"
        assert fx.guard_engine("AUDJPY=X") == "dow" and fx.guard_engine("NZDJPY=X") == "dow"
        assert fx.guard_engine("EURUSD=X") == fx.GUARD_DEFAULT_ENGINE
        _os.environ["GUARD_DOW_PAIRS"] = "usdjpy, EURUSD=X"
        assert fx.guard_engine("USDJPY=X") == "dow" and fx.guard_engine("EURUSD=X") == "dow"
        assert fx.guard_engine("AUDJPY=X") == "sma"
        _os.environ["GUARD_DOW_PAIRS"] = ""
        assert fx.guard_engine("AUDJPY=X") == "sma"
    finally:
        if saved is None:
            _os.environ.pop("GUARD_DOW_PAIRS", None)
        else:
            _os.environ["GUARD_DOW_PAIRS"] = saved
    assert fx.guard_module("dow") is dg and fx.guard_module("sma") is lg


def test_guard_backtest_dow_engine_runs():
    df = _ohlc_df(_wave_closes())
    with patched(bt, fetch_history=lambda t, p, i, s: df):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = bt.backtest_guard("NZDJPY=X", "1y", "1d", detail=True, source="x", engine="dow")
    out = buf.getvalue()
    assert rc == 0 and "ガード[dow]" in out and "切替履歴" in out


def test_dow_wave_rule_leg_ignores_short_bounces():
    # 下落中に 4〜5本の短い戻りを挟む系列: leg は戻り高値を数えず復帰ラインが据え置き、hill は追従する
    seg = [100 + 0.5 * i for i in range(12)]            # 上昇 → 山
    seg += [105.5 - 0.3 * i for i in range(1, 9)]        # 谷（8本）
    seg += [103.1 + 0.6 * i for i in range(1, 10)]       # 高値更新 → 押し安値確定
    seg += [108.5 - 0.9 * i for i in range(1, 12)]       # 崩れ → EXIT
    for k in range(3):                                   # 短い戻り(4本)と下落(8本)を繰り返す
        base = seg[-1]
        seg += [base + 0.3 * i for i in range(1, 5)]
        seg += [seg[-1] - 0.4 * i for i in range(1, 9)]
    df = _ohlc_df(seg)
    leg = dg.analyze(df, lr=3, min_wave=6, wave_rule="leg")
    hill = dg.analyze(df, lr=3, min_wave=6, wave_rule="hill")
    assert leg["trend"] == dg.STATE_EXIT and hill["trend"] == dg.STATE_EXIT
    # hill は短い戻りの山を採用するので復帰ラインが下がる。leg は崩れ直後の山（または未形成）のまま
    if leg["reentry_high"] is not None and hill["reentry_high"] is not None:
        assert hill["reentry_high"].price <= leg["reentry_high"].price
        assert hill["reentry_high"].pos >= leg["reentry_high"].pos
    # 既定は leg（較正で安定）
    assert dg.WAVE_RULE == "leg"


# ─────────────────────────────────────────────────────────────
# プレーン実行用ランナー（pytest 不要）
# ─────────────────────────────────────────────────────────────
def _run_all() -> int:
    tests = sorted(
        (name, obj)
        for name, obj in globals().items()
        if name.startswith("test_") and callable(obj)
    )
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
