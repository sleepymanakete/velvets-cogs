"""
InactivityKick - a Red-DiscordBot cog
--------------------------------------
Kicks members who haven't sent a message in a configurable number of days.

Commands (all under the `inactivity` group, require Manage Server):
    [p]inactivity setdays <n>
    [p]inactivity toggle
    [p]inactivity exemptrole <role>
    [p]inactivity unexemptrole <role>
    [p]inactivity whitelist <member>
    [p]inactivity unwhitelist <member>
    [p]inactivity status
    [p]inactivity check
    [p]inactivity audit
    [p]inactivity forceinactive <member>       (testing helper)
    [p]inactivity previewkick                  (DMs you a sample kick message)
    [p]inactivity runnow
"""

import discord
from redbot.core import commands, Config, checks
from redbot.core.bot import Red
from discord.ext import tasks
from datetime import datetime, timezone, timedelta

COLOR_OK = discord.Color.green()
COLOR_INFO = discord.Color.blurple()
COLOR_WARN = discord.Color.orange()
COLOR_ERROR = discord.Color.red()


def make_embed(title, description="", color=COLOR_INFO, fields=None, footer=True):
    embed = discord.Embed(title=title, description=description, color=color)
    for name, value, inline in (fields or []):
        embed.add_field(name=name, value=value, inline=inline)
    if footer:
        embed.set_footer(text="InactivityKick")
    return embed


