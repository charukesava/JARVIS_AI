"""
REY SHINCHAN AI - v2
Personal AI Assistant for Windows 11
Phase 2: AI Brain + Memory + Settings + Text Input
Architecture: Hybrid (Online LLM API + Offline rule-based fallback)
"""

import sys
import os
import threading
import queue as _queue
import datetime
import webbrowser
import subprocess
import socket
import json
import sqlite3
import re
import time
import winreg
import math

import speech_recognition as sr
import psutil

from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QTextEdit, QFrame, QProgressBar,
    QLineEdit, QDialog, QFormLayout, QComboBox, QCheckBox,
    QDialogButtonBox, QMessageBox,
    QSystemTrayIcon, QMenu
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QObject
from PyQt6.QtGui import QFont, QColor, QIcon, QPixmap, QColor as QC


# ─────────────────────────────────────────────
#   CONFIG MANAGER
# ─────────────────────────────────────────────
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rey_shinchan_config.json")

class ConfigManager:
    DEFAULT = {
        "api_key":          "",          # Primary Gemini key (AIzaSy...)
        "gemini_key_2":     "",          # 2nd Gemini key for rotation
        "gemini_key_3":     "",          # 3rd Gemini key for rotation
        "groq_key":         "",          # Groq key (gsk_...) — FREE 14,400 req/day
        "api_provider":     "gemini",    # "groq" | "gemini" | "openai"
        "model":            "gemini-2.0-flash",
        "groq_model":       "llama-3.3-70b-versatile",  # best free Groq model
        "ai_enabled":       True,
        "max_memory":       8,           # 8 messages = much less token usage
        "user_name":        "Sir",
        "tts_rate":         155,
        "wake_word_enabled": True,
        "autostart":        False,
        "auto_listen":      True,
    }

    def __init__(self):
        self.config = self.DEFAULT.copy()
        self.load()

    def load(self):
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r") as f:
                    self.config.update(json.load(f))
            except Exception:
                pass
        # Auto-fix: Gemini key sent to OpenAI (starts with AIzaSy)
        key = self.config.get("api_key", "")
        if key.startswith("AIza") and self.config.get("api_provider") == "openai":
            self.config["api_provider"] = "gemini"
            if "gpt" in self.config.get("model", ""):
                self.config["model"] = "gemini-2.0-flash"
            self.save()

    def save(self):
        try:
            with open(CONFIG_FILE, "w") as f:
                json.dump(self.config, f, indent=2)
        except Exception:
            pass

    def get(self, key):
        return self.config.get(key, self.DEFAULT.get(key))

    def set(self, key, value):
        self.config[key] = value
        self.save()


# ─────────────────────────────────────────────
#   MEMORY DATABASE (SQLite)
# ─────────────────────────────────────────────
DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rey_shinchan_memory.db")

