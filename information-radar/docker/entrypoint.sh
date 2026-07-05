#!/bin/sh
# entrypoint.sh —— run: 立即执行一次；cron: 常驻定时（默认）
set -e

MODE="${1:-cron}"

if [ "$MODE" = "run" ]; then
    exec python -m paper_bridge.cli run
elif [ "$MODE" = "cron" ]; then
    printenv | grep -E '^(RADAR_|OPENAI_|FEISHU_|FRESHRSS_|ZOTERO_|BILIBILI_|WEWE_|HTTP_PROXY|HTTPS_PROXY|NO_PROXY|TZ|RSSHUB_|WEWE_RSS_)=' > /etc/paper-bridge.env
    CRON_EXPR="${RADAR_CRON:-30 8 * * *}"
    echo "$CRON_EXPR cd /app && env $(cat /etc/paper-bridge.env | tr '\n' ' ') python -m paper_bridge.cli run >> /app/logs/cron.log 2>&1" > /etc/cron.d/paper-bridge
    chmod 0644 /etc/cron.d/paper-bridge
    crontab /etc/cron.d/paper-bridge
    echo "[entrypoint] cron started: $CRON_EXPR ($TZ)"
    if [ "${RUN_ON_STARTUP:-false}" = "true" ]; then
        python -m paper_bridge.cli run || true
    fi
    exec cron -f
else
    exec "$@"
fi
