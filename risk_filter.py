#!/usr/bin/env python3
"""ニュース・経済イベントの「リスクフィルター層」.

決定論的な SMA シグナル判定には一切手を入れず、シグナル通知に「警告メタ情報」を
添えるだけの付加層。役割分担を厳格に守る:

- 経済指標カレンダーの取得・パース・「次の重要イベントまでの時間」算出は、すべて
  このモジュール内で **決定論的に** 行う（日時計算を LLM にやらせない）。
- LLM(Claude API) は **テキストの定性的な解釈だけ** に使う。売買判定・指標計算・
  日時計算はさせない。

設計上の絶対原則:
- リスク層が失敗しても本体のシグナル通知は絶対に止めない／落とさない。
  あらゆる失敗は握りつぶし「リスク不明(unknown)」を返して継続する。
- `RISK_FILTER_ENABLED = False` で丸ごと無効化できる。
- ANTHROPIC_API_KEY 未設定でも本体は動く（リスク層だけスキップ）。

返り値の status でサイレント故障を可視化する（"unknown" 一括に畳まない）:
    "ok"                   … LLMが評価し正規化に成功
    "disabled"             … RISK_FILTER_ENABLED=False
    "no_api_key"           … 有効だが APIキー未設定 / anthropic SDK 未導入
    "calendar_unavailable" … 全カレンダーソースが取得失敗
    "calendar_schema"      … 取得は成功したがフィードの構造が認識できない
    "llm_failed"           … LLM呼び出しが例外 / 応答なし
    "parse_failed"         … LLM応答はあったがJSON正規化に失敗

公開エントリは assess_risk() ただ1つ。本体 fx_signal.py からはこれを呼ぶだけ。
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests


# カレンダー取得の失敗種別を区別するための例外
class CalendarUnavailable(Exception):
    """全ソースが到達不能/通信失敗（リスク不明だが構造の問題ではない）。"""


class CalendarSchemaError(Exception):
    """到達はできたがフィードの構造が認識できない（フィールド名変更など）。"""

# ─────────────────────────────────────────────────────────────
# 設定フラグ（ここに集約）
# ─────────────────────────────────────────────────────────────
RISK_FILTER_ENABLED = True      # False でリスク層を丸ごと無効化
RISK_SUPPRESS_SIGNALS = False   # True かつ risk_level=high の時だけシグナルを情報のみに格下げ
RISK_LOOKAHEAD_HOURS = 6        # 何時間先までのイベントを警戒対象にするか
RISK_LOOKBACK_HOURS = 1         # 直近何時間前までの「通過済みイベント」も考慮するか

# 改善5（任意・既定OFF）: 警戒イベントが0件かつ値動きが穏やかなら、LLMを呼ばず
# 決定論的に low を返してコストを節約する。ONでも監査ログは残す（llm_skipped=True）。
RISK_SKIP_LLM_WHEN_QUIET = False
RISK_QUIET_PCT = 0.3            # |直近1h変化率| がこの%未満を「穏やか」とみなす

# LLM 設定（最安・最速ティア。最新IDは docs.claude.com で確認: 2026-05 時点 Haiku 4.5）
LLM_MODEL = "claude-haiku-4-5"
LLM_TEMPERATURE = 0.0
LLM_MAX_TOKENS = 600
LLM_TIMEOUT = 20      # 秒。LLMが長時間待って通知が大幅遅延するのを防ぐ
LLM_MAX_RETRIES = 1   # SDKの自動リトライ回数（バックオフによる遅延を抑える）
API_KEY_ENV = "ANTHROPIC_API_KEY"

# 監視対象通貨と対象インパクト
CURRENCIES = ("USD", "JPY")
TARGET_IMPACTS = ("High",)

# 経済指標カレンダー（APIキー不要を優先。上から順に到達確認し、使えるものを採用）
#   1. Forex Factory 週次JSON（キー不要・稼働確認済み・全通貨を一括取得）
#   2. JBlanked（※APIキーが必要なため、キー無し環境では到達しても401で失敗扱い）
# type:
#   "all"          … 1リクエストで全通貨のイベントを返す（FF）。
#   "per_currency" … URL の {cur} を通貨ごとに埋めて取得し、対象通貨ぶんを集約する。
#                    複数ペア対応のため、固定通貨ではなく監視ペアの通貨で動的に組む。
CALENDAR_SOURCES = (
    {
        "name": "forexfactory",
        "type": "all",
        "url": "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
        "parser": "ff",
    },
    {
        "name": "jblanked",
        "type": "per_currency",
        "url": "https://www.jblanked.com/news/api/forex-factory/calendar/week/?currency={cur}&impact=High",
        "parser": "jblanked",
    },
)

HTTP_TIMEOUT = 15  # 秒。requests には必ずタイムアウトを設定する
HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (fx-signal risk-filter)"}

RISK_LOG_FILE = Path(__file__).with_name("risk_log.jsonl")
TOKYO = ZoneInfo("Asia/Tokyo")

# 想定フィールド（Forex Factory）。スキーマ崩れ検知の基準にする。
FF_EXPECTED_FIELDS = ("country", "impact", "date", "title")

# 「リスク不明」を表す共通フォールバック（あらゆる失敗時に返す）
UNKNOWN_RISK = {
    "enabled": True,
    "available": False,
    "status": "unknown",
    "risk_level": "unknown",
    "advise_caution": False,
    "headline": "",
    "reason": "",
    "events": [],
}


def _result(status: str, **extra) -> dict:
    """status 付きの結果 dict を組み立てる。available は status=="ok" のみ True。"""
    r = {**UNKNOWN_RISK, "status": status, "available": status == "ok", "upcoming_events": []}
    r.update(extra)
    return r


# ─────────────────────────────────────────────────────────────
# カレンダー取得・パース（すべて決定論的）
# ─────────────────────────────────────────────────────────────
def _parse_ff(data) -> list[dict]:
    """Forex Factory 週次JSONをパースし、全通貨の High イベントを返す。

    スキーマ崩れ（フィールド名変更など）は CalendarSchemaError で通知する。
    「スキーマは正常だが今週は High が0件」は正常（空リストを返す）として扱い、
    両者を混同しない。
    """
    if not isinstance(data, list):
        raise CalendarSchemaError("FFフィードが配列ではありません")

    # 非空ペイロードなのに想定フィールドが揃っていない → スキーマ未認識。
    # 「どれか1つ」ではなく「各フィールドが先頭数件のどこかに存在する」ことを要求する。
    # こうしないと impact/date だけ改名された場合に全件スキップされ、偽の0件になる。
    if data:
        head = [it for it in data[:5] if isinstance(it, dict)]
        if not head or not all(
            any(field in it for it in head) for field in FF_EXPECTED_FIELDS
        ):
            missing = [f for f in FF_EXPECTED_FIELDS if not any(f in it for it in head)]
            raise CalendarSchemaError(
                f"FFフィードに想定フィールドが不足: {missing}"
            )

    events: list[dict] = []
    for item in data:
        try:
            currency = str(item.get("country", "")).upper()
            impact = str(item.get("impact", ""))
            # 通貨での絞り込みはペアごとに呼び出し側(_relevant_events)で行う。
            # ここでは全通貨の High イベントを保持する（複数ペア対応）。
            if impact not in TARGET_IMPACTS:
                continue
            dt = _parse_dt(item.get("date"))
            if dt is None:
                continue
            events.append(
                {
                    "title": str(item.get("title", "")).strip(),
                    "currency": currency,
                    "impact": impact,
                    "dt_utc": dt,
                }
            )
        except Exception:
            # 1件の異常で全体を落とさない
            continue
    return events


def _parse_jblanked(data) -> list[dict]:
    """JBlanked 形式をベストエフォートでパース（キー必須のため通常は到達しない）。"""
    events: list[dict] = []
    rows = data if isinstance(data, list) else data.get("data") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return events
    for item in rows:
        try:
            if not isinstance(item, dict):
                continue
            currency = str(item.get("currency") or item.get("country") or "").upper()
            impact = str(item.get("impact") or item.get("strength") or "")
            # 通貨絞り込みは呼び出し側で行う（全通貨の High を保持）。
            if impact and impact not in TARGET_IMPACTS:
                continue
            dt = _parse_dt(item.get("date") or item.get("datetime") or item.get("time"))
            if dt is None:
                continue
            events.append(
                {
                    "title": str(item.get("name") or item.get("title") or "").strip(),
                    "currency": currency or "?",
                    "impact": impact or "High",
                    "dt_utc": dt,
                }
            )
        except Exception:
            continue
    return events


_PARSERS = {"ff": _parse_ff, "jblanked": _parse_jblanked}


def _parse_dt(value) -> datetime | None:
    """ISO日時文字列を tz-aware な UTC datetime に変換。失敗で None。"""
    if not value or not isinstance(value, str):
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    # tz-naive なら UTC とみなす（イベント時刻は UTC 前提）
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _fetch_one(url: str) -> object:
    resp = requests.get(url, headers=HTTP_HEADERS, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _fetch_calendar(currencies: tuple[str, ...]) -> list[dict]:
    """到達できたソースから High インパクトのイベント一覧を取得する。

    - type="all" のソース（FF）は1リクエストで全通貨を返すのでそのまま採用。
    - type="per_currency" のソース（jblanked）は、対象 currencies ごとに URL を組んで
      取得し集約する（複数ペアでも対象通貨を取りこぼさない）。

    失敗種別を区別して送出する（呼び出し側で status に反映）:
    - 到達はできたが構造を認識できない → CalendarSchemaError
    - すべて到達不能/通信失敗            → CalendarUnavailable
    どのソースも成功しなかったとき、スキーマ崩れが観測されていればそれを優先して
    送出する（「到達はしている」ことが分かるため）。
    """
    net_error: Exception | None = None
    schema_error: CalendarSchemaError | None = None

    for src in CALENDAR_SOURCES:
        parser = _PARSERS[src["parser"]]
        # 1) 取得（通信）— 失敗は net_error として次ソースへ
        try:
            if src.get("type") == "per_currency":
                payloads = [_fetch_one(src["url"].format(cur=cur)) for cur in currencies]
            else:
                payloads = [_fetch_one(src["url"])]
        except Exception as exc:  # noqa: BLE001 - 通信失敗
            net_error = exc
            continue
        # 2) パース（構造）— スキーマ崩れは schema_error として次ソースへ
        try:
            events: list[dict] = []
            for payload in payloads:
                events.extend(parser(payload))
            return events  # 到達＆認識OK（0件＝休場週でも正常採用）
        except CalendarSchemaError as exc:
            schema_error = exc
            continue

    if schema_error is not None:
        raise schema_error
    raise CalendarUnavailable(f"全カレンダーソースが利用不可: {net_error}")


def _relevant_events(
    events: list[dict], now_utc: datetime, currencies: tuple[str, ...]
) -> list[dict]:
    """対象通貨かつ警戒ウィンドウ内のイベントを抽出し時刻順に並べる。"""
    lo = now_utc - timedelta(hours=RISK_LOOKBACK_HOURS)
    hi = now_utc + timedelta(hours=RISK_LOOKAHEAD_HOURS)
    out: list[dict] = []
    for ev in events:
        if currencies and ev["currency"] not in currencies:
            continue
        dt = ev["dt_utc"]
        if lo <= dt <= hi:
            delta_min = round((dt - now_utc).total_seconds() / 60.0)
            out.append(
                {
                    "title": ev["title"],
                    "currency": ev["currency"],
                    "impact": ev["impact"],
                    "time_jst": dt.astimezone(TOKYO).strftime("%Y-%m-%d %H:%M JST"),
                    "delta_min": delta_min,
                }
            )
    out.sort(key=lambda e: e["delta_min"])
    return out


# ─────────────────────────────────────────────────────────────
# LLM 呼び出し（テキストの定性解釈のみ）
# ─────────────────────────────────────────────────────────────
_SYSTEM_PROMPT = (
    "あなたはFXのリスク管理アシスタントです。入力の pair フィールドで指定された"
    "通貨ペアのトレードシグナルに添える『定性的なリスク評価』だけを行います。"
    "売買の是非・価格予測・指標計算・日時計算は"
    "一切しません（それらは別システムが決定論的に処理済み）。\n"
    "与えられた『直近・直後の重要経済イベント一覧』と『直近の価格変化率』から、"
    "(1)イベントによるボラティリティ拡大リスク (2)急変動による為替介入の可能性、を評価してください。\n"
    "出力は次のスキーマに厳密準拠した JSON のみ。前後に説明文・コードフェンス・改行を付けないこと。\n"
    '{"risk_level":"high|medium|low","advise_caution":true/false,'
    '"headline":"日本語40字以内の短い警告見出し","reason":"日本語1〜2文の根拠",'
    '"events":["重要イベント名と日本時間の時刻", ...]}\n'
    "headline と reason は日本語。重要イベントが無く価格変動も穏やかなら risk_level は low。"
)


def _extract_json(text: str) -> dict | None:
    """防御的に JSON を取り出す。失敗で None。"""
    if not text:
        return None
    s = text.strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s.lower().startswith("json"):
            s = s[4:]
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else None
    except (json.JSONDecodeError, ValueError):
        pass
    # 最初の { から最後の } までを試す
    i, j = s.find("{"), s.rfind("}")
    if i != -1 and j != -1 and j > i:
        try:
            obj = json.loads(s[i : j + 1])
            return obj if isinstance(obj, dict) else None
        except (json.JSONDecodeError, ValueError):
            return None
    return None


def _call_llm(payload: dict) -> tuple[str, dict | None]:
    """Claude API を呼び (call_status, raw_json) を返す。

    call_status:
        "no_api_key" … APIキー未設定 / anthropic SDK 未導入（意図的にオフ相当）
        "llm_failed" … 呼び出しが例外 / 応答テキストなし
        "got_text"   … 応答テキストを取得（raw_json は抽出結果。失敗時 None）
    """
    api_key = os.environ.get(API_KEY_ENV)
    if not api_key:
        return ("no_api_key", None)
    try:
        import anthropic  # 遅延 import（未インストールでも本体を壊さない）
    except ImportError:
        return ("no_api_key", None)

    try:
        client = anthropic.Anthropic(
            api_key=api_key, timeout=LLM_TIMEOUT, max_retries=LLM_MAX_RETRIES
        )
        resp = client.messages.create(
            model=LLM_MODEL,
            max_tokens=LLM_MAX_TOKENS,
            temperature=LLM_TEMPERATURE,
            system=_SYSTEM_PROMPT,
            messages=[
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                # assistant を "{" で prefill して JSON のみ出力を強制する
                {"role": "assistant", "content": "{"},
            ],
        )
        text = "{" + "".join(
            b.text for b in resp.content if getattr(b, "type", None) == "text"
        )
    except Exception:
        return ("llm_failed", None)

    if not text or text == "{":
        return ("llm_failed", None)
    return ("got_text", _extract_json(text))


def _coerce_bool(value) -> bool:
    """真偽値を頑健に判定。文字列 "false"/"true" も正しく扱う。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "1")
    if isinstance(value, (int, float)):
        return bool(value)
    return False


