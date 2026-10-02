"""Локальный Джарвис: Ollama, веб-поиск, погода, Vosk и голос Windows."""

from __future__ import annotations

import ast
import datetime as dt
import difflib
import ipaddress
import json
import operator
import os
import queue
import re
import sqlite3
import subprocess
import sys
import webbrowser
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable
from urllib.parse import urlencode, urlparse

import pyttsx3
import requests
import sounddevice as sd
from vosk import KaldiRecognizer, Model

from integrations import google_calendar_events, home_assistant_control, home_assistant_states
from settings import load_settings


BASE_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434/api/chat")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", load_settings().get("model", "qwen2.5:7b"))
VOSK_MODEL_DIR = BASE_DIR / "vosk-model-small-ru-0.22"
MEMORY_DB = BASE_DIR / "jarvis_memory.sqlite3"
KNOWLEDGE_DB = BASE_DIR / "jarvis_knowledge.sqlite3"
REMINDERS_DB = BASE_DIR / "jarvis_reminders.sqlite3"
SAMPLE_RATE = 16_000

WAKE_WORDS = {"джарвис", "джарвес", "жарвис", "jarvis"}
OPEN_VERBS = {"открой", "открыть", "запусти", "запустить", "включи", "включить"}
EXIT_PHRASES = {"выход", "выключайся", "остановись", "заверши работу", "закройся"}
CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")

# Запуск программ ограничен этим белым списком. Модель не получает shell-доступ.
SAFE_APPS = [
    ("Блокнот", {"блокнот", "нотпад", "notepad"}, "notepad.exe"),
    ("Калькулятор", {"калькулятор", "кальк", "calc"}, "calc.exe"),
    ("Проводник", {"проводник", "файлы", "explorer"}, "explorer.exe"),
    ("Paint", {"paint", "пейнт", "рисование"}, "mspaint.exe"),
]

SYSTEM_MESSAGE = {
    "role": "system",
    "content": (
        "Ты — Джарвис, полезный русскоязычный помощник. КРИТИЧЕСКОЕ ПРАВИЛО: "
        "всегда отвечай только на русском языке, кириллицей; никогда не вставляй "
        "китайские иероглифы, китайские фразы или случайные фрагменты на других языках. "
        "Имена, команды и термины можно оставить на языке оригинала, если это необходимо. "
        "Отвечай ясно, дружелюбно и по делу. Не выдумывай актуальные факты: если в сообщении "
        "есть блок веб-источников, опирайся на него и укажи ссылки; если источников нет и "
        "вопрос требует данных в реальном времени, честно скажи об этом. Ты не запускаешь "
        "программы и не меняешь файлы самостоятельно — такие действия выполняет только "
        "локальный код по разрешённому списку."
    ),
}

WEATHER_WORDS = ("погод", "дожд", "осадк", "температур", "снег", "градус", "ветер")
CITY_ALIASES = [
    (("батум", "batumi"), "Батуми"),
    (("тбилиси", "tbilisi"), "Тбилиси"),
    (("кутаиси", "kutaisi"), "Кутаиси"),
    (("москв", "moscow"), "Москва"),
    (("лондон", "london"), "Лондон"),
    (("ереван", "yerevan"), "Ереван"),
    (("стамбул", "istanbul"), "Стамбул"),
    (("париж", "paris"), "Париж"),
]

WMO_CODES = {
    0: "ясно",
    1: "преимущественно ясно",
    2: "переменная облачность",
    3: "облачно",
    45: "туман",
    48: "изморозевый туман",
    51: "слабая морось",
    53: "морось",
    55: "сильная морось",
    56: "ледяная морось",
    57: "сильная ледяная морось",
    61: "слабый дождь",
    63: "дождь",
    65: "сильный дождь",
    66: "ледяной дождь",
    67: "сильный ледяной дождь",
    71: "слабый снег",
    73: "снег",
    75: "сильный снег",
    77: "снежная крупа",
    80: "слабые ливни",
    81: "ливни",
    82: "сильные ливни",
    85: "снегопад",
    86: "сильный снегопад",
    95: "гроза",
    96: "гроза с градом",
    99: "сильная гроза с градом",
}


def find_vosk_model_dir() -> Path | None:
    """Находит корень Vosk-модели как при обычной, так и при вложенной распаковке ZIP."""
    expected = BASE_DIR / "vosk-model-small-ru-0.22"
    candidates = [VOSK_MODEL_DIR, expected, expected / expected.name, VOSK_MODEL_DIR / VOSK_MODEL_DIR.name]
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate.resolve()).casefold()
        if key in seen:
            continue
        seen.add(key)
        if all((candidate / name).is_dir() for name in ("am", "conf", "graph")):
            return candidate
    return None


def normalize(text: str) -> str:
    return re.sub(r"[^\w\s]", " ", text.casefold(), flags=re.UNICODE).strip()


def extract_command_after_wake_word(text: str) -> str | None:
    words = normalize(text).split()
    if not WAKE_WORDS.intersection(words):
        return None
    return " ".join(word for word in words if word not in WAKE_WORDS).strip()


def _remember(history: list[dict[str, str]], user_text: str, answer: str) -> None:
    history.extend(
        [
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": answer},
        ]
    )
    if len(history) > 12:
        del history[:-12]


def _ensure_memory_db() -> None:
    with sqlite3.connect(MEMORY_DB) as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS memories ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, fact TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL)"
        )


def list_memories() -> list[tuple[int, str, str]]:
    _ensure_memory_db()
    with sqlite3.connect(MEMORY_DB) as db:
        rows = db.execute("SELECT id, fact, created_at FROM memories ORDER BY id DESC").fetchall()
    return [(int(row[0]), str(row[1]), str(row[2])) for row in rows]


def add_memory(fact: str) -> str:
    fact = fact.strip().strip(" ,.:;\t")
    if not fact:
        return "Напиши, что именно нужно запомнить."
    if len(fact) > 500:
        return "Слишком длинная заметка. Сохраняй один короткий факт до 500 символов."
    sensitive = ("пароль", "password", "cvv", "номер карты", "ключ api", "api key", "токен", "секретный ключ")
    if any(word in fact.casefold() for word in sensitive):
        return "Я не сохраняю пароли, номера карт, токены и секретные ключи."
    _ensure_memory_db()
    with sqlite3.connect(MEMORY_DB) as db:
        try:
            db.execute(
                "INSERT INTO memories(fact, created_at) VALUES (?, ?)",
                (fact, dt.datetime.now().isoformat(timespec="seconds")),
            )
        except sqlite3.IntegrityError:
            return "Я уже это запомнил."
    return f"Запомнил: {fact}"


