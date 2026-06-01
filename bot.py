#!/usr/bin/env python3
"""
Claude-Ops-Bot — «Claude Code через Telegram».
Forum-группа Development: каждый топик привязан к проекту (thread_id -> cwd).
Полные права (bypassPermissions), подписка Игоря (без ANTHROPIC_API_KEY),
общий + проектный CLAUDE.md через setting_sources. Spec: ~/vault/01-Projects/Claude-Ops-Bot/.
"""
import asyncio
import html
import json
import os
import re
import subprocess
import time
import traceback
from pathlib import Path
from typing import AsyncGenerator

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    RateLimitEvent,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from aiohttp import web
import webapp          # браузерный кокпит (webapp.py) — поднимается в post_init рядом с ботом, состояние через ctx
from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, NetworkError, RetryAfter, TimedOut
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ─────────────────────────── config ───────────────────────────
HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
DATA.mkdir(exist_ok=True)


def _load_env():
    f = HERE / ".env"
    if f.exists():
        for line in f.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


_load_env()
# NB: ANTHROPIC_API_KEY намеренно НЕ задаётся — SDK работает на OAuth подписки (~/.claude).
os.environ.pop("ANTHROPIC_API_KEY", None)

BOT_TOKEN = os.environ["BOT_TOKEN"]
GROUP_CHAT_ID = int(os.environ.get("GROUP_CHAT_ID", "0"))
ALLOWED_USERS = {int(x) for x in os.environ.get("ALLOWED_USERS", "").split(",") if x.strip()}
DEFAULT_CWD = os.environ.get("DEFAULT_CWD", str(Path.home()))
DEFAULT_MODEL = os.environ.get("DEFAULT_MODEL", "opus")

WEB_PORT = int(os.environ.get("WEB_PORT", "8787"))           # браузерный кокпит
WEB_PASSWORD = os.environ.get("WEB_PASSWORD", "")            # парольная фраза для входа в кокпит

MODELS = {"opus": "opus", "sonnet": "sonnet", "haiku": "haiku"}  # CLI резолвит алиасы в latest

# ─────────────────────────── именованные константы ───────────────────────────
TG_CHUNK = 4000          # макс. размер одного TG-сообщения (символы)

# Operating-brief: инжектится в КАЖДУЮ сессию (все топики), поверх главного и проектного CLAUDE.md.
# ⚠️ nudge — ТОЛЬКО то, что реально отличает Telegram от терминала. Всё про «как работать»
# (scan, хирургичность, права, необратимое) живёт в CLAUDE.md (project + ~/CLAUDE.md) — агент
# грузит их через setting_sources и читает те же файлы, что и терминал. Не дублировать сюда:
# лишний контекст каждый ход = агент тупее. Держать коротким.
TELEGRAM_NUDGE = (
    "Канал — Telegram-бот, не интерактивный терминал. В остальном ты обычный Claude Code: "
    "следуй CLAUDE.md проекта и ~/CLAUDE.md (уже загружены) — там все правила работы.\n"
    "- Интерактивных диалогов/кнопок НЕТ: нужно уточнение или выбор — спроси ТЕКСТОМ в конце ответа "
    "и заверши ход; Игорь ответит следующим сообщением, сессия продолжится.\n"
    "- Ответ — кратко, по-русски, живой прозой: что сделал → что дальше. Не дублируй лог инструментов "
    "(он виден в статусе) и не вставляй длинные листинги кода — правки Игорь видит в файлах.\n"
    "- Файл/скриншот Игорю в этот топик: `tg-reply <путь> [подпись]`.\n"
    "- Важное решение/грабли/что отвергнуто → запиши в `.claude-ops/memory/` (см. CLAUDE.md проекта)."
)
# AskUserQuestion = интерактивная голосовалка (нет ответа в TG -> агент зависает/решает сам).
DISALLOWED_TOOLS = ["AskUserQuestion"]

TOPICS_F = DATA / "topics.json"      # СЛОЙ 1: привязка thread -> проект (вечная)
SESSIONS_F = DATA / "sessions.json"  # СЛОЙ 2: thread -> session_id (чистит /reset)

def _norm(s: str) -> str:
    return "".join(c for c in s.lower() if c.isalnum())


def _home_sub(*parts: str) -> str:
    """Возвращает строковый путь относительно $HOME (динамически, без хардкода /home/igor)."""
    return str(Path.home().joinpath(*parts))


# реестр проектов: алиас(норм.) -> cwd. Покрывает имена топиков и basename папок.
# Пути строятся через Path.home() — без хардкода /home/igor.
_REG_RAW = {
    "rightforms": _home_sub("rightforms-app"),
    "rightformsapp": _home_sub("rightforms-app"),
    "networkingos": _home_sub("networking-os"),
    "mailservice": _home_sub("mail-service"),
    "linevpnbot": _home_sub("line_vpn_bot"),
    "khronika": _home_sub("khronika-portal"),
    "khronikaportal": _home_sub("khronika-portal"),
    "linevpnportal": _home_sub("linevpn-portal"),
    "eveng2": _home_sub("even-g2"),
    "igdigitallab": _home_sub("ig-digital-lab"),
    "contenteditor": _home_sub("content-editor"),
    "teleprompter": _home_sub("teleprompter"),
    "proxmonbot": _home_sub("proxmon-bot"),
    "smsgate": _home_sub("sms-gate"),
    "claudeops": _home_sub("claude-ops-bot"),
    "claudeopsbot": _home_sub("claude-ops-bot"),
    "homeassistant": _home_sub("home-assistant"),
    "hass": _home_sub("home-assistant"),
    "ha": _home_sub("home-assistant"),
    "sandbox": _home_sub("sandbox"),
    "general": DEFAULT_CWD,
}


