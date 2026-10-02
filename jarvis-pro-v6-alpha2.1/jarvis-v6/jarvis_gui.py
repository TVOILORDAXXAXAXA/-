"""Графический интерфейс Джарвиса с потоковыми ответами и плавающей HUD-голограммой."""

from __future__ import annotations

import array
import datetime as dt
import json
import math
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
import uuid
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import sounddevice as sd
from vosk import KaldiRecognizer, Model

import jarvis as core
from jarvis import (
    OLLAMA_MODEL,
    SAMPLE_RATE,
    VOSK_MODEL_DIR,
    clear_audio_queue,
    create_tts_engine,
    clear_indexed_documents,
    compare_documents,
    delete_indexed_document,
    delete_memory,
    extract_command_after_wake_word,
    forget_memories,
    handle_request,
    index_documents,
    list_indexed_documents,
    list_memories,
    make_audio_callback,
    pop_due_reminders,
)
from history_store import (
    add_bookmark,
    clear_history,
    delete_bookmark,
    list_bookmarks,
    new_conversation_id,
    save_message,
    search_messages,
)
from settings import load_settings, save_settings


BASE_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
EXPECTED_MODEL_NAME = "vosk-model-small-ru-0.22"
VOICE_MODE_LABELS = {
    "wake": "По имени «Джарвис»",
    "toggle": "Кнопочный режим",
    "ptt": "Зажать для разговора",
}
VOICE_LABEL_MODES = {label: mode for mode, label in VOICE_MODE_LABELS.items()}

COLORS = {
    "background": "#080d18",
    "sidebar": "#060a13",
    "panel": "#101827",
    "panel_alt": "#172235",
    "user_bubble": "#10364f",
    "assistant_bubble": "#121d2e",
    "text": "#e7f4ff",
    "muted": "#8da4b9",
    "cyan": "#54e8ff",
    "green": "#55ffc2",
    "amber": "#ffc857",
}


def find_vosk_model_dir() -> Path | None:
    """Находит модель Vosk при обычной и вложенной распаковке ZIP."""
    expected = BASE_DIR / EXPECTED_MODEL_NAME
    candidates = [
        VOSK_MODEL_DIR,
        expected,
        expected / EXPECTED_MODEL_NAME,
        VOSK_MODEL_DIR / EXPECTED_MODEL_NAME,
    ]
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate.resolve()).casefold()
        if key in seen:
            continue
        seen.add(key)
        if all((candidate / name).is_dir() for name in ("am", "conf", "graph")):
            return candidate
    return None


class HologramHUD:
    """Экранная голограмма-HUD: плавающее окно с анимированным энергетическим ядром."""

    def __init__(self, app: "JarvisGUI") -> None:
        self.app = app
        self.window = tk.Toplevel(app.root)
        self.window.title("Jarvis hologram")
        self.window.geometry("390x500")
        self.window.update_idletasks()
        screen_x = max(10, self.window.winfo_screenwidth() - 410)
        self.window.geometry(f"390x500+{screen_x}+90")
        self.window.resizable(False, False)
        self.window.configure(bg="#030912")
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        try:
            self.window.attributes("-alpha", 0.94)
        except tk.TclError:
            pass

        self.frame = 0
        self.state = "idle"
        self.audio_level = 0.0
        self.text_value = "На связи. Задай вопрос или включи микрофон."
        self._after_id = None
        self._drag_x = 0
        self._drag_y = 0

        titlebar = tk.Frame(self.window, bg="#071321", height=34)
        titlebar.pack(fill="x")
        titlebar.pack_propagate(False)
        tk.Label(
            titlebar,
            text="◈  JARVIS HOLOGRAM  ·  LIVE",
            bg="#071321",
            fg=COLORS["cyan"],
            font=("Segoe UI", 9, "bold"),
        ).pack(side="left", padx=12)
        close_button = tk.Button(
            titlebar,
            text="×",
            command=self.close,
            bg="#071321",
            fg="#9bb7c8",
            activebackground="#1a2939",
            activeforeground="white",
            relief="flat",
            bd=0,
            font=("Segoe UI", 15),
            cursor="hand2",
        )
        close_button.pack(side="right", padx=7)
        titlebar.bind("<ButtonPress-1>", self._drag_start)
        titlebar.bind("<B1-Motion>", self._drag_move)

        self.canvas = tk.Canvas(
            self.window,
            width=390,
            height=320,
            bg="#030912",
            highlightthickness=0,
            bd=0,
        )
        self.canvas.pack(fill="x")
        self._draw_static_hud()

        self.state_var = tk.StringVar(value="ГОТОВ")
        tk.Label(
            self.window,
            textvariable=self.state_var,
            bg="#030912",
            fg=COLORS["green"],
            font=("Segoe UI", 9, "bold"),
        ).pack(pady=(0, 4))
        self.text_label = tk.Label(
            self.window,
            text=self.text_value,
            bg="#071321",
            fg=COLORS["text"],
            justify="center",
            wraplength=340,
            padx=14,
            pady=12,
            font=("Segoe UI", 10),
        )
        self.text_label.pack(fill="x", padx=16, pady=(0, 15))
        self._animate()
        self.window.protocol("WM_DELETE_WINDOW", self.close)

    def _draw_static_hud(self) -> None:
        c = self.canvas
        # Сетка и горизонтальные линии придают окну HUD-вид.
        for y in range(18, 320, 24):
            c.create_line(18, y, 372, y, fill="#0b2332", width=1)
        for x in range(22, 390, 28):
            c.create_line(x, 8, x, 286, fill="#071a28", width=1)
        c.create_oval(46, 15, 344, 313, outline="#123346", width=1)
        c.create_oval(65, 34, 325, 294, outline="#0d2b3a", width=1)
        self.outer_ring = c.create_oval(94, 63, 296, 265, outline=COLORS["cyan"], width=2)
        self.inner_ring = c.create_oval(119, 88, 271, 240, outline="#1aaac5", width=2)
        self.core_glow = c.create_oval(137, 106, 253, 222, fill="#082537", outline=COLORS["cyan"], width=2)
        self.core = c.create_oval(153, 122, 237, 206, fill="#0d3e58", outline="#a3fbff", width=2)
        self.arc_a = c.create_arc(83, 52, 307, 276, start=20, extent=78, style="arc", outline=COLORS["cyan"], width=3)
        self.arc_b = c.create_arc(74, 43, 316, 285, start=190, extent=65, style="arc", outline=COLORS["green"], width=2)
        self.arc_c = c.create_arc(106, 75, 284, 253, start=110, extent=58, style="arc", outline="#7c6cff", width=2)
        self.center_text = c.create_text(195, 164, text="J", fill="#d7ffff", font=("Segoe UI", 30, "bold"))
        self.dots = [c.create_oval(0, 0, 0, 0, fill=COLORS["cyan"], outline="") for _ in range(20)]
        self.bars = [c.create_line(0, 0, 0, 0, fill=COLORS["cyan"], width=2) for _ in range(28)]
        c.create_text(195, 299, text="LOCAL NEURAL INTERFACE", fill="#27627a", font=("Consolas", 8, "bold"))

    def set_state(self, state: str) -> None:
        self.state = state
        titles = {
            "idle": ("ГОТОВ", COLORS["green"]),
            "listening": ("СЛУШАЮ", COLORS["green"]),
            "thinking": ("АНАЛИЗИРУЮ", COLORS["amber"]),
            "speaking": ("ОТВЕЧАЮ", COLORS["cyan"]),
        }
        title, color = titles.get(state, titles["idle"])
        self.state_var.set(title)
        for item in (self.outer_ring, self.arc_a, self.arc_b, self.arc_c):
            self.canvas.itemconfigure(item, outline=color)

    def set_text(self, text: str) -> None:
        self.text_value = text
        # Ограничиваем окошко, оставляя последнюю часть текущего ответа.
        if len(text) > 420:
            text = "…" + text[-417:]
        self.text_label.configure(text=text)

    def set_audio_level(self, level: float) -> None:
        self.audio_level = max(0.0, min(1.0, float(level)))

    def _animate(self) -> None:
        if not self.window.winfo_exists():
            return
        self.frame += 1
        c = self.canvas
        speed = 0.105 if self.state == "thinking" else 0.065
        pulse = math.sin(self.frame * speed)
        if self.state == "listening":
            activity = self.audio_level
        elif self.state in {"speaking", "thinking"}:
            activity = 0.25 + 0.55 * abs(math.sin(self.frame * 0.16))
        else:
            activity = 0.08 + 0.08 * abs(pulse)
        radius = 99 + pulse * (4 + 8 * activity)
        c.coords(self.outer_ring, 195 - radius, 164 - radius, 195 + radius, 164 + radius)
        c.coords(self.core_glow, 137 - pulse * 2, 106 - pulse * 2, 253 + pulse * 2, 222 + pulse * 2)
        c.itemconfigure(self.arc_a, start=(self.frame * 3) % 360)
        c.itemconfigure(self.arc_b, start=(180 - self.frame * 2) % 360)
        c.itemconfigure(self.arc_c, start=(100 + self.frame * 4) % 360)

        for index, dot in enumerate(self.dots):
            angle = self.frame * 0.035 + index * math.tau / len(self.dots)
            orbit = 117 + 7 * math.sin(self.frame * 0.04 + index)
            x = 195 + math.cos(angle) * orbit
            y = 164 + math.sin(angle) * orbit
            c.coords(dot, x - 2, y - 2, x + 2, y + 2)

        for index, bar in enumerate(self.bars):
            x = 67 + index * 9.5
            wave = abs(math.sin(self.frame * 0.12 + index * 0.7))
            amplitude = 2 + wave * (5 + 19 * activity)
            c.coords(bar, x, 276 - amplitude, x, 276 + amplitude)
        self._after_id = self.window.after(50, self._animate)

    def _drag_start(self, event) -> None:
        self._drag_x = event.x_root - self.window.winfo_x()
        self._drag_y = event.y_root - self.window.winfo_y()

    def _drag_move(self, event) -> None:
        x = event.x_root - self._drag_x
        y = event.y_root - self._drag_y
        self.window.geometry(f"+{x}+{y}")

    def close(self) -> None:
        if self._after_id is not None:
            try:
                self.window.after_cancel(self._after_id)
            except tk.TclError:
                pass
        try:
            self.window.destroy()
        except tk.TclError:
            pass
        self.app.hologram = None
        if not self.app.closing.is_set():
            try:
                self.app.holo_button.configure(text="◇   Показать голограмму")
            except tk.TclError:
                pass


