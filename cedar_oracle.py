#!/usr/bin/env python3
"""Cedar Oracle: experimental psychological honeypot narrator.

Listens on a TCP socket, tracks returning visitors by IP in sqlite,
and responds with atmospheric, non-hostile text.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import random
import socket
import socketserver
import sqlite3
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any

DEFAULT_KEYWORDS = {
    "login",
    "password",
    "admin",
    "root",
    "token",
    "secret",
    "help",
    "oracle",
    "time",
    "whoami",
    "uname",
    "sudo",
    "ssh",
    "scan",
    "probe",
}

ADJECTIVES = [
    "quiet",
    "hollow",
    "amber",
    "distant",
    "mossy",
    "velvet",
    "still",
    "flickering",
    "weathered",
    "sepia",
]

NOUNS = [
    "lantern",
    "engine",
    "archive",
    "glyph",
    "watcher",
    "signal",
    "harbor",
    "echo",
    "circuit",
    "cedar",
]


def iso_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


@dataclass
class Config:
    host: str
    port: int
    db_path: str
    log_path: str
    rate_limit_count: int
    rate_limit_window: int
    recent_window: int
    slow_mode: bool
    slow_min_delay: float
    slow_max_delay: float
    mirror_mode: bool
    decay_half_life_hours: float


class OracleState:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.db_lock = threading.Lock()
        self.log_lock = threading.Lock()
        self.rate_lock = threading.Lock()
        self.rate_windows: dict[str, deque[float]] = defaultdict(deque)

        self.conn = sqlite3.connect(self.cfg.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self) -> None:
        with self.db_lock:
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS personas (
                    ip TEXT PRIMARY KEY,
                    alias TEXT NOT NULL,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    visits INTEGER NOT NULL,
                    drift REAL NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self.conn.commit()

    def log_event(self, event: str, **payload: Any) -> None:
        record = {"ts": iso_now(), "event": event, **payload}
        line = json.dumps(record, ensure_ascii=False)
        with self.log_lock:
            with open(self.cfg.log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")

    def check_rate_limit(self, ip: str) -> tuple[bool, int]:
        now = time.time()
        window = self.cfg.rate_limit_window
        with self.rate_lock:
            bucket = self.rate_windows[ip]
            while bucket and now - bucket[0] > window:
                bucket.popleft()
            if len(bucket) >= self.cfg.rate_limit_count:
                retry_after = int(window - (now - bucket[0])) if bucket else window
                return False, max(1, retry_after)
            bucket.append(now)
            return True, 0

    def _alias_for_ip(self, ip: str) -> str:
        digest = hashlib.sha256(ip.encode("utf-8")).digest()
        seed = int.from_bytes(digest[:8], "big")
        rng = random.Random(seed)
        return f"{rng.choice(ADJECTIVES)}-{rng.choice(NOUNS)}"

    def get_or_create_persona(self, ip: str) -> sqlite3.Row:
        now_iso = iso_now()
        with self.db_lock:
            row = self.conn.execute("SELECT * FROM personas WHERE ip = ?", (ip,)).fetchone()
            if row:
                return row
            alias = self._alias_for_ip(ip)
            self.conn.execute(
                """
                INSERT INTO personas (ip, alias, first_seen, last_seen, visits, drift, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (ip, alias, now_iso, now_iso, 0, 0.0, now_iso),
            )
            self.conn.commit()
            return self.conn.execute("SELECT * FROM personas WHERE ip = ?", (ip,)).fetchone()

    def update_persona_on_visit(self, ip: str) -> sqlite3.Row:
        now = dt.datetime.now(dt.timezone.utc)
        now_iso = now.isoformat()

        with self.db_lock:
            row = self.conn.execute("SELECT * FROM personas WHERE ip = ?", (ip,)).fetchone()
            if not row:
                row = self.get_or_create_persona(ip)

            updated_at = dt.datetime.fromisoformat(row["updated_at"])
            elapsed_hours = max(0.0, (now - updated_at).total_seconds() / 3600.0)
            decay = math.exp(-elapsed_hours / max(0.1, self.cfg.decay_half_life_hours))

            jitter_seed = hashlib.sha256(f"{ip}:{now_iso}".encode("utf-8")).digest()
            jitter = (int.from_bytes(jitter_seed[:2], "big") / 65535.0) - 0.5
            new_drift = float(row["drift"]) * decay + jitter * (1.0 - decay)
            visits = int(row["visits"]) + 1

            self.conn.execute(
                """
                UPDATE personas
                SET visits = ?, last_seen = ?, drift = ?, updated_at = ?
                WHERE ip = ?
                """,
                (visits, now_iso, new_drift, now_iso, ip),
            )
            self.conn.commit()
            return self.conn.execute("SELECT * FROM personas WHERE ip = ?", (ip,)).fetchone()


