"""Необязательные интеграции Google Calendar и Home Assistant.

Секреты читаются из переменных окружения Windows, а не из кода/чата:
JARVIS_HA_URL, JARVIS_HA_TOKEN, JARVIS_HA_ALLOWED_ENTITIES.
Google OAuth-файл credentials.json пользователь кладёт локально рядом со скриптами.
"""
from __future__ import annotations

import datetime as dt
import os
import re
import sys
from pathlib import Path

import requests

BASE_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
GOOGLE_CREDENTIALS = BASE_DIR / "credentials.json"
GOOGLE_TOKEN = BASE_DIR / "calendar_token.json"
GOOGLE_SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]


def _ha_config() -> tuple[str, str, set[str]]:
    url = os.getenv("JARVIS_HA_URL", "").strip().rstrip("/")
    token = os.getenv("JARVIS_HA_TOKEN", "").strip()
    allowed = {item.strip() for item in os.getenv("JARVIS_HA_ALLOWED_ENTITIES", "").split(",") if item.strip()}
    return url, token, allowed


_ROOM_ALIASES = (
    ("кух", ("кухн", "kitchen")),
    ("спальн", ("спальн", "bedroom")),
    ("гостин", ("гостин", "living_room", "living room", "lounge")),
    ("офис", ("офис", "office", "study")),
    ("ванн", ("ванн", "bathroom")),
    ("коридор", ("коридор", "hallway", "hall")),
    ("гараж", ("гараж", "garage")),
)
_GENERIC_TARGET_WORDS = {
    "свет", "лампу", "лампа", "лампочка", "дома", "на", "в", "во", "для", "комнате",
    "розетку", "розетка", "выключатель", "вентилятор", "fan", "switch", "light",
}


def _ha_target_terms(target: str) -> list[set[str]]:
    terms: list[set[str]] = []
    for word in re.findall(r"[a-zа-я0-9_.]+", target.casefold()):
        if len(word) <= 2 or word in _GENERIC_TARGET_WORDS or word.startswith(("розетк", "выключател", "вентилятор", "ламп")):
            continue
        aliases = next((set(values) for prefix, values in _ROOM_ALIASES if word.startswith(prefix)), {word})
        terms.append(aliases)
    return terms


def home_assistant_states(query: str = "") -> str:
    url, token, _allowed = _ha_config()
    if not url or not token:
        return "Home Assistant пока не настроен. Задай локально переменные JARVIS_HA_URL и JARVIS_HA_TOKEN."
    try:
        response = requests.get(
            f"{url}/api/states",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            timeout=8,
        )
        response.raise_for_status()
        states = response.json()
        if query:
            q = query.casefold()
            states = [s for s in states if q in str(s.get("entity_id", "")).casefold() or q in str(s.get("attributes", {}).get("friendly_name", "")).casefold()]
        states = [s for s in states if str(s.get("entity_id", "")).split(".", 1)[0] in {"light", "switch", "sensor", "binary_sensor", "climate", "fan"}]
        if not states:
            return "Подходящих устройств не найдено."
        rows = []
        for item in states[:20]:
            name = item.get("attributes", {}).get("friendly_name", item.get("entity_id", "устройство"))
            rows.append(f"• {name}: {item.get('state', 'неизвестно')} ({item.get('entity_id', '')})")
        return "Состояние Home Assistant:\n" + "\n".join(rows)
    except requests.exceptions.RequestException as exc:
        return f"Не удалось связаться с Home Assistant: {exc}"


def home_assistant_control(action: str, target: str, confirmed: bool = False) -> str:
    """Управляет только разрешёнными entity_id и просит явное подтверждение."""
    if not confirmed:
        verb = "включить" if action == "on" else "выключить"
        return f"Пока ничего не менял. Для подтверждения скажи: «подтверждаю {verb} {target}»."
    url, token, allowed = _ha_config()
    if not url or not token:
        return "Home Assistant пока не настроен. Задай локально переменные JARVIS_HA_URL и JARVIS_HA_TOKEN."
    if not allowed:
        return "Для безопасности сначала задай JARVIS_HA_ALLOWED_ENTITIES со списком разрешённых entity_id через запятую."
    normalized_target = target.casefold()
    if any(word in normalized_target for word in ("свет", "ламп")):
        desired_domain = "light"
    elif any(word in normalized_target for word in ("вентилятор", "fan")):
        desired_domain = "fan"
    elif any(word in normalized_target for word in ("розетк", "выключател", "switch")):
        desired_domain = "switch"
    else:
        desired_domain = None
    try:
        response = requests.get(
            f"{url}/api/states",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            timeout=8,
        )
        response.raise_for_status()
        all_states = response.json()
        candidates = []
        query_terms = _ha_target_terms(target)
        for state in all_states:
            entity = str(state.get("entity_id", ""))
            domain = entity.split(".", 1)[0]
            if entity not in allowed or domain not in {"light", "switch", "fan", "input_boolean"}:
                continue
            if desired_domain and domain != desired_domain:
                continue
            friendly = str(state.get("attributes", {}).get("friendly_name", "")).casefold()
            haystack = f"{entity.casefold()} {friendly}"
            if not query_terms or all(any(alias in haystack for alias in aliases) for aliases in query_terms):
                candidates.append(entity)
        if len(candidates) != 1:
            return "Не нашёл ровно одно разрешённое устройство. Уточни его имя или проверь JARVIS_HA_ALLOWED_ENTITIES."

        entity_id = candidates[0]
        domain = entity_id.split(".", 1)[0]
        service = "turn_on" if action == "on" else "turn_off"
        response = requests.post(
            f"{url}/api/services/{domain}/{service}",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"entity_id": entity_id},
            timeout=8,
        )
        response.raise_for_status()
        return f"Отправил Home Assistant команду {service} для {entity_id}."
    except requests.exceptions.RequestException as exc:
        return f"Ошибка Home Assistant: {exc}"


def _google_credentials():
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        raise RuntimeError("Для Google Calendar установи пакеты из requirements.txt.")

    creds = None
    if GOOGLE_TOKEN.exists():
        creds = Credentials.from_authorized_user_file(str(GOOGLE_TOKEN), GOOGLE_SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    if not creds or not creds.valid:
        if not GOOGLE_CREDENTIALS.exists():
            raise RuntimeError("Положи OAuth-файл credentials.json рядом с программой после настройки Google Calendar API.")
        flow = InstalledAppFlow.from_client_secrets_file(str(GOOGLE_CREDENTIALS), GOOGLE_SCOPES)
        creds = flow.run_local_server(port=0)
        GOOGLE_TOKEN.write_text(creds.to_json(), encoding="utf-8")
    return creds


def google_calendar_events(days: int = 1) -> str:
    try:
        from googleapiclient.discovery import build
        creds = _google_credentials()
        service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        now = dt.datetime.now().astimezone()
        end = now + dt.timedelta(days=max(1, min(days, 14)))
        result = service.events().list(
            calendarId="primary",
            timeMin=now.isoformat(),
            timeMax=end.isoformat(),
            maxResults=20,
            singleEvents=True,
            orderBy="startTime",
        ).execute()
        events = result.get("items", [])
        if not events:
            return "В выбранном периоде событий в Google Calendar не нашёл."
        lines = []
        for event in events:
            start = event.get("start", {}).get("dateTime", event.get("start", {}).get("date", ""))
            title = event.get("summary", "Без названия")
            lines.append(f"• {start}: {title}")
        return "События Google Calendar:\n" + "\n".join(lines)
    except Exception as exc:
        return f"Google Calendar не настроен или недоступен: {exc}"
