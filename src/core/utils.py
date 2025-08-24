from __future__ import annotations
from typing import Any, Optional, Union
import discord
from discord.ext import commands

CtxLike = Union[commands.Context, discord.Interaction]

DEV_GUILD = 1264827735318990920
#1407453882644303922
# 905150802778271785

async def reply(
        target: CtxLike,
        content: Optional[str] = None,
        *,
        ephemeral: bool = False,
        mention_author: bool = False,
        **kwargs: Any,
) -> discord.Message | None:
    """
    Send a message to either a Context or an Interaction.
    - For slash: first reply uses interaction.response.send_message
      later replies use interaction.followup.send
    - For prefix: uses ctx.reply
    Returns the created Message when available, or None for ephemeral first replies.
    """
    if isinstance(target, commands.Context):
        # ctx.reply does not support `ephemeral`; ignore it for prefix usage
        return await target.reply(content, mention_author=mention_author, **kwargs)

    # target is an Interaction
    if not target.response.is_done():
        # First response
        await target.response.send_message(content, ephemeral=ephemeral, **kwargs)
        if ephemeral:
            return None  # Discord does not return a Message for the first ephemeral response
        return await target.original_response()
    else:
        # Follow-up response
        return await target.followup.send(content, ephemeral=ephemeral, **kwargs)


async def defer_if_needed(interaction: discord.Interaction, *, ephemeral: bool = False) -> None:
    """
    Defer if you will take longer than ~2 seconds before replying.
    Safe to call once. No-op if already responded or deferred.
    """
    if isinstance(interaction, discord.Interaction) and not interaction.response.is_done():
        await interaction.response.defer(ephemeral=ephemeral)


async def edit_original(
        interaction: discord.Interaction,
        content: Optional[str] = None,
        **kwargs: Any,
) -> discord.Message:
    """
    Edit the original response for a slash command.
    Works for both normal and ephemeral originals.
    """
    return await interaction.edit_original_response(content=content, **kwargs)
