import os
import sys
import re
import time
import html
import psutil
import json
import subprocess
import threading
import webbrowser
import queue
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime
from urllib.parse import quote_plus

import speech_recognition as sr
import pyttsx3

from PySide6.QtCore import Qt, QThread, Signal, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout,
    QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QTextEdit, QMessageBox, QFrame, QProgressBar
)


APP_NAME = "ZYPHERUS NEXUS"

NVIDIA_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

NVIDIA_MODEL = "nvidia/nemotron-3-ultra-550b-a55b"

OPENROUTER_ULTRA = os.getenv(
    "OPENROUTER_ULTRA_MODEL",
    "nvidia/nemotron-3-ultra-550b-a55b:free"
)

OPENROUTER_LIGHTNING = os.getenv(
    "OPENROUTER_LIGHTNING_MODEL",
    "nvidia/nemotron-3.5-lightning:free"
)


SYSTEM_PROMPT = """
You are ZYPHERUS, a helpful Windows desktop assistant.
Understand natural, conversational language, including polite requests,
short follow-ups, and voice-transcription quirks. Use the conversation
context when interpreting a request.
By default, answer in a short summary with the key result and actionable
next step. Expand with detail, examples, or step-by-step reasoning when
the user asks for more detail, an explanation, or a longer answer.
Be accurate, concise, and practical.
You cannot directly inspect the computer through an AI request.
Never claim to have performed a local action unless confirmed.
Local actions are handled by the desktop application's command router.
"""


# =========================================================
# APPLICATION DISCOVERY
# =========================================================

def start_menu_folders():
    folders = []

    appdata = os.getenv("APPDATA")
    programdata = os.getenv("PROGRAMDATA")

    if appdata:
        folders.append(
            Path(appdata) /
            "Microsoft/Windows/Start Menu/Programs"
        )

    if programdata:
        folders.append(
            Path(programdata) /
            "Microsoft/Windows/Start Menu/Programs"
        )

    return [folder for folder in folders if folder.is_dir()]


def discover_apps():
    apps = {}

    for folder in start_menu_folders():
        try:
            for path in folder.rglob("*"):
                if (
                    path.is_file()
                    and path.suffix.lower() in (
                        ".lnk", ".url", ".appref-ms"
                    )
                ):
                    apps.setdefault(path.stem.lower(), path)
        except (OSError, PermissionError):
            continue

    return apps


def launch_app(name):
    name = name.strip().lower()

    if not name:
        return False, "Tell me which application to open."

    aliases = {
        "notepad": ["notepad.exe"],
        "calculator": ["calc.exe"],
        "calc": ["calc.exe"],
        "paint": ["mspaint.exe"],
        "task manager": ["taskmgr.exe"],
        "command prompt": ["cmd.exe"],
        "cmd": ["cmd.exe"],
        "powershell": ["powershell.exe"],
        "file explorer": ["explorer.exe"],
        "explorer": ["explorer.exe"],
    }

    if name in aliases:
        try:
            subprocess.Popen(aliases[name])
            return True, f"Opened {name}."
        except OSError as exc:
            return False, f"Could not open {name}: {exc}"

    apps = discover_apps()
    shortcut = apps.get(name)

    if shortcut is None:
        matches = [
            (key, value)
            for key, value in apps.items()
            if name in key or key in name
        ]

        if len(matches) == 1:
            shortcut = matches[0][1]
        elif len(matches) > 1:
            choices = ", ".join(key for key, _ in matches[:8])
            return False, (
                "Several apps match. Be more specific: "
                + choices
            )

    if shortcut:
        try:
            os.startfile(str(shortcut))
            return True, f"Launched {shortcut.stem}."
        except OSError as exc:
            return False, f"Could not launch app: {exc}"

    return False, (
        f"Couldn't find '{name}' in the Start Menu. "
        "Try the installed application's name."
    )


