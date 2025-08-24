import importlib
import pkgutil
from pathlib import Path

from discord.ext import commands
import discord
from .config import Settings
from .registry import Registry
import logging


class LumiBot(commands.Bot):
    def __init__(self, settings: Settings, registry: Registry) -> None:
        intents = discord.Intents.default()
        intents.voice_states = True
        intents.message_content = True  # needed for prefix commands
        super().__init__(command_prefix="!", intents=intents)
        self.settings = settings
        self.registry = registry

    async def on_ready(self):
        print(f"Logged in as {self.user} (id={self.user.id})")

    async def setup_hook(self) -> None:
        await self.load_all_extensions("src.modules")

        def dump(label, cmds):
            names = [c.qualified_name for c in cmds]
            print(f"{label}: {len(names)} -> {names}")

        if self.settings.guild_ids:
            for gid in self.settings.guild_ids:
                gobject = discord.Object(id=gid)
                dump(f"Pre-sync (guild {gid})", self.tree.get_commands(guild=gobject))
                synced = await self.tree.sync(guild=gobject)
                print(f"Synced {len(synced)} commands to guild {gid}")
        else:
            dump("Pre-sync (global)", self.tree.get_commands())
            synced = await self.tree.sync()
            print(f"Synced {len(synced)} global commands")

    async def load_all_extensions(self, root_pkg: str) -> None:
        try:
            pkg = importlib.import_module(root_pkg)
        except ModuleNotFoundError as e:
            logging.error(f"Cannot import {root_pkg}: {e}")
            return

        found = []

        # 1) Try pkgutil walk (preferred)
        pkg_path = getattr(pkg, "__path__", None)
        if pkg_path:
            for mod in pkgutil.walk_packages(pkg_path, pkg.__name__ + "."):
                if mod.name.endswith(".cog"):
                    found.append(mod.name)

        # 2) Fallback: scan the filesystem for *.py under the package dir
        if not found:
            logging.warning("pkgutil found no modules; falling back to filesystem scan")
            # Resolve the on-disk path for the package
            try:
                pkg_file = Path(importlib.import_module(root_pkg).__file__).parent
            except Exception:
                pkg_file = None

            if pkg_file and pkg_file.exists():
                for py in pkg_file.rglob("*.py"):
                    if py.name == "__init__.py":
                        continue
                    # Expect files named cog.py
                    if py.stem == "cog":
                        # turn /path/src/modules/ping/cog.py into src.modules.ping.cog
                        rel = py.with_suffix("").relative_to(Path.cwd())
                        dotted = ".".join(rel.parts)
                        # normalize: if your project root is already on sys.path and src is a package, dotted will start with 'src.'
                        found.append(dotted)

        # 3) Load everything we found
        if not found:
            logging.warning("No extensions found under %s", root_pkg)
            return

        for ext in sorted(set(found)):
            try:
                await self.load_extension(ext)
                logging.info("Loaded extension %s", ext)
            except Exception:
                logging.exception("Failed to load %s", ext)