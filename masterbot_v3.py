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
from typing import Any, Dict, List, Optional, Tuple

import discord
from discord.ext import commands, tasks
from openai import OpenAI

import sol

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
console_handler.setLevel(logging.INFO)
logger.addHandler(console_handler)

# ----------------------------
# Storage
# ----------------------------
DB_PATH = str(LOG_DIR / "masterbot.db")

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

openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

SOL_EMBED_CACHE_PATH = LOG_DIR / "sol_embeddings_v2.pkl"
SOL_KNOWLEDGE_EXTENSIONS = {".py", ".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".ini"}
SOL_MAX_FILE_BYTES = 180_000
SOL_MAX_CHUNKS_PER_FILE = 6
SOL_TOP_K_MATCHES = 6

sol_embedding_index: Dict[str, Tuple[int, List[float], float, str, str]] = {}
sol_embeddings_loaded = False
sol_embedding_lock = asyncio.Lock()

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


def _cosine_similarity(vec_a: List[float], vec_b: List[float]) -> float:
    if not vec_a or not vec_b or len(vec_a) != len(vec_b):
        return -1.0
    dot = sum(a * b for a, b in zip(vec_a, vec_b))
    norm_a = math.sqrt(sum(a * a for a in vec_a))
    norm_b = math.sqrt(sum(b * b for b in vec_b))
    if norm_a == 0 or norm_b == 0:
        return -1.0
    return dot / (norm_a * norm_b)


def _build_sol_embedding_index_sync() -> Dict[str, Tuple[int, List[float], float, str, str]]:
    index: Dict[str, Tuple[int, List[float], float, str, str]] = {}

    try:
        cached = {}
        if SOL_EMBED_CACHE_PATH.exists():
            with SOL_EMBED_CACHE_PATH.open("rb") as fh:
                cached = pickle.load(fh)
    except Exception:
        cached = {}

    files = sol.get_all_files(
        exclude_dirs=["./.git", "./logs", "./venv", "./.venv", "./node_modules"],
        extensions=list(SOL_KNOWLEDGE_EXTENSIONS),
        recursive=True,
        verbose=False,
    )

    for filepath in files:
        path = Path(filepath)
        if not path.exists() or path.suffix.lower() not in SOL_KNOWLEDGE_EXTENSIONS:
            continue
        try:
            if path.stat().st_size > SOL_MAX_FILE_BYTES:
                continue
            content = path.read_text(encoding="utf-8", errors="ignore")
            mtime = path.stat().st_mtime
        except Exception:
            continue

        chunks = sol.chunk_text(content, chunk_size=1000)[:SOL_MAX_CHUNKS_PER_FILE]
        for chunk_index, chunk in enumerate(chunks):
            cleaned_chunk = chunk.strip()
            if not cleaned_chunk:
                continue

            key = f"{filepath}:{chunk_index}"
            cached_entry = cached.get(key)
            if cached_entry and isinstance(cached_entry, tuple) and len(cached_entry) == 5 and cached_entry[2] == mtime:
                index[key] = cached_entry
                continue

            emb_response = openai_client.embeddings.create(
                model="text-embedding-3-small",
                input=cleaned_chunk,
            )
            embedding = emb_response.data[0].embedding
            index[key] = (chunk_index, embedding, mtime, cleaned_chunk, filepath)

    try:
        with SOL_EMBED_CACHE_PATH.open("wb") as fh:
            pickle.dump(index, fh)
    except Exception:
        logger.exception("Failed to persist SOL embedding cache")

    return index


def _semantic_context_sync(question: str, top_k: int = SOL_TOP_K_MATCHES) -> str:
    if not sol_embedding_index:
        return "(No SOL knowledge base embeddings are loaded yet.)"

    query_response = openai_client.embeddings.create(
        model="text-embedding-3-small",
        input=question,
    )
    query_embedding = query_response.data[0].embedding

    scored_chunks = []
    for chunk_index, chunk_embedding, _mtime, chunk_text, filepath in sol_embedding_index.values():
        score = _cosine_similarity(query_embedding, chunk_embedding)
        scored_chunks.append((score, filepath, chunk_index, chunk_text))

    scored_chunks.sort(key=lambda item: item[0], reverse=True)
    selected = scored_chunks[:top_k]

    if not selected:
        return "(No relevant SOL context matches found.)"

    parts = []
    for score, filepath, chunk_index, chunk_text in selected:
        parts.append(f"[{Path(filepath).name}#{chunk_index} score={score:.3f}] {chunk_text[:900]}")
    return "\n\n".join(parts)


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
        "**+sol <question>**   (SOL conversational mode with semantic context)\n"
        "**+starwars**   (if ./bin/sw1.txt exists)\n"
    )
    await ctx.send(msg)