def close_app(name):
    name = name.strip().lower()

    if not name:
        return False, "Tell me which application to close."

    protected = {
        "system", "registry", "smss", "csrss", "wininit",
        "winlogon", "services", "lsass", "svchost", "dwm",
        "windows", "all", "everything", "explorer",
    }

    if name in protected:
        return False, (
            "That target is protected or too broad. "
            "Close it manually through Windows if needed."
        )

    aliases = {
        "notepad": {"notepad.exe"},
        "calculator": {"calculator.exe", "calc.exe"},
        "chrome": {"chrome.exe"},
        "google chrome": {"chrome.exe"},
        "edge": {"msedge.exe"},
        "microsoft edge": {"msedge.exe"},
        "firefox": {"firefox.exe"},
        "vscode": {"code.exe"},
        "vs code": {"code.exe"},
        "visual studio code": {"code.exe"},
        "spotify": {"spotify.exe"},
        "discord": {"discord.exe"},
        "paint": {"mspaint.exe"},
    }

    targets = aliases.get(name)

    if targets is None:
        targets = set()

        for proc in psutil.process_iter(["name"]):
            try:
                proc_name = proc.info.get("name") or ""
                stem = Path(proc_name).stem.lower()

                if stem == name or stem == name.replace(" ", ""):
                    targets.add(proc_name.lower())

            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

    forbidden = {
        "system", "registry", "smss.exe", "csrss.exe",
        "wininit.exe", "winlogon.exe", "services.exe",
        "lsass.exe", "svchost.exe", "dwm.exe",
    }

    targets = {
        target.lower()
        for target in targets
        if target.lower() not in forbidden
    }

    if not targets:
        return False, f"No safe matching process found for '{name}'."

    matches = []

    for proc in psutil.process_iter(["pid", "name"]):
        try:
            proc_name = (proc.info.get("name") or "").lower()

            if proc_name in targets:
                matches.append(proc)

        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    if not matches:
        return False, f"{name} is not running."

    return True, matches


def system_status():
    ram = psutil.virtual_memory()

    disk_path = Path.home().anchor or os.path.abspath(os.sep)
    disk = psutil.disk_usage(disk_path)

    return (
        f"CPU: {psutil.cpu_percent(interval=0.1):.0f}%\n"
        f"RAM: {ram.percent:.0f}% used "
        f"({ram.used / 1024**3:.1f}/"
        f"{ram.total / 1024**3:.1f} GB)\n"
        f"Disk: {disk.percent:.0f}% used\n"
        f"Time: {datetime.now().strftime('%d %b %Y, %I:%M:%S %p')}"
    )


# =========================================================
# AI PROVIDERS AND AUTOMATIC FAILOVER
# =========================================================

