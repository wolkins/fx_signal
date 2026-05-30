# FX シグナル監視ツール（複数通貨ペア対応）

複数の通貨ペアについて、短期SMAと長期SMAのクロスで「買い目線(LONG)/売り目線(SHORT)」を
判定し、**状態が変わった瞬間だけ** Slack に通知する個人用ツールです。

- 自動売買は **しません**。発注は手動で行う前提です。
- GitHub Actions の cron で5分おきに自動実行します。
- 同じ状態が続く間は通知しません（チャタリング防止）。
- 既定の監視ペア: **USD/JPY, AUD/JPY**（`fx_signal.py` の `PAIRS` で増減）。

---

## 仕組み

| 項目 | 内容 |
|------|------|
| データ取得 | [yfinance](https://pypi.org/project/yfinance/)（APIキー不要）。`PAIRS` の各ティッカー（例 `USDJPY=X`, `AUDJPY=X`）、5分足、`period="5d"` |
| 判定 | 短期SMA(20) > 長期SMA(50) → `LONG` / 短期 < 長期 → `SHORT`（ペアごとに独立） |
| 参考情報 | RSI(14) を計算し通知に併記 |
| 状態管理 | **ペアごと**に `state_<PAIR>.json`（例 `state_USDJPY.json`）へ保存し、前回と変わった時だけ通知 |
| 通知 | Slack Incoming Webhook（環境変数 `SLACK_WEBHOOK_URL`） |

監視ペアは `fx_signal.py` 冒頭の `PAIRS` リストで増減できます（例 `["USDJPY=X", "AUDJPY=X", "EURUSD=X"]`）。
表示名・小数桁・リスク層のカレンダー対象通貨はティッカーから**自動導出**されます
（JPYクロスは小数3桁、それ以外は5桁）。本数・足種は `SHORT_SMA`, `LONG_SMA`, `INTERVAL`, `PERIOD` で変更可能です。

> **複数ペアの独立性:** 各ペアは独立に判定・通知され、1ペアの取得失敗が他ペアを止めません。
> リスク層(LLM)は各ペアの状態変化時のみ、そのペアの対象通貨の経済指標で評価します。

> **チャタリング防止の補足:** `state.json` により「同じ状態が続く間は通知しない」のが基本動作です。
> さらに SMA が拮抗して境界で LONG/SHORT が揺れるのを抑えたい場合は、定数 `HYSTERESIS_PCT`
> を `0.0` から小さな値（例 `0.02` ≒ 0.02%）に上げると、その乖離幅未満では状態を切り替えません。

---

## セットアップ手順

### 1. Slack Incoming Webhook を作る

1. https://api.slack.com/apps にアクセスし **「Create New App」→「From scratch」** を選択。
2. アプリ名とワークスペースを指定して作成。
3. 左メニュー **「Incoming Webhooks」** を開き、トグルを **On** にする。
4. ページ下部 **「Add New Webhook to Workspace」** をクリックし、通知を送るチャンネルを選択。
5. 発行された Webhook URL（`https://hooks.slack.com/services/...`）をコピー。

> ⚠️ この URL は秘密情報です。コードに直書きせず、必ず Secret / 環境変数で渡してください。

### 2. GitHub Secret に登録

1. このリポジトリの **Settings → Secrets and variables → Actions** を開く。
2. **「New repository secret」** をクリック。
3. Name: `SLACK_WEBHOOK_URL` / Value: コピーした URL を貼り付けて保存。

### 3. パブリックリポジトリ推奨の理由

GitHub Actions の **無料枠は、パブリックリポジトリなら実質無制限**です。
プライベートリポジトリだと月あたりの無料実行時間に上限があり、5分おきの cron で
枠を消費していきます。本ツールは秘密情報を Secret で管理しコードには含めないため、
**パブリックリポジトリでの運用を推奨**します。

> なお workflow ファイルでは `runs-on: ubuntu-latest` を使っています。
> macOS ランナーは無料枠を **10倍** 消費するため使いません。

---

## ローカルでのテスト手順

```bash
pip install -r requirements.txt

# Webhook 未設定でも動作確認できます（通知内容は標準エラー出力に表示）
python fx_signal.py

# 実際に Slack へ飛ばしてテストする場合
export SLACK_WEBHOOK_URL="https://hooks.slack.com/services/XXX/YYY/ZZZ"
python fx_signal.py
```

- 初回実行（そのペアの `state_<PAIR>.json` が無い）時は「監視を開始しました」を1回だけ送り、状態を保存します。
- 2回目以降は、状態が前回と変わった時だけ通知します（ペアごとに独立）。
- 状態判定・チャタリング防止の挙動は `state_<PAIR>.json` を編集 / 削除して確認できます。

```bash
# USD/JPY の状態を強制的に SHORT にして次回の変化通知を試す
echo '{"state": "SHORT"}' > state_USDJPY.json
python fx_signal.py
```

### 過去データでのバックテスト（読み取り専用）

`backtest.py` で、本体と同じ決定論ロジックを過去データに流して転換頻度を検証できます。

```bash
python backtest.py                  # PAIRS 全ペアを 60日・5分足で要約
python backtest.py 90d 15m          # 期間/足種を指定して全ペア
python backtest.py USDJPY=X 60d 5m  # 単一ペアを転換履歴つきで詳細表示
```

#### データ源の切り替え（`--source`）

| source | キー | 特徴 |
|--------|:---:|------|
| `yfinance`（既定） | 不要 | 手軽。ただし**5分足は約60日が上限**（Yahooの仕様） |
| `dukascopy` | 不要 | **約2003年〜**。**5分足のまま長期**を遡れる（稼働ボットと同じ戦略で過去検証可） |

`dukascopy` を使うには任意依存を入れます（本番ボット/CIには不要）:

```bash
pip install -r requirements-backtest.txt
python backtest.py --source dukascopy USDJPY=X 1y 5m   # 5分足で1年前まで
```

- 取得は重いので `.cache/`（gitignore済み）にCSVキャッシュし、2回目以降は即時。
- データは Dukascopy（銀行）の流動性ベースで、稼働ボットの yfinance とは**別ソース**。
  SMAクロスの検証用途では差は小さいですが「同一データではない」点は留意。

---

## GitHub Actions での自動実行

`.github/workflows/fx-signal.yml` が以下を行います。

- FX市場が開いている時間帯のみ5分おきに実行（＋手動実行用の `workflow_dispatch`）。
  土曜と日曜日中（UTC）は実行しません。祝日など端境は `fx_signal.py` の
  `STALE_MINUTES` ガードが「最新足が古い＝市場クローズ」を検知して二重に弾きます。
- `python fx_signal.py` を実行し、各ペアの状態変化があれば Slack 通知。
- `state_<PAIR>.json` に差分があれば commit & push（差分が無ければスキップ）。
- `concurrency` で実行の重複を防止。

> GitHub Actions の cron はベストエフォートで、混雑時は数分遅延することがあります。

---

## リスクフィルター層（`risk_filter.py`）

経済イベント・ニュースの「リスク警告」をシグナル通知に**添えるだけ**の付加層です。
**決定論的な SMA 判定そのものには一切手を入れません。**

### 役割分担（厳格）
- **コード（決定論的）**: 経済指標カレンダー取得・パース・「次の重要イベントまでの時間」算出・
  価格変化率の計算。バックテスト可能なまま。
- **LLM（テキスト解釈のみ）**: 上記の構造化データを受け取り、定性的なリスク評価
  （見出し・根拠）を返すだけ。**売買判定・指標計算・日時計算はさせません。**

### 動作
- データ源: Forex Factory 週次JSON（APIキー不要）。USD/JPY の High インパクトのみ抽出。
- LLM を呼ぶのは **シグナルが発火する瞬間（状態変化時）だけ**。状態変化が無い回・
  初回起動・市場クローズ中は **一切呼びません**（コスト最小化）。
- `risk_level=high` または `advise_caution=true` の時だけ、通知の先頭に ⚠️ 警告行を付けます。
  それ以外は従来どおりの通知です。
- 監査ログ `risk_log.jsonl` に「入力・出力JSON・タイムスタンプ」を追記し、後から人手で
  レビューできます（機密は記録しません）。

### グレースフルデグレード（本体は絶対に止めない）
- カレンダー到達失敗 / タイムアウト / JSON不正 / LLM失敗 / パース失敗 / `ANTHROPIC_API_KEY` 未設定 —
  いずれの場合も「リスク不明」として**通常どおりシグナル通知を出します**。本体は落ちません。

### 失敗の可視化（status とフッター）
リスク層の失敗を「警告なし(low)」と取り違えないよう、`assess_risk` は `status` を返します。

| status | 意味 | 末尾フッター |
|--------|------|:---:|
| `ok` | LLMが評価し正規化成功 | 出さない |
| `disabled` | `RISK_FILTER_ENABLED=False` | 出さない |
| `no_api_key` | APIキー未設定 / SDK未導入（意図的オフ） | 出さない |
| `calendar_unavailable` | 全カレンダーソースが取得失敗 | **出す** |
| `calendar_schema` | 取得は成功したが構造を認識できない | **出す** |
| `llm_failed` | LLM呼び出しが例外/応答なし | **出す** |
| `parse_failed` | LLM応答はあったがJSON正規化に失敗 | **出す** |

「設定上は動くはずなのに失敗した」4種だけ、通知の**末尾**に控えめな1行を付けます
（`ℹ️ リスク評価は取得できませんでした（本体シグナルは通常どおり）`）。`high` 用の先頭 ⚠️ ブロックとは別物です。
なお「フィードは正常だが今週は High イベントが0件」は**正常(low扱い)**で、`calendar_schema` とは区別します。

### 設定（`risk_filter.py` 冒頭の定数）
| 定数 | 既定 | 説明 |
|------|------|------|
| `RISK_FILTER_ENABLED` | `True` | `False` でリスク層を丸ごと無効化 |
| `RISK_SUPPRESS_SIGNALS` | `False` | `True` かつ `risk_level=high` の時だけシグナルを「情報のみ」に格下げ |
| `RISK_LOOKAHEAD_HOURS` | `6` | 何時間先までのイベントを警戒対象にするか |
| `RISK_SKIP_LLM_WHEN_QUIET` | `False` | `True` でイベント0件かつ値動き穏やか時はLLMを呼ばず決定論的に low（監査ログは残す） |
| `LLM_MODEL` | `claude-haiku-4-5` | 最安・最速ティア |
| `LLM_TEMPERATURE` | `0.0` | 決定性重視 |

### セットアップ（任意）
リスク層を使うには `ANTHROPIC_API_KEY` を Secret 登録します（`SLACK_WEBHOOK_URL` と同じ手順）。
**未登録でも本体シグナルはそのまま動きます**（リスク層だけスキップ）。

```bash
# ローカルでリスク層込みのテスト
export ANTHROPIC_API_KEY="sk-ant-..."
python fx_signal.py
```

> ⚠️ **キー入れ忘れの儀式:** `RISK_FILTER_ENABLED=True` のまま `ANTHROPIC_API_KEY` を
> 入れ忘れると `status="no_api_key"`（=意図的オフ扱い）になり、フッターも出ず**静かに無効**になります。
> これは仕様どおりの挙動です。「オンにしたつもりが鳴らない」を防ぐため、キー登録後に一度
> シグナルを発火させ、`risk_log.jsonl` の `status` が `ok` になっているか一度だけ確認してください。

### 回帰テスト

`test_fx_signal.py` が status 分岐・スキーマ検知・フッター条件・決定論一致などを固定します
（ネット・APIキー不要、作業ツリーを汚しません）。

```bash
python test_fx_signal.py     # pytest 不要。pytest があれば `pytest test_fx_signal.py` でも可
```

---

## やらないこと（範囲外）

- 自動発注・ブローカーAPI連携・口座連携は一切行いません。
- SMA判定ロジックの変更はしません（リスク層は判定に手を入れない付加層）。
- LLM に指標計算や日時計算はさせません。LLM の役割は定性的なテキスト解釈のみ。
- Slackアプリの作成、Secret 登録、リポジトリ作成はご自身で行ってください。
  コードからアカウント作成や認証は行いません。