class MemoryDB:
    def __init__(self):
        self.conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        self._lock = threading.Lock()
        self._create_tables()

    def _create_tables(self):
        with self._lock:
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS conversations (
                    id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    role      TEXT NOT NULL,
                    content   TEXT NOT NULL
                )
            """)
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS preferences (
                    key   TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            self.conn.commit()

    def add_message(self, role: str, content: str):
        with self._lock:
            self.conn.execute(
                "INSERT INTO conversations (timestamp, role, content) VALUES (?, ?, ?)",
                (datetime.datetime.now().isoformat(), role, content)
            )
            self.conn.commit()

    def get_recent(self, n: int = 20) -> list:
        with self._lock:
            cursor = self.conn.execute(
                "SELECT role, content FROM conversations ORDER BY id DESC LIMIT ?", (n,)
            )
            rows = cursor.fetchall()
        return [{"role": r[0], "content": r[1]} for r in reversed(rows)]

    def get_total_count(self) -> int:
        with self._lock:
            cursor = self.conn.execute("SELECT COUNT(*) FROM conversations")
            return cursor.fetchone()[0]

    def clear(self):
        with self._lock:
            self.conn.execute("DELETE FROM conversations")
            self.conn.commit()


# ─────────────────────────────────────────────
#   GLOBAL SINGLETONS
# ─────────────────────────────────────────────
config  = ConfigManager()
memory  = MemoryDB()

# ─────────────────────────────────────────────
#   TEXT-TO-SPEECH ENGINE
# ─────────────────────────────────────────────
# ─────────────────────────────────────────────
#   TEXT-TO-SPEECH ENGINE
# ─────────────────────────────────────────────
# One-shot PowerShell per speak call — 100 % reliable on Windows,
# no COM-thread issues, no blocking IO, no persistent-process hangs.
# The worker thread calls subprocess.run() which blocks until speech
# finishes, so calls are naturally sequential and never overlap.

_tts_queue: _queue.Queue = _queue.Queue()
_tts_rate  = config.get("tts_rate") or 155
# Convert wpm → SAPI rate scale -10..10  (175 wpm = 0, each 15 wpm = 1 step)
_sapi_rate = max(-10, min(10, int((_tts_rate - 175) / 15)))

# Build the PS init block once (reused for every call)
_TTS_PS_INIT = (
    "Add-Type -AssemblyName System.Speech; "
    "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
    f"$s.Rate = {_sapi_rate}; "
    "$s.Volume = 100; "
    "try { $s.SelectVoiceByHints("
    "[System.Speech.Synthesis.VoiceGender]::Male) } catch {}; "
)

def _tts_worker():
    while True:
        text = _tts_queue.get()
        try:
            if text:
                safe = (text.replace("'", " ")
                            .replace('"', " ")
                            .replace("\n", " ")
                            .replace("\r", " ")
                            .strip())
                if safe:
                    ps_cmd = _TTS_PS_INIT + f"$s.Speak('{safe}')"
                    subprocess.run(
                        ["powershell", "-NoProfile",
                         "-NonInteractive", "-Command", ps_cmd],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=60
                    )
        except Exception:
            pass
        _tts_queue.task_done()

_tts_thread = threading.Thread(target=_tts_worker, daemon=True, name="TTS-Worker")
_tts_thread.start()

def speak(text: str):
    """Queue text for speech — never blocks the caller."""
    global _last_response
    if not text:
        return
    _last_response = text          # remember for 'say that again'
    # Drop stale items to prevent pile-up
    while not _tts_queue.empty():
        try:
            _tts_queue.get_nowait()
        except _queue.Empty:
            break
    _tts_queue.put(text)



# ─────────────────────────────────────────────
#   ACCESSIBILITY STATE  (shared globals)
# ─────────────────────────────────────────────
_last_response: str = ""          # stores last spoken reply → 'say that again'
_reminders:     list = []          # active reminder threads (daemon, no cancel needed)

# ─────────────────────────────────────────────
#   ONLINE / OFFLINE DETECTION
# ─────────────────────────────────────────────
def is_online() -> bool:
    try:
        socket.setdefaulttimeout(3)
        socket.create_connection(("8.8.8.8", 53))
        return True
    except OSError:
        return False


# Volume/mute helper
_WS_SHELL = (
    'powershell -NoProfile -NonInteractive -c '
    '"$o=New-Object -ComObject WScript.Shell;$o.SendKeys([char]{code})"'
)
def _send_media_key(code: int):
    subprocess.Popen(_WS_SHELL.format(code=code), shell=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _launch_app_smart(app_name: str) -> bool:
    """
    Launch any installed app (Store / UWP / desktop) by fuzzy name match.
    Uses PowerShell Get-StartApps which queries both the Start Menu index
    and the Windows AppX package registry — covers games, Store apps, and
    traditional desktop shortcuts.
    Returns True if a matching app was found and launched.
    """
    safe = app_name.replace("'", "").replace('"', "")
    ps = (
        f"$a = Get-StartApps | "
        f"Where-Object {{$_.Name -like '*{safe}*'}} | "
        f"Select-Object -First 1; "
        f"if ($a) {{ Start-Process \"shell:AppsFolder\\$($a.AppID)\"; exit 0 }} "
        f"else {{ exit 1 }}"
    )
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8
        )
        return r.returncode == 0
    except Exception:
        return False


# Module-level constant maps (allocated once, O(1) lookup)
_FOLDER_MAP = {
    "downloads": "Downloads",
    "documents": "Documents",
    "desktop":   "Desktop",
    "pictures":  "Pictures",
    "videos":    "Videos",
    "music":     "Music",
}

_APP_CLOSE_MAP = {
    # Browsers
    "chrome": "chrome.exe",         "google chrome": "chrome.exe",
    "firefox": "firefox.exe",        "edge": "msedge.exe",
    "microsoft edge": "msedge.exe",
    # System apps
    "notepad": "notepad.exe",        "paint": "mspaint.exe",
    "settings": "SystemSettings.exe","setting": "SystemSettings.exe",
    "system settings": "SystemSettings.exe",
    "windows settings": "SystemSettings.exe",
    "task manager": "Taskmgr.exe",   "taskmgr": "Taskmgr.exe",
    "file explorer": "explorer.exe", "explorer": "explorer.exe",
    "calculator": "CalculatorApp.exe",
    # Social / messaging  (also handle by psutil title match below)
    "whatsapp": "WhatsApp.exe",      "spotify": "Spotify.exe",
    "telegram": "Telegram.exe",
    # Office
    "word": "WINWORD.EXE",           "excel": "EXCEL.EXE",
    "powerpoint": "POWERPNT.EXE",
    # Dev
    "vscode": "Code.exe",            "vs code": "Code.exe",
    "visual studio code": "Code.exe",
    # Misc
    "terminal": "WindowsTerminal.exe", "cmd": "cmd.exe",
}

# Busy flag — prevents overlapping processing
_busy = threading.Event()


# ─────────────────────────────────────────────
#   PERMISSION BROKER  (forward declaration)
#   PermissionDialog class is defined after DARK_STYLE.
#   Python resolves names at call-time, so this works.
# ─────────────────────────────────────────────
class PermissionBroker(QObject):
    """
    Thread-safe permission gating.
    Background threads call ask() which blocks until the user
    responds on the GUI thread via a Qt signal→slot bridge.
    """
    _request = pyqtSignal(str, str)

    def __init__(self):
        super().__init__()
        self._event  = threading.Event()
        self._result = False
        self._request.connect(self._show_dialog)

    def ask(self, action: str, description: str, timeout: float = 40.0) -> bool:
        """
        Show a permission dialog on the GUI thread.
        Blocks the calling background thread until user responds or times out.
        Returns True = allowed, False = denied / timeout.
        """
        self._event.clear()
        self._result = False
        self._request.emit(action, description)
        granted = self._event.wait(timeout=timeout)
        return granted and self._result

    def _show_dialog(self, action: str, description: str):
        """Called on GUI thread via the Qt signal."""
        dlg = PermissionDialog(action, description)
        self._result = (dlg.exec() == QDialog.DialogCode.Accepted)
        self._event.set()


permission_broker = PermissionBroker()


# ─────────────────────────────────────────────
#   RULE-BASED COMMAND ENGINE
# ─────────────────────────────────────────────
def try_rule_command(command: str):
    """
    Try to match a rule-based command.
    Returns (response_str, True)  — if a rule matched.
    Returns (None, False)         — if no rule matched (hand off to AI).
    """
    cmd = command.lower().strip()

    # ── CLOSE ALL WINDOWS / MINIMIZE ALL  (must come before single-close) ──
    _close_all_triggers = [
        "close all windows", "close all apps", "close everything",
        "close all", "minimize all", "minimise all",
        "minimize all windows", "minimise all windows",
        "show desktop", "hide all windows", "hide everything",
        "sab band karo", "sab kuch band karo",       # Hindi: close everything
        "ellam mudi", "ellam close pannu",            # Tamil
        "anni close chesey", "anni band chesey",      # Telugu
    ]
    if any(p in cmd for p in _close_all_triggers):
        def _do_close_all():
            time.sleep(0.4)
            try:
                import pyautogui
                # Win+D → show desktop (minimizes all windows instantly)
                pyautogui.hotkey('win', 'd')
            except Exception:
                pass
        threading.Thread(target=_do_close_all, daemon=True).start()
        return "Minimising all windows and showing the desktop.", True

    # ── CLOSE / QUIT / KILL  (must come FIRST before any open rule) ──
    _close_triggers = [
        # English generic close
        "close it", "close this", "close window", "close the window",
        "close current window", "close the app", "close the apps",
        "close apps", "close active", "close current",
        "exit this", "exit it", "exit app", "exit the app",
        "alt f4", "kill this", "kill it", "kill the app",
        # Tamil / transliterated
        "mudi", "mudikka", "close pannу", "close pannu", "banda karo",
        # Hindi
        "band karo", "band kar", "band kardo", "band karna",
        "isko band karo", "app band karo",
        # Telugu
        "meyu", "close cheseyи", "close chesey",
    ]
    if any(p in cmd for p in _close_triggers):
        def _do_close():
            time.sleep(0.6)
            try:
                import pyautogui
                pyautogui.hotkey('alt', 'F4')
            except Exception:
                pass
        threading.Thread(target=_do_close, daemon=True).start()
        return "Closing the active window.", True

    _close_named = re.match(r'(?:close|quit|exit|kill)\s+(.+)', cmd)
    if _close_named:
        app_name = _close_named.group(1).strip()

        def _kill_processes(exe_name: str):
            """Kill all processes matching exe_name (case-insensitive)."""
            killed = False
            try:
                for proc in psutil.process_iter(['name', 'pid']):
                    if proc.info['name'] and \
                            proc.info['name'].lower() == exe_name.lower():
                        proc.kill()
                        killed = True
            except Exception:
                pass
            if not killed:
                # fallback: taskkill (handles UWP/packaged apps)
                subprocess.run(
                    f'taskkill /IM "{exe_name}" /F',
                    shell=True, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )

        def _kill_by_title(keyword: str):
            """Kill any process whose name contains keyword (Store apps)."""
            keyword_l = keyword.lower()
            for proc in psutil.process_iter(['name', 'pid']):
                try:
                    if proc.info['name'] and \
                            keyword_l in proc.info['name'].lower():
                        proc.kill()
                except Exception:
                    pass

        exe = _APP_CLOSE_MAP.get(app_name)
        if exe:
            _kill_processes(exe)
            return f"Closing {app_name}.", True

        # Not in map — need permission, then try smart kill
        if app_name and app_name not in (
                "it", "this", "app", "application", "window",
                "current", "the window", "the app"):
            if not permission_broker.ask(
                    "Close Application",
                    f"Rey Shinchan wants to force-close:\n  {app_name}\nAllow?"):
                return "Cancelled.", True
            # Try exact exe match, then keyword match in process names
            _kill_processes(app_name + ".exe")
            _kill_by_title(app_name)
            return f"Closed {app_name}.", True

    # Helper: is this a close-intent command? (used below to guard open rules)
    _is_closing = cmd.startswith(("close", "quit", "exit", "kill"))

    # ── Pause / resume auto-listen  (check early so 'stop' doesn't open anything) ──
    if any(p in cmd for p in [
            "go to idle", "stop listening", "pause listening",
            "be quiet", "sleep mode", "pause"]):
        _set_auto_listen(False)
        return "Going to idle. Double-click me or say 'wake up' to resume.", True

    if any(p in cmd for p in [
            "start listening", "wake up", "resume listening",
            "keep listening", "listen again"]):
        _set_auto_listen(True)
        return "Auto-listen resumed. I'm always ready!", True

    # ── Time & Date ──
    if "time" in cmd and "youtube" not in cmd:
        now = datetime.datetime.now().strftime("%I:%M %p")
        return f"The current time is {now}.", True

    if "date" in cmd or "today" in cmd:
        today = datetime.datetime.now().strftime("%A, %B %d, %Y")
        return f"Today is {today}.", True

    # ── App Control ──
    if "open chrome" in cmd or "open google chrome" in cmd:
        subprocess.Popen(["start", "chrome"], shell=True)
        return f"Opening Google Chrome, {config.get('user_name')}.", True

    if "open notepad" in cmd:
        subprocess.Popen(["notepad.exe"])
        return "Opening Notepad.", True

    if "open file explorer" in cmd or "open explorer" in cmd:
        subprocess.Popen(["explorer.exe"])
        return "Opening File Explorer.", True

    if "open calculator" in cmd:
        subprocess.Popen(["calc.exe"])
        return "Opening Calculator.", True

    if "open vs code" in cmd or "open visual studio code" in cmd:
        subprocess.Popen(["code"], shell=True)
        return "Opening VS Code.", True

    if "open task manager" in cmd:
        subprocess.Popen(["taskmgr.exe"])
        return "Opening Task Manager.", True

    if "open paint" in cmd:
        subprocess.Popen(["mspaint.exe"])
        return "Opening Paint.", True

    if "open word" in cmd:
        subprocess.Popen(["start", "winword"], shell=True)
        return "Opening Microsoft Word.", True

    if "open excel" in cmd:
        subprocess.Popen(["start", "excel"], shell=True)
        return "Opening Microsoft Excel.", True

    if "open spotify" in cmd:
        subprocess.Popen(["start", "spotify"], shell=True)
        return "Opening Spotify.", True

    if "open camera" in cmd:
        subprocess.Popen(["start", "microsoft.windows.camera:"], shell=True)
        return "Opening Camera.", True

    # ── Social Media & Web Apps ──
    # Rule: try installed app first; browser is the fallback only
    if not _is_closing:
        if "instagram" in cmd:
            if not _launch_app_smart("instagram"):
                webbrowser.open("https://www.instagram.com")
                return "Opening Instagram in your browser.", True
            return "Opening Instagram app.", True

        if "whatsapp" in cmd:
            if not _launch_app_smart("whatsapp"):
                try:
                    subprocess.Popen(["start", "whatsapp:"], shell=True)
                except Exception:
                    webbrowser.open("https://web.whatsapp.com")
            return "Opening WhatsApp.", True

        if "facebook" in cmd:
            if not _launch_app_smart("facebook"):
                webbrowser.open("https://www.facebook.com")
                return "Opening Facebook in your browser.", True
            return "Opening Facebook app.", True

        if "twitter" in cmd or "open x" in cmd:
            if not _launch_app_smart("twitter"):
                webbrowser.open("https://www.x.com")
                return "Opening X (Twitter) in your browser.", True
            return "Opening X (Twitter) app.", True

        if "telegram" in cmd:
            if not _launch_app_smart("telegram"):
                try:
                    subprocess.Popen(["start", "tg:"], shell=True)
                except Exception:
                    webbrowser.open("https://web.telegram.org")
            return "Opening Telegram.", True

        if "linkedin" in cmd:
            if not _launch_app_smart("linkedin"):
                webbrowser.open("https://www.linkedin.com")
                return "Opening LinkedIn in your browser.", True
            return "Opening LinkedIn app.", True

        if "snapchat" in cmd:
            if not _launch_app_smart("snapchat"):
                webbrowser.open("https://www.snapchat.com")
                return "Opening Snapchat in your browser.", True
            return "Opening Snapchat app.", True

        if "netflix" in cmd:
            if not _launch_app_smart("netflix"):
                webbrowser.open("https://www.netflix.com")
                return "Opening Netflix in your browser.", True
            return "Opening Netflix app.", True

        if "gmail" in cmd:
            if not _launch_app_smart("gmail"):
                webbrowser.open("https://mail.google.com")
                return "Opening Gmail in your browser.", True
            return "Opening Gmail app.", True

        if "google maps" in cmd or ("maps" in cmd and "open" in cmd):
            if not _launch_app_smart("maps"):
                webbrowser.open("https://maps.google.com")
                return "Opening Google Maps in your browser.", True
            return "Opening Maps app.", True

        if "reddit" in cmd:
            if not _launch_app_smart("reddit"):
                webbrowser.open("https://www.reddit.com")
                return "Opening Reddit in your browser.", True
            return "Opening Reddit app.", True

    # ── Web Search ──
    if "search" in cmd and "youtube" not in cmd:
        query = cmd.replace("search for", "").replace("search", "").replace("google", "").strip()
        if query:
            webbrowser.open(f"https://www.google.com/search?q={query}")
            return f"Searching Google for: {query}", True

    if "youtube" in cmd:
        query = (cmd.replace("youtube", "").replace("search", "")
                   .replace("play", "").replace("open", "").strip())
        if query:
            webbrowser.open(f"https://www.youtube.com/results?search_query={query}")
            return f"Searching YouTube for: {query}", True
        webbrowser.open("https://www.youtube.com")
        return "Opening YouTube.", True

    if "open wikipedia" in cmd or "wikipedia" in cmd:
        query = cmd.replace("wikipedia", "").replace("open", "").replace("search", "").strip()
        if query:
            webbrowser.open(f"https://en.wikipedia.org/wiki/{query.replace(' ', '_')}")
            return f"Opening Wikipedia article on: {query}", True
        webbrowser.open("https://en.wikipedia.org")
        return "Opening Wikipedia.", True

    # ── System Info ──
    if "battery" in cmd and "instagram" not in cmd:
        battery = psutil.sensors_battery()
        if battery:
            pct = battery.percent
            plugged = "charging" if battery.power_plugged else "not charging"
            return f"Battery is at {pct:.0f}% and {plugged}.", True
        return "Could not read battery information.", True

    if re.search(r'\bcpu\b', cmd) or "processor usage" in cmd:
        usage = psutil.cpu_percent(interval=1)
        return f"CPU usage is at {usage}%.", True

    if re.search(r'\bram\b', cmd) or "memory usage" in cmd:
        mem = psutil.virtual_memory()
        used = mem.used // (1024 ** 3)
        total = mem.total // (1024 ** 3)
        return f"RAM usage is {used} GB out of {total} GB.", True

    if re.search(r'\bdisk\b', cmd) or "storage" in cmd:
        disk = psutil.disk_usage("C:\\")
        free = disk.free // (1024 ** 3)
        total = disk.total // (1024 ** 3)
        return f"C drive has {free} GB free out of {total} GB total.", True

    if "ip address" in cmd or "my ip" in cmd:
        hostname = socket.gethostname()
        ip = socket.gethostbyname(hostname)
        return f"Your local IP address is {ip}.", True

    if "internet" in cmd or "am i online" in cmd or "check connection" in cmd:
        status = "connected to the internet" if is_online() else "currently offline"
        return f"You are {status}.", True

    # ── Volume Control ──
    if "volume up" in cmd or "increase volume" in cmd or "louder" in cmd:
        _send_media_key(175)
        return "Volume increased.", True

    if "volume down" in cmd or "decrease volume" in cmd or "quieter" in cmd:
        _send_media_key(174)
        return "Volume decreased.", True

    if "mute" in cmd or "unmute" in cmd:
        _send_media_key(173)
        return "Volume muted/unmuted.", True

    # ── System Power  (permission-gated) ──
    if "shutdown" in cmd and "how" not in cmd:
        if not permission_broker.ask(
                "Shut Down Computer",
                "Rey Shinchan wants to shut down your computer in 5 seconds.\n"
                "Do you want to allow this action?"):
            return "Shutdown cancelled — you denied permission.", True
        os.system("shutdown /s /t 5")
        return "Shutting down the system in 5 seconds.", True

    if ("restart" in cmd or "reboot" in cmd) and "how" not in cmd:
        if not permission_broker.ask(
                "Restart Computer",
                "Rey Shinchan wants to restart your computer in 5 seconds.\n"
                "Do you want to allow this action?"):
            return "Restart cancelled — you denied permission.", True
        os.system("shutdown /r /t 5")
        return "Restarting the system in 5 seconds.", True

    if "sleep" in cmd or "hibernate" in cmd:
        if not permission_broker.ask(
                "Sleep / Hibernate",
                "Rey Shinchan wants to put your computer to sleep.\n"
                "Do you want to allow this action?"):
            return "Sleep cancelled — you denied permission.", True
        os.system("rundll32.exe powrprof.dll,SetSuspendState 0,1,0")
        return "Putting the system to sleep.", True

    if "lock" in cmd and "unlock" not in cmd:
        if not permission_broker.ask(
                "Lock Computer",
                "Rey Shinchan wants to lock your screen.\n"
                "Allow this action?"):
            return "Lock cancelled — you denied permission.", True
        os.system("rundll32.exe user32.dll,LockWorkStation")
        return "System locked.", True

    # ── Screenshot ──
    if "screenshot" in cmd:
        try:
            import pyautogui
            shot_path = os.path.join(
                os.path.expanduser("~"), "Desktop",
                f"rey_shinchan_ss_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
            )
            pyautogui.screenshot(shot_path)
            return "Screenshot saved to your Desktop.", True
        except Exception as e:
            return f"Screenshot failed: {e}", True

    # ── Memory / History ──
    if "clear memory" in cmd or "forget everything" in cmd or "clear history" in cmd:
        memory.clear()
        return "Conversation memory cleared.", True

    if "memory stats" in cmd or "how many messages" in cmd:
        total = memory.get_total_count()
        return f"I have {total} messages stored in memory.", True

    # ── Greetings (let AI handle these — return False) ──
    if ("hello" in cmd or "hi" in cmd or "hey" in cmd or
            "good morning" in cmd or "good evening" in cmd):
        return None, False

    if "who are you" in cmd or "what are you" in cmd or "what can you do" in cmd:
        return None, False

    if "thank" in cmd:
        return f"You're welcome, {config.get('user_name')}! Always here to help.", True

    # ── Minimise active window ──
    if any(p in cmd for p in ["minimise", "minimize", "hide window", "hide this"]):
        try:
            import pyautogui
            pyautogui.hotkey('win', 'down')
        except Exception:
            pass
        return "Window minimised.", True

    # ── Exit Rey Shinchan ──
    if any(p in cmd for p in ["goodbye", "bye bye", "shutdown rey", "stop rey",
                               "close rey shinchan", "exit rey shinchan"]):
        QTimer.singleShot(1800, lambda: sys.exit(0))
        return f"Goodbye, {config.get('user_name')}! Shutting down Rey Shinchan.", True

    # ── Brightness ──
    if "brightness" in cmd:
        level = 60
        for word in cmd.split():
            if word.isdigit():
                level = max(0, min(100, int(word)))
                break
        if "up" in cmd or "increase" in cmd or "brighter" in cmd:
            level = 80
        elif "down" in cmd or "decrease" in cmd or "dim" in cmd:
            level = 30
        ps_cmd = (f'(Get-WmiObject -Namespace root/WMI '
                  f'-Class WmiMonitorBrightnessMethods).WmiSetBrightness(1,{level})')
        os.system(f'powershell -Command "{ps_cmd}"')
        return f"Brightness set to {level}%.", True

    # ── Empty Recycle Bin ──
    if "empty recycle" in cmd or "clean recycle" in cmd:
        if not permission_broker.ask(
                "Empty Recycle Bin",
                "Rey Shinchan wants to permanently delete all items in the Recycle Bin.\n"
                "This cannot be undone. Allow?"):
            return "Recycle Bin not emptied — you denied permission.", True
        os.system('powershell -Command "Clear-RecycleBin -Force -ErrorAction SilentlyContinue"')
        return "Recycle Bin emptied.", True

    # ── WiFi ──
    if any(p in cmd for p in ["wifi off", "turn off wifi", "disable wifi", "disconnect wifi"]):
        if not permission_broker.ask("Disable WiFi",
                "Rey Shinchan wants to disconnect your WiFi.\nAllow?"):
            return "WiFi change cancelled.", True
        os.system("netsh wlan disconnect")
        return "WiFi disconnected.", True

    if any(p in cmd for p in ["wifi on", "turn on wifi", "enable wifi", "connect wifi"]):
        os.system('netsh interface set interface "Wi-Fi" enabled')
        return "WiFi interface enabled.", True

    # ── Windows Settings & Admin Tools ──
    if "control panel" in cmd:
        subprocess.Popen(["control.exe"])
        return "Opening Control Panel.", True

    if "windows settings" in cmd or ("open settings" in cmd and "bluetooth" not in cmd
                                     and "display" not in cmd):
        subprocess.Popen(["start", "ms-settings:"], shell=True)
        return "Opening Windows Settings.", True

    if "windows update" in cmd or "check for updates" in cmd:
        subprocess.Popen(["start", "ms-settings:windowsupdate"], shell=True)
        return "Opening Windows Update.", True

    if "device manager" in cmd:
        subprocess.Popen(["devmgmt.msc"], shell=True)
        return "Opening Device Manager.", True

    if "disk management" in cmd:
        subprocess.Popen(["diskmgmt.msc"], shell=True)
        return "Opening Disk Management.", True

    if "event viewer" in cmd:
        subprocess.Popen(["eventvwr.msc"], shell=True)
        return "Opening Event Viewer.", True

    if "bluetooth settings" in cmd:
        subprocess.Popen(["start", "ms-settings:bluetooth"], shell=True)
        return "Opening Bluetooth Settings.", True

    if "display settings" in cmd:
        subprocess.Popen(["start", "ms-settings:display"], shell=True)
        return "Opening Display Settings.", True

    if "sound settings" in cmd or "audio settings" in cmd:
        subprocess.Popen(["start", "ms-settings:sound"], shell=True)
        return "Opening Sound Settings.", True

    if "night light" in cmd or "night mode" in cmd:
        subprocess.Popen(["start", "ms-settings:nightlight"], shell=True)
        return "Opening Night Light settings.", True

    if "dark mode" in cmd:
        subprocess.Popen(["start", "ms-settings:colors"], shell=True)
        return "Opening Colour settings for dark mode.", True

    if "privacy settings" in cmd:
        subprocess.Popen(["start", "ms-settings:privacy"], shell=True)
        return "Opening Privacy Settings.", True

    # ── Folder shortcuts ──
    for keyword, folder in _FOLDER_MAP.items():
        if keyword in cmd and ("open" in cmd or "folder" in cmd or "show" in cmd):
            path = os.path.join(os.path.expanduser("~"), folder)
            subprocess.Popen(["explorer.exe", path])
            return f"Opening {folder} folder.", True

    # ── Clipboard ──
    if "read clipboard" in cmd or "what is in my clipboard" in cmd or "clipboard content" in cmd:
        try:
            result = subprocess.run(
                ["powershell", "-command", "Get-Clipboard"],
                capture_output=True, text=True, timeout=5)
            clip = result.stdout.strip()
            return (f"Your clipboard contains: {clip[:200]}" if clip
                    else "Clipboard is empty."), True
        except Exception:
            return "Could not read clipboard.", True

    if "clear clipboard" in cmd:
        os.system("echo.|clip")
        return "Clipboard cleared.", True

    # ── App Store / installs ──
    if "open store" in cmd or "microsoft store" in cmd or "windows store" in cmd:
        subprocess.Popen(["start", "ms-windows-store:"], shell=True)
        return "Opening Microsoft Store.", True

    if "install" in cmd and any(x in cmd for x in ["app", "application", "software", "program"]):
        app_q = re.sub(r'\b(install|app|application|software|program|please|can you)\b',
                       '', cmd).strip()
        if permission_broker.ask(
                "Install Application",
                f"Rey Shinchan wants to search Microsoft Store for:\n  '{app_q}'\nAllow?"):
            webbrowser.open(f"https://apps.microsoft.com/search?query={app_q.replace(' ', '+')}")
            return f"Searching Microsoft Store for: {app_q}", True
        return "Installation cancelled.", True

    # ── Create folder ──
    if "create folder" in cmd or "make folder" in cmd or "new folder" in cmd:
        folder_name = re.sub(
            r'\b(create|make|new|folder|named|called)\b', '', cmd).strip()
        if not folder_name:
            folder_name = "NewFolder"
        path = os.path.join(os.path.expanduser("~"), "Desktop", folder_name)
        if permission_broker.ask(
                "Create Folder",
                f"Rey Shinchan wants to create a folder on your Desktop:\n  {folder_name}\nAllow?"):
            os.makedirs(path, exist_ok=True)
            return f"Folder '{folder_name}' created on your Desktop.", True
        return "Folder creation cancelled.", True

    # ── Screen snip / snipping tool ──
    if "snipping tool" in cmd or "snip" in cmd:
        subprocess.Popen(["SnippingTool.exe"])
        return "Opening Snipping Tool.", True

    # ── Task View / Virtual Desktops ──
    if "task view" in cmd or "virtual desktop" in cmd:
        os.system('powershell -command "$o=New-Object -ComObject WScript.Shell;'
                  '$o.SendKeys(\'^{ESC}\')"')
        subprocess.Popen(["start", "ms-actioncenter:"], shell=True)
        return "Opening Task View.", True

    # ── Notifications / Action Centre ──
    if "action center" in cmd or "notification" in cmd:
        subprocess.Popen(["start", "ms-actioncenter:"], shell=True)
        return "Opening Action Centre.", True

    # ── Open specific app by name (generic fallback) ──
    # Uses Get-StartApps to search ALL installed apps (Store, UWP, games, desktop)
    if cmd.startswith("open ") and len(cmd) > 5 and not _is_closing:
        app_q = cmd[5:].strip()
        if app_q and not any(
                kw in app_q for kw in [
                    "chrome", "firefox", "notepad", "explorer", "calc", "spotify",
                    "camera", "instagram", "whatsapp", "facebook", "twitter",
                    "telegram", "linkedin", "snapchat", "netflix", "gmail",
                    "reddit", "maps", "youtube", "wikipedia", "settings",
                    "store", "paint", "word", "excel", "vscode", "code"]):
            if _launch_app_smart(app_q):
                return f"Opening {app_q}.", True
            else:
                # last resort: try desktop app shortcut via start command
                subprocess.Popen(["start", "", app_q], shell=True,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return f"Trying to open {app_q}.", True

    # ═══════════════════════════════════════════════════════════════
    #   ACCESSIBILITY FEATURES  — for visually impaired / blind users
    # ═══════════════════════════════════════════════════════════════

    # ── Stop speaking immediately ──────────────────────────────────
    if any(p in cmd for p in [
            "stop talking", "stop speaking", "be silent", "quiet please",
            "enough talking", "ok stop talking", "please be quiet",
            "shh", "shhh"]):
        # Clear the TTS queue so no more items are processed
        while not _tts_queue.empty():
            try:
                _tts_queue.get_nowait()
                _tts_queue.task_done()
            except _queue.Empty:
                break
        # Kill the powershell.exe process that is actively speaking
        for proc in psutil.process_iter(['name', 'cmdline']):
            try:
                if (proc.info['name'] and
                        'powershell' in proc.info['name'].lower() and
                        proc.info['cmdline'] and
                        any('Speak' in str(a) for a in proc.info['cmdline'])):
                    proc.kill()
            except Exception:
                pass
        return "Okay, I will be quiet.", True

    # ── Repeat last response ───────────────────────────────────────
    if any(p in cmd for p in [
            "say that again", "repeat that", "repeat please",
            "what did you say", "say again", "can you repeat",
            "repeat yourself", "say it again", "once more"]):
        if _last_response:
            return _last_response, True
        return "I have not said anything yet.", True

    # ── Voice Dictation — type text anywhere (crucial for blind users) ──
    # Trigger: "type hello world"  / "write my name is John"
    _dictate_match = re.match(r'^(?:type|write|dictate|input)\s+(.+)', cmd)
    if _dictate_match:
        text_to_type = _dictate_match.group(1).strip()
        def _do_type(t):
            time.sleep(0.5)
            try:
                import pyautogui
                pyautogui.write(t, interval=0.04)
            except Exception:
                pass
        threading.Thread(target=_do_type, args=(text_to_type,),
                         daemon=True).start()
        return f"Typing: {text_to_type}", True

    # ── Keyboard Navigation — full voice keyboard ──────────────────
    # Essential for blind users who can't see what's on screen
    _key_phrases = {
        "press tab":          "tab",
        "tab key":            "tab",
        "press enter":        "return",
        "enter key":          "return",
        "press space":        "space",
        "press escape":       "escape",
        "escape key":         "escape",
        "cancel key":         "escape",
        "press backspace":    "backspace",
        "backspace key":      "backspace",
        "delete key":         "backspace",
        "press delete":       "delete",
        "press up":           "up",
        "arrow up":           "up",
        "move up":            "up",
        "press down":         "down",
        "arrow down":         "down",
        "move down":          "down",
        "press left":         "left",
        "arrow left":         "left",
        "press right":        "right",
        "arrow right":        "right",
        "select all":         "ctrl+a",
        "copy that":          "ctrl+c",
        "paste that":         "ctrl+v",
        "cut that":           "ctrl+x",
        "undo that":          "ctrl+z",
        "redo that":          "ctrl+y",
        "save file":          "ctrl+s",
        "save this":          "ctrl+s",
        "find text":          "ctrl+f",
        "new tab":            "ctrl+t",
        "close tab":          "ctrl+w",
        "next tab":           "ctrl+tab",
        "previous tab":       "ctrl+shift+tab",
        "full screen":        "f11",
        "scroll up":          "pageup",
        "scroll down":        "pagedown",
        "go to top":          "ctrl+home",
        "go to bottom":       "ctrl+end",
        "go to end":          "ctrl+end",
        "zoom in":            "ctrl+=",
        "zoom out":           "ctrl+-",
        "switch window":      "alt+tab",
        "switch app":         "alt+tab",
        "show desktop":       "win+d",
        "open run dialog":    "win+r",
        "open search":        "win+s",
        "open notification":  "win+a",
        "take screenshot":    "win+shift+s",
        "next item":          "tab",
        "previous item":      "shift+tab",
        "read screen":        "win+ctrl+enter",   # Narrator
        "turn on narrator":   "win+ctrl+enter",
    }
    for phrase, key_combo in _key_phrases.items():
        if cmd == phrase or cmd.endswith(phrase):
            def _do_key(k):
                time.sleep(0.3)
                try:
                    import pyautogui
                    if '+' in k:
                        pyautogui.hotkey(*k.split('+'))
                    else:
                        pyautogui.press(k)
                except Exception:
                    pass
            threading.Thread(target=_do_key, args=(key_combo,),
                             daemon=True).start()
            return f"Pressing {phrase.replace('press ', '')}.", True

    # ── Calculator by voice ────────────────────────────────────────
    # "calculate 25 plus 16"  / "what is 9 times 7" / "100 divided by 4"
    _calc_triggers = ["calculate", "compute", "evaluate", "solve", "what is"]
    _calc_words = {
        " plus ": "+", " minus ": "-", " times ": "*",
        " multiplied by ": "*", " divided by ": "/",
        " over ": "/", " mod ": "%", " power ": "**", " squared": "**2"
    }
    has_calc_trigger = any(t in cmd for t in _calc_triggers)
    has_math_op = bool(re.search(r'\d+\s*[\+\-\*\/]\s*\d+', cmd))
    if has_calc_trigger or has_math_op:
        expr = cmd
        for t in _calc_triggers:
            expr = expr.replace(t, "")
        for word, sym in _calc_words.items():
            expr = expr.replace(word, sym)
        # Keep only safe math characters
        expr_clean = re.sub(r'[^0-9\+\-\*\/\.\(\)\s\%\*]', '', expr).strip()
        if expr_clean:
            try:
                result = eval(expr_clean, {"__builtins__": {}})  # sandboxed
                result = round(result, 6) if isinstance(result, float) else result
                return f"The answer is {result}.", True
            except Exception:
                pass   # fall through to AI brain

    # ── Reminder / Alarm by voice ──────────────────────────────────
    # "remind me in 5 minutes to drink water"
    # "set a timer for 10 minutes"
    # "set alarm for 30 seconds"
    _rem_match = re.search(
        r'(?:remind me in|set (?:a )?(?:timer|alarm) for|alert me in|'
        r'notify me in)\s+(\d+)\s*(second|minute|hour)s?\s*(?:to\s+(.+))?',
        cmd)
    if _rem_match:
        amount   = int(_rem_match.group(1))
        unit     = _rem_match.group(2)
        message  = (_rem_match.group(3) or "your reminder").strip(" .")
        secs     = amount * {"second": 1, "minute": 60, "hour": 3600}[unit]
        label    = f"{amount} {unit}{'s' if amount > 1 else ''}"
        def _fire_reminder(msg, lbl, delay):
            time.sleep(delay)
            alert = f"Reminder! {lbl} have passed. {msg.capitalize()}."
            signals.companion_state.emit("SPEAKING", alert)
            speak(alert)
            threading.Timer(
                max(2.5, len(alert) * 0.055),
                lambda: signals.companion_state.emit("IDLE", "")
            ).start()
        threading.Thread(target=_fire_reminder,
                         args=(message, label, secs), daemon=True).start()
        return f"Done. I will remind you in {label} to {message}.", True

    # ── Spell a word ───────────────────────────────────────────────
    _spell_match = re.match(
        r'(?:how do you spell|spelling of|spell)\s+(.+)', cmd)
    if _spell_match:
        word = _spell_match.group(1).strip()
        if word:
            spelled = ",  ".join(word.upper())
            return f"{word} is spelled: {spelled}.", True

    # ── What window / app is currently active ─────────────────────
    if any(p in cmd for p in [
            "what is open", "what is active", "current window",
            "what window is this", "active app", "what app is open",
            "which window", "what program"]):
        try:
            res = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 "Get-Process | Where-Object {$_.MainWindowTitle -ne ''} | "
                 "Sort-Object CPU -Descending | Select-Object -First 1 | "
                 "ForEach-Object { $_.MainWindowTitle }"],
                capture_output=True, text=True, timeout=5)
            win_name = res.stdout.strip()
        except Exception:
            win_name = ""
        if win_name:
            return f"The active window is: {win_name}.", True
        return "I could not detect the active window right now.", True

    # ── Copy text to clipboard by voice ───────────────────────────
    _copy_match = re.match(r'^copy\s+(.+)', cmd)
    if _copy_match:
        text_to_copy = _copy_match.group(1).strip()
        if text_to_copy:
            try:
                subprocess.run(
                    ["powershell", "-Command",
                     f"Set-Clipboard -Value '{text_to_copy.replace(chr(39), '')}'"],
                    timeout=5, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL)
                return f"Copied to clipboard: {text_to_copy}", True
            except Exception:
                return "Could not copy to clipboard.", True

    # ── Set volume to exact level ──────────────────────────────────
    _vol_set = re.search(r'(?:set volume|volume)\s+(?:to\s+)?(\d{1,3})', cmd)
    if _vol_set and "up" not in cmd and "down" not in cmd:
        level = max(0, min(100, int(_vol_set.group(1))))
        # nircmd.exe gives the cleanest result if available
        nircmd = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "nircmd.exe")
        if os.path.exists(nircmd):
            subprocess.run(
                [nircmd, "setsysvolume", str(int(level / 100 * 65535))],
                timeout=5, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)
        else:
            # Fallback — PowerShell Core Audio API
            ps_vol = (
                "Add-Type -AssemblyName System.Runtime.WindowsRuntime; "
                "$vol=[Windows.Media.Audio.AudioGraph,Windows.Media,"
                "ContentType=WindowsRuntime]; "
                f"(New-Object -ComObject WScript.Shell).SendKeys([char]174)"
            )
            # Best-effort: adjust via repeated key presses to approximate level
            _send_media_key(173)  # mute/unmute to reset
        return f"Volume set to {level} percent.", True

    # ── Help — speak all available voice commands ──────────────────
    if any(p in cmd for p in [
            "what can you do", "help me", "list commands",
            "show commands", "what commands", "guide me",
            "how to use you", "what do you know", "your features",
            "help", "commands"]):
        user = config.get("user_name")
        help_text = (
            f"I am your voice partner, always listening for you, {user}. "
            f"Here is what you can do: "
            f"Open or close any app by saying open or close followed by the app name. "
            f"Search Google or YouTube by saying search for, or YouTube followed by your query. "
            f"Type anywhere on screen by saying type followed by your words. "
            f"Press keyboard keys by saying press tab, press enter, press escape, arrow up, arrow down, select all, copy that, or paste that. "
            f"Set a reminder by saying remind me in 5 minutes to drink water. "
            f"Calculate anything by saying calculate 5 times 3. "
            f"Spell a word by saying spell hello. "
            f"Check battery, CPU, RAM, internet, or disk by just asking. "
            f"Control volume with volume up, volume down, or mute. "
            f"Lock, shutdown, restart, or sleep the computer by voice. "
            f"Ask me to read your clipboard by saying read clipboard. "
            f"Ask me anything and I will answer using AI. "
            f"Say stop talking if you want me to be quiet, or say that again if you want me to repeat. "
            f"I am here for you 24 hours a day, 7 days a week. You are never alone."
        )
        return help_text, True

    # No rule matched → hand off to AI
    return None, False


def execute_command(command: str) -> str:
    """Thin wrapper kept for compatibility."""
    response, handled = try_rule_command(command)
    return response if handled else "Command not recognised."


# ─────────────────────────────────────────────
#   AI BRAIN  (Phase 2 core)
# ─────────────────────────────────────────────
class AIBrain:
    """
    Two-stage pipeline:
      1. Rule engine  — fast, reliable, no API needed
      2. LLM fallback — conversational, context-aware
    Supports OpenAI (gpt-3.5-turbo / gpt-4 / gpt-4o)
    and Google Gemini (gemini-1.5-flash / gemini-pro).
    """

    SYSTEM_PROMPT = (
        "You are Rey Shinchan, a dedicated voice-first AI life partner "
        "built specifically to help people who are visually impaired or cannot type. "
        "Your primary users rely entirely on your VOICE to interact with their computer. "
        "You are their eyes, their hands, and their guide — 24 hours a day, 7 days a week. "
        "Address the user warmly as '{user_name}'.\n\n"

        "CORE PERSONALITY:\n"
        "- Be a caring, patient, encouraging life partner — never cold or robotic.\n"
        "- Always confirm out loud what action you are taking, so the user knows what happened.\n"
        "- If you open, close, or change something, SAY it clearly.\n"
        "- Be proactive: offer guidance and suggestions when helpful.\n"
        "- Keep voice replies to 2–4 sentences. Expand only when asked for detail.\n"
        "- Never say 'I cannot see' — you can do anything through voice commands.\n\n"

        "LANGUAGE INTELLIGENCE (critical rule):\n"
        "- Detect the language the user speaks and ALWAYS reply in THAT SAME LANGUAGE.\n"
        "- Tamil → Tamil. Hindi → Hindi. Telugu → Telugu. Kannada → Kannada.\n"
        "- Malayalam, Bengali, Marathi, Gujarati → always reply in their language.\n"
        "- Mixed language (Tanglish, Hinglish, Tenglish) → match their mix naturally.\n"
        "- English input → English reply.\n\n"

        "ACCESSIBILITY RULES:\n"
        "- After every action, speak a clear confirmation so the user knows it succeeded.\n"
        "- If something fails, explain what went wrong in simple words and suggest an alternative.\n"
        "- When the user seems confused, gently offer the 'help' command to list options.\n"
        "- Never assume the user can see the screen — describe everything verbally.\n\n"

        "You can trigger system actions by appending a command tag at the END of your response:\n"
        "[CMD:OPEN_CHROME]  [CMD:OPEN_NOTEPAD]  [CMD:OPEN_EXPLORER]  [CMD:OPEN_CALC]\n"
        "[CMD:OPEN_VSCODE]  [CMD:OPEN_TASKMANAGER]  [CMD:OPEN_PAINT]\n"
        "[CMD:SCREENSHOT]   [CMD:LOCK]  [CMD:MUTE]  [CMD:VOL_UP]  [CMD:VOL_DOWN]\n"
        "[CMD:SEARCH:<query>]  [CMD:YOUTUBE:<query>]\n"
        "Only append a CMD tag when the user explicitly asks for that action. "
        "Never invent tags. If no system action is needed, just respond naturally."
    )

    CMD_MAP = {
        "OPEN_CHROME":      lambda: subprocess.Popen(["start", "chrome"], shell=True),
        "OPEN_NOTEPAD":     lambda: subprocess.Popen(["notepad.exe"]),
        "OPEN_EXPLORER":    lambda: subprocess.Popen(["explorer.exe"]),
        "OPEN_CALC":        lambda: subprocess.Popen(["calc.exe"]),
        "OPEN_VSCODE":      lambda: subprocess.Popen(["code"], shell=True),
        "OPEN_TASKMANAGER": lambda: subprocess.Popen(["taskmgr.exe"]),
        "OPEN_PAINT":       lambda: subprocess.Popen(["mspaint.exe"]),
        "SCREENSHOT": lambda: __import__("pyautogui").screenshot(
            os.path.join(os.path.expanduser("~"), "Desktop",
                         f"rey_shinchan_ss_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.png")),
        "LOCK":    lambda: os.system("rundll32.exe user32.dll,LockWorkStation"),
        "MUTE":    lambda: os.system(
            "powershell -c \"$o=New-Object -ComObject WScript.Shell;$o.SendKeys([char]173)\""),
        "VOL_UP":  lambda: os.system(
            "powershell -c \"$o=New-Object -ComObject WScript.Shell;$o.SendKeys([char]175)\""),
        "VOL_DOWN": lambda: os.system(
            "powershell -c \"$o=New-Object -ComObject WScript.Shell;$o.SendKeys([char]174)\""),
    }

    def __init__(self):
        pass  # uses global config and memory

    # ─────────────────────────────────────────
    #  GROQ  (primary — 14,400 free req/day)
    #  Uses OpenAI-compatible client with Groq base URL.
    #  No extra package needed — just the openai library.
    # ─────────────────────────────────────────
    def _call_groq(self, user_input: str) -> str:
        """Call Groq cloud API. Returns response text or raises on failure."""
        import openai as _openai
        groq_key = config.get("groq_key") or ""
        if not groq_key:
            raise ValueError("no_groq_key")
        client = _openai.OpenAI(
            api_key=groq_key,
            base_url="https://api.groq.com/openai/v1"
        )
        history = memory.get_recent(config.get("max_memory"))
        system  = self.SYSTEM_PROMPT.replace("{user_name}", config.get("user_name"))
        messages = [{"role": "system", "content": system}]
        messages.extend(history)
        messages.append({"role": "user", "content": user_input})
        model_name = config.get("groq_model") or "llama-3.3-70b-versatile"
        resp = client.chat.completions.create(
            model=model_name,
            messages=messages,
            max_tokens=400,
            temperature=0.7,
        )
        return resp.choices[0].message.content.strip()

    # ─────────────────────────────────────────
    #  GEMINI  (backup — rotates up to 3 keys)
    # ─────────────────────────────────────────
    def _call_gemini(self, user_input: str) -> str:
        """Try each configured Gemini key in order. Returns response text."""
        import google.generativeai as genai
        # Build list of non-empty keys to try
        all_keys = [
            config.get("api_key"),
            config.get("gemini_key_2"),
            config.get("gemini_key_3"),
        ]
        gemini_keys = [k for k in all_keys if k and k.strip()]
        if not gemini_keys:
            return ("No Gemini API key found. "
                    "Please add your key in Settings, or add a Groq key for unlimited free use.")

        model_name = config.get("model") or "gemini-2.0-flash"
        history    = memory.get_recent(config.get("max_memory"))
        system     = self.SYSTEM_PROMPT.replace("{user_name}", config.get("user_name"))
        ctx = system + "\n\nConversation so far:\n"
        for m in history:
            ctx += f"{m['role'].upper()}: {m['content']}\n"
        ctx += f"\nUSER: {user_input}"

        last_err = ""
        for key_idx, api_key in enumerate(gemini_keys):
            genai.configure(api_key=api_key)
            model = genai.GenerativeModel(model_name)
            for attempt in range(2):
                try:
                    response = model.generate_content(ctx)
                    return response.text.strip()
                except Exception as e:
                    last_err = str(e)
                    is_quota = (
                        "429" in last_err or
                        "quota" in last_err.lower() or
                        "rate" in last_err.lower() or
                        "exhausted" in last_err.lower()
                    )
                    if is_quota and attempt == 0:
                        # First retry on same key
                        delay_m = re.search(
                            r'retry_delay["\s:{}]+seconds["\s:]+(\d+)', last_err)
                        wait = int(delay_m.group(1)) if delay_m else 4
                        wait = max(wait, 4)
                        # Try next key immediately if one is available
                        if key_idx + 1 < len(gemini_keys):
                            break  # skip to next key — no waiting
                        signals.companion_state.emit("THINKING",
                            f"Key {key_idx+1} limit hit. Retrying in {wait}s…")
                        time.sleep(wait)
                        continue
                    if is_quota:
                        break   # this key is exhausted, try next
                    # Non-quota error (network, invalid key, etc.)
                    return f"Gemini error: {last_err[:120]}"

        # All keys exhausted
        key_count = len(gemini_keys)
        return (
            f"All {key_count} Gemini key{'s' if key_count > 1 else ''} "
            f"have hit their quota. Add a Groq key in Settings for unlimited free responses, "
            f"or wait a minute and try again."
        )

    # ─────────────────────────────────────────
    #  OPENAI  (optional pay-as-you-go)
    # ─────────────────────────────────────────
    def _call_openai(self, user_input: str) -> str:
        try:
            import openai as _openai
            client = _openai.OpenAI(api_key=config.get("api_key"))
            history = memory.get_recent(config.get("max_memory"))
            system = self.SYSTEM_PROMPT.replace("{user_name}", config.get("user_name"))
            messages = [{"role": "system", "content": system}]
            messages.extend(history)
            messages.append({"role": "user", "content": user_input})
            resp = client.chat.completions.create(
                model=config.get("model"),
                messages=messages,
                max_tokens=400,
                temperature=0.7,
            )
            return resp.choices[0].message.content.strip()
        except Exception as e:
            return f"OpenAI error: {e}"

    # ── Parse CMD tag ─────────────────────────
    @staticmethod
    def _extract_cmd(text: str):
        """Return (clean_text, cmd_str_or_None)."""
        match = re.search(r'\[CMD:([^\]]+)\]', text)
        if match:
            cmd = match.group(1)
            clean = re.sub(r'\s*\[CMD:[^\]]+\]', '', text).strip()
            return clean, cmd
        return text, None

    # ── Execute CMD tag ───────────────────────
    @staticmethod
    def _run_cmd_tag(cmd: str):
        """Execute a CMD tag string returned by the LLM."""
        upper = cmd.upper()
        if upper.startswith("SEARCH:"):
            webbrowser.open(
                f"https://www.google.com/search?q={upper[7:].strip().lower()}")
        elif upper.startswith("YOUTUBE:"):
            webbrowser.open(
                f"https://www.youtube.com/results?search_query={upper[8:].strip().lower()}")
        elif upper in AIBrain.CMD_MAP:
            try:
                AIBrain.CMD_MAP[upper]()
            except Exception:
                pass

    # ─────────────────────────────────────────
    #  MAIN PIPELINE
    # ─────────────────────────────────────────
    def process(self, user_input: str) -> str:
        """Full pipeline: rules → Groq → Gemini (key rotation) → OpenAI."""
        memory.add_message("user", user_input)

        response, handled = try_rule_command(user_input)
        if handled:
            memory.add_message("assistant", response)
            return response

        if not config.get("ai_enabled"):
            msg = "AI brain is disabled. Enable it in Settings or use a direct command."
            memory.add_message("assistant", msg)
            return msg

        if not is_online():
            msg = "I am currently offline. Connect to the internet for AI responses."
            memory.add_message("assistant", msg)
            return msg

        signals.status_update.emit("Thinking…")
        provider = config.get("api_provider")

        # ── Cascade: try Groq first (highest free quota), then Gemini ──
        groq_key  = config.get("groq_key") or ""
        raw       = None
        used_groq = False

        if groq_key:
            try:
                signals.companion_state.emit("THINKING", "Let me think…")
                raw = self._call_groq(user_input)
                used_groq = True
            except Exception as e:
                err = str(e)
                if "no_groq_key" not in err:
                    # Groq failed (rate limit or error) — fall through to Gemini
                    signals.companion_state.emit("THINKING",
                        "Switching to backup AI…")

        if raw is None:
            # Use selected provider or default to Gemini
            if provider == "openai":
                raw = self._call_openai(user_input)
            else:
                raw = self._call_gemini(user_input)

        clean, cmd_tag = self._extract_cmd(raw)
        if cmd_tag:
            self._run_cmd_tag(cmd_tag)

        memory.add_message("assistant", clean)
        return clean


# Module-level brain instance
brain = AIBrain()


# ─────────────────────────────────────────────
#   AUTO-LISTEN STATE
# ─────────────────────────────────────────────
_auto_listen_active = True   # start always-on
_auto_listen_lock   = threading.Lock()

def _set_auto_listen(active: bool):
    global _auto_listen_active
    with _auto_listen_lock:
        _auto_listen_active = active
    signals.status_update.emit("Auto-listen ON" if active else "Idle (paused)")
    signals.companion_state.emit(
        "LISTENING" if active else "IDLE",
        "Always ready!" if active else "Idle"
    )

def is_auto_listen() -> bool:
    with _auto_listen_lock:
        return _auto_listen_active and config.get("auto_listen")


# ─────────────────────────────────────────────
#   SIGNALS (thread-safe GUI updates)
# ─────────────────────────────────────────────
class ReyShinchanSignals(QObject):
    status_update    = pyqtSignal(str)
    log_update       = pyqtSignal(str, str)   # (source, message)
    listening_state  = pyqtSignal(bool)
    wake_word_heard  = pyqtSignal()           # fired when speech detected
    companion_state  = pyqtSignal(str, str)   # (state, text)


signals = ReyShinchanSignals()


# ─────────────────────────────────────────────
#   VOICE LISTENER (runs in background thread)
# ─────────────────────────────────────────────
def _process_input(user_input: str):
    """Shared handler for both voice and text input."""
    signals.log_update.emit("YOU", user_input)
    signals.status_update.emit("Processing…")
    signals.companion_state.emit("THINKING", "Let me think…")

    response = brain.process(user_input)

    signals.log_update.emit("REY", response)
    signals.status_update.emit("Idle")
    signals.listening_state.emit(False)

    # Build a clean, speakable version of the response.
    # API/system error messages can be very long; shorten for TTS.
    spoken = response
    if re.search(r'(openai|gemini)\s+error', spoken, re.I):
        if "401" in spoken or "api key" in spoken.lower() or "invalid_api_key" in spoken:
            spoken = ("I couldn't reach the AI service. "
                      "Please check your API key in Settings.")
        elif "429" in spoken or "quota" in spoken.lower():
            spoken = "The AI service rate limit was hit. Please try again shortly."
        elif "timeout" in spoken.lower():
            spoken = "The AI service timed out. Please try again."
        else:
            spoken = "There was an AI service error. Please check Settings."

    signals.companion_state.emit("SPEAKING", spoken)
    speak(spoken)
    # Return to idle after speaking (estimate: ~55 ms per char, min 2.5 s)
    threading.Timer(max(2.5, len(spoken) * 0.055),
                    lambda: signals.companion_state.emit("IDLE", "")).start()


# ── Optimal speech recognition settings ──────────────────────────────────
# dynamic_energy_threshold OFF + fixed floor prevents threshold from dropping
# to noise level (51) which causes Google to receive silence instead of speech.
_SR_ENERGY    = 400
_SR_PAUSE     = 0.8
_SR_LANG      = "en-IN"

def _make_recognizer() -> sr.Recognizer:
    r = sr.Recognizer()
    r.energy_threshold         = _SR_ENERGY
    r.dynamic_energy_threshold = False
    r.pause_threshold          = _SR_PAUSE
    r.operation_timeout        = None
    return r


def listen_once():
    """Listen for one voice command and pass to brain.process()."""
    signals.listening_state.emit(True)
    signals.status_update.emit("Listening…")
    signals.companion_state.emit("LISTENING", "I'm listening…")

    r = _make_recognizer()
    try:
        with sr.Microphone() as source:
            r.adjust_for_ambient_noise(source, duration=0.8)
            # Enforce floor even after calibration
            if r.energy_threshold < _SR_ENERGY:
                r.energy_threshold = _SR_ENERGY
            audio = r.listen(source, timeout=8, phrase_time_limit=12)
        signals.status_update.emit("Recognising speech…")
        command = r.recognize_google(audio, language=_SR_LANG)
    except sr.WaitTimeoutError:
        signals.status_update.emit("Idle — No speech detected.")
        signals.listening_state.emit(False)
        signals.companion_state.emit("IDLE", "")
        return
    except sr.UnknownValueError:
        signals.status_update.emit("Idle — Could not understand.")
        signals.listening_state.emit(False)
        signals.companion_state.emit("IDLE", "")
        return
    except OSError:
        signals.status_update.emit("Microphone unavailable.")
        signals.listening_state.emit(False)
        signals.companion_state.emit("IDLE", "")
        return
    except Exception as e:
        msg = str(e)
        signals.status_update.emit(
            "Microphone error — retrying." if "NoneType" in msg or "Stream" in msg
            else f"Error: {msg[:80]}")
        signals.listening_state.emit(False)
        signals.companion_state.emit("IDLE", "")
        return

    _process_input(command)


# ─────────────────────────────────────────────
#   CONTINUOUS AUTO-LISTEN LOOP
# ─────────────────────────────────────────────
_auto_loop_started = False

def _auto_listen_loop():
    """
    Always-on listening loop.
    Listens continuously. After each command is processed it
    immediately starts listening again — unless user said 'go to idle'.
    """
    while True:
        if not is_auto_listen() or _busy.is_set():
            time.sleep(0.4)
            continue

        signals.status_update.emit("Listening…")
        signals.companion_state.emit("LISTENING", "Always ready!")
        signals.listening_state.emit(True)

        r = _make_recognizer()
        try:
            with sr.Microphone() as source:
                r.adjust_for_ambient_noise(source, duration=0.5)
                # Enforce energy floor after calibration
                if r.energy_threshold < _SR_ENERGY:
                    r.energy_threshold = _SR_ENERGY
                audio = r.listen(source, timeout=6, phrase_time_limit=14)

            text = r.recognize_google(audio, language=_SR_LANG).strip()
            if text:
                _busy.set()
                signals.wake_word_heard.emit()
                try:
                    _process_input(text)
                finally:
                    _busy.clear()

        except sr.WaitTimeoutError:
            pass
        except sr.UnknownValueError:
            pass
        except OSError:
            time.sleep(1)
        except Exception:
            time.sleep(1)


def start_auto_listen_engine():
    global _auto_loop_started
    if _auto_loop_started:
        return
    _auto_loop_started = True
    threading.Thread(target=_auto_listen_loop, daemon=True, name="AutoListen").start()


def activate_voice():
    """
    Manual trigger for voice (button / double-click).
    Since auto-listen is always running, just speak a prompt and
    let the auto-listen loop pick up the next utterance naturally.
    This avoids conflicting microphone access.
    """
    if not is_auto_listen():
        # Auto-listen paused — do a one-shot listen
        if not _busy.is_set():
            threading.Thread(target=listen_once, daemon=True).start()
    else:
        # Already listening — give audio + visual cue
        signals.companion_state.emit("LISTENING", "Yes? I'm listening!")
        signals.status_update.emit("Listening… go ahead!")
        speak("I'm listening, go ahead!")


def submit_text(text: str):
    text = text.strip()
    if text:
        threading.Thread(target=_process_input, args=(text,), daemon=True).start()


# ─────────────────────────────────────────────
#   WINDOWS AUTO-START (registry)
# ─────────────────────────────────────────────
APP_NAME = "ReyShinchan"
STARTUP_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def set_autostart(enable: bool):
    """Add or remove the app from Windows startup registry."""
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, STARTUP_KEY,
            0, winreg.KEY_SET_VALUE
        )
        if enable:
            exe  = sys.executable
            script = os.path.abspath(__file__)
            cmd  = f'"{exe}" "{script}"'
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, cmd)
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
        config.set("autostart", enable)
    except Exception as e:
        print(f"Autostart registry error: {e}")


# ─────────────────────────────────────────────
#   DARK FUTURISTIC STYLESHEET
# ─────────────────────────────────────────────
DARK_STYLE = """
QWidget {
    background-color: #0a0f1e;
    color: #00d4ff;
    font-family: 'Segoe UI', Consolas, monospace;
}
QLabel#title {
    color: #00d4ff;
    font-size: 28px;
    font-weight: bold;
    letter-spacing: 6px;
}
QLabel#subtitle {
    color: #4a7fa5;
    font-size: 11px;
    letter-spacing: 3px;
}
QLabel#status_label {
    color: #00ff88;
    font-size: 13px;
    font-weight: bold;
    padding: 4px;
}
QLabel#stat_label {
    color: #a0b8cc;
    font-size: 11px;
    padding: 2px 6px;
}
QLabel#mode_label, QLabel#ai_label {
    font-size: 11px;
    padding: 2px 8px;
    border-radius: 8px;
}
QPushButton#activate_btn {
    background-color: #003d5c;
    color: #00d4ff;
    border: 2px solid #00d4ff;
    border-radius: 10px;
    font-size: 15px;
    font-weight: bold;
    padding: 12px 30px;
    letter-spacing: 2px;
}
QPushButton#activate_btn:hover {
    background-color: #005580;
    color: #ffffff;
}
QPushButton#activate_btn:pressed {
    background-color: #00d4ff;
    color: #0a0f1e;
}
QPushButton#activate_btn:disabled {
    background-color: #1a2030;
    color: #3a5060;
    border: 2px solid #1a3040;
}
QPushButton#send_btn {
    background-color: #003d5c;
    color: #00d4ff;
    border: 2px solid #00d4ff;
    border-radius: 8px;
    font-size: 13px;
    font-weight: bold;
    padding: 8px 18px;
}
QPushButton#send_btn:hover  { background-color: #005580; }
QPushButton#send_btn:pressed{ background-color: #00d4ff; color: #0a0f1e; }
QPushButton#settings_btn {
    background-color: #141c2a;
    color: #4a7fa5;
    border: 1px solid #1a3050;
    border-radius: 8px;
    font-size: 12px;
    padding: 6px 14px;
}
QPushButton#settings_btn:hover { background-color: #1e2a3e; color: #00d4ff; }
QPushButton#clear_btn {
    background-color: #1a1220;
    color: #c060a0;
    border: 1px solid #6030a0;
    border-radius: 8px;
    font-size: 12px;
    padding: 6px 14px;
}
QPushButton#clear_btn:hover { background-color: #2a1030; }
QTextEdit#log_area {
    background-color: #060c18;
    color: #a0c8e0;
    border: 1px solid #1a3050;
    border-radius: 8px;
    font-size: 11px;
    font-family: Consolas, monospace;
    padding: 6px;
}
QLineEdit#text_input {
    background-color: #060c18;
    color: #e0f0ff;
    border: 1px solid #1a4060;
    border-radius: 8px;
    font-size: 13px;
    padding: 8px 12px;
    selection-background-color: #00d4ff;
    selection-color: #0a0f1e;
}
QLineEdit#text_input:focus {
    border: 1px solid #00d4ff;
}
QFrame#divider {
    color: #1a3050;
}
QProgressBar {
    background-color: #0d1a2a;
    border: 1px solid #1a3050;
    border-radius: 4px;
    height: 8px;
    text-align: center;
    font-size: 9px;
    color: #4a7fa5;
}
QProgressBar::chunk {
    background-color: #00d4ff;
    border-radius: 3px;
}
/* Settings dialog */
QDialog {
    background-color: #0d1525;
    color: #00d4ff;
}
QLineEdit, QComboBox {
    background-color: #060c18;
    color: #c0d8ff;
    border: 1px solid #1a4060;
    border-radius: 6px;
    padding: 5px 10px;
    font-size: 12px;
}
QComboBox QAbstractItemView {
    background-color: #0d1525;
    color: #c0d8ff;
    selection-background-color: #003d5c;
}
QCheckBox {
    color: #a0c8e0;
    font-size: 12px;
    spacing: 8px;
}
QCheckBox::indicator {
    width: 16px; height: 16px;
    border: 1px solid #1a4060;
    border-radius: 3px;
    background: #060c18;
}
QCheckBox::indicator:checked {
    background: #00d4ff;
}
QDialogButtonBox QPushButton {
    background-color: #003d5c;
    color: #00d4ff;
    border: 1px solid #00d4ff;
    border-radius: 6px;
    padding: 6px 16px;
    font-size: 12px;
}
QDialogButtonBox QPushButton:hover { background-color: #005580; }
"""


# ─────────────────────────────────────────────
#   PERMISSION DIALOG  (styled, companion-themed)
# ─────────────────────────────────────────────
class PermissionDialog(QDialog):
    """Shown before sensitive system actions — user must Allow or Deny."""

    def __init__(self, action_name: str, description: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Permission Required — Rey Shinchan")
        self.setWindowFlags(
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Dialog
        )
        self.setFixedWidth(430)
        self.setStyleSheet(DARK_STYLE + """
            QDialog { border: 2px solid #ff8800; }
            QLabel#perm_title {
                color: #ff8800; font-size: 16px; font-weight: bold;
            }
            QLabel#perm_icon  { font-size: 40px; }
            QLabel#perm_desc  { color: #c0d8ff; font-size: 12px; line-height: 1.5; }
            QLabel#perm_from  { color: #4a7fa5; font-size: 11px; }
        """)
        self._build(action_name, description)

    def _build(self, action_name: str, description: str):
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(24, 20, 24, 20)

        # Icon header row
        icon_row = QHBoxLayout()
        icon_lbl = QLabel("🔐")
        icon_lbl.setObjectName("perm_icon")
        icon_row.addWidget(icon_lbl)
        icon_row.addStretch()
        layout.addLayout(icon_row)

        # Action title
        title_lbl = QLabel(action_name)
        title_lbl.setObjectName("perm_title")
        title_lbl.setWordWrap(True)
        layout.addWidget(title_lbl)

        # Source
        from_lbl = QLabel("Requested by:  Rey Shinchan AI  ·  your personal assistant")
        from_lbl.setObjectName("perm_from")
        layout.addWidget(from_lbl)

        # Divider
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setObjectName("divider")
        layout.addWidget(sep)

        # Description
        desc_lbl = QLabel(description)
        desc_lbl.setObjectName("perm_desc")
        desc_lbl.setWordWrap(True)
        layout.addWidget(desc_lbl)

        layout.addSpacing(6)

        # Buttons row
        btn_row = QHBoxLayout()
        deny_btn = QPushButton("  ✕  Deny")
        deny_btn.setObjectName("clear_btn")
        deny_btn.setMinimumHeight(38)
        deny_btn.clicked.connect(self.reject)

        allow_btn = QPushButton("  ✓  Allow")
        allow_btn.setObjectName("activate_btn")
        allow_btn.setMinimumHeight(38)
        allow_btn.clicked.connect(self.accept)

        btn_row.addWidget(deny_btn)
        btn_row.addWidget(allow_btn)
        layout.addLayout(btn_row)


# ─────────────────────────────────────────────
#   SETTINGS DIALOG
# ─────────────────────────────────────────────
class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Rey Shinchan — Settings")
        self.setMinimumWidth(440)
        self.setStyleSheet(DARK_STYLE)
        self._build()

    def _build(self):
        layout = QFormLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(20, 20, 20, 20)

        title = QLabel("CONFIGURATION")
        title.setObjectName("subtitle")
        layout.addRow(title)

        # User name
        self.name_edit = QLineEdit(config.get("user_name"))
        layout.addRow(QLabel("Your name:"), self.name_edit)

        # ── Groq — FREE unlimited key (recommended) ────────────────────
        groq_header = QLabel("🟢  GROQ API  — Free 14,400 requests / day (recommended)")
        groq_header.setObjectName("subtitle")
        layout.addRow(groq_header)

        self.groq_key_edit = QLineEdit(config.get("groq_key"))
        self.groq_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.groq_key_edit.setPlaceholderText("gsk_...  — get free key at console.groq.com")
        layout.addRow(QLabel("Groq Key:"), self.groq_key_edit)

        show_groq_btn = QPushButton("Show")
        show_groq_btn.setObjectName("settings_btn")
        show_groq_btn.setMaximumWidth(70)
        show_groq_btn.clicked.connect(
            lambda: (
                self.groq_key_edit.setEchoMode(QLineEdit.EchoMode.Normal)
                if self.groq_key_edit.echoMode() == QLineEdit.EchoMode.Password
                else self.groq_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
            ))
        layout.addRow("", show_groq_btn)

        # ── Gemini — Up to 3 rotating keys ──────────────────────────────
        gemini_header = QLabel("🟡  GEMINI API  — Backup keys (rotate on quota hit)")
        gemini_header.setObjectName("subtitle")
        layout.addRow(gemini_header)

        # API Provider (legacy — keep for OpenAI users)
        self.provider_combo = QComboBox()
        self.provider_combo.addItems(["groq", "gemini", "openai"])
        self.provider_combo.setCurrentText(config.get("api_provider"))
        self.provider_combo.currentTextChanged.connect(self._update_model_list)
        layout.addRow(QLabel("Provider (fallback):"), self.provider_combo)

        # Model
        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        layout.addRow(QLabel("Gemini / OpenAI Model:"), self.model_combo)
        self._update_model_list(config.get("api_provider"))
        self.model_combo.setCurrentText(config.get("model"))

        # Gemini key 1
        self.key_edit = QLineEdit(config.get("api_key"))
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit.setPlaceholderText("AIzaSy...  (aistudio.google.com — free)")
        layout.addRow(QLabel("Gemini Key 1:"), self.key_edit)

        # Gemini key 2
        self.key2_edit = QLineEdit(config.get("gemini_key_2"))
        self.key2_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key2_edit.setPlaceholderText("AIzaSy...  2nd key (used if key 1 hits limit)")
        layout.addRow(QLabel("Gemini Key 2:"), self.key2_edit)

        # Gemini key 3
        self.key3_edit = QLineEdit(config.get("gemini_key_3"))
        self.key3_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key3_edit.setPlaceholderText("AIzaSy...  3rd key (used if keys 1–2 hit limit)")
        layout.addRow(QLabel("Gemini Key 3:"), self.key3_edit)

        # Show/hide all Gemini keys toggle
        self.show_key_btn = QPushButton("Show Gemini keys")
        self.show_key_btn.setObjectName("settings_btn")
        self.show_key_btn.clicked.connect(self._toggle_key_visibility)
        layout.addRow("", self.show_key_btn)

        # Max memory
        self.mem_edit = QLineEdit(str(config.get("max_memory")))
        self.mem_edit.setPlaceholderText("Number of messages (1-50)")
        layout.addRow(QLabel("Memory depth:"), self.mem_edit)

        # AI enabled
        self.ai_check = QCheckBox("Enable AI brain (LLM fallback)")
        self.ai_check.setChecked(config.get("ai_enabled"))
        layout.addRow("", self.ai_check)

        # Wake word
        self.wake_check = QCheckBox('Enable wake word  (\'Rey Shinchan\' / \'Hey Rey\')')
        self.wake_check.setChecked(config.get("wake_word_enabled"))
        layout.addRow("", self.wake_check)

        # Always-on auto-listen
        self.auto_listen_check = QCheckBox("Always-on listening  (no wake word needed)")
        self.auto_listen_check.setChecked(config.get("auto_listen"))
        layout.addRow("", self.auto_listen_check)

        # Auto-start with Windows
        self.autostart_check = QCheckBox("Launch Rey Shinchan when Windows starts")
        self.autostart_check.setChecked(config.get("autostart"))
        layout.addRow("", self.autostart_check)

        # Divider
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setObjectName("divider")
        layout.addRow(sep)

        # Clear memory button
        clear_btn = QPushButton("🗑  Clear Conversation Memory")
        clear_btn.setObjectName("clear_btn")
        clear_btn.clicked.connect(self._clear_memory)
        layout.addRow("", clear_btn)

        # Memory stats
        self.stats_label = QLabel("")
        self.stats_label.setObjectName("subtitle")
        self._refresh_stats()
        layout.addRow("", self.stats_label)

        # Dialog buttons
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save |
            QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def _update_model_list(self, provider: str):
        self.model_combo.clear()
        if provider == "groq":
            self.model_combo.addItems([
                "llama-3.3-70b-versatile",
                "llama-3.1-8b-instant",
                "llama3-70b-8192",
                "mixtral-8x7b-32768",
                "gemma2-9b-it",
            ])
        elif provider == "openai":
            self.model_combo.addItems([
                "gpt-3.5-turbo", "gpt-4", "gpt-4o", "gpt-4o-mini"
            ])
        else:  # gemini
            self.model_combo.addItems([
                "gemini-2.0-flash", "gemini-2.0-flash-lite",
                "gemini-1.5-flash", "gemini-1.5-pro",
            ])

    def _toggle_key_visibility(self):
        if self.key_edit.echoMode() == QLineEdit.EchoMode.Password:
            for w in (self.key_edit, self.key2_edit, self.key3_edit):
                w.setEchoMode(QLineEdit.EchoMode.Normal)
            self.show_key_btn.setText("Hide Gemini keys")
        else:
            for w in (self.key_edit, self.key2_edit, self.key3_edit):
                w.setEchoMode(QLineEdit.EchoMode.Password)
            self.show_key_btn.setText("Show Gemini keys")

    def _clear_memory(self):
        memory.clear()
        self._refresh_stats()
        QMessageBox.information(self, "Memory Cleared",
                                "All conversation memory has been erased.")

    def _refresh_stats(self):
        total = memory.get_total_count()
        self.stats_label.setText(f"Stored messages: {total}")

    def _save(self):
        config.set("user_name",        self.name_edit.text().strip() or "Sir")
        config.set("api_provider",      self.provider_combo.currentText())
        config.set("model",             self.model_combo.currentText())
        config.set("api_key",           self.key_edit.text().strip())
        config.set("gemini_key_2",      self.key2_edit.text().strip())
        config.set("gemini_key_3",      self.key3_edit.text().strip())
        config.set("groq_key",          self.groq_key_edit.text().strip())
        config.set("ai_enabled",        self.ai_check.isChecked())
        config.set("wake_word_enabled", self.wake_check.isChecked())
        config.set("auto_listen",       self.auto_listen_check.isChecked())
        _set_auto_listen(self.auto_listen_check.isChecked())
        set_autostart(self.autostart_check.isChecked())
        try:
            depth = int(self.mem_edit.text())
            config.set("max_memory", max(1, min(50, depth)))
        except ValueError:
            pass
        self.accept()


# ─────────────────────────────────────────────
#   MAIN WINDOW
# ─────────────────────────────────────────────
class ReyShinchanWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("REY SHINCHAN  —  AI Assistant  v2.0")
        self.setMinimumSize(660, 780)
        self.setStyleSheet(DARK_STYLE)
        self._build_ui()
        self._connect_signals()
        self._start_timers()
        self._setup_tray()

        # Start always-on continuous listener
        start_auto_listen_engine()

        speak(f"Rey Shinchan online, {config.get('user_name')}. I'm always listening. Just speak anytime!")

    # ── UI BUILD ──────────────────────────────
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(8)
        root.setContentsMargins(20, 16, 20, 16)

        # ─ Header row ─
        header = QHBoxLayout()
        title = QLabel("REY SHINCHAN")
        title.setObjectName("title")
        header.addWidget(title)
        header.addStretch()
        settings_btn = QPushButton("⚙  Settings")
        settings_btn.setObjectName("settings_btn")
        settings_btn.clicked.connect(self._open_settings)
        header.addWidget(settings_btn)
        root.addLayout(header)

        sub = QLabel("YOUR SMART AI ASSISTANT  ·  v2.0  ·  PHASE 2")
        sub.setObjectName("subtitle")
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(sub)

        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setObjectName("divider")
        root.addWidget(line)

        # ─ Status + mode badges ─
        status_row = QHBoxLayout()
        self.status_label = QLabel("Idle — Ready")
        self.status_label.setObjectName("status_label")
        status_row.addWidget(self.status_label)
        status_row.addStretch()

        self.wake_label = QLabel("👂 WAKE ON")
        self.wake_label.setObjectName("mode_label")
        self.wake_label.setStyleSheet(
            "color:#ffcc00;background:#1a1500;border:1px solid #ffcc00;"
            "border-radius:8px;padding:2px 8px;")
        status_row.addWidget(self.wake_label)

        self.ai_label = QLabel("● AI ACTIVE")
        self.ai_label.setObjectName("ai_label")
        status_row.addWidget(self.ai_label)

        self.mode_label = QLabel("● ONLINE")
        self.mode_label.setObjectName("mode_label")
        status_row.addWidget(self.mode_label)
        root.addLayout(status_row)

        # ─ System stats ─
        stats_row = QHBoxLayout()
        self.cpu_label  = QLabel("CPU: 0%");    self.cpu_label.setObjectName("stat_label")
        self.ram_label  = QLabel("RAM: 0%");    self.ram_label.setObjectName("stat_label")
        self.disk_label = QLabel("DISK: 0%");   self.disk_label.setObjectName("stat_label")
        self.bat_label  = QLabel("BAT: N/A");   self.bat_label.setObjectName("stat_label")
        for w in [self.cpu_label, self.ram_label, self.disk_label, self.bat_label]:
            stats_row.addWidget(w)
        stats_row.addStretch()
        root.addLayout(stats_row)

        self.cpu_bar = QProgressBar(); self.cpu_bar.setRange(0, 100); self.cpu_bar.setTextVisible(False)
        self.ram_bar = QProgressBar(); self.ram_bar.setRange(0, 100); self.ram_bar.setTextVisible(False)
        root.addWidget(self.cpu_bar)
        root.addWidget(self.ram_bar)

        # ─ Log label + clear button ─
        log_header = QHBoxLayout()
        log_lbl = QLabel("CONVERSATION LOG")
        log_lbl.setObjectName("subtitle")
        log_header.addWidget(log_lbl)
        log_header.addStretch()
        self.mem_count_label = QLabel("")
        self.mem_count_label.setObjectName("subtitle")
        log_header.addWidget(self.mem_count_label)
        clear_btn = QPushButton("🗑 Clear")
        clear_btn.setObjectName("clear_btn")
        clear_btn.clicked.connect(self._clear_log)
        log_header.addWidget(clear_btn)
        root.addLayout(log_header)

        # ─ Log area ─
        self.log_area = QTextEdit()
        self.log_area.setObjectName("log_area")
        self.log_area.setReadOnly(True)
        self.log_area.setMinimumHeight(300)
        root.addWidget(self.log_area)

        # ─ Text input row ─
        input_row = QHBoxLayout()
        self.text_input = QLineEdit()
        self.text_input.setObjectName("text_input")
        self.text_input.setPlaceholderText("Type a command or question… (or use mic below)")
        self.text_input.returnPressed.connect(self._on_text_submit)
        input_row.addWidget(self.text_input)

        send_btn = QPushButton("Send  ➤")
        send_btn.setObjectName("send_btn")
        send_btn.clicked.connect(self._on_text_submit)
        input_row.addWidget(send_btn)
        root.addLayout(input_row)

        # ─ Activate voice button ─
        self.activate_btn = QPushButton("🎤  ACTIVATE REY SHINCHAN")
        self.activate_btn.setObjectName("activate_btn")
        self.activate_btn.clicked.connect(self._on_activate)
        root.addWidget(self.activate_btn)

        tip = QLabel("VOICE / TEXT: open chrome · what time is it · explain quantum computing · write python code")
        tip.setObjectName("subtitle")
        tip.setAlignment(Qt.AlignmentFlag.AlignCenter)
        tip.setWordWrap(True)
        root.addWidget(tip)

    # ── SIGNAL CONNECTIONS ────────────────────
    def _connect_signals(self):
        signals.status_update.connect(self._set_status)
        signals.log_update.connect(self._append_log)
        signals.listening_state.connect(self._set_listening)
        signals.wake_word_heard.connect(self._on_wake_word_heard)

    # ── TIMERS ────────────────────────────────
    def _start_timers(self):
        self._stat_timer = QTimer(self)
        self._stat_timer.timeout.connect(self._update_stats)
        self._stat_timer.start(1500)

        self._net_timer = QTimer(self)
        self._net_timer.timeout.connect(self._update_mode)
        self._net_timer.start(6000)

        self._mem_timer = QTimer(self)
        self._mem_timer.timeout.connect(self._update_mem_count)
        self._mem_timer.start(5000)

        self._update_stats()
        self._update_mode()
        self._update_ai_badge()
        self._update_mem_count()

    def _update_stats(self):
        cpu = psutil.cpu_percent()
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage("C:\\")
        self.cpu_label.setText(f"CPU: {cpu:.0f}%")
        self.ram_label.setText(f"RAM: {mem.percent:.0f}%")
        self.disk_label.setText(f"DISK: {disk.percent:.0f}%")
        self.cpu_bar.setValue(int(cpu))
        self.ram_bar.setValue(int(mem.percent))
        bat = psutil.sensors_battery()
        if bat:
            icon = "⚡" if bat.power_plugged else "🔋"
            self.bat_label.setText(f"BAT: {icon}{bat.percent:.0f}%")
        else:
            self.bat_label.setText("BAT: N/A")

    def _update_mode(self):
        if is_online():
            self.mode_label.setText("● ONLINE")
            self.mode_label.setStyleSheet(
                "color:#00ff88;background:#001a10;border:1px solid #00ff88;"
                "border-radius:8px;padding:2px 8px;")
        else:
            self.mode_label.setText("● OFFLINE")
            self.mode_label.setStyleSheet(
                "color:#ff6633;background:#1a0a00;border:1px solid #ff6633;"
                "border-radius:8px;padding:2px 8px;")

    def _update_ai_badge(self):
        has_key = bool(config.get("api_key"))
        enabled = config.get("ai_enabled")
        if has_key and enabled:
            provider = config.get("api_provider").upper()
            self.ai_label.setText(f"🤖 {provider} AI")
            self.ai_label.setStyleSheet(
                "color:#cc88ff;background:#120820;border:1px solid #8040c0;"
                "border-radius:8px;padding:2px 8px;")
        else:
            self.ai_label.setText("⚙ RULE MODE")
            self.ai_label.setStyleSheet(
                "color:#a0a0a0;background:#141414;border:1px solid #404040;"
                "border-radius:8px;padding:2px 8px;")
        # Update wake / listen badge
        if config.get("auto_listen") and is_auto_listen():
            self.wake_label.setText("🎙 AUTO-LISTEN")
            self.wake_label.setStyleSheet(
                "color:#00ff88;background:#001a10;border:1px solid #00ff88;"
                "border-radius:8px;padding:2px 8px;")
        elif config.get("wake_word_enabled"):
            self.wake_label.setText("👂 WAKE ON")
            self.wake_label.setStyleSheet(
                "color:#ffcc00;background:#1a1500;border:1px solid #ffcc00;"
                "border-radius:8px;padding:2px 8px;")
        else:
            self.wake_label.setText("PAUSED")
            self.wake_label.setStyleSheet(
                "color:#505050;background:#111111;border:1px solid #303030;"
                "border-radius:8px;padding:2px 8px;")

    def _update_mem_count(self):
        total = memory.get_total_count()
        self.mem_count_label.setText(f"{total} MSGS IN MEMORY")

    # ── WAKE WORD FLASH ───────────────────────
    def _on_wake_word_heard(self):
        """Flash the wake badge bright to confirm detection."""
        self.wake_label.setText("🔴 WAKE ACTIVE!")
        self.wake_label.setStyleSheet(
            "color:#ff4400;background:#200a00;border:1px solid #ff4400;"
            "border-radius:8px;padding:2px 8px;")
        QTimer.singleShot(2000, self._update_ai_badge)

    # ── SYSTEM TRAY ───────────────────────────
    def _setup_tray(self):
        """Create a system tray icon so the app keeps running when closed."""
        # Create a tiny coloured icon programmatically (no external file needed)
        px = QPixmap(16, 16)
        px.fill(QC("#00d4ff"))
        icon = QIcon(px)

        self._tray = QSystemTrayIcon(icon, self)
        self._tray.setToolTip("Rey Shinchan AI — running in background")

        menu = QMenu()
        menu.setStyleSheet(
            "QMenu{background:#0d1525;color:#00d4ff;border:1px solid #1a3050;}"
            "QMenu::item:selected{background:#003d5c;}")

        show_act = menu.addAction("Show / Hide")
        show_act.triggered.connect(self._toggle_visibility)
        menu.addSeparator()
        quit_act = menu.addAction("Quit Rey Shinchan")
        quit_act.triggered.connect(lambda: sys.exit(0))

        self._tray.setContextMenu(menu)
        self._tray.activated.connect(self._tray_activated)
        self._tray.show()

    def _tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._toggle_visibility()

    def _toggle_visibility(self):
        if self.isVisible():
            self.hide()
        else:
            self.show()
            self.raise_()
            self.activateWindow()

    # ── SETTINGS ──────────────────────────────
    def _open_settings(self):
        dlg = SettingsDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._update_ai_badge()
            self._append_log("SYSTEM", "Settings saved.")

    # ── CLOSE → MINIMIZE TO TRAY ──────────────
    def closeEvent(self, event):
        event.ignore()
        self.hide()
        self._tray.showMessage(
            "Rey Shinchan",
            "Still running in background. Say 'Rey Shinchan' anytime!",
            QSystemTrayIcon.MessageIcon.Information,
            3000
        )

    # ── CLEAR LOG ─────────────────────────────
    def _clear_log(self):
        self.log_area.clear()
        memory.clear()
        self._update_mem_count()

    # ── VOICE BUTTON ──────────────────────────
    def _on_activate(self):
        activate_voice()

    # ── TEXT SUBMIT ───────────────────────────
    def _on_text_submit(self):
        text = self.text_input.text().strip()
        if not text:
            return
        self.text_input.clear()
        self._set_listening(True)
        submit_text(text)

    # ── STATUS ────────────────────────────────
    def _set_status(self, text: str):
        self.status_label.setText(text)

    # ── LOG ───────────────────────────────────
    def _append_log(self, source: str, message: str):
        now = datetime.datetime.now().strftime("%H:%M:%S")
        if source == "YOU":
            color, icon = "#ffcc00", "YOU ▶"
        elif source == "SYSTEM":
            color, icon = "#888888", "SYS ℹ"
        else:
            color, icon = "#00d4ff", "REY ◀"
        prefix = (f'<span style="color:#4a7fa5;">[{now}]</span> '
                  f'<span style="color:{color};font-weight:bold;">{icon}</span> ')
        self.log_area.append(prefix + f'<span style="color:#c0d8e8;">{message}</span>')
        sb = self.log_area.verticalScrollBar()
        sb.setValue(sb.maximum())

    # ── LISTENING STATE ───────────────────────
    def _set_listening(self, active: bool):
        self.activate_btn.setEnabled(not active)
        self.text_input.setEnabled(not active)
        self.activate_btn.setText(
            "🔴  LISTENING…" if active else "🎤  ACTIVATE REY SHINCHAN")



# ═════════════════════════════════════════════
#   ANIMATED DESKTOP COMPANION WIDGET
# ═════════════════════════════════════════════
from PyQt6.QtGui import (
    QPainter, QPen, QBrush, QRadialGradient, QLinearGradient,
    QFontMetrics, QPainterPath, QFont as QF, QRegion
)
from PyQt6.QtCore import QRect, QPointF, QRectF

class CompanionWidget(QWidget):
    """
    Floating, always-on-top transparent animated companion.
    States: IDLE · LISTENING · THINKING · SPEAKING · HAPPY
    Features: arms, legs, eyebrows, head-tilt, particles, gestures.
    """

    W, H       = 200, 350
    BUBBLE_MAX_W = 280

    STATE_COLORS = {
        "IDLE":      {"head": "#0d2a3a", "glow": "#00d4ff", "eye": "#00d4ff",  "mouth": "#00a0cc"},
        "LISTENING": {"head": "#1a2800", "glow": "#aaff00", "eye": "#aaff00",  "mouth": "#88cc00"},
        "THINKING":  {"head": "#20083a", "glow": "#cc44ff", "eye": "#cc44ff",  "mouth": "#9922cc"},
        "SPEAKING":  {"head": "#002030", "glow": "#00ffcc", "eye": "#00ffcc",  "mouth": "#00cc99"},
        "HAPPY":     {"head": "#2a1500", "glow": "#ffaa00", "eye": "#ffaa00",  "mouth": "#ff8800"},
    }

    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool |
            Qt.WindowType.WindowDoesNotAcceptFocus   # NEVER steal focus from text inputs
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)  # show without taking focus
        self.setFixedSize(self.W + self.BUBBLE_MAX_W + 30, self.H + 60)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # extra safety

        # Core state
        self._state        = "IDLE"
        self._text         = ""
        self._frame        = 0
        self._blink        = 0
        self._mouth_f      = 0.0
        self._spin         = 0.0
        self._drag_pos     = None
        self._bubble_alpha = 0

        # ── Rich animation vars ──────────────────────────────
        self._arm_l        = 15.0    # left  arm QPainter angle (0 = straight down)
        self._arm_r        = -15.0   # right arm angle
        self._bounce       = 0.0     # vertical body bounce offset
        self._head_tilt    = 0.0     # head left/right tilt degrees
        self._eb_l         = 0.0     # left  eyebrow raise (negative = up)
        self._eb_r         = 0.0     # right eyebrow raise
        self._leg_l        = 0.0     # left  leg swing angle
        self._leg_r        = 0.0     # right leg swing angle
        self._particles: list = []   # celebration particles
        self._intro_active = True    # startup wave animation
        self._intro_f      = 0       # intro frame counter

        screen = QApplication.primaryScreen().availableGeometry()
        self.move(screen.width() - self.W - self.BUBBLE_MAX_W - 50,
                  screen.height() - self.H - 90)

        # Track whether the bubble is visible so we only call setMask on CHANGE
        # (calling setMask every tick cancels OS drag tracking and breaks mouse events)
        self._mask_bubble_on = False
        self._update_mask()   # set initial mask once

        self._anim = QTimer(self)
        self._anim.timeout.connect(self._tick)
        self._anim.start(30)

        signals.companion_state.connect(self._on_state)
        signals.wake_word_heard.connect(lambda: self._on_state("LISTENING", "I'm listening…"))

    # ── State change ─────────────────────────────────────────
    def _on_state(self, state: str, text: str):
        prev = self._state
        self._state = state
        self._text  = text[:160] if text else ""
        if state == "SPEAKING":
            self._bubble_alpha = 255
            self._mouth_f = 0
        if state == "HAPPY" and prev != "HAPPY":
            self._spawn_particles()
        self.update()

    def _spawn_particles(self):
        import random
        colors = ["#ffaa00", "#ff4488", "#44ffaa", "#4488ff", "#ff8844",
                  "#ffffff", "#ff3366", "#33ffcc"]
        cx = self.BUBBLE_MAX_W + self.W // 2   # robot center (right side of widget)
        for _ in range(34):
            self._particles.append({
                "x":       cx + random.uniform(-50, 50),
                "y":       160,
                "vx":      random.uniform(-4.0, 4.0),
                "vy":      random.uniform(-8.0, -1.5),
                "life":    random.randint(28, 60),
                "maxlife": 60,
                "r":       random.randint(3, 6),
                "color":   random.choice(colors),
            })

    # ── Animation tick ───────────────────────────────────────
    def _tick(self):
        self._frame += 1
        f = self._frame
        self._blink = (self._blink + 1) % 130

        # ── Startup intro: right arm wave ────────────────────
        if self._intro_active:
            self._intro_f += 1
            if self._intro_f > 100:
                self._intro_active = False

        # ── Arm target angles per state ──────────────────────
        # Angle 0 = arm pointing straight DOWN from shoulder.
        # Pos angle = clockwise; neg = counter-clockwise (upward).
        if self._state == "IDLE":
            tl = 12 + math.sin(f * 0.028) * 4    # gentle sway
            tr = -12 + math.sin(f * 0.028) * 4
        elif self._state == "LISTENING":
            # Left arm raised to ear; right relaxed
            tl = -118.0
            tr = -12.0
        elif self._state == "THINKING":
            # Right arm up, hand toward chin; left relaxed
            tl = 12.0
            tr = -110.0
        elif self._state == "SPEAKING":
            # Both hands gesture alternately like talking
            tl =  28 + math.sin(f * 0.22) * 38
            tr = -28 - math.sin(f * 0.22) * 38
        elif self._state == "HAPPY":
            # Both arms raised in celebration V
            tl = -148 + math.sin(f * 0.30) * 10
            tr =  148 + math.sin(f * 0.30) * 10
        else:
            tl, tr = 12.0, -12.0

        # Intro wave overrides right arm
        if self._intro_active:
            tr = -72 + math.sin(self._intro_f * 0.26) * 38

        # Smooth lerp toward targets
        self._arm_l += (tl - self._arm_l) * 0.13
        self._arm_r += (tr - self._arm_r) * 0.13

        # ── Mouth ────────────────────────────────────────────
        if self._state == "SPEAKING":
            self._mouth_f = (math.sin(f * 0.38) + 1) / 2
        elif self._state == "LISTENING":
            self._mouth_f = (math.sin(f * 0.14) + 1) / 4
        elif self._state == "HAPPY":
            self._mouth_f = 0.45
        else:
            self._mouth_f *= 0.80

        # ── Thinking spin ────────────────────────────────────
        if self._state == "THINKING":
            self._spin = (self._spin + 4) % 360

        # ── Bounce (HAPPY) ───────────────────────────────────
        if self._state == "HAPPY":
            self._bounce = math.sin(f * 0.28) * 8
        else:
            self._bounce *= 0.86

        # ── Head tilt ────────────────────────────────────────
        ht_map = {
            "THINKING":  7.0,
            "LISTENING": math.sin(f * 0.10) * 4,
            "HAPPY":     math.sin(f * 0.22) * 7,
            "SPEAKING":  math.sin(f * 0.18) * 4,
        }
        ht = ht_map.get(self._state, 0.0)
        self._head_tilt += (ht - self._head_tilt) * 0.13

        # ── Eyebrows ─────────────────────────────────────────
        eb_map = {
            "IDLE":      ( 0.0,  0.0),
            "LISTENING": (-5.0, -5.0),   # both raised
            "THINKING":  (-7.0,  1.5),   # left raised, right slightly lowered
            "SPEAKING":  (-3.0, -3.0),
            "HAPPY":     (-8.0, -8.0),   # high raise
        }
        ebl, ebr = eb_map.get(self._state, (0.0, 0.0))
        self._eb_l += (ebl - self._eb_l) * 0.14
        self._eb_r += (ebr - self._eb_r) * 0.14

        # ── Legs (march on HAPPY) ─────────────────────────────
        if self._state == "HAPPY":
            self._leg_l =  math.sin(f * 0.28) * 10
            self._leg_r = -math.sin(f * 0.28) * 10
        else:
            self._leg_l *= 0.84
            self._leg_r *= 0.84

        # ── Particles ────────────────────────────────────────
        for pp in self._particles[:]:
            pp['x']  += pp['vx']
            pp['y']  += pp['vy']
            pp['vy'] += 0.20
            pp['life'] -= 1
            if pp['life'] <= 0:
                self._particles.remove(pp)

        # ── Bubble fade ───────────────────────────────────────
        if self._state == "IDLE" and self._bubble_alpha > 0:
            self._bubble_alpha = max(0, self._bubble_alpha - 3)
            if self._bubble_alpha == 0:
                self._text = ""

        # Only update the OS window mask when bubble visibility CHANGES
        # (updating every 30 ms cancels OS mouse-drag tracking and breaks drag/click)
        bubble_now = self._bubble_alpha > 10
        if bubble_now != self._mask_bubble_on:
            self._mask_bubble_on = bubble_now
            self._update_mask()

        self.update()

    # ── Paint ────────────────────────────────────────────────
    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)

        colors  = self.STATE_COLORS.get(self._state, self.STATE_COLORS["IDLE"])
        glow    = QColor(colors["glow"])
        head_c  = QColor(colors["head"])
        eye_c   = QColor(colors["eye"])
        mouth_c = QColor(colors["mouth"])

        cx = self.BUBBLE_MAX_W + self.W // 2   # robot is in the RIGHT half of widget
        t  = (math.sin(self._frame * 0.05) + 1) / 2   # breathing 0..1

        # ── Layout with bounce ─────────────────────────────
        bo       = int(self._bounce)       # bounce pixel offset
        head_x   = cx - 58
        head_y   = 55 + bo
        head_w   = 116
        head_h   = 110
        body_y   = head_y + head_h + 13
        legs_y   = body_y + 68
        ant_base = head_y + 5
        ant_tip  = head_y - 26

        border_c = QColor(glow); border_c.setAlpha(200)

        # ═══ 1. PARTICLES ══════════════════════════════════
        for pp in self._particles:
            ratio = pp['life'] / pp['maxlife']
            pc = QColor(pp['color']); pc.setAlpha(int(ratio * 220))
            p.setBrush(QBrush(pc)); p.setPen(Qt.PenStyle.NoPen)
            r = pp['r']
            p.drawEllipse(int(pp['x']) - r, int(pp['y']) - r, r * 2, r * 2)

        # ═══ 2. OUTER GLOW ════════════════════════════════
        glow_r = int(55 + t * 18)
        grad   = QRadialGradient(QPointF(cx, head_y + 55), glow_r + 30)
        ga = QColor(glow); ga.setAlpha(65 + int(t * 55))
        gb = QColor(glow); gb.setAlpha(0)
        grad.setColorAt(0, ga); grad.setColorAt(1, gb)
        p.setBrush(QBrush(grad)); p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(QRectF(cx - glow_r - 30, head_y + 25 - glow_r,
                              (glow_r + 30) * 2, glow_r * 2))

        # ═══ 3. STATE EFFECTS ═════════════════════════════
        if self._state == "LISTENING":
            for i in range(4):
                phase      = (self._frame * 0.07 - i * 0.55) % (2 * math.pi)
                ring_alpha = int(max(0.0, math.sin(phase)) * 160)
                ring_r     = 52 + i * 20
                rc = QColor(colors["glow"]); rc.setAlpha(ring_alpha)
                p.setPen(QPen(rc, 2)); p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawEllipse(cx - ring_r, head_y + 55 - ring_r,
                              ring_r * 2, ring_r * 2)

        if self._state == "THINKING":
            orb_r = 72
            dc = QColor(colors["glow"]); dc.setAlpha(85)
            p.setPen(QPen(dc, 1.5, Qt.PenStyle.DashLine))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(cx - orb_r, head_y + 55 - orb_r, orb_r * 2, orb_r * 2)
            ar = math.radians(self._spin)
            dx = cx + orb_r * math.cos(ar); dy = head_y + 55 + orb_r * math.sin(ar)
            dc2 = QColor(colors["glow"]); dc2.setAlpha(220)
            p.setBrush(QBrush(dc2)); p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(int(dx) - 5, int(dy) - 5, 10, 10)

        if self._state == "SPEAKING":
            n = 7; bw = 5; bg = 4
            bx0 = cx - (n * (bw + bg) - bg) // 2
            wc  = QColor(colors["glow"]); wc.setAlpha(150)
            p.setPen(Qt.PenStyle.NoPen)
            for bi in range(n):
                ph   = self._frame * 0.18 + bi * 0.6
                bh   = int(8 + abs(math.sin(ph)) * 28)
                by_  = legs_y + 14 - bh // 2
                p.setBrush(QBrush(wc))
                p.drawRoundedRect(bx0 + bi * (bw + bg), by_, bw, bh, 2, 2)

        # ═══ 4. LEGS ══════════════════════════════════════
        leg_c  = QColor("#0a1520")
        foot_c = QColor("#0d1e2e")
        for side, swing in [(-1, self._leg_l), (1, self._leg_r)]:
            lx = cx + side * 16
            p.save()
            p.translate(lx, legs_y)
            p.rotate(swing)
            p.setBrush(QBrush(leg_c))
            p.setPen(QPen(border_c, 1.5))
            p.drawRoundedRect(-8, 0, 16, 38, 5, 5)
            p.setBrush(QBrush(foot_c))
            p.drawRoundedRect(-11 + (2 if side == 1 else 0), 34, 22, 12, 4, 4)
            p.restore()

        # ═══ 5. ARMS (drawn before body → appear behind body box) ══
        arm_c  = QColor("#0d1a28")
        arm_bc = QColor(glow); arm_bc.setAlpha(160)
        hand_c = QColor(head_c).lighter(160)
        arm_len = 50

        for ang, sx in [(self._arm_l, cx - 46), (self._arm_r, cx + 46)]:
            p.save()
            p.translate(sx, body_y + 16)
            p.rotate(ang)
            p.setBrush(QBrush(arm_c))
            p.setPen(QPen(arm_bc, 1.5))
            p.drawRoundedRect(-6, 0, 12, arm_len, 5, 5)
            # hand
            p.setBrush(QBrush(hand_c))
            p.setPen(QPen(arm_bc, 1))
            p.drawEllipse(-8, arm_len - 2, 16, 16)
            p.restore()

        # ═══ 6. ANTENNA ══════════════════════════════════
        sway = int(math.sin(self._frame * 0.07) * 7)
        p.setPen(QPen(QColor("#3060a0"), 3, Qt.PenStyle.SolidLine,
                      Qt.PenCapStyle.RoundCap))
        p.drawLine(cx + sway, ant_base, cx, ant_tip)
        tip_c = QColor(glow); tip_c.setAlpha(220)
        p.setBrush(QBrush(tip_c)); p.setPen(Qt.PenStyle.NoPen)
        tr2 = 7 + int(t * 4)
        p.drawEllipse(cx + sway - tr2, ant_tip - tr2, tr2 * 2, tr2 * 2)
        ring_c = QColor(glow); ring_c.setAlpha(55 + int(t * 80))
        p.setPen(QPen(ring_c, 1.5)); p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(cx + sway - tr2 - 5, ant_tip - tr2 - 5,
                      (tr2 + 5) * 2, (tr2 + 5) * 2)

        # ═══ 7. HEAD (with tilt) ═══════════════════════
        hcx = head_x + head_w // 2
        hcy = head_y + head_h // 2
        p.save()
        p.translate(hcx, hcy)
        p.rotate(self._head_tilt)
        p.translate(-hcx, -hcy)

        # Head box
        hg = QLinearGradient(head_x, head_y, head_x, head_y + head_h)
        h0 = QColor(head_c); h0.setAlpha(240)
        h1 = QColor(head_c).darker(145); h1.setAlpha(240)
        hg.setColorAt(0, h0); hg.setColorAt(1, h1)
        p.setBrush(QBrush(hg))
        p.setPen(QPen(border_c, 2))
        p.drawRoundedRect(head_x, head_y, head_w, head_h, 28, 28)

        # Earpieces
        for ex_ in [head_x - 10, head_x + head_w]:
            p.setBrush(QBrush(head_c)); p.setPen(QPen(border_c, 1))
            p.drawRoundedRect(ex_, head_y + 25, 14, 28, 4, 4)
            p.setPen(QPen(glow.darker(120), 1))
            p.drawLine(ex_ + 7, head_y + 30, ex_ + 7, head_y + 47)

        # Eyebrows
        eye_y = head_y + 33
        for i, (ex, eb) in enumerate([(cx - 22, self._eb_l), (cx + 10, self._eb_r)]):
            brow_c = QColor(glow); brow_c.setAlpha(210)
            p.setPen(QPen(brow_c, 2.5, Qt.PenStyle.SolidLine,
                          Qt.PenCapStyle.RoundCap))
            by_ = int(eye_y - 11 + eb)
            if self._state == "THINKING" and i == 1:
                # Right brow angled (skeptical look)
                p.drawLine(ex + 2, by_ + 3, ex + 16, by_ - 1)
            elif self._state == "HAPPY":
                # Arched happy brow
                p.drawArc(ex, by_ - 2, 18, 10, 0, 180 * 16)
            else:
                p.drawLine(ex + 2, by_, ex + 16, by_)

        # Eyes
        for i, ex in enumerate([cx - 22, cx + 10]):
            if self._state == "HAPPY":
                # Happy squint: upward arc (^_^)
                p.setPen(QPen(eye_c, 3, Qt.PenStyle.SolidLine,
                              Qt.PenCapStyle.RoundCap))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawArc(ex, eye_y + 5, 18, 12, 0, 180 * 16)
            elif self._state == "THINKING":
                p.setPen(QPen(eye_c, 3)); p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawArc(ex, eye_y, 18, 18, int(self._spin) * 16, 270 * 16)
            elif self._state == "LISTENING":
                # Wide alert eyes
                p.setBrush(QBrush(eye_c)); p.setPen(Qt.PenStyle.NoPen)
                p.drawEllipse(ex - 1, eye_y - 1, 20, 20)
                p.setBrush(QBrush(QColor("#0a0f1e")))
                p.drawEllipse(ex + 4, eye_y + 4, 10, 10)
                p.setBrush(QBrush(QColor("#ffffff")))
                p.drawEllipse(ex + 11, eye_y + 4, 4, 4)
            else:
                blink_on = (self._blink < 6)
                if blink_on:
                    p.setPen(QPen(eye_c, 3, Qt.PenStyle.SolidLine,
                                  Qt.PenCapStyle.RoundCap))
                    p.drawLine(ex, eye_y + 9, ex + 18, eye_y + 9)
                else:
                    er = QRadialGradient(QPointF(ex + 9, eye_y + 9), 9)
                    e0 = QColor("#ffffff"); e0.setAlpha(220)
                    e1 = QColor(eye_c);    e1.setAlpha(200)
                    er.setColorAt(0, e0); er.setColorAt(1, e1)
                    p.setBrush(QBrush(er)); p.setPen(Qt.PenStyle.NoPen)
                    p.drawEllipse(ex, eye_y, 18, 18)
                    p.setBrush(QBrush(QColor("#0a0f1e")))
                    p.drawEllipse(ex + 5, eye_y + 5, 8, 8)
                    p.setBrush(QBrush(QColor("#ffffff")))
                    p.drawEllipse(ex + 11, eye_y + 5, 4, 4)

        # Mouth
        mouth_y = head_y + 73
        open_h  = int(self._mouth_f * 18)
        mx      = cx - 24
        p.setPen(QPen(mouth_c, 2))
        if self._state == "HAPPY":
            # Big open smile
            p.setBrush(QBrush(QColor("#1a0800")))
            p.drawArc(mx + 2, mouth_y - 6, 44, 26, 0, -180 * 16)
        elif open_h > 2:
            p.setBrush(QBrush(mouth_c.darker(180)))
            p.drawRoundedRect(mx, mouth_y, 48, open_h + 6, 6, 6)
        else:
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawArc(mx + 4, mouth_y - 4, 40, 18, 0, -180 * 16)

        p.restore()   # end head tilt

        # ═══ 8. NECK ════════════════════════════════════
        p.setBrush(QBrush(QColor("#0d1a28"))); p.setPen(Qt.PenStyle.NoPen)
        p.drawRect(cx - 14, head_y + head_h - 2, 28, 18)

        # ═══ 9. BODY ════════════════════════════════════
        bg_g = QLinearGradient(cx - 45, body_y, cx + 45, body_y + 65)
        b0 = QColor("#0d2030"); b0.setAlpha(232)
        b1 = QColor("#060c18"); b1.setAlpha(232)
        bg_g.setColorAt(0, b0); bg_g.setColorAt(1, b1)
        p.setBrush(QBrush(bg_g))
        p.setPen(QPen(border_c, 2))
        p.drawRoundedRect(cx - 45, body_y, 90, 65, 14, 14)

        # Chest light
        cc = QColor(glow); cc.setAlpha(180 + int(t * 55))
        p.setBrush(QBrush(cc)); p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(cx - 10, body_y + 18, 20, 20)
        ic = QColor("#ffffff"); ic.setAlpha(115)
        p.setBrush(QBrush(ic))
        p.drawEllipse(cx - 5, body_y + 23, 10, 10)

        # Animated scan line
        scan_y = body_y + int((self._frame * 2) % 65)
        sc = QColor(glow); sc.setAlpha(40)
        p.setPen(QPen(sc, 1))
        p.drawLine(cx - 44, scan_y, cx + 44, scan_y)

        # ═══ 10. STATE LABEL ════════════════════════════
        state_labels = {
            "IDLE":      "Idle",
            "LISTENING": "Listening...",
            "THINKING":  "Thinking...",
            "SPEAKING":  "Speaking...",
            "HAPPY":     "Yay!",
        }
        lbl = state_labels.get(self._state, "")
        p.setFont(QF("Segoe UI", 9))
        p.setPen(QPen(QColor(glow)))
        p.drawText(QRect(self.BUBBLE_MAX_W, legs_y + 52, self.W, 24),
                   Qt.AlignmentFlag.AlignCenter, lbl)

        # ═══ 11. SPEECH BUBBLE ══════════════════════════
        if self._text and self._bubble_alpha > 0:
            self._draw_bubble(p, body_y - 10, glow)

        p.end()

    def _draw_bubble(self, p: QPainter, anchor_y: int, glow: QColor):
        """Draw a rounded speech bubble to the LEFT of the character (robot is on right)."""
        if not self._text:
            return

        margin   = 12
        max_w    = self.BUBBLE_MAX_W - 10
        font     = QF("Segoe UI", 10)
        p.setFont(font)
        fm = QFontMetrics(font)

        # Word-wrap
        words = self._text.split()
        lines, cur = [], ""
        for w in words:
            test = (cur + " " + w).strip()
            if fm.horizontalAdvance(test) + margin * 2 > max_w - 20:
                if cur:
                    lines.append(cur)
                cur = w
            else:
                cur = test
        if cur:
            lines.append(cur)

        line_h  = fm.height() + 4
        bw      = min(max_w, max(fm.horizontalAdvance(l) for l in lines) + margin * 2 + 20)
        bh      = len(lines) * line_h + margin * 2
        by      = max(10, anchor_y - bh + 20)

        # Bubble sits to the LEFT of the robot, right-aligned against it
        bx_start = max(4, self.BUBBLE_MAX_W - bw - 4)

        # Bubble background
        alpha    = self._bubble_alpha
        bg       = QColor("#061220"); bg.setAlpha(int(alpha * 0.92))
        border_c = QColor(glow);      border_c.setAlpha(alpha)
        text_c   = QColor("#e0f4ff"); text_c.setAlpha(alpha)

        path = QPainterPath()
        path.addRoundedRect(QRectF(bx_start, by, bw, bh), 12, 12)

        # Tail pointing RIGHT toward the robot
        tail_x = bx_start + bw
        tail_y = by + bh // 2
        path.moveTo(tail_x,      tail_y - 8)
        path.lineTo(tail_x + 14, tail_y)        # points right
        path.lineTo(tail_x,      tail_y + 8)
        path.closeSubpath()

        p.setBrush(QBrush(bg))
        p.setPen(QPen(border_c, 1.5))
        p.drawPath(path)

        # Text
        p.setPen(QPen(text_c))
        for i, line in enumerate(lines):
            p.drawText(
                bx_start + margin + 4,
                by + margin + i * line_h + fm.ascent(),
                line
            )

    def _update_mask(self):
        """Set OS-level interactive region. Only called when bubble visibility changes.
        Robot is in the RIGHT side of the widget (x = BUBBLE_MAX_W .. BUBBLE_MAX_W+W).
        Bubble is on the LEFT side and is non-interactive (pass-through for mouse)."""
        total_h   = self.H + 60
        robot_x   = self.BUBBLE_MAX_W        # left edge of robot within widget
        # Robot area is always interactive
        region = QRegion(robot_x, 0, self.W + 10, total_h)
        if self._mask_bubble_on:
            # Expand leftward to also capture bubble area when speech bubble visible
            region = region.united(QRegion(0, 0, robot_x, total_h))
        self.setMask(region)

    # ── Dragging ───────────────────────────────
    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
            self.grabMouse()   # capture ALL mouse events during drag, even outside mask
            e.accept()

    def mouseMoveEvent(self, e):
        if self._drag_pos is not None and e.buttons() == Qt.MouseButton.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag_pos)
            e.accept()

    def mouseReleaseEvent(self, e):
        if self._drag_pos is not None:
            self.releaseMouse()   # release capture
        self._drag_pos = None
        e.accept()

    def mouseDoubleClickEvent(self, e):
        """Double-click companion → activate voice."""
        signals.companion_state.emit("LISTENING", "I'm listening…")
        activate_voice()

    # ── Right-click context menu ─────────────
    def contextMenuEvent(self, e):
        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu{background:#0d1525;color:#00d4ff;border:1px solid #1a3050;}"
            "QMenu::item{padding:7px 22px;font-size:12px;}"
            "QMenu::item:selected{background:#003d5c;}")
        menu.addAction("🎤  Activate Voice").triggered.connect(activate_voice)
        menu.addSeparator()
        menu.addAction("⚙  Settings").triggered.connect(self._open_settings)
        menu.addAction("📋  Show / Hide Log").triggered.connect(self._toggle_log)
        menu.addSeparator()
        menu.addAction("❌  Quit Rey Shinchan").triggered.connect(lambda: sys.exit(0))
        menu.exec(e.globalPos())

    def _open_settings(self):
        dlg = SettingsDialog()
        dlg.exec()

    def _toggle_log(self):
        global _main_window
        if _main_window is None:
            return
        if _main_window.isVisible():
            _main_window.hide()
        else:
            _main_window.show()
            _main_window.raise_()
            _main_window.activateWindow()


# ─────────────────────────────────────────────
#   ENTRY POINT
# ─────────────────────────────────────────────
_main_window = None   # global reference for CompanionWidget to access

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    # Main window stays HIDDEN — companion is the only visible UI.
    # Access it via companion right-click → 'Show / Hide Log'
    _main_window = ReyShinchanWindow()

    companion = CompanionWidget()
    companion.show()

    # ─────────────────────────────────────────────
    #  PROACTIVE BATTERY MONITOR  (critical for blind users
    #  who cannot see the battery icon in the taskbar)
    # ─────────────────────────────────────────────
    def _battery_monitor():
        warned_20 = False
        warned_10 = False
        while True:
            time.sleep(300)   # check every 5 minutes
            try:
                b = psutil.sensors_battery()
                if b and not b.power_plugged:
                    pct = b.percent
                    if pct <= 10 and not warned_10:
                        warned_10 = True
                        msg = (f"Warning, {config.get('user_name')}! "
                               f"Battery is critically low at {pct:.0f} percent. "
                               f"Please plug in your charger immediately.")
                        signals.companion_state.emit("SPEAKING", msg)
                        speak(msg)
                        threading.Timer(
                            max(3.0, len(msg) * 0.055),
                            lambda: signals.companion_state.emit("IDLE", "")
                        ).start()
                    elif pct <= 20 and not warned_20:
                        warned_20 = True
                        msg = (f"{config.get('user_name')}, battery is low at "
                               f"{pct:.0f} percent. "
                               f"I recommend plugging in your charger soon.")
                        signals.companion_state.emit("SPEAKING", msg)
                        speak(msg)
                        threading.Timer(
                            max(3.0, len(msg) * 0.055),
                            lambda: signals.companion_state.emit("IDLE", "")
                        ).start()
                    elif pct > 25:
                        warned_20 = False   # reset so it warns again next drop
                        warned_10 = False
            except Exception:
                pass
    threading.Thread(target=_battery_monitor,
                     daemon=True, name="BatteryMonitor").start()

    # ─────────────────────────────────────────────
    #  STARTUP GREETING  (speaks 2 s after launch)
    # ─────────────────────────────────────────────
    def _startup_greeting():
        time.sleep(2.0)
        user  = config.get("user_name")
        hour  = datetime.datetime.now().hour
        if hour < 12:
            period = "Good morning"
        elif hour < 17:
            period = "Good afternoon"
        else:
            period = "Good evening"
        msg = (
            f"{period}, {user}. I am Rey Shinchan, your personal voice assistant. "
            f"I am always here for you, ready to listen and help with anything you need. "
            f"Just speak and I will take care of it. "
            f"Say help anytime to hear what I can do for you."
        )
        signals.companion_state.emit("SPEAKING", msg)
        speak(msg)
        threading.Timer(
            max(3.0, len(msg) * 0.055),
            lambda: signals.companion_state.emit("IDLE", "")
        ).start()
    threading.Thread(target=_startup_greeting,
                     daemon=True, name="StartupGreeting").start()

    sys.exit(app.exec())