def delete_memory(memory_id: int) -> bool:
    _ensure_memory_db()
    with sqlite3.connect(MEMORY_DB) as db:
        cursor = db.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
    return cursor.rowcount > 0


def forget_memories(query: str | None = None) -> int:
    _ensure_memory_db()
    with sqlite3.connect(MEMORY_DB) as db:
        if not query:
            cursor = db.execute("DELETE FROM memories")
        else:
            cursor = db.execute("DELETE FROM memories WHERE fact LIKE ?", (f"%{query.strip()}%",))
    return cursor.rowcount


def _memory_command(text: str) -> str | None:
    n = normalize(text)
    if any(term in n for term in ("что ты помнишь", "покажи память", "покажи что запомнил", "список памяти")):
        items = list_memories()
        if not items:
            return "Пока долговременная память пуста. Скажи: «Запомни, что я предпочитаю краткие ответы»."
        return "Вот факты, которые ты просил запомнить:\n" + "\n".join(f"• {fact}" for _id, fact, _date in items[:30])

    if any(term in n for term in ("забудь всё", "забудь все", "очисти память", "удали всю память")):
        count = forget_memories()
        return f"Удалил из локальной памяти записей: {count}."

    forget_match = re.match(r"\s*(?:джарвис\s+)?(?:забудь|удали из памяти)\s+(.+)", text, flags=re.IGNORECASE)
    if forget_match:
        fragment = forget_match.group(1).strip(" .,!?")
        count = forget_memories(fragment)
        return f"Удалил подходящих записей: {count}." if count else "Не нашёл такую запись в памяти."

    remember_match = re.match(r"\s*(?:джарвис\s+)?(?:запомни|сохрани в память)\s*(?:,?\s*что)?\s*[:,-]?\s*(.+)", text, flags=re.IGNORECASE)
    if remember_match:
        return add_memory(remember_match.group(1))
    return None


def _memory_context() -> str | None:
    items = list_memories()
    if not items:
        return None
    facts = "\n".join(f"- {fact}" for _id, fact, _date in items[:20])
    return (
        "Память пользователя: это факты, которые он явно попросил сохранить. "
        "Используй только если это уместно; не делай из них новых неподтверждённых выводов.\n" + facts
    )


def _ensure_reminders_db() -> None:
    with sqlite3.connect(REMINDERS_DB) as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS reminders ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, message TEXT NOT NULL, due_at TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0)"
        )


def add_reminder(delay_seconds: int, message: str) -> str:
    message = message.strip(" .,!?") or "Пора проверить напоминание."
    delay_seconds = max(10, min(int(delay_seconds), 30 * 24 * 3600))
    due = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=delay_seconds)
    _ensure_reminders_db()
    with sqlite3.connect(REMINDERS_DB) as db:
        db.execute("INSERT INTO reminders(message, due_at) VALUES (?, ?)", (message, due.isoformat(timespec="seconds")))
    local_due = due.astimezone()
    return f"Хорошо, напомню «{message}» в {local_due.strftime('%H:%M')} {local_due.strftime('%d.%m.%Y')}."


def pop_due_reminders() -> list[str]:
    _ensure_reminders_db()
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    with sqlite3.connect(REMINDERS_DB) as db:
        rows = db.execute("SELECT id, message FROM reminders WHERE done = 0 AND due_at <= ?", (now,)).fetchall()
        if rows:
            db.executemany("UPDATE reminders SET done = 1 WHERE id = ?", [(row[0],) for row in rows])
    return [str(row[1]) for row in rows]