@bot.command(name="sol")
async def sol_cmd(ctx: commands.Context, *, question: str) -> None:
    prompt = (question or "").strip()
    if not prompt:
        await ctx.send("Usage: `+sol <question>`")
        return

    if not os.getenv("OPENAI_API_KEY", "").strip():
        await ctx.send("SOL is unavailable right now: OPENAI_API_KEY is not configured.")
        return

    global sol_embedding_index, sol_embeddings_loaded

    async with sol_embedding_lock:
        if not sol_embeddings_loaded:
            try:
                sol_embedding_index = await asyncio.to_thread(_build_sol_embedding_index_sync)
                sol_embeddings_loaded = True
            except Exception:
                logger.exception("SOL embedding preload failed")
                await ctx.send("SOL failed to initialize its knowledge base cache.")
                return

    try:
        semantic_context = await asyncio.to_thread(_semantic_context_sync, prompt)
    except Exception:
        logger.exception("SOL semantic search failed")
        semantic_context = "(Semantic context lookup failed; proceed using general model knowledge.)"

    newrules = False
    level = 0
    xp = 1
    achievements: List[str] = []
    if ctx.guild:
        with _open_db() as db:
            server_bucket = _get_server_bucket(db, ctx.guild.id)
            user_bucket = _get_user_bucket(server_bucket, int(ctx.author.id))
            newrules = bool(server_bucket.get("newrules", False))
            level = int(user_bucket.get("level", 0))
            xp = int(user_bucket.get("xp", 1))
            achievements = list(user_bucket.get("achievements", []))

    transmigration = "TRANSMIGRATION" in achievements
    system_msg = (
        "You are SOL, an in-world conversational intelligence embedded in MasterBot. "
        "Speak clearly, be helpful, and preserve a slightly mythic/arcane tone when it fits. "
        "If context is missing, say so briefly instead of fabricating specifics.\n\n"
        f"Server state: newrules={newrules}.\n"
        f"User state: level={level}, xp={xp}, achievements={achievements}.\n"
        f"TRANSMIGRATION unlocked={transmigration}.\n\n"
        "Knowledge base excerpts retrieved via embeddings:\n"
        f"{semantic_context}\n"
    )

    try:
        response = await asyncio.to_thread(
            openai_client.chat.completions.create,
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": prompt},
            ],
            max_tokens=500,
        )
        answer = response.choices[0].message.content if response.choices else None
        if not answer:
            answer = "I couldn't produce a reply just now. Please try again."
        await ctx.send(answer)
    except Exception as e:
        logger.exception("SOL completion failed")
        await ctx.send(f"SOL encountered an API error: `{e}`")


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

    global sol_embedding_index, sol_embeddings_loaded
    async with sol_embedding_lock:
        if not sol_embeddings_loaded:
            try:
                sol_embedding_index = await asyncio.to_thread(_build_sol_embedding_index_sync)
                sol_embeddings_loaded = True
                logger.info("SOL embeddings preloaded: %s chunks", len(sol_embedding_index))
            except Exception:
                logger.exception("SOL embeddings failed to preload on startup")

    if not rotate_presence.is_running():
        rotate_presence.start()


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
            await bot.process_commands(message)
            return

        try:
            await message.channel.send(dm_screenplay_log(message))
        except Exception:
            pass
        return

    # In guild: update XP on every non-bot message (including command chatter)
    # If you want to skip XP on commands, uncomment the guard below:
    # if message.content.strip().startswith("+"):
    #     await bot.process_commands(message)
    #     return

    try:
        await update_xp_from_message(
            message.guild,
            message.author,
            str(message.content).lower(),
            message.channel,
        )
    except Exception:
        logger.exception("XP update failed (continuing anyway)")

    await bot.process_commands(message)


# ----------------------------
# Main
# ----------------------------
def main() -> None:
    logger.warning("STARTING...")
    bot.run(TOKEN)


if __name__ == "__main__":
    main()
