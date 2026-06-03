"""
chess_server.py  –  Chess Pro Game Server

A lightweight TCP game server that handles:
  • Account registration and login (password hashing, session tokens)
  • Persistent lobby with session creation, joining, and spectating
  • Real-time move relay between players
  • Server-authoritative chess clocks with per-move increment
  • ELO rating updates after every rated game
  • Draw offers, resignations, rematches, and disconnection handling
  • Spectator broadcast (moves, chat, game events, timers)

Protocol:
  All messages are UTF-8 text lines ending with "\n".
  Format:  COMMAND|arg1|arg2|...
  The server speaks the same protocol the PyQt6 client expects.

Threading model:
  • One daemon thread per connected client (handle_client).
  • One daemon thread per active timed session (_timer_loop).
  • A single threading.Lock per Session guards its mutable state.
  • A server-level Lock guards shared dicts (sessions, accounts, client maps).

Author notes use plain English to explain decisions, not just describe code.
"""

import socket       # TCP server socket
import threading    # one thread per client + one per active timer
import json         # session state serialisation, stats payloads
import os           # file existence checks
import hashlib      # SHA-256 password hashing
import time         # wall-clock timing for the chess clocks
import secrets      # cryptographically-safe session token generation
import sys          # stderr logging


# ──────────────────────────────────────────────────────────────────────────────
# Persistent account storage
# ──────────────────────────────────────────────────────────────────────────────

ACCOUNTS_FILE = "accounts.json"