def _reminder_command(text: str) -> str | None:
    match = re.search(
        r"напомни(?:\s+мне)?\s+через\s+(\d+)\s*(секунд\w*|минут\w*|час\w*|дн\w*)\s+(.+)",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        match = re.search(r"поставь\s+таймер\s+на\s+(\d+)\s*(минут\w*|час\w*)\s*(.*)", text, flags=re.IGNORECASE)
    if not match:
        return None
    amount = int(match.group(1))
    unit = match.group(2).casefold()
    message = match.group(3) or "таймер"
    multiplier = 1 if unit.startswith("секунд") else 60 if unit.startswith("минут") else 3600 if unit.startswith("час") else 86400
    return add_reminder(amount * multiplier, message)


def open_approved_apps(command: str) -> str | None:
    words = set(normalize(command).split())
    if not OPEN_VERBS.intersection(words):
        return None

    opened: list[str] = []
    for display_name, aliases, executable in SAFE_APPS:
        if words.intersection(aliases):
            try:
                subprocess.Popen([executable], shell=False)
                opened.append(display_name)
            except OSError:
                return f"Не получилось открыть приложение «{display_name}»."

    if opened:
        return "Открываю " + " и ".join(opened) + "."
    return None


def _call_ollama(
    messages: list[dict[str, str]],
    on_chunk: Callable[[str], None] | None = None,
) -> tuple[str, bool]:
    streaming = on_chunk is not None
    payload = {
        "model": OLLAMA_MODEL,
        "messages": messages,
        "stream": streaming,
        "keep_alive": "15m",  # не выгружай модель сразу после ответа
        "options": {
            "num_ctx": 4096,
            "num_predict": 220,
            "temperature": 0.35,
        },
    }
    response = None
    try:
        response = requests.post(
            OLLAMA_URL,
            json=payload,
            timeout=(5, 300),
            stream=streaming,
        )
        if response.status_code == 404:
            return (
                f"Модель «{OLLAMA_MODEL}» не найдена. Проверь `ollama list` "
                f"или установи её командой: ollama pull {OLLAMA_MODEL}",
                True,
            )
        response.raise_for_status()

        if not streaming:
            data = response.json()
            answer = data.get("message", {}).get("content", "").strip()
            if not answer:
                return "Модель вернула пустой ответ. Попробуй задать вопрос иначе.", True
            return answer, False

        chunks: list[str] = []
        visible_prefix = ""
        allow_stream = True
        for line in response.iter_lines(decode_unicode=True):
            if not line:
                continue
            if isinstance(line, bytes):
                line = line.decode("utf-8", errors="replace")
            item = json.loads(line)
            piece = item.get("message", {}).get("content", "")
            if piece:
                chunks.append(piece)
                candidate = visible_prefix + piece
                if allow_stream and CJK_RE.search(candidate):
                    allow_stream = False
                elif allow_stream:
                    visible_prefix = candidate
                    on_chunk(piece)
        answer = "".join(chunks).strip()
        if not answer:
            return "Модель вернула пустой ответ. Попробуй задать вопрос иначе.", True
        return answer, False

    except requests.exceptions.ConnectionError:
        return "Не удаётся подключиться к Ollama. Проверь, что приложение Ollama запущено.", True
    except requests.exceptions.Timeout:
        return "Модель отвечает слишком долго. Попробуй более лёгкую модель или повтори вопрос.", True
    except requests.exceptions.RequestException as exc:
        return f"Ошибка связи с Ollama: {exc}", True
    except (ValueError, TypeError, AttributeError):
        return "Ollama вернула ответ в неожиданном формате.", True
    finally:
        if response is not None:
            response.close()


def ask_ollama(
    history: list[dict[str, str]],
    user_text: str,
    web_context: str | None = None,
    knowledge_context: str | None = None,
    on_chunk: Callable[[str], None] | None = None,
) -> str:
    context_parts = []
    if web_context:
        context_parts.append(
            "СВЕЖИЕ ВЕБ-ИСТОЧНИКИ. Опирайся на выдержки, не придумывай актуальные факты. "
            "В конце укажи названия и URL использованных страниц:\n" + web_context
        )
    if knowledge_context:
        context_parts.append(
            "ФРАГМЕНТЫ ЛОКАЛЬНЫХ ДОКУМЕНТОВ пользователя. Отвечай по ним, если они относятся к вопросу; "
            "в ответе указывай имя файла. Не приписывай документу того, чего в нём нет:\n" + knowledge_context
        )
    prompt = user_text
    if context_parts:
        prompt = f"Вопрос пользователя: {user_text}\n\n" + "\n\n".join(context_parts)

    messages = [SYSTEM_MESSAGE]
    saved_memory = _memory_context()
    if saved_memory:
        messages.append({"role": "system", "content": saved_memory})
    messages.extend([*history[-8:], {"role": "user", "content": prompt}])
    answer, failed = _call_ollama(messages, on_chunk=on_chunk)
    if failed:
        return answer

    # Если модель всё же добавила иероглифы — делаем короткий проход исправления.
    if CJK_RE.search(answer):
        repair_messages = [
            SYSTEM_MESSAGE,
            {
                "role": "user",
                "content": (
                    "Перепиши следующий ответ целиком естественным русским языком. "
                    "Не используй китайские иероглифы и не добавляй новых фактов.\n\n" + answer
                ),
            },
        ]
        repaired, repair_failed = _call_ollama(repair_messages)
        if not repair_failed and not CJK_RE.search(repaired):
            answer = repaired
        else:
            first_cjk = CJK_RE.search(answer)
            prefix = answer[: first_cjk.start()].rstrip() if first_cjk else ""
            answer = prefix if len(prefix) >= 12 else "Модель сформировала ответ не на русском. Повтори вопрос, пожалуйста."

    _remember(history, user_text, answer)
    return answer


def search_web(query: str, max_results: int = 5) -> tuple[list[dict[str, str]], str | None]:
    """Ищет свежие страницы в интернете через DDGS; ключ API не нужен."""
    try:
        from ddgs import DDGS
    except ImportError:
        return [], "Для веб-поиска установи пакет командой: py -m pip install ddgs"

    try:
        with DDGS(timeout=12) as searcher:
            raw_results = searcher.text(query, region="ru-ru", safesearch="moderate", max_results=max_results)
        results: list[dict[str, str]] = []
        for item in raw_results or []:
            title = str(item.get("title", "")).strip()
            href = str(item.get("href", item.get("url", ""))).strip()
            body = str(item.get("body", item.get("snippet", ""))).strip()
            if title or body:
                results.append({"title": title, "href": href, "body": body})
        return results, None
    except Exception as exc:
        return [], f"Веб-поиск сейчас недоступен: {exc}"


def _search_query(text: str) -> str:
    query = text.strip()
    prefixes = (
        "найди в интернете",
        "поищи в интернете",
        "поищи в сети",
        "найди",
        "поищи",
        "веб поиск",
        "поиск в интернете",
    )
    normalized = query.casefold()
    for prefix in prefixes:
        if normalized.startswith(prefix):
            return query[len(prefix) :].strip(" :,-") or query
    return query


def _needs_web_search(text: str) -> bool:
    n = normalize(text)
    explicit = ("найди в интернете", "поищи в интернете", "поищи в сети", "поиск в интернете", "веб поиск")
    current = ("сегодня", "сейчас", "последн", "актуальн", "на данный момент", "новост", "курс", "цена", "свеж")
    return any(term in n for term in explicit + current)


def _web_context(results: list[dict[str, str]]) -> str:
    parts = []
    for index, result in enumerate(results, start=1):
        parts.append(
            f"{index}. {result.get('title', 'Источник')}\n"
            f"URL: {result.get('href', '')}\n"
            f"Фрагмент: {result.get('body', '')}"
        )
    return "\n\n".join(parts)


class _HTMLText(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.skip_depth = 0
        self.skip_tags = {"script", "style", "noscript", "svg", "nav", "footer", "header"}

    def handle_starttag(self, tag, attrs) -> None:
        if tag.lower() in self.skip_tags:
            self.skip_depth += 1
        elif tag.lower() in {"p", "br", "div", "li", "h1", "h2", "h3"} and not self.skip_depth:
            self.parts.append("\n")

    def handle_endtag(self, tag) -> None:
        if tag.lower() in self.skip_tags and self.skip_depth:
            self.skip_depth -= 1
        elif tag.lower() in {"p", "div", "li", "h1", "h2", "h3"} and not self.skip_depth:
            self.parts.append("\n")

    def handle_data(self, data) -> None:
        if not self.skip_depth:
            value = data.strip()
            if value:
                self.parts.append(value + " ")


def _safe_public_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    host = parsed.hostname.casefold()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        return False
    try:
        address = ipaddress.ip_address(host)
        if address.is_private or address.is_loopback or address.is_link_local:
            return False
    except ValueError:
        pass
    return True


def _fetch_web_page(item: dict[str, str]) -> dict[str, str] | None:
    url = item.get("href", "")
    if not _safe_public_url(url):
        return None
    response = None
    try:
        response = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; JarvisAssistant/1.0)"},
            timeout=(4, 8),
            stream=True,
            allow_redirects=True,
        )
        if not _safe_public_url(response.url):
            return None
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "").casefold()
        if "html" not in content_type and "text/plain" not in content_type:
            return None
        data = bytearray()
        for block in response.iter_content(32_768):
            data.extend(block)
            if len(data) >= 700_000:
                break
        encoding = response.encoding or "utf-8"
        raw_text = bytes(data).decode(encoding, errors="replace")
        if "html" in content_type:
            parser = _HTMLText()
            parser.feed(raw_text)
            page_text = " ".join(parser.parts)
        else:
            page_text = raw_text
        page_text = re.sub(r"\s+", " ", page_text).strip()
        if not page_text:
            return None
        return {"title": item.get("title", "Страница"), "href": url, "body": page_text[:3500]}
    except requests.exceptions.RequestException:
        return None
    finally:
        if response is not None:
            response.close()


