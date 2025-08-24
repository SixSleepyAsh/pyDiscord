from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Optional, Set, Dict, Tuple

import discord
from discord.ext import commands
from discord import app_commands

from core.utils import DEV_GUILD
from src.core.utils import reply

# -----------------------------
# Persistence
# -----------------------------
CONFIG_PATH = Path("config/voice_channels.json")
CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)

# Module defaults (used if a guild doesn't override)
FALLBACK_USER_LIMIT = 0          # 0 = unlimited
FALLBACK_BITRATE_BPS: Optional[int] = None  # None = use guild default
FALLBACK_DELETE_DELAY_SEC = 5

Key = Tuple[int, int]  # (guild_id, owner_user_id)


class VoiceChannels(commands.Cog):
    """
    Auto-creates personal voice channels when users join a configured "lobby" voice channel.
    - Remembers settings per guild in config/voice_channels.json
    - Reuses an existing personal channel for the same user if it still exists
    - Deletes empty personal channels after a delay
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot

        # --- Runtime state ---
        # guild_id -> lobby_channel_id
        self._channels: Dict[int, int] = {}
        # guild_id -> category_id (optional)
        self._categories: Dict[int, int] = {}
        # guild_id -> per-guild defaults
        self._defaults: Dict[int, Dict[str, Optional[int]]] = {}
        # track temporary (personal) channel ids for cleanup
        self._temp_channels: Set[int] = set()
        # channel_id -> cleanup task
        self._cleanup_tasks: Dict[int, asyncio.Task] = {}
        # (guild_id, owner_id) -> channel_id
        self._owner_room: Dict[Key, int] = {}

        self._load_config()

    # -----------------------------
    # Persistence helpers
    # -----------------------------
    def _config_snapshot(self) -> Dict:
        return {
            "guilds": {
                str(gid): {
                    "lobby_channel_id": self._channels.get(gid),
                    "category_id": self._categories.get(gid),
                    "user_limit": int(self._defaults.get(gid, {}).get("user_limit", FALLBACK_USER_LIMIT)),
                    "bitrate_bps": self._defaults.get(gid, {}).get("bitrate_bps", FALLBACK_BITRATE_BPS),
                    "delete_delay_sec": int(self._defaults.get(gid, {}).get("delete_delay_sec", FALLBACK_DELETE_DELAY_SEC)),
                }
                for gid in set(self._channels.keys()) | set(self._categories.keys()) | set(self._defaults.keys())
            }
        }

    def _load_config(self) -> None:
        if not CONFIG_PATH.exists():
            return
        try:
            raw = CONFIG_PATH.read_text(encoding="utf-8").strip()
            if not raw:
                return
            data = json.loads(raw)
        except Exception as e:
            # Don't crash if the file is empty/corrupt; start fresh
            print(f"[voice] Warning: failed to load {CONFIG_PATH}: {e}")
            return

        guilds = data.get("guilds", {})
        for sgid, cfg in guilds.items():
            try:
                gid = int(sgid)
            except Exception:
                continue

            lobby = cfg.get("lobby_channel_id")
            cat = cfg.get("category_id")
            if lobby:
                self._channels[gid] = int(lobby)
            if cat:
                self._categories[gid] = int(cat)

            self._defaults.setdefault(gid, {})
            self._defaults[gid]["user_limit"] = int(cfg.get("user_limit", FALLBACK_USER_LIMIT))
            self._defaults[gid]["bitrate_bps"] = cfg.get("bitrate_bps", FALLBACK_BITRATE_BPS)
            self._defaults[gid]["delete_delay_sec"] = int(cfg.get("delete_delay_sec", FALLBACK_DELETE_DELAY_SEC))

    def _save_config(self) -> None:
        snap = self._config_snapshot()
        tmp = CONFIG_PATH.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(snap, indent=2), encoding="utf-8")
            tmp.replace(CONFIG_PATH)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink(missing_ok=True)
                except Exception:
                    pass

    # -----------------------------
    # Defaults lookup
    # -----------------------------
    def _g_user_limit(self, gid: int) -> int:
        return int(self._defaults.get(gid, {}).get("user_limit", FALLBACK_USER_LIMIT))

    def _g_bitrate_bps(self, gid: int) -> Optional[int]:
        v = self._defaults.get(gid, {}).get("bitrate_bps", FALLBACK_BITRATE_BPS)
        return int(v) if isinstance(v, int) else None

    def _g_delete_delay(self, gid: int) -> int:
        return int(self._defaults.get(gid, {}).get("delete_delay_sec", FALLBACK_DELETE_DELAY_SEC))

    # -----------------------------
    # Admin setup
    # -----------------------------
    @app_commands.command(
        name="voice_setup",
        description="Configure the lobby voice channel and optional category for personal rooms (saved per guild).",
    )
    @app_commands.describe(
        channel="Voice channel users join to get a personal channel",
        category="Category to create personal channels under (optional)",
        user_limit="Max users per personal channel (0 = unlimited)",
        bitrate_kbps="Bitrate in kbps (None = guild default)",
        delete_delay="Seconds to wait before deleting an empty personal channel",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guilds(DEV_GUILD)
    async def voice_setup(
            self,
            interaction: discord.Interaction,
            channel: discord.VoiceChannel,
            category: Optional[discord.CategoryChannel] = None,
            user_limit: Optional[int] = None,
            bitrate_kbps: Optional[int] = None,
            delete_delay: Optional[int] = None,
    ):
        gid = interaction.guild_id
        assert gid is not None

        self._channels[gid] = channel.id
        if category:
            self._categories[gid] = category.id

        d = self._defaults.setdefault(gid, {})
        if user_limit is not None:
            d["user_limit"] = max(0, int(user_limit))
        if bitrate_kbps is not None:
            # Stored as bps; actual clamp happens at creation time
            d["bitrate_bps"] = max(8000, int(bitrate_kbps) * 1000)
        if delete_delay is not None:
            d["delete_delay_sec"] = max(5, int(delete_delay))

        self._save_config()

        await reply(
            interaction,
            f"Lobby: {channel.mention}"
            + (f" | Category: {category.mention}" if category else " | Category: (unchanged/none)")
            + f"\nUser limit: {self._g_user_limit(gid) or 'unlimited'}"
            + f" | Bitrate: {(bitrate_kbps if bitrate_kbps is not None else 'default')} kbps"
            + f" | Delete delay: {self._g_delete_delay(gid)}s",
            ephemeral=True,
            )

    @commands.hybrid_command(name="voice_status", description="Show current personal-voice settings (saved).")
    @app_commands.guilds(DEV_GUILD)
    @commands.has_guild_permissions(manage_guild=True)
    async def voice_status(self, ctx: commands.Context):
        gid = ctx.guild.id
        channel = self._channels.get(gid)
        cat = self._categories.get(gid)
        await ctx.reply(
            "Voice Personal Rooms (saved):\n"
            f"- Lobby channel: {('<#'+str(channel)+'>') if channel else 'not set'}\n"
            f"- Category: {('<#'+str(cat)+'>') if cat else '(none)'}\n"
            f"- User limit: {self._g_user_limit(gid) or 'unlimited'}\n"
            f"- Bitrate: { (str(int(self._g_bitrate_bps(gid)/1000))+' kbps') if self._g_bitrate_bps(gid) else 'guild default' }\n"
            f"- Delete delay: {self._g_delete_delay(gid)}s\n"
            f"- Temp channels tracked: {len(self._temp_channels)}"
        )

    # -----------------------------
    # Core behavior
    # -----------------------------
    @commands.Cog.listener()
    async def on_voice_state_update(
            self,
            member: discord.Member,
            before: discord.VoiceState,
            after: discord.VoiceState,
    ):
        # Any move/leave may unlock a cleanup
        if before.channel:
            await self._maybe_schedule_cleanup(before.channel)

        # Only care about joins/moves into a configured lobby
        if after.channel is None or after.channel == before.channel:
            return

        gid = member.guild.id
        lobby_id = self._channels.get(gid)
        if not lobby_id or after.channel.id != lobby_id:
            return

        # Joined the lobby: move to existing room or create one
        await self._move_to_existing_or_create(member, after.channel)

        # Also check the lobby for cleanup (it might be empty now)
        await self._maybe_schedule_cleanup(after.channel)

    async def _move_to_existing_or_create(self, member: discord.Member, lobby: discord.VoiceChannel):
        gid = member.guild.id
        key: Key = (gid, member.id)

        # Reuse existing room if we have one and it still exists
        chan_id = self._owner_room.get(key)
        if chan_id:
            chan = member.guild.get_channel(chan_id)
            if isinstance(chan, discord.VoiceChannel):
                # Cancel pending cleanup for that channel
                task = self._cleanup_tasks.pop(chan.id, None)
                if task:
                    task.cancel()
                # If already there, nothing to do
                if member.voice and member.voice.channel and member.voice.channel.id == chan.id:
                    return
                try:
                    await member.move_to(chan, reason="Back to existing personal channel")
                except discord.HTTPException:
                    pass
                return

            # Channel vanished; forget it
            self._owner_room.pop(key, None)
            self._temp_channels.discard(chan_id)
            self._cleanup_tasks.pop(chan_id, None)

        # Otherwise create a new personal channel and move
        new_channel = await self._create_personal_channel(member, lobby)
        self._owner_room[key] = new_channel.id
        try:
            await member.move_to(new_channel, reason="Move to personal channel")
        except discord.HTTPException:
            pass

    async def _create_personal_channel(self, member: discord.Member, lobby: discord.VoiceChannel) -> discord.VoiceChannel:
        guild = member.guild
        gid = guild.id

        # Category selection (from saved category id if present)
        category = None
        cat_id = self._categories.get(gid)
        if cat_id:
            maybe = guild.get_channel(cat_id)
            if isinstance(maybe, discord.CategoryChannel):
                category = maybe

        # Name & perms
        name = f"{member.display_name}'s Channel"
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(connect=True, view_channel=True),
            member: discord.PermissionOverwrite(
                manage_channels=True,
                move_members=True,
                mute_members=True,
                deafen_members=True,
                connect=True,
                view_channel=True,
                manage_permissions=True,
            ),
        }

        # Bitrate (clamped to guild limit); if None, mirror lobby bitrate
        desired_bps = self._g_bitrate_bps(gid)
        bitrate = desired_bps if desired_bps is not None else lobby.bitrate
        bitrate = min(int(bitrate), int(guild.bitrate_limit))

        channel = await guild.create_voice_channel(
            name=name,
            category=category or lobby.category,
            overwrites=overwrites,
            user_limit=self._g_user_limit(gid),
            bitrate=bitrate,
            reason=f"Personal voice channel for {member} from lobby {lobby.name}",
        )

        self._temp_channels.add(channel.id)
        # Cancel any stray scheduled cleanup (shouldn't exist yet)
        task = self._cleanup_tasks.pop(channel.id, None)
        if task:
            task.cancel()

        return channel

    async def _maybe_schedule_cleanup(self, channel: discord.VoiceChannel):
        """If a tracked personal channel becomes empty, schedule deletion with a delay."""
        if channel.id not in self._temp_channels:
            return

        gid = channel.guild.id
        delay = self._g_delete_delay(gid)

        # If members still present, cancel cleanup (if any)
        if channel.members:
            t = self._cleanup_tasks.pop(channel.id, None)
            if t:
                t.cancel()
            return

        # Already scheduled?
        if channel.id in self._cleanup_tasks:
            return

        async def _cleanup():
            try:
                await asyncio.sleep(delay)
                if channel.members:
                    return
                # Clear owner mappings that point to this channel
                to_remove = [k for k, cid in list(self._owner_room.items()) if cid == channel.id]
                for k in to_remove:
                    self._owner_room.pop(k, None)
                try:
                    await channel.delete(reason="Personal voice channel empty")
                finally:
                    self._temp_channels.discard(channel.id)
            except asyncio.CancelledError:
                return
            except discord.HTTPException:
                # If deletion fails, still clear tracking/mapping
                self._temp_channels.discard(channel.id)
                to_remove = [k for k, cid in list(self._owner_room.items()) if cid == channel.id]
                for k in to_remove:
                    self._owner_room.pop(k, None)

        self._cleanup_tasks[channel.id] = asyncio.create_task(_cleanup())

    # -----------------------------
    # Lifecycle
    # -----------------------------
    def cog_unload(self):
        # Best-effort: cancel timers and persist config
        for t in list(self._cleanup_tasks.values()):
            t.cancel()
        self._save_config()


async def setup(bot: commands.Bot):
    await bot.add_cog(VoiceChannels(bot))
