"""
Тесты для bot._is_destructive — детектор «необратимых» команд.

Сигналит в TG-футере ⚠️ и audit-логе.
Из CLAUDE.md: «НЕ использовать -f / rm / kill (ловят tail -f, perform, и т.п.).
Только rm -rf/rm -f/git push/--force и пр.»
"""
import pytest

from bot import _is_destructive


# ─────────────────────────── позитивные кейсы (должно вернуть True) ───────────────────────────

@pytest.mark.parametrize("cmd", [
    "git push",
    "git push origin master",
    "git push --force",
    "git reset --hard HEAD~5",
    "git rebase main",
    "git clean -fdx",
    "rm -rf /tmp/foo",
    "rm -rf node_modules",
    "rm -r /var/cache",
    "rm -f some_file",
    "DROP TABLE users",
    "drop table users",
    "drop database mydb",
    "delete from users where id=1",
    "TRUNCATE products",
    "docker rm -f mycontainer",
    "docker stop mycontainer",
    "docker compose down",
    "docker-compose down -v",
    "systemctl restart nginx",
    "systemctl stop my-service",
    "curl coolify.coscore.us/api/v1/deploy",
    "cmd --force",
])
def test_destructive_commands(cmd):
    assert _is_destructive(cmd), f"Должно сработать как destructive: {cmd!r}"


# ─────────────────────────── негативные кейсы (НЕ должно срабатывать) ───────────────────────────

@pytest.mark.parametrize("cmd", [
    "ls -la",
    "pwd",
    "git status",
    "git log",
    "git diff",
    "git pull",
    "git commit -m 'fix'",
    "git checkout main",
    "tail -f /var/log/syslog",       # из CLAUDE.md: -f не должен ловиться
    "perform action",                  # из CLAUDE.md: «perform» содержит 'rm '? нет, perform
    "cat file.txt",
    "echo hello",
    "python script.py",
    "npm install",
    "npm run build",
    "make test",
    "journalctl -u my-service",
    "systemctl status nginx",          # status — не разрушительно
    "systemctl is-active nginx",
    "ps aux | grep python",
    "kill 12345",                      # kill без пробела-окружения не в списке
    "sudo apt update",
])
def test_safe_commands(cmd):
    assert not _is_destructive(cmd), f"Не должно сработать как destructive: {cmd!r}"


# ─────────────────────────── case-insensitive ───────────────────────────

def test_case_insensitive_uppercase():
    assert _is_destructive("RM -RF /tmp")


def test_case_insensitive_mixed():
    assert _is_destructive("Git Push origin")


def test_case_insensitive_drop():
    assert _is_destructive("Drop Table foo")


# ─────────────────────────── edge cases ───────────────────────────

def test_empty_string_not_destructive():
    assert not _is_destructive("")


def test_destructive_substring_in_arg():
    """Подстрока в кавычках-аргументе тоже срабатывает (намеренный over-match — безопаснее)."""
    # 'rm -rf' встретится в строке-аргументе — будет помечено
    assert _is_destructive("echo 'rm -rf /tmp'")


def test_tail_dash_f_not_caught_regression():
    """REGRESSION-тест: 'tail -f' содержит '-f ' но НЕ должен ловиться (бывший footgun из CLAUDE.md)."""
    # Проверяем что детектор не использует слишком общий паттерн '-f '
    assert not _is_destructive("tail -f /var/log/syslog")
    assert not _is_destructive("watch -f")
