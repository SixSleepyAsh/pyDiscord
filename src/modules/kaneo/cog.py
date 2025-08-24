from __future__ import annotations

import asyncio, json, logging
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Any, Tuple, List
from urllib.parse import quote

import aiohttp
import discord
from discord.ext import commands
from discord import app_commands
from src.core.utils import reply
from core.utils import DEV_GUILD

CONFIG_PATH = Path("config/kaneo.json")
DEFAULT_POLL_SEC = 30

# ---------- logging ----------
log = logging.getLogger("kaneo")
if not log.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

def _normalize_base(url: str) -> str:
    u = (url or "").strip()
    if not u:
        return ""
    if "://" not in u:
        u = "https://" + u
    return u.rstrip("/")

def _join(base: str, prefix: str, path: str) -> str:
    base = _normalize_base(base)
    prefix = (prefix or "").strip("/")
    path = path.lstrip("/")
    return f"{base}/{prefix}/{path}" if prefix else f"{base}/{path}"

@dataclass
class GuildConfig:
    base_url: str = ""
    poll_sec: int = DEFAULT_POLL_SEC
    path_prefix: str = ""           # e.g. "", "api", or "v1"
    email: str = ""                 # for session login
    password: str = ""              # for session login
    routes: Dict[str, int] = None   # project -> channel_id
    since: Dict[str, str] = None    # project -> cursor
    # cache: chosen feed path per project after probe (so we don’t probe every poll)
    _feed: Dict[str, str] = None    # project -> relative path (without base/prefix)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "base_url": self.base_url,
            "poll_sec": self.poll_sec,
            "path_prefix": self.path_prefix,
            "email": self.email,
            "password": self.password,
            "routes": self.routes or {},
            "since": self.since or {},
            "_feed": self._feed or {},
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "GuildConfig":
        gc = GuildConfig()
        gc.base_url = d.get("base_url", "")
        gc.poll_sec = int(d.get("poll_sec", DEFAULT_POLL_SEC))
        gc.path_prefix = d.get("path_prefix", "")
        gc.email = d.get("email", "")
        gc.password = d.get("password", "")
        gc.routes = {k: int(v) for k, v in (d.get("routes") or {}).items()}
        gc.since  = {k: str(v) for k, v in (d.get("since") or {}).items()}
        gc._feed  = {k: str(v) for k, v in (d.get("_feed") or {}).items()}
        return gc


