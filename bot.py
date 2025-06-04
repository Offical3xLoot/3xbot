import os
import re
import time
import json
import asyncio
import atexit
import logging
from typing import Optional, Set, Dict, List

from dotenv import load_dotenv
load_dotenv()  # loads DISCORD_BOT_TOKEN from Replit secrets or local .env

DISCORD_BOT_TOKEN: str = os.getenv("DISCORD_BOT_TOKEN")
GUILD_ID: int = 1257773619405262860

SOURCE_CHANNEL_IDS: Set[int] = {
    1257773621410267219,
    1350591798636056636,
    1295756759771643966,
}
DESTINATION_CHANNEL_ID: int = 1378835460171763712
LOOKUP_CHANNEL_ID: int = 1257773620768411735

CACHE_FILE = "cache.json"
IGNORE_FILE = "ignore.txt"
CACHE_EXPIRY_SECONDS: int = 3600  # 1 hour
LOW_GAMERSCORE_THRESHOLD: int = 2000

# ======== LOGGING SETUP ========
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("GamerscoreBot")
logging.getLogger("aiohttp.client").setLevel(logging.WARNING)

# ======== INTENTS & BOT SETUP ========
import aiohttp
from bs4 import BeautifulSoup

import discord
from discord.ext import commands, tasks
from discord import app_commands

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ======== GLOBAL STATE ========
checked_gamertags: Set[str] = set()
gamerscore_cache: Dict[str, tuple[int, float]] = {}  # tag → (score, timestamp)
failure_backoff: Dict[str, int] = {}  # tag → consecutive failure count
http_session: Optional[aiohttp.ClientSession] = None

# A global lock so only one HTTP request happens at a time
rate_limit_lock = asyncio.Lock()


# ======== IGNORE LIST LOADING & UPDATING ========
def load_ignore_list() -> Set[str]:
    """
    Read IGNORE_FILE, strip whitespace, and return a set of
    normalized Gamertags to skip checking.
    """
    ignore_set: Set[str] = set()
    if os.path.exists(IGNORE_FILE):
        with open(IGNORE_FILE, "r", encoding="utf-8") as f:
            for line in f:
                tag = line.strip()
                if tag:
                    ignore_set.add(tag.lower())
    return ignore_set

def append_ignore(tag: str) -> None:
    """
    Append a Gamertag to IGNORE_FILE (one per line) and to the in-memory set.
    """
    normalized = tag.lower().strip()
    if normalized in ignore_set:
        return
    try:
        with open(IGNORE_FILE, "a", encoding="utf-8") as f:
            f.write(f"{tag.strip()}\n")
        ignore_set.add(normalized)
        logger.info(f"Appended '{tag}' to {IGNORE_FILE}.")
    except Exception as e:
        logger.error(f"Failed to append '{tag}' to {IGNORE_FILE}: {e}", exc_info=True)

# Load ignore list on startup
ignore_set = load_ignore_list()


# ======== PERSISTENT CACHE FUNCTIONS ========
def load_cache() -> None:
    global gamerscore_cache, failure_backoff
    if os.path.exists(CACHE_FILE):
        try:
            data = json.load(open(CACHE_FILE, "r"))
            gamerscore_cache = {
                k: (v["score"], v["timestamp"]) for k, v in data.get("scores", {}).items()
            }
            failure_backoff = data.get("failures", {})
            logger.info(f"Loaded {len(gamerscore_cache)} cache entries from disk.")
        except Exception as e:
            logger.warning(f"Failed to load {CACHE_FILE}: {e}")


def save_cache() -> None:
    try:
        data = {
            "scores": {
                k: {"score": v[0], "timestamp": v[1]} for k, v in gamerscore_cache.items()
            },
            "failures": failure_backoff,
        }
        with open(CACHE_FILE, "w") as f:
            json.dump(data, f)
        logger.info(f"Saved {len(gamerscore_cache)} cache entries to disk.")
    except Exception as e:
        logger.error(f"Failed to save {CACHE_FILE}: {e}", exc_info=True)