def build_registry() -> dict:
    reg = dict(_REG_RAW)
    base = Path.home()  # динамически, без хардкода /home/igor
    for d in sorted(base.iterdir()):
        if d.is_dir() and ((d / ".git").exists() or (d / "CLAUDE.md").exists()):
            reg.setdefault(_norm(d.name), str(d))
    return reg


REGISTRY = build_registry()


def resolve_project(name: str):
    """name -> (display, cwd) либо None. Принимает алиас, basename или абсолютный путь."""
    name = name.strip()
    if name.startswith("/") and Path(name).is_dir():
        return Path(name).name, name
    cwd = REGISTRY.get(_norm(name))
    if cwd and Path(cwd).is_dir():
        return Path(cwd).name, cwd
    return None


# ─────────────────────────── state ───────────────────────────
def _read(f, default):
    try:
        return json.loads(f.read_text())
    except Exception:
        return default


topics = _read(TOPICS_F, {})       # "chat:thread" -> {project, cwd, model}
sessions = _read(SESSIONS_F, {})   # "chat:thread" -> session_id
costs = {}                         # "chat:thread" -> last cost usd
running = {}                       # "chat:thread" -> ClaudeSDKClient (для /stop)
rate_limits = {}                   # rate_limit_type -> {status, resets_at, utilization, ts} (пассивно)


def save_topics():
    TOPICS_F.write_text(json.dumps(topics, ensure_ascii=False, indent=2))


def save_sessions():
    SESSIONS_F.write_text(json.dumps(sessions, ensure_ascii=False, indent=2))


def key_of(update: Update) -> str:
    chat = update.effective_chat.id
    thread = update.effective_message.message_thread_id or 0
    return f"{chat}:{thread}"


def binding_for(update: Update) -> dict:
    """Привязка топика. General/без топика -> дефолт. Авто-привязка по имени НЕ здесь
    (она в on_topic_created). Тут только чтение + дефолт для General."""
    k = key_of(update)
    if k in topics:
        return topics[k]
    # топик без привязки -> дефолт на /home/igor, но проект помечаем как unbound
    thread = update.effective_message.message_thread_id
    if not thread:
        return {"project": "General", "cwd": DEFAULT_CWD, "model": DEFAULT_MODEL}
    return None  # неизвестный топик -> попросим /project


# ─────────────────────────── auth ───────────────────────────
def authorized(update: Update) -> bool:
    u = update.effective_user
    return bool(u and u.id in ALLOWED_USERS)


# ─────────────────────────── helpers ───────────────────────────
async def _tg_call(factory, tries=6):
    """Один вызов TG API, переживающий ТРАНЗИЕНТНЫЕ сбои — главную причину «пропажи ответа»
    на длинных задачах: RetryAfter (flood-control), NetworkError/Bad Gateway, TimedOut.
    factory — функция без аргументов, отдающая СВЕЖУЮ корутину (нужно для повторов).
    BadRequest сюда НЕ ловим — это логическая ошибка (битый HTML), её разбирает вызывающий."""
    delay = 1.0
    for attempt in range(tries):
        try:
            return await factory()
        except RetryAfter as e:
            await asyncio.sleep(getattr(e, "retry_after", delay) + 0.5)
        except (NetworkError, TimedOut):
            if attempt == tries - 1:
                raise
            await asyncio.sleep(delay)
            delay = min(delay * 2, 15)


async def send(context, chat, thread, text, **kw):
    # Чанкуем по строкам (не вслепую по байтам) — чтобы не резать HTML-теги/entity на границе.
    for chunk in _smart_chunks(text, TG_CHUNK):
        try:
            await _tg_call(lambda c=chunk: context.bot.send_message(
                chat, c, message_thread_id=thread or None, **kw))
        except BadRequest:
            # парсер HTML/MD подавился — шлём как plain text, лишь бы дошло.
            # ВАЖНО: chunk сюда приходит уже html-escaped (<b>... &lt;... &amp;...).
            # Без unescape Игорь увидит сырые &lt;b&gt; — это и есть «не смог прочитать».
            kw2 = {k: v for k, v in kw.items() if k != "parse_mode"}
            plain = html.unescape(re.sub(r"</?(b|i|code|pre|a)[^>]*>", "", chunk))
            await _tg_call(lambda p=plain: context.bot.send_message(
                chat, p, message_thread_id=thread or None, **kw2))


CODE_MAX_LINES = 20      # длиннее — сворачиваем (в TG-ответе важна суть, не простыни кода)
CODE_PREVIEW_LINES = 10  # сколько строк показать перед сворачиванием


def _render_code_block(body: str, lang: str = "") -> str:
    """Блок ```...``` -> моноширинный <pre>. Длинный (> CODE_MAX_LINES) сворачивается в превью +
    маркер: правки Игорь и так видит в файлах/diff, простыни кода в TG — шум («без лишнего кода»)."""
    lines = body.split("\n")
    while lines and not lines[-1].strip():   # срезаем пустой хвост
        lines.pop()
    n = len(lines)
    if n > CODE_MAX_LINES:
        head = "\n".join(lines[:CODE_PREVIEW_LINES])
        tag = f"{lang} · " if lang else ""
        return f"<pre>{html.escape(head)}\n…</pre><i>‹{tag}{n} строк кода свёрнуто›</i>"
    return f"<pre>{html.escape(chr(10).join(lines))}</pre>"


