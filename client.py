"""
chess_pro.py  –  Chess Pro  (PyQt6 desktop chess client)

A full-featured chess application that supports:
  • Offline two-player games on the same machine
  • Single-player games against a built-in minimax AI (Easy)
    or the Stockfish engine (Hard)
  • Online multiplayer via a custom TCP server, with lobby,
    session creation/joining, chat, draw offers, rematch, resign
  • Spectator mode so you can watch live online games
  • Animated piece movement, captured-piece display, timers,
    sound effects, and background music

Author comments use plain English and explain the *why*, not
just the *what* – the kind of notes a senior dev would leave
for a junior joining the project mid-sprint.
"""

# ──────────────────────────────────────────────────────────────────────────────
# Standard-library imports
# ──────────────────────────────────────────────────────────────────────────────
import sys          # sys.exit, sys._MEIPASS (PyInstaller), sys.argv
import os           # file / directory helpers
import json         # session persistence and server protocol
import time         # wall-clock timing for the chess clock
import random       # random AI moves (easy mode) + random capture sounds
import shutil       # shutil.which – find stockfish on PATH
import platform     # detect Windows vs. macOS vs. Linux
import socket       # raw TCP for the online server
import threading    # background listener thread

# ──────────────────────────────────────────────────────────────────────────────
# Third-party imports
# ──────────────────────────────────────────────────────────────────────────────
import chess            # python-chess: board logic, move gen, FEN, UCI
import chess.engine     # SimpleEngine wrapper around the Stockfish binary

# Qt core + GUI
from PyQt6.QtGui import QIcon, QFont, QCursor, QPixmap, QPainter, QColor
from PyQt6.QtCore import (
    Qt, QTimer, pyqtSignal, QObject, QSize, QUrl, QPoint,
    QPropertyAnimation, QEasingCurve,
)
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QPushButton, QLabel, QVBoxLayout,
    QWidget, QGridLayout, QInputDialog, QMessageBox, QHBoxLayout,
    QScrollArea, QTextEdit, QLineEdit, QDialog, QTabWidget,
    QComboBox, QListWidget, QListWidgetItem, QFrame, QRadioButton,
    QButtonGroup, QSizePolicy,
)
from PyQt6.QtMultimedia import QSoundEffect, QMediaPlayer, QAudioOutput


# ──────────────────────────────────────────────────────────────────────────────
# DPI awareness – must run BEFORE QApplication is created
# ──────────────────────────────────────────────────────────────────────────────

def _set_dpi_awareness():
    """
    On Windows, tell the OS that this process is aware of per-monitor DPI
    scaling.  Without this Qt renders everything slightly blurry on a 4K or
    125 % scaled display because Windows would automatically up-scale the
    window instead of letting the app do it properly.

    We try the newer API (SetProcessDpiAwareness level 2) first and fall back
    to the legacy SetProcessDPIAware if we're on an older Windows version.
    On macOS / Linux this function does nothing.
    """
    if sys.platform == "win32":
        try:
            import ctypes
            # Level 2 = PROCESS_PER_MONITOR_DPI_AWARE (best quality on multi-monitor rigs)
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                # Fallback: system-level DPI awareness (single monitor, good enough)
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass   # If both fail we just live with the blurriness


# ──────────────────────────────────────────────────────────────────────────────
# Session persistence – keep the user logged-in across restarts
# ──────────────────────────────────────────────────────────────────────────────

SESSION_FILE = "session.json"   # lives next to the executable / script


def load_session():
    """
    Try to read a previously saved login session from disk.
    Returns a dict with keys  username / stats / token / server,
    or None if no session file exists or it is corrupt.
    """
    if os.path.exists(SESSION_FILE):
        try:
            with open(SESSION_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass   # Corrupt JSON – treat as if there's no session
    return None


def save_session(username, stats, token, server_addr):
    """
    Write the current session to disk so the user stays logged in
    next time they launch the app without re-entering credentials.
    """
    with open(SESSION_FILE, "w") as f:
        json.dump(
            {"username": username, "stats": stats,
             "token": token, "server": server_addr},
            f,
        )


def clear_session():
    """Delete the session file – used when the user explicitly signs out."""
    if os.path.exists(SESSION_FILE):
        os.remove(SESSION_FILE)


# ──────────────────────────────────────────────────────────────────────────────
# Utility helpers
# ──────────────────────────────────────────────────────────────────────────────

def no_window():
    """
    Hide the Windows console window that pops up when you run a Python script
    that was compiled with PyInstaller in --console mode but you actually don't
    want a terminal visible to the user.  This is a no-op on macOS / Linux.
    """
    if sys.platform == 'win32':
        import ctypes
        ctypes.windll.user32.ShowWindow(
            ctypes.windll.kernel32.GetConsoleWindow(), 0
        )


def resource_path(relative_path):
    """
    Resolve a path to a bundled resource file.

    When the app is frozen by PyInstaller, all assets are unpacked into a
    temporary folder pointed at by sys._MEIPASS.  In development (running
    the raw .py file) we just use the directory that contains this script.

    This lets us write resource_path("w_pawn.png") and get the right file
    regardless of how the app was launched.
    """
    try:
        base_path = sys._MEIPASS          # PyInstaller frozen bundle
    except AttributeError:
        base_path = os.path.dirname(os.path.abspath(sys.argv[0]))
    return os.path.join(base_path, relative_path)


def get_stockfish_path():
    """
    Walk through a priority-ordered list of candidate locations and return
    the first path where the Stockfish binary actually exists.

    Priority (roughly):
      1. ./stockfish/stockfish.exe  (bundled alongside our app)
      2. ./stockfish.exe            (flat layout next to the script)
      3. sys._MEIPASS/stockfish/… (PyInstaller frozen bundle)
      4. Whatever 'stockfish' resolves to on PATH
      5. A handful of well-known system-wide install locations

    Returns None if Stockfish cannot be found anywhere – the UI handles
    that gracefully by falling back to the built-in minimax engine.
    """
    candidates = []
    base_dir = os.path.dirname(os.path.abspath(sys.argv[0]))

    # Most common bundled layouts
    candidates.append(os.path.join(base_dir, "stockfish", "stockfish.exe"))
    candidates.append(os.path.join(base_dir, "stockfish.exe"))

    # PyInstaller frozen bundle path
    if getattr(sys, 'frozen', False):
        try:
            candidates.append(os.path.join(sys._MEIPASS, "stockfish", "stockfish.exe"))
        except Exception:
            pass

    # Check PATH – covers "brew install stockfish" on Mac, apt on Linux, etc.
    found = shutil.which("stockfish")
    if found:
        candidates.append(found)

    if platform.system() != "Windows":
        # Unix-like systems: also check the non-.exe binary name
        candidates.append(os.path.join(base_dir, "stockfish", "stockfish"))
        candidates.append(os.path.join(base_dir, "stockfish"))
        candidates += [
            "/opt/homebrew/bin/stockfish",   # Apple Silicon Homebrew
            "/usr/local/bin/stockfish",      # Intel Mac Homebrew
            "/usr/games/stockfish",          # Debian/Ubuntu apt
            "/usr/bin/stockfish",
        ]
    else:
        # Windows system-wide installs
        candidates += [
            r"C:\Program Files\stockfish\stockfish.exe",
            r"C:\stockfish\stockfish.exe",
        ]

    for path in candidates:
        if path and os.path.isfile(path):
            return path

    return None   # Caller should check for None and degrade gracefully


# ──────────────────────────────────────────────────────────────────────────────
# Shared Qt stylesheet constants
# ──────────────────────────────────────────────────────────────────────────────

# Dark-theme base style reused across every dialog in the app.
# Keeping it in one place means a designer can change the look-and-feel
# in a single edit rather than hunting through a dozen dialog classes.
DIALOG_STYLE = """
    QDialog {
        background-color: #1e272e;
        border: 2px solid #576574;
        border-radius: 15px;
    }
    QLabel { color: #d2dae2; font-size: 13px; }
    QRadioButton {
        color: #d2dae2; font-size: 13px;
        spacing: 8px; padding: 4px;
    }
    QPushButton {
        background-color: #51cf66; color: white;
        border-radius: 10px; font-size: 14px;
        font-weight: bold; padding: 10px;
    }
    QPushButton:hover { background-color: #3ba856; }
    QListWidget {
        background-color: #2f3542; color: white;
        border-radius: 8px; font-size: 13px; padding: 4px;
    }
    QListWidget::item { padding: 10px; border-radius: 6px; margin: 2px; }
    QListWidget::item:selected { background-color: #3d6b8f; }
    QListWidget::item:hover   { background-color: #485460; }
"""

# Override style for 'Cancel' / destructive buttons
BTN_CANCEL = (
    "QPushButton { background-color: #576574; color: white; border-radius: 10px; "
    "font-size: 14px; font-weight: bold; padding: 10px; } "
    "QPushButton:hover { background-color: #ff7979; }"
)


# ──────────────────────────────────────────────────────────────────────────────
# Dialog: choose color + time control (shared by offline & AI modes)
# ──────────────────────────────────────────────────────────────────────────────

class ColorAndTimeControlDialog(QDialog):
    """
    A modal dialog that lets the player pick:
      • which side to play (White / Black / Random) – optional, controlled
        by the show_color flag
      • the time control for the game

    Used for offline 2-player and AI games.  Online games have their own
    dialog (OnlineCreateDialog) because they have slightly different UX needs.
    """

    def __init__(self, parent=None, show_color=True, title_prefix=""):
        super().__init__(parent)
        self.setWindowTitle("Game Settings")
        self.setModal(True)
        # Height depends on whether we're showing the color picker row
        self.setFixedSize(420, 520 if show_color else 420)
        self.setStyleSheet(DIALOG_STYLE)

        self.show_color = show_color
        # QButtonGroup enforces mutual exclusion – only one radio can be active
        self.color_group = QButtonGroup(self) if show_color else None
        self.tc_group = QButtonGroup(self)

        # ── Layout ────────────────────────────────────────────────────────────
        layout = QVBoxLayout()
        layout.setContentsMargins(25, 25, 25, 25)
        layout.setSpacing(12)

        title = QLabel(f"{title_prefix}Game Settings")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("font-size: 20px; font-weight: bold; color: #ffd32a;")
        layout.addWidget(title)

        # ── Color picker (optional) ────────────────────────────────────────
        if show_color:
            color_lbl = QLabel("♟ Choose Your Color:")
            color_lbl.setStyleSheet(
                "font-size: 14px; font-weight: bold; color: #a4b0be; margin-top: 6px;"
            )
            layout.addWidget(color_lbl)

            color_row = QHBoxLayout()
            for text, value in [
                ("♔ White", "White"),
                ("♚ Black", "Black"),
                ("🎲 Random", "Random"),
            ]:
                rb = QRadioButton(text)
                rb.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
                rb.setProperty("color_value", value)
                self.color_group.addButton(rb)
                color_row.addWidget(rb)
                if value == "White":
                    rb.setChecked(True)   # sensible default
            layout.addLayout(color_row)

        # ── Time control picker ────────────────────────────────────────────
        tc_lbl = QLabel("⏱ Choose Time Control:")
        tc_lbl.setStyleSheet(
            "font-size: 14px; font-weight: bold; color: #a4b0be; margin-top: 6px;"
        )
        layout.addWidget(tc_lbl)

        # Each tuple is (display text, internal value).
        # "0" means unlimited; "M+I" means M minutes + I seconds increment.
        time_controls = [
            ("♾ Unlimited (No timer)", "0"),
            ("⚡ 1+0 (Bullet)",         "1+0"),
            ("⏰ 3+2 (Blitz)",          "3+2"),
            ("⏱ 5+0 (Blitz)",          "5+0"),
            ("⌛ 10+0 (Rapid)",         "10+0"),
        ]
        for text, value in time_controls:
            radio = QRadioButton(text)
            radio.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            radio.setProperty("tc_value", value)
            self.tc_group.addButton(radio)
            layout.addWidget(radio)
            if value == "0":
                radio.setChecked(True)   # unlimited by default

        layout.addStretch()

        # ── OK / Cancel ────────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        ok_btn = QPushButton("✔ START GAME")
        ok_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        ok_btn.clicked.connect(self.accept)

        cancel_btn = QPushButton("✖ CANCEL")
        cancel_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        cancel_btn.setStyleSheet(BTN_CANCEL)
        cancel_btn.clicked.connect(self.reject)

        btn_row.addWidget(ok_btn)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)
        self.setLayout(layout)

    # ── Accessors called by the parent window ─────────────────────────────

    def get_color(self):
        """
        Return the selected color string: "White", "Black".
        If "Random" was chosen we flip a coin here so the caller
        never has to worry about it.
        """
        if not self.show_color or self.color_group is None:
            return "White"
        for btn in self.color_group.buttons():
            if btn.isChecked():
                val = btn.property("color_value")
                return random.choice(["White", "Black"]) if val == "Random" else val
        return "White"

    def get_time_control(self):
        """Return the selected time-control string, e.g. "5+0" or "0"."""
        for btn in self.tc_group.buttons():
            if btn.isChecked():
                return btn.property("tc_value")
        return "0"


# ──────────────────────────────────────────────────────────────────────────────
# Dialog: create a new online session
# ──────────────────────────────────────────────────────────────────────────────

class OnlineCreateDialog(QDialog):
    """
    When a logged-in user decides to host a new online game, this dialog
    pops up asking them to pick their color and the time control.

    Notably: the opponent automatically receives the *opposite* color, so
    there's no need for the opponent to make a color choice.  We show a
    short info note to explain this to users who might be confused.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Create Online Session")
        self.setModal(True)
        self.setFixedSize(440, 560)
        self.setStyleSheet(DIALOG_STYLE)

        self.color_group = QButtonGroup(self)
        self.tc_group    = QButtonGroup(self)

        layout = QVBoxLayout()
        layout.setContentsMargins(25, 25, 25, 25)
        layout.setSpacing(12)

        title = QLabel("🌐 Create Online Session")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("font-size: 20px; font-weight: bold; color: #ffd32a;")
        layout.addWidget(title)

        # Color selection row
        color_lbl = QLabel("♟ Choose Your Color:")
        color_lbl.setStyleSheet(
            "font-size: 14px; font-weight: bold; color: #a4b0be; margin-top: 6px;"
        )
        layout.addWidget(color_lbl)

        color_row = QHBoxLayout()
        for text, value in [
            ("♔ White", "White"),
            ("♚ Black", "Black"),
            ("🎲 Random", "Random"),
        ]:
            rb = QRadioButton(text)
            rb.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            rb.setProperty("color_value", value)
            self.color_group.addButton(rb)
            color_row.addWidget(rb)
            if value == "White":
                rb.setChecked(True)
        layout.addLayout(color_row)

        # Small helper note – saves the "wait, what color does my friend get?" question
        note = QLabel("ℹ️  Your opponent will automatically get the opposite color.")
        note.setStyleSheet("color: #747d8c; font-size: 11px; padding: 4px;")
        note.setWordWrap(True)
        layout.addWidget(note)

        # Time control radios (same set as offline dialog)
        tc_lbl = QLabel("⏱ Choose Time Control:")
        tc_lbl.setStyleSheet(
            "font-size: 14px; font-weight: bold; color: #a4b0be; margin-top: 6px;"
        )
        layout.addWidget(tc_lbl)

        for text, value in [
            ("♾ Unlimited (No timer)", "0"),
            ("⚡ 1+0 (Bullet)",         "1+0"),
            ("⏰ 3+2 (Blitz)",          "3+2"),
            ("⏱ 5+0 (Blitz)",          "5+0"),
            ("⌛ 10+0 (Rapid)",         "10+0"),
        ]:
            radio = QRadioButton(text)
            radio.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            radio.setProperty("tc_value", value)
            self.tc_group.addButton(radio)
            layout.addWidget(radio)
            if value == "0":
                radio.setChecked(True)

        layout.addStretch()

        btn_row = QHBoxLayout()
        ok_btn = QPushButton("➕ CREATE SESSION")
        ok_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        ok_btn.clicked.connect(self.accept)
        cancel_btn = QPushButton("✖ CANCEL")
        cancel_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        cancel_btn.setStyleSheet(BTN_CANCEL)
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(ok_btn)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)
        self.setLayout(layout)

    def get_color(self):
        for btn in self.color_group.buttons():
            if btn.isChecked():
                val = btn.property("color_value")
                return random.choice(["White", "Black"]) if val == "Random" else val
        return "White"

    def get_time_control(self):
        for btn in self.tc_group.buttons():
            if btn.isChecked():
                return btn.property("tc_value")
        return "0"


# ──────────────────────────────────────────────────────────────────────────────
# Dialog: choose time control for AI games (no color picker needed here
#         because color is already selected via ColorAndTimeControlDialog)
# ──────────────────────────────────────────────────────────────────────────────

class AITimeControlDialog(QDialog):
    """
    Standalone time-control picker used specifically for AI game setups.
    Kept separate from ColorAndTimeControlDialog so we have a clean,
    minimal dialog when color has already been decided.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Time Control")
        self.setModal(True)
        self.setFixedSize(400, 420)
        self.setStyleSheet(DIALOG_STYLE)

        self.radio_group = QButtonGroup(self)
        layout = QVBoxLayout()
        layout.setContentsMargins(25, 25, 25, 25)
        layout.setSpacing(15)

        title = QLabel("⏱ Choose Time Control")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("font-size: 20px; font-weight: bold; color: #ffd32a;")
        layout.addWidget(title)

        for text, value in [
            ("♾ Unlimited (No timer)", "0"),
            ("⚡ 1+0 (Bullet)",         "1+0"),
            ("⏰ 3+2 (Blitz)",          "3+2"),
            ("⏱ 5+0 (Blitz)",          "5+0"),
            ("⌛ 10+0 (Rapid)",         "10+0"),
        ]:
            radio = QRadioButton(text)
            radio.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            radio.setProperty("tc_value", value)
            self.radio_group.addButton(radio)
            layout.addWidget(radio)
            if value == "0":
                radio.setChecked(True)

        layout.addStretch()

        btn_row = QHBoxLayout()
        ok_btn = QPushButton("✔ START GAME")
        ok_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        ok_btn.clicked.connect(self.accept)
        cancel_btn = QPushButton("✖ CANCEL")
        cancel_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        cancel_btn.setStyleSheet(BTN_CANCEL)
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(ok_btn)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)
        self.setLayout(layout)

    def get_time_control(self):
        for btn in self.radio_group.buttons():
            if btn.isChecked():
                return btn.property("tc_value")
        return "0"


# ──────────────────────────────────────────────────────────────────────────────
# Dialog: game-over screen with Retry / Menu options
# ──────────────────────────────────────────────────────────────────────────────

class GameOverDialog(QDialog):
    """
    Pops up whenever a game ends (checkmate, stalemate, timeout, resign, draw).
    The caller reads self.clicked_action after exec() returns to decide
    whether to start a new game or go back to the main menu.
    """

    def __init__(self, result_text, parent=None):
        super().__init__(parent)
        self.setWindowTitle("GAME OVER")
        self.setModal(True)
        self.setFixedSize(400, 220)
        self.setStyleSheet(
            "QDialog { background-color: #1e272e; border: 2px solid #576574;"
            " border-radius: 15px; }"
        )
        # Will be set to "retry" or "menu" before accept() is called
        self.clicked_action = None

        layout = QVBoxLayout()
        layout.setSpacing(15)
        layout.setContentsMargins(25, 25, 25, 25)

        title = QLabel("🏁 GAME OVER")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("color: #ffd32a; font-size: 22px; font-weight: bold;")
        layout.addWidget(title)

        result_label = QLabel(result_text)
        result_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        result_label.setStyleSheet("color: #d2dae2; font-size: 15px;")
        result_label.setWordWrap(True)
        layout.addWidget(result_label)

        btn_layout = QHBoxLayout()

        retry_btn = QPushButton("🔄 RETRY")
        retry_btn.setFixedHeight(45)
        retry_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        retry_btn.setStyleSheet(
            "QPushButton { background-color: #51cf66; color: white; border-radius: 10px;"
            " font-size: 14px; font-weight: bold; }"
            " QPushButton:hover { background-color: #3ba856; }"
        )
        retry_btn.clicked.connect(lambda: self._close("retry"))
        btn_layout.addWidget(retry_btn)

        menu_btn = QPushButton("🏠 MENU")
        menu_btn.setFixedHeight(45)
        menu_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        menu_btn.setStyleSheet(
            "QPushButton { background-color: #576574; color: white; border-radius: 10px;"
            " font-size: 14px; font-weight: bold; }"
            " QPushButton:hover { background-color: #ff7979; }"
        )
        menu_btn.clicked.connect(lambda: self._close("menu"))
        btn_layout.addWidget(menu_btn)

        layout.addLayout(btn_layout)
        self.setLayout(layout)

    def _close(self, action):
        """Record which button was pressed before closing the dialog."""
        self.clicked_action = action
        self.accept()


