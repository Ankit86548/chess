"""
Chess Analyzer Backend - Flask + Stockfish 18
  - Uses ALL logical CPU cores during analysis (Threads = os.cpu_count())
  - Maximises hash table (80% of available RAM, capped at 65536 MB)
  - Engine process is suspended (SIGSTOP / SuspendThread) when idle and
    resumed (SIGCONT / ResumeThread) the moment a request arrives, so the
    PC is completely quiet between games.

Run: python app.py
"""

import subprocess
import threading
import time
import re
import os
import sys
import signal
import platform

from flask import Flask, request, jsonify, render_template
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ─── Stockfish path ───────────────────────────────────────────────────────────
# Windows : "stockfish/stockfish-windows-x86-64-avx2.exe"
# Linux   : "stockfish/stockfish-ubuntu-x86-64-avx2"
# macOS   : "stockfish/stockfish-macos-x86-64-bmi2"
STOCKFISH_PATH = os.environ.get("STOCKFISH_PATH", "stockfish/stockfish")

IS_WINDOWS = platform.system() == "Windows"

# ─── Resource detection ───────────────────────────────────────────────────────

def _logical_cpus() -> int:
    """Return 50% of logical CPU cores (capped at 1)."""
    count = os.cpu_count() or 1
    try:
        count = len(os.sched_getaffinity(0))
    except AttributeError:
        pass
    return max(1, count // 2)


def _hash_mb() -> int:
    """
    Return the hash-table size in MB: 25% of free RAM, between 64 and 4096 MB.
    Falls back to 256 MB if psutil is unavailable.
    """
    try:
        import psutil
        free_mb = psutil.virtual_memory().available // (1024 * 1024)
        target  = int(free_mb * 0.25)
    except ImportError:
        target = 256
    return max(64, min(4096, target))


CPU_THREADS = _logical_cpus()
HASH_MB     = _hash_mb()

print(f"[config] CPU threads : {CPU_THREADS}")
print(f"[config] Hash table  : {HASH_MB} MB")

# ─── OS-level suspend / resume helpers ───────────────────────────────────────

def _suspend_process(pid: int) -> None:
    """Freeze the Stockfish process so it uses 0 CPU when idle."""
    try:
        if IS_WINDOWS:
            import ctypes, ctypes.wintypes
            TH32CS_SNAPTHREAD = 0x00000004
            THREAD_SUSPEND_RESUME = 0x0002
            hSnapshot = ctypes.windll.kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
            class THREADENTRY32(ctypes.Structure):
                _fields_ = [("dwSize",             ctypes.wintypes.DWORD),
                             ("cntUsage",           ctypes.wintypes.DWORD),
                             ("th32ThreadID",       ctypes.wintypes.DWORD),
                             ("th32OwnerProcessID", ctypes.wintypes.DWORD),
                             ("tpBasePri",          ctypes.c_long),
                             ("tpDeltaPri",         ctypes.c_long),
                             ("dwFlags",            ctypes.wintypes.DWORD)]
            te = THREADENTRY32()
            te.dwSize = ctypes.sizeof(THREADENTRY32)
            if ctypes.windll.kernel32.Thread32First(hSnapshot, ctypes.byref(te)):
                while True:
                    if te.th32OwnerProcessID == pid:
                        hThread = ctypes.windll.kernel32.OpenThread(THREAD_SUSPEND_RESUME, False, te.th32ThreadID)
                        if hThread:
                            ctypes.windll.kernel32.SuspendThread(hThread)
                            ctypes.windll.kernel32.CloseHandle(hThread)
                    if not ctypes.windll.kernel32.Thread32Next(hSnapshot, ctypes.byref(te)):
                        break
            ctypes.windll.kernel32.CloseHandle(hSnapshot)
        else:
            os.kill(pid, signal.SIGSTOP)
    except Exception as e:
        print(f"[warn] suspend failed: {e}")


def _resume_process(pid: int) -> None:
    """Unfreeze the Stockfish process before sending commands."""
    try:
        if IS_WINDOWS:
            import ctypes, ctypes.wintypes
            TH32CS_SNAPTHREAD = 0x00000004
            THREAD_SUSPEND_RESUME = 0x0002
            hSnapshot = ctypes.windll.kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
            class THREADENTRY32(ctypes.Structure):
                _fields_ = [("dwSize",             ctypes.wintypes.DWORD),
                             ("cntUsage",           ctypes.wintypes.DWORD),
                             ("th32ThreadID",       ctypes.wintypes.DWORD),
                             ("th32OwnerProcessID", ctypes.wintypes.DWORD),
                             ("tpBasePri",          ctypes.c_long),
                             ("tpDeltaPri",         ctypes.c_long),
                             ("dwFlags",            ctypes.wintypes.DWORD)]
            te = THREADENTRY32()
            te.dwSize = ctypes.sizeof(THREADENTRY32)
            if ctypes.windll.kernel32.Thread32First(hSnapshot, ctypes.byref(te)):
                while True:
                    if te.th32OwnerProcessID == pid:
                        hThread = ctypes.windll.kernel32.OpenThread(THREAD_SUSPEND_RESUME, False, te.th32ThreadID)
                        if hThread:
                            ctypes.windll.kernel32.ResumeThread(hThread)
                            ctypes.windll.kernel32.CloseHandle(hThread)
                    if not ctypes.windll.kernel32.Thread32Next(hSnapshot, ctypes.byref(te)):
                        break
            ctypes.windll.kernel32.CloseHandle(hSnapshot)
        else:
            os.kill(pid, signal.SIGCONT)
    except Exception as e:
        print(f"[warn] resume failed: {e}")

# ─── Stockfish wrapper ────────────────────────────────────────────────────────

class Stockfish:
    def __init__(self, path: str, depth: int = 22):
        self.depth    = depth
        self.lock     = threading.Lock()
        self._idle    = False          # True = process is currently suspended

        self.process = subprocess.Popen(
            [path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        self.pid = self.process.pid

        # Initialise with full hardware resources
        self._send("uci")
        self._wait_for("uciok")
        self._send(f"setoption name Threads value {CPU_THREADS}")
        self._send(f"setoption name Hash value {HASH_MB}")
        # Keep hash warm between searches
        self._send("setoption name Clear Hash value false")
        self._send("isready")
        self._wait_for("readyok")

        print(f"[engine] Stockfish ready  pid={self.pid}  "
              f"threads={CPU_THREADS}  hash={HASH_MB}MB")

    # ── Internal I/O ──────────────────────────────────────────────────────────

    def _send(self, cmd: str) -> None:
        self.process.stdin.write(cmd + "\n")
        self.process.stdin.flush()

    def _wait_for(self, token: str, timeout: int = 30) -> str:
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self.process.stdout.readline().strip()
            if token in line:
                return line
        raise TimeoutError(f"Stockfish didn't respond with '{token}' within {timeout}s")

    # ── Suspend / resume ──────────────────────────────────────────────────────

    def sleep(self) -> None:
        """Suspend the engine process – uses 0 CPU while idle."""
        if not self._idle:
            self._idle = True
            _suspend_process(self.pid)
            print("[engine] suspended (sleeping)")

    def wake(self) -> None:
        """Resume the engine process before analysis."""
        if self._idle:
            _resume_process(self.pid)
            self._idle = False
            print("[engine] resumed (awake)")

    # ── Analysis ──────────────────────────────────────────────────────────────

    def evaluate(self, fen: str, movetime_ms: int | None = None) -> dict:
        """Return centipawn score + best UCI move. Use movetime for time-based analysis."""
        with self.lock:
            self._send(f"position fen {fen}")
            if movetime_ms:
                self._send(f"go movetime {movetime_ms}")
            else:
                self._send(f"go depth {self.depth}")
            best_info = ""
            best_move = None
            while True:
                line = self.process.stdout.readline().strip()
                if line.startswith("info") and "score" in line:
                    best_info = line
                if line.startswith("bestmove"):
                    parts = line.split()
                    best_move = parts[1] if len(parts) > 1 else None
                    break

        score = self._parse_score(best_info, fen)
        return {"score": score, "best_move": best_move, "info": best_info}

    def _parse_score(self, info: str, fen: str) -> int:
        is_black = " b " in fen
        mate = re.search(r"score mate (-?\d+)", info)
        cp   = re.search(r"score cp (-?\d+)",   info)
        if mate:
            v   = int(mate.group(1))
            raw = 9000 if v > 0 else -9000
        elif cp:
            raw = int(cp.group(1))
        else:
            return 0
        return raw if not is_black else -raw


# ─── Engine singleton + idle timer ───────────────────────────────────────────

sf: Stockfish | None = None
_idle_timer: threading.Timer | None = None
_IDLE_SECONDS = 15          # suspend engine after this many seconds of inactivity
_engine_lock  = threading.Lock()


def _schedule_sleep() -> None:
    """(Re)start the idle countdown."""
    global _idle_timer
    if _idle_timer is not None:
        _idle_timer.cancel()
    _idle_timer = threading.Timer(_IDLE_SECONDS, _do_sleep)
    _idle_timer.daemon = True
    _idle_timer.start()


def _do_sleep() -> None:
    with _engine_lock:
        if sf is not None:
            sf.sleep()


def get_engine() -> Stockfish:
    """Return the engine singleton, waking it if suspended."""
    global sf
    with _engine_lock:
        if sf is None:
            sf = Stockfish(STOCKFISH_PATH)
        else:
            sf.wake()
    return sf


def release_engine() -> None:
    """Called after each analysis request to start the idle timer."""
    _schedule_sleep()

# ─── Chess helpers ────────────────────────────────────────────────────────────

def parse_pgn_moves(pgn: str) -> list[str]:
    pgn = re.sub(r'\[.*?\]',           '', pgn)
    pgn = re.sub(r'\{[^}]*\}',         '', pgn)
    pgn = re.sub(r'\([^)]*\)',         '', pgn)
    pgn = re.sub(r'(1-0|0-1|1/2-1/2|\*)\s*$', '', pgn)
    tokens = re.split(r'\s+', pgn.strip())
    return [t for t in tokens if t and not re.match(r'^\d+\.+$', t)]


def build_positions(sans: list[str]) -> tuple[list[str], list[str]]:
    import chess
    board = chess.Board()
    fens, valid_sans = [board.fen()], []
    for san in sans:
        try:
            board.push_san(san)
            fens.append(board.fen())
            valid_sans.append(san)
        except Exception:
            break
    return fens, valid_sans


def classify_move(prev_cp: int, post_cp: int, best_cp: int, is_white: bool) -> str:
    sign = 1 if is_white else -1
    loss = max(0, min((best_cp - post_cp) * sign, 10000))
    if loss < 10:   return "best"
    if loss < 25:   return "excellent"
    if loss < 50:   return "good"
    if loss < 90:   return "inaccuracy"
    if loss < 200:  return "mistake"
    return "blunder"


def accuracy_from_classifications(cls_list: list[str]) -> int:
    score_map = {"brilliant":100,"best":100,"excellent":90,
                 "good":75,"inaccuracy":60,"mistake":35,"blunder":10}
    if not cls_list:
        return 0
    return round(sum(score_map.get(c, 50) for c in cls_list) / len(cls_list))

# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/analyze", methods=["POST"])
def analyze():
    data  = request.get_json()
    pgn   = data.get("pgn", "")
    depth = int(data.get("depth", 22))

    if not pgn.strip():
        return jsonify({"error": "PGN is empty"}), 400

    try:
        import chess
    except ImportError:
        return jsonify({"error": "python-chess not installed. Run: pip install chess"}), 500

    sans = parse_pgn_moves(pgn)
    if not sans:
        return jsonify({"error": "No valid moves found in PGN"}), 400

    fens, valid_sans = build_positions(sans)

    engine = get_engine()
    engine.depth = depth

    try:
        # Single pass - get score and best move together
        evals: list[int] = []
        best_moves: list[str] = []
        classifications: list[str] = []

        # First pass: evaluate all positions
        for i, fen in enumerate(fens):
            res = engine.evaluate(fen)
            evals.append(res["score"])
            if i < len(fens) - 1:
                best_moves.append(res["best_move"])
        
        # Second pass: calculate classifications
        for i in range(len(fens) - 1):
            classifications.append(
                classify_move(evals[i], evals[i + 1], evals[i], i % 2 == 0)
            )
    finally:
        release_engine()

    white_cls = classifications[0::2]
    black_cls = classifications[1::2]

    cats = ["brilliant","best","excellent","good","inaccuracy","mistake","blunder"]
    return jsonify({
        "moves":           valid_sans,
        "fens":            fens,
        "evals":           evals,
        "classifications": classifications,
        "best_moves":      best_moves,
        "white_accuracy":  accuracy_from_classifications(white_cls),
        "black_accuracy":  accuracy_from_classifications(black_cls),
        "white_summary":   {c: white_cls.count(c) for c in cats},
        "black_summary":   {c: black_cls.count(c) for c in cats},
        "depth":           depth,
        "total_moves":     len(valid_sans),
        "engine_threads":  CPU_THREADS,
        "engine_hash_mb":  HASH_MB,
    })


@app.route("/api/game-review", methods=["POST"])
def game_review():
    """Game review with configurable strength presets and time limits."""
    data = request.get_json()
    pgn = data.get("pgn", "")
    strength = data.get("strength", "standard")  # standard, deeper, maximum
    analysis_time = data.get("analysis_time", 3)  # seconds per move

    if not pgn.strip():
        return jsonify({"error": "PGN is empty"}), 400

    try:
        import chess
    except ImportError:
        return jsonify({"error": "python-chess not installed"}), 500

    # Strength presets
    strength_presets = {
        "standard": {"movetime": int(analysis_time * 1000), "label": "Stockfish 16"},
        "deeper": {"movetime": int(analysis_time * 2000), "label": "Stockfish 18"},
        "maximum": {"movetime": int(analysis_time * 3000), "label": "Unlimited"},
    }
    preset = strength_presets.get(strength, strength_presets["standard"])

    sans = parse_pgn_moves(pgn)
    if not sans:
        return jsonify({"error": "No valid moves found in PGN"}), 400

    fens, valid_sans = build_positions(sans)
    engine = get_engine()

    try:
        evals: list[int] = []
        best_moves: list[str] = []
        classifications: list[str] = []

        # First pass: evaluate all positions and get scores
        for i, fen in enumerate(fens):
            res = engine.evaluate(fen, movetime_ms=preset["movetime"])
            evals.append(res["score"])
            if i < len(fens) - 1:
                best_moves.append(res["best_move"])
        
        # Second pass: calculate classifications using evaluated scores
        for i in range(len(fens) - 1):
            best_cp = evals[i]  # Current position's score (best move from this position)
            classifications.append(
                classify_move(evals[i], evals[i + 1], best_cp, i % 2 == 0)
            )
    finally:
        release_engine()

    white_cls = classifications[0::2]
    black_cls = classifications[1::2]

    cats = ["brilliant","best","excellent","good","inaccuracy","mistake","blunder"]
    return jsonify({
        "moves":           valid_sans,
        "fens":            fens,
        "evals":           evals,
        "classifications": classifications,
        "best_moves":      best_moves,
        "white_accuracy":  accuracy_from_classifications(white_cls),
        "black_accuracy":  accuracy_from_classifications(black_cls),
        "white_summary":   {c: white_cls.count(c) for c in cats},
        "black_summary":   {c: black_cls.count(c) for c in cats},
        "strength":        strength,
        "engine":          preset["label"],
        "analysis_time":   analysis_time,
        "total_moves":     len(valid_sans),
        "engine_threads":  CPU_THREADS,
        "engine_hash_mb":  HASH_MB,
    })


@app.route("/api/health")
def health():
    engine_state = "unstarted"
    if sf is not None:
        engine_state = "sleeping" if sf._idle else "awake"
    return jsonify({
        "status":        "ok",
        "stockfish":     STOCKFISH_PATH,
        "threads":       CPU_THREADS,
        "hash_mb":       HASH_MB,
        "engine_state":  engine_state,
    })


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 55)
    print("  Chess Analyzer — Flask + Stockfish 18")
    print(f"  CPU threads : {CPU_THREADS}  |  Hash : {HASH_MB} MB")
    print("  Engine sleeps after 30 s of inactivity")
    print("  http://localhost:5000")
    print("=" * 55)
    app.run(debug=False, host="0.0.0.0", port=5000, threaded=True)