def md_to_html(text: str) -> str:
    """Markdown ответа модели -> безопасный HTML для Telegram (поддерживает <b><i><code><pre><a>).
    Стратегия: код/ссылки вынимаем в плейсхолдеры ДО экранирования (чтобы не побить и посчитать
    строки), экранируем остальное, накладываем лёгкий markdown, возвращаем плейсхолдеры обратно.
    Всё на стороне бота — агент про формат не думает, остаётся таким же умным, как в терминале."""
    stash = []

    def _stash(fragment: str) -> str:
        stash.append(fragment)
        return f"\x00P{len(stash) - 1}\x00"   # \x00 не трогается html.escape и markdown-регексами

    # 1) блоки кода ```lang\n...``` (до экранирования — нужно посчитать строки)
    text = re.sub(r"```([^\n]*)\n?(.*?)```",
                  lambda m: _stash(_render_code_block(m.group(2), (m.group(1) or "").strip())),
                  text, flags=re.DOTALL)
    # 2) инлайн `код`
    text = re.sub(r"`([^`\n]+?)`",
                  lambda m: _stash(f"<code>{html.escape(m.group(1))}</code>"), text)
    # 3) ссылки [текст](url)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)",
                  lambda m: _stash(f'<a href="{html.escape(m.group(2), quote=True)}">{html.escape(m.group(1))}</a>'),
                  text)
    # 4) экранируем остальное (сырые <title>/<div> из ответа становятся безопасными)
    text = html.escape(text)
    # 5) построчно: заголовки #..###### -> жирная строка; маркеры списка -*+ -> •
    out = []
    for line in text.split("\n"):
        h = re.match(r"\s*#{1,6}\s+(.*)", line)
        if h:
            out.append(f"<b>{h.group(1).rstrip()}</b>")
        else:
            out.append(re.sub(r"^(\s*)[-*+]\s+", r"\1• ", line))
    text = "\n".join(out)
    # 6) **жирный** и *курсив* (курсив — консервативно, чтобы не ловить «2 * 3» и остатки списков)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<![\*\w])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\*\w])", r"<i>\1</i>", text)
    # 7) возвращаем код/ссылки
    return re.sub(r"\x00P(\d+)\x00", lambda m: stash[int(m.group(1))], text)


async def report_error(context, chat, thread, where: str, exc: BaseException):
    """Шлёт краш Игорю: место + тип + трейсбек в копируемом <pre>-блоке.
    Любые ошибки отправки глушим — если httpx-клиент PTB уже закрыт (shutdown сервиса),
    слать всё равно некуда, и второй необработанный exception только шумит в логах."""
    chat = chat or GROUP_CHAT_ID or (next(iter(ALLOWED_USERS), None))
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    head = f"💥 <b>Краш</b>\nГде: {html.escape(where)}\nЧто: <b>{type(exc).__name__}</b>: {html.escape(str(exc))}"
    block = tb[-3500:]  # хвост трейсбека — самое релевантное
    text = f"{head}\n<pre>{html.escape(block)}</pre>"
    try:
        await _tg_call(lambda: context.bot.send_message(
            chat, text, message_thread_id=thread or None, parse_mode=ParseMode.HTML))
        return
    except Exception:
        pass
    try:
        plain = f"💥 Краш\nГде: {where}\nЧто: {type(exc).__name__}: {exc}\n\n{block}"
        await _tg_call(lambda: context.bot.send_message(
            chat, plain[:TG_CHUNK], message_thread_id=thread or None))
    except Exception as send_exc:
        print(f"[report_error] не смог отправить отчёт в TG ({type(send_exc).__name__}): {exc!r}")


def _chunks(s, n):
    if not s:
        return [""]
    return [s[i:i + n] for i in range(0, len(s), n)] or [""]


def _smart_chunks(s: str, n: int):
    """Режем по строкам (потом по пробелам), чтобы не рвать HTML-теги/entity на границе чанка.
    Если строка сама длиннее n — fallback на грубое разбиение."""
    if not s:
        return [""]
    out, buf = [], ""
    for line in s.splitlines(keepends=True):
        if len(line) > n:
            if buf:
                out.append(buf)
                buf = ""
            out.extend(_chunks(line, n))
            continue
        if len(buf) + len(line) > n:
            out.append(buf)
            buf = line
        else:
            buf += line
    if buf:
        out.append(buf)
    return out or [""]


def short(cmd: str, limit=90) -> str:
    cmd = " ".join(cmd.split())
    return cmd if len(cmd) <= limit else cmd[:limit] + "…"


# ─────────────────────────── audit + watchdog ───────────────────────────
AUDIT_DIR = DATA / "audit"
STALL_SECONDS = int(os.environ.get("STALL_SECONDS", "300"))   # нет событий N сек -> прервать
MAX_SECONDS = int(os.environ.get("MAX_SECONDS", "1800"))      # общий потолок задачи (30 мин)
_DESTRUCTIVE = ("git push", "push origin", "reset --hard", "rebase", "git clean", "--force",
                "rm -rf", "rm -r ", "rm -f", "drop table", "drop database", "delete from",
                "truncate", "coolify", "docker rm", "docker stop", "compose down",
                "systemctl restart", "systemctl stop")


