from .reactionevents import ReactionEvents


async def setup(bot):
    await bot.add_cog(ReactionEvents(bot))
