from redbot.core.bot import Red
from .boosterrole import BoosterRole


async def setup(bot: Red) -> None:
    await bot.add_cog(BoosterRole(bot))