class Kaneo(commands.Cog):
    """Poll Kaneo API and post project updates into mapped channels."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._cfg: Dict[int, GuildConfig] = {}
        self._session: Optional[aiohttp.ClientSession] = None
        self._poll_task: Optional[asyncio.Task] = None

        self._ensure_config_file()
        self._load_config()

    # -------------------- file persistence --------------------

    def _ensure_config_file(self):
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        if not CONFIG_PATH.exists():
            CONFIG_PATH.write_text(json.dumps({}, indent=2))
            log.info("Created config file at %s", CONFIG_PATH)

    def _load_config(self):
        try:
            if CONFIG_PATH.exists():
                data = json.loads(CONFIG_PATH.read_text())
                self._cfg = {int(gid): GuildConfig.from_dict(cfg) for gid, cfg in data.items()}
                log.info("Loaded config for %d guild(s)", len(self._cfg))
        except Exception as e:
            log.exception("Failed to load config: %s", e)

    def _save_config(self):
        try:
            data = {str(gid): cfg.to_dict() for gid, cfg in self._cfg.items()}
            CONFIG_PATH.write_text(json.dumps(data, indent=2))
            log.info("Saved config (%d guilds)", len(self._cfg))
        except Exception as e:
            log.exception("Failed to save config: %s", e)

    # -------------------- lifecycle --------------------

    async def cog_load(self):
        if not self._session:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
            log.info("HTTP session created")
        if not self._poll_task:
            self._poll_task = asyncio.create_task(self._poll_loop())
            log.info("Started poll loop")

    async def cog_unload(self):
        if self._poll_task:
            self._poll_task.cancel()
            log.info("Stopped poll loop")
        if self._session:
            await self._session.close()
            log.info("HTTP session closed")

    # -------------------- HTTP helpers --------------------

    async def _login_if_needed(self, cfg: GuildConfig) -> bool:
        """If email/password present, POST to sign-in and store cookie in session."""
        if not cfg.email or not cfg.password:
            return True  # no auth required
        url = _join(cfg.base_url, cfg.path_prefix, "sign-in")
        try:
            async with self._session.post(url, json={"email": cfg.email, "password": cfg.password}) as resp:
                body = await resp.text()
                if resp.status == 200:
                    log.info("Login OK for %s", cfg.email)
                    return True
                log.warning("Login failed %s: %s", resp.status, body[:300])
                return False
        except Exception as e:
            log.exception("Login error: %s", e)
            return False

    async def _api_request(self, method: str, cfg: GuildConfig, path: str, *,
                           params: Optional[dict] = None,
                           json_body: Optional[dict] = None) -> tuple[int, Any, str]:
        if not self._session:
            raise RuntimeError("HTTP session not initialized")
        url = _join(cfg.base_url, cfg.path_prefix, path)
        try:
            async with self._session.request(method, url,
                                             headers={"Accept": "application/json"},
                                             params=params, json=json_body) as resp:
                status = resp.status
                text = await resp.text()
                try:
                    data = json.loads(text)
                except Exception:
                    data = text
                log.debug("%s %s -> %s", method, url, status)
                return status, data, text
        except Exception as e:
            log.exception("%s %s failed: %s", method, url, e)
            return 0, None, ""

    async def _api_get(self, cfg: GuildConfig, path: str, params: Optional[dict] = None) -> tuple[int, Any, str]:
        return await self._api_request("GET", cfg, path, params=params)

    async def _api_post(self, cfg: GuildConfig, path: str, json_body: Optional[dict] = None) -> tuple[int, Any, str]:
        return await self._api_request("POST", cfg, path, json_body=json_body)

    async def _check_connectivity(self, cfg: GuildConfig) -> tuple[bool, str]:
        """Login (if provided), then try a couple of simple endpoints to confirm auth + prefix."""
        if not await self._login_if_needed(cfg):
            return False, "login failed"

        # Try a few likely endpoints (GET), accept 200 with any JSON (not just {user:null})
        candidates = ["health", "app-info", "me"]
        for p in candidates:
            status, data, raw = await self._api_get(cfg, p)
            if status == 200 and (raw.strip() != '{"user":null}'):
                return True, f"OK {p}"
        # Fallback: even if we still see {user:null}, consider URL reachable
        status, _, _ = await self._api_get(cfg, "health")
        if status == 200:
            return True, "OK health (content ambiguous)"
        return False, "No reachable endpoint (try prefix or auth)"

    async def _project_exists(self, cfg: GuildConfig, project: str) -> tuple[bool, str]:
        """Try a few shapes to verify the project exists."""
        slug = quote(project, safe="")
        # 1) REST-ish
        for path in (f"projects/{slug}", f"project/{slug}"):
            status, data, raw = await self._api_get(cfg, path)
            if status == 200 and raw.strip() != '{"user":null}':
                return True, "found"
            if status == 404:
                return False, "not found"
        # 2) Controller-style GET with query
        status, data, raw = await self._api_get(cfg, "project/controllers/get-project", params={"slug": project})
        if status == 200 and raw.strip() != '{"user":null}':
            return True, "found (controller)"
        # 3) Controller-style POST
        status, data, raw = await self._api_post(cfg, "project/controllers/get-project", json_body={"slug": project})
        if status == 200 and raw.strip() != '{"user":null}':
            return True, "found (controller-post)"
        return False, f"unexpected status (last={status})"

    # -------------------- admin / setup --------------------

    @app_commands.command(
        name="kaneo_setup",
        description="Configure Kaneo API (base URL, prefix, optional login). Verifies access.",
    )
    @app_commands.guilds(DEV_GUILD)
    @app_commands.describe(
        base_url="http(s)://host[:port] (no trailing slash)",
        path_prefix="Route prefix",
        email="Login email (optional)",
        password="Login password (optional)",
        poll_sec="Polling interval (s)",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def kaneo_setup(self,
                          interaction: discord.Interaction,
                          base_url: str,
                          path_prefix: Optional[str] = "",
                          email: Optional[str] = "",
                          password: Optional[str] = "",
                          poll_sec: Optional[int] = None):
        gid = interaction.guild_id
        assert gid is not None
        tmp_cfg = GuildConfig(
            base_url=_normalize_base(base_url),
            path_prefix=(path_prefix or "").strip(),
            email=(email or "").strip(),
            password=(password or ""),
            poll_sec=max(10, poll_sec or DEFAULT_POLL_SEC),
        )

        await reply(interaction, "Testing Kaneo connectivity…", ephemeral=True)
        ok, msg = await self._check_connectivity(tmp_cfg)
        log.info("kaneo_setup connectivity: %s (%s)", ok, msg)

        if not ok:
            await reply(interaction, f"❌ Could not reach Kaneo: {msg}", ephemeral=True)
            return

        cfg = self._cfg.get(gid, GuildConfig())
        cfg.base_url = tmp_cfg.base_url
        cfg.path_prefix = tmp_cfg.path_prefix
        cfg.email = tmp_cfg.email
        cfg.password = tmp_cfg.password
        cfg.poll_sec = tmp_cfg.poll_sec
        self._cfg[gid] = cfg
        self._save_config()
        await reply(interaction,
                    f"✅ Kaneo configured.\nBase: `{cfg.base_url}`"
                    f"\nPrefix: `{cfg.path_prefix or '(none)'}`"
                    f"\nPoll: `{cfg.poll_sec}s`"
                    f"\nCheck: `{msg}`",
                    ephemeral=True)

    @app_commands.command(name="kaneo_link", description="Route a Kaneo project to a channel (verifies project exists)")
    @app_commands.guilds(DEV_GUILD)
    @app_commands.describe(project="Project key/slug", channel="Destination channel for updates")
    @app_commands.default_permissions(manage_guild=True)
    async def kaneo_link(self, interaction: discord.Interaction, project: str, channel: discord.TextChannel):
        gid = interaction.guild_id
        assert gid is not None
        cfg = self._cfg.get(gid)
        if not cfg or not cfg.base_url:
            await reply(interaction, "Run /kaneo_setup first.", ephemeral=True)
            return

        # ensure session if needed
        if not await self._login_if_needed(cfg):
            await reply(interaction, "Login failed (check email/password).", ephemeral=True)
            return

        await reply(interaction, f"Checking project `{project}`…", ephemeral=True)
        exists, why = await self._project_exists(cfg, project)
        log.info("kaneo_link project check %s -> %s", project, why)

        if not exists:
            await reply(interaction, f"❌ Project `{project}` {why}.", ephemeral=True)
            return

        cfg.routes = cfg.routes or {}
        cfg._feed = cfg._feed or {}
        cfg.routes[project] = channel.id
        # reset chosen feed path so it probes on first poll
        cfg._feed.pop(project, None)
        self._cfg[gid] = cfg
        self._save_config()
        await reply(interaction, f"🔗 `{project}` → {channel.mention} (linked)", ephemeral=True)

    @app_commands.command(name="kaneo_unlink", description="Stop routing a project")
    @app_commands.guilds(DEV_GUILD)
    @app_commands.describe(project="Project key/slug")
    @app_commands.default_permissions(manage_guild=True)
    async def kaneo_unlink(self, interaction: discord.Interaction, project: str):
        gid = interaction.guild_id
        assert gid is not None
        cfg = self._cfg.get(gid, GuildConfig())
        if cfg.routes and project in cfg.routes:
            cfg.routes.pop(project)
            if cfg._feed:
                cfg._feed.pop(project, None)
            self._cfg[gid] = cfg
            self._save_config()
            await reply(interaction, f"🗑️ Unlinked `{project}`", ephemeral=True)
        else:
            await reply(interaction, f"`{project}` is not linked.", ephemeral=True)

    @commands.hybrid_command(name="kaneo_status", description="Show Kaneo routes & settings")
    async def kaneo_status(self, ctx: commands.Context):
        gid = ctx.guild.id
        cfg = self._cfg.get(gid)
        if not cfg:
            await reply(ctx, "Kaneo not configured.")
            return
        lines = [
            f"Base URL: `{cfg.base_url or 'not set'}`",
            f"Prefix: `{cfg.path_prefix or '(none)'}`",
            f"Poll: `{cfg.poll_sec}s`",
            "**Routes:**" if cfg.routes else "No routes.",
        ]
        if cfg.routes:
            for p, ch in cfg.routes.items():
                feed = (cfg._feed or {}).get(p, "?")
                lines.append(f"• `{p}` → <#{ch}>  (since: `{(cfg.since or {}).get(p, '-')}`, feed:`{feed}`)")
        await reply(ctx, "\n".join(lines))

    @commands.hybrid_command(name="kaneo_test", description="Send a test message for a project")
    async def kaneo_test(self, ctx: commands.Context, project: str, *, text: str = "Test message"):
        gid = ctx.guild.id
        cfg = self._cfg.get(gid)
        routes = (cfg.routes or {}) if cfg else {}
        ch_id = routes.get(project)
        if not cfg or not ch_id:
            await reply(ctx, f"`{project}` isn’t linked.")
            return
        ch = ctx.guild.get_channel(ch_id)
        if isinstance(ch, discord.TextChannel):
            await ch.send(self._format_message(project, {"type": "test", "title": text, "by": str(ctx.author)}))
            await reply(ctx, "Sent.")
        else:
            await reply(ctx, "Linked channel not found. Re-link the project.")

    # -------------------- Polling --------------------

    async def _poll_loop(self):
        try:
            while True:
                tasks = [
                    self._poll_guild(gid, cfg)
                    for gid, cfg in list(self._cfg.items())
                    if cfg.base_url and cfg.routes
                ]
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
                interval = min(
                    (cfg.poll_sec for cfg in self._cfg.values() if cfg.base_url and cfg.routes),
                    default=DEFAULT_POLL_SEC,
                )
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            return

    async def _probe_feed_for_project(self, cfg: GuildConfig, project: str) -> Optional[str]:
        """Find a working feed path for this project. Returns relative path WITHOUT base/prefix."""
        slug = quote(project, safe="")
        # Try REST-ish events first
        candidates: List[Tuple[str, str]] = [
            (f"projects/{slug}/events", "events"),
            (f"projects/{slug}/tasks",  "tasks"),
            # Controller-style activity/tasks—try GET then POST
            ("activity/controllers/get-activities", "activities?q"),
            ("task/controllers/get-tasks", "tasks?q"),
        ]

        # GET attempts
        for path, tag in candidates[:2]:
            status, data, raw = await self._api_get(cfg, path)
            if status == 200 and raw.strip() != '{"user":null}' and self._looks_like_list(data):
                log.info("Feed for %s -> GET %s", project, path)
                return path

        # Controller GET with query param
        for path, tag in candidates[2:4]:
            status, data, raw = await self._api_get(cfg, path, params={"project": project})
            if status == 200 and raw.strip() != '{"user":null}' and self._looks_like_list(data):
                log.info("Feed for %s -> GET %s?project=", project, path)
                return f"{path}?project={{project}}"

        # Controller POST with JSON
        for path, tag in candidates[2:4]:
            status, data, raw = await self._api_post(cfg, path, json_body={"project": project})
            if status == 200 and raw.strip() != '{"user":null}' and self._looks_like_list(data):
                log.info("Feed for %s -> POST %s (project in body)", project, path)
                return f"POST {path}"

        log.warning("No feed path worked for %s", project)
        return None

    @staticmethod
    def _looks_like_list(data: Any) -> bool:
        return isinstance(data, list) and (len(data) == 0 or isinstance(data[0], (dict, str, int)))

    async def _fetch_events(self, cfg: GuildConfig, project: str, since: Optional[str]) -> List[Dict[str, Any]]:
        """Use cached feed path if available; otherwise probe. Normalize output to event dicts."""
        cfg._feed = cfg._feed or {}
        feed = cfg._feed.get(project)
        if not feed:
            feed = await self._probe_feed_for_project(cfg, project)
            if feed:
                cfg._feed[project] = feed
                self._save_config()
            else:
                return []

        # Execute feed
        slug = quote(project, safe="")
        params = {}
        if since:
            # common params used by various feeds
            params = {"since": since, "updated_since": since, "cursor": since}

        # 1) Simple GET path: "projects/<slug>/events" or "projects/<slug>/tasks"
        if not feed.startswith("POST") and "?" not in feed:
            path = feed.replace("{slug}", slug).replace("{project}", project)
            status, data, raw = await self._api_get(cfg, path, params=params or None)
        # 2) Controller GET with query: "activity/controllers/get-activities?project={project}"
        elif not feed.startswith("POST") and "?" in feed:
            base_path, _ = feed.split("?", 1)
            q = {"project": project}
            q.update({"since": since} if since else {})
            status, data, raw = await self._api_get(cfg, base_path, params=q)
        # 3) Controller POST: "POST path"
        else:
            path = feed.split(" ", 1)[1]
            payload = {"project": project}
            if since:
                payload["since"] = since
                payload["updated_since"] = since
            status, data, raw = await self._api_post(cfg, path, json_body=payload)

        if status != 200 or raw.strip() == '{"user":null}':
            log.warning("Feed call failed for %s via %s (status %s)", project, feed, status)
            # Next time, force re-probe
            cfg._feed.pop(project, None)
            self._save_config()
            return []

        # Normalize
        return self._normalize_events(data)

    async def _poll_guild(self, gid: int, cfg: GuildConfig):
        # ensure logged in (if creds provided)
        if not await self._login_if_needed(cfg):
            log.warning("Skipping poll for gid=%s due to login failure", gid)
            return

        for project, channel_id in list((cfg.routes or {}).items()):
            since = (cfg.since or {}).get(project)

            try:
                events = await self._fetch_events(cfg, project, since)
            except Exception as e:
                log.error("Poll %s failed: %s", project, e)
                continue

            if not events:
                log.debug("Poll %s: no new events", project)
                continue

            guild = self.bot.get_guild(gid)
            ch = guild.get_channel(channel_id) if guild else None
            if not isinstance(ch, discord.TextChannel):
                log.warning("Poll %s: channel %s not found", project, channel_id)
                continue

            for ev in events:
                try:
                    await ch.send(self._format_message(project, ev))
                except Exception as e:
                    log.exception("Failed to post event in #%s: %s", ch.id, e)
                cursor = str(ev.get("id") or ev.get("timestamp") or ev.get("ts") or ev.get("updated_at") or "")
                if cursor:
                    cfg.since = cfg.since or {}
                    cfg.since[project] = cursor

        self._cfg[gid] = cfg
        self._save_config()

    # -------------------- Formatting & normalization --------------------

    @staticmethod
    def _normalize_events(data: Any) -> List[Dict[str, Any]]:
        """
        Accepts:
        - list of 'events' dicts
        - list of 'tasks' dicts
        - list of 'activities' dicts
        Returns a list of dicts with keys: id, title, by, type, url, timestamp, details
        """
        if isinstance(data, dict) and "events" in data:
            items = data["events"]
        elif isinstance(data, list):
            items = data
        else:
            return []

        out: List[Dict[str, Any]] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            # try multiple common shapes
            ev_id = it.get("id") or it.get("key") or it.get("uid") or it.get("ts") or it.get("timestamp")
            title = it.get("title") or it.get("summary") or it.get("name") or it.get("task_title") or "Update"
            author = it.get("by") or it.get("author") or it.get("user") or it.get("assignee") or ""
            typ = it.get("type") or it.get("event") or it.get("status") or "update"
            url = it.get("url") or it.get("link") or it.get("href")
            ts = it.get("timestamp") or it.get("ts") or it.get("updated_at") or it.get("created_at")
            details = it.get("details") or it.get("description") or it.get("body") or it.get("comment")

            out.append({
                "id": ev_id,
                "title": title,
                "by": author,
                "type": typ,
                "url": url,
                "timestamp": ts,
                "details": details[:500] + "…" if isinstance(details, str) and len(details) > 500 else details,
            })
        return out

    @staticmethod
    def _format_message(project: str, ev: Dict[str, Any]) -> str:
        title = ev.get("title") or "Update"
        typ = ev.get("type") or "update"
        by = ev.get("by") or ""
        url = ev.get("url")
        details = ev.get("details")
        pieces = [f"**[{project}] {title}**  •  *{typ}*"]
        if by:
            pieces.append(f"by **{by}**")
        if url:
            pieces.append(url)
        if details:
            pieces.append(details if len(details) <= 500 else details[:500] + "…")
        return "\n".join(pieces)


async def setup(bot: commands.Bot):
    await bot.add_cog(Kaneo(bot))
