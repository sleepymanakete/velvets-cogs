"""
ReactionEvents - a Red-DiscordBot cog
---------------------------------------
NadekoBot-style reaction currency events. An admin starts an event funding
a pot from their own balance; members claim a fixed share by reacting to
the announcement message, until the pot runs out or time expires. Any
unclaimed pot is refunded to whoever started it.

Uses Red's built-in bank API, so it works with whatever economy setup
you already have (global or per-server currency).

Commands (require Manage Server):
    [p]eventstart reaction -a <amount per user> -p <pot total> -d <duration hours>
    [p]eventstart emoji <emoji>      - set the claim emoji for this server
    [p]eventend <message_id>         - end an active event early, refunding the rest
"""

import asyncio
import re
from datetime import datetime, timedelta, timezone

import discord
from redbot.core import commands, Config, checks, bank
from redbot.core.bot import Red

COLOR_OK = discord.Color.green()
COLOR_INFO = discord.Color.blurple()
COLOR_WARN = discord.Color.orange()
COLOR_ERROR = discord.Color.red()

DEFAULT_EMOJI = "🎉"

ARG_PATTERN = re.compile(
    r"(?:-a\s+(?P<amount>\d+))|(?:-p\s+(?P<pot>\d+))|(?:-d\s+(?P<duration>\d+(?:\.\d+)?))"
)


def parse_event_args(raw: str):
    """Parses '-a 100 -p 5000 -d 2' into (amount, pot, duration_hours)."""
    amount = pot = duration = None
    for match in ARG_PATTERN.finditer(raw):
        if match.group("amount"):
            amount = int(match.group("amount"))
        elif match.group("pot"):
            pot = int(match.group("pot"))
        elif match.group("duration"):
            duration = float(match.group("duration"))
    return amount, pot, duration


def make_embed(title, description="", color=COLOR_INFO, fields=None):
    embed = discord.Embed(title=title, description=description, color=color)
    for name, value, inline in (fields or []):
        embed.add_field(name=name, value=value, inline=inline)
    embed.set_footer(text="ReactionEvents")
    return embed