def deep_web_context(results: list[dict[str, str]], max_pages: int = 3) -> str:
    """Загружает несколько публичных страниц параллельно, ограничивая размер текста."""
    selected = results[:max_pages]
    with ThreadPoolExecutor(max_workers=min(3, len(selected) or 1)) as pool:
        pages = list(pool.map(_fetch_web_page, selected))
    fetched = [page for page in pages if page]
    return _web_context(fetched if fetched else results)


SUPPORTED_DOC_EXTENSIONS = {".txt", ".md", ".pdf", ".docx", ".html", ".htm"}


def _ensure_knowledge_db() -> None:
    with sqlite3.connect(KNOWLEDGE_DB) as db:
        db.execute("CREATE TABLE IF NOT EXISTS documents (path TEXT PRIMARY KEY, mtime REAL NOT NULL, title TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS chunks (id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT NOT NULL, text TEXT NOT NULL)")
        db.execute("CREATE INDEX IF NOT EXISTS chunks_path_idx ON chunks(path)")


def _extract_document_text(path: Path) -> str:
    ext = path.suffix.casefold()
    if ext in {".txt", ".md"}:
        return path.read_text(encoding="utf-8", errors="ignore")[:300_000]
    if ext in {".html", ".htm"}:
        parser = _HTMLText()
        parser.feed(path.read_text(encoding="utf-8", errors="ignore")[:1_000_000])
        return " ".join(parser.parts)[:300_000]
    if ext == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        return "\n".join((page.extract_text() or "") for page in reader.pages[:150])[:300_000]
    if ext == ".docx":
        from docx import Document
        document = Document(str(path))
        return "\n".join(paragraph.text for paragraph in document.paragraphs)[:300_000]
    raise ValueError(f"Формат {ext or 'без расширения'} не поддерживается")


def compare_documents(paths: list[str], max_chars: int = 12_000) -> str:
    """Сравнивает два поддерживаемых документа локально и возвращает ограниченный diff."""
    if len(paths) != 2:
        return "Для сравнения выбери ровно два документа."
    first, second = (Path(raw).expanduser() for raw in paths)
    try:
        for path in (first, second):
            if not path.is_file():
                raise FileNotFoundError(f"Файл не найден: {path.name}")
            if path.suffix.casefold() not in SUPPORTED_DOC_EXTENSIONS:
                raise ValueError(f"Формат {path.suffix or 'без расширения'} не поддерживается")
            if path.stat().st_size > 20_000_000:
                raise ValueError(f"Файл больше 20 МБ: {path.name}")
        left_raw = _extract_document_text(first)
        right_raw = _extract_document_text(second)
        left = [re.sub(r"[ \t]+", " ", line).strip() for line in left_raw.splitlines() if line.strip()][:5_000]
        right = [re.sub(r"[ \t]+", " ", line).strip() for line in right_raw.splitlines() if line.strip()][:5_000]
        matcher = difflib.SequenceMatcher(a=left, b=right)
        added = sum(j2 - j1 for tag, _i1, _i2, j1, j2 in matcher.get_opcodes() if tag in {"insert", "replace"})
        removed = sum(i2 - i1 for tag, i1, i2, _j1, _j2 in matcher.get_opcodes() if tag in {"delete", "replace"})
        diff = list(difflib.unified_diff(left, right, fromfile=first.name, tofile=second.name, lineterm="", n=2))
        if not diff:
            return f"Документы «{first.name}» и «{second.name}» текстуально совпадают."
        body = "\n".join(diff)
        if len(body) > max_chars:
            body = body[:max_chars].rstrip() + "\n… (diff сокращён)"
        return f"Сравнение «{first.name}» → «{second.name}»: добавлено строк {added}, удалено {removed}.\n```diff\n{body}\n```"
    except Exception as exc:
        return f"Не удалось сравнить документы: {exc}"


def index_documents(paths: list[str]) -> tuple[int, list[str]]:
    """Индексирует выбранные файлы только в локальной SQLite-базе."""
    _ensure_knowledge_db()
    indexed = 0
    errors: list[str] = []
    for raw_path in paths:
        path = Path(raw_path).expanduser()
        try:
            if not path.is_file():
                raise FileNotFoundError("файл не найден")
            if path.suffix.casefold() not in SUPPORTED_DOC_EXTENSIONS:
                raise ValueError("поддерживаются TXT, MD, PDF, DOCX и HTML")
            if path.stat().st_size > 20_000_000:
                raise ValueError("файл больше 20 МБ")
            text = re.sub(r"\s+", " ", _extract_document_text(path)).strip()
            if not text:
                raise ValueError("не удалось извлечь текст")

            chunks: list[str] = []
            start = 0
            while start < len(text):
                end = min(start + 1200, len(text))
                if end < len(text):
                    space = text.rfind(" ", start + 700, end)
                    if space > start:
                        end = space
                chunk = text[start:end].strip()
                if chunk:
                    chunks.append(chunk)
                start = end

            resolved = str(path.resolve())
            mtime = path.stat().st_mtime
            with sqlite3.connect(KNOWLEDGE_DB) as db:
                existing = db.execute("SELECT mtime FROM documents WHERE path = ?", (resolved,)).fetchone()
                if existing and float(existing[0]) == mtime:
                    continue
                db.execute("DELETE FROM chunks WHERE path = ?", (resolved,))
                db.execute("DELETE FROM documents WHERE path = ?", (resolved,))
                db.execute("INSERT INTO documents(path, mtime, title) VALUES (?, ?, ?)", (resolved, mtime, path.name))
                db.executemany("INSERT INTO chunks(path, text) VALUES (?, ?)", [(resolved, chunk) for chunk in chunks])
            indexed += 1
        except Exception as exc:
            errors.append(f"{path.name}: {exc}")
    return indexed, errors