class JarvisGUI:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Джарвис | локальный AI-помощник")
        self.root.geometry("1100x760")
        self.root.minsize(780, 570)
        self.root.configure(bg=COLORS["background"])

        self.request_queue: queue.Queue[tuple[str, str, str]] = queue.Queue()
        self.ui_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.settings = load_settings()
        self.history_id = new_conversation_id()
        self.last_answer = ""
        self.model_var = tk.StringVar(value=core.OLLAMA_MODEL)
        self.voice_mode_var = tk.StringVar(value=VOICE_MODE_LABELS.get(self.settings.get("voice_mode", "wake"), VOICE_MODE_LABELS["wake"]))
        self.voice_active = threading.Event()
        self.ptt_held = threading.Event()
        self.speaking = threading.Event()
        self.stop_speaking = threading.Event()
        self.closing = threading.Event()
        self.vosk_lock = threading.Lock()
        self.vosk_model = None
        self.voice_thread: threading.Thread | None = None
        self.hologram: HologramHUD | None = None
        self.stream_body = None
        self.stream_text = ""

        self._build_ui()
        self._configure_mic_button()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(60, self._poll_ui_events)
        self.root.after(1000, self._check_reminders)
        self._append_message(
            "Джарвис",
            "Привет! Все мои навыки доступны прямо здесь: напиши или скажи «Покажи навыки», чтобы увидеть примеры команд. Пиши обычными словами — интернет, документы, память, календарь, умный дом, таймеры и другие запросы идут в этот же чат. Кнопки слева — только необязательные ярлыки.",
        )
        threading.Thread(target=self._assistant_worker, daemon=True, name="jarvis-assistant").start()

    def _setup_theme(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(
            "Jarvis.TCombobox",
            fieldbackground=COLORS["panel_alt"],
            background=COLORS["panel_alt"],
            foreground=COLORS["text"],
            arrowcolor=COLORS["cyan"],
            bordercolor=COLORS["panel_alt"],
            lightcolor=COLORS["panel_alt"],
            darkcolor=COLORS["panel_alt"],
            padding=(8, 6),
        )
        style.map(
            "Jarvis.TCombobox",
            fieldbackground=[("readonly", COLORS["panel_alt"]), ("focus", COLORS["panel_alt"])],
            foreground=[("readonly", COLORS["text"])],
            selectbackground=[("readonly", COLORS["panel_alt"])],
            selectforeground=[("readonly", COLORS["text"])],
        )
        style.configure(
            "Jarvis.Vertical.TScrollbar",
            background=COLORS["panel_alt"],
            troughcolor=COLORS["background"],
            bordercolor=COLORS["background"],
            arrowcolor=COLORS["muted"],
            width=10,
        )

    def _build_ui(self) -> None:
        self._setup_theme()
        self.root.grid_rowconfigure(0, weight=1)
        self.root.grid_columnconfigure(1, weight=1)

        sidebar_shell = tk.Frame(self.root, bg=COLORS["sidebar"], width=238)
        sidebar_shell.grid(row=0, column=0, sticky="nsew")
        sidebar_shell.grid_propagate(False)
        sidebar_canvas = tk.Canvas(sidebar_shell, bg=COLORS["sidebar"], highlightthickness=0, width=218)
        self.sidebar_canvas = sidebar_canvas
        sidebar_scroll = ttk.Scrollbar(sidebar_shell, orient="vertical", command=sidebar_canvas.yview, style="Jarvis.Vertical.TScrollbar")
        sidebar_canvas.configure(yscrollcommand=sidebar_scroll.set)
        sidebar_canvas.pack(side="left", fill="both", expand=True)
        sidebar_scroll.pack(side="right", fill="y")
        sidebar = tk.Frame(sidebar_canvas, bg=COLORS["sidebar"])
        sidebar_window = sidebar_canvas.create_window((0, 0), window=sidebar, anchor="nw")
        sidebar.bind("<Configure>", lambda _event: sidebar_canvas.configure(scrollregion=sidebar_canvas.bbox("all")))
        sidebar_canvas.bind("<Configure>", lambda event: sidebar_canvas.itemconfigure(sidebar_window, width=event.width))

        brand = tk.Frame(sidebar, bg=COLORS["sidebar"], padx=19, pady=22)
        brand.pack(fill="x")
        tk.Label(brand, text="◈  JARVIS", bg=COLORS["sidebar"], fg=COLORS["cyan"], font=("Segoe UI", 19, "bold")).pack(anchor="w")
        tk.Label(brand, text="LOCAL AI · VOICE · WEB", bg=COLORS["sidebar"], fg=COLORS["muted"], font=("Segoe UI", 8, "bold")).pack(anchor="w", pady=(4, 0))

        model_card = tk.Frame(sidebar, bg=COLORS["panel"], padx=12, pady=11)
        model_card.pack(fill="x", padx=12, pady=(4, 17))
        tk.Label(model_card, text="●  Ollama локально", bg=COLORS["panel"], fg=COLORS["green"], font=("Segoe UI", 9, "bold")).pack(anchor="w")
        tk.Label(model_card, text="Модель для ответа:", bg=COLORS["panel"], fg=COLORS["muted"], font=("Segoe UI", 8)).pack(anchor="w", pady=(5, 2))
        model_values = self._installed_models()
        self.model_combo = ttk.Combobox(model_card, textvariable=self.model_var, values=model_values, state="readonly", font=("Segoe UI", 9), style="Jarvis.TCombobox")
        self.model_combo.pack(fill="x")
        self.model_combo.bind("<<ComboboxSelected>>", self._on_model_changed)

        tk.Label(sidebar, text="РЕЖИМ ГОЛОСА", bg=COLORS["sidebar"], fg="#64748b", font=("Segoe UI", 8, "bold"), padx=17, pady=5).pack(anchor="w", pady=(3, 5))
        self.voice_mode_combo = ttk.Combobox(
            sidebar,
            textvariable=self.voice_mode_var,
            values=list(VOICE_LABEL_MODES),
            state="readonly",
            font=("Segoe UI", 9),
            style="Jarvis.TCombobox",
        )
        self.voice_mode_combo.pack(fill="x", padx=12, pady=(0, 10))
        self.voice_mode_combo.bind("<<ComboboxSelected>>", self._on_voice_mode_changed)

        tk.Label(sidebar, text="БЫСТРЫЕ КНОПКИ", bg=COLORS["sidebar"], fg="#64748b", font=("Segoe UI", 8, "bold"), padx=17, pady=5).pack(anchor="w")
        self._side_button(sidebar, "＋   Новый диалог", self.clear_chat)
        self._side_button(sidebar, "⌕   Веб-поиск и ответ", self.search_from_input, accent=True)
        self._side_button(sidebar, "▤   Подробно изучить сайты", self.deep_search_from_input)
        self._side_button(sidebar, "↗   Найти в Google", self.google_from_input)
        self._side_button(sidebar, "☼   Погода в Тбилиси", self.ask_tbilisi_weather)
        self._side_button(sidebar, "▤   Мой календарь", self.ask_calendar)
        self._side_button(sidebar, "⌂   Умный дом", self.ask_homeassistant)
        self._side_button(sidebar, "⚙   Настройка интеграций", lambda: self.send_preset("Настрой интеграции"))
        self._side_button(sidebar, "▧   Добавить документы", lambda: self.send_preset("Добавь документ"))
        self._side_button(sidebar, "▧   Управлять документами", lambda: self.send_preset("Управляй документами"))
        self._side_button(sidebar, "◌   Управлять памятью", lambda: self.send_preset("Управляй памятью"))
        self._side_button(sidebar, "◷   История чатов", lambda: self.send_preset("Открой историю чатов"))
        self._side_button(sidebar, "☆   Избранные ответы", lambda: self.send_preset("Покажи закладки"))
        self.mic_button = self._side_button(sidebar, "◉   Включить микрофон", self.toggle_microphone)
        self.stop_button = self._side_button(sidebar, "■   Остановить озвучивание", self.stop_current_speech)
        self.holo_button = self._side_button(sidebar, "◇   Показать голограмму", self.toggle_hologram)
        self._side_button(sidebar, "▦   Открыть блокнот", lambda: self.send_preset("Открой блокнот"))
        self._side_button(sidebar, "▦   Открыть калькулятор", lambda: self.send_preset("Открой калькулятор"))

        footer = tk.Frame(sidebar, bg=COLORS["sidebar"], padx=17, pady=14)
        footer.pack(side="bottom", fill="x")
        tk.Label(
            footer,
            text="ГОЛОС РАСПОЗНАЁТСЯ ЛОКАЛЬНО\nГолограмма — экранный HUD,\nне физический проектор.",
            bg=COLORS["sidebar"],
            fg="#637b91",
            justify="left",
            font=("Segoe UI", 8),
        ).pack(anchor="w")

        main = tk.Frame(self.root, bg=COLORS["background"])
        main.grid(row=0, column=1, sticky="nsew")
        main.grid_rowconfigure(1, weight=1)
        main.grid_columnconfigure(0, weight=1)

        topbar = tk.Frame(main, bg=COLORS["background"], padx=23, pady=15)
        topbar.grid(row=0, column=0, sticky="ew")
        title_group = tk.Frame(topbar, bg=COLORS["background"])
        title_group.pack(side="left", fill="x", expand=True)
        tk.Label(title_group, text="JARVIS  /  ЕДИНЫЙ ЧАТ", bg=COLORS["background"], fg=COLORS["text"], font=("Segoe UI", 16, "bold")).pack(anchor="w")
        tk.Label(title_group, text="Команды, поиск и локальные инструменты — в одном диалоге", bg=COLORS["background"], fg=COLORS["muted"], font=("Segoe UI", 9)).pack(anchor="w", pady=(2, 0))
        self.status_var = tk.StringVar(value="Готов к работе")
        help_button = tk.Button(topbar, text="?  Навыки", command=lambda: self.send_preset("Покажи навыки"), bg=COLORS["panel_alt"], fg=COLORS["cyan"], activebackground="#20344c", activeforeground="white", relief="flat", padx=12, pady=7, font=("Segoe UI", 9, "bold"), cursor="hand2")
        help_button.pack(side="right", padx=(9, 0))
        status_pill = tk.Frame(topbar, bg=COLORS["panel"], padx=10, pady=6, highlightthickness=1, highlightbackground="#1c3147")
        status_pill.pack(side="right")
        tk.Label(status_pill, text="●", bg=COLORS["panel"], fg=COLORS["green"], font=("Segoe UI", 9, "bold")).pack(side="left", padx=(0, 6))
        tk.Label(status_pill, textvariable=self.status_var, bg=COLORS["panel"], fg=COLORS["muted"], font=("Segoe UI", 9)).pack(side="left")

        chat_frame = tk.Frame(main, bg=COLORS["background"])
        chat_frame.grid(row=1, column=0, sticky="nsew", padx=(12, 7), pady=(0, 11))
        chat_frame.grid_rowconfigure(0, weight=1)
        chat_frame.grid_columnconfigure(0, weight=1)
        self.chat_canvas = tk.Canvas(chat_frame, bg=COLORS["background"], highlightthickness=0, bd=0)
        self.chat_canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(chat_frame, orient="vertical", command=self.chat_canvas.yview, style="Jarvis.Vertical.TScrollbar")
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.chat_canvas.configure(yscrollcommand=scrollbar.set)
        self.chat_inner = tk.Frame(self.chat_canvas, bg=COLORS["background"])
        self.chat_window = self.chat_canvas.create_window((0, 0), window=self.chat_inner, anchor="nw")
        self.chat_inner.bind("<Configure>", self._on_chat_inner_configure)
        self.chat_canvas.bind("<Configure>", self._on_chat_canvas_configure)
        self.chat_canvas.bind_all("<MouseWheel>", self._on_mousewheel)

        quickbar = tk.Frame(main, bg=COLORS["background"])
        quickbar.grid(row=2, column=0, sticky="ew", padx=23, pady=(0, 7))
        tk.Label(quickbar, text="БЫСТРО", bg=COLORS["background"], fg="#5e758a", font=("Segoe UI", 8, "bold")).pack(side="left", padx=(0, 9))
        for label, command in (
            ("✦ Навыки", lambda: self.send_preset("Покажи навыки")),
            ("⌕ Интернет", self.search_from_input),
            ("▤ Календарь", self.ask_calendar),
            ("☼ Погода", self.ask_tbilisi_weather),
            ("＋ Документ", lambda: self.send_preset("Добавь документ")),
        ):
            tk.Button(quickbar, text=label, command=command, bg=COLORS["panel"], fg=COLORS["muted"], activebackground=COLORS["panel_alt"], activeforeground=COLORS["cyan"], relief="flat", padx=10, pady=5, font=("Segoe UI", 8, "bold"), cursor="hand2").pack(side="left", padx=(0, 6))

        composer = tk.Frame(main, bg=COLORS["panel"], padx=13, pady=11, highlightthickness=1, highlightbackground="#1c3147")
        composer.grid(row=3, column=0, sticky="ew", padx=21, pady=(0, 6))
        composer.grid_columnconfigure(0, weight=1)
        self.input_box = tk.Text(
            composer,
            height=3,
            wrap="word",
            bg=COLORS["panel"],
            fg=COLORS["text"],
            insertbackground=COLORS["text"],
            selectbackground="#155e75",
            relief="flat",
            bd=0,
            font=("Segoe UI", 11),
            undo=True,
        )
        self.input_box.grid(row=0, column=0, sticky="ew", padx=(4, 12))
        self.input_box.bind("<Control-Return>", self._send_from_keyboard)
        self.input_box.bind("<Shift-Return>", lambda _event: None)
        send_button = tk.Button(
            composer,
            text="Отправить  ➤",
            command=lambda: self.send_message("auto"),
            bg="#087ca5",
            fg="white",
            activebackground="#0891b2",
            activeforeground="white",
            relief="flat",
            padx=16,
            pady=9,
            font=("Segoe UI", 10, "bold"),
            cursor="hand2",
        )
        send_button.grid(row=0, column=1, sticky="se")
        tk.Label(
            main,
            text="Enter — новая строка   •   Ctrl+Enter — отправить   •   Ответ появляется по мере генерации",
            bg=COLORS["background"],
            fg="#5e758a",
            anchor="w",
            padx=24,
            font=("Segoe UI", 8),
        ).grid(row=4, column=0, sticky="ew", pady=(0, 9))

    def _installed_models(self) -> list[str]:
        try:
            result = subprocess.run(["ollama", "list"], capture_output=True, text=True, timeout=4, check=False)
            names = [line.split()[0] for line in result.stdout.splitlines()[1:] if line.split()]
            if names:
                return names
        except (OSError, subprocess.SubprocessError):
            pass
        return [core.OLLAMA_MODEL]

    def _on_model_changed(self, _event=None) -> None:
        model = self.model_var.get().strip()
        if model:
            core.OLLAMA_MODEL = model
            self.settings = save_settings(model=model)
            self.status_var.set(f"Модель выбрана: {model}")

    def _side_button(self, parent, text: str, command, accent: bool = False) -> tk.Button:
        bg = "#0c2638" if accent else COLORS["sidebar"]
        fg = COLORS["cyan"] if accent else COLORS["text"]
        button = tk.Button(
            parent,
            text=text,
            command=command,
            anchor="w",
            bg=bg,
            fg=fg,
            activebackground=COLORS["panel_alt"],
            activeforeground="white",
            relief="flat",
            padx=15,
            pady=7,
            font=("Segoe UI", 9),
            cursor="hand2",
        )
        button.pack(fill="x", padx=8, pady=2)
        return button

    def _on_chat_inner_configure(self, _event=None) -> None:
        self.chat_canvas.configure(scrollregion=self.chat_canvas.bbox("all"))

    def _on_chat_canvas_configure(self, event) -> None:
        self.chat_canvas.itemconfigure(self.chat_window, width=event.width)

    def _on_mousewheel(self, event) -> None:
        try:
            steps = int(-1 * (event.delta / 120))
            if str(event.widget).startswith(str(self.sidebar_canvas)):
                self.sidebar_canvas.yview_scroll(steps, "units")
            else:
                self.chat_canvas.yview_scroll(steps, "units")
        except tk.TclError:
            pass

    def _scroll_to_bottom(self) -> None:
        self.root.after_idle(lambda: self.chat_canvas.yview_moveto(1.0))

    def _create_message_bubble(self, role: str, text: str):
        is_user = role == "Ты"
        is_system = role == "Система"
        row = tk.Frame(self.chat_inner, bg=COLORS["background"])
        row.pack(fill="x", padx=16, pady=7)
        bubble_color = COLORS["user_bubble"] if is_user else COLORS["panel"]
        bubble = tk.Frame(row, bg=bubble_color, padx=14, pady=10, highlightthickness=1, highlightbackground="#1b3248" if not is_user else "#155071")
        bubble.pack(side="right" if is_user else "left", anchor="e" if is_user else "w")
        header = tk.Frame(bubble, bg=bubble_color)
        header.pack(fill="x", anchor="w")
        name_color = COLORS["cyan"] if is_user else COLORS["amber"] if is_system else COLORS["green"]
        tk.Label(header, text=role, bg=bubble_color, fg=name_color, font=("Segoe UI", 9, "bold")).pack(side="left")
        tk.Label(header, text=dt.datetime.now().strftime("%H:%M"), bg=bubble_color, fg="#64748b", font=("Segoe UI", 8)).pack(side="right", padx=(12, 0))
        wrap = max(260, min(710, int(max(self.chat_canvas.winfo_width(), 400) * 0.72)))
        body = tk.Label(
            bubble,
            text=text,
            bg=bubble_color,
            fg=COLORS["text"],
            justify="left",
            anchor="w",
            wraplength=wrap,
            font=("Segoe UI", 10),
        )
        body.pack(fill="x", anchor="w", pady=(5, 0))
        self._scroll_to_bottom()
        return body

    def _history_enabled(self) -> bool:
        return self.settings.get("save_chat_history", "false").casefold() == "true"

    def _record_chat_message(self, role: str, text: str) -> None:
        if self._history_enabled() and role in {"Ты", "Джарвис"}:
            try:
                save_message(self.history_id, role, text)
            except Exception as exc:
                self.status_var.set(f"Не удалось сохранить историю: {exc}")

    def _append_message(self, role: str, text: str) -> None:
        self._create_message_bubble(role, text)
        self._record_chat_message(role, text)

    def _send_from_keyboard(self, _event) -> str:
        self.send_message("auto")
        return "break"

    def _reply_in_chat(self, text: str) -> None:
        self.last_answer = text
        self._append_message("Джарвис", text)
        if self.hologram:
            self.hologram.set_text(text)

    def _handle_chat_ui_command(self, text: str) -> bool:
        n = core.normalize(text)

        if n in {"включи локальную историю", "включи историю чатов", "сохраняй историю чата"}:
            self.settings = save_settings(save_chat_history="true")
            self._reply_in_chat("Локальная история включена. Переписка сохраняется только на этом компьютере; отключи её командой «Выключи локальную историю».")
            return True
        if n in {"выключи локальную историю", "выключи историю чатов", "не сохраняй историю"}:
            self.settings = save_settings(save_chat_history="false")
            self._reply_in_chat("Сохранение новых сообщений в локальную историю выключено. Уже сохранённые записи не удалены.")
            return True
        if n.startswith("найди в истории "):
            query = text.strip()[len("найди в истории "):].strip()
            matches = search_messages(query, limit=8)
            if not matches:
                self._reply_in_chat("В сохранённой локальной истории совпадений нет. История сохраняется только после явного включения.")
            else:
                lines = [f"• {created[:16].replace('T', ' ')} · {role}: {' '.join(body.split())[:220]}" for _mid, _cid, _title, role, body, created in matches]
                self._reply_in_chat("Нашёл в локальной истории:\n" + "\n".join(lines))
            return True
        if n in {"открой историю чатов", "покажи историю чатов", "управляй историей чатов", "история чатов"}:
            self._append_message("Джарвис", "Открыл локальную историю чатов.")
            self.show_history()
            return True
        if n in {"покажи закладки", "покажи избранные ответы", "открой избранное"}:
            self._append_message("Джарвис", "Открыл сохранённые ответы.")
            self.show_bookmarks()
            return True
        if n in {"закрепи последний ответ", "сохрани последний ответ в избранное", "добавь ответ в закладки"}:
            if self.last_answer:
                add_bookmark(self.history_id, "Джарвис", self.last_answer)
                self._reply_in_chat("Сохранил последний ответ в локальные закладки.")
            else:
                self._reply_in_chat("Пока нет ответа, который можно сохранить.")
            return True
        if n in {"очисти историю чатов", "удали историю чатов"}:
            if messagebox.askyesno("Удалить историю чатов", "Удалить локально сохранённые сообщения? Избранные ответы останутся.", parent=self.root):
                count = clear_history()
                self.history_id = new_conversation_id()
                self._append_message("Система", f"Удалил сохранённые сообщения: {count}. Избранные ответы не затронуты.")
            else:
                self._reply_in_chat("Историю не удалял.")
            return True

        clipboard_instruction = None
        if any(phrase in n for phrase in ("переведи текст из буфера", "переведи буфер обмена")):
            target_language = next((language for phrase, language in (("на английский", "английский"), ("на грузинский", "грузинский"), ("на русский", "русский"), ("на французский", "французский"), ("на немецкий", "немецкий"), ("на испанский", "испанский")) if phrase in n), "русский")
            clipboard_instruction = f"Переведи текст из буфера обмена на {target_language}. Сохрани смысл и структуру."
        elif any(phrase in n for phrase in ("суммируй буфер", "кратко изложи текст из буфера", "объясни текст из буфера")):
            clipboard_instruction = "Кратко и понятно объясни текст из буфера обмена."
        elif any(phrase in n for phrase in ("отформатируй текст из буфера", "приведи буфер в порядок")):
            clipboard_instruction = "Аккуратно отформатируй текст из буфера обмена, сохраняя его смысл."
        if clipboard_instruction:
            try:
                clipboard_text = self.root.clipboard_get().strip()
            except tk.TclError:
                clipboard_text = ""
            if not clipboard_text:
                self._reply_in_chat("Буфер обмена пуст или недоступен.")
            else:
                if len(clipboard_text) > 20_000:
                    clipboard_text = clipboard_text[:20_000] + "\n… (текст сокращён до 20 000 символов)"
                self.request_queue.put(("user", clipboard_instruction + "\n\n" + clipboard_text, "local_only"))
                self.status_var.set("Обрабатываю текст из буфера локально…")
            return True

        if any(phrase in n for phrase in ("сравни два документа", "сравни документы", "сравнить документы", "сравни версии файлов")):
            self._start_document_comparison()
            return True

        if "последний ответ" in n and any(phrase in n for phrase in ("аудио", "голосом", "wav", "mp3", "озвуч")):
            self._export_last_answer_audio()
            return True
        if any(phrase in n for phrase in ("сохрани последний ответ", "экспортируй последний ответ", "сохрани ответ в файл")):
            self._export_last_answer_document()
            return True

        if n in {"добавь документ",  "добавить документ", "добавь документы", "загрузи документ", "загрузить документ", "добавь файл", "добавить файл"} or n.startswith(("добавь документ ", "загрузи документ ", "добавь файл ")):
            self._append_message("Джарвис", "Открыл выбор файлов. Выбери документы для локальной базы знаний.")
            self.add_documents()
            return True

        if n in {"управляй документами", "управление документами", "открой базу знаний", "открой управление документами"}:
            self._append_message("Джарвис", "Открыл менеджер локальных документов.")
            self.show_documents()
            return True

        if n in {"управляй памятью", "управление памятью", "открой управление памятью", "настрой память"}:
            self._append_message("Джарвис", "Открыл управление локальной памятью.")
            self.show_memory()
            return True

        if n in {"настрой интеграции", "покажи интеграции", "управляй интеграциями", "настрой календарь и умный дом"}:
            self._append_message("Джарвис", "Открываю локальные инструкции и статус интеграций.")
            self.show_integrations()
            return True

        if n in {"новый диалог", "начни новый диалог", "очисти чат", "очисти историю диалога"}:
            self.clear_chat()
            return True

        if any(phrase in n for phrase in ("скрой голограмму",  "закрой голограмму", "выключи голограмму", "скрой hud", "закрой hud")):
            if self.hologram is not None:
                self.toggle_hologram()
            self._reply_in_chat("Экранный HUD скрыт.")
            return True
        if any(phrase in n for phrase in ("покажи голограмму", "открой голограмму", "включи голограмму", "покажи hud", "включи hud")):
            if self.hologram is None:
                self.toggle_hologram()
            self._reply_in_chat("Экранный HUD открыт." if self.hologram is not None else "Не удалось открыть экранный HUD.")
            return True

        mode_intent = any(phrase in n for phrase in ("режим микрофона", "режим голоса", "режим по имени", "режим кнопоч", "push to talk", "режим push", "режим по кнопке"))
        if mode_intent:
            if any(phrase in n for phrase in ("push to talk", "режим push", "зажим", "удерживай")):
                selected_mode = "ptt"
            elif any(phrase in n for phrase in ("кнопк", "по кнопке", "непрерывн")):
                selected_mode = "toggle"
            elif any(phrase in n for phrase in ("по имени", "по слову", "wake word", "активац", "джарвис")):
                selected_mode = "wake"
            else:
                self._reply_in_chat("Назови режим: по имени «Джарвис», кнопочный или push-to-talk.")
                return True
            self.voice_mode_var.set(VOICE_MODE_LABELS[selected_mode])
            self._on_voice_mode_changed()
            self._reply_in_chat(f"Режим микрофона изменён: {VOICE_MODE_LABELS[selected_mode]}.")
            return True

        if any(phrase in n for phrase in ("выключи микрофон", "останови микрофон", "отключи микрофон", "прекрати слушать")):
            self.voice_active.clear()
            self.ptt_held.clear()
            self._configure_mic_button()
            self.status_var.set("Микрофон выключен")
            self._set_hologram_state("idle")
            self._reply_in_chat("Микрофон выключен.")
            return True
        if n in {"включи микрофон", "активируй микрофон", "начни слушать", "начни слушать меня"}:
            if self.settings.get("voice_mode") == "ptt":
                self._reply_in_chat("Сейчас выбран push-to-talk: зажми кнопку микрофона, чтобы начать говорить.")
            else:
                if not self.voice_active.is_set():
                    self.toggle_microphone()
                self._reply_in_chat("Микрофон включён. Для остановки скажи «выключи микрофон» или нажми кнопку.")
            return True

        if any(phrase in n for phrase in ("останови озвучивание", "останови речь", "замолчи")):
            self.stop_current_speech()
            self._reply_in_chat("Озвучивание остановлено.")
            return True

        model_prefixes = ("выбери модель", "переключи модель", "используй модель", "поставь модель")
        if n.startswith(model_prefixes):
            raw = text.casefold()
            values = self.model_combo["values"]
            installed = [str(values)] if isinstance(values, str) else [str(item) for item in values]
            selected = next((item for item in installed if item.casefold() in raw), None)
            if selected is None:
                self._reply_in_chat("Не нашёл такую установленную Ollama-модель. Выбери одну из списка в левой панели.")
            else:
                self.model_var.set(selected)
                self._on_model_changed()
                self._reply_in_chat(f"Выбрал модель {selected}.")
            return True

        return False

    def _chat_request_mode(self, text: str, requested_mode: str) -> tuple[str, str]:
        if requested_mode != "auto":
            return text, requested_mode
        raw = text.strip()
        for prefix, mode in (("/deep ", "deep_web"), ("/web ", "web"), ("/google ", "google")):
            if raw.casefold().startswith(prefix):
                query = raw[len(prefix):].strip()
                return (query or raw), mode
        normalized = core.normalize(raw)
        deep_phrases = (
            "подробно изучи", "подробно исследуй", "глубокий поиск", "глубоко исследуй",
            "сравни несколько источников", "сравни источники", "изучи несколько сайтов", "обзор источников",
        )
        if any(phrase in normalized for phrase in deep_phrases):
            for prefix in ("подробно изучи несколько сайтов", "подробно изучи сайты", "подробно изучи", "подробно исследуй", "глубоко исследуй", "изучи несколько сайтов", "сравни несколько источников", "сравни источники"):
                if raw.casefold().startswith(prefix):
                    query = raw[len(prefix):].strip(" :,-")
                    query = re.sub(r"^(?:про|о|об)\s+", "", query, flags=re.IGNORECASE).strip()
                    raw = query or raw
                    break
            return raw, "deep_web"
        return raw, requested_mode

    def send_message(self, mode: str = "auto", preset: str | None = None) -> None:
        text = preset if preset is not None else self.input_box.get("1.0", "end").strip()
        if not text.strip():
            self.status_var.set("Сначала введи вопрос или запрос для поиска")
            return
        if preset is None:
            self.input_box.delete("1.0", "end")
        text = text.strip()
        self._append_message("Ты", text)
        if self._handle_chat_ui_command(text):
            return
        request_text, mode = self._chat_request_mode(text, mode)
        self.request_queue.put(("user", request_text, mode))
        self.status_var.set("Ищу источники…" if mode in {"web", "deep_web"} else "Открываю Google…" if mode == "google" else "Обрабатываю запрос…")
        if self.hologram:
            self.hologram.set_state("thinking")
            self.hologram.set_text("Получил запрос. Начинаю обработку…")

    def send_preset(self, text: str) -> None:
        self.send_message("auto", text)

    def search_from_input(self) -> None:
        self.send_message("web")

    def deep_search_from_input(self) -> None:
        self.send_message("deep_web")

    def _start_document_comparison(self) -> None:
        paths = filedialog.askopenfilenames(
            parent=self.root,
            title="Выбери ровно два документа для локального сравнения",
            filetypes=[("Поддерживаемые документы", "*.txt *.md *.pdf *.docx *.html *.htm"), ("Все файлы", "*.*")],
        )
        if not paths:
            return
        if len(paths) != 2:
            self._reply_in_chat("Для сравнения нужно выбрать ровно два документа.")
            return
        self.status_var.set("Сравниваю документы локально…")
        threading.Thread(target=self._compare_documents_worker, args=(list(paths),), daemon=True, name="jarvis-doc-compare").start()

    def _compare_documents_worker(self, paths: list[str]) -> None:
        result = compare_documents(paths)
        self.ui_queue.put(("compare_done", result))

    def _export_last_answer_document(self) -> None:
        text = self.last_answer.strip()
        if not text:
            self._reply_in_chat("Сначала дождись ответа, который нужно сохранить.")
            return
        path = filedialog.asksaveasfilename(
            parent=self.root,
            title="Сохранить ответ локально",
            defaultextension=".docx",
            initialfile="jarvis_answer.docx",
            filetypes=[("Документ Word", "*.docx"), ("Текстовый файл", "*.txt")],
        )
        if not path:
            return
        try:
            destination = Path(path)
            if destination.suffix.casefold() == ".docx":
                from docx import Document
                document = Document()
                for line in text.splitlines() or [text]:
                    document.add_paragraph(line)
                document.save(str(destination))
            else:
                destination.write_text(text, encoding="utf-8")
            self._reply_in_chat(f"Сохранил последний ответ локально: {destination.name}")
        except Exception as exc:
            self._reply_in_chat(f"Не удалось сохранить документ: {exc}")

    def _export_last_answer_audio(self) -> None:
        text = self.last_answer.strip()
        if not text:
            self._reply_in_chat("Сначала дождись ответа, который нужно озвучить и сохранить.")
            return
        path = filedialog.asksaveasfilename(
            parent=self.root,
            title="Сохранить озвученный ответ",
            defaultextension=".wav",
            initialfile="jarvis_answer.wav",
            filetypes=[("Аудио WAV", "*.wav")],
        )
        if not path:
            return
        self.status_var.set("Создаю аудиофайл локально…")
        threading.Thread(target=self._tts_export_worker, args=(text, path), daemon=True, name="jarvis-tts-export").start()

    def _tts_export_worker(self, text: str, path: str) -> None:
        engine = None
        try:
            engine, _voice_found = create_tts_engine()
            engine.save_to_file(text, path)
            engine.runAndWait()
            engine.stop()
            self.ui_queue.put(("file_export_done", path))
        except Exception as exc:
            self.ui_queue.put(("file_export_error", str(exc)))
        finally:
            if engine is not None:
                try:
                    engine.stop()
                except Exception:
                    pass

    def add_documents(self) -> None:
        if self.voice_active.is_set():

            self.voice_active.clear()
            self.ptt_held.clear()
            self._configure_mic_button()
        paths = filedialog.askopenfilenames(
            parent=self.root,
            title="Выбери документы для локальной базы знаний",
            filetypes=[
                ("Поддерживаемые документы", "*.txt *.md *.pdf *.docx *.html *.htm"),
                ("Все файлы", "*.*"),
            ],
        )
        if not paths:
            return
        self.status_var.set("Индексирую выбранные файлы локально…")
        self.ui_queue.put(("state", "thinking"))
        threading.Thread(target=self._index_worker, args=(list(paths),), daemon=True, name="jarvis-indexer").start()

    def _index_worker(self, paths: list[str]) -> None:
        indexed, errors = index_documents(paths)
        message = f"Добавлено/обновлено документов: {indexed}."
        if errors:
            message += "\n\nНе удалось обработать:\n" + "\n".join(errors[:6])
        self.ui_queue.put(("index_done", message))

    def show_history(self) -> None:
        window = tk.Toplevel(self.root)
        window.title("Локальная история чатов")
        window.geometry("820x520")
        window.configure(bg=COLORS["background"])
        window.transient(self.root)
        state = "включена" if self._history_enabled() else "выключена; новые сообщения не сохраняются"
        tk.Label(window, text=f"История хранится в локальном файле на этом ПК. Сейчас запись {state}.", bg=COLORS["background"], fg=COLORS["muted"], anchor="w", padx=16, pady=10).pack(fill="x")
        controls = tk.Frame(window, bg=COLORS["background"])
        controls.pack(fill="x", padx=16, pady=(0, 8))
        query_var = tk.StringVar()
        query_entry = tk.Entry(controls, textvariable=query_var, bg=COLORS["panel"], fg=COLORS["text"], insertbackground="white", relief="flat")
        query_entry.pack(side="left", fill="x", expand=True, ipady=7)
        listbox = tk.Listbox(window, bg=COLORS["panel"], fg=COLORS["text"], selectbackground="#155e75", relief="flat", font=("Segoe UI", 9))
        listbox.pack(fill="both", expand=True, padx=16, pady=6)
        rows: list[tuple[int, str, str, str, str, str]] = []

        def refresh(_event=None) -> None:
            listbox.delete(0, "end")
            rows.clear()
            rows.extend(search_messages(query_var.get(), limit=200))
            for _mid, _cid, title, role, body, created in rows:
                snippet = " ".join(body.split())[:150]
                listbox.insert("end", f"{created[:16].replace('T', ' ')} · {title} · {role}: {snippet}")
            if not rows:
                listbox.insert("end", "Совпадений нет. Включи сохранение командой «Включи локальную историю».")

        def show_selected() -> None:
            selected = listbox.curselection()
            if selected and selected[0] < len(rows):
                _mid, _cid, title, role, body, created = rows[selected[0]]
                messagebox.showinfo(f"{title} · {created[:16]}", f"{role}:\n\n{body}", parent=window)

        def insert_selected() -> None:
            selected = listbox.curselection()
            if selected and selected[0] < len(rows):
                body = rows[selected[0]][4]
                self.input_box.insert("end", body)
                self.root.focus_force()
                window.destroy()

        def delete_all() -> None:
            if messagebox.askyesno("Очистить историю", "Удалить сохранённую историю сообщений? Избранные ответы останутся.", parent=window):
                clear_history()
                refresh()

        query_entry.bind("<Return>", refresh)
        tk.Button(controls, text="Найти", command=refresh, bg=COLORS["panel_alt"], fg=COLORS["text"], relief="flat", padx=12).pack(side="left", padx=(8, 0))
        bottom = tk.Frame(window, bg=COLORS["background"])
        bottom.pack(fill="x", padx=16, pady=(4, 14))
        tk.Button(bottom, text="Открыть запись", command=show_selected, bg=COLORS["panel_alt"], fg=COLORS["text"], relief="flat", padx=10, pady=7).pack(side="left")
        tk.Button(bottom, text="Вставить в чат", command=insert_selected, bg=COLORS["panel_alt"], fg=COLORS["text"], relief="flat", padx=10, pady=7).pack(side="left", padx=8)
        tk.Button(bottom, text="Удалить историю", command=delete_all, bg="#51232b", fg="white", relief="flat", padx=10, pady=7).pack(side="right")
        listbox.bind("<Double-Button-1>", lambda _event: show_selected())
        refresh()
        query_entry.focus_set()

    def show_bookmarks(self) -> None:
        window = tk.Toplevel(self.root)
        window.title("Избранные ответы")
        window.geometry("760x470")
        window.configure(bg=COLORS["background"])
        window.transient(self.root)
        tk.Label(window, text="Ответы, которые ты явно сохранил. Данные остаются локальными.", bg=COLORS["background"], fg=COLORS["muted"], anchor="w", padx=16, pady=10).pack(fill="x")
        listbox = tk.Listbox(window, bg=COLORS["panel"], fg=COLORS["text"], selectbackground="#155e75", relief="flat", font=("Segoe UI", 9))
        listbox.pack(fill="both", expand=True, padx=16, pady=8)
        items: list[tuple[int, str, str, str]] = []

        def refresh() -> None:
            listbox.delete(0, "end")
            items.clear()
            items.extend(list_bookmarks())
            for _bid, role, body, created in items:
                listbox.insert("end", f"{created[:16].replace('T', ' ')} · {role}: {' '.join(body.split())[:180]}")
            if not items:
                listbox.insert("end", "Пока ничего не сохранено. Скажи: «Закрепи последний ответ».")

        def selected_item():
            selected = listbox.curselection()
            return items[selected[0]] if selected and selected[0] < len(items) else None

        def copy_selected() -> None:
            item = selected_item()
            if item:
                self.root.clipboard_clear()
                self.root.clipboard_append(item[2])
                self.status_var.set("Ответ скопирован из закладок")

        def insert_selected() -> None:
            item = selected_item()
            if item:
                self.input_box.insert("end", item[2])
                self.root.focus_force()
                window.destroy()

        def delete_selected() -> None:
            item = selected_item()
            if item:
                delete_bookmark(item[0])
                refresh()

        bottom = tk.Frame(window, bg=COLORS["background"])
        bottom.pack(fill="x", padx=16, pady=(4, 14))
        tk.Button(bottom, text="Скопировать", command=copy_selected, bg=COLORS["panel_alt"], fg=COLORS["text"], relief="flat", padx=10, pady=7).pack(side="left")
        tk.Button(bottom, text="Вставить в чат", command=insert_selected, bg=COLORS["panel_alt"], fg=COLORS["text"], relief="flat", padx=10, pady=7).pack(side="left", padx=8)
        tk.Button(bottom, text="Удалить выбранное", command=delete_selected, bg="#51232b", fg="white", relief="flat", padx=10, pady=7).pack(side="right")
        listbox.bind("<Double-Button-1>", lambda _event: copy_selected())
        refresh()

    def show_documents(self) -> None:
        window = tk.Toplevel(self.root)
        window.title("Локальная база знаний")
        window.geometry("700x460")
        window.configure(bg=COLORS["background"])
        window.transient(self.root)
        tk.Label(
            window,
            text="В базе хранится извлечённый текст для локального поиска. Удаление записи не удаляет исходный файл.",
            bg=COLORS["background"],
            fg=COLORS["muted"],
            wraplength=660,
            justify="left",
            font=("Segoe UI", 9),
        ).pack(anchor="w", padx=16, pady=(15, 8))
        listbox = tk.Listbox(
            window,
            bg=COLORS["panel"],
            fg=COLORS["text"],
            selectbackground="#155e75",
            relief="flat",
            font=("Segoe UI", 9),
        )
        listbox.pack(fill="both", expand=True, padx=16, pady=8)
        paths: list[str] = []

        def refresh() -> None:
            listbox.delete(0, "end")
            paths.clear()
            for path, title, _mtime in list_indexed_documents():
                paths.append(path)
                listbox.insert("end", f"{title}  ·  {path}")
            if not paths:
                listbox.insert("end", "База знаний пуста. Добавь TXT, MD, PDF, DOCX или HTML.")

        def delete_selected() -> None:
            selected = listbox.curselection()
            if selected and selected[0] < len(paths):
                delete_indexed_document(paths[selected[0]])
                refresh()

        def delete_all() -> None:
            if messagebox.askyesno("Очистить базу знаний", "Удалить все индексированные документы из локальной базы? Исходные файлы останутся.", parent=window):
                clear_indexed_documents()
                refresh()

        controls = tk.Frame(window, bg=COLORS["background"])
        controls.pack(fill="x", padx=16, pady=(4, 14))
        tk.Button(controls, text="Удалить выбранное", command=delete_selected, bg=COLORS["panel_alt"], fg=COLORS["text"], relief="flat", padx=10, pady=7).pack(side="left")
        tk.Button(controls, text="Удалить всё", command=delete_all, bg="#51232b", fg="white", relief="flat", padx=10, pady=7).pack(side="right")
        refresh()

    def show_memory(self) -> None:
        window = tk.Toplevel(self.root)
        window.title("Память Джарвиса")
        window.geometry("540x430")
        window.configure(bg=COLORS["background"])
        window.transient(self.root)
        tk.Label(
            window,
            text="Локальная память — сохраняются только факты, которые ты явно попросил запомнить.",
            bg=COLORS["background"],
            fg=COLORS["muted"],
            wraplength=490,
            justify="left",
            font=("Segoe UI", 9),
        ).pack(anchor="w", padx=16, pady=(15, 8))
        listbox = tk.Listbox(
            window,
            bg=COLORS["panel"],
            fg=COLORS["text"],
            selectbackground="#155e75",
            relief="flat",
            font=("Segoe UI", 10),
        )
        listbox.pack(fill="both", expand=True, padx=16, pady=8)
        ids: list[int] = []

        def refresh() -> None:
            listbox.delete(0, "end")
            ids.clear()
            for memory_id, fact, created_at in list_memories():
                ids.append(memory_id)
                listbox.insert("end", f"{created_at[:16].replace('T', ' ')}  ·  {fact}")
            if not ids:
                listbox.insert("end", "Память пока пуста. Скажи: «Запомни, что …»")

        def delete_selected() -> None:
            selected = listbox.curselection()
            if selected and selected[0] < len(ids):
                delete_memory(ids[selected[0]])
                refresh()

        def delete_all() -> None:
            if messagebox.askyesno("Очистить память", "Удалить все сохранённые факты?", parent=window):
                forget_memories()
                refresh()

        controls = tk.Frame(window, bg=COLORS["background"])
        controls.pack(fill="x", padx=16, pady=(4, 14))
        tk.Button(controls, text="Удалить выбранное", command=delete_selected, bg=COLORS["panel_alt"], fg=COLORS["text"], relief="flat", padx=10, pady=7).pack(side="left")
        tk.Button(controls, text="Удалить всё", command=delete_all, bg="#51232b", fg="white", relief="flat", padx=10, pady=7).pack(side="right")
        refresh()

    def google_from_input(self) -> None:
        query = self.input_box.get("1.0", "end").strip()
        if query:
            self.send_message("google")
        else:
            self.send_preset("Открой Google")

    def ask_tbilisi_weather(self) -> None:
        self.send_preset("Какая сегодня погода в Тбилиси и будет ли дождь?")

    def ask_calendar(self) -> None:
        self.send_preset("Какие события у меня сегодня в календаре?")

    def ask_homeassistant(self) -> None:
        self.send_preset("Покажи состояние умного дома")

    def show_integrations(self) -> None:
        ha_ready = bool(os.getenv("JARVIS_HA_URL") and os.getenv("JARVIS_HA_TOKEN"))
        ha_allowlist = bool(os.getenv("JARVIS_HA_ALLOWED_ENTITIES"))
        google_ready = (BASE_DIR / "credentials.json").exists() or (BASE_DIR / "calendar_token.json").exists()
        message = (
            "Home Assistant: " + ("переменные заданы" if ha_ready else "не настроен") + "\n"
            "Разрешённые устройства: " + ("заданы" if ha_allowlist else "не заданы — управление выключено") + "\n\n"
            "Google Calendar: " + ("локальные OAuth-данные найдены" if google_ready else "нет локального credentials.json") + "\n\n"
            "Настрой Home Assistant в PowerShell перед запуском:\n"
            '$env:JARVIS_HA_URL = "http://адрес-home-assistant:8123"\n'
            '$env:JARVIS_HA_TOKEN = "токен-вставлять-только-на-своём-ПК"\n'
            '$env:JARVIS_HA_ALLOWED_ENTITIES = "light.kitchen,switch.office"\n\n'
            "Для Google Calendar положи OAuth-файл credentials.json рядом с jarvis.py. "
            "Токен OAuth хранится локально в calendar_token.json. Не отправляй эти файлы другим людям."
        )
        messagebox.showinfo("Интеграции Джарвиса", message, parent=self.root)

    def clear_chat(self) -> None:
        for child in self.chat_inner.winfo_children():
            child.destroy()
        self.stream_body = None
        self.stream_text = ""
        self.history_id = new_conversation_id()
        self.last_answer = ""
        self.request_queue.put(("clear", "", "auto"))
        self._append_message("Система", "Новый диалог. История беседы очищена.")
        self.status_var.set("Готов к работе")

    def toggle_hologram(self) -> None:
        if self.hologram is not None:
            self.hologram.close()
            self.holo_button.configure(text="◇   Показать голограмму")
            return
        try:
            self.hologram = HologramHUD(self)
            self.holo_button.configure(text="◇   Скрыть голограмму")
            self.hologram.set_state("idle")
        except tk.TclError as exc:
            self.status_var.set(f"Не удалось открыть HUD: {exc}")

    def _set_hologram_state(self, state: str) -> None:
        if self.hologram is not None:
            self.hologram.set_state(state)

    def stop_current_speech(self) -> None:
        self.stop_speaking.set()
        self.status_var.set("Остановлю озвучивание после текущей фразы")

    def _on_voice_mode_changed(self, _event=None) -> None:
        if self.voice_active.is_set():
            self.voice_active.clear()
        self.ptt_held.clear()
        mode = VOICE_LABEL_MODES.get(self.voice_mode_var.get(), "wake")
        self.settings = save_settings(voice_mode=mode)
        self._configure_mic_button()
        self.status_var.set(f"Режим микрофона: {VOICE_MODE_LABELS[mode]}")

    def _configure_mic_button(self) -> None:
        self.mic_button.unbind("<ButtonPress-1>")
        self.mic_button.unbind("<ButtonRelease-1>")
        mode = self.settings.get("voice_mode", "wake")
        if mode == "ptt":
            self.mic_button.configure(command=lambda: None, text="🎙   Зажать для разговора")
            self.mic_button.bind("<ButtonPress-1>", self._ptt_start)
            self.mic_button.bind("<ButtonRelease-1>", self._ptt_end)
        else:
            if self.voice_active.is_set():
                label = "■   Выключить микрофон"
            else:
                label = "◉   Включить микрофон"
            self.mic_button.configure(command=self.toggle_microphone, text=label)

    def _start_voice_worker(self) -> None:
        if self.voice_active.is_set():
            return
        if self.voice_thread is not None and self.voice_thread.is_alive():
            self.status_var.set("Микрофон останавливается; подожди и нажми ещё раз")
            return
        self.voice_active.set()
        self._configure_mic_button()
        self.status_var.set("Загружаю локальное распознавание речи…")
        self._set_hologram_state("listening")
        self.voice_thread = threading.Thread(target=self._voice_worker, daemon=True, name="jarvis-microphone")
        self.voice_thread.start()

    def toggle_microphone(self) -> None:
        if self.settings.get("voice_mode") == "ptt":
            return
        if self.voice_active.is_set():
            self.voice_active.clear()
            self._configure_mic_button()
            self.status_var.set("Микрофон выключен")
            self._set_hologram_state("idle")
            return
        self._start_voice_worker()

    def _ptt_start(self, _event=None):
        # Allow Tk's class binding to grab the pointer, so release is observed even if the cursor moves away.
        self.ptt_held.set()
        self._start_voice_worker()
        return None

    def _ptt_end(self, _event=None):
        self.ptt_held.clear()
        self.status_var.set("Push-to-talk: обрабатываю конец фразы…")
        return None

    def _get_vosk_model(self):
        with self.vosk_lock:
            if self.vosk_model is None:
                model_dir = find_vosk_model_dir()
                if model_dir is None:
                    raise FileNotFoundError("Не найдена модель Vosk. Проверь папки am, conf и graph.")
                self.vosk_model = Model(str(model_dir))
            return self.vosk_model

    def _voice_worker(self) -> None:
        mode = self.settings.get("voice_mode", "wake")
        try:
            recognizer = KaldiRecognizer(self._get_vosk_model(), SAMPLE_RATE)
            if not self.voice_active.is_set() or self.closing.is_set() or (mode == "ptt" and not self.ptt_held.is_set()):
                return
            audio_queue: queue.Queue[bytes] = queue.Queue(maxsize=20)
            enqueue_audio = make_audio_callback(audio_queue)

            def microphone_callback(indata, frames, time_info, status):
                if mode == "ptt" and not self.ptt_held.is_set():
                    return
                enqueue_audio(indata, frames, time_info, status)
                try:
                    samples = array.array("h")
                    samples.frombytes(bytes(indata))
                    if samples:
                        rms = math.sqrt(sum(sample * sample for sample in samples) / len(samples))
                        self.ui_queue.put(("audio_level", min(1.0, rms / 7000.0)))
                except Exception:
                    pass

            with sd.RawInputStream(
                samplerate=SAMPLE_RATE,
                blocksize=8_000,
                dtype="int16",
                channels=1,
                callback=microphone_callback,
            ):
                status = "Скажи «Джарвис, …»" if mode == "wake" else "Слушаю любую фразу…"
                self.ui_queue.put(("status", status))
                self.ui_queue.put(("state", "listening"))
                release_deadline = None
                post_release_silence_blocks = 0
                while self.voice_active.is_set() and not self.closing.is_set():
                    if mode == "ptt":
                        if self.ptt_held.is_set():
                            release_deadline = None
                            post_release_silence_blocks = 0
                        elif release_deadline is None:
                            release_deadline = time.monotonic() + 0.8
                        elif time.monotonic() >= release_deadline:
                            break
                    if self.speaking.is_set():
                        clear_audio_queue(audio_queue)
                        recognizer.Reset()
                        time.sleep(0.1)
                        continue
                    try:
                        audio = audio_queue.get(timeout=0.25)
                    except queue.Empty:
                        if mode == "ptt" and not self.ptt_held.is_set():
                            if post_release_silence_blocks >= 2:
                                break
                            audio = bytes(16_000)  # synthetic silence; do not capture after the button is released
                            post_release_silence_blocks += 1
                        else:
                            continue
                    if not recognizer.AcceptWaveform(audio):
                        continue
                    phrase = json.loads(recognizer.Result()).get("text", "").strip()
                    if not phrase:
                        continue
                    if mode == "wake":
                        command = extract_command_after_wake_word(phrase)
                        if command is None:
                            continue
                        if not command:
                            self.request_queue.put(("direct", "Я слушаю. Скажи вопрос или команду.", "auto"))
                            continue
                    else:
                        command = phrase
                    self.ui_queue.put(("voice_command", command))
                    self.ui_queue.put(("state", "thinking"))
        except Exception as exc:
            self.ui_queue.put(("voice_error", str(exc)))
        finally:
            self.voice_active.clear()
            self.ptt_held.clear()
            self.ui_queue.put(("voice_stopped", None))

    def _assistant_worker(self) -> None:
        try:
            tts_engine, _russian_voice_found = create_tts_engine()
        except Exception as exc:
            tts_engine = None
            self.ui_queue.put(("status", f"Озвучка недоступна: {exc}"))

        history: list[dict[str, str]] = []
        while not self.closing.is_set():
            try:
                kind, text, mode = self.request_queue.get(timeout=0.25)
            except queue.Empty:
                continue
            if kind == "stop":
                break
            if kind == "clear":
                history.clear()
                continue
            self.stop_speaking.clear()
            self.speaking.set()  # на время обработки и озвучивания не распознаём эхо микрофона
            try:
                if kind == "direct":
                    answer = text
                else:
                    self.ui_queue.put(("state", "thinking"))

                    def on_chunk(piece: str) -> None:
                        self.ui_queue.put(("stream_chunk", piece))

                    try:
                        answer = handle_request(history, text, mode=mode, on_chunk=on_chunk)
                    except Exception as exc:
                        answer = f"Не удалось обработать запрос: {exc}"

                self.ui_queue.put(("answer", answer))
                # Обновление HUD выполняется только главным Tk-потоком.
                self.ui_queue.put(("holo_text", answer))
                if tts_engine is not None:
                    self.ui_queue.put(("state", "speaking"))
                    self.ui_queue.put(("status", "Джарвис отвечает голосом…"))
                    try:
                        sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+|\n+", answer) if part.strip()]
                        if not sentences:
                            sentences = [answer]
                        for sentence in sentences:
                            if self.stop_speaking.is_set():
                                break
                            tts_engine.say(sentence)
                            tts_engine.runAndWait()
                    except Exception as exc:
                        self.ui_queue.put(("status", f"Не удалось озвучить ответ: {exc}"))
            finally:
                self.speaking.clear()

            self.ui_queue.put(("state", "listening" if self.voice_active.is_set() else "idle"))
            self.ui_queue.put(("status", "Слушаю: скажи «Джарвис, …»" if self.voice_active.is_set() else "Готов к работе"))

    def _poll_ui_events(self) -> None:
        if self.closing.is_set():
            return
        while True:
            try:
                kind, payload = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            if kind == "message":
                role, text = payload  # type: ignore[misc]
                self._append_message(role, text)
            elif kind == "voice_command":
                self.send_message("auto", str(payload))
            elif kind == "answer":
                answer = str(payload)
                self.last_answer = answer
                if self.stream_body is not None:
                    self.stream_body.configure(text=answer)
                    self.stream_body = None
                    self.stream_text = ""
                    self._scroll_to_bottom()
                    self._record_chat_message("Джарвис", answer)
                else:
                    self._append_message("Джарвис", answer)
                if self.hologram:
                    self.hologram.set_text(answer)
            elif kind == "stream_chunk":
                piece = str(payload)
                if self.stream_body is None:
                    self.stream_text = ""
                    self.stream_body = self._create_message_bubble("Джарвис", "")
                self.stream_text += piece
                self.stream_body.configure(text=self.stream_text)
                self._scroll_to_bottom()
                if self.hologram:
                    self.hologram.set_text(self.stream_text)
            elif kind == "holo_text":
                if self.hologram:
                    self.hologram.set_text(str(payload))
            elif kind == "audio_level":
                if self.hologram:
                    self.hologram.set_audio_level(float(payload))
            elif kind == "index_done":
                self._append_message("Система", str(payload))
                self.status_var.set("Индекс документов завершён")
                self._set_hologram_state("idle")
            elif kind == "compare_done":
                self._reply_in_chat(str(payload))
                self.status_var.set("Сравнение завершено")
            elif kind == "file_export_done":
                self._reply_in_chat(f"Аудиофайл сохранён локально: {Path(str(payload)).name}")
                self.status_var.set("Экспорт завершён")
            elif kind == "file_export_error":
                self._reply_in_chat(f"Не удалось создать аудио: {payload}")
                self.status_var.set("Ошибка экспорта аудио")
            elif kind == "status":
                self.status_var.set(str(payload))
            elif kind == "state":
                self._set_hologram_state(str(payload))
            elif kind == "voice_error":
                self.status_var.set("Микрофон или Vosk недоступны")
                self._append_message("Система", str(payload))
                self._set_hologram_state("idle")
            elif kind == "voice_stopped":
                self._configure_mic_button()
                if self.settings.get("voice_mode") == "ptt" and self.status_var.get() == "Push-to-talk: обрабатываю конец фразы…":
                    self.status_var.set("Push-to-talk: удерживай кнопку для разговора")
        self.root.after(60, self._poll_ui_events)

    def _check_reminders(self) -> None:
        if self.closing.is_set():
            return
        try:
            due = pop_due_reminders()
            for message in due:
                text = f"Напоминание: {message}"
                self._append_message("Система", text)
                self.root.bell()
                self.request_queue.put(("direct", text, "auto"))
        except Exception as exc:
            self.status_var.set(f"Ошибка таймера: {exc}")
        self.root.after(1000, self._check_reminders)

    def close(self) -> None:
        self.closing.set()
        self.voice_active.clear()
        self.ptt_held.clear()
        self.request_queue.put(("stop", "", "auto"))
        if self.hologram:
            self.hologram.close()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    JarvisGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