class ReactionEvents(commands.Cog):
    """NadekoBot-style reaction currency events."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0x9F3D21AB, force_registration=True)
        self.config.register_guild(emoji=DEFAULT_EMOJI)
        self.active_events = {}  # message_id -> event dict

    def cog_unload(self):
        for event in self.active_events.values():
            task = event.get("task")
            if task and not task.done():
                task.cancel()

    # ---------- embed builder ----------

    def _build_event_embed(self, amount, pot_total, pot_remaining, end_time, emoji, ended, participant_count=0):
        if ended:
            title = "🎉 Reaction Event Ended!"
            description = "This event is no longer accepting reactions."
            color = COLOR_WARN
            time_left = "Ended"
        else:
            title = "✨ REACTION EVENT STARTED! ✨"
            description = f"React to this message with {emoji} to claim your reward!"
            color = COLOR_OK
            remaining_seconds = max(0, (end_time - datetime.now(timezone.utc)).total_seconds())
            hours = int(remaining_seconds // 3600)
            minutes = int((remaining_seconds % 3600) // 60)
            time_left = f"{hours}h {minutes}m"

        embed = discord.Embed(title=title, description=description, color=color)
        embed.add_field(name="💰 Reward", value=f"{amount} Currency", inline=True)
        embed.add_field(name="🏆 Remaining Pot", value=f"{pot_remaining} Currency", inline=True)
        embed.add_field(name="⏳ Time Left", value=time_left, inline=True)
        embed.add_field(name=f"{emoji} Claimed", value=str(participant_count), inline=False)
        embed.set_footer(text="ReactionEvents")
        return embed

    # ---------- commands ----------

    @commands.group(name="eventstart")
    @commands.guild_only()
    @checks.admin_or_permissions(manage_guild=True)
    async def eventstart(self, ctx: commands.Context):
        """Start a currency event."""
        pass

    @eventstart.command(name="reaction")
    async def eventstart_reaction(self, ctx: commands.Context, *, args: str):
        """
        Start a reaction event.

        Usage: [p]eventstart reaction -a <amount per user> -p <pot total> -d <duration hours>
        Example: [p]eventstart reaction -a 100 -p 5000 -d 2
        """
        amount, pot, duration = parse_event_args(args)
        if amount is None or pot is None or duration is None:
            await ctx.send(embed=make_embed(
                "Invalid arguments",
                "Usage: `-a <amount per user> -p <pot total> -d <duration hours>`\n"
                "Example: `-a 100 -p 5000 -d 2`",
                COLOR_ERROR,
            ))
            return
        if amount <= 0 or pot <= 0 or duration <= 0:
            await ctx.send(embed=make_embed("Invalid arguments", "All values must be greater than 0.", COLOR_ERROR))
            return
        if amount > pot:
            await ctx.send(embed=make_embed("Invalid arguments", "Amount per user can't exceed the pot.", COLOR_ERROR))
            return

        currency_name = await bank.get_currency_name(ctx.guild)

        if not await bank.can_spend(ctx.author, pot):
            await ctx.send(embed=make_embed(
                "Insufficient funds",
                f"You need **{pot} {currency_name}** to fund this event.",
                COLOR_ERROR,
            ))
            return

        await bank.withdraw_credits(ctx.author, pot)

        emoji = await self.config.guild(ctx.guild).emoji()
        end_time = datetime.now(timezone.utc) + timedelta(hours=duration)

        embed = self._build_event_embed(amount, pot, pot, end_time, emoji, ended=False)
        msg = await ctx.send(embed=embed)
        try:
            await msg.add_reaction(emoji)
        except discord.HTTPException:
            pass

        event = {
            "guild_id": ctx.guild.id,
            "channel_id": ctx.channel.id,
            "message_id": msg.id,
            "starter_id": ctx.author.id,
            "amount": amount,
            "pot_total": pot,
            "pot_remaining": pot,
            "emoji": emoji,
            "end_time": end_time,
            "participants": set(),
            "ended": False,
        }
        self.active_events[msg.id] = event
        event["task"] = asyncio.create_task(self._end_event_later(msg.id, duration))

    @eventstart.command(name="emoji")
    async def eventstart_emoji(self, ctx: commands.Context, emoji: str):
        """Set the emoji used for reaction events in this server."""
        await self.config.guild(ctx.guild).emoji.set(emoji)
        await ctx.send(embed=make_embed(
            "Emoji updated",
            f"Reaction events will now use {emoji}",
            COLOR_OK,
        ))

    @commands.command(name="eventend")
    @commands.guild_only()
    @checks.admin_or_permissions(manage_guild=True)
    async def eventend(self, ctx: commands.Context, message_id: int):
        """End an active reaction event early, refunding whatever's left."""
        event = self.active_events.get(message_id)
        if event is None or event["guild_id"] != ctx.guild.id:
            await ctx.send(embed=make_embed("Not found", "No active event with that message ID.", COLOR_ERROR))
            return
        await self._end_event(message_id)
        await ctx.send(embed=make_embed("Event ended", "The event was ended early.", COLOR_OK))

    # ---------- reaction handling ----------

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.message_id not in self.active_events:
            return
        event = self.active_events[payload.message_id]
        if event["ended"]:
            return
        if payload.user_id == self.bot.user.id:
            return
        if str(payload.emoji) != event["emoji"]:
            return
        if payload.user_id in event["participants"]:
            return
        if payload.user_id == event["starter_id"]:
            return  # starter can't claim from their own pot

        remaining = event["pot_remaining"]
        amount = event["amount"]
        if remaining < amount:
            return  # pot exhausted, ignore further claims

        guild = self.bot.get_guild(event["guild_id"])
        if guild is None:
            return
        member = guild.get_member(payload.user_id)
        if member is None:
            return

        event["participants"].add(payload.user_id)
        event["pot_remaining"] -= amount
        await bank.deposit_credits(member, amount)

        channel = guild.get_channel(event["channel_id"])
        if channel:
            try:
                msg = await channel.fetch_message(payload.message_id)
                embed = self._build_event_embed(
                    amount, event["pot_total"], event["pot_remaining"],
                    event["end_time"], event["emoji"], ended=False,
                    participant_count=len(event["participants"]),
                )
                await msg.edit(embed=embed)
            except discord.HTTPException:
                pass

        if event["pot_remaining"] < amount:
            await self._end_event(payload.message_id)

    # ---------- lifecycle ----------

    async def _end_event_later(self, message_id, duration_hours):
        try:
            await asyncio.sleep(duration_hours * 3600)
            await self._end_event(message_id)
        except asyncio.CancelledError:
            pass

    async def _end_event(self, message_id):
        event = self.active_events.get(message_id)
        if event is None or event["ended"]:
            return
        event["ended"] = True

        guild = self.bot.get_guild(event["guild_id"])
        channel = guild.get_channel(event["channel_id"]) if guild else None
        if channel:
            try:
                msg = await channel.fetch_message(message_id)
                embed = self._build_event_embed(
                    event["amount"], event["pot_total"], event["pot_remaining"],
                    event["end_time"], event["emoji"], ended=True,
                    participant_count=len(event["participants"]),
                )
                await msg.edit(embed=embed)
            except discord.HTTPException:
                pass

        # Refund whatever's left in the pot back to whoever started it
        leftover = event["pot_remaining"]
        if leftover > 0 and guild:
            starter = guild.get_member(event["starter_id"])
            if starter:
                await bank.deposit_credits(starter, leftover)
                if channel:
                    currency_name = await bank.get_currency_name(guild)
                    await channel.send(embed=make_embed(
                        "Pot refunded",
                        f"💸 {leftover} unclaimed {currency_name} refunded to {starter.mention}.",
                        COLOR_INFO,
                    ))

        task = event.get("task")
        if task and not task.done():
            task.cancel()

        self.active_events.pop(message_id, None)
