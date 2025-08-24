from __future__ import annotations

import asyncio
import time
import logging
from dataclasses import dataclass
from typing import Optional, Deque, Dict, List, cast
from collections import deque

import discord
from discord.ext import commands
from discord import app_commands

from core.utils import DEV_GUILD, reply  # unified import

import yt_dlp

log = logging.getLogger("voice")

YDL_OPTS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "default_search": "ytsearch",
    "extract_flat": False,
}

FFMPEG_OPTS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}

if not discord.opus.is_loaded():
    try:
        discord.opus.load_opus("libopus.so.0")   # Linux
        log.info("Loaded Opus: %s", discord.opus.is_loaded())
    except Exception as e:
        log.warning("Could not load libopus: %s (voice can connect but playback will fail)", e)


def fmt_time(sec: Optional[float]) -> str:
    if not sec or sec < 0:
        return "?:??"
    sec = int(sec)
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def progress_bar(frac: float, width: int = 16) -> str:
    frac = max(0.0, min(1.0, frac))
    fill = int(frac * width)
    return "▰" * fill + "▱" * (width - fill)


def clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def volume_bar(percent: int, width: int = 12) -> str:
    filled = int(round(percent / 100 * width))
    return "█" * filled + "░" * (width - filled)


@dataclass
class Track:
    title: str
    url: str            # direct ffmpeg stream URL
    web_url: str        # watch/share URL
    duration: float     # seconds
    requested_by: str


