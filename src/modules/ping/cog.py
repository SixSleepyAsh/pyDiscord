# src/modules/ping/cog.py
from discord.ext import commands
from src.core.utils import reply, defer_if_needed, edit_original
import asyncio, discord
from core.utils import DEV_GUILD


class Ping(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.hybrid_command(name="ping", description="Pong")
    @discord.app_commands.guilds(DEV_GUILD)
    async def ping(self, ctx: commands.Context):
        await reply(ctx, "Pong!")

    @discord.app_commands.command(name="slow", description="Shows defer + edit pattern")
    @discord.app_commands.guilds(DEV_GUILD)
    async def slow(self, interaction: discord.Interaction):
        await defer_if_needed(interaction, ephemeral=True)
        await asyncio.sleep(5)
        await edit_original(interaction, "Done.")

async def setup(bot: commands.Bot):
    await bot.add_cog(Ping(bot))