class ProviderManager:
    """Cloud-only AI with NVIDIA primary and OpenRouter failover."""

    def __init__(self):
        self.lock = threading.Lock()
        self.cooldowns = {}
        self.cooldown_seconds = 45
        self.last_provider = "Not connected"

    def available(self, provider):
        return time.monotonic() >= self.cooldowns.get(provider, 0)

    def failed(self, provider):
        self.cooldowns[provider] = (
            time.monotonic() + self.cooldown_seconds
        )

    def succeeded(self, provider):
        self.cooldowns.pop(provider, None)
        self.last_provider = provider

    def request(self, messages):
        # Always try NVIDIA first on every new request, so it automatically
        # recovers as soon as the NVIDIA endpoint is healthy again.
        providers = [
            (
                "NVIDIA Ultra",
                NVIDIA_URL,
                os.getenv("NVIDIA_API_KEY", "").strip(),
                NVIDIA_MODEL,
            ),
            (
                "OpenRouter Ultra",
                OPENROUTER_URL,
                os.getenv("OPENROUTER_API_KEY", "").strip(),
                OPENROUTER_ULTRA,
            ),
            (
                "OpenRouter Lightning Free",
                OPENROUTER_URL,
                os.getenv("OPENROUTER_API_KEY", "").strip(),
                OPENROUTER_LIGHTNING,
            ),
        ]

        # 35 seconds balances response time with large-model latency.
        # Override via ZYPHERUS_API_TIMEOUT if needed (10–120 seconds).
        try:
            timeout = int(os.getenv("ZYPHERUS_API_TIMEOUT", "35"))
        except (TypeError, ValueError):
            timeout = 35
        timeout = max(10, min(timeout, 120))

        errors = []

        for provider, url, key, model in providers:
            if not key:
                errors.append(f"{provider}: API key is not configured")
                continue

            if not self.available(provider):
                errors.append(f"{provider}: cooling down after a recent failure")
                continue

            payload = {
                "model": model,
                "messages": messages,
                "temperature": 0.35,
                "max_tokens": 480,
                "stream": False,
            }
            body = json.dumps(payload).encode("utf-8")
            headers = {
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            }

            if provider.startswith("OpenRouter"):
                headers["HTTP-Referer"] = "http://localhost"
                headers["X-Title"] = APP_NAME

            api_request = urllib.request.Request(
                url,
                data=body,
                headers=headers,
                method="POST",
            )

            try:
                with urllib.request.urlopen(
                    api_request, timeout=timeout
                ) as response:
                    raw = response.read().decode("utf-8", errors="replace")
                data = json.loads(raw)
                answer = data["choices"][0]["message"]["content"]

                if not isinstance(answer, str) or not answer.strip():
                    raise ValueError("Provider returned an empty answer")

                self.succeeded(provider)
                return answer.strip(), provider

            except urllib.error.HTTPError as exc:
                # Read the API's actual error message; this makes 400/401/429
                # failures diagnosable instead of showing only "HTTP Error".
                try:
                    detail = exc.read().decode("utf-8", errors="replace").strip()
                except Exception:
                    detail = ""
                self.failed(provider)
                if not detail:
                    detail = str(exc.reason)
                errors.append(
                    f"{provider}: HTTP {exc.code}: {detail[:350]}"
                )

            except Exception as exc:
                self.failed(provider)
                errors.append(f"{provider}: {str(exc)[:220]}")

        self.last_provider = "Offline"
        return (
            "All configured cloud AI providers failed or are unavailable.\n\n"
            + "\n".join(errors)
            + "\n\nLocal Windows commands are still available. "
              "No local AI model (Ollama) is used."
        ), "Offline"


PROVIDERS = ProviderManager()


class AIWorker(QThread):
    finished_answer = Signal(str, str)

    def __init__(self, messages):
        super().__init__()
        self.messages = messages

    def run(self):
        answer, provider = PROVIDERS.request(self.messages)
        self.finished_answer.emit(answer, provider)


# =========================================================
# TEXT-TO-SPEECH WORKER
# =========================================================

class SpeechWorker(QThread):
    spoken = Signal()

    def __init__(self, text):
        super().__init__()
        self.text = text

    def run(self):
        engine = None

        try:
            engine = pyttsx3.init()
            engine.setProperty("rate", 185)
            engine.setProperty("volume", 0.9)
            engine.say(self.text)
            engine.runAndWait()

        except Exception:
            pass

        finally:
            if engine is not None:
                try:
                    engine.stop()
                except Exception:
                    pass

            self.spoken.emit()


# =========================================================
# MICROPHONE WORKER
# =========================================================

class VoiceListener(QThread):
    recognized = Signal(str)
    status = Signal(str)

    def __init__(self):
        super().__init__()
        self.stop_requested = threading.Event()

    def stop(self):
        self.stop_requested.set()

    def run(self):
        recognizer = sr.Recognizer()
        recognizer.dynamic_energy_threshold = True
        recognizer.pause_threshold = 0.65
        recognizer.non_speaking_duration = 0.4

        try:
            with sr.Microphone() as source:
                self.status.emit("● CALIBRATING MICROPHONE")

                recognizer.adjust_for_ambient_noise(
                    source, duration=0.8
                )

                self.status.emit("● LISTENING")

                while not self.stop_requested.is_set():
                    try:
                        audio = recognizer.listen(
                            source,
                            timeout=1,
                            phrase_time_limit=12,
                        )

                        self.status.emit("● RECOGNIZING")

                        text = recognizer.recognize_google(
                            audio,
                            language=os.getenv(
                                "ZYPHERUS_VOICE_LANGUAGE", "en-IN"
                            ),
                        ).strip()

                        if text:
                            self.recognized.emit(text)

                        self.status.emit("● LISTENING")

                    except sr.WaitTimeoutError:
                        continue

                    except sr.UnknownValueError:
                        self.status.emit("● DID NOT CATCH THAT")

                    except sr.RequestError as exc:
                        self.recognized.emit(
                            "__VOICE_ERROR__:" + str(exc)
                        )
                        break

        except Exception as exc:
            self.recognized.emit(
                "__VOICE_ERROR__:" + str(exc)
            )

        self.status.emit("● VOICE STANDBY")


