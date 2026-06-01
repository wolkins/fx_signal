#!/usr/bin/env bash
# さくらVPS等で fx_signal を実行するラッパー。
# system cron から呼ぶ想定。GitHub Actions のスロットルが無く、正確に5分間隔で動く。
#
# 使い方（cron）例:
#   */5 * * * * /path/to/fx_signal/deploy/run.sh            >> /path/to/fx_signal/cron.log 2>&1
#   0  8 * * 2 /path/to/fx_signal/deploy/run.sh --heartbeat >> /path/to/fx_signal/cron.log 2>&1
#
# 事前準備:
#   - リポジトリを clone
#   - python -m venv .venv && .venv/bin/pip install -r requirements.txt
#   - fx-signal.env を用意（fx-signal.env.example をコピーして秘密情報を記入）
set -euo pipefail

# このスクリプトの2つ上＝リポジトリroot（deploy/ の親）へ移動
cd "$(cd "$(dirname "$0")/.." && pwd)"

# 秘密情報を環境変数として読み込む（SLACK_WEBHOOK_URL, ANTHROPIC_API_KEY）
if [ -f fx-signal.env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./fx-signal.env
  set +a
fi

# 実行時刻のログ（追記）。状態は state_<PAIR>.json にローカル保存され、
# VPSではGit commitは不要（ファイルがそのまま永続化される）。
echo "----- $(date '+%Y-%m-%d %H:%M:%S %Z') run -----"
exec .venv/bin/python fx_signal.py "$@"
