# ♟ Chess Pro

> A full-featured desktop chess client with online multiplayer, AI opponents, spectator mode, and a built-in game server — all in pure Python.

---

## Table of Contents

- [Overview](#overview)
- [Screenshots](#screenshots)
- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Running the Game](#running-the-game)
- [Running the Server](#running-the-server)
- [Game Modes](#game-modes)
- [Online Play Guide](#online-play-guide)
- [Time Controls](#time-controls)
- [AI Difficulty](#ai-difficulty)
- [Stockfish Setup](#stockfish-setup)
- [Sound & Music](#sound--music)
- [File Structure](#file-structure)
- [Server Protocol](#server-protocol)
- [Configuration](#configuration)
- [Troubleshooting](#troubleshooting)
- [License](#license)

---

## Overview

Chess Pro is a desktop chess application built with **PyQt6** and **python-chess**. It supports everything you'd expect from a polished chess client — smooth piece animations, a real chess clock, captured-piece tracking, move history navigation, and full online multiplayer with a lobby, chat, draw offers, and ELO ratings.

You can play it completely offline (two players on the same machine, or against an AI), or connect to a hosted server for live games against other people anywhere on the internet.

---

## Features

### Gameplay
- ✅ Offline two-player mode (pass-and-play on one screen)
- ✅ AI opponent — Easy (random moves) and Hard (Stockfish engine or built-in minimax)
- ✅ Smooth animated piece movement
- ✅ Pawn promotion dialog (choose Queen / Rook / Knight / Bishop)
- ✅ En-passant and castling fully supported via python-chess
- ✅ Check highlighting (red king square) and check sound
- ✅ Stalemate, checkmate, and draw detection

### Chess Clock
- ✅ Five selectable time controls: Unlimited, 1+0, 3+2, 5+0, 10+0
- ✅ Per-move increment support
- ✅ Colour-coded countdown (green → yellow → red as time runs low)
- ✅ Server-authoritative clock for online games (no client-side cheating)

### Online Multiplayer
- ✅ Account registration and login (password hashing, session tokens)
- ✅ Auto-login on restart (saved session token)
- ✅ Live lobby showing all open and active sessions
- ✅ Create a session and choose your color; opponent gets the opposite
- ✅ In-game chat with quick-emoji buttons
- ✅ Draw offers, resign, and rematch requests
- ✅ ELO rating system (K=32, updated after every rated game)
- ✅ Credit rewards for winning
- ✅ Ping / latency display
- ✅ Spectator count badge

### Spectator Mode
- ✅ Watch any live game in real time
- ✅ Board reconstructed from full move history on join
- ✅ Spectator clocks interpolated locally between server syncs
- ✅ Read the players' in-game chat

### UI & UX
- ✅ Responsive board that scales to any window size
- ✅ Board flip button and auto-flip for Black
- ✅ Move history navigation (⏮ ⏭ buttons to step through past positions)
- ✅ Pre-move support in online mode (queue a move while waiting for the opponent)
- ✅ Full-screen mode (launches full-screen, F11 to toggle)
- ✅ Captured-piece display with piece icons
- ✅ Background music + sound effects (all optional, fully mutable)
- ✅ Capture visual effect (configurable image flash)
- ✅ Per-monitor HiDPI / 4K support on Windows

---

## Requirements

| Dependency | Version | Purpose |
|---|---|---|
| Python | 3.9 + | Runtime |
| PyQt6 | 6.4 + | GUI framework |
| python-chess | 1.9 + | Chess logic, move generation, FEN |
| Stockfish *(optional)* | any recent | Hard AI engine |

> The server (`chess_server.py`) has **no third-party dependencies** — it uses only the Python standard library.

---

## Installation

### 1. Clone or download the project

```bash
git clone https://github.com/yourname/chess-pro.git
cd chess-pro
```

### 2. Create a virtual environment (recommended)

```bash
python -m venv venv

# Windows
venv\Scripts\activate

# macOS / Linux
source venv/bin/activate
```

### 3. Install Python dependencies

```bash
pip install PyQt6 python-chess
```

### 4. Add asset files

The client expects these files alongside `chess_pro.py`:

```
chess_pro.py
chess_server.py

# Piece images (PNG, any size — recommend 128×128 or larger)
w_pawn.png    b_pawn.png
w_rook.png    b_rook.png
w_knight.png  b_knight.png
w_bishop.png  b_bishop.png
w_queen.png   b_queen.png
w_king.png    b_king.png

# App icon (optional)
chess.png

# Sound effects (optional, .wav)
click.wav     move.wav     check.wav
start.wav     end.wav

# Background music (optional, any of these)
background.mp3   background.ogg   background.wav

# Capture sounds folder (optional — put .wav/.mp3/.ogg files inside)
capture_sounds/

# Capture visual effects folder (optional — put .png images inside)
capture_effects/
```

Piece images can be sourced from any free chess piece set (e.g. [Wikimedia Commons Chess Pieces](https://commons.wikimedia.org/wiki/Category:SVG_chess_pieces)).

---

## Running the Game

```bash
python chess_pro.py
```

The window opens in full-screen mode. Press **F11** at any time to switch between full-screen and windowed.

---

## Running the Server

The server is a single-file TCP server with no dependencies beyond the standard library.

```bash
python chess_server.py
```

By default it listens on **0.0.0.0:12345**. To change the host or port, edit the bottom of `chess_server.py`:

```python
if __name__ == "__main__":
    ChessServer(host="0.0.0.0", port=12345).run()
```

### Hosting for LAN play

Anyone on the same local network can connect using your **local IP address** (e.g. `192.168.1.42:12345`). Find your local IP with:

```bash
# Windows
ipconfig

# macOS / Linux
hostname -I
```

### Hosting for internet play

Forward port **12345** on your router to your server machine, then share your **public IP** with your friends. For a more stable setup, consider running the server on a VPS (DigitalOcean, Linode, etc.).

The server stores accounts in `accounts.json` in the same directory. **Back this file up** if you want to preserve player ratings across server restarts.

---

## Game Modes

### ⚔️ Offline 2 Player
Two people take turns on the same keyboard and screen. The board auto-flips after each move so both players always see it from their own perspective. No account required.

### 🤖 AI Easy
You play against an AI that picks a random legal move every turn. Good for beginners or a relaxed casual game.

### 🤖 AI Hard
You play against Stockfish (if installed) or a built-in minimax engine with alpha-beta pruning (depth 3). Stockfish is significantly stronger — see [Stockfish Setup](#stockfish-setup).

### 🌐 Online
Play against another human over the network. Requires a running server and a registered account. See [Online Play Guide](#online-play-guide).

### 🎥 Spectate
Watch a live online game without playing. You'll see the board update in real time and can read the players' chat.

---

## Online Play Guide

### First time

1. Launch `chess_pro.py` and click **🔑 SIGN IN**.
2. Enter the server address in `IP:PORT` format (e.g. `192.168.1.42:12345`).
3. Switch to the **✨ Register** tab, pick a username and password, then click **CREATE ACCOUNT**.
4. You'll be taken straight to the lobby.

### Subsequent launches

If you signed in before and didn't click Sign Out, the app will reconnect automatically using your saved session token — no password needed.

### Creating a game

1. Click **🌐 ONLINE** from the main menu.
2. In the lobby, click **➕ CREATE SESSION**.
3. Choose your color and time control, then click **CREATE SESSION**.
4. Share the session number shown in the lobby with your friend.
5. Your friend clicks **JOIN SELECTED** on that session.
6. The game starts automatically.

### Joining a game

1. Click **🌐 ONLINE** from the main menu.
2. Find the session in the lobby list (green = waiting for opponent).
3. Click it to highlight it, then click **⚔ JOIN SELECTED**.

### In-game actions

| Button | Action |
|---|---|
| 🤝 Draw | Offer a draw to your opponent |
| 🏳️ Resign | Forfeit the current game |
| 🔄 Rematch | Request another game (colors swap automatically) |

---

## Time Controls

| Label | Format | Description |
|---|---|---|
| Unlimited | ∞ | No clock — play at your own pace |
| Bullet | 1+0 | 1 minute per player, no increment |
| Blitz | 3+2 | 3 minutes + 2 second increment per move |
| Blitz | 5+0 | 5 minutes per player, no increment |
| Rapid | 10+0 | 10 minutes per player, no increment |

The chess clock is **server-authoritative** in online games. Each client receives a `TIMER_SYNC` message every second to keep the displays accurate.

---

## AI Difficulty

### Easy
Picks a uniformly random move from all legal moves. Plays as fast as the animation allows.

### Hard — Stockfish
When Stockfish is installed and found, it runs in a background thread at skill level 20 (maximum) with a 0.2-second time limit per move. This is very strong — consider using Easy mode if you're learning.

### Hard — Built-in minimax (fallback)
If Stockfish is not found, the client falls back to a minimax engine with:
- Alpha-beta pruning
- Depth 3 search
- Move ordering (captures and checks searched first)
- Evaluation: material balance + central control + mobility

The fallback is weaker than Stockfish but still plays reasonable chess.

---

## Stockfish Setup

### Windows

1. Download the latest Stockfish release from [stockfishchess.org/download](https://stockfishchess.org/download/).
2. Extract and place `stockfish.exe` in one of these locations:
   - `./stockfish/stockfish.exe` (next to `chess_pro.py`) — **recommended**
   - `./stockfish.exe`
   - Anywhere on your system `PATH`

### macOS

```bash
# Homebrew (easiest)
brew install stockfish
```

Or download a binary from the Stockfish website and place it at `/usr/local/bin/stockfish`.

### Linux

```bash
# Debian / Ubuntu
sudo apt install stockfish

# Arch
sudo pacman -S stockfish

# Fedora
sudo dnf install stockfish
```

### Verifying detection

Launch the game and look at the **AI HARD** button on the main menu. If it shows **🤖 AI HARD ⚡**, Stockfish was found. If the ⚡ is missing and a warning note appears, Stockfish was not detected and the fallback will be used.

---

## Sound & Music

All audio is optional. The game runs fine without any sound files.

| File / Folder | Type | Triggered by |
|---|---|---|
| `click.wav` | SFX | Clicking a square |
| `move.wav` | SFX | Moving a piece (non-capture) |
| `capture_sounds/` | SFX (random) | Capturing a piece |
| `check.wav` | SFX | King put in check |
| `start.wav` | SFX | Game start |
| `end.wav` | SFX | Game over |
| `background.mp3/.ogg/.wav` | Music | Looping background |
| `capture_effects/` | Images (random) | Visual flash on capture |

Click **🔊 MUTE** during a game to silence everything. Click **🔇 UNMUTE** to restore sounds.

---

## File Structure

```
chess-pro/
│
├── chess_pro.py          # Main game client (PyQt6)
├── chess_server.py       # TCP game server (stdlib only)
│
├── accounts.json         # Player accounts — auto-created by the server
├── session.json          # Local login session — auto-created by the client
│
├── chess.png             # App window icon
│
├── w_pawn.png            # White piece images
├── w_rook.png
├── w_knight.png
├── w_bishop.png
├── w_queen.png
├── w_king.png
│
├── b_pawn.png            # Black piece images
├── b_rook.png
├── b_knight.png
├── b_bishop.png
├── b_queen.png
├── b_king.png
│
├── click.wav             # Sound effects (all optional)
├── move.wav
├── check.wav
├── start.wav
├── end.wav
├── background.mp3
│
├── capture_sounds/       # Put .wav/.mp3/.ogg files here
│   └── *.wav
│
├── capture_effects/      # Put .png images here for capture flash
│   └── *.png
│
└── stockfish/            # Optional — place the Stockfish binary here
    └── stockfish.exe     # (or stockfish on Linux/macOS)
```

---

## Server Protocol

The client and server communicate over a plain-text, newline-delimited TCP protocol. Each message is a single line in the form `COMMAND|arg1|arg2|...`.

### Authentication

| Direction | Message | Meaning |
|---|---|---|
| Client → Server | `LOGIN\|user\|pass` | Login with credentials |
| Client → Server | `REGISTER\|user\|pass` | Create account + login |
| Client → Server | `AUTO_LOGIN\|token` | Re-authenticate with saved token |
| Server → Client | `AUTH_OK\|user\|{stats}\|token` | Auth succeeded |
| Server → Client | `AUTH_FAIL\|reason` | Auth failed |

### Lobby

| Direction | Message | Meaning |
|---|---|---|
| Client → Server | `SESSION_LIST_REQUEST` | Ask for current sessions |
| Server → Client | `SESSION_LIST\|[json]` | Full session list |
| Client → Server | `CREATE_SESSION\|tc\|color` | Create a new game room |
| Server → Client | `SESSION_CREATED\|id` | Confirmation + assigned session ID |
| Client → Server | `JOIN_SESSION\|id` | Join an existing room |
| Server → Client | `JOIN_FAIL\|reason` | Join was rejected |
| Client → Server | `SPECTATE_SESSION\|id` | Start watching a game |
| Server → Client | `SPECTATE_OK\|{info}` | Full game state snapshot |
| Server → Client | `SPECTATE_FAIL\|reason` | Spectate was rejected |

### In-game

| Direction | Message | Meaning |
|---|---|---|
| Server → Client | `PLAYER\|pid\|color` | Your color assignment |
| Server → Client | `OPPONENT_INFO\|user\|{stats}` | Opponent joined |
| Server → Client | `GAME_START\|tc\|base\|inc` | Game begins |
| Client → Server | `MOVE\|uci` | Your move (e.g. `e2e4`) |
| Server → Client | `MOVE\|uci` | Opponent's move |
| Server → Client | `TIMER_SYNC\|w\|b` | Clock update (seconds, 1 decimal) |
| Server → Client | `TIMEOUT\|slot` | A player's clock hit zero |
| Client → Server | `CHECKMATE\|pid` | Notify server of checkmate |
| Client → Server | `RESIGN` | Forfeit the game |
| Server → Client | `OPPONENT_RESIGNED` | Opponent forfeited |
| Client → Server | `DRAW_OFFER` | Offer a draw |
| Client → Server | `DRAW_ACCEPT` | Accept opponent's draw offer |
| Client → Server | `DRAW_DECLINE` | Decline opponent's draw offer |
| Client → Server | `REMATCH_REQUEST` | Ask for a rematch |
| Client → Server | `REMATCH_ACCEPT` | Agree to rematch |
| Client → Server | `REMATCH_DECLINE` | Decline rematch |
| Server → Client | `STATS_UPDATE\|{stats}` | Your new ELO / stats |
| Server → Client | `OPPONENT_LEFT` | Opponent disconnected |
| Client ↔ Server | `PING` / `PONG` | Keep-alive / latency check |

---

## Configuration

Most settings are hardcoded constants at the top of each file. Here are the ones you're most likely to want to change:

### Server (`server.py`)

```python
# Bottom of the file
Server(host="0.0.0.0", port=12345).run()
```

Change `host` to `"127.0.0.1"` to accept local connections only.

### Client (`client.py`)

```python
# Stockfish think time (seconds per move) — lower = faster but weaker
self.stockfish_think_time = 0.2

# Stockfish skill level (0–20)
self.stockfish_skill = 20

# Background music volume (0.0–1.0)
self.audio_output.setVolume(0.2)
```

---

## Troubleshooting

### "No module named PyQt6"
```bash
pip install PyQt6
```

### "No module named chess"
```bash
pip install python-chess
```

### Piece images don't appear
Make sure all 12 piece PNG files (`w_pawn.png`, `b_pawn.png`, etc.) are in the **same directory** as `client.py`. The app will still run without them but the board will be blank.

### Can't connect to the server
- Make sure `server.py` is running before launching the client.
- Check that the IP address and port are correct (`IP:PORT` format, e.g. `192.168.1.42:12345`).
- For internet play, verify that port 12345 is open in your firewall and router.
- Try `127.0.0.1:12345` if the server is running on the same machine.

### Server port already in use
Another process is using port 12345. Either stop that process or change the port in both `chess_server.py` and use the new port when connecting.

### Stockfish not detected
- Confirm the binary is named exactly `stockfish` (Linux/macOS) or `stockfish.exe` (Windows).
- Try placing it in a `stockfish/` subfolder next to `chess_pro.py`.
- Run `which stockfish` (macOS/Linux) or `where stockfish` (Windows) to check if it's on your PATH.

### Board looks blurry on a 4K / HiDPI display (Windows)
This is usually caused by DPI awareness not being set before Qt initialises. The app attempts to set per-monitor DPI awareness automatically. If it still looks blurry, try running:
```bash
python chess_pro.py
```
from a terminal that was launched with "Run as administrator", or check your Windows display scaling settings.

### Background music doesn't play
Qt's multimedia stack requires platform-specific codec support. On Linux, install:
```bash
sudo apt install gstreamer1.0-plugins-good gstreamer1.0-plugins-bad
```
On Windows, `.mp3` should work out of the box. Try `.ogg` or `.wav` as alternatives.
Stockfish Setup
Chess Pro uses Stockfish for the "Hard" AI difficulty. Stockfish is a powerful open-source chess engine that makes the AI opponent much stronger.

⚠️ Important: Stockfish is not included in this repository due to GitHub's 25 MB file size limit. You must download it separately.

Step 1: Download Stockfish
Visit the official Stockfish download page:
🔗 https://stockfishchess.org/download/

Choose the version for your operating system:

OS	File to download
Windows	stockfish-windows-x86-64.zip (or .exe)
macOS	stockfish-macos-x86-64.zip (Intel) or stockfish-macos-arm64.zip (Apple Silicon M1/M2/M3)
Linux	stockfish-ubuntu-x86-64.zip (or your distribution's package)
Step 2: Install Stockfish
🪟 Windows
Extract the ZIP file

Copy stockfish.exe to one of these locations (choose one):

chess-pro/stockfish.exe (same folder as chess_pro.py) — recommended

chess-pro/stockfish/stockfish.exe (inside a stockfish subfolder)

Any folder in your system PATH (e.g. C:\Windows\System32)

🍎 macOS
Option A: Homebrew (easiest)

bash
brew install stockfish
Option B: Manual install

Extract the downloaded ZIP

Copy the stockfish binary to:

chess-pro/stockfish (same folder as chess_pro.py) — recommended

/usr/local/bin/stockfish

Make it executable (if manual install):

bash
chmod +x chess-pro/stockfish
🐧 Linux
Option A: Package manager (recommended)

bash
# Debian / Ubuntu
sudo apt install stockfish

# Arch Linux
sudo pacman -S stockfish

# Fedora
sudo dnf install stockfish
Option B: Manual install

Extract the downloaded ZIP

Copy the stockfish binary to chess-pro/stockfish

Make it executable:

bash
chmod +x chess-pro/stockfish
Step 3: Verify Installation
Launch Chess Pro and look at the main menu:

✅ Stockfish found → AI HARD button shows 🤖 AI HARD ⚡ (note the lightning bolt)

❌ Stockfish not found → AI HARD button shows 🤖 AI HARD (no lightning bolt) and a warning appears

Alternative: Use the built-in AI
If you skip Stockfish installation, the "Hard" mode will automatically fall back to a built-in minimax engine with:

Alpha-beta pruning

Depth 3 search

Move ordering (captures first)

Position evaluation (material + center control + mobility)

The built-in AI is weaker than Stockfish but still provides a decent challenge for casual play.

Troubleshooting Stockfish Detection
Problem	Solution
Stockfish not detected	Check the filename exactly matches stockfish.exe (Windows) or stockfish (Mac/Linux)
Permission denied (Mac/Linux)	Run chmod +x stockfish to make it executable
Wrong architecture	Download the correct version for your CPU (Intel vs Apple Silicon)
Still not working	Place stockfish.exe in the exact same folder as chess_pro.py
---

## License

This project is released under the **MIT License**. See `LICENSE` for details.

---

*Built with Python, PyQt6, python-chess, and ♟ a lot of caffeine.*
