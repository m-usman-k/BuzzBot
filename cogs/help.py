import discord
from discord import app_commands
from discord.ext import commands

# ── Cog Display Information ───────────────────────────────────────────────────

HOME_VALUE = "__home__"

COG_DISPLAY_INFO = {
    "Levelling": {
        "title": "Levelling Commands",
        "emoji": "🏆",
        "description": "View rank, leaderboards, and configure XP rewards",
        "summary": "A comprehensive levelling system for Discord servers"
    },
    "Welcome": {
        "title": "Welcome Commands",
        "emoji": "👋",
        "description": "Configure welcome channels and custom cards",
        "summary": "A fully customisable welcome system with card generation"
    },
    "AuditLog": {
        "title": "Audit Log Commands",
        "emoji": "📁",
        "description": "Configure moderator action logging channels",
        "summary": "Detailed event logging for administrators and moderators"
    }
}

# ── Helpers ────────────────────────────────────────────────────────────────────

def build_cog_map(bot: commands.Bot) -> dict[str, list]:
    """Return {cog_name: [app_commands]} for cogs containing other commands."""
    cog_map = {}
    for cog_name, cog in bot.cogs.items():
        cmds = cog.get_app_commands()
        if cmds:
            # Exclude help cog/command to keep list clean
            filtered_cmds = [c for c in cmds if c.name != "help"]
            if filtered_cmds:
                cog_map[cog_name] = filtered_cmds
    return cog_map


def build_category_embed(category_name: str, cmds: list, guild: discord.Guild) -> discord.Embed:
    """Build a styled embed for one command category matching the reference design."""
    info = COG_DISPLAY_INFO.get(category_name, {
        "title": f"{category_name} Commands",
        "summary": f"Commands belonging to {category_name}"
    })

    embed = discord.Embed(
        title=info["title"],
        colour=0x3498DB  # Sleek blue border color
    )

    if info["summary"]:
        embed.description = f"{info['summary']}\n\n"
    else:
        embed.description = ""

    lines = []
    # Sort commands alphabetically by name
    for cmd in sorted(cmds, key=lambda c: c.name):
        sig = f"`/{cmd.name}`"
        desc = cmd.description or "No description"
        lines.append(f"{sig}\n└ {desc}")

    embed.description += "\n\n".join(lines) if lines else "No commands available."

    if guild:
        embed.set_footer(
            text=guild.name,
            icon_url=guild.icon.url if guild.icon else None
        )
    else:
        embed.set_footer(text="BuzzBot")

    return embed


def build_home_embed(bot: commands.Bot, guild: discord.Guild | None) -> discord.Embed:
    """Build the default home embed with an overview of the bot."""
    embed = discord.Embed(
        title="BuzzBot",
        colour=0x3498DB,
        description=(
            "BuzzBot is a Discord utility bot for your server. "
            "Use the menu below to browse commands by category.\n"
        ),
    )

    if bot.user:
        embed.set_thumbnail(url=bot.user.display_avatar.url)

    features = []
    for name, info in COG_DISPLAY_INFO.items():
        emoji = info.get("emoji", "•")
        features.append(f"{emoji} **{info['title'].replace(' Commands', '')}** — {info['summary']}")

    if features:
        embed.add_field(
            name="Features",
            value="\n".join(features),
            inline=False,
        )

    embed.add_field(
        name="Getting started",
        value="Select a category from the dropdown to see its slash commands.",
        inline=False,
    )

    if guild:
        embed.set_footer(
            text=guild.name,
            icon_url=guild.icon.url if guild.icon else None,
        )
    else:
        embed.set_footer(text="BuzzBot")

    return embed


# ── UI Components ──────────────────────────────────────────────────────────────

class CategorySelect(discord.ui.Select):
    def __init__(self, cog_map: dict, bot: commands.Bot):
        self.cog_map = cog_map
        self.bot = bot

        options = [
            discord.SelectOption(
                label="Home",
                value=HOME_VALUE,
                emoji="🏠",
                description="About BuzzBot",
            ),
        ]
        for name in cog_map.keys():
            info = COG_DISPLAY_INFO.get(name, {
                "title": f"{name} Commands",
                "emoji": "🤖",
                "description": f"Commands in category {name}"
            })
            options.append(
                discord.SelectOption(
                    label=info["title"],
                    value=name,
                    emoji=info["emoji"],
                    description=info["description"]
                )
            )

        super().__init__(
            placeholder="Choose a section...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        selected = self.values[0]
        if selected == HOME_VALUE:
            embed = build_home_embed(self.bot, interaction.guild)
        else:
            embed = build_category_embed(selected, self.cog_map[selected], interaction.guild)
        await interaction.response.edit_message(embed=embed, view=self.view)


class HelpView(discord.ui.View):
    def __init__(self, cog_map: dict, bot: commands.Bot):
        super().__init__(timeout=120)
        self.add_item(CategorySelect(cog_map, bot))

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


# ── Cog ───────────────────────────────────────────────────────────────────────

class HelpCog(commands.Cog, name="Help"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="help", description="Browse all bot commands by category")
    async def help_command(self, interaction: discord.Interaction):
        cog_map = build_cog_map(self.bot)

        if not cog_map:
            await interaction.response.send_message(
                "No commands found.", ephemeral=True
            )
            return

        embed = build_home_embed(self.bot, interaction.guild)
        view = HelpView(cog_map, self.bot)

        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @commands.Cog.listener()
    async def on_ready(self):
        print("Help cog loaded successfully!")


async def setup(bot: commands.Bot):
    await bot.add_cog(HelpCog(bot))