# =========================================================
# LOCAL COMMAND ROUTER
# =========================================================

class CommandRouter:
    def __init__(self, window):
        self.window = window

    def execute(self, text):
        original = text.strip()
        original = re.sub(
            r"^(?:hey\s+)?(?:zypherus|nexus)[, ]+",
            "",
            original,
            flags=re.I,
        )
        original = re.sub(
            r"^(?:(?:could|would|can|will)\s+you\s+|"
            r"please\s+|"
            r"i\s+want\s+you\s+to\s+|"
            r"i'd\s+like\s+you\s+to\s+)+",
            "",
            original,
            flags=re.I,
        ).strip()
        command = original.lower().strip().rstrip("?.! ")

        if not command:
            return True

        if command in {
            "help", "commands", "what can you do"
        }:
            answer = (
                "Commands:\n"
                "• open Chrome\n"
                "• launch Notepad\n"
                "• close Notepad\n"
                "• what time is it\n"
                "• what is today's date\n"
                "• system status\n"
                "• search Google for ...\n"
                "• open YouTube\n"
                "• clear chat\n"
                "• voice on / voice off\n"
                "• exit"
            )
            self.window.reply(answer)
            return True

        if command in {"exit", "quit", "shutdown zypherus"}:
            self.window.close()
            return True

        if command in {"clear", "clear chat"}:
            self.window.chat.clear()
            return True

        if command in {
            "voice on", "enable voice", "turn voice on"
        }:
            self.window.set_voice_enabled(True)
            self.window.reply("Voice responses enabled.")
            return True

        if command in {
            "voice off", "disable voice", "turn voice off"
        }:
            self.window.set_voice_enabled(False)
            self.window.add_message(
                "ZYPHERUS", "Voice responses disabled."
            )
            return True

        if command in {
            "start listening", "listen", "activate microphone"
        }:
            self.window.toggle_microphone()
            return True

        if command in {
            "stop listening", "stop microphone"
        }:
            self.window.stop_microphone()
            self.window.add_message(
                "ZYPHERUS", "Microphone stop requested."
            )
            return True

        if command in {
            "time", "what time is it", "current time"
        }:
            self.window.reply(
                datetime.now().strftime("%I:%M:%S %p")
            )
            return True

        if command in {
            "date", "today's date", "what is today's date"
        }:
            self.window.reply(
                datetime.now().strftime("%A, %d %B %Y")
            )
            return True

        if command in {"status", "system status", "pc status"}:
            self.window.reply(system_status(), "SYSTEM")
            return True

        if command in {"open youtube", "youtube"}:
            webbrowser.open("https://www.youtube.com")
            self.window.reply("Opened YouTube.")
            return True

        if command in {"open google", "google"}:
            webbrowser.open("https://www.google.com")
            self.window.reply("Opened Google.")
            return True

        search_match = re.match(
            r"^(?:search google for|google search for|search for)\s+(.+)$",
            original,
            re.I,
        )

        if search_match:
            query = search_match.group(1)
            webbrowser.open(
                "https://www.google.com/search?q="
                + quote_plus(query)
            )
            self.window.reply(f"Searching for {query}.")
            return True

        open_match = re.match(
            r"^(?:please\s+)?(?:open|launch|start|run)\s+(.+)$",
            original,
            re.I,
        )

        if open_match:
            _, result = launch_app(open_match.group(1))
            self.window.reply(result)
            return True

        close_match = re.match(
            r"^(?:please\s+)?(?:close|quit|terminate)\s+(.+)$",
            original,
            re.I,
        )

        if close_match:
            name = close_match.group(1).strip()

            if name.lower() in {
                "all apps", "all applications",
                "everything", "all programs",
            }:
                self.window.reply(
                    "For safety, bulk-closing all applications "
                    "is not supported. Close apps by name."
                )
                return True

            ok, result = close_app(name)

            if not ok:
                self.window.reply(result)
                return True

            processes = result

            answer = QMessageBox.question(
                self.window,
                "Confirm application close",
                f"Close {name}?\n"
                f"{len(processes)} matching process(es) found.\n\n"
                "Save your work first. Unsaved work may be lost.",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )

            if answer != QMessageBox.StandardButton.Yes:
                self.window.reply("Close cancelled.")
                return True

            closed = 0
            failed = 0

            for proc in processes:
                try:
                    proc.terminate()

                    try:
                        proc.wait(timeout=3)
                        closed += 1
                    except psutil.TimeoutExpired:
                        failed += 1

                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    failed += 1

            self.window.reply(
                f"Close request complete. Closed: {closed}; "
                f"not closed: {failed}."
            )
            return True

        return False