# ──────────────────────────────────────────────────────────────────────────────
# Dialog: login / register to connect to the online server
# ──────────────────────────────────────────────────────────────────────────────

class AuthDialog(QDialog):
    """
    Tabbed dialog with Login and Register tabs.  Both tabs open a real TCP
    socket to the game server and perform the handshake synchronously.  On
    success the socket is kept alive and handed back to the caller via
    get_socket() so we don't need to reconnect immediately.

    The server protocol is line-oriented:
      Client → "LOGIN|user|pass\n"   or   "REGISTER|user|pass\n"
      Server → "AUTH_OK|user|{stats_json}|token\n"
              or "AUTH_FAIL|reason\n"
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Chess Pro — Sign In")
        self.setModal(True)
        self.setFixedSize(420, 360)
        self.setStyleSheet("""
            QDialog { background-color: #1e272e; border: 2px solid #576574;
                      border-radius: 15px; }
            QLabel  { color: #d2dae2; font-size: 13px; }
            QLineEdit {
                background-color: #2f3542; color: white;
                border: 1px solid #576574; border-radius: 8px;
                padding: 8px; font-size: 13px;
            }
            QLineEdit:focus { border: 1px solid #51cf66; }
            QTabWidget::pane { border: none; background: transparent; }
            QTabBar::tab {
                background: #2f3542; color: #a4b0be;
                padding: 8px 24px; border-radius: 6px;
                margin-right: 4px; font-size: 13px;
            }
            QTabBar::tab:selected { background: #576574; color: white; }
        """)

        # These get filled in when auth succeeds
        self.result_username = None
        self.result_stats    = None
        self.result_token    = None

        self._temp_socket    = None   # the live socket to pass back to the caller
        self._ip_port        = None   # "ip:port" string set by set_connection_info()
        self.auth_completed  = False  # guard against double-submit

        layout = QVBoxLayout()
        layout.setContentsMargins(20, 15, 20, 20)
        layout.setSpacing(12)

        title = QLabel("♟ CHESS PRO")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet(
            "font-size: 26px; font-weight: bold; color: #ffd32a; letter-spacing: 3px;"
        )
        layout.addWidget(title)

        tabs = QTabWidget()

        # ── Login tab ─────────────────────────────────────────────────────────
        login_tab = QWidget()
        lf = QVBoxLayout()
        lf.setSpacing(8)
        lf.setContentsMargins(10, 15, 10, 10)

        self.login_user = QLineEdit()
        self.login_user.setPlaceholderText("Username")
        self.login_pass = QLineEdit()
        self.login_pass.setPlaceholderText("Password")
        self.login_pass.setEchoMode(QLineEdit.EchoMode.Password)
        # Let the user press Enter in the password field to submit
        self.login_pass.returnPressed.connect(self._do_login)

        login_btn = QPushButton("LOGIN")
        login_btn.setFixedHeight(42)
        login_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        login_btn.setStyleSheet(
            "QPushButton { background-color: #51cf66; color: white; border-radius: 10px;"
            " font-size: 15px; font-weight: bold; }"
            " QPushButton:hover { background-color: #3ba856; }"
        )
        login_btn.clicked.connect(self._do_login)

        self.login_status = QLabel("")
        self.login_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.login_status.setStyleSheet("color: #ff6b6b; font-size: 12px;")

        lf.addWidget(self.login_user)
        lf.addWidget(self.login_pass)
        lf.addWidget(login_btn)
        lf.addWidget(self.login_status)
        login_tab.setLayout(lf)

        # ── Register tab ──────────────────────────────────────────────────────
        reg_tab = QWidget()
        rf = QVBoxLayout()
        rf.setSpacing(8)
        rf.setContentsMargins(10, 15, 10, 10)

        self.reg_user  = QLineEdit(); self.reg_user.setPlaceholderText("Choose username")
        self.reg_pass  = QLineEdit(); self.reg_pass.setPlaceholderText("Choose password")
        self.reg_pass.setEchoMode(QLineEdit.EchoMode.Password)
        self.reg_pass2 = QLineEdit(); self.reg_pass2.setPlaceholderText("Confirm password")
        self.reg_pass2.setEchoMode(QLineEdit.EchoMode.Password)
        self.reg_pass2.returnPressed.connect(self._do_register)

        reg_btn = QPushButton("CREATE ACCOUNT")
        reg_btn.setFixedHeight(42)
        reg_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        reg_btn.setStyleSheet(
            "QPushButton { background-color: #3d6b8f; color: white; border-radius: 10px;"
            " font-size: 15px; font-weight: bold; }"
            " QPushButton:hover { background-color: #2980b9; }"
        )
        reg_btn.clicked.connect(self._do_register)

        self.reg_status = QLabel("")
        self.reg_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.reg_status.setStyleSheet("color: #ff6b6b; font-size: 12px;")

        rf.addWidget(self.reg_user)
        rf.addWidget(self.reg_pass)
        rf.addWidget(self.reg_pass2)
        rf.addWidget(reg_btn)
        rf.addWidget(self.reg_status)
        reg_tab.setLayout(rf)

        tabs.addTab(login_tab, "🔑 Login")
        tabs.addTab(reg_tab,   "✨ Register")
        layout.addWidget(tabs)
        self.setLayout(layout)

    def set_connection_info(self, ip_port):
        """Must be called before the dialog is shown so we know where to connect."""
        self._ip_port = ip_port

    # ── Auth action handlers ───────────────────────────────────────────────

    def _do_login(self):
        """Validate fields, then attempt a LOGIN handshake with the server."""
        if self.auth_completed:
            return   # prevent the double-click race condition
        user = self.login_user.text().strip()
        pw   = self.login_pass.text().strip()
        if not user or not pw:
            self.login_status.setText("Please fill in all fields.")
            return
        self._try_auth(f"LOGIN|{user}|{pw}", self.login_status)

    def _do_register(self):
        """Validate fields, confirm password match, then attempt REGISTER."""
        if self.auth_completed:
            return
        user = self.reg_user.text().strip()
        pw   = self.reg_pass.text().strip()
        pw2  = self.reg_pass2.text().strip()
        if not user or not pw:
            self.reg_status.setText("Please fill in all fields.")
            return
        if pw != pw2:
            self.reg_status.setText("Passwords do not match.")
            return
        self._try_auth(f"REGISTER|{user}|{pw}", self.reg_status)

    def _try_auth(self, msg, status_label):
        """
        Open a TCP connection, send the auth message, read one response line.
        On success: store results and accept the dialog.
        On failure: show an error in the status label and close the socket.

        We do this synchronously because we're already in a modal dialog – no
        need for the complexity of async here.  The 8-second timeout prevents
        the UI from hanging forever if the server is unreachable.
        """
        if not self._ip_port:
            status_label.setText("No server address set.")
            return
        try:
            ip, port = self._ip_port.split(":")
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(8)
            sock.connect((ip, int(port)))
            sock.send((msg + "\n").encode())

            # Read until we see a newline – response may arrive in multiple packets
            resp = b""
            while b"\n" not in resp:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                resp += chunk

            line = resp.split(b"\n")[0].decode().strip()

            if line.startswith("AUTH_OK|"):
                self.auth_completed = True
                parts = line.split("|", 3)
                self.result_username = parts[1]
                self.result_stats    = json.loads(parts[2]) if len(parts) > 2 else {}
                self.result_token    = parts[3]              if len(parts) > 3 else None
                self._temp_socket    = sock   # keep alive for the caller
                self.accept()
            else:
                reason = line.split("|", 1)[1] if "|" in line else "Auth failed"
                status_label.setText(f"❌ {reason}")
                sock.close()

        except Exception as e:
            status_label.setText(f"❌ Connection error: {e}")

    def get_socket(self):
        """Return the live socket so the caller can reuse it without reconnecting."""
        return self._temp_socket


# ──────────────────────────────────────────────────────────────────────────────
# Dialog: online lobby – list sessions, create/join/spectate
# ──────────────────────────────────────────────────────────────────────────────

class SessionDialog(QDialog):
    """
    The multiplayer lobby screen.

    Works in two modes controlled by the `mode` parameter:
      "online"   – shows waiting + active sessions; buttons to Create and Join
      "spectate" – shows only games that are currently being played; Watch button

    The dialog supports live refresh: the main window can call refresh() at
    any time and the list will update without closing and reopening the dialog.
    This happens when the server broadcasts SESSION_LIST updates.
    """

    def __init__(self, sessions_list, mode="online", parent=None):
        super().__init__(parent)
        self.mode = mode
        self.setWindowTitle("Online Lobby" if mode == "online" else "Spectate a Game")
        self.setModal(True)
        self.setFixedSize(660, 520)
        self.setStyleSheet(DIALOG_STYLE)

        # These get populated when the user makes a choice
        self.result_action     = None
        self.result_session_id = None
        self.result_tc         = "0"
        self.result_color      = "White"
        self.session_data      = []   # raw session dicts – needed for ID lookup

        layout = QVBoxLayout()
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(10)

        title = QLabel("🌐 ONLINE LOBBY" if mode == "online" else "🎥 SPECTATE GAMES")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("font-size: 22px; font-weight: bold; color: #ffd32a;")
        layout.addWidget(title)

        # Info note – only relevant for online mode
        if mode == "online":
            info_note = QLabel(
                "ℹ️  Create a session and pick your color – "
                "your opponent gets the opposite color automatically."
            )
            info_note.setStyleSheet(
                "color: #a4b0be; font-size: 12px; background: #2f3542;"
                " border-radius: 6px; padding: 6px;"
            )
            info_note.setAlignment(Qt.AlignmentFlag.AlignCenter)
            info_note.setWordWrap(True)
            layout.addWidget(info_note)

        sessions_lbl = QLabel(
            "📋 Active Sessions:" if mode == "online" else "📋 Live Games:"
        )
        sessions_lbl.setStyleSheet("font-weight: bold; color: #a4b0be; margin-top: 6px;")
        layout.addWidget(sessions_lbl)

        self.list_widget = QListWidget()
        self.list_widget.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.list_widget.setMinimumHeight(280)
        layout.addWidget(self.list_widget)
        self._populate(sessions_list)

        # ── Action buttons ────────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        if mode == "online":
            create_btn = QPushButton("➕ CREATE SESSION")
            create_btn.setFixedHeight(44)
            create_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            create_btn.setStyleSheet(
                "QPushButton { background-color: #51cf66; color: white; border-radius: 10px;"
                " font-size: 14px; font-weight: bold; }"
                " QPushButton:hover { background-color: #3ba856; }"
            )
            create_btn.clicked.connect(self._do_create)
            btn_row.addWidget(create_btn)

            join_btn = QPushButton("⚔ JOIN SELECTED")
            join_btn.setFixedHeight(44)
            join_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            join_btn.setStyleSheet(
                "QPushButton { background-color: #f39c12; color: white; border-radius: 10px;"
                " font-size: 14px; font-weight: bold; }"
                " QPushButton:hover { background-color: #e67e22; }"
            )
            join_btn.clicked.connect(self._do_join)
            btn_row.addWidget(join_btn)
        else:
            watch_btn = QPushButton("👁 WATCH SELECTED")
            watch_btn.setFixedHeight(44)
            watch_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            watch_btn.setStyleSheet(
                "QPushButton { background-color: #51cf66; color: white; border-radius: 10px;"
                " font-size: 14px; font-weight: bold; }"
                " QPushButton:hover { background-color: #3ba856; }"
            )
            watch_btn.clicked.connect(self._do_spectate)
            btn_row.addWidget(watch_btn)

        cancel_btn = QPushButton("✖ CANCEL")
        cancel_btn.setFixedHeight(44)
        cancel_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        cancel_btn.setStyleSheet(BTN_CANCEL)
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)

        layout.addLayout(btn_row)

        refresh_lbl = QLabel(
            "Sessions update automatically when the server broadcasts changes."
        )
        refresh_lbl.setStyleSheet("color: #747d8c; font-size: 11px;")
        refresh_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(refresh_lbl)

        self.setLayout(layout)

    def _populate(self, sessions_list):
        """
        Rebuild the list widget from fresh session data.
        We colour-code rows: green for waiting (joinable), yellow for in-progress.
        """
        self.list_widget.clear()
        self.session_data = []

        # Filter based on the current mode
        filtered = [
            s for s in sessions_list
            if (self.mode == "spectate" and s.get("status") == "playing")
            or (self.mode != "spectate" and s.get("status") in ("waiting", "playing", "open"))
        ]

        if not filtered:
            placeholder = (
                "No active games to spectate."
                if self.mode == "spectate"
                else "No sessions open. Create one!"
            )
            item = QListWidgetItem(placeholder)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            self.list_widget.addItem(item)
            return

        for s in filtered:
            tc           = s.get("time_control", "0")
            tc_str       = tc if tc and tc != "0" else "∞"
            status       = s.get("status", "?")
            white        = s.get("white") or "—"
            black        = s.get("black") or "—"
            white_elo    = s.get("white_elo", 1200)
            black_elo    = s.get("black_elo", 1200)
            specs        = s.get("spectator_count", 0)
            sid          = s.get("session_id", "?")
            creator_color = s.get("creator_color", "White")

            # Pick an icon that reflects the game state at a glance
            status_icon = {"waiting": "⏳", "playing": "♟", "open": "🟢"}.get(status, "❓")
            spec_str    = f"  👁{specs}" if specs else ""

            if status == "waiting":
                open_color = "Black" if creator_color == "White" else "White"
                open_str   = "  [Join as ♚ Black]" if open_color == "Black" else "  [Join as ♔ White]"
            else:
                open_str = ""

            text = (
                f"{status_icon} #{sid}  ♔ {white} ({white_elo})"
                f" vs ♚ {black} ({black_elo})"
                f"  [{tc_str}]  {status.upper()}{open_str}{spec_str}"
            )
            item = QListWidgetItem(text)
            if status == "playing":
                item.setForeground(QColor("#ffd32a"))
            elif status == "waiting":
                item.setForeground(QColor("#51cf66"))

            self.list_widget.addItem(item)
            self.session_data.append(s)

    def refresh(self, sessions_list):
        """
        Called by the main window when a new SESSION_LIST arrives from the server.
        We preserve the selection index so the highlighted row doesn't jump.
        """
        sel_idx = self.list_widget.currentRow()
        self._populate(sessions_list)
        if 0 <= sel_idx < self.list_widget.count():
            self.list_widget.setCurrentRow(sel_idx)

    def _do_create(self):
        """Open the creation sub-dialog to get color + time control, then accept."""
        dlg = OnlineCreateDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self.result_action = "create"
        self.result_color  = dlg.get_color()
        self.result_tc     = dlg.get_time_control()
        self.accept()

    def _do_join(self):
        """Join the currently highlighted session."""
        idx = self.list_widget.currentRow()
        if 0 <= idx < len(self.session_data):
            self.result_action     = "join"
            self.result_session_id = str(self.session_data[idx]["session_id"])
            self.accept()
        else:
            QMessageBox.warning(self, "Select", "Please select a session to join.")

    def _do_spectate(self):
        """Watch the currently highlighted game."""
        idx = self.list_widget.currentRow()
        if 0 <= idx < len(self.session_data):
            self.result_action     = "spectate"
            self.result_session_id = str(self.session_data[idx]["session_id"])
            self.accept()
        else:
            QMessageBox.warning(self, "Select", "Please select a game to watch.")


# ──────────────────────────────────────────────────────────────────────────────
# Cross-thread signals
# ──────────────────────────────────────────────────────────────────────────────

class Signals(QObject):
    """
    Centralised signal hub for the listener thread → UI thread boundary.

    The background listener thread receives raw bytes from the server and
    emits these signals.  Qt then delivers them safely on the main thread,
    avoiding any GUI updates from a non-GUI thread (which would crash Qt).

    Every signal name mirrors the server message that triggers it, making
    the listener code very easy to read.
    """
    update_ui_signal           = pyqtSignal()
    move_received              = pyqtSignal(str)
    player_assigned            = pyqtSignal(int, str)
    opponent_left              = pyqtSignal()
    game_started_signal        = pyqtSignal(str, int, int)
    chat_received              = pyqtSignal(str)
    draw_offer_received        = pyqtSignal()
    draw_declined_received     = pyqtSignal()
    draw_accepted_received     = pyqtSignal()
    rematch_received           = pyqtSignal()
    rematch_accepted           = pyqtSignal()
    rematch_declined           = pyqtSignal()
    opponent_resigned          = pyqtSignal()
    opponent_info              = pyqtSignal(str, str)
    stats_update               = pyqtSignal(str)
    session_list_received      = pyqtSignal(str)
    spectate_ok                = pyqtSignal(str)
    spectate_fail              = pyqtSignal(str)
    spectate_move              = pyqtSignal(str)
    spectate_chat              = pyqtSignal(str, str)
    spectate_event             = pyqtSignal(str)
    timer_sync                 = pyqtSignal(float, float)
    waiting_signal             = pyqtSignal()
    spectator_count_signal     = pyqtSignal(int)
    stockfish_move_ready       = pyqtSignal(str)
    challenge_received         = pyqtSignal(str, str)
    challenge_declined         = pyqtSignal(str)
    challenge_fail             = pyqtSignal(str)
    session_created            = pyqtSignal(str)
    join_fail                  = pyqtSignal(str)
    timeout_signal             = pyqtSignal(str)


# ──────────────────────────────────────────────────────────────────────────────
# SquareBoardWidget – a self-sizing square container for the 8×8 grid
# ──────────────────────────────────────────────────────────────────────────────

class SquareBoardWidget(QWidget):
    """
    A QWidget that always maintains a perfect 1:1 (square) aspect ratio
    and exposes the inner QGridLayout for the board buttons.

    Why do we need this?
    Qt's layout engine doesn't natively enforce aspect ratios.  By overriding
    hasHeightForWidth() and heightForWidth() we tell Qt "this widget needs its
    height to equal its width" so the layout resizes it correctly.

    The widget emits resized(int) with the new per-cell pixel size whenever
    it gets resized.  The main window connects this to _on_board_resized() to
    rescale button icons and fonts proportionally.
    """

    # Emits the new square_size (pixels per board cell) after a resize settles
    resized = pyqtSignal(int)

    # Never shrink a cell below 100 px – keeps the board usable on small screens
    MIN_SQ = 100

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        self.setMinimumSize(self.MIN_SQ * 8, self.MIN_SQ * 8)

        self._grid = QGridLayout(self)
        self._grid.setSpacing(2)
        self._grid.setContentsMargins(8, 8, 8, 8)

        self._last_sq = 0

        # Debounce timer: fire _emit_resized only after the window stops
        # being resized for 30 ms, preventing dozens of icon reloads per second
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.timeout.connect(self._emit_resized)

    # ── Square-enforcing geometry overrides ───────────────────────────────

    def hasHeightForWidth(self):
        """Tell Qt that we have a height preference based on width."""
        return True

    def heightForWidth(self, w):
        """We want height == width (square)."""
        return w

    def sizeHint(self):
        """Suggest a size based on the available space in our parent."""
        p = self.parentWidget()
        side = min(p.width(), p.height()) if p else 760
        return QSize(side, side)

    def resizeEvent(self, event):
        """
        Force square whenever Qt tries to give us a non-square size.
        This can happen during window resize or when adjacent panels change.
        """
        super().resizeEvent(event)
        side = min(event.size().width(), event.size().height())
        if side != event.size().width() or side != event.size().height():
            self.resize(side, side)
        # Debounce: restart the 30ms timer on each resize event
        self._resize_timer.start(30)

    def _emit_resized(self):
        """
        Calculate the actual usable square size and emit resized() if it changed.
        We subtract the grid margins and inter-cell spacing before dividing by 8.
        """
        side    = min(self.width(), self.height())
        margin  = 16     # 8 px each side
        spacing = 2 * 7  # 7 gaps between 8 columns/rows
        sq      = max(self.MIN_SQ, (side - margin - spacing) // 8)
        if sq != self._last_sq:
            self._last_sq = sq
            self.resized.emit(sq)

    @property
    def grid(self):
        return self._grid

    def current_sq_size(self):
        """Compute the current cell size without waiting for a signal."""
        side    = min(self.width(), self.height())
        margin  = 16
        spacing = 2 * 7
        return max(self.MIN_SQ, (side - margin - spacing) // 8)


# ──────────────────────────────────────────────────────────────────────────────
# Main window – the heart of the application
# ──────────────────────────────────────────────────────────────────────────────

class ChessUI(QMainWindow):
    """
    The main application window.  It handles:
      • Menu rendering (mode selection, sign-in, stats)
      • Game UI layout (board, timers, chat, captured pieces)
      • Click handling and move validation for all game modes
      • Animated piece movement
      • Chess clock (local ticking + server-synced updates)
      • AI opponent (easy = random, hard = Stockfish / minimax fallback)
      • Online networking (TCP socket + listener thread)
      • Spectator UI for watching live games

    The pattern throughout is:
      - Long-running work (Stockfish, socket listening) runs in daemon threads
      - Threads communicate back to the UI exclusively through Qt signals
      - All GUI mutations happen on the main thread (enforced by Qt signals)
    """

    # The design was originally built for 93 px board squares.
    # All hardcoded pixel values are scaled relative to this baseline.
    _BASE_SQ = 93

    def __init__(self):
        super().__init__()

        # Make sure our asset-cache directories exist on first run
        for folder in ["capture_sounds", "capture_effects"]:
            os.makedirs(folder, exist_ok=True)

        # ── Piece image paths ──────────────────────────────────────────────────
        # Map (piece_type, color) → PNG file path.  We look these up during
        # draw_board() rather than loading all images into memory up front.
        self.pieces = {
            (chess.PAWN,   chess.WHITE): resource_path("w_pawn.png"),
            (chess.PAWN,   chess.BLACK): resource_path("b_pawn.png"),
            (chess.ROOK,   chess.WHITE): resource_path("w_rook.png"),
            (chess.ROOK,   chess.BLACK): resource_path("b_rook.png"),
            (chess.KNIGHT, chess.WHITE): resource_path("w_knight.png"),
            (chess.KNIGHT, chess.BLACK): resource_path("b_knight.png"),
            (chess.BISHOP, chess.WHITE): resource_path("w_bishop.png"),
            (chess.BISHOP, chess.BLACK): resource_path("b_bishop.png"),
            (chess.QUEEN,  chess.WHITE): resource_path("w_queen.png"),
            (chess.QUEEN,  chess.BLACK): resource_path("b_queen.png"),
            (chess.KING,   chess.WHITE): resource_path("w_king.png"),
            (chess.KING,   chess.BLACK): resource_path("b_king.png"),
        }

        # ── Signal wiring ──────────────────────────────────────────────────────
        # We create the Signals object once and connect every signal here in
        # __init__ so the connections are stable for the lifetime of the window.
        self.signals = Signals()
        self.signals.update_ui_signal.connect(self.update_ui)
        self.signals.move_received.connect(self.process_opponent_move)
        self.signals.player_assigned.connect(self.on_player_assigned)
        self.signals.opponent_left.connect(self.on_opponent_left)
        self.signals.game_started_signal.connect(self.on_game_started)
        self.signals.chat_received.connect(self.on_chat_received)
        self.signals.draw_offer_received.connect(self.on_draw_offer_received)
        self.signals.draw_declined_received.connect(self.on_draw_declined)
        self.signals.draw_accepted_received.connect(self.on_draw_accepted)
        self.signals.rematch_received.connect(self.on_rematch_received)
        self.signals.rematch_accepted.connect(self.on_rematch_accepted)
        self.signals.rematch_declined.connect(self.on_rematch_declined)
        self.signals.opponent_resigned.connect(self.on_opponent_resigned)
        self.signals.opponent_info.connect(self.on_opponent_info)
        self.signals.stats_update.connect(self.on_stats_update)
        self.signals.session_list_received.connect(self.on_session_list_received)
        self.signals.spectate_ok.connect(self.on_spectate_ok)
        self.signals.spectate_fail.connect(self.on_spectate_fail)
        self.signals.spectate_move.connect(self.on_spectate_move)
        self.signals.spectate_chat.connect(self.on_spectate_chat)
        self.signals.spectate_event.connect(self.on_spectate_event)
        self.signals.timer_sync.connect(self.on_timer_sync)
        self.signals.waiting_signal.connect(self.on_waiting)
        self.signals.spectator_count_signal.connect(self.on_spectator_count)
        self.signals.stockfish_move_ready.connect(self._on_stockfish_move_ready)
        self.signals.challenge_received.connect(self.on_challenge_received)
        self.signals.challenge_declined.connect(self.on_challenge_declined)
        self.signals.challenge_fail.connect(self.on_challenge_fail)
        self.signals.session_created.connect(self.on_session_created)
        self.signals.join_fail.connect(self.on_join_fail)
        self.signals.timeout_signal.connect(self._on_timeout_signal)

        # ── Window setup ───────────────────────────────────────────────────────
        self.setWindowTitle("Chess Pro")
        icon_path = resource_path("chess.png")
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))

        # Size the window to 85 % of the smaller screen dimension,
        # clamped between 800 and 1400 px so it looks good everywhere
        screen = QApplication.primaryScreen()
        if screen:
            avail = screen.availableGeometry()
            side  = max(800, min(1400, int(min(avail.width(), avail.height()) * 0.85)))
            self.resize(int(side * 1.35), side)
        else:
            self.resize(1250, 930)

        # Absolute minimum – stays usable on a 1366×768 laptop
        self.setMinimumSize(720, 560)

        # Current cell pixel size – updated by SquareBoardWidget.resized signal
        self._sq_size = self._BASE_SQ

        # ── Player / session state ─────────────────────────────────────────────
        self.my_username     = None
        self.my_stats        = {}
        self.my_session_token = None
        self.my_server_addr  = None

        self.opponent_username    = None
        self.opponent_stats       = {}
        self.opponent_info_label  = None

        self.white_player_name      = "White"
        self.black_player_name      = "Black"
        self.spectator_count        = 0
        self.spectator_count_label  = None

        # ── Ping tracking ─────────────────────────────────────────────────────
        self.ping_ms         = 0
        self.ping_timer      = None
        self.ping_sent_time  = 0
        self.ping_label      = None

        # ── Chess clock state ─────────────────────────────────────────────────
        self.time_control     = "0"
        self.white_time       = 0.0
        self.black_time       = 0.0
        self.white_increment  = 0
        self.black_increment  = 0
        self.timer_active     = False
        self.timer_qtime      = None      # the QTimer that ticks every 100 ms
        self.white_timer_label = None
        self.black_timer_label = None
        self.last_tick_time   = 0
        self.is_timed_game    = False

        # ── Spectator clock (runs locally between TIMER_SYNC messages) ────────
        self.spectator_timer_qtime = None
        self.spectator_last_tick   = 0
        self.spectator_board_turn  = chess.WHITE

        # ── Spectator mode state ───────────────────────────────────────────────
        self.is_spectator   = False
        self.spectate_board = None   # a separate chess.Board for the spectated game

        # ── Session-dialog state ───────────────────────────────────────────────
        self._session_dialog       = None
        self._pending_sessions     = []
        self._awaiting_session_dialog = False

        # ── Stockfish engine ───────────────────────────────────────────────────
        self.stockfish_engine     = None
        self.stockfish_path       = get_stockfish_path()
        self.stockfish_skill      = 20      # max strength
        self.stockfish_think_time = 0.2     # seconds per move (keeps the game snappy)

        # ── Core game state ────────────────────────────────────────────────────
        self.board        = chess.Board()
        self.selected_sq  = None    # currently highlighted square (first click)
        self.buttons      = {}      # square_index → QPushButton
        self.grid         = None    # the QGridLayout inside grid_widget
        self.grid_widget  = None    # the SquareBoardWidget
        self.turn_label   = None

        self.running         = True
        self.ai_timer        = None
        self.is_cleaning_up  = False
        self.game_started    = False
        self.animating       = False
        self.anim_from       = None
        self.current_animation = None
        self.game_over_shown = False

        # Pre-move: a move queued up while it's the opponent's turn
        self.premove = None

        # Move history for the <<  >> navigation buttons
        self.move_history  = []
        self.board_history = []   # FEN snapshot after each move
        self.view_index    = -1   # -1 means "watching the live position"
        self.manual_flip   = False

        # ── Network state ──────────────────────────────────────────────────────
        self.mode                 = None
        self.socket               = None
        self.my_turn              = True
        self.player_id            = None
        self.waiting_for_ai       = False
        self.listen_thread        = None
        self.should_stop_listening = False
        self.player_color         = "White"

        # ── UI widget references ───────────────────────────────────────────────
        self.chat_display = None
        self.chat_input   = None

        # ── Sound assets ───────────────────────────────────────────────────────
        self.click_sound        = None
        self.move_sound         = None
        self.capture_sounds     = []
        self.game_start_sound   = None
        self.check_sound        = None
        self.game_end_sound     = None
        self.background_music   = None
        self.audio_output       = None
        self.music_enabled      = True
        self.sound_enabled      = True
        self.sound_btn          = None

        # ── Captured pieces ────────────────────────────────────────────────────
        self.captured_white           = []   # piece types of white pieces taken
        self.captured_black           = []   # piece types of black pieces taken
        self.white_captured_widgets   = []
        self.black_captured_widgets   = []
        self.white_captured_layout    = None
        self.black_captured_layout    = None
        self.previous_check_state     = False

        # ── Capture visual effect ──────────────────────────────────────────────
        self.capture_effect_label  = None
        self.capture_effect_timer  = None
        self.capture_effect_images = []

        # ── Restore session if one was saved ──────────────────────────────────
        session = load_session()
        if session:
            self.my_username      = session.get("username")
            self.my_stats         = session.get("stats", {})
            self.my_session_token = session.get("token")
            self.my_server_addr   = session.get("server")

        # ── Bootstrap ─────────────────────────────────────────────────────────
        self.init_sounds()
        self.play_background_music()
        self.load_capture_effects()
        self.init_menu()

        # Start in full-screen; user can press F11 to toggle
        self.showFullScreen()
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    # ──────────────────────────────────────────────────────────────────────────
    # Key events
    # ──────────────────────────────────────────────────────────────────────────

    def keyPressEvent(self, event):
        """F11 toggles between full-screen and windowed mode."""
        if event.key() == Qt.Key.Key_F11:
            self.showNormal() if self.isFullScreen() else self.showFullScreen()
        else:
            super().keyPressEvent(event)

    # ──────────────────────────────────────────────────────────────────────────
    # Responsive-UI scale helpers
    # ──────────────────────────────────────────────────────────────────────────

    def _scale(self, base_value: int) -> int:
        """
        Proportionally scale a pixel value that was designed for _BASE_SQ-size cells
        to whatever the current cell size actually is.

        Example: a 10 px border at 93 px cells → 13 px at 120 px cells.
        """
        return max(1, int(base_value * self._sq_size / self._BASE_SQ))

    def _font_size(self, base_pt: int) -> int:
        """Scale a font-point value proportionally, never below 6 pt."""
        return max(6, int(base_pt * self._sq_size / self._BASE_SQ))

    def _on_board_resized(self, sq_size: int):
        """
        Slot connected to SquareBoardWidget.resized.
        Update our cached size and propagate the change to buttons and labels.
        """
        self._sq_size = sq_size
        self._rescale_board_buttons()
        self._rescale_ui_labels()

    def _rescale_board_buttons(self):
        """Resize every board button and its piece icon to match the new cell size."""
        sq      = self._sq_size
        icon_sz = int(sq * 0.72)
        for btn in self.buttons.values():
            btn.setFixedSize(sq, sq)
            btn.setIconSize(QSize(icon_sz, icon_sz))
            btn.setFont(QFont("Arial", max(8, sq // 4)))

    def _rescale_ui_labels(self):
        """Update font sizes on the timer labels and turn indicator."""
        fs = self._font_size(14)
        style_timer = (
            f"color: #51cf66; font-size: {fs}px; font-weight: bold; "
            f"background: #2f3542; border-radius: 8px; padding: 4px 8px;"
        )
        for lbl in (self.white_timer_label, self.black_timer_label):
            if lbl:
                lbl.setStyleSheet(style_timer)
        if self.turn_label:
            self.turn_label.setFont(QFont("Arial", self._font_size(16)))

    # ──────────────────────────────────────────────────────────────────────────
    # Chess clock helpers
    # ──────────────────────────────────────────────────────────────────────────

    def parse_time_control(self, tc_str):
        """
        Convert a time-control string into (base_seconds, increment_seconds).

        Supported formats:
          "0"    → (0, 0)   unlimited
          "5+3"  → (300, 3) 5-minute game with 3-second increment
          "10+0" → (600, 0) 10-minute game, no increment
          bare integer → treated as minutes
        """
        if not tc_str or tc_str == "0":
            return 0, 0
        if '+' in str(tc_str):
            parts = str(tc_str).split('+')
            try:
                return int(parts[0]) * 60, int(parts[1])
            except Exception:
                return 0, 0
        try:
            val = int(tc_str)
            # Heuristic: values ≤ 180 are probably minutes, bigger ones are already seconds
            return (val * 60, 0) if 0 < val <= 180 else (val, 0)
        except Exception:
            return 0, 0

    def start_ai_timer(self):
        """
        Start the local chess clock for offline / AI games.
        For online games the clock is driven by TIMER_SYNC messages instead.
        """
        self.stop_timer()
        base, inc = self.parse_time_control(self.time_control)
        if base == 0:
            # Unlimited game – show ∞ instead of a countdown
            self.timer_active = False
            self.is_timed_game = False
            self.white_time = self.black_time = 0
            self._update_timer_labels()
            return

        self.timer_active    = True
        self.is_timed_game   = True
        self.white_time      = float(base)
        self.black_time      = float(base)
        self.white_increment = inc
        self.black_increment = inc
        self.last_tick_time  = time.time()
        self._update_timer_labels()

        # Tick every 100 ms – fine enough for sub-second accuracy without hammering the CPU
        self.timer_qtime = QTimer()
        self.timer_qtime.timeout.connect(self._tick_ai_timer)
        self.timer_qtime.start(100)

    def stop_timer(self):
        """Stop the local countdown timer safely."""
        if self.timer_qtime:
            self.timer_qtime.stop()
            self.timer_qtime = None
        self.timer_active = False

    def _tick_ai_timer(self):
        """
        Called every 100 ms while a local game is running.
        Decrements the active player's clock and checks for a flag fall.

        We deliberately skip this for online games because the server is the
        authoritative clock there – we just display what the server tells us.
        """
        if not self.timer_active or not self.game_started:
            return
        if self.board.is_game_over():
            return
        if self.mode == "online":
            return   # server handles the clock

        now     = time.time()
        elapsed = now - self.last_tick_time
        self.last_tick_time = now

        if self.board.turn == chess.WHITE:
            self.white_time = max(0, self.white_time - elapsed)
        else:
            self.black_time = max(0, self.black_time - elapsed)

        self._update_timer_labels()

        # Check for flag fall
        if self.board.turn == chess.WHITE and self.white_time <= 0:
            self._handle_timeout("White")
        elif self.board.turn == chess.BLACK and self.black_time <= 0:
            self._handle_timeout("Black")

    def add_increment(self, color):
        """
        Add the per-move increment to the specified player's clock.
        Called after each legal move.
        """
        if not self.timer_active:
            return
        if color == "White":
            self.white_time += self.white_increment
        else:
            self.black_time += self.black_increment
        self._update_timer_labels()

    def _handle_timeout(self, loser_color):
        """
        A player's clock has hit zero.  Show the game-over dialog and clean up.
        The guard `game_over_shown` prevents the dialog from appearing twice if
        the timer fires on consecutive ticks before the dialog closes.
        """
        if self.game_over_shown:
            return
        self.game_over_shown = True
        self.stop_timer()
        winner = "Black" if loser_color == "White" else "White"
        dlg = GameOverDialog(f"⏰ {loser_color} ran out of time!\n{winner} wins!", self)
        dlg.exec()
        if dlg.clicked_action == "retry":
            self.start_game(self.mode, self.player_color)
        else:
            self.init_menu()

    def _on_timeout_signal(self, loser_color: str):
        """
        Timeout signal emitted from the listener thread.
        We defer to the main loop via QTimer.singleShot to avoid
        any chance of a re-entrant dialog issue.
        """
        if not self.game_over_shown:
            QTimer.singleShot(0, lambda: self._handle_timeout(loser_color))

    def on_timer_sync(self, white_secs, black_secs):
        """
        Server sends TIMER_SYNC periodically to keep client clocks accurate.
        For spectators we also kick off the local interpolation timer if needed.
        """
        self.white_time = white_secs
        self.black_time = black_secs
        self._update_timer_labels()
        if self.is_spectator and self.is_timed_game:
            self._start_spectator_timer()

    def _start_spectator_timer(self):
        """
        Start a client-side interpolation timer for spectators.
        Between TIMER_SYNC messages we tick locally so the clocks don't freeze.
        """
        if self.spectator_timer_qtime is not None:
            return   # already running
        self.spectator_last_tick   = time.time()
        self.spectator_board_turn  = (
            self.spectate_board.turn if self.spectate_board else chess.WHITE
        )
        self.spectator_timer_qtime = QTimer()
        self.spectator_timer_qtime.timeout.connect(self._tick_spectator_timer)
        self.spectator_timer_qtime.start(100)

    def _stop_spectator_timer(self):
        """Stop the spectator interpolation timer when a game ends."""
        if self.spectator_timer_qtime:
            self.spectator_timer_qtime.stop()
            self.spectator_timer_qtime = None

    def _tick_spectator_timer(self):
        """Local clock interpolation for spectators – runs between TIMER_SYNC messages."""
        if not self.is_timed_game or not self.is_spectator:
            return
        board = self.spectate_board
        if board is None or board.is_game_over():
            return

        now     = time.time()
        elapsed = now - self.spectator_last_tick
        self.spectator_last_tick = now

        if board.turn == chess.WHITE:
            self.white_time = max(0, self.white_time - elapsed)
        else:
            self.black_time = max(0, self.black_time - elapsed)
        self._update_timer_labels()

    def _update_timer_labels(self):
        """
        Refresh the timer display labels.
        Colour-codes the time: green = plenty, yellow = getting low, red = critical.
        Shows ∞ for unlimited games.
        """
        def fmt(secs):
            """Format seconds as MM:SS."""
            if secs <= 0:
                return "00:00"
            return f"{int(secs) // 60:02d}:{int(secs) % 60:02d}"

        def color_str(secs):
            """Return a hex colour based on how much time is left."""
            if secs <= 0:  return "#a4b0be"   # grey = flagged
            if secs > 30:  return "#51cf66"   # green = fine
            if secs > 10:  return "#ffd32a"   # yellow = hurry
            return "#ff4757"                   # red = critical

        # Decide whether this game has active timers
        if self.mode == "online" or self.is_spectator:
            is_timed = self.is_timed_game
        else:
            base, _ = self.parse_time_control(self.time_control)
            is_timed = base > 0 and self.timer_active

        fs = self._font_size(14)

        if self.white_timer_label:
            t    = self.white_time
            name = self.white_player_name
            col  = color_str(t) if is_timed else "#a4b0be"
            txt  = f"White: {name} ♔ {fmt(t)}" if is_timed else f"White: {name} ♔ ∞"
            self.white_timer_label.setText(txt)
            self.white_timer_label.setStyleSheet(
                f"color: {col}; font-size: {fs}px; font-weight: bold; "
                f"background: #2f3542; border-radius: 8px; padding: 6px 12px;"
            )

        if self.black_timer_label:
            t    = self.black_time
            name = self.black_player_name
            col  = color_str(t) if is_timed else "#a4b0be"
            txt  = f"Black: {name} ♚ {fmt(t)}" if is_timed else f"Black: {name} ♚ ∞"
            self.black_timer_label.setText(txt)
            self.black_timer_label.setStyleSheet(
                f"color: {col}; font-size: {fs}px; font-weight: bold; "
                f"background: #2f3542; border-radius: 8px; padding: 6px 12px;"
            )

    # ──────────────────────────────────────────────────────────────────────────
    # Cleanup – called before switching between screens
    # ──────────────────────────────────────────────────────────────────────────

    def cleanup(self):
        """
        Reset all game-related state before starting a new game or returning to menu.

        This method is deliberately thorough: every mutable attribute that could
        cause weird behaviour in the next game is explicitly reset here.
        The is_cleaning_up guard prevents re-entrant calls (e.g. if a signal
        fires while we're mid-cleanup).
        """
        if self.is_cleaning_up:
            return
        self.is_cleaning_up = True

        # Stop all timers first to prevent callbacks during teardown
        self.stop_timer()
        self._stop_spectator_timer()
        if self.ping_timer:
            self.ping_timer.stop()
            self.ping_timer = None
        if self.ai_timer:
            self.ai_timer.stop()
            self.ai_timer = None

        self._close_stockfish()

        # Signal the listener thread to exit on its next iteration
        self.should_stop_listening = True
        self.running        = False
        self.waiting_for_ai = False
        self.game_started   = False
        self.animating      = False
        self.anim_from      = None
        self.game_over_shown = False

        # History and navigation
        self.move_history  = []
        self.board_history = []
        self.view_index    = -1
        self.manual_flip   = False
        self.premove       = None

        # Spectator state
        self.is_spectator  = False
        self.spectate_board = None

        # Lobby / session state
        self._session_dialog          = None
        self._pending_sessions        = []
        self._awaiting_session_dialog = False

        # Clock state
        self.white_time      = 0.0
        self.black_time      = 0.0
        self.time_control    = "0"
        self.spectator_count = 0
        self.white_player_name = "White"
        self.black_player_name = "Black"
        self.timer_active    = False
        self.is_timed_game   = False
        self.spectator_timer_qtime = None
        self.spectator_last_tick   = 0

        # Close the network socket and wait briefly for the listener thread to notice
        if self.socket:
            try:
                self.socket.close()
            except Exception:
                pass
            self.socket = None
        if self.listen_thread and self.listen_thread.is_alive():
            self.listen_thread.join(timeout=1)

        # Clear all UI references (the widgets will be deleted by deleteLater)
        self.buttons.clear()
        self.selected_sq           = None
        self.turn_label            = None
        self.sound_btn             = None
        self.ping_label            = None
        self.white_timer_label     = None
        self.black_timer_label     = None
        self.opponent_info_label   = None
        self.spectator_count_label = None
        self.captured_white        = []
        self.captured_black        = []
        self.white_captured_widgets.clear()
        self.black_captured_widgets.clear()

        self.is_cleaning_up = False

    # ──────────────────────────────────────────────────────────────────────────
    # Stockfish engine management
    # ──────────────────────────────────────────────────────────────────────────

    def _open_stockfish(self):
        """
        Launch the Stockfish subprocess and keep a reference to the engine.
        On Windows we hide the console window so the user doesn't see a
        terminal flash open.  Returns True if the engine started successfully.
        """
        if self.stockfish_engine is not None:
            return True   # already open
        if not self.stockfish_path:
            return False

        try:
            import subprocess
            if sys.platform == 'win32':
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags  |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = subprocess.SW_HIDE
                self.stockfish_engine = chess.engine.SimpleEngine.popen_uci(
                    self.stockfish_path, startupinfo=startupinfo
                )
            else:
                self.stockfish_engine = chess.engine.SimpleEngine.popen_uci(
                    self.stockfish_path
                )
            self.stockfish_engine.configure({"Skill Level": self.stockfish_skill})
            return True
        except Exception:
            self.stockfish_engine = None
            return False

    def _close_stockfish(self):
        """Gracefully terminate the Stockfish process if it's running."""
        if self.stockfish_engine is not None:
            try:
                self.stockfish_engine.quit()
            except Exception:
                pass
            self.stockfish_engine = None

    def _stockfish_move_thread(self, fen):
        """
        Run in a daemon thread.  Ask Stockfish for the best move from `fen`
        and emit the result back to the main thread via a signal.

        If Stockfish crashes or times out we fall back to a random legal move
        so the game never freezes waiting for the AI.
        """
        try:
            if not self.running or not self.waiting_for_ai:
                return
            board  = chess.Board(fen)
            result = self.stockfish_engine.play(
                board, chess.engine.Limit(time=self.stockfish_think_time)
            )
            if result.move:
                self.signals.stockfish_move_ready.emit(result.move.uci())
        except Exception:
            if self.running and self.waiting_for_ai:
                board = chess.Board(fen)
                moves = list(board.legal_moves)
                if moves:
                    self.signals.stockfish_move_ready.emit(random.choice(moves).uci())

    def _on_stockfish_move_ready(self, uci: str):
        """
        Main-thread slot: apply the Stockfish move returned from the worker thread.
        We pre-compute capture info before pushing the move so we can update the
        captured-pieces display correctly.
        """
        if not self.running or not self.waiting_for_ai:
            self.waiting_for_ai = False
            return
        if self.board.is_game_over():
            self.waiting_for_ai = False
            self.update_ui()
            return

        try:
            move = chess.Move.from_uci(uci)
        except Exception:
            self.waiting_for_ai = False
            return

        # Determine capture info before the move is pushed
        target = self.board.piece_at(move.to_square)
        cap_type = cap_color = None
        if target:
            cap_type, cap_color = target.piece_type, target.color
        else:
            # Check for en-passant capture
            p = self.board.piece_at(move.from_square)
            if (p and p.piece_type == chess.PAWN
                    and move.to_square == self.board.ep_square):
                ep_sq = move.to_square - 8 if p.color == chess.WHITE else move.to_square + 8
                ep_p  = self.board.piece_at(ep_sq)
                if ep_p:
                    cap_type, cap_color = ep_p.piece_type, ep_p.color
        captured_info = (cap_type, cap_color)

        def after():
            """Callback run after the animation finishes."""
            self.board.push(move)
            if captured_info[0]:
                ct, cc = captured_info
                if cc == chess.WHITE:
                    self.captured_white.append(ct)
                else:
                    self.captured_black.append(ct)
                self.update_captured_display()
                if self.sound_enabled:
                    self.play_random_capture_sound()
            else:
                if self.move_sound and self.sound_enabled:
                    self.move_sound.play()

            self._record_move(move)
            self.selected_sq   = None
            self.my_turn       = True
            self.waiting_for_ai = False

            # Add increment to the AI's side
            ai_color = "Black" if self.player_color == "White" else "White"
            self.add_increment(ai_color)

            if self.turn_label:
                self.turn_label.setText(
                    f"Your Turn ({'White' if self.player_color == 'White' else 'Black'})"
                )
            self.update_ui()

        self.animate_move(move.from_square, move.to_square, move, after)

    # ──────────────────────────────────────────────────────────────────────────
    # Main menu
    # ──────────────────────────────────────────────────────────────────────────

    def init_menu(self):
        """
        Build and display the main menu screen.

        We call cleanup() first so any previous game is properly torn down
        before we replace the central widget.  If we're currently in
        full-screen mode we return to windowed view for the menu.
        """
        if self.isFullScreen():
            self.showNormal()

        self.cleanup()
        self.board   = chess.Board()
        self.running = True
        self.game_started = False

        # Safely remove the old central widget
        if self.centralWidget():
            old = self.centralWidget()
            self.setCentralWidget(None)
            old.deleteLater()

        widget = QWidget()
        widget.setStyleSheet("background-color: #1e272e;")
        outer_layout = QVBoxLayout()
        outer_layout.setSpacing(0)
        outer_layout.setContentsMargins(0, 0, 0, 0)

        # ── Top bar: title + user info ────────────────────────────────────────
        top_bar = QHBoxLayout()
        top_bar.setContentsMargins(30, 20, 30, 0)

        title = QLabel("♟ CHESS PRO")
        title.setStyleSheet(
            "font-size: 48px; font-weight: bold; color: #d2dae2; letter-spacing: 4px;"
        )
        top_bar.addWidget(title)
        top_bar.addStretch()

        if self.my_username:
            # Show username button + sign-out
            user_lbl = QPushButton(f"👤 {self.my_username}")
            user_lbl.setFixedSize(160, 30)
            user_lbl.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            user_lbl.setStyleSheet(
                "QPushButton { background-color: #3d6b8f; color: #d2dae2; border-radius: 8px;"
                " font-size: 12px; padding: 4px 10px; font-weight: bold; }"
                " QPushButton:hover { background-color: #2980b9; }"
            )
            top_bar.addWidget(user_lbl, alignment=Qt.AlignmentFlag.AlignTop)

            signout_btn = QPushButton("🚪 Sign Out")
            signout_btn.setFixedSize(120, 30)
            signout_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            signout_btn.setStyleSheet(
                "QPushButton { background-color: #576574; color: #d2dae2; border-radius: 8px;"
                " font-size: 12px; padding: 4px 10px; }"
                " QPushButton:hover { background-color: #e74c3c; color: white; }"
            )
            signout_btn.clicked.connect(self._sign_out)
            top_bar.addWidget(signout_btn, alignment=Qt.AlignmentFlag.AlignTop)
        else:
            # Show sign-in button for guests
            signin_btn = QPushButton("🔑 SIGN IN")
            signin_btn.setFixedSize(120, 30)
            signin_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            signin_btn.setStyleSheet(
                "QPushButton { background-color: #51cf66; color: white; border-radius: 8px;"
                " font-size: 12px; padding: 4px 10px; font-weight: bold; }"
                " QPushButton:hover { background-color: #3ba856; }"
            )
            signin_btn.clicked.connect(self._show_sign_in)
            top_bar.addWidget(signin_btn, alignment=Qt.AlignmentFlag.AlignTop)

        outer_layout.addLayout(top_bar)

        # ── Mode selection buttons ────────────────────────────────────────────
        layout = QVBoxLayout()
        layout.setSpacing(12)
        layout.setContentsMargins(80, 20, 80, 60)

        # Stats bar – only show when the user is logged in
        if self.my_username and self.my_stats:
            s = self.my_stats
            stats_text = (
                f"👤 {self.my_username}  |  ⭐ ELO: {s.get('elo', 1200)}  |  "
                f"💰 Credits: {s.get('credits', 100)}  |  "
                f"✅ W: {s.get('wins', 0)}  ❌ L: {s.get('losses', 0)}"
                f"  🤝 D: {s.get('draws', 0)}"
            )
            stats_lbl = QLabel(stats_text)
            stats_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            stats_lbl.setStyleSheet(
                "font-size: 13px; color: #ffd32a; background: #2f3542;"
                " border-radius: 10px; padding: 8px; margin-bottom: 10px;"
            )
            layout.addWidget(stats_lbl)
        else:
            sub = QLabel("Choose your game mode")
            sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
            sub.setStyleSheet("font-size: 14px; color: #747d8c; margin-bottom: 20px;")
            layout.addWidget(sub)

        sf_available = self.stockfish_path is not None

        # Each mode button triggers start_game() with the appropriate mode string
        for text, mode in [
            ("⚔️  OFFLINE 2 PLAYER",                                  "offline"),
            ("🤖  AI EASY",                                            "easy"),
            (("🤖  AI HARD ⚡" if sf_available else "🤖  AI HARD"), "hard"),
            ("🌐  ONLINE",                                             "online"),
            ("🎥  SPECTATE",                                           "spectate"),
        ]:
            btn = QPushButton(text)
            btn.setFixedHeight(62)
            btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            color = "#3d5a80" if mode == "spectate" else "#485460"
            btn.setStyleSheet(
                f"QPushButton {{ background-color: {color}; color: white;"
                f" border-radius: 15px; font-size: 18px; }}"
                f" QPushButton:hover {{ background-color: #576574; }}"
            )
            btn.clicked.connect(lambda _, m=mode: self.start_game(m))
            layout.addWidget(btn)

        if not sf_available:
            # Let the user know the "hard" button is degraded
            sf_note = QLabel("⚠️ Stockfish not found — AI HARD uses built-in minimax.")
            sf_note.setAlignment(Qt.AlignmentFlag.AlignCenter)
            sf_note.setWordWrap(True)
            sf_note.setStyleSheet("color: #ffa502; font-size: 11px; margin-top: 6px;")
            layout.addWidget(sf_note)

        outer_layout.addLayout(layout)
        widget.setLayout(outer_layout)
        self.setCentralWidget(widget)

    def _show_sign_in(self):
        """
        Prompt for a server address, then show the auth dialog.
        After a successful login we rebuild the menu to show the stats bar.
        """
        text, ok = QInputDialog.getText(
            self, "Server", "Enter IP:PORT",
            text=self.my_server_addr or "127.0.0.1:12345"
        )
        if not ok or not text:
            return

        dlg = AuthDialog(self)
        dlg.set_connection_info(text.strip())
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        self.my_username      = dlg.result_username
        self.my_stats         = dlg.result_stats or {}
        self.my_session_token = dlg.result_token
        self.my_server_addr   = text.strip()

        # Close the socket created by AuthDialog – we don't need it here
        if dlg.get_socket():
            try:
                dlg.get_socket().close()
            except Exception:
                pass

        save_session(
            self.my_username, self.my_stats,
            self.my_session_token, self.my_server_addr
        )
        self.init_menu()   # rebuild to show the stats bar

    def _sign_out(self):
        """Clear the saved session and return to the unauthenticated menu view."""
        clear_session()
        self.my_username      = None
        self.my_stats         = {}
        self.my_session_token = None
        self.my_server_addr   = None
        self.opponent_username = None
        self.init_menu()

    # ──────────────────────────────────────────────────────────────────────────
    # Game start dispatcher
    # ──────────────────────────────────────────────────────────────────────────

    def start_game(self, mode, player_color=None):
        """
        Entry point for every game mode.  Does common setup then branches
        based on `mode`:

          offline  – two players on the same machine
          easy     – player vs. random-move AI
          hard     – player vs. Stockfish (or minimax fallback)
          online   – player vs. remote player over TCP
          spectate – watch an ongoing online game
        """
        # Online modes require a logged-in account
        if mode in ["online", "spectate"] and not self.my_username:
            QMessageBox.information(
                self, "Sign In Required",
                "Please sign in first.\nClick the SIGN IN button in the top right."
            )
            return

        self.showFullScreen()
        self.cleanup()

        # ── Reset all game state ───────────────────────────────────────────────
        self.mode           = mode
        self.board          = chess.Board()
        self.selected_sq    = None
        self.buttons.clear()
        self.waiting_for_ai = False
        self.running        = True
        self.should_stop_listening = False
        self.previous_check_state  = False
        self.captured_white = []
        self.captured_black = []
        self.white_captured_widgets.clear()
        self.black_captured_widgets.clear()
        self.game_started   = False
        self.animating      = False
        self.anim_from      = None
        self.game_over_shown = False
        self.move_history   = []
        self.board_history  = []
        self.view_index     = -1
        self.manual_flip    = False
        self.premove        = None
        self.is_spectator   = False
        self.spectator_count = 0
        self.white_player_name = "White"
        self.black_player_name = "Black"
        self.timer_active   = False
        self.is_timed_game  = False
        self.white_time     = 0.0
        self.black_time     = 0.0
        self.white_increment = 0
        self.black_increment = 0

        # ── Mode-specific branching ────────────────────────────────────────────

        if mode == "offline":
            dlg = ColorAndTimeControlDialog(
                self, show_color=True, title_prefix="⚔️ Offline "
            )
            if dlg.exec() != QDialog.DialogCode.Accepted:
                self.init_menu()
                return
            chosen_color      = dlg.get_color() if player_color is None else player_color
            self.time_control = dlg.get_time_control()
            self.player_color = chosen_color
            self.my_turn      = True
            self.player_id    = None
            self.game_started = True

            if chosen_color == "White":
                self.white_player_name = "Player 1"
                self.black_player_name = "Player 2"
            else:
                self.white_player_name = "Player 2"
                self.black_player_name = "Player 1"

            if self.sound_enabled and self.game_start_sound:
                self.game_start_sound.play()
            self.init_game_ui()
            self.start_ai_timer()

        elif mode in ["easy", "hard"]:
            prefix = "🤖 AI Easy " if mode == "easy" else "🤖 AI Hard "
            dlg = ColorAndTimeControlDialog(
                self, show_color=True, title_prefix=prefix
            )
            if dlg.exec() != QDialog.DialogCode.Accepted:
                self.init_menu()
                return

            chosen_color      = dlg.get_color() if player_color is None else player_color
            self.time_control = dlg.get_time_control()
            self.player_color = chosen_color
            self.player_id    = 1 if chosen_color == "White" else 2
            self.my_turn      = chosen_color == "White"
            self.game_started = True

            self.white_player_name = "You" if chosen_color == "White" else "AI"
            self.black_player_name = "AI"  if chosen_color == "White" else "You"

            if self.sound_enabled and self.game_start_sound:
                self.game_start_sound.play()
            self.init_game_ui()
            self.start_ai_timer()

            if mode == "hard":
                if self.stockfish_path and not self._open_stockfish():
                    QMessageBox.warning(
                        self, "Stockfish Error",
                        "Could not start Stockfish. Falling back to built-in AI."
                    )

            # If the player chose Black, the AI goes first
            if chosen_color == "Black":
                self.waiting_for_ai = True
                self.ai_timer = QTimer()
                self.ai_timer.setSingleShot(True)
                self.ai_timer.timeout.connect(self.ai_move)
                self.ai_timer.start(500)

        elif mode == "online":
            self._connect_and_show_sessions(spectate=False)

        elif mode == "spectate":
            self._connect_and_show_sessions(spectate=True)

    # ──────────────────────────────────────────────────────────────────────────
    # Online: connection + session-dialog flow
    # ──────────────────────────────────────────────────────────────────────────

    def _connect_and_show_sessions(self, spectate=False):
        """
        Attempt to (re)connect to the server and open the lobby dialog.

        If we have a saved token we try a silent AUTO_LOGIN first.  This means
        users who haven't explicitly signed out don't see the login dialog again.
        If AUTO_LOGIN fails (token expired, server restarted, etc.) we fall back
        to showing the full auth dialog.
        """
        self.is_spectator = spectate

        if self.my_session_token and self.my_server_addr:
            sock = self._try_auto_login(self.my_server_addr, self.my_session_token)
            if sock:
                self.socket = sock
                self.socket.settimeout(None)
                self._start_listener()
                self._request_sessions_then_show_dialog(spectate)
                return

        # No saved session or auto-login failed – ask the user
        addr_text, ok = QInputDialog.getText(
            self, "Connect", "Enter IP:PORT",
            text=self.my_server_addr or "127.0.0.1:12345"
        )
        if not ok or not addr_text:
            self.init_menu()
            return

        dlg = AuthDialog(self)
        dlg.set_connection_info(addr_text.strip())
        if dlg.exec() != QDialog.DialogCode.Accepted:
            self.init_menu()
            return

        self.my_username      = dlg.result_username
        self.my_stats         = dlg.result_stats or {}
        self.my_session_token = dlg.result_token
        self.my_server_addr   = addr_text.strip()
        self.socket           = dlg.get_socket()

        if not self.socket:
            QMessageBox.critical(self, "Error", "Could not establish connection.")
            self.init_menu()
            return

        self.socket.settimeout(None)
        save_session(
            self.my_username, self.my_stats,
            self.my_session_token, self.my_server_addr
        )
        self._start_listener()
        self._request_sessions_then_show_dialog(spectate)

    def _try_auto_login(self, server_addr, token):
        """
        Attempt a quick token-based re-login.
        Returns the live socket on success, None on any failure.
        """
        try:
            ip, port = server_addr.split(":")
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(8)
            sock.connect((ip, int(port)))
            sock.send(f"AUTO_LOGIN|{token}\n".encode())

            resp = b""
            while b"\n" not in resp:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                resp += chunk

            line = resp.split(b"\n")[0].decode().strip()
            if line.startswith("AUTH_OK|"):
                parts = line.split("|", 3)
                self.my_username      = parts[1]
                self.my_stats         = json.loads(parts[2]) if len(parts) > 2 else {}
                self.my_session_token = parts[3] if len(parts) > 3 else token
                save_session(
                    self.my_username, self.my_stats,
                    self.my_session_token, server_addr
                )
                return sock
            else:
                sock.close()
                return None
        except Exception:
            return None

    def _start_listener(self):
        """
        Spawn the background TCP listener thread and start the ping heartbeat.
        The listener thread runs until should_stop_listening is set to True.
        """
        self.should_stop_listening = False
        self.listen_thread = threading.Thread(target=self.listen, daemon=True)
        self.listen_thread.start()

        # Ping every 3 seconds to keep the connection alive and measure latency
        self.ping_timer = QTimer()
        self.ping_timer.timeout.connect(self._send_ping)
        self.ping_timer.start(3000)

    def _request_sessions_then_show_dialog(self, spectate):
        """
        Ask the server for the current session list.
        The listener thread will emit session_list_received when the response
        arrives, which triggers on_session_list_received → _show_session_dialog.
        """
        self._awaiting_session_dialog = True
        self._spectate_mode_pending   = spectate
        if self.socket:
            try:
                self.socket.send("SESSION_LIST_REQUEST\n".encode())
            except Exception:
                pass

    # ──────────────────────────────────────────────────────────────────────────
    # Session dialog handlers
    # ──────────────────────────────────────────────────────────────────────────

    def on_session_list_received(self, json_str):
        """
        Called on the main thread when a SESSION_LIST message arrives.

        If the lobby dialog is already open we just refresh its list.
        Otherwise, if we were waiting to show it, we do so now.
        """
        try:
            sessions = json.loads(json_str)
        except Exception:
            sessions = []
        self._pending_sessions = sessions

        if self._session_dialog and self._session_dialog.isVisible():
            self._session_dialog.refresh(sessions)
            return

        if self._awaiting_session_dialog:
            self._awaiting_session_dialog = False
            spectate = getattr(self, '_spectate_mode_pending', False)
            # Small delay so the UI can finish any pending layout work
            QTimer.singleShot(50, lambda: self._show_session_dialog(spectate, sessions))

    def _show_session_dialog(self, spectate, sessions):
        """
        Show the lobby dialog and act on the user's choice.

        After the dialog closes we send the appropriate command to the server
        and either enter the game UI (create/join) or wait for a SPECTATE_OK.
        """
        mode_str = "spectate" if spectate else "online"
        dlg = SessionDialog(sessions, mode=mode_str, parent=self)
        self._session_dialog = dlg
        result = dlg.exec()
        self._session_dialog = None

        if result != QDialog.DialogCode.Accepted:
            self.init_menu()
            return

        if dlg.result_action == "create":
            self.player_color = dlg.result_color
            if self.socket:
                try:
                    self.socket.send(
                        f"CREATE_SESSION|{dlg.result_tc}|{dlg.result_color}\n".encode()
                    )
                except Exception:
                    self.init_menu()
            self.init_game_ui()

        elif dlg.result_action == "join":
            if self.socket:
                try:
                    self.socket.send(f"JOIN_SESSION|{dlg.result_session_id}\n".encode())
                except Exception:
                    self.init_menu()
            self.init_game_ui()

        elif dlg.result_action == "spectate":
            if self.socket:
                try:
                    self.socket.send(
                        f"SPECTATE_SESSION|{dlg.result_session_id}\n".encode()
                    )
                except Exception:
                    self.init_menu()

    def on_session_created(self, sid):
        """Server confirms our session was created.  Update the turn label."""
        if self.turn_label:
            color_icon = "♔" if self.player_color == "White" else "♚"
            self.turn_label.setText(
                f"{color_icon} You are {self.player_color} — waiting for opponent..."
            )

    def on_join_fail(self, reason):
        """
        The session we tried to join no longer exists (full, started, etc.).
        Re-request the session list and show the lobby again.
        """
        QMessageBox.warning(self, "Join Failed", reason)
        self._awaiting_session_dialog = True
        self._spectate_mode_pending   = self.is_spectator
        if self.socket:
            try:
                self.socket.send("SESSION_LIST_REQUEST\n".encode())
            except Exception:
                self.init_menu()

    # ──────────────────────────────────────────────────────────────────────────
    # Ping / latency display
    # ──────────────────────────────────────────────────────────────────────────

    def _send_ping(self):
        """Send a PING message and record the timestamp so we can measure RTT."""
        if self.socket:
            try:
                self.ping_sent_time = time.time()
                self.socket.send("PING\n".encode())
            except Exception:
                pass

    def _handle_pong(self):
        """Server replied with PONG – compute round-trip time in milliseconds."""
        self.ping_ms = int((time.time() - self.ping_sent_time) * 1000)
        if self.ping_label:
            self.ping_label.setText(f"🟢 {self.ping_ms}ms")

    # ──────────────────────────────────────────────────────────────────────────
    # Online game actions (chat, draw, resign, rematch)
    # ──────────────────────────────────────────────────────────────────────────

    def send_chat(self):
        """Send the current chat input to the server and display it locally."""
        if not self.socket or not self.game_started:
            return
        message = self.chat_input.text().strip()
        if message:
            try:
                self.socket.send(f"CHAT|{message}\n".encode())
                self.chat_display.append(
                    f"<b style='color:#51cf66'>You:</b> {message}"
                )
                self.chat_input.clear()
            except Exception:
                pass

    def send_emoji(self, emoji):
        """Append an emoji to the chat input field without sending it yet."""
        if self.chat_input:
            self.chat_input.setText(self.chat_input.text() + emoji)
            self.chat_input.setFocus()

    def offer_draw(self):
        """Offer a draw to the opponent."""
        if not self.socket or not self.game_started:
            return
        try:
            self.socket.send("DRAW_OFFER\n".encode())
            QMessageBox.information(self, "Draw", "Draw offer sent.")
        except Exception:
            pass

    def resign_game(self):
        """Ask for confirmation, then resign the current game."""
        if not self.game_started:
            return
        reply = QMessageBox.question(
            self, "Resign", "Are you sure you want to resign?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            if self.socket:
                try:
                    self.socket.send("RESIGN\n".encode())
                except Exception:
                    pass
            self.game_over_shown = True
            dlg = GameOverDialog("You resigned. Opponent wins.", self)
            dlg.exec()
            if dlg.clicked_action == "retry":
                self.start_game(self.mode, self.player_color)
            else:
                self.init_menu()

    def request_rematch(self):
        """Ask the opponent for a rematch after the game ends."""
        if not self.socket:
            return
        try:
            self.socket.send("REMATCH_REQUEST\n".encode())
            QMessageBox.information(self, "Rematch", "Rematch request sent.")
        except Exception:
            pass

    # ──────────────────────────────────────────────────────────────────────────
    # Signal handlers – called on the main thread by connected signals
    # ──────────────────────────────────────────────────────────────────────────

    def on_waiting(self):
        """Server says our session is open and waiting for an opponent."""
        if self.turn_label:
            self.turn_label.setText("🔍 Waiting for opponent...")

    def on_chat_received(self, message):
        """Display a chat message from the opponent."""
        if self.chat_display:
            self.chat_display.append(
                f"<b style='color:#ff6b6b'>Opponent:</b> {message}"
            )

    def on_player_assigned(self, player_id: int, color: str):
        """Server tells us which color we're playing.  Update names + board orientation."""
        self.player_id    = player_id
        self.player_color = color
        if color == "White":
            self.white_player_name = self.my_username or "White"
        else:
            self.black_player_name = self.my_username or "Black"
        self.setWindowTitle(f"Chess Pro — You are {color}")
        self.draw_board()
        self._update_timer_labels()
        if self.turn_label and not self.game_started:
            icon = "♔" if color == "White" else "♚"
            self.turn_label.setText(
                f"{icon} You are {color} — waiting for opponent..."
            )

    def on_game_started(self, tc_str, base_seconds, increment):
        """
        Both players are connected – the real game begins.
        Initialise clocks and update the turn label.
        """
        self.game_started = True
        self.time_control = tc_str
        self.my_turn      = (self.player_color == "White")

        if base_seconds > 0:
            self.white_time      = float(base_seconds)
            self.black_time      = float(base_seconds)
            self.white_increment = increment
            self.black_increment = increment
            self.timer_active    = True
            self.is_timed_game   = True
        else:
            self.white_time = self.black_time = 0
            self.timer_active  = False
            self.is_timed_game = False

        self._update_timer_labels()
        if self.turn_label:
            icon     = "♔" if self.my_turn else "♚"
            turn_str = "Your Turn" if self.my_turn else "Opponent's Turn"
            self.turn_label.setText(f"{icon} {turn_str} ({self.player_color})")

        if self.sound_enabled and self.game_start_sound:
            self.game_start_sound.play()
        self.draw_board()

    def on_opponent_left(self):
        """The opponent disconnected mid-game.  Return to menu after a warning."""
        QMessageBox.warning(self, "Disconnected", "Your opponent has left the game.")
        self.init_menu()

    def on_opponent_info(self, username, stats_json):
        """Server sends the opponent's name and stats when they join the session."""
        self.opponent_username = username
        try:
            self.opponent_stats = json.loads(stats_json)
        except Exception:
            self.opponent_stats = {}

        # Update player-name labels for the timer display
        if self.player_color == "White":
            self.white_player_name = self.my_username or "White"
            self.black_player_name = username
        else:
            self.white_player_name = username
            self.black_player_name = self.my_username or "Black"

        self._update_timer_labels()

        if self.opponent_info_label:
            s = self.opponent_stats
            self.opponent_info_label.setText(
                f"🎯 {username}  ELO: {s.get('elo','?')}  "
                f"W:{s.get('wins',0)} L:{s.get('losses',0)} D:{s.get('draws',0)}"
            )

    def on_stats_update(self, stats_json):
        """Server sends updated stats after a rated game finishes."""
        try:
            self.my_stats = json.loads(stats_json)
            if self.my_session_token and self.my_server_addr:
                save_session(
                    self.my_username, self.my_stats,
                    self.my_session_token, self.my_server_addr
                )
        except Exception:
            pass

    def on_draw_offer_received(self):
        """Opponent offered a draw.  Ask the player to accept or decline."""
        reply = QMessageBox.question(
            self, "Draw Offer", "Opponent offers a draw. Accept?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            try:
                self.socket.send("DRAW_ACCEPT\n".encode())
            except Exception:
                pass
            self.game_over_shown = True
            dlg = GameOverDialog("Draw by agreement.", self)
            dlg.exec()
            if dlg.clicked_action == "retry":
                self.start_game(self.mode, self.player_color)
            else:
                self.init_menu()
        else:
            try:
                self.socket.send("DRAW_DECLINE\n".encode())
            except Exception:
                pass

    def on_draw_declined(self):
        QMessageBox.information(self, "Draw", "Opponent declined your draw offer.")

    def on_draw_accepted(self):
        self.game_over_shown = True
        dlg = GameOverDialog("Opponent accepted the draw!", self)
        dlg.exec()
        if dlg.clicked_action == "retry":
            self.start_game(self.mode, self.player_color)
        else:
            self.init_menu()

    def on_rematch_received(self):
        """Opponent wants a rematch.  Swap colors if the player agrees."""
        reply = QMessageBox.question(
            self, "Rematch", "Opponent wants a rematch! Accept?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        new_color = "Black" if self.player_color == "White" else "White"
        if reply == QMessageBox.StandardButton.Yes:
            try:
                self.socket.send("REMATCH_ACCEPT\n".encode())
            except Exception:
                pass
            self.start_game(self.mode, new_color)
        else:
            try:
                self.socket.send("REMATCH_DECLINE\n".encode())
            except Exception:
                pass

    def on_rematch_accepted(self):
        new_color = "Black" if self.player_color == "White" else "White"
        self.start_game(self.mode, new_color)

    def on_rematch_declined(self):
        QMessageBox.information(self, "Rematch", "Opponent declined the rematch.")

    def on_opponent_resigned(self):
        if self.game_over_shown:
            return
        self.game_over_shown = True
        dlg = GameOverDialog("Opponent resigned. You win! 🏆", self)
        dlg.exec()
        if dlg.clicked_action == "retry":
            self.start_game(self.mode, self.player_color)
        else:
            self.init_menu()

    def on_spectator_count(self, count):
        """Update the spectator badge in the info panel."""
        self.spectator_count = count
        if self.spectator_count_label:
            self.spectator_count_label.setText(f"👁 {count}")

    # ── Spectate signal handlers ───────────────────────────────────────────────

    def on_spectate_ok(self, info_json):
        """
        Server confirms we're now spectating.  The payload contains the full
        game state (moves played so far, current timers, player names).
        Replay all moves to reconstruct the board, then show the spectator UI.
        """
        try:
            info = json.loads(info_json)
        except Exception:
            info = {}

        # Replay moves to bring the board up to date
        self.spectate_board = chess.Board()
        for uci in info.get("moves", []):
            try:
                self.spectate_board.push(chess.Move.from_uci(uci))
            except Exception:
                pass

        # Restore timers
        timers = info.get("timers", [0, 0])
        self.white_time = float(timers[0]) if timers[0] else 0
        self.black_time = float(timers[1]) if timers[1] else 0
        tc = info.get("time_control", 0)
        self.time_control = str(tc) if tc else "0"
        base, _ = self.parse_time_control(self.time_control)
        self.is_timed_game = base > 0

        self.white_player_name = info.get("white", "White")
        self.black_player_name = info.get("black", "Black")

        self.init_spectator_ui(info)
        if self.is_timed_game:
            self._start_spectator_timer()

    def on_spectate_fail(self, reason):
        """Spectate request was rejected (game ended, full, etc.)."""
        QMessageBox.warning(self, "Spectate Failed", reason)
        self.init_menu()

    def on_spectate_move(self, uci):
        """A move was played in the game we're watching – update the board."""
        if self.spectate_board is None:
            return
        try:
            move = chess.Move.from_uci(uci)
            self.spectate_board.push(move)
            self._draw_spectator_board()
        except Exception as e:
            print(f"Error in on_spectate_move: {e}")
            return
        self.spectator_board_turn = self.spectate_board.turn
        self._update_timer_labels()

    def on_spectate_chat(self, sender, msg):
        """Display a chat message from one of the players we're watching."""
        if self.chat_display:
            self.chat_display.append(
                f"<b style='color:#ffd32a'>{sender}:</b> {msg}"
            )

    def on_spectate_event(self, text):
        """
        Show a game event (checkmate, resign, draw, disconnect) in the turn label
        and stop the local clock since the game is over.
        """
        if self.turn_label:
            self.turn_label.setText(text)
            self.turn_label.setStyleSheet(
                "color: #ffd32a; font-size: 15px; font-weight: bold; padding: 8px;"
            )
        game_ending_keywords = ["resigned", "Checkmate", "Draw", "disconnected"]
        if any(kw in text for kw in game_ending_keywords):
            self._stop_spectator_timer()

    def on_challenge_received(self, challenger, tc):
        """Another online player has challenged us to a game."""
        tc_str = tc if tc != "0" else "Unlimited"
        reply = QMessageBox.question(
            self, "Challenge!",
            f"⚔️ {challenger} challenges you to a {tc_str} game!\nAccept?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            if self.socket:
                try:
                    self.socket.send(
                        f"CHALLENGE_ACCEPT_TC|{challenger}|{tc}\n".encode()
                    )
                except Exception:
                    pass
        else:
            if self.socket:
                try:
                    self.socket.send(f"CHALLENGE_DECLINE|{challenger}\n".encode())
                except Exception:
                    pass

    def on_challenge_declined(self, who):
        QMessageBox.information(self, "Challenge Declined", f"{who} declined your challenge.")

    def on_challenge_fail(self, reason):
        QMessageBox.warning(self, "Challenge Failed", reason)

    # ──────────────────────────────────────────────────────────────────────────
    # Spectator UI
    # ──────────────────────────────────────────────────────────────────────────

    def init_spectator_ui(self, info):
        """
        Build the spectator screen layout.
        Layout: [left panel: game info + chat] | [right panel: board + timers]
        """
        if self.centralWidget():
            old = self.centralWidget()
            self.setCentralWidget(None)
            old.deleteLater()

        widget = QWidget()
        widget.setStyleSheet("background-color: #1e272e;")
        main_layout = QHBoxLayout()
        main_layout.setSpacing(10)
        main_layout.setContentsMargins(10, 10, 10, 10)

        # ── Left panel: game info + read-only chat ─────────────────────────
        left_widget = QWidget()
        left_widget.setMinimumWidth(200)
        left_widget.setMaximumWidth(360)
        left_widget.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        left_layout = QVBoxLayout()
        left_layout.setSpacing(8)

        info_lbl = QLabel(
            f"🎥 SPECTATING\n"
            f"♔ {info.get('white','?')} ({info.get('white_elo','?')}) vs "
            f"♚ {info.get('black','?')} ({info.get('black_elo','?')})"
        )
        info_lbl.setStyleSheet(
            "color: #ffd32a; font-size: 13px; font-weight: bold;"
            " background: #2f3542; border-radius: 8px; padding: 8px;"
        )
        info_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        info_lbl.setWordWrap(True)
        left_layout.addWidget(info_lbl)

        # Read-only chat so spectators can follow the players' conversation
        chat_group = QWidget()
        chat_group.setStyleSheet("background-color: #2f3542; border-radius: 12px;")
        chat_layout = QVBoxLayout()
        chat_layout.setContentsMargins(8, 8, 8, 8)
        chat_lbl = QLabel("💬 GAME CHAT")
        chat_lbl.setStyleSheet("color: white; font-weight: bold; font-size: 13px;")
        chat_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.chat_display = QTextEdit()
        self.chat_display.setReadOnly(True)
        self.chat_display.setMaximumHeight(300)
        self.chat_display.setStyleSheet(
            "QTextEdit { background-color: #1e272e; color: #d2dae2;"
            " border-radius: 8px; padding: 6px; font-size: 12px; }"
        )
        chat_layout.addWidget(chat_lbl)
        chat_layout.addWidget(self.chat_display)
        chat_group.setLayout(chat_layout)
        left_layout.addWidget(chat_group)
        left_layout.addStretch()
        left_widget.setLayout(left_layout)
        main_layout.addWidget(left_widget, 1)

        # ── Right panel: board + timers + back button ─────────────────────
        right_widget = QWidget()
        right_widget.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        right_layout = QVBoxLayout()
        right_layout.setSpacing(6)

        top_row = QHBoxLayout()
        back_btn = QPushButton("⬅ MENU")
        back_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        back_btn.setFixedHeight(40)
        back_btn.setStyleSheet(
            "QPushButton { background: #576574; color: white; border-radius: 10px;"
            " padding: 6px 14px; font-weight: bold; }"
            " QPushButton:hover { background: #ff7979; }"
        )
        back_btn.clicked.connect(self.init_menu)
        top_row.addWidget(back_btn)
        top_row.addStretch()

        self.sound_btn = QPushButton("🔊 MUTE")
        self.sound_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.sound_btn.setFixedHeight(40)
        self.sound_btn.setStyleSheet(
            "QPushButton { background: #485460; color: white; border-radius: 10px;"
            " padding: 6px 14px; font-size: 13px; }"
            " QPushButton:hover { background: #ff6b6b; }"
        )
        self.sound_btn.clicked.connect(self.toggle_sounds)
        top_row.addWidget(self.sound_btn)
        right_layout.addLayout(top_row)

        # Timer labels
        timer_row = QHBoxLayout()
        self.white_timer_label = QLabel()
        self.black_timer_label = QLabel()
        for lbl in [self.white_timer_label, self.black_timer_label]:
            lbl.setStyleSheet(
                "color: #51cf66; font-size: 14px; font-weight: bold;"
                " background: #2f3542; border-radius: 8px; padding: 6px 12px;"
            )
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        timer_row.addWidget(self.white_timer_label)
        timer_row.addStretch()
        timer_row.addWidget(self.black_timer_label)
        right_layout.addLayout(timer_row)
        self._update_timer_labels()

        # The board widget itself
        self.grid_widget = SquareBoardWidget()
        self.grid_widget.setStyleSheet("""
            SquareBoardWidget {
                background-color: #2f3542;
                border-radius: 25px;
                padding: 5px;
            }
        """)
        self.grid_widget.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.grid = self.grid_widget.grid
        self.grid_widget.resized.connect(self._on_board_resized)
        self._draw_spectator_board()

        # Centre the board horizontally
        board_container = QWidget()
        board_container_layout = QHBoxLayout(board_container)
        board_container_layout.setContentsMargins(0, 0, 0, 0)
        board_container_layout.addStretch()
        board_container_layout.addWidget(self.grid_widget)
        board_container_layout.addStretch()
        right_layout.addWidget(board_container, 1)

        self.turn_label = QLabel("🎥 Spectating...")
        self.turn_label.setStyleSheet(
            "color: #ffd32a; font-size: 15px; font-weight: bold; padding: 8px;"
        )
        self.turn_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        right_layout.addWidget(self.turn_label)

        right_widget.setLayout(right_layout)
        main_layout.addWidget(right_widget, 3)
        widget.setLayout(main_layout)
        self.setCentralWidget(widget)
        self._update_timer_labels()

    def _draw_spectator_board(self):
        """
        Re-render the spectator board from scratch.
        Spectators can't interact with pieces so we don't attach click handlers.
        """
        if self.grid is None:
            return

        # Clear the existing grid
        while self.grid.count():
            item = self.grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.buttons.clear()

        sq      = self.grid_widget.current_sq_size() if self.grid_widget else self._BASE_SQ
        icon_sz = int(sq * 0.72)
        board   = self.spectate_board or chess.Board()

        for r in range(8):
            for c in range(8):
                square = chess.square(c, 7 - r)
                btn    = QPushButton()
                btn.setFixedSize(sq, sq)
                btn.setEnabled(True)
                btn.clicked.connect(lambda: None)   # no-op for spectators

                is_dark = (r + c) % 2 != 0
                bg      = "#485460" if is_dark else "#57606f"
                btn.setStyleSheet(f"QPushButton {{ background-color: {bg}; border: none; }}")

                piece = board.piece_at(square)
                if piece:
                    ip = self.pieces.get((piece.piece_type, piece.color))
                    if ip and os.path.exists(ip):
                        btn.setIcon(QIcon(ip))
                        btn.setIconSize(QSize(icon_sz, icon_sz))

                self.grid.addWidget(btn, r, c)
                self.buttons[square] = btn

    def animate_spectator_move(self, from_sq, to_sq, move):
        """
        Show a brief animation when a move is played in the game we're watching.
        A floating piece label glides from the source square to the target square.
        """
        from_btn = self.buttons.get(from_sq)
        to_btn   = self.buttons.get(to_sq)
        if not from_btn or not to_btn:
            return

        piece = self.spectate_board.piece_at(from_sq)
        if not piece:
            return

        icon_path = self.pieces.get((piece.piece_type, piece.color))
        if not icon_path or not os.path.exists(icon_path):
            return

        icon_sz  = int(self._sq_size * 0.72)
        floating = QLabel(self)
        floating.setPixmap(QIcon(icon_path).pixmap(QSize(icon_sz, icon_sz)))
        floating.setFixedSize(icon_sz, icon_sz)
        floating.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        floating.show()

        half  = icon_sz // 2
        start = self.mapFromGlobal(from_btn.mapToGlobal(from_btn.rect().center()))
        end   = self.mapFromGlobal(to_btn.mapToGlobal(to_btn.rect().center()))
        floating.move(start - QPoint(half, half))

        anim = QPropertyAnimation(floating, b"pos")
        anim.setDuration(200)
        anim.setStartValue(start - QPoint(half, half))
        anim.setEndValue(end   - QPoint(half, half))
        anim.setEasingCurve(QEasingCurve.Type.OutQuad)
        anim.finished.connect(floating.deleteLater)
        anim.start()

    # ──────────────────────────────────────────────────────────────────────────
    # Network listener thread
    # ──────────────────────────────────────────────────────────────────────────

    def listen(self):
        """
        Background thread: continuously read lines from the TCP socket and
        dispatch them as Qt signals so the main thread can update the GUI safely.

        The protocol is text-based, newline-delimited.  Each incoming line has
        the form  "COMMAND|payload\n"  (or just  "COMMAND\n"  for simple events).

        We use a 1-second socket timeout so we can check should_stop_listening
        between reads without blocking forever on a dead connection.
        """
        buffer = ""
        while not self.should_stop_listening and self.running:
            try:
                self.socket.settimeout(1.0)
                data = self.socket.recv(4096).decode('utf-8', errors='replace')
                if not data:
                    continue

                buffer += data

                # Process all complete lines we have so far
                while '\n' in buffer:
                    line, buffer = buffer.split('\n', 1)
                    line = line.strip()
                    if not line:
                        continue

                    # ── Dispatch by message prefix ────────────────────────────

                    if line.startswith("PLAYER|"):
                        parts = line.split("|")
                        pid   = int(parts[1]) if len(parts) > 1 else 1
                        color = parts[2]       if len(parts) > 2 else "White"
                        if color not in ("White", "Black"):
                            color = "White" if pid == 1 else "Black"
                        self.signals.player_assigned.emit(pid, color)

                    elif line.startswith("GAME_START"):
                        parts    = line.split("|")
                        tc_str   = parts[1] if len(parts) > 1 else "0"
                        base     = int(parts[2]) if len(parts) > 2 else 0
                        inc      = int(parts[3]) if len(parts) > 3 else 0
                        self.signals.game_started_signal.emit(tc_str, base, inc)

                    elif line.startswith("WAITING|"):
                        self.signals.waiting_signal.emit()

                    elif line.startswith("SESSION_CREATED|"):
                        self.signals.session_created.emit(line.split("|", 1)[1])

                    elif line.startswith("JOIN_FAIL|"):
                        self.signals.join_fail.emit(line.split("|", 1)[1])

                    elif line.startswith("SESSION_LIST|"):
                        self.signals.session_list_received.emit(line.split("|", 1)[1])

                    elif line.startswith("MOVE|"):
                        self.signals.move_received.emit(line.split("|", 1)[1])

                    elif line.startswith("CHAT|"):
                        self.signals.chat_received.emit(line.split("|", 1)[1])

                    elif line == "DRAW_OFFER":
                        self.signals.draw_offer_received.emit()
                    elif line == "DRAW_DECLINE":
                        self.signals.draw_declined_received.emit()
                    elif line == "DRAW_ACCEPT":
                        self.signals.draw_accepted_received.emit()
                    elif line == "REMATCH_REQUEST":
                        self.signals.rematch_received.emit()
                    elif line == "REMATCH_ACCEPT":
                        self.signals.rematch_accepted.emit()
                    elif line == "REMATCH_DECLINE":
                        self.signals.rematch_declined.emit()
                    elif line in ("OPPONENT_RESIGNED", "RESIGN"):
                        self.signals.opponent_resigned.emit()

                    elif line.startswith("OPPONENT_INFO|"):
                        parts = line.split("|", 2)
                        uname = parts[1] if len(parts) > 1 else "?"
                        sj    = parts[2] if len(parts) > 2 else "{}"
                        self.signals.opponent_info.emit(uname, sj)

                    elif line.startswith("STATS_UPDATE|"):
                        self.signals.stats_update.emit(line.split("|", 1)[1])

                    elif line == "PONG":
                        self._handle_pong()

                    elif line.startswith("TIMER_SYNC|"):
                        parts = line.split("|")
                        try:
                            w = float(parts[1])
                            b = float(parts[2])
                            self.signals.timer_sync.emit(w, b)
                        except Exception:
                            pass

                    elif line.startswith("TIMEOUT|"):
                        try:
                            idx   = int(line.split("|")[1])
                            loser = "White" if idx == 0 else "Black"
                            self.signals.timeout_signal.emit(loser)
                        except Exception:
                            pass

                    elif line == "OPPONENT_LEFT":
                        self.signals.opponent_left.emit()
                        break   # Stop listening – game is over

                    elif line.startswith("SPECTATOR_COUNT|"):
                        try:
                            count = int(line.split("|")[1])
                            self.signals.spectator_count_signal.emit(count)
                        except Exception:
                            pass

                    elif line.startswith("SPECTATE_OK|"):
                        self.signals.spectate_ok.emit(line.split("|", 1)[1])
                    elif line.startswith("SPECTATE_FAIL|"):
                        self.signals.spectate_fail.emit(line.split("|", 1)[1])
                    elif line.startswith("SPECTATE_MOVE|"):
                        self.signals.spectate_move.emit(line.split("|", 1)[1])
                    elif line.startswith("SPECTATE_CHAT|"):
                        parts  = line.split("|", 2)
                        sender = parts[1] if len(parts) > 1 else "?"
                        msg    = parts[2] if len(parts) > 2 else ""
                        self.signals.spectate_chat.emit(sender, msg)
                    elif line.startswith("SPECTATE_EVENT|"):
                        self.signals.spectate_event.emit(line.split("|", 1)[1])
                    elif line.startswith("SPECTATE_RESIGN|"):
                        who = line.split("|", 1)[1]
                        self.signals.spectate_event.emit(f"🏳️ {who} resigned")
                    elif line == "SPECTATE_DRAW":
                        self.signals.spectate_event.emit("🤝 Draw agreed")
                    elif line.startswith("SPECTATE_CHECKMATE|"):
                        who = line.split("|", 1)[1]
                        self.signals.spectate_event.emit(f"♟ Checkmate! {who} wins!")
                    elif line.startswith("SPECTATE_DISCONNECT|"):
                        who = line.split("|", 1)[1]
                        self.signals.spectate_event.emit(f"🔌 {who} disconnected")
                    elif line.startswith("CHALLENGE_RECEIVED|"):
                        parts      = line.split("|", 2)
                        challenger = parts[1] if len(parts) > 1 else "?"
                        tc         = parts[2] if len(parts) > 2 else "0"
                        self.signals.challenge_received.emit(challenger, tc)
                    elif line.startswith("CHALLENGE_DECLINED|"):
                        self.signals.challenge_declined.emit(line.split("|", 1)[1])
                    elif line.startswith("CHALLENGE_FAIL|"):
                        self.signals.challenge_fail.emit(line.split("|", 1)[1])

            except socket.timeout:
                continue   # Normal – check the stop flag and keep going
            except Exception:
                break      # Socket error – exit the loop

        # Clean up the socket on exit
        if self.socket:
            try:
                self.socket.close()
            except Exception:
                pass
        self.socket = None

    # ──────────────────────────────────────────────────────────────────────────
    # Sound helpers
    # ──────────────────────────────────────────────────────────────────────────

    def play_check_sound(self):
        if self.sound_enabled and self.check_sound:
            self.check_sound.play()

    def init_sounds(self):
        """
        Load all sound effects from disk.  Missing files are silently skipped –
        the game is fully playable without sound assets.
        """
        def load_sfx(path):
            """Helper: create a QSoundEffect from a file path, or return None."""
            if os.path.exists(path):
                s = QSoundEffect()
                s.setSource(QUrl.fromLocalFile(path))
                return s
            return None

        self.click_sound      = load_sfx(resource_path("click.wav"))
        self.move_sound       = load_sfx(resource_path("move.wav"))
        self.check_sound      = load_sfx(resource_path("check.wav"))
        self.game_start_sound = load_sfx(resource_path("start.wav"))
        self.game_end_sound   = load_sfx(resource_path("end.wav"))

        # Load all .wav/.mp3/.ogg files from the capture_sounds/ folder
        self.capture_sounds = []
        sounds_folder = resource_path("capture_sounds")
        if os.path.exists(sounds_folder):
            for f in os.listdir(sounds_folder):
                if f.lower().endswith(('.wav', '.mp3', '.ogg')):
                    s = load_sfx(os.path.join(sounds_folder, f))
                    if s:
                        self.capture_sounds.append(s)

    def play_background_music(self):
        """
        Try to start looping background music.  We try .mp3 first, then
        .ogg, then .wav, stopping as soon as we find one that exists.
        """
        if not self.music_enabled:
            return
        self.background_music = QMediaPlayer()
        self.audio_output      = QAudioOutput()
        self.background_music.setAudioOutput(self.audio_output)
        for ext in [".mp3", ".ogg", ".wav"]:
            fp = resource_path(f"background{ext}")
            if os.path.exists(fp):
                self.background_music.setSource(QUrl.fromLocalFile(os.path.abspath(fp)))
                self.audio_output.setVolume(0.2)   # quiet background, not distracting
                self.background_music.setLoops(QMediaPlayer.Loops.Infinite)
                self.background_music.play()
                return

    def load_capture_effects(self):
        """
        Load all PNG images from the capture_effects/ folder.
        One is shown at random after each capture (the comic-book "POW!" style flash).
        """
        self.capture_effect_images = []
        folder = resource_path("capture_effects")
        if os.path.exists(folder):
            for f in os.listdir(folder):
                if f.lower().endswith('.png'):
                    try:
                        self.capture_effect_images.append(
                            QIcon(os.path.join(folder, f)).pixmap(QSize(120, 120))
                        )
                    except Exception:
                        pass

    def play_random_capture_sound(self):
        """Play a random capture sound and briefly show a visual effect."""
        if not self.sound_enabled:
            return
        if self.capture_sounds:
            random.choice(self.capture_sounds).play()
        self.show_capture_effect()

    def show_capture_effect(self):
        """Flash a random capture-effect image for 1 second."""
        if not self.capture_effect_images or not self.capture_effect_label:
            return
        self.capture_effect_label.setPixmap(random.choice(self.capture_effect_images))
        self.capture_effect_label.show()
        if not self.capture_effect_timer:
            self.capture_effect_timer = QTimer()
            self.capture_effect_timer.setSingleShot(True)
            self.capture_effect_timer.timeout.connect(self.hide_capture_effect)
        self.capture_effect_timer.start(1000)

    def hide_capture_effect(self):
        if self.capture_effect_label:
            self.capture_effect_label.hide()

    def toggle_sounds(self):
        """Toggle sound on/off and update the mute button label accordingly."""
        self.sound_enabled = not self.sound_enabled
        if self.sound_btn:
            self.sound_btn.setText("🔇 UNMUTE" if not self.sound_enabled else "🔊 MUTE")
        if not self.sound_enabled:
            if self.background_music:
                self.background_music.stop()
        else:
            self.play_background_music()

    # ──────────────────────────────────────────────────────────────────────────
    # Game UI layout
    # ──────────────────────────────────────────────────────────────────────────

    def init_game_ui(self):
        """
        Build the in-game layout.

        Structure:
          [left panel: captured pieces + chat (online only)]
          [right panel: back btn | mute btn, timers, board, turn label, nav buttons]

        The board container has stretch on both sides to centre it horizontally.
        """
        if self.centralWidget():
            old = self.centralWidget()
            self.setCentralWidget(None)
            old.deleteLater()

        widget = QWidget()
        widget.setStyleSheet("background-color: #1e272e;")
        main_layout = QHBoxLayout()
        main_layout.setSpacing(10)
        main_layout.setContentsMargins(10, 10, 10, 10)

        # ── Left panel ────────────────────────────────────────────────────────
        left_widget = QWidget()
        left_widget.setMinimumWidth(180)
        left_widget.setMaximumWidth(380)
        left_widget.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        left_layout = QVBoxLayout()
        left_layout.setSpacing(8)
        left_layout.addWidget(self.init_captured_display())

        if self.mode == "online":
            # ── Online-only: opponent info + ping + chat ──────────────────────
            info_group  = QWidget()
            info_group.setStyleSheet("background-color: #2f3542; border-radius: 12px;")
            ig_layout = QVBoxLayout()
            ig_layout.setContentsMargins(8, 8, 8, 8)
            ig_layout.setSpacing(4)

            my_lbl = QLabel(f"👤 You: {self.my_username or 'Unknown'}")
            my_lbl.setStyleSheet("color: #51cf66; font-size: 12px; font-weight: bold;")

            s = self.my_stats
            my_stats_lbl = QLabel(
                (f"ELO: {s.get('elo','?')}  W:{s.get('wins',0)} "
                 f"L:{s.get('losses',0)} D:{s.get('draws',0)}  💰{s.get('credits',0)}")
                if s else ""
            )
            my_stats_lbl.setStyleSheet("color: #a4b0be; font-size: 11px;")

            self.opponent_info_label = QLabel("🎯 Waiting for opponent...")
            self.opponent_info_label.setStyleSheet(
                "color: #ff6b6b; font-size: 12px; font-weight: bold;"
            )
            self.opponent_info_label.setWordWrap(True)

            spec_row = QHBoxLayout()
            self.spectator_count_label = QLabel("👁 0")
            self.spectator_count_label.setStyleSheet("color: #a4b0be; font-size: 12px;")
            self.ping_label = QLabel("🟡 ---ms")
            self.ping_label.setStyleSheet("color: #a4b0be; font-size: 11px;")
            spec_row.addWidget(self.spectator_count_label)
            spec_row.addStretch()
            spec_row.addWidget(self.ping_label)

            ig_layout.addWidget(my_lbl)
            ig_layout.addWidget(my_stats_lbl)
            ig_layout.addWidget(self.opponent_info_label)
            ig_layout.addLayout(spec_row)
            info_group.setLayout(ig_layout)
            left_layout.addWidget(info_group)

            # Chat panel
            chat_group = QWidget()
            chat_group.setStyleSheet("background-color: #2f3542; border-radius: 12px;")
            chat_layout = QVBoxLayout()
            chat_layout.setContentsMargins(8, 8, 8, 8)

            chat_title = QLabel("💬 CHAT")
            chat_title.setStyleSheet("color: white; font-weight: bold; font-size: 14px;")
            chat_title.setAlignment(Qt.AlignmentFlag.AlignCenter)

            self.chat_display = QTextEdit()
            self.chat_display.setReadOnly(True)
            self.chat_display.setMaximumHeight(120)
            self.chat_display.setStyleSheet(
                "QTextEdit { background-color: #1e272e; color: #d2dae2;"
                " border-radius: 8px; padding: 6px; font-size: 12px; }"
            )

            # Quick-emoji buttons so people don't have to type :)
            emoji_row = QHBoxLayout()
            emoji_row.setSpacing(4)
            for emoji in ["😭", "😠", "😊", "😄", "🤔", "😏", "🥱", "😲"]:
                ebtn = QPushButton(emoji)
                ebtn.setFixedSize(32, 32)
                ebtn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
                ebtn.setStyleSheet(
                    "QPushButton { background-color: #485460; border-radius: 6px;"
                    " font-size: 14px; }"
                    " QPushButton:hover { background-color: #576574; }"
                )
                ebtn.clicked.connect(lambda _, e=emoji: self.send_emoji(e))
                emoji_row.addWidget(ebtn)
            emoji_row.addStretch()

            row = QHBoxLayout()
            self.chat_input = QLineEdit()
            self.chat_input.setPlaceholderText("Type a message...")
            self.chat_input.setStyleSheet(
                "QLineEdit { background-color: #1e272e; color: white;"
                " border-radius: 8px; padding: 6px; }"
            )
            self.chat_input.returnPressed.connect(self.send_chat)

            send_btn = QPushButton("Send")
            send_btn.setFixedWidth(55)
            send_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            send_btn.setStyleSheet(
                "QPushButton { background-color: #51cf66; color: white;"
                " border-radius: 8px; padding: 6px; }"
                " QPushButton:hover { background-color: #3ba856; }"
            )
            send_btn.clicked.connect(self.send_chat)
            row.addWidget(self.chat_input)
            row.addWidget(send_btn)

            # Game actions (draw, resign, rematch)
            action_row = QHBoxLayout()
            for label, slot, color in [
                ("🤝 Draw",    self.offer_draw,     "#f39c12"),
                ("🏳️ Resign", self.resign_game,    "#e74c3c"),
                ("🔄 Rematch", self.request_rematch, "#3d6b8f"),
            ]:
                b = QPushButton(label)
                b.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
                b.setStyleSheet(
                    f"QPushButton {{ background-color: {color}; color: white;"
                    f" border-radius: 8px; padding: 5px; font-size: 11px; }}"
                )
                b.clicked.connect(slot)
                action_row.addWidget(b)

            chat_layout.addWidget(chat_title)
            chat_layout.addWidget(self.chat_display)
            chat_layout.addLayout(emoji_row)
            chat_layout.addLayout(row)
            chat_layout.addLayout(action_row)
            chat_group.setLayout(chat_layout)
            left_layout.addWidget(chat_group)

        left_layout.addStretch()

        # Capture visual effect label (shows "POW!" images)
        self.capture_effect_label = QLabel()
        self.capture_effect_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.capture_effect_label.setStyleSheet("background: transparent;")
        self.capture_effect_label.setFixedSize(120, 120)
        self.capture_effect_label.hide()
        left_layout.addWidget(self.capture_effect_label, alignment=Qt.AlignmentFlag.AlignCenter)

        left_widget.setLayout(left_layout)
        main_layout.addWidget(left_widget, 1)

        # ── Right panel (board area) ───────────────────────────────────────────
        right_widget = QWidget()
        right_widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        right_layout = QVBoxLayout()
        right_layout.setSpacing(6)

        # Top row: back btn, fullscreen hint, mute btn
        top_row = QHBoxLayout()
        back_btn = QPushButton("⬅ MENU")
        back_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        back_btn.setFixedHeight(40)
        back_btn.setStyleSheet(
            "QPushButton { background: #576574; color: white; border-radius: 10px;"
            " padding: 6px 14px; font-weight: bold; font-size: 13px; }"
            " QPushButton:hover { background: #ff7979; }"
        )
        back_btn.clicked.connect(self.init_menu)
        top_row.addWidget(back_btn)
        top_row.addStretch()

        fs_label = QLabel("🖥 FULL SCREEN (F11 to exit)")
        fs_label.setStyleSheet("color: #a4b0be; font-size: 11px;")
        fs_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        top_row.addWidget(fs_label)

        self.sound_btn = QPushButton("🔊 MUTE")
        self.sound_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.sound_btn.setFixedHeight(40)
        self.sound_btn.setStyleSheet(
            "QPushButton { background: #485460; color: white; border-radius: 10px;"
            " padding: 6px 14px; font-size: 13px; }"
            " QPushButton:hover { background: #ff6b6b; }"
        )
        self.sound_btn.clicked.connect(self.toggle_sounds)
        top_row.addWidget(self.sound_btn)
        right_layout.addLayout(top_row)

        # Timers
        timer_row = QHBoxLayout()
        self.white_timer_label = QLabel()
        self.black_timer_label = QLabel()
        for lbl in [self.white_timer_label, self.black_timer_label]:
            lbl.setStyleSheet(
                "color: #51cf66; font-size: 14px; font-weight: bold;"
                " background: #2f3542; border-radius: 8px; padding: 6px 12px;"
            )
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        timer_row.addWidget(self.white_timer_label)
        timer_row.addStretch()
        timer_row.addWidget(self.black_timer_label)
        right_layout.addLayout(timer_row)
        self._update_timer_labels()

        # Board – centred with stretch on both sides
        board_center_container = QWidget()
        board_center_layout    = QHBoxLayout(board_center_container)
        board_center_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        board_center_layout.setContentsMargins(0, 0, 0, 0)
        board_center_layout.addStretch()

        self.grid_widget = SquareBoardWidget()
        self.grid_widget.setStyleSheet("""
            SquareBoardWidget {
                background-color: #2f3542;
                border-radius: 25px;
                padding: 5px;
            }
        """)
        self.grid_widget.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.grid = self.grid_widget.grid
        self.grid_widget.resized.connect(self._on_board_resized)
        self.draw_board()
        board_center_layout.addWidget(self.grid_widget)
        board_center_layout.addStretch()
        right_layout.addWidget(board_center_container, 1)

        # Turn / status label
        self.turn_label = QLabel()
        if self.mode == "offline":
            self.turn_label.setText("White's Turn")
        elif self.mode in ["easy", "hard"]:
            if self.player_color == "White":
                self.turn_label.setText("Your Turn (White)")
            else:
                self.turn_label.setText("AI's Turn (White)")
        else:
            if self.player_color and self.player_color != "White":
                icon = "♚"
                self.turn_label.setText(
                    f"{icon} You are {self.player_color} — waiting for opponent..."
                )
            else:
                self.turn_label.setText("🔍 Waiting for opponent...")
        self.turn_label.setStyleSheet(
            "color: white; font-size: 16px; font-weight: bold; padding: 8px;"
        )
        self.turn_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        right_layout.addWidget(self.turn_label)

        if self.mode in ["easy", "hard"]:
            lbl = QLabel(f"Playing as: {self.player_color}")
            lbl.setStyleSheet("color: #a4b0be; font-size: 12px; padding: 3px;")
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            right_layout.addWidget(lbl)

        # Bottom navigation: history << >>, flip
        btn_style = (
            "QPushButton {{ background: {bg}; color: white; border-radius: 10px;"
            " padding: 8px 0px; font-size: 15px; font-weight: bold; }}"
            " QPushButton:hover {{ background: {hv}; }}"
        )
        bottom_row = QHBoxLayout()
        bottom_row.setSpacing(8)
        for text, tip, slot, bg, hv in [
            ("⏮", "Previous move",          self.go_to_previous,  "#485460", "#576574"),
            ("⏭", "Next / return to live",  self.go_to_next,      "#485460", "#576574"),
            ("🔄 FLIP", "Flip board",         self.flip_board_manual, "#3d6b8f", "#2980b9"),
        ]:
            b = QPushButton(text)
            b.setToolTip(tip)
            b.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            b.setFixedHeight(42)
            b.setStyleSheet(btn_style.format(bg=bg, hv=hv))
            b.clicked.connect(slot)
            bottom_row.addWidget(b)
        right_layout.addLayout(bottom_row)

        right_widget.setLayout(right_layout)
        main_layout.addWidget(right_widget, 3)
        widget.setLayout(main_layout)
        self.setCentralWidget(widget)
        self.update_captured_display()

    # ──────────────────────────────────────────────────────────────────────────
    # Board orientation helpers
    # ──────────────────────────────────────────────────────────────────────────

    def should_flip_board(self):
        """
        Decide whether to flip the board (show it from Black's perspective).

        Rules:
          online/AI  – flip if we're playing Black
          offline    – flip to match whose turn it is (optional nice-to-have)
          manual_flip – XOR with the above so the player can always toggle

        The XOR with manual_flip means one click always toggles the current state.
        """
        if self.mode == "online":
            base = (self.player_color == "Black")
        elif self.mode in ["easy", "hard"]:
            base = (self.player_color == "Black")
        elif self.mode == "offline":
            base = (self.board.turn == chess.BLACK)
        else:
            base = False
        return base ^ self.manual_flip

    # ──────────────────────────────────────────────────────────────────────────
    # Board drawing
    # ──────────────────────────────────────────────────────────────────────────

    def draw_board(self):
        """
        Clear and rebuild all 64 board-square buttons.
        Called whenever the board orientation changes (flip, new game, etc.).

        After this method we call update_ui() to overlay piece icons and
        highlight states on top of the freshly built buttons.
        """
        if self.grid is None:
            return

        # Remove all existing child widgets from the grid
        while self.grid.count():
            item = self.grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.buttons.clear()

        sq = (
            self.grid_widget.current_sq_size()
            if isinstance(self.grid_widget, SquareBoardWidget)
            else self._BASE_SQ
        )
        self._sq_size = sq
        icon_sz = int(sq * 0.72)
        flip    = self.should_flip_board()

        for r in range(8):
            for c in range(8):
                # Convert grid row/col to chess square index
                file_ = c       if not flip else (7 - c)
                rank_ = (7 - r) if not flip else r
                square = chess.square(file_, rank_)

                btn = QPushButton()
                btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
                btn.setFixedSize(sq, sq)
                btn.setFont(QFont("Arial", max(8, sq // 4)))
                btn.setIconSize(QSize(icon_sz, icon_sz))
                btn.setStyleSheet(self._sq_color_style(r, c))

                # Left-click: move piece / select square
                btn.clicked.connect(lambda _, s=square: self.handle_click(s))
                # Right-click: cancel pre-move
                btn.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
                btn.customContextMenuRequested.connect(
                    lambda _, s=square: self._right_click(s)
                )
                self.grid.addWidget(btn, r, c)
                self.buttons[square] = btn

        self.update_ui()

    def _sq_color_style(self, grid_row, grid_col, highlight=None):
        """Return a basic stylesheet string for a light or dark square."""
        is_dark = (grid_row + grid_col) % 2 != 0
        bg = "#485460" if is_dark else "#57606f"
        return f"background:{bg}; border: none;"

    # ──────────────────────────────────────────────────────────────────────────
    # Pre-move (queue a move while it's the opponent's turn)
    # ──────────────────────────────────────────────────────────────────────────

    def _right_click(self, sq):
        """Right-clicking any square clears the queued pre-move."""
        if self.premove:
            self.premove = None
            self._clear_premove_highlight()
            self.update_ui()

    def _set_premove(self, from_sq, to_sq, promotion=None):
        """Store a pre-move and show the purple highlight."""
        self.premove = (from_sq, to_sq, promotion)
        self._highlight_premove()

    def _clear_premove(self):
        self.premove = None
        self._clear_premove_highlight()

    def _highlight_premove(self):
        """Purple highlight on pre-move source and target squares."""
        self._clear_premove_highlight()
        if not self.premove:
            return
        from_sq, to_sq, _ = self.premove
        for sq in [from_sq, to_sq]:
            btn = self.buttons.get(sq)
            if btn:
                btn.setStyleSheet(
                    "background: rgba(128,0,200,180); border: 2px solid #cc00ff;"
                )

    def _clear_premove_highlight(self):
        """Restore normal colours on the two highlighted pre-move squares."""
        flip = self.should_flip_board()
        for sq, btn in self.buttons.items():
            file_ = chess.square_file(sq)
            rank_ = chess.square_rank(sq)
            grid_r = (7 - rank_) if not flip else rank_
            grid_c = file_       if not flip else (7 - file_)
            btn.setStyleSheet(self._sq_color_style(grid_r, grid_c))

    def _try_execute_premove(self):
        """
        After the opponent moves, attempt to execute any queued pre-move.
        If it's no longer legal (the opponent's reply changed the position)
        we silently discard it.
        """
        if not self.premove:
            return
        from_sq, to_sq, promo = self.premove
        self._clear_premove()

        move = chess.Move(from_sq, to_sq, promotion=promo or chess.QUEEN)
        if move not in self.board.legal_moves:
            move = chess.Move(from_sq, to_sq)
            if move not in self.board.legal_moves:
                return   # The pre-move was invalidated by the opponent's reply
        self._execute_move(move)

    # ──────────────────────────────────────────────────────────────────────────
    # Click handling
    # ──────────────────────────────────────────────────────────────────────────

    def handle_click(self, sq):
        """
        Process a click on board square `sq`.

        Two-click move entry:
          First click  → select the piece (highlighted in yellow)
          Second click → attempt the move, or re-select if another own piece clicked

        Pre-move (online only, not our turn):
          First click  → select own piece while it's still the opponent's turn
          Second click → queue a pre-move (purple highlight); executes automatically
                         when the opponent moves

        History browsing:
          If view_index != -1 we're looking at a past position.  Clicking
          anything returns us to the live position.
        """
        if not self.running or self.animating:
            return
        if self.is_spectator:
            return   # spectators can't interact

        # A click during history review returns to live view
        if self.view_index != -1:
            self.view_index = -1
            self.update_ui()
            return

        if self.sound_enabled and self.click_sound:
            self.click_sound.play()

        # Don't accept moves before the game officially starts
        if self.mode == "online"    and not self.game_started:
            return
        if self.mode in ["easy", "hard"] and self.waiting_for_ai:
            return

        # ── Pre-move logic (online, not my turn) ──────────────────────────────
        if self.mode == "online" and not self.my_turn and self.game_started:
            my_chess_color = chess.WHITE if self.player_color == "White" else chess.BLACK
            if self.selected_sq is None:
                piece = self.board.piece_at(sq)
                if piece and piece.color == my_chess_color:
                    self.selected_sq = sq
                    self.update_ui()
            else:
                from_sq = self.selected_sq
                piece   = self.board.piece_at(from_sq)
                if piece:
                    promo = None
                    if piece.piece_type == chess.PAWN:
                        tr = chess.square_rank(sq)
                        if (my_chess_color == chess.WHITE and tr == 7) or \
                           (my_chess_color == chess.BLACK and tr == 0):
                            promo = chess.QUEEN
                    self._set_premove(from_sq, sq, promo)
                self.selected_sq = None
                self.update_ui()
            return

        # Online mode with disconnected socket
        if self.mode == "online" and not self.socket:
            QMessageBox.warning(self, "Disconnected", "Not connected.")
            self.init_menu()
            return

        # ── First click: select a piece ────────────────────────────────────────
        if self.selected_sq is None:
            piece = self.board.piece_at(sq)
            if not piece:
                return

            # Enforce color restriction per mode
            if self.mode == "offline":
                if piece.color != self.board.turn:
                    return
            elif self.mode in ["easy", "hard"]:
                pc = chess.WHITE if self.player_color == "White" else chess.BLACK
                if piece.color != pc:
                    return
            elif self.mode == "online" and self.player_color:
                ec = chess.WHITE if self.player_color == "White" else chess.BLACK
                if piece.color != ec:
                    return

            self.selected_sq = sq
            self.update_ui()
            return

        # ── Second click: attempt a move ───────────────────────────────────────
        from_sq = self.selected_sq
        piece   = self.board.piece_at(from_sq)
        move    = chess.Move(from_sq, sq)

        # Handle pawn promotion dialog
        if piece and piece.piece_type == chess.PAWN:
            tr = chess.square_rank(sq)
            if ((piece.color == chess.WHITE and tr == 7) or
                    (piece.color == chess.BLACK and tr == 0)):
                if chess.Move(from_sq, sq, promotion=chess.QUEEN) in self.board.legal_moves:
                    choice, ok = QInputDialog.getItem(
                        self, "Promotion", "Promote to:",
                        ["Queen", "Rook", "Knight", "Bishop"], 0, False,
                    )
                    if not ok:
                        self.selected_sq = None
                        self.update_ui()
                        return
                    promo_map = {
                        "Queen": chess.QUEEN, "Rook": chess.ROOK,
                        "Knight": chess.KNIGHT, "Bishop": chess.BISHOP,
                    }
                    move = chess.Move(from_sq, sq, promotion=promo_map[choice])

        # If the move is illegal, maybe re-select another piece
        if move not in self.board.legal_moves:
            new_piece = self.board.piece_at(sq)
            if new_piece:
                # Check if this is one of our own pieces – if so, re-select
                can_select = False
                if self.mode == "offline":
                    can_select = new_piece.color == self.board.turn
                elif self.mode in ["easy", "hard"]:
                    pc = chess.WHITE if self.player_color == "White" else chess.BLACK
                    can_select = new_piece.color == pc
                elif self.mode == "online" and self.player_color:
                    ec = chess.WHITE if self.player_color == "White" else chess.BLACK
                    can_select = new_piece.color == ec
                if can_select:
                    self.selected_sq = sq
                    self.update_ui()
                    return
            self.selected_sq = None
            self.update_ui()
            return

        self._execute_move(move)

    def _execute_move(self, move):
        """
        Apply a legal move made by the local player.

        Order of operations:
          1. Record capture info (before the move is pushed)
          2. Animate the piece from source → target
          3. Push the move onto the board inside the animation callback
          4. Update captured pieces, send the move to the server (online),
             or trigger the AI response
        """
        from_sq = move.from_square
        sq      = move.to_square
        piece   = self.board.piece_at(from_sq)
        target  = self.board.piece_at(sq)

        # Pre-record capture details so we can display them after the animation
        cap_type = cap_color = None
        if target:
            cap_type, cap_color = target.piece_type, target.color
        elif piece and piece.piece_type == chess.PAWN and sq == self.board.ep_square:
            # En-passant: the captured pawn is on a different square
            ep_sq = sq - 8 if piece.color == chess.WHITE else sq + 8
            ep_p  = self.board.piece_at(ep_sq)
            if ep_p:
                cap_type, cap_color = ep_p.piece_type, ep_p.color
        captured_info = (cap_type, cap_color)

        def do_move():
            self.board.push(move)

            if captured_info[0]:
                ct, cc = captured_info
                if cc == chess.WHITE:
                    self.captured_white.append(ct)
                else:
                    self.captured_black.append(ct)
                if self.sound_enabled:
                    self.play_random_capture_sound()

            self._record_move(move)
            self.selected_sq = None

            # Add time increment for the player who just moved
            if self.mode in ["easy", "hard", "offline"] and self.timer_active:
                if self.board.turn == chess.WHITE:
                    self.add_increment("Black")   # Black just moved
                else:
                    self.add_increment("White")   # White just moved

            if self.mode == "offline":
                # In offline mode both players share one screen – just flip the turn
                self.my_turn = not self.my_turn
                self.draw_board()

            elif self.mode in ["easy", "hard"]:
                # Trigger the AI response after a short delay (feels more natural)
                self.my_turn       = False
                self.waiting_for_ai = True
                QTimer.singleShot(300, self.ai_move)

            elif self.mode == "online":
                try:
                    self.socket.send(f"MOVE|{move.uci()}\n".encode())
                    self.my_turn = False
                except Exception:
                    self.init_menu()
                    return

            self.update_ui()
            self.update_captured_display()

        self.animate_move(from_sq, sq, move, do_move)

    def process_opponent_move(self, move_uci):
        """
        Apply a move received from the server (opponent's move in online play).
        After applying it we check for a queued pre-move and execute it if valid.
        """
        if not self.game_started:
            return
        try:
            move = chess.Move.from_uci(move_uci)
        except Exception:
            return
        if move not in self.board.legal_moves:
            return

        target = self.board.piece_at(move.to_square)
        cap_type = cap_color = None
        if target:
            cap_type, cap_color = target.piece_type, target.color
        else:
            piece = self.board.piece_at(move.from_square)
            if (piece and piece.piece_type == chess.PAWN
                    and move.to_square == self.board.ep_square):
                ep_sq = (move.to_square - 8 if piece.color == chess.WHITE
                         else move.to_square + 8)
                ep_p = self.board.piece_at(ep_sq)
                if ep_p:
                    cap_type, cap_color = ep_p.piece_type, ep_p.color
        captured_info = (cap_type, cap_color)

        def do_move():
            self.board.push(move)
            if captured_info[0]:
                ct, cc = captured_info
                if cc == chess.WHITE:
                    self.captured_white.append(ct)
                else:
                    self.captured_black.append(ct)
                if self.sound_enabled:
                    self.play_random_capture_sound()
            self._record_move(move)
            self.selected_sq = None
            self.my_turn     = True
            if self.turn_label:
                self.turn_label.setText("Your Turn")
            self.update_ui()
            self.update_captured_display()
            # Try to fire any queued pre-move now that it's our turn
            QTimer.singleShot(50, self._try_execute_premove)

        self.animate_move(move.from_square, move.to_square, move, do_move)

    # ──────────────────────────────────────────────────────────────────────────
    # Move history navigation
    # ──────────────────────────────────────────────────────────────────────────

    def _record_move(self, move):
        """
        Save the move and a FEN snapshot after it was pushed.
        Called after every legal move regardless of game mode.
        """
        self.move_history.append(move)
        self.board_history.append(self.board.fen())
        self.view_index = -1   # snap back to live view

    def go_to_previous(self):
        """Step backward through move history (⏮ button)."""
        if not self.board_history:
            return
        if self.view_index == -1:
            self.view_index = len(self.board_history) - 1
        elif self.view_index > 0:
            self.view_index -= 1
        else:
            return
        self._show_history_board()

    def go_to_next(self):
        """Step forward through history, or return to live position (⏭ button)."""
        if self.view_index == -1:
            return
        if self.view_index < len(self.board_history) - 1:
            self.view_index += 1
            self._show_history_board()
        else:
            # We've reached the latest move – return to live view
            self.view_index = -1
            self.update_ui()

    def _show_history_board(self):
        """Render a historical board position without changing the actual game state."""
        temp = chess.Board(self.board_history[self.view_index])
        self._render_board_state(temp)
        if self.turn_label:
            n     = self.view_index + 1
            total = len(self.board_history)
            self.turn_label.setText(f"📖 Move {n}/{total}  — ⏮ ⏭ to navigate")
            self.turn_label.setStyleSheet(
                "color: #ffd32a; font-size: 14px; font-weight: bold; padding: 10px;"
            )

    def flip_board_manual(self):
        """Toggle the board orientation (🔄 FLIP button)."""
        self.manual_flip = not self.manual_flip
        self.draw_board()

    # ──────────────────────────────────────────────────────────────────────────
    # Move animation
    # ──────────────────────────────────────────────────────────────────────────

    def animate_move(self, from_sq, to_sq, move, after_callback=None):
        """
        Animate a piece gliding from from_sq to to_sq over ~170 ms.

        We create a temporary floating QLabel on top of the main window,
        hide the piece icons on the source and target buttons, run the animation,
        then call after_callback() once it finishes.

        The `animating` flag blocks any further clicks until the animation ends.
        """
        from_btn = self.buttons.get(from_sq)
        to_btn   = self.buttons.get(to_sq)
        if not from_btn or not to_btn:
            if after_callback:
                after_callback()
            return

        piece = self.board.piece_at(from_sq)
        if not piece:
            if after_callback:
                after_callback()
            return

        icon_path = self.pieces.get((piece.piece_type, piece.color))
        if not icon_path or not os.path.exists(icon_path):
            if after_callback:
                after_callback()
            return

        self.animating = True
        self.anim_from = from_sq

        # Clear icons from the buttons involved so we don't see a double piece
        from_btn.setIcon(QIcon())
        to_btn.setIcon(QIcon())
        QApplication.processEvents()

        # Create the floating piece image
        icon_sz  = int(self._sq_size * 0.72)
        floating = QLabel(self)
        floating.setPixmap(QIcon(icon_path).pixmap(QSize(icon_sz, icon_sz)))
        floating.setFixedSize(icon_sz, icon_sz)
        floating.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        floating.show()

        # Map button centres to main-window coordinates for the animation
        half  = icon_sz // 2
        start = self.mapFromGlobal(from_btn.mapToGlobal(from_btn.rect().center()))
        end   = self.mapFromGlobal(to_btn.mapToGlobal(to_btn.rect().center()))
        floating.move(start - QPoint(half, half))

        anim = QPropertyAnimation(floating, b"pos")
        anim.setDuration(170)
        anim.setStartValue(start - QPoint(half, half))
        anim.setEndValue(end   - QPoint(half, half))
        anim.setEasingCurve(QEasingCurve.Type.OutQuad)

        def finish():
            floating.deleteLater()
            self.animating = False
            self.anim_from = None
            if after_callback:
                after_callback()
            self.update_ui()

        anim.finished.connect(finish)
        anim.start()
        self.current_animation = anim   # keep a reference so it isn't GC'd

    # ──────────────────────────────────────────────────────────────────────────
    # AI engine
    # ──────────────────────────────────────────────────────────────────────────

    def ai_move(self):
        """
        Ask the AI for its move.

        Easy mode:  random legal move (instant)
        Hard mode:  Stockfish in a worker thread, or minimax as fallback
        """
        if not self.running or not self.waiting_for_ai:
            self.waiting_for_ai = False
            return
        if self.board.is_game_over():
            self.waiting_for_ai = False
            self.update_ui()
            return
        moves = list(self.board.legal_moves)
        if not moves:
            self.waiting_for_ai = False
            return

        if self.mode == "easy":
            self._apply_ai_move_sync(random.choice(moves))
        elif self.mode == "hard":
            if self.stockfish_engine:
                # Fire-and-forget: the signal stockfish_move_ready brings the result back
                fen = self.board.fen()
                t   = threading.Thread(
                    target=self._stockfish_move_thread, args=(fen,), daemon=True
                )
                t.start()
            else:
                # Stockfish isn't available – use our built-in minimax
                self._apply_ai_move_sync(self.best_move())

    def _apply_ai_move_sync(self, move):
        """
        Apply an AI move that was chosen synchronously (easy mode / minimax).
        Shares the same capture-detection + animation flow as human moves.
        """
        target = self.board.piece_at(move.to_square)
        cap_type = cap_color = None
        if target:
            cap_type, cap_color = target.piece_type, target.color
        else:
            p = self.board.piece_at(move.from_square)
            if (p and p.piece_type == chess.PAWN
                    and move.to_square == self.board.ep_square):
                ep_sq = (move.to_square - 8 if p.color == chess.WHITE
                         else move.to_square + 8)
                ep_p  = self.board.piece_at(ep_sq)
                if ep_p:
                    cap_type, cap_color = ep_p.piece_type, ep_p.color
        captured_info = (cap_type, cap_color)

        def after():
            self.board.push(move)
            if captured_info[0]:
                ct, cc = captured_info
                if cc == chess.WHITE:
                    self.captured_white.append(ct)
                else:
                    self.captured_black.append(ct)
                self.update_captured_display()
                if self.sound_enabled:
                    self.play_random_capture_sound()
            else:
                if self.move_sound and self.sound_enabled:
                    self.move_sound.play()

            self._record_move(move)
            self.selected_sq    = None
            self.my_turn        = True
            self.waiting_for_ai = False

            ai_color = "Black" if self.player_color == "White" else "White"
            self.add_increment(ai_color)

            if self.turn_label:
                self.turn_label.setText(
                    f"Your Turn ({'White' if self.player_color == 'White' else 'Black'})"
                )
            self.update_ui()

        self.animate_move(move.from_square, move.to_square, move, after)

    # ── Minimax (hard AI fallback) ────────────────────────────────────────────

    def best_move(self):
        """
        Pick the best move using alpha-beta minimax (depth 3).
        Used as a fallback when Stockfish is unavailable.
        """
        best, best_val = None, -float('inf')
        for move in self.order_moves(list(self.board.legal_moves)):
            self.board.push(move)
            val = self.minimax(3, -float('inf'), float('inf'), False)
            self.board.pop()
            if val > best_val:
                best_val, best = val, move
        return best

    def minimax(self, depth, alpha, beta, maximizing):
        """
        Standard alpha-beta minimax search.
        Positive scores favour the AI (always the side that moved last),
        negative scores favour the opponent.
        """
        if depth == 0 or self.board.is_game_over():
            return self.evaluate_board_advanced()

        moves = self.order_moves(list(self.board.legal_moves))
        if maximizing:
            mx = -float('inf')
            for m in moves:
                self.board.push(m)
                mx    = max(mx, self.minimax(depth - 1, alpha, beta, False))
                alpha = max(alpha, mx)
                self.board.pop()
                if beta <= alpha:
                    break   # Beta cut-off
            return mx
        else:
            mn = float('inf')
            for m in moves:
                self.board.push(m)
                mn   = min(mn, self.minimax(depth - 1, alpha, beta, True))
                beta = min(beta, mn)
                self.board.pop()
                if beta <= alpha:
                    break   # Alpha cut-off
            return mn

    def evaluate_board_advanced(self):
        """
        Static board evaluation function.

        Score components:
          • Material balance (classic piece values)
          • Central control bonus (d4/e4/d5/e5 occupation)
          • Mobility (number of legal moves available)

        A positive score means White is better; negative means Black is better.
        We negate the score when it's Black's turn so minimax always searches
        from the AI's perspective.
        """
        if self.board.is_checkmate():
            return -999999 if self.board.turn else 999999
        if self.board.is_stalemate():
            return 0

        values = {
            chess.PAWN:   100,
            chess.KNIGHT: 320,
            chess.BISHOP: 330,
            chess.ROOK:   500,
            chess.QUEEN:  900,
            chess.KING:   20000,
        }
        score = sum(
            (len(self.board.pieces(pt, chess.WHITE))
             - len(self.board.pieces(pt, chess.BLACK))) * v
            for pt, v in values.items()
        )

        # Small bonus for occupying or attacking the four central squares
        for sq in [chess.D4, chess.E4, chess.D5, chess.E5]:
            p = self.board.piece_at(sq)
            if p:
                score += 30 if p.color == chess.WHITE else -30

        # Mobility bonus encourages development and active play
        score += len(list(self.board.legal_moves)) * 3

        return score if self.board.turn == chess.WHITE else -score

    def order_moves(self, moves):
        """
        Sort moves to improve alpha-beta pruning efficiency.
        Captures and checks are searched first because they're most likely to
        cause a cutoff, reducing the effective branching factor significantly.
        """
        def score(m):
            s = 1000 if self.board.is_capture(m) else 0
            self.board.push(m)
            if self.board.is_check():
                s += 500
            self.board.pop()
            return s
        return sorted(moves, key=score, reverse=True)

    # ──────────────────────────────────────────────────────────────────────────
    # Captured-piece display
    # ──────────────────────────────────────────────────────────────────────────

    def update_captured_display(self):
        """
        Refresh the captured-pieces panels on the left side of the board.
        We rebuild from scratch each time rather than trying to diff the old layout.
        """
        if self.white_captured_layout is None or self.black_captured_layout is None:
            return

        self.clear_layout(self.white_captured_layout)
        self.clear_layout(self.black_captured_layout)

        # Inner grid layouts so pieces wrap neatly in rows of 3
        wg = QGridLayout(); wg.setSpacing(3)
        bg = QGridLayout(); bg.setSpacing(3)

        for i, pt in enumerate(self.captured_white):
            wg.addWidget(self.create_piece_widget(pt, chess.WHITE), i // 3, i % 3)
        for i, pt in enumerate(self.captured_black):
            bg.addWidget(self.create_piece_widget(pt, chess.BLACK), i // 3, i % 3)

        wc = QWidget(); wc.setLayout(wg)
        bc = QWidget(); bc.setLayout(bg)
        self.white_captured_layout.addWidget(wc)
        self.black_captured_layout.addWidget(bc)

    def clear_layout(self, layout):
        """Remove all widgets from a layout without leaving dangling pointers."""
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def create_piece_widget(self, piece_type, color):
        """
        Create a small square widget showing a piece icon.
        Size is scaled to ~75 % of the current board-cell size,
        clamped between 36 and 80 px so it's always readable.
        """
        sz      = max(36, min(80, int(self._sq_size * 0.75)))
        widget  = QWidget()
        widget.setFixedSize(sz, sz)
        lo      = QVBoxLayout()
        lo.setContentsMargins(1, 1, 1, 1)
        lo.setAlignment(Qt.AlignmentFlag.AlignCenter)

        icon_path = self.pieces.get((piece_type, color))
        icon_sz   = int(sz * 0.85)

        if icon_path and os.path.exists(icon_path):
            label = QLabel()
            label.setPixmap(QIcon(icon_path).pixmap(QSize(icon_sz, icon_sz)))
            label.setFixedSize(icon_sz, icon_sz)
            label.setScaledContents(True)
            label.setStyleSheet("background: transparent;")
        else:
            # Fallback: Unicode chess symbol
            symbols = {
                chess.PAWN:   "♟" if color == chess.BLACK else "♙",
                chess.KNIGHT: "♞" if color == chess.BLACK else "♘",
                chess.BISHOP: "♝" if color == chess.BLACK else "♗",
                chess.ROOK:   "♜" if color == chess.BLACK else "♖",
                chess.QUEEN:  "♛" if color == chess.BLACK else "♕",
                chess.KING:   "♚" if color == chess.BLACK else "♔",
            }
            label = QLabel(symbols.get(piece_type, "?"))
            label.setFont(QFont("Arial", max(8, sz // 3)))
            label.setFixedSize(icon_sz, icon_sz)
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setStyleSheet("color: white; background: transparent;")

        lo.addWidget(label)
        widget.setLayout(lo)
        widget.setStyleSheet(
            "QWidget { background-color: #1e272e; border-radius: 6px; margin: 1px; }"
        )
        return widget

    def init_captured_display(self):
        """
        Build the scrollable captured-pieces panel that sits in the left sidebar.
        Returns a QWidget containing two sections: pieces taken by Black, and by White.
        """
        cw = QWidget()
        cw.setStyleSheet(
            "QWidget { background-color: #2f3542; border-radius: 10px; padding: 10px; }"
        )
        cw.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        # Scroll area in case there are many captured pieces
        sa = QScrollArea()
        sa.setWidgetResizable(True)
        sa.setStyleSheet(
            "QScrollArea { border: none; background: transparent; }"
            " QScrollBar:vertical { background: #1e272e; width: 8px; border-radius: 4px; }"
            " QScrollBar::handle:vertical { background: #576574; border-radius: 4px; }"
        )
        content = QWidget()
        cl      = QVBoxLayout()
        cl.setSpacing(10)

        wl = QLabel("🎲 Captured by BLACK:")
        wl.setStyleSheet(
            "color: #ff6b6b; font-weight: bold; font-size: 13px; padding: 4px;"
        )
        wl.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.white_captured_layout = QVBoxLayout()
        self.white_captured_layout.setSpacing(5)
        self.white_captured_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        wf = QWidget()
        wf.setLayout(self.white_captured_layout)
        wf.setStyleSheet("background: #1e272e; border-radius: 8px; padding: 6px;")

        sep = QLabel("──────────────")
        sep.setStyleSheet("color: #576574; font-size: 11px;")
        sep.setAlignment(Qt.AlignmentFlag.AlignCenter)

        bl = QLabel("🎲 Captured by WHITE:")
        bl.setStyleSheet(
            "color: #51cf66; font-weight: bold; font-size: 13px; padding: 4px;"
        )
        bl.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.black_captured_layout = QVBoxLayout()
        self.black_captured_layout.setSpacing(5)
        self.black_captured_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        bf = QWidget()
        bf.setLayout(self.black_captured_layout)
        bf.setStyleSheet("background: #1e272e; border-radius: 8px; padding: 6px;")

        cl.addWidget(wl)
        cl.addWidget(wf)
        cl.addWidget(sep)
        cl.addWidget(bl)
        cl.addWidget(bf)
        cl.addStretch()
        content.setLayout(cl)
        sa.setWidget(content)

        ml = QVBoxLayout()
        ml.addWidget(sa)
        cw.setLayout(ml)
        return cw

    # ──────────────────────────────────────────────────────────────────────────
    # Board rendering helpers
    # ──────────────────────────────────────────────────────────────────────────

    def _render_board_state(self, board):
        """
        Update the existing button grid to show the position from `board`
        without rebuilding the buttons.  Used for history browsing.
        """
        flip    = self.should_flip_board()
        sq_size = self._sq_size
        icon_sz = int(sq_size * 0.72)

        for sq, btn in self.buttons.items():
            file_ = chess.square_file(sq)
            rank_ = chess.square_rank(sq)
            grid_r = (7 - rank_) if not flip else rank_
            grid_c = file_       if not flip else (7 - file_)

            btn.setFixedSize(sq_size, sq_size)
            btn.setIconSize(QSize(icon_sz, icon_sz))
            btn.setStyleSheet(self._sq_color_style(grid_r, grid_c))

            piece = board.piece_at(sq)
            if piece:
                ip = self.pieces.get((piece.piece_type, piece.color))
                if ip and os.path.exists(ip):
                    btn.setIcon(QIcon(ip))
                    btn.setText("")
                    continue
            btn.setIcon(QIcon())
            btn.setText("")

    # ──────────────────────────────────────────────────────────────────────────
    # Main update – called after every move or state change
    # ──────────────────────────────────────────────────────────────────────────

    def update_ui(self):
        """
        Refresh the board display to reflect the current game state.

        This does several things in order:
          1. Check for game-over conditions (checkmate, stalemate, etc.)
             and show the game-over dialog if the game just ended.
          2. Play the check sound if the king was just put in check.
          3. Update the turn label text and style.
          4. Iterate over all 64 squares and set the correct background
             colour + piece icon for each button.

        We skip updates while an animation is running (animating == True)
        or when we're browsing history (view_index != -1) to avoid conflicts.
        """
        if self.animating or not self.running:
            return
        if self.view_index != -1:
            return   # History view is managed by _show_history_board()

        # ── Game-over checks ───────────────────────────────────────────────────
        if not self.game_over_shown:
            if self.board.is_checkmate():
                self.game_over_shown = True
                self.stop_timer()
                if self.sound_enabled and self.game_end_sound:
                    self.game_end_sound.play()
                loser_is_white = (self.board.turn == chess.WHITE)
                winner_str = "Black wins! ♚" if loser_is_white else "White wins! ♔"
                if self.mode == "online" and self.socket:
                    winner_pid = 2 if loser_is_white else 1
                    try:
                        self.socket.send(f"CHECKMATE|{winner_pid}\n".encode())
                    except Exception:
                        pass
                self._render_board_state(self.board)
                QApplication.processEvents()
                dlg = GameOverDialog(f"Checkmate! {winner_str}", self)
                dlg.exec()
                if dlg.clicked_action == "retry":
                    self.start_game(self.mode, self.player_color)
                else:
                    self.init_menu()
                return

            elif self.board.is_stalemate():
                self.game_over_shown = True
                self.stop_timer()
                if self.sound_enabled and self.game_end_sound:
                    self.game_end_sound.play()
                self._render_board_state(self.board)
                QApplication.processEvents()
                dlg = GameOverDialog("Stalemate! It's a draw. 🤝", self)
                dlg.exec()
                if dlg.clicked_action == "retry":
                    self.start_game(self.mode, self.player_color)
                else:
                    self.init_menu()
                return

            elif self.board.is_game_over():
                self.game_over_shown = True
                self.stop_timer()
                if self.sound_enabled and self.game_end_sound:
                    self.game_end_sound.play()
                result = self.board.result()
                msg    = {"1-0": "White wins! ♔", "0-1": "Black wins! ♚"}.get(
                    result, "It's a draw!"
                )
                self._render_board_state(self.board)
                QApplication.processEvents()
                dlg = GameOverDialog(f"Result: {result}\n{msg}", self)
                dlg.exec()
                if dlg.clicked_action == "retry":
                    self.start_game(self.mode, self.player_color)
                else:
                    self.init_menu()
                return

        # ── Check detection and sound ──────────────────────────────────────────
        is_in_check = self.board.is_check()
        # Only play the check sound on the transition into check, not every tick
        if is_in_check and not self.previous_check_state:
            self.play_check_sound()
        self.previous_check_state = is_in_check

        # ── Turn label text ────────────────────────────────────────────────────
        if self.turn_label:
            if is_in_check:
                ct = "White" if self.board.turn == chess.WHITE else "Black"
                self.turn_label.setText(f"⚠️ CHECK! {ct} King in danger! ⚠️")
                self.turn_label.setStyleSheet(
                    "color: #ff6b6b; font-size: 16px; font-weight: bold;"
                    " padding: 10px; background-color: #2f3542; border-radius: 5px;"
                )
            else:
                self.turn_label.setStyleSheet(
                    "color: white; font-size: 16px; font-weight: bold; padding: 10px;"
                )
                if self.mode == "offline":
                    self.turn_label.setText(
                        "White's Turn" if self.board.turn == chess.WHITE else "Black's Turn"
                    )
                elif self.mode in ["easy", "hard"]:
                    if self.player_color == "White":
                        self.turn_label.setText(
                            "Your Turn (White)" if self.my_turn else "AI's Turn (Black)"
                        )
                    else:
                        self.turn_label.setText(
                            "Your Turn (Black)" if self.my_turn else "AI's Turn (White)"
                        )
                elif self.mode == "online":
                    if not self.game_started or self.player_color is None:
                        self.turn_label.setText("Waiting for opponent...")
                    elif self.premove:
                        self.turn_label.setText("⚡ Pre-move set — waiting for opponent")
                    else:
                        self.turn_label.setText(
                            "Your Turn" if self.my_turn else "Opponent's Turn"
                        )

        # ── Render squares ─────────────────────────────────────────────────────
        flip    = self.should_flip_board()
        sq_size = self._sq_size
        icon_sz = int(sq_size * 0.72)

        for sq, btn in self.buttons.items():
            file_ = chess.square_file(sq)
            rank_ = chess.square_rank(sq)
            grid_r = (7 - rank_) if not flip else rank_
            grid_c = file_       if not flip else (7 - file_)

            btn.setFixedSize(sq_size, sq_size)
            btn.setIconSize(QSize(icon_sz, icon_sz))

            # Determine background colour, in priority order:
            style = self._sq_color_style(grid_r, grid_c)   # 1. default checker

            if self.premove and sq in (self.premove[0], self.premove[1]):
                style = "background: rgba(128,0,200,180); border: 2px solid #cc00ff;"
            elif is_in_check and sq == self.board.king(self.board.turn):
                style = "background:#e74c3c; border: none;"             # red = king in check
            elif sq == self.selected_sq:
                style = "background:#f1c40f; border: none;"             # yellow = selected
            elif self.selected_sq is not None:
                if chess.Move(self.selected_sq, sq) in self.board.legal_moves:
                    style = "background:#2ecc71; border: none;"         # green = legal target

            btn.setStyleSheet(style)

            # Hide the piece icon during its animation
            if self.animating and sq == self.anim_from:
                btn.setIcon(QIcon())
                btn.setText("")
                continue

            # Draw piece icon
            piece = self.board.piece_at(sq)
            if piece:
                ip = self.pieces.get((piece.piece_type, piece.color))
                if ip and os.path.exists(ip):
                    btn.setIcon(QIcon(ip))
                    btn.setText("")
                    continue
            btn.setIcon(QIcon())
            btn.setText("")


# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    no_window()          # Hide console window on Windows
    _set_dpi_awareness() # Set DPI awareness BEFORE creating QApplication

    # Qt 6: let fractional scale factors pass through unchanged (e.g. 1.25×)
    # so the app looks crisp on 125 % / 150 % Windows displays
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(sys.argv)
    window = ChessUI()
    window.show()
    sys.exit(app.exec())