class InactivityKick(commands.Cog):
    """Kicks members who haven't been active in a configurable number of days."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0x1AC71EE5, force_registration=True)

        default_guild = {
            "days": 30,
            "enabled": False,
            "exempt_roles": [],
            "whitelist": [],
            "bot_first_seen": None,  # set on first run per-guild
        }
        default_member = {
            "last_seen": None,  # ISO timestamp, set whenever they send a message
        }

        self.config.register_guild(**default_guild)
        self.config.register_member(**default_member)

        self.inactivity_check.start()

    def cog_unload(self):
        self.inactivity_check.cancel()

    # ---------- listeners ----------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.author.bot:
            return
        await self.config.member(message.author).last_seen.set(
            datetime.now(timezone.utc).isoformat()
        )

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        # Start the clock the moment they join, so new members aren't flagged instantly
        await self.config.member(member).last_seen.set(
            datetime.now(timezone.utc).isoformat()
        )

    # ---------- helpers ----------

    async def _bot_first_seen(self, guild: discord.Guild) -> datetime:
        ts = await self.config.guild(guild).bot_first_seen()
        if ts is None:
            now = datetime.now(timezone.utc)
            await self.config.guild(guild).bot_first_seen.set(now.isoformat())
            return now
        return datetime.fromisoformat(ts)

    async def _get_last_seen(self, guild: discord.Guild, member: discord.Member) -> datetime:
        ts = await self.config.member(member).last_seen()
        if ts:
            return datetime.fromisoformat(ts)
        # No recorded message since the bot has been watching -> don't trust join
        # date (it can be years old and has nothing to do with real activity).
        # Use whichever is more recent: when they joined, or when the bot started
        # tracking this guild, so nobody looks falsely ancient right after install.
        first_seen = await self._bot_first_seen(guild)
        joined = member.joined_at or first_seen
        if joined.tzinfo is None:
            joined = joined.replace(tzinfo=timezone.utc)
        return max(joined, first_seen)

    async def _is_exempt(self, member: discord.Member, settings: dict) -> bool:
        if member.bot:
            return True
        if member.guild_permissions.administrator:
            return True
        if member.id in settings["whitelist"]:
            return True
        exempt_role_ids = set(settings["exempt_roles"])
        return any(role.id in exempt_role_ids for role in member.roles)

    async def _find_inactive_members(self, guild: discord.Guild):
        settings = await self.config.guild(guild).all()
        cutoff = datetime.now(timezone.utc) - timedelta(days=settings["days"])
        inactive = []
        for member in guild.members:
            if await self._is_exempt(member, settings):
                continue
            last_seen = await self._get_last_seen(guild, member)
            if last_seen < cutoff:
                inactive.append((member, last_seen))
        return inactive

    # ---------- enforcement (shared by loop + manual trigger) ----------

    async def _process_guild(self, guild: discord.Guild, settings: dict = None) -> int:
        """Kick all currently-inactive members in this guild. Returns how
        many were processed."""
        if settings is None:
            settings = await self.config.guild(guild).all()

        inactive = await self._find_inactive_members(guild)

        for member, last_seen in inactive:
            try:
                unix_last_seen = int(last_seen.timestamp())
                dm_embed = make_embed(
                    f"Removed from {guild.name}",
                    f"You were kicked for inactivity. You haven't posted since "
                    f"<t:{unix_last_seen}:R> (<t:{unix_last_seen}:f>).",
                    COLOR_WARN,
                )
                await member.send(embed=dm_embed)
            except discord.Forbidden:
                pass
            try:
                await guild.kick(member, reason=f"Inactive for {settings['days']}+ days")
            except discord.Forbidden:
                pass

        return len(inactive)

    # ---------- background loop ----------

    @tasks.loop(hours=24)
    async def inactivity_check(self):
        for guild in self.bot.guilds:
            settings = await self.config.guild(guild).all()
            if not settings["enabled"]:
                continue
            await self._process_guild(guild, settings)

    @inactivity_check.before_loop
    async def before_check(self):
        await self.bot.wait_until_red_ready()

    # ---------- commands ----------

    @commands.group(name="inactivity")
    @commands.guild_only()
    @checks.admin_or_permissions(manage_guild=True)
    async def inactivity(self, ctx: commands.Context):
        """Manage inactivity kicking for this server."""
        # Intentionally no body here: Red automatically shows its own help
        # for a group command when no subcommand is invoked.
        pass

    @inactivity.command(name="setdays")
    async def setdays(self, ctx: commands.Context, days: int):
        """Set how many days of silence before a kick."""
        if days < 1:
            await ctx.send(embed=make_embed("Invalid value", "Days must be at least 1.", COLOR_ERROR))
            return
        await self.config.guild(ctx.guild).days.set(days)
        await ctx.send(embed=make_embed(
            "Threshold updated",
            f"Members will be kicked after **{days} days** of inactivity.",
            COLOR_OK,
        ))

    @inactivity.command(name="toggle")
    async def toggle(self, ctx: commands.Context):
        """Turn automatic inactivity kicking on or off."""
        current = await self.config.guild(ctx.guild).enabled()
        new_state = not current
        await self.config.guild(ctx.guild).enabled.set(new_state)
        state = "enabled" if new_state else "disabled"
        await ctx.send(embed=make_embed(
            "Auto-kick toggled",
            f"Automatic inactivity kicking is now **{state}**.",
            COLOR_OK if new_state else COLOR_WARN,
        ))

    @inactivity.command(name="exemptrole")
    async def exemptrole(self, ctx: commands.Context, role: discord.Role):
        """Exempt a role from inactivity kicking (e.g. mods)."""
        async with self.config.guild(ctx.guild).exempt_roles() as roles:
            if role.id not in roles:
                roles.append(role.id)
        await ctx.send(embed=make_embed(
            "Role exempted",
            f"**{role.name}** is now exempt from inactivity kicking.",
            COLOR_OK,
        ))

    @inactivity.command(name="unexemptrole")
    async def unexemptrole(self, ctx: commands.Context, role: discord.Role):
        """Remove a role from the exempt list."""
        async with self.config.guild(ctx.guild).exempt_roles() as roles:
            if role.id in roles:
                roles.remove(role.id)
        await ctx.send(embed=make_embed(
            "Role exemption removed",
            f"**{role.name}** is no longer exempt.",
            COLOR_WARN,
        ))

    @inactivity.command(name="whitelist")
    async def whitelist(self, ctx: commands.Context, member: discord.Member):
        """Exempt a specific person, regardless of role."""
        async with self.config.guild(ctx.guild).whitelist() as wl:
            if member.id not in wl:
                wl.append(member.id)
        await ctx.send(embed=make_embed(
            "Member whitelisted",
            f"{member.mention} won't be kicked for inactivity.",
            COLOR_OK,
        ))

    @inactivity.command(name="unwhitelist")
    async def unwhitelist(self, ctx: commands.Context, member: discord.Member):
        """Remove a person from the whitelist."""
        async with self.config.guild(ctx.guild).whitelist() as wl:
            if member.id in wl:
                wl.remove(member.id)
        await ctx.send(embed=make_embed(
            "Whitelist updated",
            f"{member.mention} removed from the whitelist.",
            COLOR_WARN,
        ))

    @inactivity.command(name="status")
    async def status(self, ctx: commands.Context):
        """Show current inactivity-kick settings."""
        settings = await self.config.guild(ctx.guild).all()
        roles = [ctx.guild.get_role(r) for r in settings["exempt_roles"]]
        roles = [r.name for r in roles if r]
        whitelisted = [ctx.guild.get_member(u) for u in settings["whitelist"]]
        whitelisted = [m.display_name for m in whitelisted if m]

        embed = make_embed(
            "Inactivity Settings",
            color=COLOR_OK if settings["enabled"] else COLOR_INFO,
            fields=[
                ("Threshold", f"{settings['days']} days", True),
                ("Enabled", "✅ Yes" if settings["enabled"] else "❌ No", True),
                ("Exempt roles", ", ".join(roles) if roles else "none", False),
                ("Whitelisted users", ", ".join(whitelisted) if whitelisted else "none", False),
            ],
        )
        await ctx.send(embed=embed)

    @inactivity.command(name="check")
    async def check(self, ctx: commands.Context):
        """Dry run: list who WOULD be kicked right now, without taking action."""
        async with ctx.typing():
            settings = await self.config.guild(ctx.guild).all()
            inactive = await self._find_inactive_members(ctx.guild)

        if not inactive:
            await ctx.send(embed=make_embed(
                "No inactive members",
                f"Everyone has been active within the last {settings['days']} days.",
                COLOR_OK,
            ))
            return

        lines = [f"{m.mention} — last seen {ls.strftime('%Y-%m-%d')}" for m, ls in inactive[:20]]
        description = "\n".join(lines)
        if len(inactive) > 20:
            description += f"\n\n...and {len(inactive) - 20} more."

        embed = make_embed(
            f"{len(inactive)} member(s) would be kicked",
            description,
            COLOR_WARN,
            fields=[("Threshold", f"{settings['days']} days", True)],
        )
        await ctx.send(embed=embed)

    @inactivity.command(name="forceinactive")
    async def forceinactive(self, ctx: commands.Context, member: discord.Member):
        """Testing helper: backdates a member's last-seen timestamp so they
        immediately qualify as inactive under the current threshold, without
        waiting real days. Follow with `check` or `runnow` to test."""
        settings = await self.config.guild(ctx.guild).all()
        fake_last_seen = datetime.now(timezone.utc) - timedelta(days=settings["days"] + 1)
        await self.config.member(member).last_seen.set(fake_last_seen.isoformat())
        await ctx.send(embed=make_embed(
            "Backdated for testing",
            f"{member.display_name}'s last-seen was set to {fake_last_seen.strftime('%Y-%m-%d')}, "
            f"past the current {settings['days']}-day threshold. Run `check` or `runnow` to test.",
            COLOR_WARN,
        ))

    @inactivity.command(name="audit")
    async def audit(self, ctx: commands.Context):
        """Show every member's actual tracked last-seen date, oldest first.
        Useful for verifying the underlying data behind `check`/`runnow`."""
        async with ctx.typing():
            rows = []
            for member in ctx.guild.members:
                if member.bot:
                    continue
                raw_ts = await self.config.member(member).last_seen()
                last_seen = await self._get_last_seen(ctx.guild, member)
                rows.append((member, last_seen, raw_ts is not None))

        rows.sort(key=lambda r: r[1])

        lines = []
        for member, last_seen, has_real_data in rows[:25]:
            tag = "" if has_real_data else " — *no message seen yet, using fallback*"
            lines.append(f"{member.display_name}: {last_seen.strftime('%Y-%m-%d %H:%M UTC')}{tag}")
        description = "\n".join(lines) if lines else "No members found."
        if len(rows) > 25:
            description += f"\n\n...and {len(rows) - 25} more."

        untracked = sum(1 for _, _, real in rows if not real)
        embed = make_embed(
            "Activity Audit",
            description,
            COLOR_INFO,
            fields=[
                ("Total members", str(len(rows)), True),
                ("Never seen posting", str(untracked), True),
            ],
        )
        await ctx.send(embed=embed)

    @inactivity.command(name="previewkick")
    async def previewkick(self, ctx: commands.Context):
        """DMs you a preview of the kick message, exactly as a real member
        would see it, without kicking anyone."""
        settings = await self.config.guild(ctx.guild).all()
        last_seen = await self._get_last_seen(ctx.guild, ctx.author)
        unix_last_seen = int(last_seen.timestamp())

        preview_embed = make_embed(
            f"Removed from {ctx.guild.name}",
            f"You were kicked for inactivity. You haven't posted since "
            f"<t:{unix_last_seen}:R> (<t:{unix_last_seen}:f>).",
            COLOR_WARN,
        )

        try:
            await ctx.author.send(
                content="👀 This is a **preview only** — you were not kicked.",
                embed=preview_embed,
            )
        except discord.Forbidden:
            await ctx.send(embed=make_embed(
                "Couldn't DM you",
                "I can't send you a DM — check that you allow DMs from server members.",
                COLOR_ERROR,
            ))
            return

        await ctx.send(embed=make_embed(
            "Preview sent",
            "Check your DMs — sent a preview of the kick message using your own last-seen date.",
            COLOR_OK,
        ))

    @inactivity.command(name="runnow")
    async def runnow(self, ctx: commands.Context):
        """Immediately kick everyone currently inactive — not a dry run.
        Run `[p]inactivity check` first if you want to preview who's affected."""
        settings = await self.config.guild(ctx.guild).all()

        async with ctx.typing():
            inactive = await self._find_inactive_members(ctx.guild)

        if not inactive:
            await ctx.send(embed=make_embed(
                "No inactive members",
                f"Everyone has been active within the last {settings['days']} days. Nothing to do.",
                COLOR_OK,
            ))
            return

        await ctx.send(embed=make_embed(
            f"Processing {len(inactive)} member(s)...",
            f"About to kick {len(inactive)} member(s) inactive for {settings['days']}+ days. This may take a moment.",
            COLOR_WARN,
        ))

        async with ctx.typing():
            count = await self._process_guild(ctx.guild, settings)

        await ctx.send(embed=make_embed(
            "Done",
            f"Finished processing {count} member(s).",
            COLOR_OK,
        ))