def _is_destructive(cmd: str) -> bool:
    low = cmd.lower()
    return any(p in low for p in _DESTRUCTIVE)


def audit(project: str, kind: str, text: str):
    """Аппендит в data/audit/audit-YYYY-MM.log — постоянный след действий full-auto бота на проде."""
    try:
        AUDIT_DIR.mkdir(exist_ok=True)
        ts = time.strftime("%Y-%m-%dT%H:%M:%S")
        with open(AUDIT_DIR / f"audit-{time.strftime('%Y-%m')}.log", "a", encoding="utf-8") as f:
            f.write(f"{ts} [{project}] {kind}: {text}\n")
    except Exception:
        pass


# ─────────────────────────── ДВИЖОК (async-генератор событий) ───────────────────────────
#
# run_engine — независимый генератор событий. Не знает про Telegram, aiohttp или любой транспорт.
# Транспорты (TG-адаптер run_agent) потребляют его события.
#
# Схема событий:
#   {"type": "tool",       "name": str, "input": dict}        — инструмент запущен агентом
#   {"type": "text",       "text": str}                        — текстовый блок ответа модели
#   {"type": "result",     "session_id": str|None,
#                          "cost_usd": float|None}             — финальный ResultMessage
#   {"type": "rate_limit", "rate_limit_type": str, ...}        — RateLimitEvent (пассивное)
#   {"type": "error",      "exc": BaseException}               — исключение из SDK
#
# ВАЖНО — running[session_key]:
#   Адаптер (on_message) ставит running[k] = True СИНХРОННО до первого await (гонка!).
#   run_engine заменяет его реальным ClaudeSDKClient сразу после создания.
#   Снятие running.pop(k) — ответственность адаптера (в finally).

async def run_engine(  # type: ignore[return]
    project_name: str,
    cwd: str,
    prompt: str,
    session_key: str,
    model: str = None,
    system_prompt: dict = None,
    env: dict = None,
    resume_session_id: str = None,
) -> "AsyncGenerator[dict, None]":
    """Async-генератор событий SDK. Единственный источник истины для выполнения промпта.

    Аргументы:
        project_name      — имя проекта (для audit-лога)
        cwd               — рабочая директория
        prompt            — промпт пользователя
        session_key       — ключ в running/sessions (напр. "chat:thread")
        model             — модель (алиас из MODELS или строка напрямую)
        system_prompt     — dict {type,preset,append}, по умолчанию — TG-preset
        env               — доп. env-переменные для агента (TG_CHAT_ID и т.п.)
        resume_session_id — session_id для resume (None = новая сессия)

    Yields dict событий. Исключения SDK оборачиваются в {"type": "error", "exc": ...}.
    """
    if system_prompt is None:
        system_prompt = {"type": "preset", "preset": "claude_code", "append": TELEGRAM_NUDGE}

    resolved_model = MODELS.get(model, model) if model else MODELS.get(DEFAULT_MODEL, DEFAULT_MODEL)

    opts = ClaudeAgentOptions(
        model=resolved_model,
        permission_mode="bypassPermissions",
        cwd=cwd,
        setting_sources=["user", "project"],
        resume=resume_session_id,
        disallowed_tools=DISALLOWED_TOOLS,
        system_prompt=system_prompt,
        env=env or {},
    )

    audit(project_name, "TASK", short(prompt, 300))

    last_ctx_tokens = 0   # реальный размер контекста = prompt-токены последнего AssistantMessage
    try:
        async with ClaudeSDKClient(options=opts) as client:
            running[session_key] = client   # заменяем True-placeholder реальным клиентом (для /stop)
            await client.query(prompt)
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    # usage последнего ассистент-сообщения = весь промпт текущего хода:
                    # input + cache_read + cache_creation == get_context_usage().totalTokens (проверено)
                    u = getattr(msg, "usage", None) or {}
                    pt = (u.get("input_tokens", 0)
                          + u.get("cache_read_input_tokens", 0)
                          + u.get("cache_creation_input_tokens", 0))
                    if pt:
                        last_ctx_tokens = pt
                    for blk in msg.content:
                        if isinstance(blk, TextBlock) and blk.text.strip():
                            yield {"type": "text", "text": blk.text}
                        elif isinstance(blk, ToolUseBlock):
                            yield {"type": "tool", "name": blk.name, "input": blk.input or {}}
                elif isinstance(msg, RateLimitEvent):
                    i = msg.rate_limit_info
                    yield {
                        "type": "rate_limit",
                        "rate_limit_type": i.rate_limit_type,
                        "status": i.status,
                        "resets_at": i.resets_at,
                        "utilization": i.utilization,
                    }
                elif isinstance(msg, ResultMessage):
                    yield {
                        "type": "result",
                        "session_id": getattr(msg, "session_id", None),
                        "cost_usd": getattr(msg, "total_cost_usd", None),
                        "context_tokens": last_ctx_tokens,
                    }
                elif isinstance(msg, SystemMessage):
                    pass   # системные сообщения SDK — не транслируем
    except Exception as exc:
        yield {"type": "error", "exc": exc}


# ─────────────────────────── TG-адаптер ───────────────────────────
#
# run_agent — потребитель run_engine для Telegram-канала.
# Рендерит статус-сообщение (edit), watchdog, heartbeat, audit-лог, финальный ответ.
# Поведение 1-в-1 с оригиналом — только источник событий заменён на генератор.

