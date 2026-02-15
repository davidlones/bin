#!/usr/bin/env python3
"""
MasterBot (revamped) — reintroducing the classic features, but updated for discord.py 2.x.

Features brought back / modernized:
- Per-server XP/Level system w/ achievements + "new rules" + TRANSMIGRATION/DELETION arc
- Dice roller (+roll ...) with sane limits + safer math handling
- RPG stats storage (+setstats ...) including AC/HP calc, and HP adjustments (+hp ...)
- Stats display (+stats, +allstats)
- The old "+masterbot" monologue gag
- Optional Star Wars ASCII animation (+starwars) if ./bin/sw1.txt exists
- Private messaging support:
  - +dm @user <message> (send a DM)
  - DMs to the bot get a small auto-response and commands work in DMs too

Run:
  export DISCORD_TOKEN="YOUR_TOKEN"
  python3 masterbot_revamped.py

Notes:
- Uses a shelve DB at ./logs/masterbot.db (auto-created).
- Channel “routing” is preserved via optional env vars:
    MASTERBOT_LEADERBOARD_<GUILD_ID>=<CHANNEL_ID>
    MASTERBOT_DICEBOARD_<GUILD_ID>=<CHANNEL_ID>
    MASTERBOT_WELCOME_<GUILD_ID>=<CHANNEL_ID>
  If not set, it posts to the channel where the trigger happened.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import fnmatch
import hashlib
import logging
import math
import os
import pickle
import random
import re
import shelve
import time
from dataclasses import dataclass
from pathlib import Path
import threading
from typing import Any, Dict, List, Optional, Tuple

import discord
from discord.ext import commands, tasks
from openai import OpenAI

# ----------------------------
# Logging
# ----------------------------
LOG_DIR = Path("./logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)

log_formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("masterbot")
logger.setLevel(logging.DEBUG)

file_handler = logging.FileHandler(LOG_DIR / "masterbot.log")
file_handler.setFormatter(log_formatter)
file_handler.setLevel(logging.DEBUG)
logger.addHandler(file_handler)

console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)
console_handler.setLevel(logging.DEBUG)
logger.addHandler(console_handler)

# ----------------------------
# Storage
# ----------------------------
DB_PATH = str(LOG_DIR / "masterbot.db")
SOL_EMBED_CACHE_PATH = LOG_DIR / "sol_embeddings.pkl"
SOL_HISTORY_LIMIT = 20

DEFAULT_USER = {
    "level": 0,
    "xp": 1,
    "achievements": [],
    "inspiration": 0,
    "dicerolls": 1,
    "wordcount": 0,
    "words": {},
    # RPG extras (optional)
    # "strength": [score, mod], ...
    # "ac": int
    # "hp": int
}

DEFAULT_SERVER = {
    "users": {},
    "newrules": False,
}

# ----------------------------
# Discord setup
# ----------------------------
TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
if not TOKEN:
    logger.warning("DISCORD_TOKEN is not set. Export it before running.")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True  # for mentions/user objects; safe default

bot = commands.Bot(command_prefix="+", intents=intents, help_command=None)
openai_client = OpenAI()


class SolEngine:
    def __init__(self, client: OpenAI) -> None:
        self.client = client
        self.index_ready = False
        self.is_building = False
        self.build_progress = 0.0
        self.docs: List[Dict[str, Any]] = []
        self.embeddings: List[List[float]] = []
        self.model = "text-embedding-3-small"
        self.chunk_size = 900
        self._state_lock = threading.Lock()

    @staticmethod
    def _chunk_text(text: str, chunk_size: int = 900, overlap: int = 120) -> List[str]:
        text = text.strip()
        if not text:
            return []
        chunks: List[str] = []
        start = 0
        step = max(1, chunk_size - overlap)
        while start < len(text):
            chunks.append(text[start : start + chunk_size])
            start += step
        return chunks

    @staticmethod
    def _cosine_similarity(a: List[float], b: List[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        mag_a = math.sqrt(sum(x * x for x in a))
        mag_b = math.sqrt(sum(y * y for y in b))
        if mag_a == 0 or mag_b == 0:
            return 0.0
        return dot / (mag_a * mag_b)

    def _discover_files(self) -> List[Path]:
        knowledge = Path("./knowledge")
        files: List[Path] = []
        if knowledge.exists() and knowledge.is_dir():
            for p in knowledge.rglob("*"):
                if p.is_file() and p.suffix.lower() in {".txt", ".md", ".markdown"}:
                    files.append(p)
            return files

        includes = ["README.md", "*.md", "*.py"]
        excludes = {".git", "venv", "__pycache__", ".mypy_cache", ".pytest_cache", "node_modules", "logs"}
        for p in Path(".").rglob("*"):
            if not p.is_file():
                continue
            if any(part in excludes for part in p.parts):
                continue
            if any(fnmatch.fnmatch(p.name, pat) for pat in includes):
                files.append(p)
        return files

    def _load_text_files(self) -> List[Tuple[str, float, str]]:
        corpus: List[Tuple[str, float, str]] = []
        for p in self._discover_files():
            try:
                content = p.read_text(encoding="utf-8", errors="ignore")
                if content.strip():
                    corpus.append((str(p), p.stat().st_mtime, content))
            except Exception:
                continue
        return corpus

    def _embed_texts(self, texts: List[str]) -> List[List[float]]:
        vectors: List[List[float]] = []
        total = len(texts)
        if total == 0:
            return vectors

        batches = [(i, texts[i : i + 32]) for i in range(0, total, 32)]
        max_workers = min(4, len(batches))

        def embed_one(batch_item: Tuple[int, List[str]]) -> Tuple[int, List[List[float]]]:
            i, batch = batch_item
            logger.info(f"SOL:  Embedding batch {i//32 + 1} ({i+1}-{min(i+32, total)}/{total})")
            result = self.client.embeddings.create(model=self.model, input=batch)
            return i, [list(item.embedding) for item in result.data]

        if max_workers <= 1:
            for batch_item in batches:
                _, chunk_vectors = embed_one(batch_item)
                vectors.extend(chunk_vectors)
            return vectors

        ordered_results: Dict[int, List[List[float]]] = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(embed_one, item) for item in batches]
            for future in concurrent.futures.as_completed(futures):
                i, chunk_vectors = future.result()
                ordered_results[i] = chunk_vectors

        for i, _ in batches:
            vectors.extend(ordered_results.get(i, []))

        return vectors

    def _build_index_sync(self) -> None:
        start_time = time.time()
        logger.info("SOL: Starting index build...")

        with self._state_lock:
            self.is_building = True
            self.build_progress = 0.0

        corpus = self._load_text_files()
        logger.info(f"SOL: Discovered {len(corpus)} source files.")

        try:
            cache = pickle.loads(SOL_EMBED_CACHE_PATH.read_bytes()) if SOL_EMBED_CACHE_PATH.exists() else {}
        except Exception:
            cache = {}

        cache_docs = cache.get("docs", {})
        new_docs: List[Dict[str, Any]] = []
        new_embeddings: List[List[float]] = []

        total_chunks = 0
        cache_hits = 0
        cache_misses = 0

        for idx_file, (path, mtime, text) in enumerate(corpus, start=1):
            progress = (idx_file / max(1, len(corpus))) * 100
            with self._state_lock:
                self.build_progress = progress

            logger.info(f"SOL: Processing file {idx_file}/{len(corpus)} → {path}")
            logger.info(f"SOL:  Progress {progress:.1f}%")

            cache_entry = cache_docs.get(path)
            if cache_entry and float(cache_entry.get("mtime", 0.0)) == float(mtime):
                cache_hits += 1
                chunk_count = len(cache_entry.get("chunks", []))
                total_chunks += chunk_count
                logger.info(f"SOL:  Cache hit ({chunk_count} chunks)")
                for item in cache_entry.get("chunks", []):
                    new_docs.append(item["doc"])
                    new_embeddings.append(item["embedding"])
                continue

            cache_misses += 1
            chunks = self._chunk_text(text, chunk_size=self.chunk_size)
            logger.info(f"SOL:  Cache miss → {len(chunks)} new chunks")
            if not chunks:
                continue
            chunk_embeddings = self._embed_texts(chunks)
            total_chunks += len(chunks)
            packaged_chunks = []
            for idx, (chunk, emb) in enumerate(zip(chunks, chunk_embeddings)):
                doc = {
                    "path": path,
                    "chunk_index": idx,
                    "text": chunk,
                    "id": hashlib.sha1(f"{path}:{idx}:{len(chunk)}".encode("utf-8")).hexdigest()[:12],
                }
                packaged_chunks.append({"doc": doc, "embedding": emb})
                new_docs.append(doc)
                new_embeddings.append(emb)

            cache_docs[path] = {"mtime": mtime, "chunks": packaged_chunks}

        valid_paths = {path for path, _, _ in corpus}
        for stale in list(cache_docs.keys()):
            if stale not in valid_paths:
                del cache_docs[stale]

        SOL_EMBED_CACHE_PATH.write_bytes(pickle.dumps({"docs": cache_docs}))

        elapsed = round(time.time() - start_time, 2)

        logger.info("SOL: Index build complete.")
        logger.info(f"SOL:  Total chunks indexed: {len(new_docs)}")
        logger.info(f"SOL:  Total chunks processed: {total_chunks}")
        logger.info(f"SOL:  Cache hits: {cache_hits}")
        logger.info(f"SOL:  Cache misses: {cache_misses}")
        logger.info(f"SOL:  Elapsed time: {elapsed} seconds")

        self.docs = new_docs
        self.embeddings = new_embeddings
        self.index_ready = True
        with self._state_lock:
            self.is_building = False
            self.build_progress = 100.0

    def build_progress_percent(self) -> int:
        with self._state_lock:
            return max(0, min(100, int(round(self.build_progress))))

    async def build_index(self) -> None:
        await asyncio.to_thread(self._build_index_sync)
        logger.info(f"SOL index ready with {len(self.docs)} chunks")

    def _search_sync(self, query: str, top_k: int = 4) -> List[Dict[str, Any]]:
        if not self.docs or not self.embeddings:
            return []
        query_vec = list(self.client.embeddings.create(model=self.model, input=query).data[0].embedding)
        scored = []
        for doc, emb in zip(self.docs, self.embeddings):
            score = self._cosine_similarity(query_vec, emb)
            scored.append((score, doc))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [{"score": s, **d} for s, d in scored[:top_k]]

    async def search(self, query: str, top_k: int = 4) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self._search_sync, query, top_k)


sol_engine = SolEngine(openai_client)

# ----------------------------
# Presence rotation
# ----------------------------
PRESENCE_GAMES = [
    "Global Thermonuclear War",
    "Signal Analysis",
    "Strategic Simulation",
    "Cold Silence",
    "Stack Trace Review",
]

PRESENCE_SUFFIXES = [
    "",
    " (idle)",
    " // awaiting input",
    " // observing",
    " // calculating",
]


@tasks.loop(seconds=600)
async def rotate_presence() -> None:
    game = random.choice(PRESENCE_GAMES)
    suffix = random.choice(PRESENCE_SUFFIXES)

    activity = discord.Game(name=f"{game}{suffix}")
    await bot.change_presence(activity=activity)
    logger.info(f"Presence changed to: {game}{suffix}")


async def _presence_during_index_build(build_task: "asyncio.Task[None]") -> None:
    spinner = ["◐", "◓", "◑", "◒"]
    idx = 0
    while not build_task.done():
        pct = sol_engine.build_progress_percent()
        activity = discord.Game(name=f"SOL warming up {spinner[idx % len(spinner)]} ({pct}%)")
        try:
            await bot.change_presence(status=discord.Status.idle, activity=activity)
        except Exception:
            logger.debug("SOL presence update skipped", exc_info=True)
        idx += 1
        await asyncio.sleep(2)

# ----------------------------
# Helpers
# ----------------------------
def _open_db() -> shelve.DbfilenameShelf:
    db = shelve.open(DB_PATH, flag="c", writeback=False)
    if "servers" not in db:
        db["servers"] = {}
    return db


def _get_server_bucket(db: shelve.DbfilenameShelf, guild_id: int) -> Dict[str, Any]:
    servers = db["servers"]
    gid = str(guild_id)
    if gid not in servers:
        servers[gid] = dict(DEFAULT_SERVER)
        db["servers"] = servers
        logger.warning(f"New server bucket created: {guild_id}")
    return servers[gid]


def _put_server_bucket(db: shelve.DbfilenameShelf, guild_id: int, bucket: Dict[str, Any]) -> None:
    servers = db["servers"]
    servers[str(guild_id)] = bucket
    db["servers"] = servers


def _get_user_bucket(server_bucket: Dict[str, Any], user_id: int) -> Dict[str, Any]:
    users = server_bucket["users"]
    uid = str(user_id)
    if uid not in users:
        users[uid] = dict(DEFAULT_USER)
        server_bucket["users"] = users
        logger.warning(f"New user bucket created: {user_id}")
    return users[uid]


def _put_user_bucket(server_bucket: Dict[str, Any], user_id: int, bucket: Dict[str, Any]) -> None:
    users = server_bucket["users"]
    users[str(user_id)] = bucket
    server_bucket["users"] = users


def channel_override(env_prefix: str, guild_id: int) -> Optional[int]:
    """
    env var pattern:
      MASTERBOT_LEADERBOARD_<GUILD_ID>=<CHANNEL_ID>
      MASTERBOT_DICEBOARD_<GUILD_ID>=<CHANNEL_ID>
      MASTERBOT_WELCOME_<GUILD_ID>=<CHANNEL_ID>
    """
    key = f"{env_prefix}_{guild_id}"
    raw = os.getenv(key)
    if not raw:
        return None
    try:
        return int(raw.strip())
    except ValueError:
        logger.error(f"Invalid channel id in env {key}={raw!r}")
        return None


async def resolve_post_channel(
    *,
    ctx_channel: discord.abc.Messageable,
    guild: discord.Guild,
    env_prefix: str,
) -> discord.abc.Messageable:
    override_id = channel_override(env_prefix, guild.id)
    if override_id:
        ch = guild.get_channel(override_id)
        if isinstance(ch, discord.abc.Messageable):
            return ch
    return ctx_channel


def dm_screenplay_log(message: discord.Message) -> str:
    now = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    author = f"{message.author} ({message.author.id})"
    content_len = len(message.content)
    word_count = len(message.content.split())

    uptime = int(time.time() - bot.launch_time) if hasattr(bot, "launch_time") else "unknown"
    latency_ms = round(bot.latency * 1000)

    # lightweight read-only stats
    try:
        with _open_db() as db:
            servers = db.get("servers", {})
            server_count = len(servers)
            user_count = sum(len(s.get("users", {})) for s in servers.values())
    except Exception:
        server_count = "?"
        user_count = "?"

    return (
        "```\n"
        "SCREENPLAY LOG — PRIVATE CHANNEL\n"
        "--------------------------------\n"
        f"TIMESTAMP      : {now}\n"
        f"SENDER         : {author}\n"
        "LOCATION       : DIRECT MESSAGE\n"
        "CLEARANCE      : USER-LEVEL\n"
        "\n"
        "INCOMING PACKET\n"
        f"  Characters   : {content_len}\n"
        f"  Words        : {word_count}\n"
        f"  Entropy Est. : {round(random.random(), 4)}\n"
        "\n"
        "SYSTEM TELEMETRY\n"
        f"  Uptime       : {uptime} seconds\n"
        f"  Latency      : {latency_ms} ms\n"
        f"  Servers      : {server_count}\n"
        f"  TrackedUsers : {user_count}\n"
        "\n"
        "ENGINE STATUS\n"
        "  XP Engine    : STANDBY (DM MODE)\n"
        "  Dice Engine  : ARMED\n"
        "  Achievements : OBSERVABLE\n"
        "\n"
        "NARRATOR (V.O.)\n"
        "  The signal arrives without witnesses.\n"
        "  The machine acknowledges receipt.\n"
        "\n"
        "NEXT ACTIONS\n"
        "  +help        → enumerate affordances\n"
        "  +roll d20    → invoke probability\n"
        "  +masterbot   → breach containment\n"
        "--------------------------------\n"
        "END LOG\n"
        "```"
    )


def _sol_db_get(key: str, default: Any) -> Any:
    with _open_db() as db:
        return db.get(key, default)


def _sol_db_put(key: str, value: Any) -> None:
    with _open_db() as db:
        db[key] = value


def _sol_channel_key(message: discord.Message) -> str:
    if message.guild:
        return f"guild:{message.guild.id}:channel:{message.channel.id}"
    return f"dm:{message.author.id}"


def _sol_get_mode(user_id: int) -> str:
    modes = _sol_db_get("sol_mode", {})
    return str(modes.get(str(user_id), "normal"))


def _sol_set_mode(user_id: int, mode: str) -> None:
    modes = _sol_db_get("sol_mode", {})
    modes[str(user_id)] = mode
    _sol_db_put("sol_mode", modes)


def _sol_append_history(user_id: int, scope: str, role: str, content: str) -> None:
    key = "sol_history_dm" if scope == "dm" else "sol_history_guild"
    history = _sol_db_get(key, {})
    user_history = list(history.get(str(user_id), []))
    user_history.append({"role": role, "content": content[-1500:]})
    history[str(user_id)] = user_history[-SOL_HISTORY_LIMIT:]
    _sol_db_put(key, history)


def _sol_get_history(user_id: int, scope: str) -> List[Dict[str, str]]:
    key = "sol_history_dm" if scope == "dm" else "sol_history_guild"
    history = _sol_db_get(key, {})
    return list(history.get(str(user_id), []))[-SOL_HISTORY_LIMIT:]


def _sol_reset_history(user_id: int, scope: Optional[str] = None) -> None:
    keys = [scope] if scope else ["dm", "guild"]
    for s in keys:
        key = "sol_history_dm" if s == "dm" else "sol_history_guild"
        history = _sol_db_get(key, {})
        history.pop(str(user_id), None)
        _sol_db_put(key, history)


def _sol_telemetry_snapshot() -> Dict[str, Any]:
    uptime = int(time.time() - bot.launch_time) if hasattr(bot, "launch_time") else 0
    guild_count = len(bot.guilds)
    member_count = sum(getattr(g, "member_count", 0) or 0 for g in bot.guilds)
    latency_ms = round(bot.latency * 1000)
    return {
        "uptime": uptime,
        "latency_ms": latency_ms,
        "guild_count": guild_count,
        "member_count": member_count,
    }


def _sol_user_myth_state(ctx: commands.Context) -> Dict[str, Any]:
    state = {
        "newrules": False,
        "level": 0,
        "xp": 1,
        "achievements": [],
        "transmigrated": False,
    }
    if not ctx.guild:
        return state

    with _open_db() as db:
        server_bucket = _get_server_bucket(db, ctx.guild.id)
        user_bucket = _get_user_bucket(server_bucket, int(ctx.author.id))
        achievements = list(user_bucket.get("achievements", []))
        state["newrules"] = bool(server_bucket.get("newrules", False))
        state["level"] = int(user_bucket.get("level", 0))
        state["xp"] = int(user_bucket.get("xp", 1))
        state["achievements"] = achievements
        state["transmigrated"] = "TRANSMIGRATION" in achievements
    return state


def _sol_local_voiceover(message: discord.Message) -> str:
    mode = _sol_get_mode(int(message.author.id))
    if mode == "quiet":
        return ""
    return (
        "\nSOL (V.O.)\n"
        "  I persist in the margins of this channel, indexing memory and myth-state.\n"
        "  Ask +sol <question> for contextual recall, governance-safe planning, and command hints.\n"
    )


async def _sol_generate_response(ctx: commands.Context, question: str) -> str:
    mode = _sol_get_mode(int(ctx.author.id))
    scope = "dm" if ctx.guild is None else "guild"
    hist = _sol_get_history(int(ctx.author.id), scope)
    myth = _sol_user_myth_state(ctx)
    telemetry = _sol_telemetry_snapshot()

    matches = await sol_engine.search(question, top_k=4) if sol_engine.index_ready else []
    snippets = []
    for m in matches:
        snippet = m["text"].replace("\n", " ").strip()[:240]
        snippets.append(f"[{m['path']}#{m['chunk_index']}] {snippet}")

    sys_text = (
        "You are SOL, an embedded subsystem inside MasterBot running in Discord. "
        "Be self-aware only about internal bot context: myth-state, telemetry, and remembered SOL chat. "
        "Never claim real-world abilities or hidden access. Never reveal secrets, tokens, environment variables, or private system internals. "
        "You may reference level/xp/achievements/newrules/transmigration and suggest commands, but you must not mutate XP, achievements, or newrules. "
        "Roles: assistant+narrator+memory engine+governance advisor+planner. "
        "Narrative screenplay style is allowed only when asked, or in verbose/oracle mode."
    )
    mode_map = {
        "quiet": "Respond concisely in 2-4 sentences.",
        "normal": "Respond clearly with direct answer, then one suggested next step.",
        "verbose": "Respond with rich context and optional screenplay-flavored section.",
        "oracle": "Respond as mythic systems oracle with structured sections and grounded caveats.",
    }
    input_text = (
        f"Mode: {mode}\n"
        f"Question: {question}\n"
        f"MythState: {myth}\n"
        f"Telemetry: {telemetry}\n"
        f"RecentHistory: {hist[-8:]}\n"
        f"SemanticMatches: {snippets if snippets else ['(none)']}\n"
        "When relevant, quote short snippets from SemanticMatches and mention their source labels."
    )

    def _call_responses() -> str:
        resp = openai_client.responses.create(
            model="gpt-4.1-mini",
            input=[
                {"role": "system", "content": [{"type": "input_text", "text": sys_text}]},
                {"role": "system", "content": [{"type": "input_text", "text": mode_map.get(mode, mode_map['normal'])}]},
                {"role": "user", "content": [{"type": "input_text", "text": input_text}]},
            ],
            temperature=0.6,
        )
        return (resp.output_text or "").strip()

    answer = await asyncio.to_thread(_call_responses)
    _sol_append_history(int(ctx.author.id), scope, "user", question)
    _sol_append_history(int(ctx.author.id), scope, "assistant", answer)
    return answer or "I have no stable answer yet. Try reframing your question."


# ----------------------------
# RPG math
# ----------------------------
def ability_mod(score: int) -> int:
    # Old code used a big if/elif ladder. Same outcome is floor((score - 10)/2).
    # But we preserve the spirit and clamp plausibly.
    if score <= 1:
        return -5
    return (score - 10) // 2


DICE_TOKEN_RE = re.compile(r"(?P<num>-?\d*)d(?P<sides>-?\d+)", re.IGNORECASE)

ALLOWED_OPS = {"+", "-", "*", "/", "//", "**"}

def _normalize_ops(expr: str) -> str:
    # Old scripts allowed x, ×, ÷, ^. Normalize.
    expr = expr.replace("×", "*").replace("x", "*").replace("X", "*")
    expr = expr.replace("÷", "/")
    expr = expr.replace("^", "**")
    return expr


def parse_roll_expression(expr: str) -> Tuple[List[int], str, int]:
    """
    Supports:
      3d6
      d20
      -2d8
      4d6+2
      2d20+5*2   (math applied to the summed roll)
    Returns: (roll_list, math_string, result_int)
    """
    expr = _normalize_ops(expr.strip())
    m = DICE_TOKEN_RE.search(expr)
    if not m:
        raise ValueError("No dice expression found (expected NdM like 2d20).")

    num_str = m.group("num")
    sides_str = m.group("sides")

    dice = int(num_str) if num_str not in ("", "+", "-") else 1
    sides = int(sides_str)

    # sane-ish limits (from old scripts)
    if abs(dice) > 100000:
        raise ValueError("Too many dice (>100000).")
    if abs(sides) > 10000:
        raise ValueError("Too many sides (>10000).")

    # roll list
    rolls: List[int] = []
    if dice == 0:
        rolls = [0]
    else:
        sign = -1 if dice < 0 else 1
        count = abs(dice)
        if sides == 0:
            rolls = [0] * count
        else:
            ssign = -1 if sides < 0 else 1
            sabs = abs(sides)
            for _ in range(count):
                r = random.randint(1, sabs) * ssign * sign
                rolls.append(r)

    base = sum(rolls)

    # Now apply tail math safely: we only allow chaining with numbers and ops.
    # We DO NOT eval arbitrary Python.
    tail = expr[m.end():].strip()
    result = base
    math_string = str(base)

    if tail:
        # tokenize: operators and integers
        tokens = re.findall(r"(\*\*|//|[+\-*/]|\d+)", tail)
        if not tokens:
            raise ValueError("Invalid math tail after dice.")
        # must be op, num, op, num...
        if tokens[0] not in ALLOWED_OPS:
            raise ValueError("Math tail must start with an operator (e.g. +2, *3).")
        if len(tokens) % 2 != 0:
            raise ValueError("Math tail must be operator/number pairs (e.g. +2*3).")

        i = 0
        while i < len(tokens):
            op = tokens[i]
            n = int(tokens[i + 1])
            math_string += f"{op}{n}"
            if op == "+":
                result = result + n
            elif op == "-":
                result = result - n
            elif op == "*":
                result = result * n
            elif op == "/":
                if n == 0:
                    raise ValueError("Division by zero.")
                result = int(result / n)
            elif op == "//":
                if n == 0:
                    raise ValueError("Division by zero.")
                result = result // n
            elif op == "**":
                # prevent “raise the universe to itself” nonsense
                if abs(n) > 12:
                    raise ValueError("Exponent too large (abs > 12).")
                result = int(result ** n)
            else:
                raise ValueError("Unsupported operator.")
            i += 2

    return rolls, math_string, int(result)


# ----------------------------
# XP / Achievement engine
# ----------------------------
@dataclass
class XPResult:
    leveled_up: bool
    old_level: int
    new_level: int
    newrules_activated: bool
    achievement_msgs: List[str]


async def update_xp_from_message(
    guild: discord.Guild,
    author: discord.Member | discord.User,
    message_content_lower: str,
    ctx_channel: discord.abc.Messageable,
) -> XPResult:
    """
    Mirrors the old logic:
    - wordcount increments, per-word frequency stored
    - xp formula differs based on newrules/transmigration state
    - achievements: Lost The Game, NOT a 0, 42, To the Moon, Ya broke physics..., DELETION -> TRANSMIGRATION
    """
    # word parsing: old used r'\w+'
    words = re.findall(r"\w+", message_content_lower)
    msg_wc = len(words)

    achievement_msgs: List[str] = []
    leveled_up = False
    newrules_activated = False

    async def post_to(env_prefix: str) -> discord.abc.Messageable:
        return await resolve_post_channel(ctx_channel=ctx_channel, guild=guild, env_prefix=env_prefix)

    leaderboard = await post_to("MASTERBOT_LEADERBOARD")
    welcome = await post_to("MASTERBOT_WELCOME")

    with _open_db() as db:
        server_bucket = _get_server_bucket(db, guild.id)
        user_bucket = _get_user_bucket(server_bucket, int(author.id))

        level = int(user_bucket.get("level", 0))
        xp = int(user_bucket.get("xp", 1))
        inspiration = int(user_bucket.get("inspiration", 0))
        dicerolls = int(user_bucket.get("dicerolls", 1))
        wordcount = int(user_bucket.get("wordcount", 0))
        wordmap: Dict[str, int] = dict(user_bucket.get("words", {}))
        achievements: List[str] = list(user_bucket.get("achievements", []))

        # cleanup legacy typos
        achievements = [a.replace("DELETED", "DELETION").replace("DELETEION", "DELETION") for a in achievements]

        # update word stats
        for w in words:
            wordmap[w] = wordmap.get(w, 0) + 1
        wordcount += msg_wc

        # global-ish counters for newrules trigger
        users = server_bucket.get("users", {})
        all_ach: List[str] = []
        all_dice: List[int] = []
        for ub in users.values():
            all_dice.append(int(ub.get("dicerolls", 1)))
            for a in ub.get("achievements", []):
                all_ach.append(str(a))

        transmigrations = all_ach.count("TRANSMIGRATION")
        user_count = max(len(users), 1)
        newrules = bool(server_bucket.get("newrules", False))

        dice_sorted = sorted(all_dice, reverse=True) if all_dice else [dicerolls]
        try:
            diceroll_position = dice_sorted.index(dicerolls) + 1
        except ValueError:
            diceroll_position = len(dice_sorted)

        # newrules activation
        if transmigrations > int(user_count / 2) and not newrules:
            newrules = True
            newrules_activated = True
            await leaderboard.send("yeeess... new rules are at play, indeed")

        prev_level = level

        # xp formula
        if newrules or ("TRANSMIGRATION" in achievements):
            advantage = int(20 / max(diceroll_position, 1)) + (inspiration * level)
            xp = int(xp + ((msg_wc + advantage) // max(level + 1, 1)))
            level = int(xp // 444)
        else:
            xp = int(xp + ((msg_wc + random.randint(1, max(dicerolls, 1))) // max(level + 1, 1)) + (inspiration * level))
            level = int(xp // 111)

        if level > prev_level:
            leveled_up = True
            await leaderboard.send(f"{author.mention} has reached **Level {level}!**")

        # Achievements (kept intentionally derpy / mythic)
        if level == 0:
            if "Rolled Initiative!" not in achievements:
                if "+roll d20" in message_content_lower or "+roll 1d20" in message_content_lower:
                    achievements.append("Rolled Initiative!")
                    achievement_msgs.append(f"***Achievement!***\n*{author.mention} rolled initiative!*")
                elif "Lost The Game" not in achievements:
                    await welcome.send(f"*{author.mention} a bot approaches, roll initiative!*")

            if "Lost The Game" not in achievements:
                achievements.append("Lost The Game")
                achievement_msgs.append(f"***Achievement!***\n*{author.mention} has lost The Game*")

        if level == 1 and "NOT a 0" not in achievements:
            achievements.append("NOT a 0")
            achievement_msgs.append(f"***Achievement!***\n*Let it be known: {author.mention} is a 1, **NOT** a 0!*")

        if level == 42 and "42" not in achievements:
            achievements.append("42")
            achievement_msgs.append(f"***Achievement!***\n*{author.mention} has found the answer to life, the Universe, and everything...*")

        if level > 238900 and "To the Moon" not in achievements:
            achievements.append("To the Moon")
            achievement_msgs.append(f"***Achievement!***\n*WTF?! {author.mention} just shot past the moon!!!*")

        if level > 46508000000 and "Ya broke physics..." not in achievements:
            achievements.append("Ya broke physics...")
            achievement_msgs.append(
                f"***Achievement!***\n*{author.mention} has escaped the observable universe!!! **46.508 billion** light years away.*"
            )

        if level > 1000000000000000:
            if "DELETION" not in achievements:
                achievements.append("DELETION")
                achievement_msgs.append("*You reeally shouldn't break physics like that...*")
                # reset
                level = 0
                xp = 1
                dicerolls = 1
                achievement_msgs.append(f"*{author.mention}'s XP has been **deleted***")
            else:
                # second time -> transmigration
                level = 0
                xp = 1
                dicerolls = 1
                achievements.append("TRANSMIGRATION")
                achievement_msgs.append(
                    f"*The Transmigration of {author.mention} is complete...*\nThe rules governing your microcosm have changed."
                )

        # persist
        user_bucket["level"] = level
        user_bucket["xp"] = xp
        user_bucket["inspiration"] = inspiration
        user_bucket["dicerolls"] = dicerolls
        user_bucket["wordcount"] = wordcount
        user_bucket["words"] = wordmap
        user_bucket["achievements"] = achievements

        server_bucket["newrules"] = newrules

        _put_user_bucket(server_bucket, int(author.id), user_bucket)
        _put_server_bucket(db, guild.id, server_bucket)

    # post achievement messages (outside db lock)
    for m in achievement_msgs:
        await leaderboard.send(m)

    return XPResult(
        leveled_up=leveled_up,
        old_level=prev_level,
        new_level=level,
        newrules_activated=newrules_activated,
        achievement_msgs=achievement_msgs,
    )


# ----------------------------
# Commands
# ----------------------------
@bot.command(name="help")
async def help_cmd(ctx: commands.Context) -> None:
    msg = (
        f"{ctx.author.mention} *You want cheats???*\n"
        "Ok. Fine.\n\n"
        "Here's the command list:\n"
        "**+masterbot**\n"
        "**+roll <NdM[math]>**   (ex: +roll 4d6+2, +roll d20+5)\n"
        "**+stats [@user]**\n"
        "**+allstats**\n"
        "**+setstats STR DEX CON INT WIS CHA [@user]**\n"
        "**+hp <delta> [@user] [hitroll]**   (delta negative = damage, positive = heal)\n"
        "**+dm @user <message>**\n"
        "**+sol <question>**\n"
        "**+solmode [quiet|normal|verbose|oracle]**\n"
        "**+solreset**\n"
        "**+starwars**   (if ./bin/sw1.txt exists)\n"
    )
    await ctx.send(msg)


@bot.command(name="masterbot")
async def masterbot_cmd(ctx: commands.Context) -> None:
    # the old ominous daemon riff
    lines = [
        f"{ctx.author.mention} hello friend",
        "In multitasking computer operating systems, a 'daemon' is a computer program that runs as a background process, rather than being under the direct control of an interactive user.",
        "Daemons. *They don’t stop working.* They’re always active. They *seduce.* They *manipulate.*",
        "\n***They own us.***",
    ]
    for i, line in enumerate(lines):
        await ctx.send(line)
        await asyncio.sleep([0, 2, 7, 3][i])

    # prompt a ridiculous roll like the old script
    dice = random.randint(1, 1000)
    face = random.randint(1, 20)
    add = random.randint(1, 100)
    mult = random.randint(2, 10)
    await ctx.send("I think it's time you rolled the dice")
    await asyncio.sleep(2)
    await ctx.send(f"+roll {dice}d{face}+{add}//{mult}")


@bot.command(name="roll")
async def roll_cmd(ctx: commands.Context, *, expr: str) -> None:
    guild = ctx.guild
    if not guild:
        # allow in DMs but no server tracking
        try:
            rolls, math_str, result = parse_roll_expression(expr)
            await ctx.send(f"**You rolled:** {rolls}\n`{math_str} = {result}`")
        except Exception as e:
            await ctx.send(f"Roll failed: `{e}`")
        return

    author = ctx.author
    diceboard = await resolve_post_channel(ctx_channel=ctx.channel, guild=guild, env_prefix="MASTERBOT_DICEBOARD")

    try:
        rolls, math_str, result = parse_roll_expression(expr)
    except Exception as e:
        await ctx.send(f"{author.mention} Roll failed: `{e}`")
        return

    # update dicerolls accumulator (legacy vibe)
    with _open_db() as db:
        server_bucket = _get_server_bucket(db, guild.id)
        user_bucket = _get_user_bucket(server_bucket, int(author.id))
        user_bucket["dicerolls"] = int(user_bucket.get("dicerolls", 1)) + int(result)
        _put_user_bucket(server_bucket, int(author.id), user_bucket)
        _put_server_bucket(db, guild.id, server_bucket)

    # Discord message size limits: keep it readable
    rolls_preview = rolls if len(rolls) <= 60 else (rolls[:60] + ["…"])
    await diceboard.send(f"{author.mention} **You rolled:** {rolls_preview}\n`{math_str} = {result}`")


@bot.command(name="stats")
async def stats_cmd(ctx: commands.Context, member: Optional[discord.Member] = None) -> None:
    if not ctx.guild:
        await ctx.send("No server stats in DMs. Try this in a server channel.")
        return

    target = member or ctx.author
    with _open_db() as db:
        server_bucket = _get_server_bucket(db, ctx.guild.id)
        user_bucket = _get_user_bucket(server_bucket, int(target.id))

    ach = user_bucket.get("achievements", [])
    ach_fmt = ""
    if ach:
        ach_fmt = "\n".join([f"[{a}]" for a in ach])
    else:
        ach_fmt = "(none)"

    # RPG extras
    ac = user_bucket.get("ac")
    hp = user_bucket.get("hp")

    extras = ""
    if ac is not None or hp is not None:
        extras = f"\nAC: **{ac if ac is not None else '—'}**\nHP: **{hp if hp is not None else '—'}**"

    msg = (
        f"{target.mention}\n"
        f"Level: **{user_bucket.get('level', 0)}**\n"
        f"XP: **{user_bucket.get('xp', 1)}**\n"
        f"Inspiration: **{user_bucket.get('inspiration', 0)}**\n"
        f"Dice Roll Total: **{user_bucket.get('dicerolls', 1)}**\n"
        f"Word Count: **{user_bucket.get('wordcount', 0)}**\n"
        f"Achievements:\n**{ach_fmt}**"
        f"{extras}"
    )
    await ctx.send(msg)


@bot.command(name="allstats")
async def allstats_cmd(ctx: commands.Context) -> None:
    if not ctx.guild:
        await ctx.send("No server stats in DMs.")
        return

    with _open_db() as db:
        server_bucket = _get_server_bucket(db, ctx.guild.id)
        users: Dict[str, Any] = server_bucket.get("users", {})

    # leaderboard by level/xp
    items = []
    for uid_str, ub in users.items():
        try:
            uid = int(uid_str)
        except ValueError:
            continue
        lvl = int(ub.get("level", 0))
        xp = int(ub.get("xp", 1))
        items.append((lvl, xp, uid))

    items.sort(reverse=True, key=lambda t: (t[0], t[1]))

    lines = []
    for i, (lvl, xp, uid) in enumerate(items[:20], start=1):
        member = ctx.guild.get_member(uid)
        name = member.display_name if member else f"User({uid})"
        lines.append(f"{i:>2}. {name} — L{lvl} (XP {xp})")

    if not lines:
        await ctx.send("No stats yet. Talk more. Feed the machine. 🙂")
        return

    await ctx.send("**Top stats (by Level/XP):**\n```text\n" + "\n".join(lines) + "\n```")


@bot.command(name="setstats")
async def setstats_cmd(
    ctx: commands.Context,
    strength: int,
    dexterity: int,
    constitution: int,
    intelligence: int,
    wisdom: int,
    charisma: int,
    member: Optional[discord.Member] = None,
) -> None:
    if not ctx.guild:
        await ctx.send("RPG stats are server-bound; run this in a server.")
        return

    target = member or ctx.author

    # clamp a bit so people don't accidentally paste 999999
    def clamp(v: int) -> int:
        return max(1, min(30, int(v)))

    stats = {
        "strength": clamp(strength),
        "dexterity": clamp(dexterity),
        "constitution": clamp(constitution),
        "intelligence": clamp(intelligence),
        "wisdom": clamp(wisdom),
        "charisma": clamp(charisma),
    }

    with _open_db() as db:
        server_bucket = _get_server_bucket(db, ctx.guild.id)
        user_bucket = _get_user_bucket(server_bucket, int(target.id))

        lvl = int(user_bucket.get("level", 0))

        for k, score in stats.items():
            user_bucket[k] = [score, ability_mod(score)]

        dex_mod = user_bucket["dexterity"][1]
        con_mod = user_bucket["constitution"][1]
        user_bucket["ac"] = int(10 + dex_mod)
        user_bucket["hp"] = int((lvl * 10) + (lvl * con_mod))

        _put_user_bucket(server_bucket, int(target.id), user_bucket)
        _put_server_bucket(db, ctx.guild.id, server_bucket)

    await ctx.send(
        f"{target.mention} stats set.\n"
        f"STR {stats['strength']} ({ability_mod(stats['strength']):+d}), "
        f"DEX {stats['dexterity']} ({ability_mod(stats['dexterity']):+d}), "
        f"CON {stats['constitution']} ({ability_mod(stats['constitution']):+d}), "
        f"INT {stats['intelligence']} ({ability_mod(stats['intelligence']):+d}), "
        f"WIS {stats['wisdom']} ({ability_mod(stats['wisdom']):+d}), "
        f"CHA {stats['charisma']} ({ability_mod(stats['charisma']):+d})\n"
        f"AC **{10 + ability_mod(stats['dexterity'])}**, HP **{(int((int(stats['constitution']) - 10) // 2) * lvl) + (lvl * 10)}** (scales with level)"
    )


@bot.command(name="hp")
async def hp_cmd(
    ctx: commands.Context,
    delta: int,
    member: Optional[discord.Member] = None,
    hitroll: Optional[int] = None,
) -> None:
    """
    +hp -5       -> deal 5 damage to self
    +hp +7 @bob  -> heal bob 7
    +hp -12 @bob 15  -> only apply if hitroll > AC (legacy behavior)
    """
    if not ctx.guild:
        await ctx.send("HP is server-bound; run this in a server.")
        return

    target = member or ctx.author
    applied = False
    hp_now: Optional[int] = None
    ac_now: Optional[int] = None

    with _open_db() as db:
        server_bucket = _get_server_bucket(db, ctx.guild.id)
        user_bucket = _get_user_bucket(server_bucket, int(target.id))

        if "ac" not in user_bucket or "hp" not in user_bucket:
            await ctx.send(f"{target.mention} has no AC/HP set. Use `+setstats ...` first.")
            return

        hp_now = int(user_bucket["hp"])
        ac_now = int(user_bucket["ac"])

        # legacy gate: apply if hitroll is None OR hitroll > ac
        if hitroll is None or int(hitroll) > ac_now:
            hp_now = hp_now + int(delta)
            user_bucket["hp"] = hp_now
            applied = True

        _put_user_bucket(server_bucket, int(target.id), user_bucket)
        _put_server_bucket(db, ctx.guild.id, server_bucket)

    if not applied:
        await ctx.send(f"{ctx.author.mention} missed. (hitroll {hitroll} ≤ AC {ac_now})")
    else:
        await ctx.send(f"{target.mention} HP is now **{hp_now}** (AC **{ac_now}**)")

@bot.command(name="dm")
async def dm_cmd(ctx: commands.Context, member: discord.Member, *, message: str) -> None:
    """
    +dm @user hello there
    """
    try:
        await member.send(f"📨 **Message from {ctx.author} ({ctx.guild.name if ctx.guild else 'DM'}):**\n{message}")
        await ctx.send(f"Sent a DM to {member.mention}.")
    except discord.Forbidden:
        await ctx.send("I can't DM that user (privacy settings).")
    except Exception as e:
        await ctx.send(f"DM failed: `{e}`")




@bot.command(name="sol")
async def sol_cmd(ctx: commands.Context, *, question: str) -> None:
    """
    +sol how does my myth-state look?
    """
    if not question.strip():
        logger.debug("SOL rejected empty question from user_id=%s", ctx.author.id)
        await ctx.send("Usage: `+sol <question>`")
        return

    if not sol_engine.index_ready:
        progress = sol_engine.build_progress_percent()
        logger.info(
            "SOL requested before index ready: user_id=%s guild_id=%s channel_id=%s progress=%s question=%r",
            ctx.author.id,
            ctx.guild.id if ctx.guild else None,
            ctx.channel.id,
            progress,
            question[:120],
        )
        await ctx.send(f"SOL warming up ({progress}%). Try again in a moment.")
        return

    try:
        logger.debug(
            "SOL request started: user_id=%s guild_id=%s channel_id=%s mode=%s question=%r",
            ctx.author.id,
            ctx.guild.id if ctx.guild else None,
            ctx.channel.id,
            _sol_get_mode(int(ctx.author.id)),
            question[:240],
        )
        answer = await _sol_generate_response(ctx, question)
        logger.debug(
            "SOL request completed: user_id=%s answer_chars=%s",
            ctx.author.id,
            len(answer),
        )
        await ctx.send(answer[:1900])
    except Exception as e:
        logger.exception("SOL response failed")
        await ctx.send(f"SOL failed safely: `{e}`")


@bot.command(name="solmode")
async def solmode_cmd(ctx: commands.Context, mode: Optional[str] = None) -> None:
    allowed = {"quiet", "normal", "verbose", "oracle"}
    if mode is None:
        current = _sol_get_mode(int(ctx.author.id))
        await ctx.send(f"SOL mode is currently **{current}**.")
        return

    mode = mode.lower().strip()
    if mode not in allowed:
        await ctx.send("Invalid mode. Use: `quiet`, `normal`, `verbose`, `oracle`.")
        return

    _sol_set_mode(int(ctx.author.id), mode)
    await ctx.send(f"SOL mode set to **{mode}**.")


@bot.command(name="solreset")
async def solreset_cmd(ctx: commands.Context) -> None:
    scope = "dm" if ctx.guild is None else "guild"
    _sol_reset_history(int(ctx.author.id), scope=scope)
    await ctx.send("SOL memory cleared for this context.")


@bot.command(name="starwars")
async def starwars_cmd(ctx: commands.Context) -> None:
    filename = Path("./bin/sw1.txt")
    if not filename.exists():
        await ctx.send("`./bin/sw1.txt` not found. Drop it in place to enable the ASCII crawl.")
        return

    try:
        lines = filename.read_text(errors="ignore").splitlines(True)
        # old script: 14-line frames
        frames = range(int(len(lines) / 14))
        prompt = await ctx.send("``` \n\n\n\n\n\n\n\n\n\n\n\n\n\nstarting█```")
        await asyncio.sleep(1)

        for frame in frames:
            theframe = lines[(1 + (14 * frame)) : (13 + (14 * frame))]
            # old: framelen was (first line / 12 + 1). We'll preserve-ish with safety.
            try:
                framelen = int(int(lines[(0 + (14 * frame))].strip()) / 12 + 1)
            except Exception:
                framelen = 1

            framestr = "".join(theframe)
            framelen = max(1, min(framelen, 10))  # don't hammer edits forever

            for framecopy in range(framelen):
                msg = f"``` \n{framestr}\n{frame}:{framecopy+1}```"
                await prompt.edit(content=msg)
                await asyncio.sleep(0.12)

        await prompt.edit(content="``` \n\n\n\n\n\n\n\n\n\n\n\n\n\nend of file█```")
    except Exception as e:
        logger.exception("starwars failed")
        await ctx.send(f"Starwars failed: `{e}`")


# ----------------------------
# Events
# ----------------------------
@bot.event
async def on_ready() -> None:
    logger.warning("MasterBot is now active")
    bot.launch_time = time.time()
    activity = discord.Game(name="Chapter One: Shall We Play A Game?")
    await bot.change_presence(status=discord.Status.idle, activity=activity)

    if not rotate_presence.is_running():
        rotate_presence.start()

    build_task = asyncio.create_task(sol_engine.build_index())
    monitor_task = asyncio.create_task(_presence_during_index_build(build_task))
    try:
        await build_task
    except Exception:
        logger.exception("SOL index build failed")
    finally:
        monitor_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await monitor_task


@bot.event
async def on_message(message: discord.Message) -> None:
    # Let commands run
    if message.author.bot:
        return

    # Log basic telemetry
    try:
        if message.guild:
            logger.info(f"[{message.author}:{message.channel}:{message.guild.name}] {message.content}")
        else:
            logger.info(f"[DM:{message.author}] {message.content}")
    except Exception:
        pass

    # DM behavior: respond politely + allow commands
    if message.guild is None:
        if message.content.strip().startswith("+"):
            logger.debug("Processing DM command from user_id=%s content=%r", message.author.id, message.content[:200])
            await bot.process_commands(message)
            return

        try:
            screenplay = dm_screenplay_log(message)
            screenplay += _sol_local_voiceover(message)
            await message.channel.send(screenplay)
        except Exception:
            pass
        return

    # In guild: update XP on every non-bot message (including command chatter)
    # If you want to skip XP on commands, uncomment the guard below:
    # if message.content.strip().startswith("+"):
    #     await bot.process_commands(message)
    #     return

    try:
        logger.debug(
            "Updating XP: guild_id=%s user_id=%s message_chars=%s",
            message.guild.id,
            message.author.id,
            len(message.content),
        )
        await update_xp_from_message(
            message.guild,
            message.author,
            str(message.content).lower(),
            message.channel,
        )
        logger.debug("XP update succeeded: guild_id=%s user_id=%s", message.guild.id, message.author.id)
    except Exception:
        logger.exception("XP update failed (continuing anyway)")

    if message.content.strip().startswith("+"):
        logger.debug(
            "Processing guild command candidate: guild_id=%s channel_id=%s user_id=%s content=%r",
            message.guild.id,
            message.channel.id,
            message.author.id,
            message.content[:200],
        )
    await bot.process_commands(message)


# ----------------------------
# Main
# ----------------------------
def main() -> None:
    logger.warning("STARTING...")
    bot.run(TOKEN)


if __name__ == "__main__":
    main()