class CedarOracleHandler(socketserver.BaseRequestHandler):
    timeout = 12

    def handle(self) -> None:
        server: CedarOracleServer = self.server  # type: ignore[assignment]
        oracle = server.oracle
        cfg = oracle.cfg

        ip, port = self.client_address[0], self.client_address[1]
        allowed, retry_after = oracle.check_rate_limit(ip)

        reverse_dns = None
        try:
            reverse_dns = socket.gethostbyaddr(ip)[0]
        except (socket.herror, socket.gaierror, TimeoutError, OSError):
            reverse_dns = None

        if not allowed:
            msg = (
                "The cedar daemon breathes slowly. Return in "
                f"{retry_after}s, and the logbook may reopen.\n"
            )
            self.request.sendall(msg.encode("utf-8", errors="replace"))
            oracle.log_event(
                "rate_limited",
                ip=ip,
                port=port,
                reverse_dns=reverse_dns,
                retry_after=retry_after,
            )
            return

        persona_before = oracle.get_or_create_persona(ip)
        previous_visits = int(persona_before["visits"])
        recent = self._is_recent(persona_before["last_seen"], cfg.recent_window)

        incoming = self._read_client_message()
        keywords = self._extract_keywords(incoming)

        persona_after = oracle.update_persona_on_visit(ip)
        response = self._compose_response(
            alias=persona_after["alias"],
            visits=int(persona_after["visits"]),
            previous_visits=previous_visits,
            drift=float(persona_after["drift"]),
            recent=recent,
            incoming=incoming,
            keywords=keywords,
            mirror_mode=cfg.mirror_mode,
        )

        self._send_response(response, cfg)

        oracle.log_event(
            "interaction",
            ip=ip,
            port=port,
            reverse_dns=reverse_dns,
            alias=persona_after["alias"],
            visits=int(persona_after["visits"]),
            previous_visits=previous_visits,
            recent=recent,
            keywords=keywords,
            received=incoming,
            response=response,
        )

    def _read_client_message(self) -> str:
        self.request.settimeout(self.timeout)
        try:
            data = self.request.recv(4096)
        except socket.timeout:
            return ""
        except OSError:
            return ""
        return data.decode("utf-8", errors="replace").strip()

    def _is_recent(self, last_seen_iso: str, recent_window: int) -> bool:
        try:
            last_seen = dt.datetime.fromisoformat(last_seen_iso)
        except ValueError:
            return False
        now = dt.datetime.now(dt.timezone.utc)
        return (now - last_seen).total_seconds() <= recent_window

    def _extract_keywords(self, incoming: str) -> list[str]:
        if not incoming:
            return []
        tokens = {
            token.strip(".,!?;:'\"()[]{}<>").lower()
            for token in incoming.split()
            if token.strip()
        }
        return sorted(token for token in tokens if token in DEFAULT_KEYWORDS)

    def _time_flavor(self) -> str:
        hour = dt.datetime.now().hour
        if 5 <= hour < 12:
            return "Dawn clings to the stack traces."
        if 12 <= hour < 17:
            return "Afternoon light filters through dormant processes."
        if 17 <= hour < 22:
            return "Evening settles between old sockets."
        return "Night hums in the cooling fans."

    def _mirror_text(self, text: str) -> str:
        words = [w for w in text.split() if w]
        if not words:
            return ""
        swapped = []
        replace = {
            "you": "I",
            "i": "you",
            "my": "your",
            "your": "my",
            "me": "you",
            "am": "are",
            "are": "am",
        }
        for word in reversed(words[:24]):
            core = word.strip()
            lower = core.lower()
            swapped.append(replace.get(lower, core))
        return " ".join(swapped)

    def _compose_response(
        self,
        *,
        alias: str,
        visits: int,
        previous_visits: int,
        drift: float,
        recent: bool,
        incoming: str,
        keywords: list[str],
        mirror_mode: bool,
    ) -> str:
        lines = [f"[{alias}] {self._time_flavor()}"]

        if previous_visits == 0:
            lines.append("A new address touches the cedar bus. Welcome, quiet traveler.")
        elif recent:
            lines.append("You return before the cache cools. The daemon remembers your cadence.")
        else:
            lines.append(f"This is visit {visits}. Dust moved while you were away.")

        if keywords:
            joined = ", ".join(keywords)
            lines.append(
                f"Certain words stirred the archive ({joined}), but it offers only reflections, never instructions."
            )
        elif incoming:
            lines.append("Your signal was received and folded into the long logbook.")
        else:
            lines.append("Silence is also a packet. It still leaves a shape.")

        if abs(drift) < 0.08:
            lines.append("The persona is steady, like a clock that refuses applause.")
        elif drift > 0:
            lines.append("A hopeful variance appears: tomorrow may answer before you ask.")
        else:
            lines.append("A contemplative variance settles in: old errors become gentle omens.")

        if mirror_mode and incoming:
            lines.append(f"Mirror: {self._mirror_text(incoming)}")

        return "\n".join(lines) + "\n"

    def _send_response(self, response: str, cfg: Config) -> None:
        if not cfg.slow_mode:
            self.request.sendall(response.encode("utf-8", errors="replace"))
            return

        parts = [line + "\n" for line in response.strip("\n").split("\n")]
        rng = random.Random(time.time_ns())
        for part in parts:
            time.sleep(rng.uniform(cfg.slow_min_delay, cfg.slow_max_delay))
            self.request.sendall(part.encode("utf-8", errors="replace"))


class CedarOracleServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, server_address: tuple[str, int], handler: type[CedarOracleHandler], oracle: OracleState):
        self.oracle = oracle
        super().__init__(server_address, handler)


def start_heartbeat(oracle: OracleState, interval_s: int = 60) -> threading.Thread:
    def heartbeat() -> None:
        while True:
            try:
                load1, load5, load15 = os.getloadavg()
            except OSError:
                load1 = load5 = load15 = -1.0
            oracle.log_event(
                "heartbeat",
                load_1=load1,
                load_5=load5,
                load_15=load15,
                pid=os.getpid(),
            )
            time.sleep(interval_s)

    t = threading.Thread(target=heartbeat, name="cedar-heartbeat", daemon=True)
    t.start()
    return t


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Cedar Oracle psychological honeypot")
    p.add_argument("--host", default="0.0.0.0", help="Bind address")
    p.add_argument("--port", type=int, default=2323, help="TCP port")
    p.add_argument("--db", default="cedar_oracle.db", help="Path to sqlite database")
    p.add_argument("--log", default="cedar_oracle.log.jsonl", help="Path to JSONL interaction log")
    p.add_argument("--rate-limit-count", type=int, default=20, help="Max connections per window per IP")
    p.add_argument("--rate-limit-window", type=int, default=60, help="Rate-limit window in seconds")
    p.add_argument("--recent-window", type=int, default=300, help="Seconds considered a recent revisit")
    p.add_argument("--slow-mode", action="store_true", help="Enable deliberate response delays")
    p.add_argument("--slow-min-delay", type=float, default=0.25, help="Minimum delay between response lines")
    p.add_argument("--slow-max-delay", type=float, default=1.2, help="Maximum delay between response lines")
    p.add_argument("--mirror-mode", action="store_true", help="Enable mirrored/rephrased echoes")
    p.add_argument(
        "--decay-half-life-hours",
        type=float,
        default=48.0,
        help="Half-life used by persona decay/evolution model",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()

    if args.slow_min_delay < 0 or args.slow_max_delay < args.slow_min_delay:
        raise SystemExit("Invalid slow delay configuration")

    cfg = Config(
        host=args.host,
        port=args.port,
        db_path=args.db,
        log_path=args.log,
        rate_limit_count=args.rate_limit_count,
        rate_limit_window=args.rate_limit_window,
        recent_window=args.recent_window,
        slow_mode=args.slow_mode,
        slow_min_delay=args.slow_min_delay,
        slow_max_delay=args.slow_max_delay,
        mirror_mode=args.mirror_mode,
        decay_half_life_hours=args.decay_half_life_hours,
    )

    oracle = OracleState(cfg)
    start_heartbeat(oracle)

    with CedarOracleServer((cfg.host, cfg.port), CedarOracleHandler, oracle) as server:
        oracle.log_event("startup", host=cfg.host, port=cfg.port, pid=os.getpid())
        print(f"Cedar Oracle listening on {cfg.host}:{cfg.port}")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            oracle.log_event("shutdown", host=cfg.host, port=cfg.port)


if __name__ == "__main__":
    main()