async def run_agent(context, update, prompt: str):
    chat = update.effective_chat.id
    thread = update.effective_message.message_thread_id or 0
    k = key_of(update)
    b = topics.get(k) or binding_for(update)
    cwd, model = b["cwd"], b.get("model", DEFAULT_MODEL)
    # слот уже зарезервирован в on_message (running[k]=True) — здесь только работаем

    status = await context.bot.send_message(
        chat, f"⚙️ <b>{b['project']}</b> · {model}\n<i>думаю…</i>",
        message_thread_id=thread or None, parse_mode=ParseMode.HTML,
    )
    log_lines, answer, n_edits = [], [], 0
    last_edit = 0.0
    t_start = time.time()
    last_event = [t_start]        # обновляется на каждом событии SDK (для watchdog)
    stalled = {"reason": None}

    def _elapsed():
        s = int(time.time() - t_start)
        return f"{s // 60}м {s % 60:02d}с" if s >= 60 else f"{s}с"

    async def push_status(force=False):
        nonlocal last_edit
        now = asyncio.get_event_loop().time()
        if not force and now - last_edit < 2.0:
            return
        last_edit = now
        tail = "\n".join(log_lines[-8:]) or "думаю…"
        # таймер в шапке всегда меняется → нет ошибки "message is not modified" и видно, что жив
        body = f"⚙️ <b>{b['project']}</b> · {model} · ⏱ {_elapsed()}\n{html.escape(tail)}"
        try:
            await context.bot.edit_message_text(body, chat, status.message_id, parse_mode=ParseMode.HTML)
        except Exception:
            pass

    async def heartbeat():
        """Тикает статус каждые ~12с даже без новых tool-вызовов — сигнал «работаю, не завис»."""
        try:
            while True:
                await asyncio.sleep(12)
                await push_status(force=True)
        except asyncio.CancelledError:
            pass

    async def watchdog():
        """Прерывает зависшую задачу: нет событий STALL_SECONDS ИЛИ превышен MAX_SECONDS."""
        try:
            while True:
                await asyncio.sleep(20)
                now = time.time()
                if now - last_event[0] > STALL_SECONDS:
                    stalled["reason"] = f"нет событий {int((now - last_event[0]) // 60)} мин"
                elif now - t_start > MAX_SECONDS:
                    stalled["reason"] = f"превышен лимит {MAX_SECONDS // 60} мин"
                cl = running.get(k)
                if stalled["reason"] and hasattr(cl, "interrupt"):
                    try:
                        await cl.interrupt()
                    finally:
                        return
        except asyncio.CancelledError:
            pass

    hb = asyncio.create_task(heartbeat())
    wd = asyncio.create_task(watchdog())
    engine_exc = None
    webapp._bus_publish(k, {"kind": "run_start", "source": "tg", "prompt": prompt, "run_id": None})
    try:
        async for event in run_engine(
            project_name=b["project"],
            cwd=cwd,
            prompt=prompt,
            session_key=k,
            model=model,
            system_prompt={"type": "preset", "preset": "claude_code", "append": TELEGRAM_NUDGE},
            env={"TG_CHAT_ID": str(chat), "TG_THREAD_ID": str(thread or 0)},
            resume_session_id=sessions.get(k),
        ):
            last_event[0] = time.time()   # любое событие SDK = «живо» для watchdog
            etype = event["type"]

            if etype == "text":
                answer.append(event["text"])
                log_lines.append("💬 " + short(event["text"].replace("\n", " "), 70))
                webapp._bus_publish(k, {"kind": "text", "text": event["text"], "run_id": None})

            elif etype == "tool":
                name = event["name"]
                inp = event["input"]
                if name == "Bash":
                    cmd = inp.get("command", "")
                    log_lines.append(f"$ {short(cmd, 70)}")
                    audit(b["project"], "BASH⚠️" if _is_destructive(cmd) else "BASH", cmd)
                elif name in ("Edit", "Write", "NotebookEdit"):
                    n_edits += 1
                    fp = str(inp.get("file_path", ""))
                    log_lines.append(f"✏️ {name}: {short(fp, 60)}")
                    audit(b["project"], name.upper(), fp)
                else:
                    log_lines.append(f"🔧 {name}")
                webapp._bus_publish(k, {
                    "kind": "tool", "run_id": None,
                    "tool": webapp._format_tool(name, inp if isinstance(inp, dict) else {}),
                })
                await push_status()

            elif etype == "result":
                if event.get("session_id"):
                    sessions[k] = event["session_id"]
                    save_sessions()
                if event.get("cost_usd") is not None:
                    costs[k] = event["cost_usd"]

            elif etype == "rate_limit":
                rl_type = event.get("rate_limit_type")
                if rl_type:
                    rate_limits[rl_type] = {
                        "status": event.get("status"),
                        "resets_at": event.get("resets_at"),
                        "utilization": event.get("utilization"),
                        "ts": time.time(),
                    }

            elif etype == "error":
                engine_exc = event["exc"]

    except Exception as exc:
        engine_exc = exc
    finally:
        hb.cancel()
        wd.cancel()
        webapp._bus_publish(k, {
            "kind": "run_end",
            "outcome": "ok" if engine_exc is None else "fail",
            "run_id": None,
        })
        running.pop(k, None)

    # Если движок упал — удаляем статус и пробрасываем (safe_run/report_error обработает)
    if engine_exc is not None:
        try:
            await context.bot.delete_message(chat, status.message_id)
        except Exception:
            pass
        raise engine_exc

    # финал: СНАЧАЛА шлём ответ, и только ПОТОМ убираем статус-сообщение.
    # Порядок критичен: если отправка ответа упадёт даже после ретраев — на экране
    # останется последний прогресс (а не пустота). Удаление статуса до отправки и было
    # причиной «пропали и ход работы, и ответ» на длинных задачах.
    footer = []
    if n_edits:
        footer.append(f"✏️ правок файлов: {n_edits}")
    if stalled["reason"]:
        footer.append(f"⚠️ авто-прервано watchdog: {stalled['reason']}")
    ans = md_to_html("\n".join(answer).strip() or "(агент завершил без текстового ответа)")
    if footer:
        ans += "\n\n— — —\n" + "\n".join(footer)
    await send(context, chat, thread, ans, parse_mode=ParseMode.HTML)
    try:
        await context.bot.delete_message(chat, status.message_id)
    except Exception:
        pass
    audit(b["project"], "DONE", f"edits={n_edits}" + (f" STALLED:{stalled['reason']}" if stalled["reason"] else ""))



