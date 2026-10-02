

from __future__ import annotations

import curses
import ipaddress
import platform
import queue
import re
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# moteur de ping
# ---------------------------------------------------------------------------

# regex qui marche quelle que soit la langue : "time=1ms" (EN), "temps<1ms" (FR), "Zeit=1ms" (DE)...
TIME_RE = re.compile(r"[=<]\s*(\d+(?:\.\d+)?)\s*ms", re.IGNORECASE)


@dataclass
class PingResult:
    target: str
    seq: int = 0
    ok: bool = False
    rtt_ms: float | None = None
    error: str | None = None


@dataclass
class TargetStats:
    target: str
    sent: int = 0
    received: int = 0
    lost: int = 0
    errors: int = 0
    rtts: list[float] = field(default_factory=list)

    @property
    def loss_pct(self) -> float:
        return self.lost / self.sent * 100.0 if self.sent else 0.0

    @property
    def min_rtt(self) -> float | None:
        return min(self.rtts) if self.rtts else None

    @property
    def avg_rtt(self) -> float | None:
        return sum(self.rtts) / len(self.rtts) if self.rtts else None

    @property
    def max_rtt(self) -> float | None:
        return max(self.rtts) if self.rtts else None


def ping_once(target: str, timeout_s: float) -> PingResult:
    """Envoie un seul ping avec la commande ping du système."""
    if platform.system() == "Windows":
        cmd = ["ping", "-n", "1", "-w", str(max(1, int(timeout_s * 1000))), target]
    else:  # Linux / macOS
        cmd = ["ping", "-c", "1", "-W", str(max(1, int(timeout_s))), target]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s + 3)
    except subprocess.TimeoutExpired:
        return PingResult(target, error="timed out")
    except FileNotFoundError:
        return PingResult(target, error="ping command not found")
    except OSError as exc:
        return PingResult(target, error=str(exc))

    output = f"{proc.stdout or ''}\n{proc.stderr or ''}"
    match = TIME_RE.search(output)
    if match:
        return PingResult(target, ok=True, rtt_ms=float(match.group(1)))
    # sans réponse (timeout / injoignable), ping affiche toujours une
    # ligne de stats avec un % ; un nom d'hôte bidon n'affiche rien
    if "%" in output:
        return PingResult(target)  # perdu
    return PingResult(target, error="host error")


def ping_worker(targets, count, interval, timeout_s, stop_event, result_queue):
    """Ping chaque cible `count` fois en arrière-plan, résultats dans la file."""
    for seq in range(1, count + 1):
        if stop_event.is_set():
            break
        for target in targets:
            if stop_event.is_set():
                break
            result = ping_once(target, timeout_s)
            result.seq = seq
            result_queue.put(result)
        if seq < count and not stop_event.is_set():
            deadline = time.monotonic() + interval
            while time.monotonic() < deadline and not stop_event.is_set():
                time.sleep(0.05)
    result_queue.put(None)  # sentinelle : la série est finie



# interface TUI


C_TITLE, C_OK, C_LOST, C_ERR, C_INFO, C_FIELD, C_BAR = range(7)

HELP = {
    1: "Type targets, comma separated  |  Enter: next  |  Esc: quit",
    2: "Tab/Arrows: switch field  Up/Down or +/-: change  |  Enter: start  |  Esc: back",
    3: "Q or Esc: stop early",
    4: "R: run again  |  Q: quit",
}