# Register save_cache on exit
atexit.register(save_cache)


# ======== UTILITY FUNCTIONS ========
def extract_tags_from_embed(embed: discord.Embed) -> Set[str]:
    content = ""
    if embed.description:
        content += embed.description + "\n"
    for field in embed.fields:
        content += f"{field.name}\n{field.value}\n"

    tags: Set[str] = set()
    for line in content.splitlines():
        match = re.match(r"-\s+\*{0,2}(.+?)\*{0,2}$", line.strip())
        if match:
            tags.add(match.group(1).strip())
    return tags


def get_cached_score(tag: str) -> Optional[int]:
    normalized = tag.lower().strip()
    entry = gamerscore_cache.get(normalized)
    if entry:
        score, ts = entry
        if time.time() - ts < CACHE_EXPIRY_SECONDS:
            return score
        else:
            del gamerscore_cache[normalized]
    return None


def set_cached_score(tag: str, score: int) -> None:
    normalized = tag.lower().strip()
    gamerscore_cache[normalized] = (score, time.time())


async def fetch_gamerscore_http(tag: str) -> Optional[int]:
    """
    Send a single HTTP request (protected by rate_limit_lock).
    Returns the integer score on success, or None.
    On 429, sleeps 5s and returns None to let retry loop handle it.
    """
    global http_session

    async with rate_limit_lock:
        url_safe = tag.replace(" ", "%20")
        url = f"https://xboxgamertag.com/search/{url_safe}"
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; DiscordBot/1.0; +https://example.com/bot)"
        }

        try:
            resp = await http_session.get(url, headers=headers, timeout=15)
        except asyncio.TimeoutError:
            logger.warning(f"Timeout fetching page for '{tag}'.")
            return None
        except Exception as e:
            logger.error(f"HTTP error fetching '{tag}': {e}", exc_info=True)
            return None

        if resp.status == 429:
            logger.warning(f"Rate limited for '{tag}'. Sleeping 5s before retry.")
            await resp.release()
            await asyncio.sleep(5)
            return None

        if resp.status != 200:
            logger.warning(f"Received status {resp.status} for tag '{tag}'.")
            await resp.release()
            return None

        try:
            html = await resp.text()
        except Exception as e:
            logger.error(f"Error reading response text for '{tag}': {e}", exc_info=True)
            await resp.release()
            return None

        await resp.release()

    # Parse HTML outside the lock
    try:
        soup = BeautifulSoup(html, "html.parser")
        token = soup.find(string=re.compile(r"Gamerscore", re.IGNORECASE))
        if token and token.parent.name == "span":
            val_parent = token.parent.parent
            m = re.search(r"([\d,]+)", val_parent.get_text())
            if m:
                return int(m.group(1).replace(",", ""))
    except Exception as e:
        logger.error(f"Error parsing HTML for '{tag}': {e}", exc_info=True)

    return None


async def fetch_gamerscore(tag: str) -> Optional[int]:
    """
    1) Check cache
    2) Exponential backoff loop (3 tries)
       - Sleep 1s before each attempt
       - Call fetch_gamerscore_http (with lock)
       - On success, cache & return
       - On failure, log, back off, and retry
    """
    normalized = tag.lower().strip()
    cached = get_cached_score(normalized)
    if cached is not None:
        return cached

    for attempt in range(3):
        await asyncio.sleep(1)  # throttle between attempts

        score = await fetch_gamerscore_http(normalized)
        if score is not None:
            set_cached_score(normalized, score)
            failure_backoff[normalized] = 0
            return score

        failure_backoff[normalized] = failure_backoff.get(normalized, 0) + 1
        wait = min(1 << failure_backoff[normalized], 8)
        logger.warning(f"Attempt {attempt+1} failed for '{tag}'. Retrying in {wait}s…")
        await asyncio.sleep(wait)

    logger.error(f"All attempts failed for '{tag}'. Skipping until next cache expiry.")
    return None