def load_accounts():
    """
    Read the accounts database from disk.
    Returns a dict keyed by username, or an empty dict if the file is missing
    or corrupt.  We swallow exceptions here rather than crashing the server
    on startup just because the accounts file is temporarily unreadable.
    """
    if os.path.exists(ACCOUNTS_FILE):
        try:
            with open(ACCOUNTS_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_accounts(accounts):
    """
    Write the full accounts dict back to disk.
    Called after every registration, login (new token), and ELO update.

    This is a full-overwrite write, which is simple but not great for very
    large player bases.  A real production server would use a proper database.
    For this project it's more than fast enough.
    """
    with open(ACCOUNTS_FILE, "w") as f:
        json.dump(accounts, f, indent=2)


def hash_password(pw):
    """
    Return the SHA-256 hex digest of a password string.

    We deliberately don't salt passwords here to keep the code simple.
    A production system should use bcrypt or argon2 with per-user salts.
    """
    return hashlib.sha256(pw.encode()).hexdigest()


# ──────────────────────────────────────────────────────────────────────────────
# Session – a single game room
# ──────────────────────────────────────────────────────────────────────────────

class Session:
    """
    Represents one game room in the lobby.

    A session goes through these states:
      "waiting"  – created by one player, second slot empty
      "playing"  – both players connected, game in progress
      "finished" – game over (checkmate / resign / timeout / draw / disconnect)

    Slot model (always fixed, regardless of who joined first):
      slot 0  →  White player
      slot 1  →  Black player

    Player IDs (pid) sent to clients:
      pid 1  →  slot 0  →  White
      pid 2  →  slot 1  →  Black

    The creator picks their color up front.  When a joiner arrives they
    automatically get the opposite slot.  This means the creator might be
    in slot 1 (Black) and the joiner in slot 0 (White) – that's intentional
    and fully supported.
    """

    def __init__(self, session_id, creator_name, time_control_str, creator_color="White"):
        self.session_id       = session_id
        self.time_control_str = time_control_str
        self.name             = f"Game {session_id}"

        # Validate creator color – default to White if garbage comes in
        self.creator_color = creator_color if creator_color in ("White", "Black") else "White"
        self.joiner_color  = "Black" if self.creator_color == "White" else "White"

        # Two-slot player array indexed by color: [White_conn, Black_conn]
        self.players      = [None, None]   # socket connections
        self.player_names = [None, None]   # usernames
        self.player_elo   = [1200, 1200]   # ELO at the time the game started

        # Spectator list – grows and shrinks as watchers join / leave
        self.spectators = []

        # Reverse map: connection → slot index, for O(1) lookup during move handling
        self._conn_slot = {}

        # Chess clock state
        self.base_seconds = 0      # starting seconds per player
        self.bonus        = 0      # increment (seconds) added after each move
        self.timers       = [0.0, 0.0]   # remaining seconds, indexed by slot
        self._parse_tc(time_control_str)

        # Game flow state
        self.active_player  = 0    # 0 = White's turn, 1 = Black's turn
        self.last_tick      = None # wall-clock time of the last timer update
        self.moves          = []   # list of UCI strings in play order
        self.finished       = False
        self.started        = False
        self.timer_running  = False
        self.timer_thread   = None
        self.status         = "waiting"

        # Each Session has its own lock so multiple client threads can
        # safely update timers, move lists, and player slots concurrently.
        self.lock = threading.Lock()

    # ── Time-control parsing ──────────────────────────────────────────────────

    def _parse_tc(self, tc):
        """
        Parse the time-control string and populate base_seconds / bonus / timers.

        Supported formats (same as the client uses):
          "0"    → unlimited, no clock
          "5+3"  → 5 minutes base + 3 second increment
          "10+0" → 10 minutes, no increment
          bare integer ≤ 180 → minutes, bare integer > 180 → seconds already

        If parsing fails we leave base_seconds == 0 which means unlimited.
        """
        if not tc or tc == "0":
            return
        if '+' in str(tc):
            parts = str(tc).split('+')
            try:
                self.base_seconds = int(parts[0]) * 60
                self.bonus        = int(parts[1])
                self.timers       = [float(self.base_seconds), float(self.base_seconds)]
            except Exception:
                pass
        else:
            try:
                val = int(tc)
                self.base_seconds = val * 60 if 0 < val <= 180 else val
                self.timers       = [float(self.base_seconds), float(self.base_seconds)]
            except Exception:
                pass

    def is_unlimited(self):
        """Return True when there is no clock (base_seconds == 0)."""
        return self.base_seconds == 0

    # ── Slot / color helpers ──────────────────────────────────────────────────

    def creator_slot(self):
        """
        Return the slot index (0 or 1) that the creator occupies.
        White creators sit in slot 0; Black creators sit in slot 1.
        """
        return 0 if self.creator_color == "White" else 1

    def joiner_slot(self):
        """Return the slot index for the player who joins after the creator."""
        return 1 - self.creator_slot()

    def slot_color(self, slot_idx):
        """Convert a slot index to the color string the client expects."""
        return "White" if slot_idx == 0 else "Black"

    def slot_pid(self, slot_idx):
        """
        Convert a slot index to a 1-based player ID (pid).
        pid 1 always means White (slot 0), pid 2 always means Black (slot 1).
        The client uses the pid to track whose turn it is.
        """
        return slot_idx + 1

    # ── Chess clock ───────────────────────────────────────────────────────────

    def start_timer(self):
        """
        Spawn the background timer thread.
        For unlimited games this is a no-op – we never tick the clock at all.
        """
        if self.is_unlimited():
            return
        self.timer_running = True
        self.last_tick     = time.time()
        self.timer_thread  = threading.Thread(
            target=self._timer_loop, daemon=True
        )
        self.timer_thread.start()

    def stop_timer(self):
        """Signal the timer thread to exit on its next iteration."""
        self.timer_running = False

    def _timer_loop(self):
        """
        Background timer thread – ticks every 100 ms.

        Responsibilities:
          • Decrement the active player's clock by elapsed wall time.
          • Broadcast TIMER_SYNC to all clients every ~1 second so their
            local display stays accurate without floating-point drift.
          • Detect a flag fall (clock hits zero) and call _handle_timeout().

        We use a real elapsed-time calculation (now - last_tick) rather than
        blindly subtracting 0.1 each iteration so the clock stays accurate
        even if the OS scheduler is slow to wake us up.
        """
        last_sync = time.time()
        while self.timer_running and not self.finished:
            time.sleep(0.1)
            with self.lock:
                if self.finished or not self.timer_running:
                    break

                now     = time.time()
                elapsed = now - self.last_tick
                self.last_tick = now

                # Tick the active player's clock down
                if self.timers[self.active_player] > 0:
                    self.timers[self.active_player] = max(
                        0.0, self.timers[self.active_player] - elapsed
                    )

                # Broadcast a sync update roughly once per second
                if now - last_sync >= 1.0:
                    self._broadcast_raw(
                        f"TIMER_SYNC|{self.timers[0]:.1f}|{self.timers[1]:.1f}"
                    )
                    last_sync = now

                # Flag fall – the active player ran out of time
                if self.timers[self.active_player] <= 0:
                    self.timers[self.active_player] = 0.0
                    self.finished       = True
                    self.timer_running  = False
                    self._handle_timeout()
                    break

    def _broadcast_raw(self, msg):
        """
        Send a message to every connected client in this session
        (both players + all spectators).

        We use a try/except per-client so one broken socket can't
        prevent the others from receiving the message.
        """
        if not msg.endswith("\n"):
            msg += "\n"
        for c in list(self.players) + list(self.spectators):
            if c:
                try:
                    c.send(msg.encode('utf-8'))
                except Exception:
                    pass

    def _handle_timeout(self):
        """
        A player's clock hit zero.  Notify each player individually
        (TIMEOUT|slot_index) so the client knows who lost, and send a
        human-readable event to spectators.
        """
        loser_name = self.player_names[self.active_player] or "?"

        # Tell each player with their slot index so the client can
        # determine whether they won or lost
        for idx, c in enumerate(self.players):
            if c:
                try:
                    c.send(f"TIMEOUT|{idx}\n".encode('utf-8'))
                except Exception:
                    pass

        # Spectators get a plain-text event description
        for c in list(self.spectators):
            if c:
                try:
                    c.send(
                        f"SPECTATE_EVENT|{loser_name} ran out of time\n".encode('utf-8')
                    )
                except Exception:
                    pass

    def switch_turn(self):
        """
        Called after every move.  Adds the increment to the player who just
        moved, then flips active_player and resets last_tick so the clock
        starts counting against the next player immediately.
        """
        with self.lock:
            if not self.is_unlimited():
                self.timers[self.active_player] += self.bonus
            self.active_player = 1 - self.active_player
            self.last_tick     = time.time()

    # ── Serialisation ─────────────────────────────────────────────────────────

    def get_info(self):
        """
        Return a dict representing the session that is safe to JSON-serialise
        and send as part of the lobby list or a SPECTATE_OK payload.

        We include creator_color so the lobby can show "Join as ♚ Black"
        next to a waiting session without the client having to infer it.
        """
        return {
            "session_id":      self.session_id,
            "name":            self.name,
            "white":           self.player_names[0] or "",
            "black":           self.player_names[1] or "",
            "white_elo":       self.player_elo[0],
            "black_elo":       self.player_elo[1],
            "time_control":    self.time_control_str,
            "base_seconds":    self.base_seconds,
            "bonus":           self.bonus,
            "moves":           list(self.moves),      # replay list for spectators
            "timers":          list(self.timers),     # current clock values
            "spectator_count": len(self.spectators),
            "status":          self.status,
            "started":         self.started,
            "creator_color":   self.creator_color,
        }


# ──────────────────────────────────────────────────────────────────────────────
# ChessServer – the main TCP server
# ──────────────────────────────────────────────────────────────────────────────

class ChessServer:
    """
    Listens for incoming TCP connections and spawns a handler thread for each one.

    Shared state (guarded by self.lock):
      self.sessions         – dict[session_id str → Session]
      self.session_counter  – auto-incrementing session ID
      self.accounts         – dict[username → account_dict]  (also on disk)
      self.session_tokens   – dict[token str → username]
      self.client_usernames – dict[conn → username]
      self.client_session   – dict[conn → Session]
      self.client_role      – dict[conn → 'player'|'spectator']
      self.client_slot      – dict[conn → 0|1]  (players only)
    """

    def __init__(self, host="0.0.0.0", port=12345):
        # Create and bind the listening socket
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # SO_REUSEADDR prevents "address already in use" errors when restarting
        # the server within a couple of seconds of the previous run
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind((host, port))
        self.server.listen(50)   # queue up to 50 pending connections

        # Single lock for all server-level shared state
        self.lock = threading.Lock()

        # Active game rooms
        self.sessions        = {}
        self.session_counter = 0

        # Per-connection lookup tables
        self.client_usernames = {}   # conn → username
        self.client_session   = {}   # conn → Session (or None if in lobby)
        self.client_role      = {}   # conn → 'player' or 'spectator'
        self.client_slot      = {}   # conn → 0 (White) or 1 (Black), players only

        # Token-based re-authentication (survives server restart if tokens persist)
        self.session_tokens = {}

        # Load accounts from disk; start with an empty dict if the file is missing
        self.accounts = load_accounts()

        sys.stderr.write(f"[SERVER] Running on {host}:{port}\n")

    # ──────────────────────────────────────────────────────────────────────────
    # Low-level send helper
    # ──────────────────────────────────────────────────────────────────────────

    def _send(self, conn, msg):
        """
        Send a single newline-terminated message to a client socket.
        Silently swallows exceptions so a dead socket never crashes the server –
        the disconnect will be detected the next time we try to recv from it.
        """
        try:
            if not msg.endswith("\n"):
                msg += "\n"
            conn.send(msg.encode('utf-8'))
        except Exception:
            pass

    # ──────────────────────────────────────────────────────────────────────────
    # Session token helpers
    # ──────────────────────────────────────────────────────────────────────────

    def _generate_token(self, username):
        """
        Create a new 64-character hex session token for the given username,
        store it in the in-memory token map, and return it.

        Tokens let clients reconnect without re-entering their password
        (AUTO_LOGIN).  In production you'd want these to expire and live
        in a database rather than in memory.
        """
        token = secrets.token_hex(32)    # 32 random bytes → 64 hex chars
        self.session_tokens[token] = username
        return token

    def _validate_token(self, token):
        """
        Return the username associated with a token, or None if the token
        is unknown / expired.
        """
        return self.session_tokens.get(token)

    # ──────────────────────────────────────────────────────────────────────────
    # Broadcast helpers
    # ──────────────────────────────────────────────────────────────────────────

    def _broadcast_session_list(self):
        """
        Push an updated session list to every connected client.
        Called after any change to the lobby (session created, started, finished).

        We snapshot the connected-client list under the lock, then release it
        before doing network I/O so we don't hold the lock while potentially
        blocking on a slow send.
        """
        sl  = self._session_list_payload()
        msg = f"SESSION_LIST|{json.dumps(sl)}\n"
        with self.lock:
            conns = list(self.client_usernames.keys())
        for c in conns:
            try:
                c.send(msg.encode('utf-8'))
            except Exception:
                pass

    def _session_list_payload(self):
        """
        Build the list of non-finished sessions to send to clients.
        We exclude finished sessions so the lobby stays clean.
        """
        with self.lock:
            sessions = list(self.sessions.values())
        return [s.get_info() for s in sessions if not s.finished]

    def _broadcast_session_count(self, session):
        """
        Tell each player in a session how many spectators are currently watching.
        Used to update the spectator badge in the game UI.
        """
        count = len(session.spectators)
        for c in session.players:
            if c:
                self._send(c, f"SPECTATOR_COUNT|{count}")

    # ──────────────────────────────────────────────────────────────────────────
    # Authentication
    # ──────────────────────────────────────────────────────────────────────────

    def _handle_auth(self, conn, line):
        """
        Process a LOGIN or REGISTER message from a freshly-connected client.

        Format:  "LOGIN|username|password"
                 "REGISTER|username|password"

        On success:  sends  AUTH_OK|username|{stats_json}|token
                     returns the username string
        On failure:  sends  AUTH_FAIL|reason
                     returns None  (caller should drop the connection)

        Passwords are stored as SHA-256 hashes.  We compare hashes, never
        plaintext, so we can't recover anyone's password even with disk access.
        """
        parts = line.split("|", 2)
        if len(parts) != 3:
            self._send(conn, "AUTH_FAIL|Invalid format")
            return None

        action, username, password = parts
        username = username.strip()
        password = password.strip()

        if not username or not password:
            self._send(conn, "AUTH_FAIL|Username and password required")
            return None

        hashed = hash_password(password)

        if action == "REGISTER":
            with self.lock:
                if username in self.accounts:
                    self._send(conn, "AUTH_FAIL|Username already taken")
                    return None
                # Create the new account with default stats
                self.accounts[username] = {
                    "password": hashed,
                    "elo":      1200,
                    "wins":     0,
                    "losses":   0,
                    "draws":    0,
                    "credits":  100,
                }
                save_accounts(self.accounts)

            stats = {k: v for k, v in self.accounts[username].items() if k != "password"}
            token = self._generate_token(username)
            self._send(conn, f"AUTH_OK|{username}|{json.dumps(stats)}|{token}")
            return username

        elif action == "LOGIN":
            with self.lock:
                acc = self.accounts.get(username)
            if not acc or acc.get("password") != hashed:
                # Same error message for "user not found" and "wrong password"
                # to prevent username enumeration attacks
                self._send(conn, "AUTH_FAIL|Wrong username or password")
                return None
            stats = {k: v for k, v in acc.items() if k != "password"}
            token = self._generate_token(username)
            self._send(conn, f"AUTH_OK|{username}|{json.dumps(stats)}|{token}")
            return username

        self._send(conn, "AUTH_FAIL|Unknown action")
        return None

    def _handle_auto_login(self, conn, token):
        """
        Attempt token-based re-authentication (AUTO_LOGIN).

        The client sends its saved token after an app restart.  If the token is
        still valid we send a fresh AUTH_OK (with a new token) so the client
        stays logged in seamlessly.  If the token has expired (server restarted,
        token revoked) we send AUTH_FAIL and the client falls back to the
        full login dialog.
        """
        username = self._validate_token(token)
        if not username:
            self._send(conn, "AUTH_FAIL|Invalid or expired session")
            return None

        with self.lock:
            acc = self.accounts.get(username)
        if not acc:
            self._send(conn, "AUTH_FAIL|Account not found")
            return None

        stats     = {k: v for k, v in acc.items() if k != "password"}
        new_token = self._generate_token(username)   # rotate the token on each login
        self._send(conn, f"AUTH_OK|{username}|{json.dumps(stats)}|{new_token}")
        return username

    # ──────────────────────────────────────────────────────────────────────────
    # ELO rating update
    # ──────────────────────────────────────────────────────────────────────────

    def _update_elo(self, session, winner_idx, draw=False):
        """
        Apply the Elo formula to both players after a game ends.

        winner_idx: the slot index (0 or 1) of the winning player.
                    Ignored when draw=True – both sides get the draw outcome.

        We use K=32 which is standard for beginner/intermediate players.
        After updating we immediately send STATS_UPDATE to both clients so
        they can display their new ELO without needing to re-login.

        This method acquires the server lock for the full account update +
        save, which means it briefly blocks other threads.  That's acceptable
        because disk I/O here is rare and fast compared to network latency.
        """
        winner_name = session.player_names[winner_idx]
        loser_name  = session.player_names[1 - winner_idx]
        if not winner_name or not loser_name:
            return   # Can't update if a slot was never filled (shouldn't happen)

        with self.lock:
            winner_acc = self.accounts.get(winner_name)
            loser_acc  = self.accounts.get(loser_name)
            if not winner_acc or not loser_acc:
                return

            K = 32
            # Expected score for the winner based on the ELO difference
            expected_winner = 1 / (1 + 10 ** ((loser_acc["elo"] - winner_acc["elo"]) / 400))

            if draw:
                # Draw: both players score 0.5
                winner_acc["elo"] += int(K * (0.5 - expected_winner))
                loser_acc["elo"]  += int(K * (0.5 - (1 - expected_winner)))
                winner_acc["draws"] = winner_acc.get("draws", 0) + 1
                loser_acc["draws"]  = loser_acc.get("draws", 0)  + 1
            else:
                # Decisive result: winner scores 1, loser scores 0
                winner_acc["elo"]    += int(K * (1 - expected_winner))
                loser_acc["elo"]     += int(K * (0 - (1 - expected_winner)))
                winner_acc["wins"]    = winner_acc.get("wins",   0) + 1
                loser_acc["losses"]   = loser_acc.get("losses",  0) + 1
                # Small credit reward for winning – part of the game's economy
                winner_acc["credits"] = winner_acc.get("credits", 0) + 10

            save_accounts(self.accounts)

            # Build stats payloads (exclude the password hash)
            winner_stats = {k: v for k, v in winner_acc.items() if k != "password"}
            loser_stats  = {k: v for k, v in loser_acc.items()  if k != "password"}

        # Push updated stats to both clients (outside the lock – just network I/O)
        winner_conn = session.players[winner_idx]
        loser_conn  = session.players[1 - winner_idx]
        if winner_conn:
            self._send(winner_conn, f"STATS_UPDATE|{json.dumps(winner_stats)}")
        if loser_conn:
            self._send(loser_conn,  f"STATS_UPDATE|{json.dumps(loser_stats)}")

    # ──────────────────────────────────────────────────────────────────────────
    # Session lifecycle
    # ──────────────────────────────────────────────────────────────────────────

    def _create_session(self, conn, username, tc, creator_color):
        """
        Handle CREATE_SESSION from a logged-in client.

        Creates a new Session, places the creator in their chosen color slot,
        and tells them to wait for an opponent.  The new session is immediately
        visible in the lobby list broadcast to all connected clients.

        Protocol sent to the creator:
          SESSION_CREATED|<sid>
          PLAYER|<pid>|<color>
          WAITING|Waiting for opponent...
        """
        creator_color = creator_color if creator_color in ("White", "Black") else "White"

        with self.lock:
            self.session_counter += 1
            sid  = str(self.session_counter)
            sess = Session(sid, username, tc, creator_color)

            acc    = self.accounts.get(username, {})
            c_slot = sess.creator_slot()   # 0 if White, 1 if Black

            # Place the creator in their chosen slot
            sess.players[c_slot]      = conn
            sess.player_names[c_slot] = username
            sess.player_elo[c_slot]   = acc.get("elo", 1200)
            sess._conn_slot[conn]     = c_slot

            # Register the session and the client's role
            self.sessions[sid]         = sess
            self.client_session[conn]  = sess
            self.client_role[conn]     = 'player'
            self.client_slot[conn]     = c_slot

        pid   = sess.slot_pid(c_slot)     # 1 or 2
        color = sess.slot_color(c_slot)   # "White" or "Black"

        self._send(conn, f"SESSION_CREATED|{sid}")
        self._send(conn, f"PLAYER|{pid}|{color}")
        self._send(conn, "WAITING|Waiting for opponent...")

        # Push the updated lobby list to everyone
        self._broadcast_session_list()

    def _join_session(self, conn, username, sid):
        """
        Handle JOIN_SESSION from a logged-in client.

        The joiner is placed in whichever slot the creator left empty.
        Once both slots are filled the game starts immediately:
          – Both players are told their color and their opponent's info.
          – GAME_START is sent to both.
          – The chess clock starts ticking (if timed).

        We handle several edge cases:
          • Session doesn't exist or is already finished → JOIN_FAIL
          • The joiner's slot is already taken → JOIN_FAIL (race condition)
          • The creator tries to join their own session → JOIN_FAIL
        """
        with self.lock:
            sess = self.sessions.get(sid)
            if not sess or sess.finished:
                self._send(conn, "JOIN_FAIL|Session not found or finished")
                return

            j_slot = sess.joiner_slot()   # the slot the creator left empty
            if sess.players[j_slot] is not None:
                # Someone else joined in the split second between our check and lock
                self._send(conn, "JOIN_FAIL|Session is full")
                return
            if conn in sess._conn_slot:
                # The creator of this session is trying to join it
                self._send(conn, "JOIN_FAIL|You created this session")
                return

            acc = self.accounts.get(username, {})
            sess.players[j_slot]      = conn
            sess.player_names[j_slot] = username
            sess.player_elo[j_slot]   = acc.get("elo", 1200)
            sess._conn_slot[conn]     = j_slot
            sess.status               = "playing"
            sess.started              = True

            self.client_session[conn] = sess
            self.client_role[conn]    = 'player'
            self.client_slot[conn]    = j_slot

        # ── Both players are now in their slots – start the game ──────────────
        c_slot = sess.creator_slot()
        j_slot = sess.joiner_slot()   # re-read after lock released is fine (immutable)

        p_creator = sess.players[c_slot]
        p_joiner  = sess.players[j_slot]

        creator_name = sess.player_names[c_slot]
        joiner_name  = sess.player_names[j_slot]

        creator_pid   = sess.slot_pid(c_slot)
        joiner_pid    = sess.slot_pid(j_slot)
        creator_color = sess.slot_color(c_slot)
        joiner_color  = sess.slot_color(j_slot)

        # Build stats payloads for the opponent-info messages
        p_creator_acc  = self.accounts.get(creator_name, {})
        p_joiner_acc   = self.accounts.get(joiner_name,  {})
        creator_stats  = {k: v for k, v in p_creator_acc.items() if k != "password"}
        joiner_stats   = {k: v for k, v in p_joiner_acc.items()  if k != "password"}

        # Tell the joiner their color assignment (creator already knew theirs)
        self._send(p_joiner, f"PLAYER|{joiner_pid}|{joiner_color}")

        # Each player learns who their opponent is
        self._send(p_creator, f"OPPONENT_INFO|{joiner_name}|{json.dumps(joiner_stats)}")
        self._send(p_joiner,  f"OPPONENT_INFO|{creator_name}|{json.dumps(creator_stats)}")

        # Kick off the game on both sides simultaneously
        gs = f"GAME_START|{sess.time_control_str}|{sess.base_seconds}|{sess.bonus}"
        self._send(p_creator, gs)
        self._send(p_joiner,  gs)

        # Start the server-side chess clock and send the initial sync
        sess.start_timer()
        if not sess.is_unlimited():
            tmsg = f"TIMER_SYNC|{sess.timers[0]:.1f}|{sess.timers[1]:.1f}"
            self._send(p_creator, tmsg)
            self._send(p_joiner,  tmsg)

        # Refresh the lobby (session status is now "playing")
        self._broadcast_session_list()

    def _spectate_session(self, conn, sid):
        """
        Handle SPECTATE_SESSION from a logged-in client.

        Spectators receive the full game state (all moves played so far,
        current timer values, player names) so they can reconstruct the
        position locally.  They continue to receive SPECTATE_MOVE messages
        as the game progresses.
        """
        with self.lock:
            sess = self.sessions.get(sid)
            if not sess or sess.finished:
                self._send(conn, "SPECTATE_FAIL|Session not found or finished")
                return
            if conn not in sess.spectators:
                sess.spectators.append(conn)
            self.client_session[conn] = sess
            self.client_role[conn]    = 'spectator'

        # Send the full game snapshot so the spectator can reconstruct the board
        info = sess.get_info()
        self._send(conn, f"SPECTATE_OK|{json.dumps(info)}")

        # Update the spectator count badge on the players' screens
        self._broadcast_session_count(sess)

    def _end_session(self, sess):
        """
        Mark a session as finished and remove it from all tracking structures.

        Called after checkmate, resign, draw, timeout, or both players
        disconnecting.  Once a session is ended it is removed from the
        sessions dict so it won't appear in the lobby and new clients
        can't join it.

        Note: we do NOT remove spectator entries here – spectators keep
        receiving broadcast messages until they disconnect or navigate away.
        That's handled in the individual disconnect-cleanup code.
        """
        sess.stop_timer()
        sess.finished = True
        sess.status   = "finished"
        with self.lock:
            self.sessions.pop(sess.session_id, None)
            # Clean up player slot tracking but leave spectators alone
            for c in sess.players:
                if c:
                    self.client_session.pop(c, None)
                    self.client_role.pop(c, None)
                    self.client_slot.pop(c, None)

    # ──────────────────────────────────────────────────────────────────────────
    # Per-client handler thread
    # ──────────────────────────────────────────────────────────────────────────

    def handle_client(self, conn, addr):
        """
        Main loop for a single client connection.  Runs in its own daemon thread.

        Phase 1 – Authentication:
          The client must send LOGIN|… or REGISTER|… (or AUTO_LOGIN|…) before
          any game messages are accepted.  A 60-second timeout prevents half-open
          connections from blocking resources indefinitely.

        Phase 2 – Lobby / game:
          After auth we remove the timeout and process game messages in a loop
          until the connection drops or an error occurs.

        Phase 3 – Disconnect cleanup:
          On exit we update ELO (if a game was in progress), notify the opponent
          and spectators, and remove all per-connection state.
        """
        buffer   = ""
        username = None

        # ── Phase 1: Authentication ───────────────────────────────────────────
        try:
            # 60-second deadline to complete login – prevents idle half-connections
            conn.settimeout(60)
            while username is None:
                chunk = conn.recv(4096).decode('utf-8', errors='replace')
                if not chunk:
                    conn.close()
                    return
                buffer += chunk
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    if line.startswith("LOGIN|") or line.startswith("REGISTER|"):
                        username = self._handle_auth(conn, line)
                    elif line.startswith("AUTO_LOGIN|"):
                        token    = line.split("|", 1)[1].strip()
                        username = self._handle_auto_login(conn, token)
                    else:
                        # Reject anything that isn't an auth message at this stage
                        self._send(conn, "AUTH_FAIL|Please login first")

        except Exception as e:
            sys.stderr.write(f"[AUTH ERROR] {addr}: {e}\n")
            try:
                conn.close()
            except Exception:
                pass
            return

        # Remove the auth timeout – the game session can run indefinitely
        conn.settimeout(None)

        # Register the username → connection mapping so we can broadcast to them
        with self.lock:
            self.client_usernames[conn] = username

        # Send the current lobby list so the client can open the session dialog
        sl = self._session_list_payload()
        self._send(conn, f"SESSION_LIST|{json.dumps(sl)}")

        # ── Phase 2: Lobby / game message loop ────────────────────────────────
        buffer = ""   # reset buffer after auth phase
        while True:
            try:
                data = conn.recv(4096).decode('utf-8', errors='replace')
                if not data:
                    break   # Clean disconnect
                buffer += data

                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue

                    # ── Keep-alive ────────────────────────────────────────────
                    if line == "PING":
                        # The client sends PING every 3 seconds and uses the RTT
                        # to display the latency badge.  We just bounce it back.
                        self._send(conn, "PONG")
                        continue

                    # ── Explicit lobby refresh ────────────────────────────────
                    if line == "SESSION_LIST_REQUEST":
                        sl = self._session_list_payload()
                        self._send(conn, f"SESSION_LIST|{json.dumps(sl)}")
                        continue

                    # ── Session creation ──────────────────────────────────────
                    if line.startswith("CREATE_SESSION|"):
                        # Protocol: CREATE_SESSION|time_control|creator_color
                        rest          = line.split("|", 2)
                        tc            = rest[1] if len(rest) > 1 else "0"
                        creator_color = rest[2] if len(rest) > 2 else "White"
                        self._create_session(conn, username, tc, creator_color)
                        continue

                    # ── Session joining ───────────────────────────────────────
                    if line.startswith("JOIN_SESSION|"):
                        sid = line.split("|", 1)[1]
                        self._join_session(conn, username, sid)
                        continue

                    # ── Spectating ────────────────────────────────────────────
                    if line.startswith("SPECTATE_SESSION|"):
                        sid = line.split("|", 1)[1]
                        self._spectate_session(conn, sid)
                        continue

                    # ── Voluntary session leave ───────────────────────────────
                    if line == "LEAVE_SESSION":
                        sess = self.client_session.get(conn)
                        if sess:
                            role = self.client_role.get(conn, '')
                            if role == 'spectator':
                                # Spectator just closes the watch view – no game impact
                                with self.lock:
                                    if conn in sess.spectators:
                                        sess.spectators.remove(conn)
                                    self.client_session.pop(conn, None)
                                    self.client_role.pop(conn, None)
                                if not sess.finished:
                                    self._broadcast_session_count(sess)

                            elif role == 'player' and not sess.started:
                                # Creator leaving before anyone joined – remove the room
                                self._end_session(sess)
                                self._broadcast_session_list()

                            else:
                                # Player leaving a game in progress – treat as forfeit
                                if not sess.finished:
                                    slot       = self.client_slot.get(conn, 0)
                                    winner_idx = 1 - slot
                                    self._update_elo(sess, winner_idx)
                                    for c in sess.players:
                                        if c and c is not conn:
                                            self._send(c, "OPPONENT_LEFT")
                                    for sp in list(sess.spectators):
                                        self._send(sp, f"SPECTATE_EVENT|{username} left")
                                    self._end_session(sess)
                                    self._broadcast_session_list()
                        continue

                    # ── Game messages (require an active session) ─────────────
                    sess = self.client_session.get(conn)
                    if sess is None:
                        continue   # Not in a session – ignore game messages

                    role         = self.client_role.get(conn, '')
                    is_spectator = (role == 'spectator')

                    # ── Move relay ────────────────────────────────────────────
                    if line.startswith("MOVE|"):
                        if is_spectator:
                            continue   # Spectators can't make moves
                        uci = line.split("|", 1)[1]
                        sess.moves.append(uci)
                        sess.switch_turn()   # flip clock to the opponent

                        # Build the timer sync message while we have fresh values
                        with sess.lock:
                            tmsg = f"TIMER_SYNC|{sess.timers[0]:.1f}|{sess.timers[1]:.1f}"

                        # Relay the move to the opponent + timer update
                        for c in sess.players:
                            if c and c is not conn:
                                self._send(c, f"MOVE|{uci}")
                                self._send(c, tmsg)

                        # Spectators get a different prefix so the client can
                        # apply it without being in a "my turn" state
                        for sp in list(sess.spectators):
                            self._send(sp, f"SPECTATE_MOVE|{uci}")
                            self._send(sp, tmsg)
                        continue

                    # ── Resignation ───────────────────────────────────────────
                    if line == "RESIGN":
                        if is_spectator:
                            continue
                        slot       = self.client_slot.get(conn, 0)
                        winner_idx = 1 - slot
                        self._update_elo(sess, winner_idx)
                        for c in sess.players:
                            if c and c is not conn:
                                self._send(c, "OPPONENT_RESIGNED")
                        for sp in list(sess.spectators):
                            self._send(sp, f"SPECTATE_EVENT|{username} resigned")
                        self._end_session(sess)
                        self._broadcast_session_list()
                        continue

                    # ── Draw offer ────────────────────────────────────────────
                    if line == "DRAW_OFFER":
                        if not is_spectator:
                            for c in sess.players:
                                if c and c is not conn:
                                    self._send(c, "DRAW_OFFER")
                        continue

                    # ── Draw accepted ─────────────────────────────────────────
                    if line == "DRAW_ACCEPT":
                        if not is_spectator:
                            for c in sess.players:
                                if c and c is not conn:
                                    self._send(c, "DRAW_ACCEPT")
                            # Draw is a half-point for both: use winner_idx=0 with draw=True
                            self._update_elo(sess, 0, draw=True)
                            for sp in list(sess.spectators):
                                self._send(sp, "SPECTATE_EVENT|Draw agreed")
                            self._end_session(sess)
                            self._broadcast_session_list()
                        continue

                    # ── Draw declined ─────────────────────────────────────────
                    if line == "DRAW_DECLINE":
                        if not is_spectator:
                            for c in sess.players:
                                if c and c is not conn:
                                    self._send(c, "DRAW_DECLINE")
                        continue

                    # ── Rematch request ───────────────────────────────────────
                    if line == "REMATCH_REQUEST":
                        if not is_spectator:
                            for c in sess.players:
                                if c and c is not conn:
                                    self._send(c, "REMATCH_REQUEST")
                        continue

                    # ── Rematch accepted ──────────────────────────────────────
                    if line == "REMATCH_ACCEPT":
                        if not is_spectator:
                            for c in sess.players:
                                if c and c is not conn:
                                    self._send(c, "REMATCH_ACCEPT")
                        continue

                    # ── Rematch declined ──────────────────────────────────────
                    if line == "REMATCH_DECLINE":
                        if not is_spectator:
                            for c in sess.players:
                                if c and c is not conn:
                                    self._send(c, "REMATCH_DECLINE")
                        continue

                    # ── Checkmate notification ────────────────────────────────
                    if line.startswith("CHECKMATE|"):
                        if not is_spectator:
                            try:
                                winner_pid = int(line.split("|")[1])
                                # pid 1 → slot 0 (White), pid 2 → slot 1 (Black)
                                winner_idx = winner_pid - 1
                                self._update_elo(sess, winner_idx)
                                winner_name = sess.player_names[winner_idx] or "?"
                                for sp in list(sess.spectators):
                                    self._send(
                                        sp,
                                        f"SPECTATE_EVENT|{winner_name} wins by checkmate!"
                                    )
                                self._end_session(sess)
                                self._broadcast_session_list()
                            except Exception:
                                pass
                        continue

                    # ── In-game chat relay ────────────────────────────────────
                    if line.startswith("CHAT|"):
                        msg_text = line.split("|", 1)[1]
                        if not is_spectator:
                            # Relay to the other player under a plain CHAT| prefix
                            for c in sess.players:
                                if c and c is not conn:
                                    self._send(c, f"CHAT|{msg_text}")
                        # Spectators see a labelled SPECTATE_CHAT so they know who said what
                        for sp in list(sess.spectators):
                            self._send(sp, f"SPECTATE_CHAT|{username}|{msg_text}")
                        continue

            except ConnectionResetError:
                break   # Client closed the connection abruptly (common on Windows)
            except Exception as e:
                sys.stderr.write(f"[ERROR {username}] {e}\n")
                break

        # ── Phase 3: Disconnect cleanup ───────────────────────────────────────
        # This runs whether the client disconnected cleanly or not.
        sess = self.client_session.get(conn)
        if sess:
            role = self.client_role.get(conn, '')
            if role == 'spectator':
                # Just remove them from the spectator list quietly
                with self.lock:
                    if conn in sess.spectators:
                        sess.spectators.remove(conn)
                    self.client_session.pop(conn, None)
                    self.client_role.pop(conn, None)
                if not sess.finished:
                    self._broadcast_session_count(sess)

            else:
                # A player disconnected – forfeit the game if it was in progress
                if not sess.finished:
                    if sess.started:
                        # Award the win to the remaining player and update ELO
                        slot       = self.client_slot.get(conn, 0)
                        winner_idx = 1 - slot
                        self._update_elo(sess, winner_idx)
                        for c in sess.players:
                            if c and c is not conn:
                                self._send(c, "OPPONENT_LEFT")
                        for sp in list(sess.spectators):
                            self._send(sp, f"SPECTATE_EVENT|{username} disconnected")
                    else:
                        # Creator disconnected before anyone joined
                        for sp in list(sess.spectators):
                            self._send(sp, f"SPECTATE_EVENT|{username} left")
                    self._end_session(sess)
                    self._broadcast_session_list()

        # Remove all references to this connection from server-level dicts
        with self.lock:
            self.client_usernames.pop(conn, None)
            self.client_session.pop(conn, None)
            self.client_role.pop(conn, None)
            self.client_slot.pop(conn, None)

        try:
            conn.close()
        except Exception:
            pass

    # ──────────────────────────────────────────────────────────────────────────
    # Main accept loop
    # ──────────────────────────────────────────────────────────────────────────

    def run(self):
        """
        Block forever, accepting new connections and spawning a daemon thread
        for each one.  Daemon threads means they die automatically when the
        main process exits (e.g. Ctrl+C) without needing explicit join() calls.
        """
        sys.stderr.write("[SERVER] Waiting for players...\n")
        while True:
            try:
                conn, addr = self.server.accept()
                t = threading.Thread(
                    target=self.handle_client,
                    args=(conn, addr),
                    daemon=True,
                )
                t.start()
            except Exception as e:
                sys.stderr.write(f"[SERVER ERROR] {e}\n")
                break


# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ChessServer().run()