class GuildPlayer:
    """One player per guild; manages queue, voice connection, and playback loop."""

    def __init__(self, bot: commands.Bot, guild: discord.Guild):
        self.bot = bot
        self.guild = guild
        self.queue: Deque[Track] = deque()
        self.current: Optional[Track] = None
        self.voice: Optional[discord.VoiceClient] = None
        self._play_task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self._started_at: Optional[float] = None

        # volume + UI
        self.volume: float = 1.0  # 0.0–2.0
        self._pcm: Optional[discord.PCMVolumeTransformer] = None
        self._last_nonzero_volume: float = 1.0

        # where to announce now playing
        self.announce_channel: Optional[discord.abc.Messageable] = None

    # ---------- presence / occupancy helpers ----------
    def others_in_channel_count(self) -> int:
        """How many members are in the current VC, excluding THIS bot."""
        if not self.voice or not self.voice.channel or not self.bot.user:
            return 0
        return sum(1 for m in self.voice.channel.members if m.id != self.bot.user.id)

    def channel_empty_excluding_self(self) -> bool:
        return self.others_in_channel_count() == 0

    async def teardown(self):
        """Stop playback, clear queue, and disconnect."""
        try:
            self.queue.clear()
            self._stop_event.set()
            if self.voice and self.voice.is_playing():
                self.voice.stop()
            if self.voice and self.voice.is_connected():
                await self.voice.disconnect()
        finally:
            self.current = None
            self._started_at = None
            self._pcm = None

    # ---------- durations / positions ----------
    def total_queue_seconds(self) -> int:
        return int(sum(t.duration for t in self.queue))

    def position_seconds(self) -> float:
        if self._started_at is None:
            return 0.0
        return max(0.0, time.monotonic() - self._started_at)

    # ---------- voice connection ----------
    async def ensure_connected(self, channel: discord.VoiceChannel):
        """Connect or move to the target voice channel."""
        if self.voice and self.voice.is_connected():
            if self.voice.channel.id != channel.id:
                await self.voice.move_to(channel)
            return

        if self.voice and getattr(self.voice, "is_connecting", lambda: False)():
            for _ in range(10):
                if self.voice.is_connected():
                    break
                await asyncio.sleep(0.2)

        if not self.voice or not self.voice.is_connected():
            self.voice = await channel.connect(self_deaf=True)

    # ---------- queue ops ----------
    async def add(self, track: Track):
        self.queue.append(track)
        if not self._play_task or self._play_task.done():
            self._stop_event.clear()
            self._play_task = asyncio.create_task(self._player_loop())

    async def skip(self):
        if self.voice and self.voice.is_playing():
            self.voice.stop()
        self._stop_event.set()

    def shuffle(self):
        if not self.queue:
            return
        import random
        q = list(self.queue)
        random.shuffle(q)
        self.queue = deque(q)

    def clear_queue(self):
        self.queue.clear()

    # ---------- volume helpers ----------
    def volume_percent(self) -> int:
        return int(round(self.volume * 100))

    def set_volume_percent(self, percent: int) -> int:
        pct = int(clamp(percent, 0, 200))
        self.volume = pct / 100.0
        if self.volume > 0:
            self._last_nonzero_volume = self.volume
        if self._pcm:
            self._pcm.volume = self.volume
        return pct

    def mute(self) -> None:
        if self.volume > 0:
            self._last_nonzero_volume = self.volume
        self.set_volume_percent(0)

    def unmute(self) -> int:
        target = self._last_nonzero_volume if self._last_nonzero_volume > 0 else 1.0
        return self.set_volume_percent(int(target * 100))

    # ---------- pause/resume ----------
    def pause(self) -> bool:
        if self.voice and self.voice.is_playing():
            self.voice.pause()
            return True
        return False

    def resume(self) -> bool:
        if self.voice and self.voice.is_paused():
            self.voice.resume()
            return True
        return False

    # ---------- player loop ----------
    async def _player_loop(self):
        while self.queue:
            # If nobody else is here, bail and disconnect after loop
            if self.channel_empty_excluding_self():
                break

            self.current = self.queue.popleft()
            self._started_at = None

            if not self.voice or not self.voice.is_connected():
                break

            try:
                src = discord.FFmpegPCMAudio(self.current.url, **FFMPEG_OPTS)
                self._pcm = discord.PCMVolumeTransformer(src, volume=self.volume)
            except Exception:
                self.current = None
                await asyncio.sleep(0)
                continue

            finished = asyncio.Event()

            def _after(_err):
                finished.set()

            self.voice.play(self._pcm, after=_after)
            self._started_at = time.monotonic()

            # --- announce now playing (public) ---
            try:
                if self.announce_channel and self.current:
                    pos = 0.0
                    dur = self.current.duration or 0.0
                    frac = 0.0 if not dur else min(1.0, pos / dur)
                    bar = progress_bar(frac)
                    await self.announce_channel.send(
                        f"▶️ **Now Playing:** [{self.current.title}]({self.current.web_url})\n"
                        f"{bar}  {fmt_time(pos)} / {fmt_time(dur)} • requested by {self.current.requested_by}"
                    )
            except Exception:
                pass
            # -------------------------------------

            w1 = asyncio.create_task(finished.wait())
            w2 = asyncio.create_task(self._stop_event.wait())
            try:
                await asyncio.wait({w1, w2}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                w1.cancel()
                w2.cancel()

            self._stop_event.clear()
            self._pcm = None
            await asyncio.sleep(0.2)

        # cleanup at the end of loop
        if self.channel_empty_excluding_self() and self.voice and self.voice.is_connected():
            await self.voice.disconnect()

        self.current = None
        self._started_at = None
        self._pcm = None


class Music(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.players: Dict[int, GuildPlayer] = {}

    # ---- helpers ----

    def get_player(self, guild: discord.Guild) -> GuildPlayer:
        if guild.id not in self.players:
            self.players[guild.id] = GuildPlayer(self.bot, guild)
        return self.players[guild.id]

    def _is_voice_connected(self, guild: discord.Guild) -> bool:
        vc = guild.voice_client
        return bool(vc and vc.is_connected())

    def _user_vc(self, ctx: commands.Context | discord.Interaction) -> discord.VoiceChannel | None:
        user = ctx.user if isinstance(ctx, discord.Interaction) else ctx.author
        if getattr(user, "voice", None) and user.voice.channel and isinstance(user.voice.channel, discord.VoiceChannel):
            return user.voice.channel
        return None

    async def _defer_if_interaction(self, ctx: commands.Context, *, ephemeral: bool = True) -> Optional[discord.Interaction]:
        it = getattr(ctx, "interaction", None)
        if it and not it.response.is_done():
            await it.response.defer(ephemeral=ephemeral)
        return it

    async def _resolve_query(self, query: str, requester: str) -> Track:
        with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
            info = ydl.extract_info(query, download=False)
            if "entries" in info:
                info = info["entries"][0]
            url = info.get("url")
            if not url:
                raise RuntimeError("No playable URL found for this query.")
            web = info.get("webpage_url") or info.get("original_url") or query
            title = info.get("title") or "Unknown"
            duration = float(info.get("duration") or 0)
            return Track(title=title, url=url, web_url=web, duration=duration, requested_by=requester)

    async def _author_vc_or_error(self, ctx: commands.Context) -> discord.VoiceChannel:
        if not ctx.author.voice or not ctx.author.voice.channel:
            raise commands.CommandError("You must be in a voice channel.")
        return ctx.author.voice.channel

    # ---- auto-disconnect listener ----
    @commands.Cog.listener()
    async def on_voice_state_update(
            self,
            member: discord.Member,
            before: discord.VoiceState,
            after: discord.VoiceState,
    ):
        # Only handle if a player exists for this guild and bot is connected
        player = self.players.get(member.guild.id)
        if not player or not player.voice or not player.voice.is_connected():
            return

        ch = player.voice.channel
        # Only consider updates that touch the channel we're in
        affected = (
                (before.channel and before.channel.id == ch.id)
                or (after.channel and after.channel.id == ch.id)
        )
        if not affected:
            return

        if player.others_in_channel_count() == 0:
            try:
                if player.announce_channel:
                    await player.announce_channel.send("👋 Channel’s empty — disconnecting.")
            except Exception:
                pass
            await player.teardown()
            # keep the GuildPlayer object — or remove; here we remove
            self.players.pop(member.guild.id, None)

    # ---- UI view for /volume ----
    class VolumeView(discord.ui.View):
        def __init__(self, player: GuildPlayer):
            super().__init__(timeout=120)
            self.player = player

        def embed(self) -> discord.Embed:
            pct = self.player.volume_percent()
            e = discord.Embed(
                title="🔊 Volume",
                description=f"{volume_bar(pct)}  **{pct}%**",
                color=discord.Color.blurple(),
            )
            return e

        async def _bump(self, interaction: discord.Interaction, delta: int):
            new_pct = self.player.volume_percent() + delta
            self.player.set_volume_percent(new_pct)
            await interaction.response.edit_message(embed=self.embed(), view=self)

        @discord.ui.button(label="-10%", style=discord.ButtonStyle.secondary, row=0)
        async def quieter(self, interaction: discord.Interaction, _: discord.ui.Button):
            await self._bump(interaction, -10)

        @discord.ui.button(label="+10%", style=discord.ButtonStyle.secondary, row=0)
        async def louder(self, interaction: discord.Interaction, _: discord.ui.Button):
            await self._bump(interaction, +10)

        @discord.ui.button(label="Mute/Unmute", style=discord.ButtonStyle.danger, row=1)
        async def mute_unmute(self, interaction: discord.Interaction, _: discord.ui.Button):
            if self.player.volume_percent() == 0:
                self.player.unmute()
            else:
                self.player.mute()
            await interaction.response.edit_message(embed=self.embed(), view=self)

        @discord.ui.button(label="50%", style=discord.ButtonStyle.primary, row=1)
        async def p50(self, interaction: discord.Interaction, _: discord.ui.Button):
            self.player.set_volume_percent(50)
            await interaction.response.edit_message(embed=self.embed(), view=self)

        @discord.ui.button(label="100%", style=discord.ButtonStyle.primary, row=1)
        async def p100(self, interaction: discord.Interaction, _: discord.ui.Button):
            self.player.set_volume_percent(100)
            await interaction.response.edit_message(embed=self.embed(), view=self)

        @discord.ui.button(label="150%", style=discord.ButtonStyle.primary, row=1)
        async def p150(self, interaction: discord.Interaction, _: discord.ui.Button):
            self.player.set_volume_percent(150)
            await interaction.response.edit_message(embed=self.embed(), view=self)

    # ---- Animated Queue UI ----
    class QueueView(discord.ui.View):
        """Paginated, live-updating queue with controls (<=5 rows)."""
        def __init__(self, cog: Music, player: GuildPlayer, per_page: int = 8):
            super().__init__(timeout=120)
            self.cog = cog
            self.player = player
            self.per_page = per_page
            self.page = 0
            self.message: Optional[discord.Message] = None
            self._refresh_task: Optional[asyncio.Task] = None
            self._stop_evt = asyncio.Event()

            # Row 2: dynamic select for removal (its own row)
            self.remove_select = discord.ui.Select(
                placeholder="Remove…",
                min_values=1,
                max_values=1,
                options=[],
                row=2,
            )
            self.remove_select.callback = self._on_remove_select
            self.add_item(self.remove_select)

        # --- embed builder ---
        def _queue_pages(self) -> List[List[Track]]:
            q = list(self.player.queue)
            pages: List[List[Track]] = []
            for i in range(0, len(q), self.per_page):
                pages.append(q[i:i + self.per_page])
            if not pages:
                pages = [[]]
            return pages

        def _nowplaying_line(self) -> str:
            cur = self.player.current
            if not cur:
                return "Nothing playing."
            pos = self.player.position_seconds()
            dur = cur.duration or 0.0
            frac = 0.0 if not dur else min(1.0, pos / dur)
            bar = progress_bar(frac, 22)
            return f"[{cur.title}]({cur.web_url})\n{bar}  **{fmt_time(pos)} / {fmt_time(dur)}**"

        def _queue_lines(self, page_tracks: List[Track], offset: int) -> List[str]:
            lines: List[str] = []
            for i, t in enumerate(page_tracks, 1):
                idx = offset + i
                lines.append(f"`{idx:02d}.` [{t.title}]({t.web_url}) • {fmt_time(t.duration)} • {t.requested_by}")
            return lines

        def _total_time_line(self) -> str:
            tot_q = self.player.total_queue_seconds()
            return f"**Total queued time:** {fmt_time(tot_q)}"

        def embed(self) -> discord.Embed:
            pages = self._queue_pages()
            page = max(0, min(self.page, len(pages) - 1))
            page_tracks = pages[page]
            offset = page * self.per_page

            e = discord.Embed(
                title="🎶 Queue",
                color=discord.Color.blurple(),
                description=self._nowplaying_line(),
            )

            if page_tracks:
                e.add_field(
                    name=f"Up Next (Page {page+1}/{len(pages)})",
                    value="\n".join(self._queue_lines(page_tracks, offset)),
                    inline=False,
                )
            else:
                e.add_field(name="Up Next", value="(empty)", inline=False)

            e.set_footer(text=self._total_time_line())
            return e

        # --- control enabling/disabling ---
        def _sync_button_states(self):
            pages = self._queue_pages()
            has_prev = self.page > 0
            has_next = self.page < len(pages) - 1

            for child in self.children:
                if isinstance(child, discord.ui.Button):
                    if child.custom_id == "queue_prev":
                        child.disabled = not has_prev
                    elif child.custom_id == "queue_next":
                        child.disabled = not has_next

            # refresh remove select options
            page_tracks = pages[self.page]
            opts = []
            for i, t in enumerate(page_tracks, 1):
                label = f"{(self.page*self.per_page)+i}. {t.title[:90]}"
                opts.append(discord.SelectOption(label=label, value=str((self.page*self.per_page)+i)))
            if not opts:
                opts = [discord.SelectOption(label="(queue empty)", value="-1", default=True)]
            self.remove_select.options = opts
            self.remove_select.disabled = (opts[0].value == "-1")

        # --- live updater ---
        async def _updater(self):
            try:
                while not self._stop_evt.is_set():
                    self._sync_button_states()
                    if self.message:
                        try:
                            await self.message.edit(embed=self.embed(), view=self)
                        except Exception:
                            pass
                    await asyncio.sleep(2)
            except asyncio.CancelledError:
                pass

        async def on_timeout(self):
            self._stop_evt.set()
            if self._refresh_task:
                self._refresh_task.cancel()
            for child in self.children:
                child.disabled = True
            if self.message:
                try:
                    await self.message.edit(view=self)
                except Exception:
                    pass

        async def start(self, message: discord.Message):
            self.message = message
            self._sync_button_states()
            await self.message.edit(embed=self.embed(), view=self)
            self._refresh_task = asyncio.create_task(self._updater())

        # --- callbacks with explicit rows (<=5 total rows) ---
        @discord.ui.button(emoji="◀️", style=discord.ButtonStyle.secondary,
                           custom_id="queue_prev", row=0)
        async def prev_page(self, interaction: discord.Interaction, _: discord.ui.Button):
            self.page = max(0, self.page - 1)
            await interaction.response.edit_message(embed=self.embed(), view=self)

        @discord.ui.button(emoji="▶️", style=discord.ButtonStyle.secondary,
                           custom_id="queue_next", row=0)
        async def next_page(self, interaction: discord.Interaction, _: discord.ui.Button):
            self.page += 1
            await interaction.response.edit_message(embed=self.embed(), view=self)

        @discord.ui.button(emoji="⏯️", style=discord.ButtonStyle.primary, row=0)
        async def pause_resume(self, interaction: discord.Interaction, _: discord.ui.Button):
            if self.player.voice and self.player.voice.is_paused():
                self.player.resume()
            else:
                self.player.pause()
            await interaction.response.edit_message(embed=self.embed(), view=self)

        @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.primary, row=0)
        async def skip(self, interaction: discord.Interaction, _: discord.ui.Button):
            await self.player.skip()
            await interaction.response.edit_message(embed=self.embed(), view=self)

        @discord.ui.button(emoji="🔀", style=discord.ButtonStyle.secondary, row=1)
        async def shuffle(self, interaction: discord.Interaction, _: discord.ui.Button):
            self.player.shuffle()
            self.page = 0
            await interaction.response.edit_message(embed=self.embed(), view=self)

        @discord.ui.button(emoji="🧹", style=discord.ButtonStyle.danger, row=1)
        async def clear(self, interaction: discord.Interaction, _: discord.ui.Button):
            self.player.clear_queue()
            self.page = 0
            await interaction.response.edit_message(embed=self.embed(), view=self)

        @discord.ui.button(emoji="🔉", style=discord.ButtonStyle.secondary, row=1)
        async def vol_down(self, interaction: discord.Interaction, _: discord.ui.Button):
            self.player.set_volume_percent(self.player.volume_percent() - 10)
            await interaction.response.edit_message(embed=self.embed(), view=self)

        @discord.ui.button(emoji="🔊", style=discord.ButtonStyle.secondary, row=1)
        async def vol_up(self, interaction: discord.Interaction, _: discord.ui.Button):
            self.player.set_volume_percent(self.player.volume_percent() + 10)
            await interaction.response.edit_message(embed=self.embed(), view=self)

        async def _on_remove_select(self, interaction: discord.Interaction):
            try:
                idx = int(self.remove_select.values[0])
            except Exception:
                await interaction.response.defer()
                return

            q_len = len(self.player.queue)
            if idx < 1 or idx > q_len:
                await interaction.response.send_message("Invalid selection.", ephemeral=True)
                return

            q_list = list(self.player.queue)
            track = q_list.pop(idx - 1)
            self.player.queue = deque(q_list)

            max_page = max(0, (len(self.player.queue) - 1) // self.per_page)
            self.page = min(self.page, max_page)

            await interaction.response.edit_message(
                content=f"🗑️ Removed **{track.title}**",
                embed=self.embed(),
                view=self,
            )

    # ---- commands ----

    @commands.hybrid_command(
        name="join",
        description="Have the bot join (or move to) your current voice channel",
    )
    @app_commands.guilds(DEV_GUILD)
    async def join(self, ctx: commands.Context):
        it = getattr(ctx, "interaction", None)
        if it and not it.response.is_done():
            await it.response.defer(ephemeral=True)

        vc = self._user_vc(ctx)
        if not isinstance(vc, discord.VoiceChannel):
            await reply(ctx, "❌ You must be in a voice channel.", ephemeral=True)
            return

        current = cast(Optional[discord.VoiceClient], ctx.guild.voice_client)

        try:
            if current and current.is_connected():
                cur_ch = cast(Optional[discord.VoiceChannel], getattr(current, "channel", None))
                if cur_ch and cur_ch.id == vc.id:
                    await reply(ctx, f"✅ Already in {vc.mention}", ephemeral=True)
                else:
                    await current.move_to(vc)
                    await reply(ctx, f"🔄 Moved to {vc.mention}", ephemeral=True)
            else:
                await vc.connect(self_deaf=True)
                await reply(ctx, f"🎶 Joined {vc.mention}", ephemeral=True)
        except Exception as e:
            await reply(ctx, f"⚠️ Failed to connect: `{e}`", ephemeral=True)

    @commands.hybrid_command(name="play", description="Queue a song by URL or search (joins your VC if needed)")
    @app_commands.guilds(DEV_GUILD)
    async def play(self, ctx: commands.Context, *, query: str):
        it = await self._defer_if_interaction(ctx, ephemeral=False)
        player = self.get_player(ctx.guild)

        # remember where to announce "now playing"
        player.announce_channel = ctx.channel  # type: ignore

        if not self._is_voice_connected(ctx.guild):
            try:
                vc = await self._author_vc_or_error(ctx)
            except commands.CommandError as e:
                msg = f"{e}\nTip: use **/join** first."
                if it:
                    await it.followup.send(msg)
                else:
                    await reply(ctx, msg)
                return
            try:
                await player.ensure_connected(vc)
            except Exception as e:
                msg = f"Voice connect failed: {e}"
                if it:
                    await it.followup.send(msg)
                else:
                    await reply(ctx, msg)
                return
        else:
            if player.voice is None or player.voice is not ctx.guild.voice_client:
                player.voice = ctx.guild.voice_client

        try:
            track = await self._resolve_query(query, requester=str(ctx.author))
        except Exception as e:
            msg = f"Failed to fetch: {e}"
            if it:
                await it.followup.send(msg)
            else:
                await reply(ctx, msg)
            return

        await player.add(track)

        msg = (
            f"➕ **Queued:** [{track.title}]({track.web_url}) "
            f"• {fmt_time(track.duration)} • requested by {track.requested_by}"
        )
        if it:
            await it.followup.send(msg)
        else:
            await reply(ctx, msg)

    @commands.hybrid_command(name="queue", description="Show the queue with live controls")
    @app_commands.guilds(DEV_GUILD)
    async def queue_cmd(self, ctx: commands.Context):
        player = self.get_player(ctx.guild)
        view = Music.QueueView(self, player, per_page=8)

        it = getattr(ctx, "interaction", None)
        try:
            if it and not it.response.is_done():
                await it.response.send_message(embed=view.embed(), view=view)  # public
                message = await it.original_response()
            elif it:
                message = await it.followup.send(embed=view.embed(), view=view)
            else:
                message = await ctx.send(embed=view.embed(), view=view)
        except Exception as e:
            # Fallback: non-interactive text
            lines = []
            if player.current:
                pos = player.position_seconds()
                lines.append(f"**Now:** {player.current.title} • {fmt_time(pos)} / {fmt_time(player.current.duration)}")
            for i, t in enumerate(list(player.queue), 1):
                lines.append(f"{i}. {t.title} • {fmt_time(t.duration)} • queued by {t.requested_by}")
            tot_q = player.total_queue_seconds()
            msg = ("Queue is empty." if not lines else "\n".join(lines)) + f"\n\n**Total queued time:** {fmt_time(tot_q)}"
            await reply(ctx, msg)
            log.warning("queue_cmd fell back to text: %r", e)
            return

        await view.start(message)

    @commands.hybrid_command(name="nowplaying", description="Show current song with a progress bar")
    @app_commands.guilds(DEV_GUILD)
    async def nowplaying(self, ctx: commands.Context):
        player = self.get_player(ctx.guild)
        cur = player.current
        if not cur:
            await reply(ctx, "Nothing playing.")
            return
        pos = player.position_seconds()
        frac = 0.0 if not cur.duration else min(1.0, pos / cur.duration)
        bar = progress_bar(frac)
        await reply(
            ctx,
            f"🎵 **{cur.title}**\n{bar}  {fmt_time(pos)} / {fmt_time(cur.duration)}\nQueued by {cur.requested_by}\n{cur.web_url}",
        )

    @commands.hybrid_command(
        name="volume",
        description="Show/set the player volume (0–200%).",
    )
    @app_commands.describe(level="Optional numeric volume (0–200)")
    @app_commands.guilds(DEV_GUILD)
    async def volume(self, ctx: commands.Context, level: Optional[int] = None):
        player = self.get_player(ctx.guild)

        if level is not None:
            pct = player.set_volume_percent(level)
            await reply(ctx, f"🔊 Volume set to **{pct}%**")
            return

        view = Music.VolumeView(player)
        await reply(ctx, content=None, embed=view.embed(), view=view)

    @commands.hybrid_command(name="remove", description="Remove a song at index (see /queue)")
    @app_commands.guilds(DEV_GUILD)
    async def remove(self, ctx: commands.Context, index: int):
        player = self.get_player(ctx.guild)
        q_len = len(player.queue)
        if index < 1 or index > q_len:
            await reply(ctx, f"Invalid index. Queue has {q_len} item(s).", ephemeral=True)
            return
        q_list = list(player.queue)
        track = q_list.pop(index - 1)
        player.queue = deque(q_list)
        await reply(ctx, f"🗑️ Removed **{track.title}**")

    @commands.hybrid_command(name="skip", description="Skip the current song")
    @app_commands.guilds(DEV_GUILD)
    async def skip(self, ctx: commands.Context):
        player = self.get_player(ctx.guild)
        if not player.current:
            await reply(ctx, "Nothing to skip.")
            return
        await player.skip()
        await reply(ctx, "⏭️ Skipped.")

    @commands.hybrid_command(name="stop", description="Stop and clear the queue")
    @app_commands.guilds(DEV_GUILD)
    async def stop(self, ctx: commands.Context):
        player = self.get_player(ctx.guild)
        player.queue.clear()
        await player.skip()
        await reply(ctx, "⏹️ Stopped and cleared the queue.")


async def setup(bot: commands.Bot):
    if hasattr(bot.intents, "voice_states"):
        bot.intents.voice_states = True
    await bot.add_cog(Music(bot))