class App:
    def __init__(self, scr):
        self.scr = scr
        self.step = 1
        self.targets_input = ""
        self.cursor = 0
        self.targets: list[str] = []
        self.count = 4
        self.interval = 1.0
        self.timeout = 2.0
        self.field = 0
        self.message = ""
        self.stats: dict[str, TargetStats] = {}
        self.results: list[PingResult] = []
        self.done_pings = 0
        self.total_pings = 0
        self.current_seq = 0
        self.stopped_early = False
        self.stop_event = threading.Event()
        self.result_queue: queue.Queue[PingResult | None] = queue.Queue()
        self.worker: threading.Thread | None = None

    # -- affichage -----------------------------------------------------------

    def put(self, y, x, text, attr=0):
        """Écrit du texte, découpé si ça dépasse de l'écran."""
        h, w = self.scr.getmaxyx()
        if not (0 <= y < h and 0 <= x < w):
            return
        text = str(text)[: max(0, w - 1 - x)]
        if text:
            try:
                self.scr.addstr(y, x, text, attr)
            except curses.error:
                pass

    def draw_frame(self):
        self.scr.erase()
        h, w = self.scr.getmaxyx()
        try:
            self.scr.border()
        except curses.error:
            pass
        title_attr = curses.color_pair(C_TITLE) | curses.A_BOLD
        self.put(0, 2, " IP PINGER ", title_attr)
        step_label = f" STEP {self.step}/4 "
        self.put(0, max(2, w - len(step_label) - 2), step_label, title_attr)
        self.put(h - 1, 2, HELP[self.step], curses.color_pair(C_INFO))

    def draw_step1(self):
        self.put(2, 4, "Step 1 - Targets", curses.A_BOLD)
        self.put(4, 4, "Enter IP addresses or hostnames, separated by commas.")
        self.put(5, 4, "Examples:  8.8.8.8    1.1.1.1,8.8.4.4    google.com")
        self.put(7, 4, "Targets:")
        y, x = 8, 6
        self.put(y, 4, ">")
        field_attr = curses.color_pair(C_FIELD)
        before = self.targets_input[: self.cursor]
        after = self.targets_input[self.cursor :]
        self.put(y, x, before, field_attr)
        cx = x + len(before)
        if after:
            self.put(y, cx, after[0], field_attr | curses.A_REVERSE)
            self.put(y, cx + 1, after[1:], field_attr)
        else:
            self.put(y, cx, " ", field_attr | curses.A_REVERSE)
        preview = [t.strip() for t in self.targets_input.split(",") if t.strip()]
        if preview:
            self.put(10, 4, f"Parsed {len(preview)} target(s):")
            for i, target in enumerate(preview[:6]):
                self.put(11 + i, 6, f"* {target}")
        if self.message:
            self.put(18, 4, self.message, curses.color_pair(C_ERR))

    def draw_step2(self):
        self.put(2, 4, "Step 2 - Settings", curses.A_BOLD)
        fields = [
            ("Ping count", str(self.count), "pings per target (1-100)"),
            ("Interval", f"{self.interval:g}", "seconds between rounds (0.1-10)"),
            ("Timeout", f"{self.timeout:g}", "seconds to wait per reply (0.5-10)"),
        ]
        y = 5
        for i, (label, value, desc) in enumerate(fields):
            marker = ">" if i == self.field else " "
            attr = curses.color_pair(C_FIELD) if i == self.field else 0
            self.put(y, 4, f"{marker} {label}: {value:<6}  {desc}", attr)
            y += 2
        total = len(self.targets) * self.count
        self.put(y + 1, 4, f"Plan: {len(self.targets)} target(s) x {self.count} ping(s) = {total} ping(s) total")

    def draw_step3(self):
        self.put(2, 4, f"Step 3 - Pinging: {', '.join(self.targets)}", curses.A_BOLD)
        pct = self.done_pings / self.total_pings if self.total_pings else 0.0
        _, w = self.scr.getmaxyx()
        bar_w = max(10, min(50, w - 24))
        filled = round(bar_w * pct)
        bar = "#" * filled + "-" * (bar_w - filled)
        self.put(4, 4, f"[{bar}] {self.done_pings}/{self.total_pings} pings", curses.color_pair(C_BAR))
        self.put(5, 4, f"Round {min(self.current_seq, self.count)}/{self.count}   (Q/Esc to stop early)")
        h, _ = self.scr.getmaxyx()
        lines = []
        for r in self.results:
            if r.ok:
                lines.append((f"#{r.seq:<3} {r.target:<24} reply    {r.rtt_ms:7.1f} ms", C_OK))
            elif r.error:
                lines.append((f"#{r.seq:<3} {r.target:<24} ERROR   {r.error}", C_ERR))
            else:
                lines.append((f"#{r.seq:<3} {r.target:<24} timeout", C_LOST))
        visible = lines[-max(1, h - 12) :]
        y = 7
        for text, color in visible:
            self.put(y, 4, text, curses.color_pair(color))
            y += 1

    def draw_step4(self):
        self.put(2, 4, "Step 4 - Summary", curses.A_BOLD)
        if self.stopped_early:
            self.put(3, 4, "(stopped early - partial results)", curses.color_pair(C_LOST))

        def fmt(v):
            return f"{v:9.1f}" if v is not None else "       --"

        header = (
            f"{'Target':<24}{'Sent':>6}{'Recv':>6}{'Lost':>6}"
            f"{'Loss%':>8}{'Min ms':>9}{'Avg ms':>9}{'Max ms':>9}"
        )
        self.put(5, 4, header, curses.A_BOLD)
        self.put(6, 4, "-" * 78)
        y = 7
        for st in self.stats.values():
            line = (
                f"{st.target:<24}{st.sent:>6}{st.received:>6}{st.lost:>6}"
                f"{st.loss_pct:>7.0f}%{fmt(st.min_rtt)}{fmt(st.avg_rtt)}{fmt(st.max_rtt)}"
            )
            color = C_OK if st.lost == 0 else (C_LOST if st.received > 0 else C_ERR)
            self.put(y, 4, line, curses.color_pair(color))
            y += 1
        total_sent = sum(s.sent for s in self.stats.values())
        total_recv = sum(s.received for s in self.stats.values())
        loss = (1 - total_recv / total_sent) * 100 if total_sent else 0.0
        self.put(
            y + 1, 4,
            f"Overall: {total_recv}/{total_sent} replies received ({loss:.0f}% loss)",
            curses.A_BOLD,
        )

    def draw(self):
        self.draw_frame()
        if self.step == 1:
            self.draw_step1()
        elif self.step == 2:
            self.draw_step2()
        elif self.step == 3:
            self.draw_step3()
        else:
            self.draw_step4()
        self.scr.refresh()

    # -- logique ----------------------------------------------------------------

    def parse_targets(self) -> bool:
        raw = [t.strip() for t in self.targets_input.split(",")]
        targets = []
        for t in raw:
            if t and t not in targets:
                targets.append(t)
        if not targets:
            self.message = "Error: enter at least one target."
            return False
        for t in targets:
            try:
                ipaddress.ip_address(t)
                continue
            except ValueError:
                pass
            try:
                socket.getaddrinfo(t, None)
            except socket.gaierror:
                self.message = f"Error: '{t}' is not a valid IP address or hostname."
                return False
        self.targets = targets
        return True

    def start_run(self):
        self.stats = {t: TargetStats(t) for t in self.targets}
        self.results = []
        self.done_pings = 0
        self.total_pings = len(self.targets) * self.count
        self.current_seq = 0
        self.stopped_early = False
        self.stop_event.clear()
        self.result_queue = queue.Queue()
        self.worker = threading.Thread(
            target=ping_worker,
            args=(self.targets, self.count, self.interval, self.timeout,
                  self.stop_event, self.result_queue),
            daemon=True,
        )
        self.worker.start()

    def drain_queue(self):
        while True:
            try:
                item = self.result_queue.get_nowait()
            except queue.Empty:
                return
            if item is None:  # sentinelle : la série est finie
                self.step = 4
                continue
            r = item
            self.current_seq = r.seq
            self.results.append(r)
            self.done_pings += 1
            st = self.stats[r.target]
            st.sent += 1
            if r.ok:
                st.received += 1
                st.rtts.append(r.rtt_ms)
            else:
                st.lost += 1
                if r.error:
                    st.errors += 1

    def finish_run(self):
        if self.worker is not None and self.worker.is_alive():
            self.worker.join(timeout=self.timeout + 3)
        self.drain_queue()
        self.step = 4

    # -- saisie ----------------------------------------------------------------

    def handle(self, key) -> bool:
        """Gère une touche. Renvoie True pour quitter."""
        if self.step == 1:
            return self.handle_step1(key)
        if self.step == 2:
            return self.handle_step2(key)
        if self.step == 3:
            return self.handle_step3(key)
        return self.handle_step4(key)

    def handle_step1(self, key) -> bool:
        if key in (curses.KEY_BACKSPACE, 127, 8):
            if self.cursor > 0:
                self.targets_input = (
                    self.targets_input[: self.cursor - 1] + self.targets_input[self.cursor :]
                )
                self.cursor -= 1
        elif key == curses.KEY_DC:
            if self.cursor < len(self.targets_input):
                self.targets_input = (
                    self.targets_input[: self.cursor] + self.targets_input[self.cursor + 1 :]
                )
        elif key == curses.KEY_LEFT:
            self.cursor = max(0, self.cursor - 1)
        elif key == curses.KEY_RIGHT:
            self.cursor = min(len(self.targets_input), self.cursor + 1)
        elif key == curses.KEY_HOME:
            self.cursor = 0
        elif key == curses.KEY_END:
            self.cursor = len(self.targets_input)
        elif key in (10, 13, curses.KEY_ENTER):
            if self.parse_targets():
                self.message = ""
                self.step = 2
        elif key == 27:  # Échap
            return True
        elif 32 <= key <= 126:
            self.targets_input = (
                self.targets_input[: self.cursor] + chr(key) + self.targets_input[self.cursor :]
            )
            self.cursor += 1
        return False

    def handle_step2(self, key) -> bool:
        if key in (9, curses.KEY_RIGHT):  # Tab ou flèche droite : champ suivant
            self.field = (self.field + 1) % 3
        elif key in (getattr(curses, "KEY_BTAB", -1), curses.KEY_LEFT):
            self.field = (self.field - 1) % 3
        elif key in (curses.KEY_UP, ord("+"), ord("=")):
            self.bump(1)
        elif key in (curses.KEY_DOWN, ord("-"), ord("_")):
            self.bump(-1)
        elif key in (10, 13, curses.KEY_ENTER):
            self.start_run()
            self.step = 3
        elif key == 27:  # Échap
            self.step = 1
        return False

    def bump(self, direction):
        if self.field == 0:
            self.count = max(1, min(100, self.count + direction))
        elif self.field == 1:
            self.interval = round(max(0.1, min(10.0, self.interval + direction * 0.1)), 1)
        else:
            self.timeout = round(max(0.5, min(10.0, self.timeout + direction * 0.5)), 1)

    def handle_step3(self, key) -> bool:
        if key in (27, ord("q"), ord("Q")):
            self.stopped_early = True
            self.stop_event.set()
            self.finish_run()
        return False

    def handle_step4(self, key) -> bool:
        if key in (ord("r"), ord("R")):
            self.step = 1
            self.message = ""
        elif key in (27, ord("q"), ord("Q")):
            return True
        return False

    # -- boucle principale ------------------------------------------------------------

    def run(self):
        self.scr.keypad(True)
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        while True:
            if self.step == 3:
                self.drain_queue()
                self.scr.timeout(100)  # récupère les résultats pendant les pings
            else:
                self.scr.timeout(-1)  # attend une touche
            self.draw()
            key = self.scr.getch()
            if key == curses.KEY_RESIZE:
                continue
            if self.handle(key):
                break


def main(stdscr):
    try:
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(C_TITLE, curses.COLOR_CYAN, -1)
        curses.init_pair(C_OK, curses.COLOR_GREEN, -1)
        curses.init_pair(C_LOST, curses.COLOR_YELLOW, -1)
        curses.init_pair(C_ERR, curses.COLOR_RED, -1)
        curses.init_pair(C_INFO, curses.COLOR_BLUE, -1)
        curses.init_pair(C_FIELD, curses.COLOR_BLACK, curses.COLOR_WHITE)
        curses.init_pair(C_BAR, curses.COLOR_MAGENTA, -1)
    except curses.error:
        pass  # terminal sans couleurs : on continue quand même
    App(stdscr).run()


if __name__ == "__main__":
    try:
        curses.wrapper(main)
    except KeyboardInterrupt:
        pass
    except curses.error as exc:
        print(f"Could not start the TUI: {exc}")
        print("Run this from a real terminal window (stdin must be a TTY).")
