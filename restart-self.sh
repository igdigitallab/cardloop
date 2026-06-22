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

# --on-active=6: задержка должна быть с запасом БОЛЬШЕ времени, которое harness
# тратит на фиксацию текущего хода/ответа. При 1с рестарт SIGTERM-ил процесс-ход
# агента (он живёт в этом же cgroup) ДО коммита результата tool-call → harness
# показывал ложный «Command failed with exit code 143». 6с дают ходу гарантированно
# завершиться и доставиться; рестарт срабатывает позже, без видимой «ошибки».
sudo systemd-run --collect --quiet \
  --unit="cops-self-restart-$(date +%s)" \
  --on-active=6 --timer-property=AccuracySec=200ms \
  systemctl restart claude-ops-bot

echo "🔁 Рестарт claude-ops-bot запланирован (~6с, detached через systemd-run, вне cgroup)."
echo "   Заверши ход СРАЗУ после этой команды (без bash-хвоста) — веб (:8787) и Telegram-бот поднимутся сами."