def list_indexed_documents() -> list[tuple[str, str, float]]:
    _ensure_knowledge_db()
    with sqlite3.connect(KNOWLEDGE_DB) as db:
        rows = db.execute("SELECT path, title, mtime FROM documents ORDER BY title COLLATE NOCASE").fetchall()
    return [(str(path), str(title), float(mtime)) for path, title, mtime in rows]


def delete_indexed_document(path: str) -> bool:
    _ensure_knowledge_db()
    with sqlite3.connect(KNOWLEDGE_DB) as db:
        db.execute("DELETE FROM chunks WHERE path = ?", (path,))
        cursor = db.execute("DELETE FROM documents WHERE path = ?", (path,))
    return cursor.rowcount > 0


def clear_indexed_documents() -> int:
    _ensure_knowledge_db()
    with sqlite3.connect(KNOWLEDGE_DB) as db:
        count = int(db.execute("SELECT COUNT(*) FROM documents").fetchone()[0])
        db.execute("DELETE FROM chunks")
        db.execute("DELETE FROM documents")
    return count


def search_local_knowledge(query: str, max_results: int = 4) -> list[dict[str, str]]:
    _ensure_knowledge_db()
    stopwords = {
        "что", "как", "где", "когда", "зачем", "почему", "для", "это", "мне", "тебе", "можно", "будет",
        "есть", "или", "про", "этот", "эта", "эти", "the", "and", "for", "with", "from", "what", "how",
    }
    query_tokens = {t for t in re.findall(r"[a-zа-яё0-9]{3,}", query.casefold()) if t not in stopwords}
    if not query_tokens:
        return []
    with sqlite3.connect(KNOWLEDGE_DB) as db:
        rows = db.execute(
            "SELECT chunks.path, documents.title, chunks.text FROM chunks "
            "JOIN documents ON chunks.path = documents.path"
        ).fetchall()

    ranked: list[tuple[float, dict[str, str]]] = []
    for path, title, text in rows:
        tokens = Counter(re.findall(r"[a-zа-яё0-9]{3,}", str(text).casefold()))
        overlap = sum(min(tokens[token], 4) for token in query_tokens if token in tokens)
        if overlap <= 0:
            continue
        score = overlap / max(1, len(query_tokens))
        ranked.append((score, {"path": str(path), "title": str(title), "text": str(text)}))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [item for score, item in ranked[:max_results] if score >= 0.15]


def _knowledge_context(results: list[dict[str, str]]) -> str:
    return "\n\n".join(
        f"Файл: {item['title']}\nФрагмент: {item['text']}" for item in results
    )


def _extract_city(text: str) -> str:
    n = normalize(text)
    for aliases, display_name in CITY_ALIASES:
        if any(alias in n for alias in aliases):
            return display_name

    match = re.search(r"\b(?:в|для|около)\s+([a-zа-яё-]{3,})", n)
    if match and match.group(1) not in {"какое", "какая", "какой", "сегодня", "завтра", "городе"}:
        return match.group(1)
    return "Тбилиси"


def _rain_windows(hourly: dict, day: str) -> tuple[list[str], int]:
    times = hourly.get("time", [])
    probabilities = hourly.get("precipitation_probability", [])
    amounts = hourly.get("precipitation", [])
    selected: list[tuple[int, str, int, float]] = []

    for index, stamp in enumerate(times):
        if not stamp.startswith(day):
            continue
        probability = int(probabilities[index] or 0) if index < len(probabilities) and probabilities[index] is not None else 0
        amount = float(amounts[index] or 0) if index < len(amounts) and amounts[index] is not None else 0.0
        if probability >= 35 or amount >= 0.2:
            selected.append((index, stamp[11:16], probability, amount))

    today_probabilities = [
        int(probabilities[index])
        for index, stamp in enumerate(times)
        if stamp.startswith(day)
        and index < len(probabilities)
        and probabilities[index] is not None
    ]
    max_probability = max(today_probabilities, default=0)

    groups: list[list[tuple[int, str, int, float]]] = []
    for point in selected:
        if groups and point[0] == groups[-1][-1][0] + 1:
            groups[-1].append(point)
        else:
            groups.append([point])

    windows: list[str] = []
    for group in groups:
        start = group[0][1]
        last_hour = int(group[-1][1][:2])
        end = f"{(last_hour + 1) % 24:02d}:00"
        peak = max(point[2] for point in group)
        if len(group) == 1:
            windows.append(f"около {start} (вероятность до {peak}%)")
        else:
            windows.append(f"{start}–{end} (вероятность до {peak}%)")
    return windows, max_probability


