#!/usr/bin/env bash
# Безопасный САМО-рестарт claude-ops-bot.
#
# Зачем: прямой `sudo systemctl restart/stop claude-ops-bot` из шелла самого бота
# убивает этот шелл — он живёт в cgroup сервиса, и systemctl сносит весь cgroup
# ПОСРЕДИ команды. Поэтому `stop && start` не доживает до start → бот остаётся
# выключенным (так он вырубил себя 2026-05-30, см. CLAUDE.md).
#
# Решение: systemd-run запускает рестарт в ОТДЕЛЬНОМ transient-юните вне cgroup
# сервиса → он переживает смерть бота и гарантированно доводит restart до конца.
set -euo pipefail

sudo systemd-run --collect --quiet \
  --unit="cops-self-restart-$(date +%s)" \
  --on-active=1 --timer-property=AccuracySec=200ms \
  systemctl restart claude-ops-bot

echo "🔁 Рестарт claude-ops-bot запланирован (~1-2с, detached через systemd-run, вне cgroup)."
echo "   Текущий процесс скоро завершится; веб (:8787) и Telegram-бот поднимутся сами."
