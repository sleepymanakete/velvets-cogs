import discord
from redbot.core import commands, Config
from datetime import datetime, timezone, timedelta
import asyncio

class InactivityKick(commands.Cog):
    """Tracks user activity, manages lurkers in a holding channel, and checks for inactive members."""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=1459283749, force_registration=True)
        
        default_guild = {
            "days": 30,
            "holding_channel": None,
            "lurker_role": None,
            "whitelist_roles": []
        }
        default_member = {
            "last_active": None
        }
        self.config.register_guild(**default_guild)
        self.config.register_member(**default_member)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Passively update timestamps as new messages arrive."""
        if not message.guild or message.author.bot:
            return

        now_ts = int(datetime.now(timezone.utc).timestamp())
        await self.config.member(message.author).last_active.set(now_ts)

    @commands.guild_only()
    @commands.admin_or_permissions(kick_members=True)
    @commands.group(name="inactivity")
    async def inactivity(self, ctx: commands.Context):
        """Inactivity and lurker management commands."""
        pass

    @inactivity.command(name="setholdingchannel")
    async def set_holding_channel(self, ctx: commands.Context, channel: discord.TextChannel = None):
        """Set or remove the holding channel for lurkers/inactive members."""
        if channel is None:
            await self.config.guild(ctx.guild).holding_channel.set(None)
            await ctx.send("Holding channel cleared.")
        else:
            await self.config.guild(ctx.guild).holding_channel.set(channel.id)
            await ctx.send(f"Holding channel set to {channel.mention}.")

    @inactivity.command(name="setlurkerrole")
    async def set_lurker_role(self, ctx: commands.Context, role: discord.Role = None):
        """Set or remove the lurker role assigned to inactive members."""
        if role is None:
            await self.config.guild(ctx.guild).lurker_role.set(None)
            await ctx.send("Lurker role cleared.")
        else:
            await self.config.guild(ctx.guild).lurker_role.set(role.id)
            await ctx.send(f"Lurker role set to **{role.name}**.")

    @inactivity.command(name="setdays")
    async def set_days(self, ctx: commands.Context, days: int):
        """Set the default inactivity cutoff threshold in days."""
        if days < 1:
            await ctx.send("Threshold must be at least 1 day.")
            return
        await self.config.guild(ctx.guild).days.set(days)
        await ctx.send(f"Inactivity threshold set to **{days}** days.")

    @inactivity.command(name="whitelist")
    async def set_whitelist(self, ctx: commands.Context, role: discord.Role):
        """Toggle role exemption from inactivity actions."""
        async with self.config.guild(ctx.guild).whitelist_roles() as roles:
            if role.id in roles:
                roles.remove(role.id)
                await ctx.send(f"Removed **{role.name}** from the inactivity whitelist.")
            else:
                roles.append(role.id)
                await ctx.send(f"Added **{role.name}** to the inactivity whitelist.")

    @inactivity.command(name="settings")
    async def show_settings(self, ctx: commands.Context):
        """Show the current configuration for this server."""
        guild_data = await self.config.guild(ctx.guild).all()
        
        channel = ctx.guild.get_channel(guild_data["holding_channel"])
        holding_str = channel.mention if channel else "None"
        
        role = ctx.guild.get_role(guild_data["lurker_role"])
        lurker_str = role.name if role else "None"
        
        whitelist_str = ", ".join(
            [r.name for rid in guild_data["whitelist_roles"] if (r := ctx.guild.get_role(rid))]
        ) or "None"

        embed = discord.Embed(title="Inactivity Kick & Lurker Settings", color=discord.Color.blue())
        embed.add_field(name="Cutoff Days", value=f"{guild_data['days']} days", inline=False)
        embed.add_field(name="Holding Channel", value=holding_str, inline=False)
        embed.add_field(name="Lurker Role", value=lurker_str, inline=False)
        embed.add_field(name="Whitelisted Roles", value=whitelist_str, inline=False)
        
        await ctx.send(embed=embed)

    @inactivity.command(name="scan")
    async def scan_history(self, ctx: commands.Context, limit_per_channel: int = 1000):
        """Scans past channel history to backfill everyone's last message timestamp."""
        status_msg = await ctx.send("🔍 Scanning channel history to find everyone's last message... This might take a minute.")
        
        latest_timestamps = {}
        channels = [
            ch for ch in ctx.guild.text_channels 
            if ch.permissions_for(ctx.guild.me).read_message_history and ch.permissions_for(ctx.guild.me).read_messages
        ]

        for channel in channels:
            try:
                async for message in channel.history(limit=limit_per_channel):
                    if message.author.bot:
                        continue
                    msg_ts = int(message.created_at.replace(tzinfo=timezone.utc).timestamp())
                    if message.author.id not in latest_timestamps or msg_ts > latest_timestamps[message.author.id]:
                        latest_timestamps[message.author.id] = msg_ts
            except (discord.Forbidden, discord.HTTPException):
                continue

        for user_id, ts in latest_timestamps.items():
            member = ctx.guild.get_member(user_id)
            if member:
                current_saved = await self.config.member(member).last_active()
                if current_saved is None or ts > current_saved:
                    await self.config.member(member).last_active.set(ts)

        await status_msg.edit(content=f"✅ Scan complete! Logged activity for **{len(latest_timestamps)}** members across **{len(channels)}** channels.")

    @inactivity.command(name="check")
    async def check_user(self, ctx: commands.Context, member: discord.Member):
        """Check when a specific user was last active."""
        last_active = await self.config.member(member).last_active()
        
        if not last_active:
            await ctx.send(f"{member.display_name} has no message history found.")
            return

        discord_time = f"<t:{last_active}:R>"
        await ctx.send(f"{member.display_name} last sent a message {discord_time}.")

    @inactivity.command(name="listinactive", aliases=["list"])
    async def list_inactive(self, ctx: commands.Context, days: int = None):
        """List members who haven't sent a message past the cutoff threshold."""
        if days is None:
            days = await self.config.guild(ctx.guild).days()

        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        cutoff_ts = int(cutoff.timestamp())
        whitelist_roles = await self.config.guild(ctx.guild).whitelist_roles()
        all_members_data = await self.config.all_members(ctx.guild)
        
        inactive = []

        for member in ctx.guild.members:
            if member.bot or any(r.id in whitelist_roles for r in member.roles):
                continue

            last_active = all_members_data.get(member.id, {}).get("last_active")
            
            if last_active is None:
                if member.joined_at and member.joined_at < cutoff:
                    inactive.append(f"{member.mention} (No messages found, joined <t:{int(member.joined_at.timestamp())}:R>)")
            elif last_active < cutoff_ts:
                inactive.append(f"{member.mention} (Last active <t:{last_active}:R>)")

        if not inactive:
            await ctx.send(f"No inactive members found beyond {days} days.")
            return

        report = "\n".join(inactive[:20])
        total = len(inactive)
        
        embed = discord.Embed(
            title=f"Inactive Members (> {days} days)",
            description=report,
            color=discord.Color.red()
        )
        if total > 20:
            embed.set_footer(text=f"Showing top 20 of {total} inactive members.")
        
        await ctx.send(embed=embed)