# =========================================================
# ZYPHERUS NEXUS HUD
# =========================================================

class NexusWindow(QMainWindow):
    def __init__(self):
        super().__init__()

        self.setWindowTitle(APP_NAME)
        self.resize(1100, 760)
        self.setMinimumSize(760, 560)

        self.history = [
            {"role": "system", "content": SYSTEM_PROMPT}
        ]

        self.worker = None
        self.voice_listener = None
        self.speech_worker = None
        self.voice_enabled = True
        self.closing = False

        self.router = CommandRouter(self)

        self.setStyleSheet("""
            QMainWindow, QWidget {
                    background-color: #050912;
                color: #D9F4FF;
                font-family: Segoe UI;
                font-size: 13px;
            }
            QFrame#panel {
                    background-color: #0A1220;
                    border: 1px solid #17415A;
                    border-radius: 10px;
            }
            QLabel#brand {
                    color: #5CEBFF;
                    font-size: 25px;
                font-weight: bold;
            }
            QLabel#subtitle {
                    color: #7893AA;
                    font-size: 10px;
            }
            QLabel#metric {
                    color: #61DFF5;
                    font-size: 12px;
                font-weight: bold;
            }
            QTextEdit {
                    background: #070D17;
                color: #C7EDFF;
                    border: 1px solid #17364B;
                border-radius: 8px;
                    padding: 14px;
                    selection-background-color: #15516B;
            }
            QLineEdit {
                    background: #091321;
                color: white;
                    border: 1px solid #21627B;
                border-radius: 8px;
                    padding: 12px;
            }
            QPushButton {
                    background: #0D2435;
                    color: #72E8FC;
                    border: 1px solid #20536B;
                    border-radius: 6px;
                    padding: 9px 12px;
            }
            QPushButton:hover {
                    background: #12394F;
                    border-color: #42CDEB;
                }
            QPushButton:disabled {
                color: #637585;
                border-color: #273643;
            }
            QProgressBar {
                background: #101B29;
                border: 0;
                border-radius: 4px;
                height: 6px;
            }
            QProgressBar::chunk {
                background: #35D8F2;
                border-radius: 3px;
            }
        """)

        root = QWidget()
        self.setCentralWidget(root)

        layout = QVBoxLayout(root)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        # Header
        header = QHBoxLayout()
        brand_box = QVBoxLayout()

        brand = QLabel("ZYPHERUS")
        brand.setObjectName("brand")
        brand_box.addWidget(brand)

        subtitle = QLabel(
            "NEXUS  /  PERSONAL INTELLIGENCE"
        )
        subtitle.setObjectName("subtitle")
        brand_box.addWidget(subtitle)

        header.addLayout(brand_box)
        header.addStretch()

        self.connection = QLabel("● INITIALIZING")
        self.connection.setObjectName("metric")
        header.addWidget(self.connection)

        layout.addLayout(header)

        # System metrics
        metrics = QFrame()
        metrics.setObjectName("panel")
        metric_layout = QHBoxLayout(metrics)
        metric_layout.setContentsMargins(14, 9, 14, 9)
        metric_layout.setSpacing(22)

        self.cpu_label = QLabel("CPU: --%")
        self.ram_label = QLabel("RAM: --%")
        self.disk_label = QLabel("DISK: --%")
        self.metric_bars = {}

        for name, label in (
            ("CPU", self.cpu_label),
            ("RAM", self.ram_label),
            ("DISK", self.disk_label),
        ):
            label.setObjectName("metric")
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setTextVisible(False)
            bar.setFixedHeight(6)
            bar.setToolTip(f"{name} utilization")
            self.metric_bars[name] = bar

            metric_group = QVBoxLayout()
            metric_group.setSpacing(5)
            metric_group.addWidget(label)
            metric_group.addWidget(bar)
            metric_layout.addLayout(metric_group, 1)

        layout.addWidget(metrics)

        # Chat history
        self.chat = QTextEdit()
        self.chat.setReadOnly(True)
        self.chat.setFont(QFont("Consolas", 10))
        layout.addWidget(self.chat, 1)

        # Input row
        input_row = QHBoxLayout()

        self.entry = QLineEdit()
        self.entry.setPlaceholderText(
            "Ask or command..."
        )
        self.entry.returnPressed.connect(self.send_message)
        input_row.addWidget(self.entry, 1)

        self.send_button = QPushButton("SEND  ↗")
        self.send_button.clicked.connect(self.send_message)
        input_row.addWidget(self.send_button)

        self.mic_button = QPushButton("MIC")
        self.mic_button.setToolTip("Start or stop voice recognition")
        self.mic_button.clicked.connect(self.toggle_microphone)
        input_row.addWidget(self.mic_button)

        self.voice_button = QPushButton("SPEAK ON")
        self.voice_button.setToolTip("Toggle spoken responses")
        self.voice_button.clicked.connect(self.toggle_voice)
        input_row.addWidget(self.voice_button)

        layout.addLayout(input_row)

        # Quick actions
        quick_row = QHBoxLayout()

        for label, command in [
            ("STATUS", "system status"),
            ("NOTEPAD", "open Notepad"),
            ("YOUTUBE", "open YouTube"),
            ("HELP", "help"),
        ]:
            button = QPushButton(label)
            button.clicked.connect(
                lambda checked=False, c=command:
                self.run_quick(c)
            )
            quick_row.addWidget(button)

        layout.addLayout(quick_row)

        self.add_message(
            "SYSTEM",
            "ZYPHERUS NEXUS initialized.\n"
            "Local commands are available. Type 'help' for commands."
        )

        self.update_metrics()

        self.metrics_timer = QTimer(self)
        self.metrics_timer.timeout.connect(self.update_metrics)
        self.metrics_timer.start(2000)

    # -----------------------------------------------------
    # Chat helpers
    # -----------------------------------------------------

    def add_message(self, speaker, message):
        safe_speaker = html.escape(str(speaker))
        safe_message = html.escape(str(message)).replace(
            "\n", "<br>"
        )

        self.chat.append(
            f"<b style='color:#48DFFF'>{safe_speaker}</b>"
            f"<br>{safe_message}<br>"
        )

        scrollbar = self.chat.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def reply(self, message, speaker="ZYPHERUS"):
        self.add_message(speaker, message)

        if speaker == "ZYPHERUS":
            self.speak(message)

    def run_quick(self, command):
        self.entry.setText(command)
        self.send_message()

    # -----------------------------------------------------
    # Voice controls
    # -----------------------------------------------------

    def toggle_microphone(self):
        if self.voice_listener and self.voice_listener.isRunning():
            self.stop_microphone()
            return

        self.voice_listener = VoiceListener()
        self.voice_listener.recognized.connect(
            self.on_voice_recognized
        )
        self.voice_listener.status.connect(
            self.connection.setText
        )
        self.voice_listener.finished.connect(
            self.on_microphone_stopped
        )

        self.mic_button.setText("STOP MIC")
        self.connection.setText("● STARTING MICROPHONE")
        self.voice_listener.start()

    def stop_microphone(self):
        if self.voice_listener and self.voice_listener.isRunning():
            self.voice_listener.stop()
            self.connection.setText("● STOPPING MICROPHONE")
            self.mic_button.setEnabled(False)

    def on_microphone_stopped(self):
        self.mic_button.setEnabled(True)
        self.mic_button.setText("MIC")
        self.connection.setText("● VOICE STANDBY")

    def on_voice_recognized(self, text):
        if text.startswith("__VOICE_ERROR__:"):
            self.add_message(
                "VOICE ERROR",
                text.replace("__VOICE_ERROR__:", "", 1)
            )
            return

        if self.closing:
            return

        self.entry.setText(text)
        self.add_message("YOU / VOICE", text)
        self.process_message(text, already_displayed=True)

    def toggle_voice(self):
        self.set_voice_enabled(not self.voice_enabled)

    def set_voice_enabled(self, enabled):
        self.voice_enabled = enabled
        self.voice_button.setText(
            "SPEAK ON"
            if enabled else "SPEAK OFF"
        )

    def speak(self, text):
        if not self.voice_enabled or self.closing:
            return

        # Avoid launching overlapping speech workers.
        if self.speech_worker and self.speech_worker.isRunning():
            return

        clean_text = re.sub(r"[`*_#]", "", str(text))
        clean_text = re.sub(
            r"https?://\S+", "link", clean_text
        )
        clean_text = clean_text[:1800]

        self.speech_worker = SpeechWorker(clean_text)
        self.speech_worker.start()

    # -----------------------------------------------------
    # Message processing and AI
    # -----------------------------------------------------

    def send_message(self):
        text = self.entry.text().strip()

        if not text:
            return

        self.entry.clear()
        self.add_message("YOU", text)
        self.process_message(text, already_displayed=True)

    def process_message(self, text, already_displayed=False):
        if not text.strip():
            return

        # Commands are handled locally before any AI request.
        if self.router.execute(text):
            self.entry.clear()
            return

        if self.worker and self.worker.isRunning():
            self.reply(
                "I'm still processing the previous AI request. "
                "Please wait for it to finish."
            )
            return

        self.history.append({
            "role": "user",
            "content": text,
        })

        if len(self.history) > 17:
            self.history = [self.history[0]] + self.history[-16:]

        self.send_button.setEnabled(False)
        self.connection.setText("● THINKING")

        self.worker = AIWorker(self.history.copy())
        self.worker.finished_answer.connect(self.on_ai_answer)
        self.worker.start()

    def on_ai_answer(self, answer, provider):
        self.history.append({
            "role": "assistant",
            "content": answer,
        })

        if len(self.history) > 17:
            self.history = [self.history[0]] + self.history[-16:]

        self.add_message(f"ZYPHERUS / {provider}", answer)
        self.speak(answer)

        self.connection.setText(f"● {provider.upper()}")
        self.send_button.setEnabled(True)
        self.entry.setFocus()

    # -----------------------------------------------------
    # System metrics
    # -----------------------------------------------------

    def update_metrics(self):
        try:
            cpu = psutil.cpu_percent()
            ram = psutil.virtual_memory().percent

            disk_path = Path.home().anchor or os.path.abspath(os.sep)
            disk = psutil.disk_usage(disk_path).percent

            self.cpu_label.setText(f"CPU: {cpu:.0f}%")
            self.ram_label.setText(f"RAM: {ram:.0f}%")
            self.disk_label.setText(f"DISK: {disk:.0f}%")

            for name, value in (
                ("CPU", cpu), ("RAM", ram), ("DISK", disk)
            ):
                self.metric_bars[name].setValue(int(value))

        except Exception:
            pass

    # -----------------------------------------------------
    # Safe shutdown
    # -----------------------------------------------------

    def closeEvent(self, event):
        self.closing = True
        self.metrics_timer.stop()

        if self.voice_listener and self.voice_listener.isRunning():
            self.voice_listener.stop()
            self.voice_listener.wait(2500)

        if self.speech_worker and self.speech_worker.isRunning():
            self.speech_worker.wait(2500)

        if self.worker and self.worker.isRunning():
            self.worker.wait(1500)

        event.accept()


# =========================================================
# APPLICATION ENTRY POINT
# =========================================================

def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)

    window = NexusWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