def get_weather_forecast(city: str) -> str:
    """Получает местный прогноз через Open-Meteo; API-ключ не требуется."""
    try:
        geocode = requests.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": city, "count": 5, "language": "ru", "format": "json"},
            timeout=12,
        )
        geocode.raise_for_status()
        candidates = geocode.json().get("results") or []
        if not candidates:
            return f"Не нашёл город «{city}» в погодном сервисе. Проверь название и попробуй ещё раз."

        place = candidates[0]
        for candidate in candidates:
            country = str(candidate.get("country", "")).casefold()
            if city.casefold() in {"тбилиси", "батуми", "кутаиси"} and ("georgia" in country or "груз" in country):
                place = candidate
                break

        forecast = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": place["latitude"],
                "longitude": place["longitude"],
                "current": "temperature_2m,precipitation,weather_code",
                "hourly": "precipitation_probability,precipitation",
                "daily": "precipitation_probability_max,temperature_2m_max,temperature_2m_min,weather_code",
                "forecast_days": 2,
                "timezone": "auto",
            },
            timeout=15,
        )
        forecast.raise_for_status()
        data = forecast.json()

        current = data.get("current", {})
        daily = data.get("daily", {})
        hourly = data.get("hourly", {})
        current_stamp = str(current.get("time", ""))
        today = current_stamp[:10] or dt.date.today().isoformat()
        daily_dates = daily.get("time", [])
        day_index = daily_dates.index(today) if today in daily_dates else 0

        max_temps = daily.get("temperature_2m_max", [])
        min_temps = daily.get("temperature_2m_min", [])
        daily_rain = daily.get("precipitation_probability_max", [])
        weather_codes = daily.get("weather_code", [])

        tmax = max_temps[day_index] if day_index < len(max_temps) else None
        tmin = min_temps[day_index] if day_index < len(min_temps) else None
        description = ""
        if day_index < len(weather_codes):
            description = WMO_CODES.get(int(weather_codes[day_index]), "")

        windows, max_probability = _rain_windows(hourly, today)
        if day_index < len(daily_rain) and daily_rain[day_index] is not None:
            max_probability = max(max_probability, int(daily_rain[day_index]))

        place_name = place.get("name", city)
        country_name = place.get("country", "")
        location = f"{place_name}, {country_name}" if country_name else str(place_name)
        current_temp = current.get("temperature_2m")
        current_info = f"Сейчас {current_temp}°C" if current_temp is not None else ""
        temp_range = ""
        if tmin is not None and tmax is not None:
            temp_range = f"Днём ожидается от {tmin}°C до {tmax}°C"
        if description:
            temp_range = f"{temp_range}; {description}" if temp_range else description

        if windows:
            rain_info = "Дождь возможен " + "; ".join(windows) + "."
        else:
            rain_info = f"Заметного дождя по почасовому прогнозу не ожидается (максимальная вероятность около {max_probability}%)."

        parts = [f"Прогноз на сегодня, {today}, для {location} (местное время)."]
        if current_info:
            parts.append(current_info + ".")
        if temp_range:
            parts.append(temp_range + ".")
        parts.append(rain_info)
        parts.append("Источник: Open-Meteo.")
        return " ".join(parts)

    except requests.exceptions.RequestException:
        return "Не удалось получить свежий прогноз погоды. Проверь подключение к интернету и попробуй ещё раз."
    except (KeyError, ValueError, TypeError, IndexError):
        return "Погодный сервис вернул неполные данные. Попробуй повторить запрос чуть позже."


