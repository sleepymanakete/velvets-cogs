from .inactivitykick import InactivityKick


async def setup(bot):
    await bot.add_cog(InactivityKick(bot))