async def find_latest_tag_mention(tag: str) -> Optional[str]:
    channel = bot.get_channel(LOOKUP_CHANNEL_ID)
    if not channel:
        return None

    tag_lower = tag.lower()
    async for msg in channel.history(oldest_first=False, limit=15000):
        if tag_lower in msg.content.lower():
            return msg.jump_url
        for embed in msg.embeds:
            if tag_lower in (embed.description or "").lower():
                return msg.jump_url
    return None


# ======== BOT EVENTS & TASKS ========
@bot.event
async def on_ready() -> None:
    global http_session
    logger.info(f"Logged in as {bot.user} ({bot.user.id})")

    # Load cache from disk on startup
    load_cache()

    missing_channels: List[int] = []
    for cid in SOURCE_CHANNEL_IDS | {DESTINATION_CHANNEL_ID, LOOKUP_CHANNEL_ID}:
        channel = bot.get_channel(cid)
        if channel is None:
            missing_channels.append(cid)
        else:
            perms = channel.permissions_for(channel.guild.me)
            if not (perms.read_messages and perms.send_messages):
                missing_channels.append(cid)

    if missing_channels:
        logger.critical(f"Missing or inaccessible channels: {missing_channels}. Shutting down.")
        await bot.close()
        return

    if http_session is None:
        http_session = aiohttp.ClientSession()
        logger.info("Initialized shared aiohttp ClientSession.")

    try:
        for cmd in tree.get_commands():
            cmd.guild_ids = [GUILD_ID]
        await tree.sync(guild=discord.Object(id=GUILD_ID))
        logger.info(f"Synced slash commands to guild {GUILD_ID}.")
    except Exception as e:
        logger.error(f"Slash command sync failed: {e}", exc_info=True)

    clear_expired_cache.start()


@tasks.loop(minutes=10)
async def clear_expired_cache() -> None:
    now = time.time()
    expired = [tag for tag, (_, ts) in gamerscore_cache.items() if now - ts > CACHE_EXPIRY_SECONDS]
    for tag in expired:
        del gamerscore_cache[tag]
        failure_backoff.pop(tag, None)
    if expired:
        logger.info(f"Purged {len(expired)} expired cache entries")


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author == bot.user or message.channel.id not in SOURCE_CHANNEL_IDS:
        return

    if not message.embeds:
        return

    new_tags: Set[str] = set()
    for embed in message.embeds:
        tags = extract_tags_from_embed(embed)
        for tag in tags:
            # Skip any tag in ignore_set
            if tag.lower() in ignore_set:
                continue
            if tag not in checked_gamertags:
                new_tags.add(tag)
                checked_gamertags.add(tag)

    for tag in new_tags:
        score = await fetch_gamerscore(tag)
        logger.info(f"Checked '{tag}': {score if score is not None else 'None'} GS")
        if score is not None and score < LOW_GAMERSCORE_THRESHOLD:
            dest = bot.get_channel(DESTINATION_CHANNEL_ID)
            if dest:
                link = await find_latest_tag_mention(tag)
                mention_text = f"✅ Mentioned: {link}" if link else "❌ not mentioned in GT-Link channel."
                await dest.send(
                    f"⚠️ **{tag}** has a low Gamerscore: `{score}`\n"
                    f"{mention_text}"
                )
        # After processing, add to ignore.txt so it's never rechecked
        append_ignore(tag)
        # Pause before next tag to avoid bursts
        await asyncio.sleep(1)