# ─────────────────────────── handlers ───────────────────────────
async def fetch_files(context, msg) -> list:
    """Скачивает вложения (документ/фото) в data/inbox/ и возвращает абсолютные пути.
    Лимит Telegram getFile — 20MB. Агент потом читает их по пути через Read."""
    inbox = DATA / "inbox"
    inbox.mkdir(exist_ok=True)
    paths = []
    if msg.document:
        d = msg.document
        f = await context.bot.get_file(d.file_id)
        name = (d.file_name or f"doc_{msg.message_id}").replace("/", "_")
        dest = inbox / f"{msg.message_id}_{name}"
        await f.download_to_drive(str(dest))
        paths.append(str(dest))
    if msg.photo:
        f = await context.bot.get_file(msg.photo[-1].file_id)
        dest = inbox / f"{msg.message_id}.jpg"
        await f.download_to_drive(str(dest))
        paths.append(str(dest))
    return paths


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    msg = update.effective_message
    text = (msg.text or msg.caption or "").strip()
    has_file = bool(msg.document or msg.photo)
    if not text and not has_file:
        return
    k = key_of(update)
    if k not in topics and msg.message_thread_id:
        await send(context, update.effective_chat.id, msg.message_thread_id,
                   "🔌 Топик не привязан к проекту. Привяжи: /project <имя>")
        return
    # защита от гонки: резервируем слот СИНХРОННО, до первого await
    if k in running:
        await send(context, update.effective_chat.id, msg.message_thread_id,
                   "⏳ Уже работаю в этом топике. /stop чтобы прервать.")
        return
    running[k] = True  # placeholder; run_engine заменит на реальный client
    cid, tid = update.effective_chat.id, msg.message_thread_id
    # вложения -> скачиваем, путь отдаём агенту
    files = []
    if has_file:
        try:
            files = await fetch_files(context, msg)
        except Exception as e:
            await send(context, cid, tid, f"⚠️ Не смог скачать вложение ({e}). Возможно >20MB.")
    base = text or "Посмотри прикреплённый файл и скажи/сделай по нему, что нужно."
    if msg.forward_origin:
        prompt = ("[Это пересланное сообщение/алерт от одного из моих сервисов. "
                  "Диагностируй причину и почини.]\n\n" + base)
    else:
        prompt = base
    if files:
        prompt += ("\n\n[Прикреплённые файлы — абсолютные пути на сервере, прочитай их через Read:\n"
                   + "\n".join(files) + "]")
    await context.bot.send_chat_action(cid, ChatAction.TYPING, message_thread_id=tid or None)
    asyncio.create_task(safe_run(context, update, prompt))


async def safe_run(context, update, prompt):
    """Обёртка фоновой задачи: PTB не ловит исключения из asyncio.create_task сам."""
    chat = update.effective_chat.id
    thread = update.effective_message.message_thread_id
    k = key_of(update)
    try:
        await run_agent(context, update, prompt)
    except Exception as e:
        # SIGTERM/SIGKILL CLI при systemctl restart/stop сервиса -> SDK отдаёт exit 143/137.
        # Это не баг бота, а штатный shutdown — не шумим «крашем» в TG.
        if "exit code 143" in str(e) or "exit code 137" in str(e):
            print(f"[safe_run] CLI killed during shutdown (exit 143/137), prompt={short(prompt, 60)}")
        else:
            await report_error(context, chat, thread, f"run_agent · {short(prompt, 60)}", e)
    finally:
        running.pop(k, None)  # гарантированно снимаем резерв, даже если упало до try в run_agent


async def on_error(update, context):
    """Глобальный обработчик ошибок PTB (хэндлеры команд и т.п.)."""
    chat = thread = None
    where = "handler"
    if isinstance(update, Update):
        if update.effective_chat:
            chat = update.effective_chat.id
        if update.effective_message:
            thread = update.effective_message.message_thread_id
            txt = update.effective_message.text or update.effective_message.caption
            if txt:
                where = f"update · {short(txt, 60)}"
    await report_error(context, chat, thread, where, context.error)


