import os
import re
import time
import asyncio
import logging
from typing import Optional, Set, Dict, List

import aiohttp
from bs4 import BeautifulSoup

import discord
from discord.ext import commands, tasks
from discord import app_commands

# ======== CONFIG ========
DISCORD_BOT_TOKEN: str = os.getenv(
    "DISCORD_BOT_TOKEN",
    "MTM3ODgxOTc5MjM1NzgyMjQ2NA.GVrJYI.zVWjuvQV_Sbty6T7-lYTCpMeGPb82um2CJ0jds"
)
GUILD_ID: int = 1257773619405262860

SOURCE_CHANNEL_IDS: Set[int] = {
    1257773621410267219,
    1350591798636056636,
    1295756759771643966,   # corrected channel ID
}
DESTINATION_CHANNEL_ID: int = 1378835460171763712
LOOKUP_CHANNEL_ID: int = 1257773620768411735

CACHE_EXPIRY_SECONDS: int = 3600  # 1 hour
LOW_GAMERSCORE_THRESHOLD: int = 2000

# ======== LOGGING SETUP ========
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("GamerscoreBot")
# Silence excessive SSL-shutdown warnings from aiohttp
logging.getLogger("aiohttp.client").setLevel(logging.WARNING)

# ======== INTENTS & BOT SETUP ========
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree  # Slash command tree

# ======== GLOBAL STATE ========
checked_gamertags: Set[str] = set()
gamerscore_cache: Dict[str, tuple[int, float]] = {}  # tag → (score, timestamp)
failure_backoff: Dict[str, int] = {}  # tag → consecutive failure count

# We will create a single aiohttp.ClientSession and reuse it
http_session: Optional[aiohttp.ClientSession] = None


# ======== UTILITY FUNCTIONS ========
def extract_tags_from_embed(embed: discord.Embed) -> Set[str]:
    """
    Parse an embed for lines matching "- *Gamertag*" and return a set of unique tags.
    """
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
    """
    Return cached gamerscore if not expired, else None.
    """
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
    """
    Store gamerscore in cache with current timestamp.
    """
    normalized = tag.lower().strip()
    gamerscore_cache[normalized] = (score, time.time())


async def fetch_gamerscore_http(tag: str) -> Optional[int]:
    """
    Attempt to fetch gamerscore via HTTP + BeautifulSoup using the shared session.
    Returns score on success, or None on failure.
    """
    global http_session
    if http_session is None:
        logger.error("HTTP session is not initialized.")
        return None

    url_safe = tag.replace(" ", "%20")
    url = f"https://xboxgamertag.com/search/{url_safe}"
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; DiscordBot/1.0; +https://example.com/bot)"
    }

    try:
        async with http_session.get(url, headers=headers, timeout=15) as resp:
            if resp.status != 200:
                logger.warning(f"Received status {resp.status} for tag '{tag}'.")
                return None
            html = await resp.text()
    except asyncio.TimeoutError:
        logger.warning(f"Timeout fetching page for '{tag}'.")
        return None
    except Exception as e:
        logger.error(f"HTTP error fetching '{tag}': {e}", exc_info=True)
        return None

    try:
        soup = BeautifulSoup(html, "html.parser")
        token = soup.find(string=re.compile(r"Gamerscore", re.IGNORECASE))
        if token and token.parent.name == "span":
            val_parent = token.parent.parent
            m = re.search(r"([\d,]+)", val_parent.get_text())
            if m:
                score = int(m.group(1).replace(",", ""))
                return score
    except Exception as e:
        logger.error(f"Error parsing HTML for '{tag}': {e}", exc_info=True)

    return None


async def fetch_gamerscore(tag: str) -> Optional[int]:
    """
    Fetch gamerscore for a given tag with:
      1) Cache check
      2) HTTP+HTML parsing (up to 3 retries with exponential backoff)
    Returns score if found, else None.
    """
    normalized = tag.lower().strip()
    cached = get_cached_score(normalized)
    if cached is not None:
        return cached

    failure_count = failure_backoff.get(normalized, 0)
    delay = 1 << failure_count  # 2^failure_count
    if delay > 8:
        delay = 8

    if failure_count > 0:
        logger.info(f"Waiting {delay}s before retrying '{tag}' (failures={failure_count})")
        await asyncio.sleep(delay)

    for attempt in range(3):
        score = await fetch_gamerscore_http(normalized)
        if score is not None:
            set_cached_score(normalized, score)
            failure_backoff[normalized] = 0
            return score

        failure_backoff[normalized] = failure_backoff.get(normalized, 0) + 1
        wait = min(1 << failure_backoff[normalized], 8)
        logger.warning(f"Attempt {attempt+1} failed for '{tag}'. Retrying in {wait}s...")
        await asyncio.sleep(wait)

    logger.error(f"All attempts failed for '{tag}'. Skipping until next cache expiry.")
    return None


async def find_latest_tag_mention(tag: str) -> Optional[str]:
    """
    Scan LOOKUP_CHANNEL_ID for the most recent message or embed that mentions `tag`.
    Returns jump_url if found, otherwise None.
    """
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

    # 1) Validate all channel IDs and permissions
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

    # 2) Initialize a shared aiohttp.ClientSession exactly once
    if http_session is None:
        http_session = aiohttp.ClientSession()
        logger.info("Initialized shared aiohttp ClientSession.")

    # 3) Sync slash commands to this guild only
    try:
        for cmd in tree.get_commands():
            cmd.guild_ids = [GUILD_ID]
        await tree.sync(guild=discord.Object(id=GUILD_ID))
        logger.info(f"Synced slash commands to guild {GUILD_ID}.")
    except Exception as e:
        logger.error(f"Slash command sync failed: {e}", exc_info=True)

    # 4) Launch the background cache‐cleaner
    clear_expired_cache.start()


@tasks.loop(minutes=10)
async def clear_expired_cache() -> None:
    """
    Evict any cached gamerscore entries older than CACHE_EXPIRY_SECONDS.
    """
    now = time.time()
    expired = [tag for tag, (_, ts) in gamerscore_cache.items() if now - ts > CACHE_EXPIRY_SECONDS]
    for tag in expired:
        del gamerscore_cache[tag]
        failure_backoff.pop(tag, None)
    if expired:
        logger.info(f"Purged {len(expired)} expired cache entries")


@bot.event
async def on_message(message: discord.Message) -> None:
    """
    Watch for any embed in SOURCE_CHANNEL_IDS, extract fresh gamertags,
    fetch their gamerscore, and post a warning if it’s below LOW_GAMERSCORE_THRESHOLD.
    Also include a “mentioned?” link from find_latest_tag_mention().
    """
    if message.author == bot.user or message.channel.id not in SOURCE_CHANNEL_IDS:
        return

    if not message.embeds:
        return

    new_tags: Set[str] = set()
    for embed in message.embeds:
        tags = extract_tags_from_embed(embed)
        for tag in tags:
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
        await asyncio.sleep(1)  # throttle between lookups


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


# ======== BOT RUN ========
if __name__ == "__main__":
    if not DISCORD_BOT_TOKEN or "YOUR_TOKEN_HERE" in DISCORD_BOT_TOKEN:
        logger.critical(
            "Discord bot token is not set. Please configure the DISCORD_BOT_TOKEN environment variable."
        )
        exit(1)

    bot.run(DISCORD_BOT_TOKEN)