# ======== SLASH COMMAND: /checklast ========
@tree.command(
    name="checklast",
    description="Manually check last 50 messages in source channels for new gamertags",
    guild=discord.Object(id=GUILD_ID),
)
async def checklast(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(
        "🔁 Scanning last 50 messages in each source channel...", ephemeral=True
    )

    all_tags: Set[str] = set()
    for channel_id in SOURCE_CHANNEL_IDS:
        channel = bot.get_channel(channel_id)
        if channel is None:
            continue

        async for msg in channel.history(limit=50):
            for embed in msg.embeds:
                tags = extract_tags_from_embed(embed)
                for tag in tags:
                    # Skip ignored tags
                    if tag.lower() in ignore_set:
                        continue
                    if tag not in checked_gamertags:
                        all_tags.add(tag)
                        checked_gamertags.add(tag)

    logger.info(f"Extracted from history: {sorted(all_tags)}")
    for tag in all_tags:
        score = await fetch_gamerscore(tag)
        logger.info(f"Checked '{tag}': {score if score is not None else 'None'} GS")
        if score is not None and score < LOW_GAMERSCORE_THRESHOLD:
            dest = bot.get_channel(DESTINATION_CHANNEL_ID)
            if dest:
                link = await find_latest_tag_mention(tag)
                mention_text = f"✅ Mentioned: {link}" if link else "❌ not mentioned in GT-Link channel."
                await dest.send(
                    f"⚠️ **{tag}** has a low Gamerscore: `{score}`\n"
                    f"{mention_text}"
                )
        # After processing, add to ignore.txt so it won't be rechecked
        append_ignore(tag)
        await asyncio.sleep(1)


# ======== SLASH COMMAND: /lookup ========
@tree.command(
    name="lookup",
    description="Check if one or more gamertags were linked in the lookup channel",
    guild=discord.Object(id=GUILD_ID),
)
@app_commands.describe(gamertags="One or more gamertags (separated by commas or spaces)")
async def lookup(interaction: discord.Interaction, gamertags: str) -> None:
    await interaction.response.defer(ephemeral=False)
    channel = bot.get_channel(LOOKUP_CHANNEL_ID)
    if channel is None:
        await interaction.followup.send("❌ Lookup channel not found or inaccessible.")
        return

    raw_tags = re.split(r"[,\s]+", gamertags.strip())
    tags_to_search: List[str] = [t for t in raw_tags if t]

    results: Dict[str, Optional[str]] = {}
    for tag in tags_to_search:
        tag_lower = tag.lower()
        found_url: Optional[str] = None

        async for msg in channel.history(oldest_first=False, limit=15000):
            if tag_lower in msg.content.lower():
                found_url = msg.jump_url
                break
            for embed in msg.embeds:
                if tag_lower in (embed.description or "").lower():
                    found_url = msg.jump_url
                    break
            if found_url:
                break

        results[tag] = found_url

    response_lines: List[str] = []
    for tag, url in results.items():
        if url:
            response_lines.append(f"✅ Found `{tag}` in message: {url}")
        else:
            response_lines.append(f"❌ `{tag}` was not found in GT-Link channel.")

    await interaction.followup.send("\n".join(response_lines))


# ======== SLASH COMMAND: /gamerscore ========
@tree.command(
    name="gamerscore",
    description="Look up the gamerscore for a specific Xbox gamertag",
    guild=discord.Object(id=GUILD_ID),
)
@app_commands.describe(gamertag="Gamertag to check")
async def gamerscore(interaction: discord.Interaction, gamertag: str) -> None:
    await interaction.response.defer(ephemeral=False)
    score = await fetch_gamerscore(gamertag)
    if score is not None:
        await interaction.followup.send(f"🎮 **{gamertag}** has `{score}` Gamerscore.")
    else:
        await interaction.followup.send(f"❌ Gamerscore for **{gamertag}** could not be found.")


# ======== FLASK “KEEP-ALIVE” WEB SERVER + BOT RUN ========
from flask import Flask
import threading

app = Flask(__name__)

@app.route("/")
def home():
    return "3xBot is alive!", 200

def run_web():
    port = int(os.environ.get("PORT", 3000))
    app.run(host="0.0.0.0", port=port)

if __name__ == "__main__":
    # 1) Start the Flask server in a daemon thread
    web_thread = threading.Thread(target=run_web)
    web_thread.daemon = True
    web_thread.start()

    # 2) Then start the Discord bot itself
    bot.run(DISCORD_BOT_TOKEN)
