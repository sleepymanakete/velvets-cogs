import asyncio
import random
import time
from typing import Optional

import discord
from redbot.core import Config, bank, commands, checks
from redbot.core.bot import Red
from redbot.core.utils.chat_formatting import box, humanize_number, humanize_timedelta

DEFAULT_GUILD = {
    "enabled": False,
    "channels": [],  # channel ids eligible for chest spawns
    "spawn_chance": 0.02,  # chance per eligible message that a chest spawns
    "cooldown": 300,  # seconds min between spawns, guild-wide
    "min_reward": 50,
    "max_reward": 666,
    "max_claims": 3,
    "expires_after": 300,  # seconds the chest stays open
    "currency_emoji": "\U0001F4B0",  # 💰 shown next to reward amounts (display only)
    "chest_emoji": "\U0001F381",  # 🎁
    "last_spawn": 0,
}


class ClaimView(discord.ui.View):
    """Persistent-ish view attached to a single chest message."""

    def __init__(self, cog: "ChestDrop", guild_id: int, message_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.guild_id = guild_id
        self.message_id = message_id

    @discord.ui.button(label="Claim Chest", style=discord.ButtonStyle.success, emoji="\U0001F381")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.handle_claim(interaction, self)


class ChestDrop(commands.Cog):
    """Randomly spawn claimable currency chests in chat, à la Naya's chest drops."""

    __version__ = "1.0.0"

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0xC4E57D20, force_registration=True)
        self.config.register_guild(**DEFAULT_GUILD)

        # active chests: message_id -> state dict
        self._active_chests = {}
        self._locks = {}

    def cog_unload(self):
        for task in getattr(self, "_expiry_tasks", {}).values():
            task.cancel()

    # ---------------------------------------------------------------- #
    # Listener: chance-based chest spawns
    # ---------------------------------------------------------------- #

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.author.bot:
            return

        guild = message.guild
        settings = await self.config.guild(guild).all()

        if not settings["enabled"]:
            return
        if message.channel.id not in settings["channels"]:
            return

        now = time.time()
        if now - settings["last_spawn"] < settings["cooldown"]:
            return

        if random.random() > settings["spawn_chance"]:
            return

        await self.config.guild(guild).last_spawn.set(now)
        await self.spawn_chest(message.channel, settings)

    # ---------------------------------------------------------------- #
    # Chest spawning / claiming
    # ---------------------------------------------------------------- #

    async def spawn_chest(self, channel: discord.TextChannel, settings: dict):
        currency = await bank.get_currency_name(channel.guild)
        c_emoji = settings["currency_emoji"]
        chest_emoji = settings["chest_emoji"]
        max_claims = settings["max_claims"]
        lo, hi = settings["min_reward"], settings["max_reward"]

        embed = discord.Embed(
            title=f"{chest_emoji} Chest — {c_emoji} {currency}",
            description=(
                "Chests may randomly appear in selected channels.\n"
                f"Be quick and **Claim Chest** to earn rewards!\n"
                f"Admins can configure spawn rates, rewards, and appearance via "
                f"`{ctx_prefix(self.bot)}currencysettings`.\n\n"
                f"**Status**\n`0/{max_claims}` claimed\n\n"
                f"**Reward**\n{c_emoji} `{lo}-{hi}`\n\n"
                f"**Expires**\n<t:{int(time.time() + settings['expires_after'])}:R>\n\n"
                f"**Claimers**\n*none yet*"
            ),
            colour=discord.Colour.gold(),
        )

        view = ClaimView(self, channel.guild.id, message_id=0)
        try:
            msg = await channel.send(embed=embed, view=view)
        except discord.Forbidden:
            return
        view.message_id = msg.id

        self._active_chests[msg.id] = {
            "guild_id": channel.guild.id,
            "channel_id": channel.id,
            "claims": [],  # list of (member_id, amount)
            "max_claims": max_claims,
            "min_reward": lo,
            "max_reward": hi,
            "currency_name": currency,
            "currency_emoji": c_emoji,
            "chest_emoji": chest_emoji,
            "closed": False,
        }
        self._locks[msg.id] = asyncio.Lock()

        self.bot.loop.create_task(
            self._expire_chest(msg, settings["expires_after"])
        )

    async def _expire_chest(self, message: discord.Message, delay: int):
        await asyncio.sleep(delay)
        state = self._active_chests.get(message.id)
        if not state or state["closed"]:
            return
        await self._close_chest(message, state)

    async def handle_claim(self, interaction: discord.Interaction, view: ClaimView):
        message_id = view.message_id
        state = self._active_chests.get(message_id)

        if state is None or state["closed"]:
            await interaction.response.send_message(
                "This chest has already closed.", ephemeral=True
            )
            return

        lock = self._locks.setdefault(message_id, asyncio.Lock())
        async with lock:
            state = self._active_chests.get(message_id)
            if state is None or state["closed"]:
                await interaction.response.send_message(
                    "This chest has already closed.", ephemeral=True
                )
                return

            claimer_ids = {uid for uid, _ in state["claims"]}
            if interaction.user.id in claimer_ids:
                await interaction.response.send_message(
                    "You've already claimed this chest.", ephemeral=True
                )
                return

            if len(state["claims"]) >= state["max_claims"]:
                await interaction.response.send_message(
                    "This chest has no claims left.", ephemeral=True
                )
                return

            amount = random.randint(state["min_reward"], state["max_reward"])

            try:
                await bank.deposit_credits(interaction.user, amount)
            except (ValueError, RuntimeError) as e:
                # ValueError: would exceed the bank's max balance for this user
                # RuntimeError: bank operations disabled for this user/context
                await interaction.response.send_message(
                    f"Couldn't deposit your reward: {e}", ephemeral=True
                )
                return

            state["claims"].append((interaction.user.id, amount))

            await interaction.response.send_message(
                f"You claimed {state['currency_emoji']} **{humanize_number(amount)} "
                f"{state['currency_name']}**!",
                ephemeral=True,
            )

            await self._refresh_embed(interaction.message, state)

            if len(state["claims"]) >= state["max_claims"]:
                await self._close_chest(interaction.message, state)

    async def _refresh_embed(self, message: discord.Message, state: dict):
        claimed = len(state["claims"])
        max_claims = state["max_claims"]

        if state["claims"]:
            lines = []
            for uid, amount in state["claims"]:
                lines.append(
                    f"<@{uid}> — +{state['currency_emoji']} {humanize_number(amount)}"
                )
            claimers_text = "\n".join(lines)
        else:
            claimers_text = "*none yet*"

        embed = message.embeds[0] if message.embeds else discord.Embed()
        prefix_desc = embed.description.split("**Status**")[0] if embed.description else ""

        embed.description = (
            f"{prefix_desc}"
            f"**Status**\n`{claimed}/{max_claims}` claimed\n\n"
            f"**Reward**\n{state['currency_emoji']} `{state['min_reward']}-{state['max_reward']}`\n\n"
            f"**Claimers**\n{claimers_text}"
        )
        try:
            await message.edit(embed=embed)
        except discord.HTTPException:
            pass

    async def _close_chest(self, message: discord.Message, state: dict):
        state["closed"] = True
        view = ClaimView(self, state["guild_id"], message.id)
        for child in view.children:
            child.disabled = True
            child.label = "Chest Closed"
            child.style = discord.ButtonStyle.secondary
        try:
            await message.edit(view=view)
        except discord.HTTPException:
            pass
        self._active_chests.pop(message.id, None)
        self._locks.pop(message.id, None)

    # Balances now go through Red's core Economy/bank system directly —
    # use the bot's own `[p]balance` command, this cog no longer tracks
    # a separate currency of its own.

    # ---------------------------------------------------------------- #
    # Admin configuration
    # ---------------------------------------------------------------- #

    @commands.guild_only()
    @checks.admin_or_permissions(manage_guild=True)
    @commands.group()
    async def currencysettings(self, ctx: commands.Context):
        """Configure chest drop spawn rates, rewards, and appearance."""

    @currencysettings.command(name="toggle")
    async def cs_toggle(self, ctx: commands.Context, on_off: Optional[bool] = None):
        """Enable or disable chest spawning in this server."""
        guild_conf = self.config.guild(ctx.guild)
        if on_off is None:
            on_off = not await guild_conf.enabled()
        await guild_conf.enabled.set(on_off)
        await ctx.send(f"Chest spawning is now **{'enabled' if on_off else 'disabled'}**.")

    @currencysettings.command(name="channel")
    async def cs_channel(self, ctx: commands.Context, channel: discord.TextChannel, add_remove: str):
        """Add or remove a channel as eligible for chest spawns.

        `add_remove` must be `add` or `remove`.
        """
        add_remove = add_remove.lower()
        if add_remove not in ("add", "remove"):
            await ctx.send("Please specify `add` or `remove`.")
            return

        async with self.config.guild(ctx.guild).channels() as channels:
            if add_remove == "add":
                if channel.id not in channels:
                    channels.append(channel.id)
                    await ctx.send(f"{channel.mention} added as a chest-eligible channel.")
                else:
                    await ctx.send(f"{channel.mention} is already eligible.")
            else:
                if channel.id in channels:
                    channels.remove(channel.id)
                    await ctx.send(f"{channel.mention} removed from chest-eligible channels.")
                else:
                    await ctx.send(f"{channel.mention} wasn't in the list.")

    @currencysettings.command(name="chance")
    async def cs_chance(self, ctx: commands.Context, percent: float):
        """Set the percent chance (0-100) a chest spawns per eligible message."""
        if not 0 <= percent <= 100:
            await ctx.send("Percent must be between 0 and 100.")
            return
        await self.config.guild(ctx.guild).spawn_chance.set(percent / 100)
        await ctx.send(f"Spawn chance set to **{percent}%** per eligible message.")

    @currencysettings.command(name="cooldown")
    async def cs_cooldown(self, ctx: commands.Context, seconds: int):
        """Set the minimum cooldown (seconds) between chest spawns, server-wide."""
        if seconds < 0:
            await ctx.send("Cooldown can't be negative.")
            return
        await self.config.guild(ctx.guild).cooldown.set(seconds)
        await ctx.send(f"Spawn cooldown set to **{humanize_timedelta(seconds=seconds)}**.")

    @currencysettings.command(name="reward")
    async def cs_reward(self, ctx: commands.Context, minimum: int, maximum: int):
        """Set the min/max reward range for a claimed chest."""
        if minimum < 0 or maximum < minimum:
            await ctx.send("Make sure `0 <= minimum <= maximum`.")
            return
        await self.config.guild(ctx.guild).min_reward.set(minimum)
        await self.config.guild(ctx.guild).max_reward.set(maximum)
        await ctx.send(f"Reward range set to **{minimum}-{maximum}**.")

    @currencysettings.command(name="maxclaims")
    async def cs_maxclaims(self, ctx: commands.Context, amount: int):
        """Set how many members can claim a single chest."""
        if amount < 1:
            await ctx.send("Max claims must be at least 1.")
            return
        await self.config.guild(ctx.guild).max_claims.set(amount)
        await ctx.send(f"Chests will now allow up to **{amount}** claim(s) each.")

    @currencysettings.command(name="expiry")
    async def cs_expiry(self, ctx: commands.Context, seconds: int):
        """Set how long (seconds) a chest stays open before closing."""
        if seconds < 10:
            await ctx.send("Expiry should be at least 10 seconds.")
            return
        await self.config.guild(ctx.guild).expires_after.set(seconds)
        await ctx.send(f"Chests will now stay open for **{humanize_timedelta(seconds=seconds)}**.")

    @currencysettings.command(name="currencyemoji")
    async def cs_currencyemoji(self, ctx: commands.Context, emoji: str):
        """Set the emoji used to represent the currency."""
        await self.config.guild(ctx.guild).currency_emoji.set(emoji)
        await ctx.send(f"Currency emoji set to {emoji}.")

    @currencysettings.command(name="chestemoji")
    async def cs_chestemoji(self, ctx: commands.Context, emoji: str):
        """Set the emoji used to represent the chest itself."""
        await self.config.guild(ctx.guild).chest_emoji.set(emoji)
        await ctx.send(f"Chest emoji set to {emoji}.")

    @currencysettings.command(name="settings")
    async def cs_settings(self, ctx: commands.Context):
        """Show the current chest-drop configuration for this server."""
        s = await self.config.guild(ctx.guild).all()
        currency_name = await bank.get_currency_name(ctx.guild)
        channels = ", ".join(f"<#{c}>" for c in s["channels"]) or "none"
        msg = (
            f"Enabled: {s['enabled']}\n"
            f"Channels: {channels}\n"
            f"Spawn chance: {s['spawn_chance'] * 100:.2f}%\n"
            f"Cooldown: {humanize_timedelta(seconds=s['cooldown'])}\n"
            f"Reward range: {s['min_reward']}-{s['max_reward']}\n"
            f"Max claims per chest: {s['max_claims']}\n"
            f"Expiry: {humanize_timedelta(seconds=s['expires_after'])}\n"
            f"Currency: {s['currency_emoji']} {currency_name} (managed by Red's bank/Economy)\n"
            f"Chest emoji: {s['chest_emoji']}"
        )
        await ctx.send(box(msg, lang="yaml"))

    @currencysettings.command(name="spawn")
    @checks.admin_or_permissions(manage_guild=True)
    async def cs_spawn(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None):
        """Manually spawn a chest right now, for testing."""
        channel = channel or ctx.channel
        settings = await self.config.guild(ctx.guild).all()
        await self.spawn_chest(channel, settings)
        await ctx.tick()


def ctx_prefix(bot: Red) -> str:
    """Best-effort prefix string for embed text (falls back to '[p]')."""
    try:
        prefixes = bot.command_prefix
        if isinstance(prefixes, str):
            return prefixes
        if isinstance(prefixes, (list, tuple)) and prefixes:
            return prefixes[0]
    except Exception:
        pass
    return "[p]"