def _normalize_llm_output(raw: dict | None) -> dict | None:
    """LLM出力を検証・正規化。スキーマ不正なら None。"""
    if not isinstance(raw, dict):
        return None
    level = str(raw.get("risk_level", "")).lower()
    if level not in ("high", "medium", "low"):
        return None
    events = raw.get("events", [])
    if not isinstance(events, list):
        events = []
    return {
        "enabled": True,
        "available": True,
        "status": "ok",
        "risk_level": level,
        # 文字列 "false" を True と誤判定しないよう頑健に変換
        "advise_caution": _coerce_bool(raw.get("advise_caution", False)),
        "headline": str(raw.get("headline", "")).strip()[:80],
        "reason": str(raw.get("reason", "")).strip(),
        "events": [str(e) for e in events][:8],
    }


# ─────────────────────────────────────────────────────────────
# 監査ログ（追記式・失敗しても無視）
# ─────────────────────────────────────────────────────────────
def _log(entry: dict) -> None:
    try:
        with RISK_LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


# ─────────────────────────────────────────────────────────────
# 公開エントリ
# ─────────────────────────────────────────────────────────────
def assess_risk(
    signal_info: dict,
    currencies: tuple[str, ...] | None = None,
    pair_label: str | None = None,
) -> dict:
    """シグナル発火時のリスク評価を返す。

    Args:
        signal_info: fx_signal の判定結果 dict（state, price, 価格変化率などを含む）。
        currencies: 対象通貨ペアの通貨（例 ('AUD','JPY')）。カレンダー絞り込みに使う。
            None なら既定の CURRENCIES。
        pair_label: 通貨ペア表示名（例 'AUD/JPY'）。LLM入力に渡す。

    Returns:
        常に dict を返し、例外は決して送出しない。失敗時も status 付きで返す。
        キー: enabled, available, status, risk_level(high/medium/low/unknown),
              advise_caution, headline, reason, events, upcoming_events
    """
    if not RISK_FILTER_ENABLED:
        return _result("disabled", enabled=False)

    now_utc = datetime.now(timezone.utc)
    target_currencies = tuple(currencies) if currencies else CURRENCIES
    pair = pair_label or "/".join(target_currencies)

    # 1) カレンダー取得＋時間計算（決定論的）。失敗種別を status に反映。
    try:
        all_events = _fetch_calendar(target_currencies)
    except CalendarSchemaError as exc:
        print(f"[{pair}] カレンダーのスキーマを認識できません: {exc}", file=sys.stderr)
        return _result("calendar_schema")
    except Exception:
        return _result("calendar_unavailable")

    upcoming = _relevant_events(all_events, now_utc, target_currencies)

    def _make_log(status, output_raw, output_normalized, payload, **extra):
        _log(
            {
                "timestamp": now_utc.astimezone(TOKYO).strftime("%Y-%m-%d %H:%M:%S JST"),
                "pair": pair,
                "status": status,
                "model": LLM_MODEL,
                "input": payload,
                "output_raw": output_raw,
                "output_normalized": output_normalized,
                **extra,
            }
        )

    # 2) LLM へ渡す構造化入力を組み立て（日時計算は上で済ませてある）
    payload = {
        "pair": pair,
        "signal": {
            "state": signal_info.get("state"),
            "price": signal_info.get("price"),
            "change_15m_pct": signal_info.get("change_15m_pct"),
            "change_1h_pct": signal_info.get("change_1h_pct"),
        },
        "now_jst": now_utc.astimezone(TOKYO).strftime("%Y-%m-%d %H:%M JST"),
        "lookahead_hours": RISK_LOOKAHEAD_HOURS,
        "events": upcoming,
    }

    # 改善5（任意）: イベント0件かつ値動きが穏やかなら、LLMを呼ばず決定論的に low。
    if RISK_SKIP_LLM_WHEN_QUIET and not upcoming:
        ch = signal_info.get("change_1h_pct")
        if ch is None or abs(ch) < RISK_QUIET_PCT:
            res = _result(
                "ok",
                risk_level="low",
                reason="重要イベントなし・値動き穏やか（LLMスキップ）",
                upcoming_events=upcoming,
            )
            _make_log("ok", None, res, payload, llm_skipped=True)
            return res

    # 3) LLM 呼び出し（テキスト解釈のみ）。call_status で失敗種別を区別する。
    call_status, raw = _call_llm(payload)
    if call_status == "no_api_key":
        status, normalized = "no_api_key", None
    elif call_status == "llm_failed":
        status, normalized = "llm_failed", None
    else:  # got_text
        normalized = _normalize_llm_output(raw)
        status = "ok" if normalized is not None else "parse_failed"

    # 4) 監査ログ（入力・出力・タイムスタンプ・status）を追記。
    _make_log(status, raw, normalized, payload)

    if status == "ok":
        normalized["upcoming_events"] = upcoming
        return normalized
    return _result(status, upcoming_events=upcoming)
