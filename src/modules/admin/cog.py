from __future__ import annotations

import os
from typing import List
import importlib
import pkgutil

import discord
from discord import app_commands
from discord.ext import commands
from core.utils import reply, DEV_GUILD

PATH_PREFIX = "src.modules"          # where your cogs live
COG_SUFFIX = "cog"                   # files named .../cog.py
OWNER_ID = int(os.getenv("BOT_OWNER_ID", "0"))


def full_ext(name: str) -> str:
    """Normalize user input to a full extension path."""
    n = name.strip()
    if n.startswith(PATH_PREFIX):
        return n
    # Accept "ping" or "ping.cog"
    parts = n.split(".")
    if parts[-1] != COG_SUFFIX:
        n = f"{n}.{COG_SUFFIX}"
    return f"{PATH_PREFIX}.{n}"

def all_extensions() -> List[str]:
    """Discover available extensions under PATH_PREFIX."""
    pkg = importlib.import_module(PATH_PREFIX)
    exts: List[str] = []
    for m in pkgutil.walk_packages(pkg.__path__, pkg.__name__ + "."):
        if m.name.endswith("." + COG_SUFFIX):
            exts.append(m.name)
    return sorted(exts)

def app_is_owner():
    async def predicate(interaction: discord.Interaction) -> bool:
        return interaction.user and interaction.user.id == OWNER_ID
    return app_commands.check(predicate)

async def _ext_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> List[app_commands.Choice[str]]:
    candidates = [e for e in all_extensions() if current.lower() in e.lower()]
    return [app_commands.Choice(name=e, value=e) for e in candidates[:25]]

class Admin(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ------- load -------
    @commands.is_owner()
    @commands.hybrid_command(name="load", with_app_command=True, description="Load an extension")
    @app_commands.autocomplete(ext=_ext_autocomplete)
    @discord.app_commands.guilds(DEV_GUILD)
    async def load(self, ctx: commands.Context, ext: str):
        ext = full_ext(ext) if not ext.startswith(PATH_PREFIX) else ext
        try:
            await self.bot.load_extension(ext)
            await ctx.reply(f"Loaded {ext}")
        except commands.ExtensionAlreadyLoaded:
            await ctx.reply(f"{ext} is already loaded")
        except Exception as e:
            await ctx.reply(f"Failed to load {ext}: {e}")

    # ------- unload -------
    @commands.is_owner()
    @commands.hybrid_command(name="unload", with_app_command=True, description="Unload an extension")
    @discord.app_commands.guilds(DEV_GUILD)
    @app_commands.autocomplete(ext=_ext_autocomplete)
    async def unload(self, ctx: commands.Context, ext: str):
        ext = full_ext(ext) if not ext.startswith(PATH_PREFIX) else ext
        try:
            await self.bot.unload_extension(ext)
            await ctx.reply(f"Unloaded {ext}")
        except commands.ExtensionNotLoaded:
            await ctx.reply(f"{ext} is not loaded")
        except Exception as e:
            await ctx.reply(f"Failed to unload {ext}: {e}")

    # ------- reload -------
    @commands.is_owner()
    @commands.hybrid_command(name="reload", with_app_command=True, description="Reload an extension")
    @discord.app_commands.guilds(DEV_GUILD)
    @app_commands.autocomplete(ext=_ext_autocomplete)
    async def reload(self, ctx: commands.Context, ext: str):
        ext = full_ext(ext) if not ext.startswith(PATH_PREFIX) else ext
        try:
            await self.bot.reload_extension(ext)
            await ctx.reply(f"Reloaded {ext}")
        except commands.ExtensionNotLoaded:
            await ctx.reply(f"{ext} is not loaded")
        except Exception as e:
            await ctx.reply(f"Failed to reload {ext}: {e}")

    # ------- reload all -------
    @commands.is_owner()
    @commands.hybrid_command(name="reload_all", with_app_command=True, description="Reload all discovered extensions")
    @discord.app_commands.guilds(DEV_GUILD)
    async def reload_all(self, ctx: commands.Context):
        exts = all_extensions()
        ok, fail = [], []
        for e in exts:
            try:
                # load if not loaded yet, otherwise reload
                if e in self.bot.extensions:
                    await self.bot.reload_extension(e)
                else:
                    await self.bot.load_extension(e)
                ok.append(e)
            except Exception as ex:
                fail.append((e, str(ex)))
        msg = f"Reloaded/loaded {len(ok)} extensions."
        if fail:
            msg += f" Failed {len(fail)}: " + ", ".join([f[0] for f in fail])
        await ctx.reply(msg)

    # ------- list -------
    @commands.is_owner()
    @discord.app_commands.guilds(DEV_GUILD)
    @commands.hybrid_command(name="list_ext", with_app_command=True, description="List available and loaded extensions")
    async def list_ext(self, ctx: commands.Context):
        available = set(all_extensions())
        loaded = set(self.bot.extensions.keys())
        unloaded = available - loaded
        text = (
            "**Loaded:**\n" + ("\n".join(sorted(loaded)) or "none")
            + "\n\n**Unloaded:**\n" + ("\n".join(sorted(unloaded)) or "none")
        )
        await ctx.reply(text)

    # ------- sync slash commands -------
    @commands.is_owner()
    @discord.app_commands.guilds(DEV_GUILD)
    @commands.hybrid_command(name="sync", with_app_command=True, description="Sync app commands")
    async def sync(self, ctx: commands.Context, guild_id: int | None = None):
        try:
            if guild_id:
                g = discord.Object(id=guild_id)
                synced = await self.bot.tree.sync(guild=g)
                await ctx.reply(f"Synced {len(synced)} commands to guild {guild_id}")
            else:
                synced = await self.bot.tree.sync()
                await ctx.reply(f"Synced {len(synced)} global commands")
        except Exception as e:
            await ctx.reply(f"Sync failed: {e}")

    # ---- shutdown ----
    @commands.is_owner()
    @commands.hybrid_command(
        name="shutdown",
        description="Safely shut down the bot.",
        with_app_command=True,
    )
    @discord.app_commands.guilds(DEV_GUILD)
    @app_is_owner()  # extra guard for the slash path
    @app_commands.default_permissions()  # see note below on hiding
    async def shutdown(self, ctx: commands.Context):
        await reply(ctx, "Shutting down…", ephemeral=True)
        await self.bot.close()


async def setup(bot: commands.Bot):
    await bot.add_cog(Admin(bot))