async def on_topic_created(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Новый топик -> авто-привязка по имени через реестр."""
    if not authorized(update):
        return
    msg = update.effective_message
    name = msg.forum_topic_created.name
    k = key_of(update)
    r = resolve_project(name)
    if r:
        topics[k] = {"project": r[0], "cwd": r[1], "model": DEFAULT_MODEL}
        save_topics()
        await send(context, update.effective_chat.id, msg.message_thread_id,
                   f"✅ Привязал топик к <b>{r[0]}</b>\n<code>{r[1]}</code>", parse_mode=ParseMode.HTML)
    else:
        await send(context, update.effective_chat.id, msg.message_thread_id,
                   f"🔌 Топик «{html.escape(name)}» не сматчился с проектом. Привяжи: /project &lt;имя|путь&gt;",
                   parse_mode=ParseMode.HTML)


# ── commands ──
async def cmd_start(update, context):
    if not authorized(update):
        return
    await send(context, update.effective_chat.id, update.effective_message.message_thread_id,
               "👋 Claude-Ops. Пиши задачу или пересылай алерт в топик проекта.\n"
               "Команды: /whoami /reset /resume /model /project /newtopic /diff /cost /stop")


async def cmd_whoami(update, context):
    if not authorized(update):
        return
    k = key_of(update)
    b = topics.get(k) or binding_for(update)
    if not b:
        await send(context, update.effective_chat.id, update.effective_message.message_thread_id,
                   "🔌 Топик не привязан. /project <имя>")
        return
    sid = sessions.get(k, "—")
    await send(context, update.effective_chat.id, update.effective_message.message_thread_id,
               f"📍 <b>{b['project']}</b>\ncwd: <code>{b['cwd']}</code>\nmodel: {b.get('model', DEFAULT_MODEL)}\n"
               f"session: <code>{sid}</code>", parse_mode=ParseMode.HTML)


async def cmd_reset(update, context):
    if not authorized(update):
        return
    k = key_of(update)
    sessions.pop(k, None)
    save_sessions()
    b = topics.get(k) or binding_for(update)
    proj = b["project"] if b else "—"
    await send(context, update.effective_chat.id, update.effective_message.message_thread_id,
               f"🔄 Контекст сброшен. Проект <b>{proj}</b> сохранён.", parse_mode=ParseMode.HTML)


async def cmd_resume(update, context):
    if not authorized(update):
        return
    k = key_of(update)
    if context.args:
        sessions[k] = context.args[0]
        save_sessions()
        await send(context, update.effective_chat.id, update.effective_message.message_thread_id,
                   f"⏯ Резюмлю сессию <code>{context.args[0]}</code>", parse_mode=ParseMode.HTML)
    else:
        sid = sessions.get(k, "—")
        await send(context, update.effective_chat.id, update.effective_message.message_thread_id,
                   f"Текущая сессия топика: <code>{sid}</code>", parse_mode=ParseMode.HTML)


async def cmd_model(update, context):
    if not authorized(update):
        return
    k = key_of(update)
    b = topics.get(k) or binding_for(update)
    if not b:
        await send(context, update.effective_chat.id, update.effective_message.message_thread_id,
                   "🔌 Сначала привяжи топик: /project <имя>")
        return
    if not context.args or context.args[0] not in MODELS:
        await send(context, update.effective_chat.id, update.effective_message.message_thread_id,
                   "Формат: /model opus|sonnet|haiku")
        return
    b["model"] = context.args[0]
    if k in topics:
        save_topics()
    await send(context, update.effective_chat.id, update.effective_message.message_thread_id,
               f"🧠 Модель топика: <b>{context.args[0]}</b> (со следующего запроса)", parse_mode=ParseMode.HTML)


async def cmd_project(update, context):
    if not authorized(update):
        return
    if not context.args:
        await send(context, update.effective_chat.id, update.effective_message.message_thread_id,
                   "Формат: /project <имя|путь>. Известные: " + ", ".join(sorted(set(_REG_RAW))))
        return
    r = resolve_project(" ".join(context.args))
    if not r:
        await send(context, update.effective_chat.id, update.effective_message.message_thread_id,
                   "❌ Не нашёл такой проект/путь.")
        return
    k = key_of(update)
    prev = topics.get(k, {})
    topics[k] = {"project": r[0], "cwd": r[1], "model": prev.get("model", DEFAULT_MODEL)}
    save_topics()
    await send(context, update.effective_chat.id, update.effective_message.message_thread_id,
               f"📌 Топик привязан к <b>{r[0]}</b>\n<code>{r[1]}</code>", parse_mode=ParseMode.HTML)


async def cmd_newtopic(update, context):
    """Бот сам создаёт forum-топик и привязывает к проекту."""
    if not authorized(update):
        return
    if not context.args:
        await send(context, update.effective_chat.id, update.effective_message.message_thread_id,
                   "Формат: /newtopic <имя проекта>")
        return
    name = " ".join(context.args)
    res = await context.bot.create_forum_topic(chat_id=update.effective_chat.id, name=name)
    tid = res.message_thread_id
    k = f"{update.effective_chat.id}:{tid}"
    r = resolve_project(name)
    if r:
        topics[k] = {"project": r[0], "cwd": r[1], "model": DEFAULT_MODEL}
        save_topics()
        note = f" → привязан к <code>{r[1]}</code>"
    else:
        note = " (не сматчился с проектом — привяжи /project внутри топика)"
    await context.bot.send_message(update.effective_chat.id,
                                   f"🆕 Создал топик «{html.escape(name)}»{note}",
                                   message_thread_id=tid, parse_mode=ParseMode.HTML)


async def cmd_diff(update, context):
    if not authorized(update):
        return
    k = key_of(update)
    b = topics.get(k) or binding_for(update)
    if not b:
        return
    try:
        out = subprocess.run(["git", "-C", b["cwd"], "diff", "--stat"],
                             capture_output=True, text=True, timeout=15).stdout
    except Exception as e:
        out = f"ошибка: {e}"
    await send(context, update.effective_chat.id, update.effective_message.message_thread_id,
               f"<b>git diff --stat</b> ({b['project']})\n<pre>{html.escape(out or '(пусто)')}</pre>",
               parse_mode=ParseMode.HTML)


async def cmd_cost(update, context):
    if not authorized(update):
        return
    k = key_of(update)
    c = costs.get(k)
    await send(context, update.effective_chat.id, update.effective_message.message_thread_id,
               f"💰 Последний запрос: ${c:.4f}" if c is not None else "💰 Данных пока нет")


_RL_LABELS = {
    "five_hour": "5-часовая сессия",
    "seven_day": "Недельный лимит",
    "seven_day_opus": "Недельный · Opus",
    "seven_day_sonnet": "Недельный · Sonnet",
    "overage": "Overage",
}
_RL_ICON = {"allowed": "🟢", "allowed_warning": "🟡", "rejected": "🔴"}


def _fmt_reset(ts):
    if not ts:
        return "—"
    delta = ts - time.time()
    if delta <= 0:
        return "скоро"
    h, m = int(delta // 3600), int((delta % 3600) // 60)
    return f"через {h}ч {m}м" if h else f"через {m}м"


def format_usage() -> str:
    if not rate_limits:
        return ("📊 Данных о лимитах пока нет — придут с первым же запросом к боту "
                "(они приезжают вместе с ответами).")
    lines = ["📊 <b>Лимиты подписки</b> (пассивно, с последних ответов):"]
    for t in ["five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet", "overage"]:
        d = rate_limits.get(t)
        if not d:
            continue
        icon = _RL_ICON.get(d["status"], "⚪")
        util = d.get("utilization")
        pct = f" · использовано {util * 100:.0f}% (осталось {100 - util * 100:.0f}%)" if util is not None else ""
        lines.append(f"{icon} <b>{_RL_LABELS.get(t, t)}</b>: сброс {_fmt_reset(d['resets_at'])}{pct}")
    lines.append("\n<i>Точный % приходит только при приближении к лимиту; иначе — статус и время сброса.</i>")
    return "\n".join(lines)


async def cmd_usage(update, context):
    if not authorized(update):
        return
    await send(context, update.effective_chat.id, update.effective_message.message_thread_id,
               format_usage(), parse_mode=ParseMode.HTML)


async def cmd_stop(update, context):
    if not authorized(update):
        return
    k = key_of(update)
    client = running.get(k)
    cid, tid = update.effective_chat.id, update.effective_message.message_thread_id
    if client is None:
        await send(context, cid, tid, "Нечего прерывать.")
    elif hasattr(client, "interrupt"):
        try:
            await client.interrupt()
        except Exception:
            pass
        await send(context, cid, tid, "🛑 Прерываю…")
    else:
        await send(context, cid, tid, "⏳ Задача ещё запускается — секунду.")


# ─────────────────────────── main ───────────────────────────
async def _on_start(app):
    """post_init: поднимаем внутрипроцессные HTTP-каналы рядом с ботом.
    Каждый обёрнут в собственный try/except — падение веба НЕ роняет Telegram."""
    await webapp.start(app, {              # браузерный кокпит
        "port": WEB_PORT, "password": WEB_PASSWORD,
        "topics": topics, "sessions": sessions, "running": running,
        "costs": costs, "rate_limits": rate_limits,
        "resolve_project": resolve_project, "REGISTRY": REGISTRY,
        "save_sessions": save_sessions, "save_topics": save_topics,
        "DATA": DATA, "DEFAULT_CWD": DEFAULT_CWD, "DEFAULT_MODEL": DEFAULT_MODEL,
        "VAULT_PROJECTS": Path.home() / "vault" / "01-Projects", "HERE": HERE,
        # F1: движок и модели для авто-запуска карточек канбана
        "run_engine": run_engine, "MODELS": MODELS,
        # F1: ссылка на PTB-приложение для пинга в TG
        "ptb_app": app,
        # «+ Новый проект» — нужен для синтеза session_key "<chat>:<thread>" в topics.json
        "GROUP_CHAT_ID": GROUP_CHAT_ID,
    })


def main():
    app: Application = ApplicationBuilder().token(BOT_TOKEN).post_init(_on_start).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("clear", cmd_reset))  # алиас под привычку из CLI
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CommandHandler("project", cmd_project))
    app.add_handler(CommandHandler("newtopic", cmd_newtopic))
    app.add_handler(CommandHandler("diff", cmd_diff))
    app.add_handler(CommandHandler("cost", cmd_cost))
    app.add_handler(CommandHandler("usage", cmd_usage))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(MessageHandler(filters.StatusUpdate.FORUM_TOPIC_CREATED, on_topic_created))
    app.add_handler(MessageHandler(
        (filters.TEXT | filters.CAPTION | filters.Document.ALL | filters.PHOTO) & ~filters.COMMAND,
        on_message))
    app.add_error_handler(on_error)
    print("Claude-Ops-Bot запущен.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