class _SafeMath:
    OPS = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv,
        ast.Mod: operator.mod,
        ast.Pow: operator.pow,
    }
    UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}

    @classmethod
    def evaluate(cls, node):
        if isinstance(node, ast.Expression):
            return cls.evaluate(node.body)
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            return node.value
        if isinstance(node, ast.UnaryOp) and type(node.op) in cls.UNARY:
            return cls.UNARY[type(node.op)](cls.evaluate(node.operand))
        if isinstance(node, ast.BinOp) and type(node.op) in cls.OPS:
            left = cls.evaluate(node.left)
            right = cls.evaluate(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 10:
                raise ValueError("Слишком большая степень")
            value = cls.OPS[type(node.op)](left, right)
            if abs(value) > 1e100:
                raise ValueError("Слишком большой результат")
            return value
        raise ValueError("Недопустимое выражение")


def _calculator_answer(text: str) -> str | None:
    trigger = re.search(r"посчитай|вычисли|сколько\s+будет|сколько\s+это", text, flags=re.IGNORECASE)
    if trigger:
        expression = text[trigger.end() :].strip().rstrip(" ?!.,")
    else:
        expression = text.strip()
        if not re.fullmatch(r"[\d\s+\-*/().,%]+", expression):
            return None

    expression = expression.replace("×", "*").replace("÷", "/").replace(",", ".")
    if not expression or len(expression) > 100 or not re.fullmatch(r"[\d\s+\-*/().%]+", expression):
        return None
    try:
        tree = ast.parse(expression, mode="eval")
        result = _SafeMath.evaluate(tree)
        return f"Результат: {result:g}."
    except (SyntaxError, ValueError, ZeroDivisionError, OverflowError):
        return "Не смог безопасно вычислить выражение. Проверь запись примера."


def _clock_answer(text: str) -> str | None:
    n = normalize(text)
    if any(phrase in n for phrase in ("который час", "сколько времени", "текущее время", "сейчас время")):
        return "Сейчас " + dt.datetime.now().strftime("%H:%M") + "."
    if any(phrase in n for phrase in ("какая сегодня дата", "какое сегодня число", "сегодняшняя дата")):
        return "Сегодня " + dt.datetime.now().strftime("%d.%m.%Y") + "."
    return None


def _is_weather_request(text: str) -> bool:
    n = normalize(text)
    return any(word in n for word in WEATHER_WORDS)


def _needs_web_search(text: str) -> bool:
    n = normalize(text)
    explicit = ("найди в интернете", "поищи в интернете", "поищи в сети", "поиск в интернете", "веб поиск")
    current = ("сегодня", "сейчас", "последн", "актуальн", "на данный момент", "новост", "курс", "цена", "свеж")
    return any(term in n for term in explicit + current)


def _web_query(text: str) -> str:
    query = text.strip()
    for prefix in ("найди в интернете", "поищи в интернете", "поищи в сети", "поиск в интернете", "веб поиск", "найди", "поищи"):
        if query.casefold().startswith(prefix):
            return query[len(prefix) :].strip(" :,-") or query
    return query


def _google_query(text: str) -> str | None:
    """Распознаёт безопасные команды открыть Google или поискать в нём."""
    raw = text.strip()
    patterns = (
        r"^(?:открой|запусти)\s+(?:гугл|google)(?:\s+(?:и\s+)?(?:найди|поищи|покажи))?\s*(.*)$",
        r"^(?:открой|найди|поищи)\s+в\s+(?:гугле|google)(?:\s+(?:по|про|о|об))?\s*(.*)$",
        r"^(?:найди|поищи)\s+(.+?)\s+в\s+(?:гугле|google)$",
    )
    for pattern in patterns:
        match = re.match(pattern, raw, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip(" ,.!?\t") if match.lastindex else ""
    return None


def _open_google(query: str) -> str:
    if query:
        url = "https://www.google.com/search?" + urlencode({"q": query})
        message = f"Открываю Google и ищу: {query}."
    else:
        url = "https://www.google.com/"
        message = "Открываю Google."
    try:
        webbrowser.open_new_tab(url)
        return message
    except Exception:
        return "Не удалось открыть браузер. Проверь, что в Windows выбран браузер по умолчанию."


def _skills_help_answer() -> str:
    return (
        "Все навыки вызываются прямо в этом чате — можно писать или говорить обычными словами.\n\n"
        "• Интернет: «Найди в интернете …»; подробный обзор: «Подробно изучи несколько сайтов про …».\n"
        "• Google: «Найди … в Google».\n"
        "• Погода, время, дата и расчёты: задай вопрос обычной фразой.\n"
        "• Память: «Запомни, что …», «Что ты помнишь?», «Забудь …».\n"
        "• Документы: «Добавь документ» откроет выбор файла; «Найди в моих документах …» ищет по локальному индексу; «Покажи документы» выведет список; удаление просит подтверждение.\n"
        "• Календарь: «Какие у меня сегодня события?» или «Покажи календарь».\n"
        "• Умный дом: «Покажи состояние дома», затем команда управления и отдельное подтверждение.\n"
        "• Таймеры: «Напомни через 10 минут …».\n"
        "• Приложения: «Открой блокнот» / «Открой калькулятор».\n"
        "• Интерфейс: «Покажи голограмму», «Включи микрофон», «Переключи режим микрофона на push-to-talk»; «Управляй памятью/документами» открывает менеджер.\n"
        "• Буфер обмена и файлы: «Суммируй буфер обмена», «Сравни два документа», «Сохрани последний ответ» или «Сохрани последний ответ аудио».\n"
        "• История/избранное: включение — «Включи локальную историю» (по умолчанию выключена); ищи «Найди в истории …», сохраняй «Закрепи последний ответ».\n"
        "• Интеграции: «Настрой интеграции» покажет локальные инструкции Calendar и Home Assistant.\n\n"
        "Кнопки слева остаются быстрыми ярлыками; они не переключают чат и не обязательны для команд."
    )


def _documents_chat_command(text: str) -> str | None:
    n = normalize(text)
    if any(phrase in n for phrase in ("покажи документы", "список документов", "какие документы загружены", "какие документы есть", "мои документы")):
        documents = list_indexed_documents()
        if not documents:
            return "Локальная база документов пока пуста. Напиши «Добавь документ», чтобы выбрать файлы."
        names = "\n".join(f"• {title}" for _path, title, _mtime in documents[:50])
        more = f"\n… и ещё {len(documents) - 50}." if len(documents) > 50 else ""
        return f"В локальной базе знаний {len(documents)} документ(ов):\n{names}{more}"

    clear_match = re.match(
        r"\s*(подтверждаю\s+)?(?:очисти|очистить|удали|удалить)\s+(?:всю\s+)?(?:локальную\s+)?базу\s+(?:знаний|документов)\s*[.!?]*\s*$",
        text,
        flags=re.IGNORECASE,
    )
    if clear_match:
        if not clear_match.group(1):
            return "Документы не удалены. Если уверен, напиши: «Подтверждаю очистить базу документов»."
        count = clear_indexed_documents()
        return f"Удалил из локального индекса документов: {count}. Исходные файлы не тронуты."

    delete_match = re.match(
        r"\s*(подтверждаю\s+)?(?:удали|удалить)\s+(?:документ|файл)\s+(.+?)\s*[.!?]*\s*$",
        text,
        flags=re.IGNORECASE,
    )
    if not delete_match:
        return None
    confirmed = bool(delete_match.group(1))
    query = delete_match.group(2).strip().casefold()
    matches = [(path, title) for path, title, _mtime in list_indexed_documents() if query in title.casefold() or query in path.casefold()]
    if not matches:
        return "Не нашёл документ с таким названием в локальном индексе. Напиши «Покажи документы»."
    if len(matches) > 1:
        return "Нашёл несколько совпадений: " + "; ".join(title for _path, title in matches[:10]) + ". Уточни название."
    path, title = matches[0]
    if not confirmed:
        return f"Документ «{title}» не удалён. Для подтверждения напиши: «Подтверждаю удалить документ {title}»."
    delete_indexed_document(path)
    return f"Удалил «{title}» из локального индекса. Исходный файл не удалял."


def _integration_request(text: str) -> str | None:
    n = normalize(text)
    calendar_intent = any(word in n for word in ("календарь", "календар", "расписание", "мои события", "мое расписание", "что запланировано"))
    if calendar_intent or ("событ" in n and any(word in n for word in ("сегодня", "завтра", "на неделю", "неделе"))):
        days = 7 if any(word in n for word in ("на неделю", "неделе")) else 2 if "завтра" in n else 1
        return google_calendar_events(days)

    if any(phrase in n for phrase in ("home assistant", "умный дом", "состояние дома", "устройства дома", "что включено дома")):
        return home_assistant_states()

    action_match = re.match(
        r"\s*(?:(подтверждаю)\s+)?(включи|включить|выключи|выключить)\s+(.+)",
        text,
        flags=re.IGNORECASE,
    )
    if action_match:
        confirmed = bool(action_match.group(1))
        verb = action_match.group(2)
        target = action_match.group(3).strip(" .,!?")
        if any(word in normalize(target) for word in ("свет", "ламп", "розетк", "выключател", "вентилятор", "light", "switch")):
            action = "on" if verb.casefold().startswith("включ") else "off"
            return home_assistant_control(action, target, confirmed=confirmed)
    return None


def handle_request(
    history: list[dict[str, str]],
    user_text: str,
    mode: str = "auto",
    on_chunk: Callable[[str], None] | None = None,
) -> str:
    """Маршрутизатор локальной памяти, таймеров, интеграций, приложений, веба и Ollama."""
    text = user_text.strip()
    voice_command = extract_command_after_wake_word(text)
    if voice_command is not None and voice_command:
        text = voice_command

    if not text:
        return "Напиши вопрос или скажи команду."

    if mode == "local_only":
        answer = ask_ollama(history, text, on_chunk=on_chunk)
        _remember(history, user_text, answer)
        return answer

    normalized = normalize(text)
    help_requests = {
        "помощь", "справка", "help", "что ты умеешь", "что умеешь", "покажи навыки",
        "покажи список навыков", "список навыков", "какие у тебя навыки", "как пользоваться",
    }
    if normalized in help_requests or normalized.startswith("что ты умеешь "):
        answer = _skills_help_answer()
        _remember(history, user_text, answer)
        return answer

    memory_answer = _memory_command(text)
    if memory_answer is not None:
        _remember(history, user_text, memory_answer)
        return memory_answer

    reminder_answer = _reminder_command(text)
    if reminder_answer is not None:
        _remember(history, user_text, reminder_answer)
        return reminder_answer

    document_answer = _documents_chat_command(text)
    if document_answer is not None:
        _remember(history, user_text, document_answer)
        return document_answer

    if mode == "google":
        answer = _open_google(text)
        _remember(history, user_text, answer)
        return answer

    if mode != "web":
        google_query = _google_query(text)
        if google_query is not None:
            answer = _open_google(google_query)
            _remember(history, user_text, answer)
            return answer

    app_answer = open_approved_apps(text)
    if app_answer is not None:
        _remember(history, user_text, app_answer)
        return app_answer

    integration_answer = _integration_request(text)
    if integration_answer is not None:
        _remember(history, user_text, integration_answer)
        return integration_answer

    if _is_weather_request(text):
        answer = get_weather_forecast(_extract_city(text))
        _remember(history, user_text, answer)
        return answer

    clock_answer = _clock_answer(text)
    if clock_answer:
        _remember(history, user_text, clock_answer)
        return clock_answer

    math_answer = _calculator_answer(text)
    if math_answer:
        _remember(history, user_text, math_answer)
        return math_answer

    if mode in {"web", "deep_web"} or _needs_web_search(text):
        query = _web_query(text)
        results, error = search_web(query)
        if error:
            _remember(history, user_text, error)
            return error
        if not results:
            answer = "По этому запросу не удалось найти результаты. Попробуй переформулировать его."
            _remember(history, user_text, answer)
            return answer
        context = deep_web_context(results) if mode == "deep_web" else _web_context(results)
        return ask_ollama(history, user_text, web_context=context, on_chunk=on_chunk)

    local_sources = search_local_knowledge(text)
    if local_sources:
        return ask_ollama(history, user_text, knowledge_context=_knowledge_context(local_sources), on_chunk=on_chunk)

    return ask_ollama(history, user_text, on_chunk=on_chunk)


def make_audio_callback(audio_queue: queue.Queue[bytes]):
    def callback(indata, frames, time_info, status):
        try:
            audio_queue.put_nowait(bytes(indata))
        except queue.Full:
            try:
                audio_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                audio_queue.put_nowait(bytes(indata))
            except queue.Full:
                pass

    return callback


def clear_audio_queue(audio_queue: queue.Queue[bytes]) -> None:
    while True:
        try:
            audio_queue.get_nowait()
        except queue.Empty:
            return


def listen_for_phrase(recognizer: KaldiRecognizer, audio_queue: queue.Queue[bytes]) -> str:
    while True:
        audio = audio_queue.get()
        if recognizer.AcceptWaveform(audio):
            result = json.loads(recognizer.Result())
            return result.get("text", "").strip()


def create_tts_engine() -> tuple[pyttsx3.Engine, bool]:
    engine = pyttsx3.init()
    engine.setProperty("rate", 178)
    russian_voice_found = False
    for voice in engine.getProperty("voices") or []:
        metadata = f"{voice.id} {voice.name} {getattr(voice, 'languages', [])}".casefold()
        if any(marker in metadata for marker in ("russian", "ru-ru", "ru_ru", "ruru")):
            engine.setProperty("voice", voice.id)
            russian_voice_found = True
            break
    return engine, russian_voice_found


def speak(engine: pyttsx3.Engine, text: str) -> None:
    print(f"Джарвис: {text}")
    engine.say(text)
    engine.runAndWait()


def main() -> None:
    model_dir = find_vosk_model_dir()
    if model_dir is None:
        print("Не найдена корневая папка русской модели Vosk.")
        print(f"Проверь распаковку рядом с программой: {BASE_DIR}")
        print("Внутри папки модели должны находиться каталоги am, conf и graph.")
        return

    try:
        tts_engine, russian_voice_found = create_tts_engine()
        vosk_model = Model(str(model_dir))
    except Exception as exc:
        print(f"Не удалось подготовить голос или модель Vosk: {exc}")
        return

    if not russian_voice_found:
        print("Предупреждение: русская озвучка Windows не найдена; будет использован голос по умолчанию.")

    recognizer = KaldiRecognizer(vosk_model, SAMPLE_RATE)
    audio_queue: queue.Queue[bytes] = queue.Queue(maxsize=20)
    history: list[dict[str, str]] = []
    print("Джарвис запущен. Скажи «Джарвис» перед вопросом или командой. Остановка: Ctrl+C.")

    try:
        with sd.RawInputStream(
            samplerate=SAMPLE_RATE,
            blocksize=8_000,
            dtype="int16",
            channels=1,
            callback=make_audio_callback(audio_queue),
        ):
            speak(tts_engine, "Привет. Я запущен и слушаю.")
            while True:
                clear_audio_queue(audio_queue)
                recognizer.Reset()
                phrase = listen_for_phrase(recognizer, audio_queue)
                if not phrase:
                    continue
                print(f"Распознано: {phrase}")
                command = extract_command_after_wake_word(phrase)
                if command is None:
                    continue
                if not command:
                    speak(tts_engine, "Я слушаю. Скажи вопрос или команду.")
                    continue
                if normalize(command) in EXIT_PHRASES:
                    speak(tts_engine, "Хорошо, останавливаюсь.")
                    break
                answer = handle_request(history, command)
                speak(tts_engine, answer)
    except KeyboardInterrupt:
        print("\nДжарвис остановлен.")
    except Exception as exc:
        print(f"Ошибка микрофона или аудиоустройства: {exc}")
        print("Проверь разрешение на микрофон в настройках конфиденциальности Windows.")


if __name__ == "__main__":
    main()
