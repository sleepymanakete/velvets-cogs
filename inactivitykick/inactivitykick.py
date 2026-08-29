import discord
from redbot.core import commands, Config
from datetime import datetime, timezone, timedelta
import asyncio

class InactivityKick(commands.Cog):
    """Tracks user activity and checks for inactive members."""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=1459283749, force_registration=True)
        default_member = {
            "last_active": None
        }
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
        """Inactivity management commands."""
        pass

    @inactivity.command(name="scan")
    async def scan_history(self, ctx: commands.Context, limit_per_channel: int = 1000):
        """Scans past channel history to backfill everyone's last message timestamp.
        
        Usage: [p]inactivity scan [limit_per_channel] (default: 1000 messages/channel)
        """
        status_msg = await ctx.send("🔍 Scanning channel history to find everyone's last message... This might take a minute.")
        
        # Dictionary to keep track of highest timestamp per user ID in memory first
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

        # Save all found timestamps to Red's Config
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

    @inactivity.command(name="listinactive")
    async def list_inactive(self, ctx: commands.Context, days: int = 30):
        """List members who haven't sent a message in X days."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        cutoff_ts = int(cutoff.timestamp())

        all_members_data = await self.config.all_members(ctx.guild)
        inactive = []

        for member in ctx.guild.members:
            if member.bot:
                continue

            last_active = all_members_data.get(member.id, {}).get("last_active")
            
            if last_active is None:
                if member.joined_at and member.joined_at < cutoff:
                    inactive.append(f"{member.mention} (No messages found, joined <t:{int(member.joined_at.timestamp())}:R>)")
            elif last_active < cutoff_ts:
                inactive.append(f"{member.mention} (Last active <t:{last_active}:R>)")

        if not inactive:
            await ctx.send(f"No members found inactive for more than {days} days.")
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
