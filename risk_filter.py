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

公開エントリは assess_risk() ただ1つ。本体 fx_signal.py からはこれを呼ぶだけ。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

# ─────────────────────────────────────────────────────────────
# 設定フラグ（ここに集約）
# ─────────────────────────────────────────────────────────────
RISK_FILTER_ENABLED = True      # False でリスク層を丸ごと無効化
RISK_SUPPRESS_SIGNALS = False   # True かつ risk_level=high の時だけシグナルを情報のみに格下げ
RISK_LOOKAHEAD_HOURS = 6        # 何時間先までのイベントを警戒対象にするか
RISK_LOOKBACK_HOURS = 1         # 直近何時間前までの「通過済みイベント」も考慮するか

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

# 「リスク不明」を表す共通フォールバック（あらゆる失敗時に返す）
UNKNOWN_RISK = {
    "enabled": True,
    "available": False,
    "risk_level": "unknown",
    "advise_caution": False,
    "headline": "",
    "reason": "",
    "events": [],
}


# ─────────────────────────────────────────────────────────────
# カレンダー取得・パース（すべて決定論的）
# ─────────────────────────────────────────────────────────────
def _parse_ff(data) -> list[dict]:
    """Forex Factory 週次JSONをパースし、USD/JPY の High イベントを返す。"""
    events: list[dict] = []
    if not isinstance(data, list):
        return events
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
    すべて失敗したら RuntimeError を送出（呼び出し側で握りつぶす）。
    """
    last_error: Exception | None = None
    for src in CALENDAR_SOURCES:
        try:
            parser = _PARSERS[src["parser"]]
            if src.get("type") == "per_currency":
                agg: list[dict] = []
                for cur in currencies:
                    agg.extend(parser(_fetch_one(src["url"].format(cur=cur))))
                return agg  # 到達できた（0件＝休場週でも採用）
            # type="all": 全通貨を一括取得
            return parser(_fetch_one(src["url"]))
        except Exception as exc:  # noqa: BLE001 - 1ソースの失敗で全体は止めない
            last_error = exc
            continue
    raise RuntimeError(f"全カレンダーソースが利用不可: {last_error}")


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


def _call_llm(payload: dict) -> dict | None:
    """Claude API を呼び、構造化リスク評価JSONを返す。失敗で None。"""
    api_key = os.environ.get(API_KEY_ENV)
    if not api_key:
        return None
    try:
        import anthropic  # 遅延 import（未インストールでも本体を壊さない）
    except ImportError:
        return None

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
        return _extract_json(text)
    except Exception:
        return None


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
        常に dict を返し、例外は決して送出しない。失敗時は UNKNOWN_RISK 相当。
        キー: enabled, available, risk_level(high/medium/low/unknown),
              advise_caution, headline, reason, events, upcoming_events
    """
    if not RISK_FILTER_ENABLED:
        return {**UNKNOWN_RISK, "enabled": False, "upcoming_events": []}

    result = {**UNKNOWN_RISK, "upcoming_events": []}
    now_utc = datetime.now(timezone.utc)
    target_currencies = tuple(currencies) if currencies else CURRENCIES

    # 1) カレンダー取得＋時間計算（決定論的）。失敗は握りつぶし「リスク不明」で続行。
    try:
        all_events = _fetch_calendar(target_currencies)
    except Exception:
        return result  # カレンダー到達不可 → リスク不明（本体は通常通り）

    upcoming = _relevant_events(all_events, now_utc, target_currencies)
    result["upcoming_events"] = upcoming

    # 2) LLM へ渡す構造化入力を組み立て（日時計算は上で済ませてある）
    payload = {
        "pair": pair_label or "/".join(target_currencies),
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

    # 3) LLM 呼び出し（テキスト解釈のみ）。失敗・不正は「リスク不明」フォールバック。
    raw = _call_llm(payload)
    normalized = _normalize_llm_output(raw)

    # 4) 監査ログ（入力・出力・タイムスタンプ）を追記。リプレイ不可なLLMの評価記録。
    _log(
        {
            "timestamp": now_utc.astimezone(TOKYO).strftime("%Y-%m-%d %H:%M:%S JST"),
            "pair": payload["pair"],
            "model": LLM_MODEL,
            "input": payload,
            "output_raw": raw,
            "output_normalized": normalized,
        }
    )

    if normalized is None:
        return result  # LLM未実行/失敗/パース不可 → リスク不明
    normalized["upcoming_events"] = upcoming
    return normalized
