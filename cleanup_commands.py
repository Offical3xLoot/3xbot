import discord
from discord.ext import commands

DISCORD_BOT_TOKEN = "MTM3ODgxOTc5MjM1NzgyMjQ2NA.GVrJYI.zVWjuvQV_Sbty6T7-lYTCpMeGPb82um2CJ0jds"
GUILD_ID = 1257773619405262860

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    guild = discord.Object(id=GUILD_ID)
    print(f"🧹 Cleaning commands for guild {GUILD_ID}...")

    # Clear guild commands
    try:
        await bot.tree.sync(guild=guild)
        bot.tree.clear_commands(guild=guild)
        await bot.tree.sync(guild=guild)
        print("✅ Guild commands cleared.")
    except Exception as e:
        print(f"❌ Error clearing commands: {e}")

    await bot.close()

bot.run(DISCORD_BOT_TOKEN)
