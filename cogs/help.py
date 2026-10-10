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
        "summary": "A comprehensive levelling system for Discord servers",
        "setup": (
            "**Setup Guide**\n"
            "1. `/levels channel #channel`  sets the channel for level up messages.\n"
            "2. `/levels rewards add @role text_level voice_level`  grants roles on level up.\n"
            "3. XP is earned automatically from messages and voice activity.\n"
            "4. `/rank` and `/top` let users track their progress."
        )
    },
    "Welcome": {
        "title": "Welcome Commands",
        "emoji": "👋",
        "description": "Configure welcome channels and custom cards",
        "summary": "A fully customisable welcome system with card generation",
        "setup": (
            "**Setup Guide**\n"
            "1. `/welcome channel #channel`  sets where welcome cards are sent.\n"
            "2. *(optional)* `/welcome background ./path/to/image.png`  custom card background.\n"
            "3. `/welcome test`  previews the card for yourself.\n"
            "4. Welcome cards are sent automatically when new members join."
        )
    },
    "AuditLog": {
        "title": "Audit Log Commands",
        "emoji": "📁",
        "description": "Configure moderator action logging channels",
        "summary": "Detailed event logging for administrators and moderators",
        "setup": (
            "**Setup Guide**\n"
            "1. `/logs channel audit logs #audit-logs`  logs server events.\n"
            "2. `/logs channel mod logs #mod-logs`  logs AutoMod actions, timeouts, kicks and bans.\n"
            "3. Both channels are optional, set only what you need.\n"
            "4. Logging starts immediately with no further setup required."
        )
    },
    "AutoMod": {
        "title": "AutoMod Commands",
        "emoji": "🛡️",
        "description": "Configure spam, language and link filters",
        "summary": "Automatic content filtering and moderation actions",
        "setup": (
            "**Setup Guide**\n"
            "1. `/automod status`  opens the settings panel. Everything is set from there: pick a filter to enable it, change its limits and timeouts, and choose immune roles and channels.\n"
            "2. Pick a mod log channel on the panel's front page so you can see what AutoMod does.\n"
            "3. Language filter: add banned words to `data/banned_words.txt`.\n"
            "4. Malicious links: add `VIRUSTOTAL_API_KEY` to `.env`, pick a report channel in the panel and edit `data/link_whitelist.txt` if needed."
        )
    },
    "Moderation": {
        "title": "Moderation Commands",
        "emoji": "📋",
        "description": "Look up a user's punishment history",
        "summary": "Keeps a record of every timeout, kick and ban",
        "setup": (
            "**Setup Guide**\n"
            "1. Give the bot the `View Audit Log` permission.\n"
            "2. Punishments are recorded automatically, whether a moderator or AutoMod hands them out.\n"
            "3. `/user @member`  shows their history, newest first, with buttons to page through it.\n"
            "4. On first start the bot imports the last 45 days of punishments from the audit log."
        )
    },
    "YouTube": {
        "title": "YouTube Commands",
        "emoji": "📺",
        "description": "Upload notifications for YouTube channels",
        "summary": "Posts a message and pings a role whenever a YouTube channel uploads",
        "setup": (
            "**Setup Guide**\n"
            "1. `/youtube setup #channel @role`  sets where notifications go and who gets pinged.\n"
            "2. `/youtube subscribe @handle`  follows a YouTube channel (handle, link or channel ID).\n"
            "3. `/youtube test`  posts the latest upload without pinging, to check it looks right.\n"
            "4. New uploads are checked for every few minutes automatically."
        )
    }
}

# ── Helpers ────────────────────────────────────────────────────────────────────

def flatten_commands(cmds: list) -> list[tuple[str, str]]:
    """Flatten command groups into (signature, description) tuples."""
    result = []
    for cmd in sorted(cmds, key=lambda c: c.name):
        if isinstance(cmd, app_commands.Group):
            for sub in sorted(cmd.commands, key=lambda c: c.name):
                if isinstance(sub, app_commands.Group):
                    for leaf in sorted(sub.commands, key=lambda c: c.name):
                        sig = f"/{cmd.name} {sub.name} {leaf.name}"
                        result.append((sig, leaf.description or "No description"))
                else:
                    sig = f"/{cmd.name} {sub.name}"
                    result.append((sig, sub.description or "No description"))
        else:
            sig = f"/{cmd.name}"
            result.append((sig, cmd.description or "No description"))
    return result


def build_cog_map(bot: commands.Bot) -> dict[str, list]:
    """Return {cog_name: [app_commands]} for cogs containing other commands."""
    cog_map = {}
    for cog_name, cog in bot.cogs.items():
        cmds = cog.get_app_commands()
        if cmds:
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
        colour=0x3498DB
    )

    parts = []
    if info["summary"]:
        parts.append(info["summary"])

    if info.get("setup"):
        parts.append(info["setup"])

    parts.append("")  # blank line before commands heading
    parts.append("**Commands:**")

    flat = flatten_commands(cmds)
    lines = [f"`{sig}`\n  {desc}" for sig, desc in flat]
    parts.append("\n\n".join(lines) if lines else "No commands available.")

    embed.description = "\n\n".join(parts)

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
        features.append(f"{emoji} **{info['title'].replace(' Commands', '')}**  {info['summary']}")

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
