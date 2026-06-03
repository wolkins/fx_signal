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
import fx_signal as fx
import risk_filter as rf

# テストは作業ツリーを汚さない（監査ログ・健全性ファイルを一時パスへ退避）。
rf.RISK_LOG_FILE = Path(tempfile.gettempdir()) / "fx_signal_test_risk_log.jsonl"
fx.HEALTH_FILE = Path(tempfile.gettempdir()) / "fx_signal_test_health.json"


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
                 fetch_data=lambda t: None,  # 鮮度チェックはスキップ（取得しない）
                 notify=sent.append):
        fx.heartbeat()
    assert sent and "稼働中" in sent[0] and "USD/JPY" in sent[0] and "LONG" in sent[0]
    assert "古い" not in sent[0]  # データ無し（None）なら鮮度警告は出さない


def test_heartbeat_stale_feed_warns():
    import pandas as pd
    sent: list[str] = []
    # 最新足が STALE_MINUTES より十分古い df を返す
    old = pd.date_range(end=pd.Timestamp("2026-05-26 00:00", tz="UTC"), periods=3, freq="5min")
    stale_df = pd.DataFrame({"Close": [1.0, 2.0, 3.0]}, index=old)
    with patched(fx, PAIRS=["USDJPY=X"],
                 load_state=lambda p: {"state": "LONG", "updated_at": "x"},
                 fetch_data=lambda t: stale_df,
                 STALE_MINUTES=90,
                 notify=sent.append):
        fx.heartbeat()
    assert "古い" in sent[0]  # 全ペア古い → 固まり疑いの警告


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
