from .chestdrop import ChestDrop


async def setup(bot):
    await bot.add_cog(ChestDrop(bot))
