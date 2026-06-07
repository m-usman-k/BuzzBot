import discord
from discord.ext import commands
from discord import app_commands
import os
import json
from PIL import Image, ImageDraw, ImageFont, ImageFilter
import io
import aiohttp

from config import WELCOME_IMAGE_SIZE, WELCOME_BACKGROUND_PATH, WELCOME_AVATAR_SIZE

# Welcome card palette (BuzzBot gold + Discord-style dark UI)
_COLOUR_GOLD = (255, 193, 7)
_COLOUR_GOLD_SOFT = (255, 214, 102)
_COLOUR_TEXT = (255, 255, 255)
_COLOUR_MUTED = (181, 186, 193)
_COLOUR_CARD = (32, 34, 40, 230)
_COLOUR_PILL = (48, 52, 60, 255)
_RING_WIDTH = 4
_RING_SEP = 2
_AVATAR_SUPERSAMPLE = 4

_FONT_REGULAR = [
    'C:/Windows/Fonts/segoeui.ttf',
    '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
    '/System/Library/Fonts/Supplemental/Arial.ttf',
    './data/arial.ttf',
]
_FONT_BOLD = [
    'C:/Windows/Fonts/segoeuib.ttf',
    '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
    '/System/Library/Fonts/Supplemental/Arial Bold.ttf',
    './data/arialbd.ttf',
]


class Welcome(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.data_dir = 'data'
        self.settings_file = os.path.join(self.data_dir, 'welcome_settings.json')
        self.init_data_files()

    # ------------------------------------------------------------------ #
    #  Data helpers (mirrors the pattern used in levelling.py)            #
    # ------------------------------------------------------------------ #

    def init_data_files(self):
        """Initialise JSON data files."""
        os.makedirs(self.data_dir, exist_ok=True)
        if not os.path.exists(self.settings_file):
            self.save_json(self.settings_file, {})

    def load_json(self, filepath):
        """Load JSON data from file."""
        try:
            if os.path.exists(filepath):
                with open(filepath, 'r', encoding='utf-8') as f:
                    return json.load(f)
            return {}
        except (json.JSONDecodeError, IOError):
            return {}

    def save_json(self, filepath, data):
        """Save JSON data to file."""
        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            return True
        except IOError:
            return False

    def get_welcome_settings(self, guild_id):
        """Get welcome settings for a guild."""
        data = self.load_json(self.settings_file)
        guild_id_str = str(guild_id)
        if guild_id_str in data:
            return data[guild_id_str]
        return {'channel_id': None, 'background_path': WELCOME_BACKGROUND_PATH}

    def set_welcome_setting(self, guild_id, key, value):
        """Set a single welcome setting for a guild."""
        data = self.load_json(self.settings_file)
        guild_id_str = str(guild_id)
        if guild_id_str not in data:
            data[guild_id_str] = {}
        data[guild_id_str][key] = value
        self.save_json(self.settings_file, data)

    # ------------------------------------------------------------------ #
    #  Image generation                                                   #
    # ------------------------------------------------------------------ #

    def _load_font(self, size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
        """Load a system font with sensible fallbacks."""
        cache_key = ('bold' if bold else 'regular', size)
        if not hasattr(self, '_font_cache'):
            self._font_cache = {}
        if cache_key in self._font_cache:
            return self._font_cache[cache_key]

        for path in (_FONT_BOLD if bold else _FONT_REGULAR):
            if os.path.exists(path):
                font = ImageFont.truetype(path, size)
                self._font_cache[cache_key] = font
                return font

        font = ImageFont.load_default()
        self._font_cache[cache_key] = font
        return font

    @staticmethod
    def _truncate(text: str, max_len: int) -> str:
        if len(text) <= max_len:
            return text
        return text[: max_len - 3] + '...'

    @staticmethod
    def _text_size(draw: ImageDraw.ImageDraw, text: str, font) -> tuple[int, int]:
        bbox = draw.textbbox((0, 0), text, font=font)
        return bbox[2] - bbox[0], bbox[3] - bbox[1]

    def _create_gradient_background(self, width: int, height: int) -> Image.Image:
        """Dark gradient with a soft gold glow (BuzzBot default)."""
        img = Image.new('RGB', (width, height))
        draw = ImageDraw.Draw(img)
        top = (22, 24, 34)
        bottom = (10, 11, 18)
        for y in range(height):
            t = y / height
            colour = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
            draw.line([(0, y), (width, y)], fill=colour)

        glow = Image.new('RGBA', (width, height), (0, 0, 0, 0))
        glow_draw = ImageDraw.Draw(glow)
        glow_draw.ellipse(
            [-width // 3, -height, width // 2, height // 2],
            fill=(*_COLOUR_GOLD, 42),
        )
        return Image.alpha_composite(img.convert('RGBA'), glow)

    def _load_background(self, width: int, height: int, bg_path: str | None) -> Image.Image:
        """Load custom background or fall back to the built-in gradient."""
        if bg_path and os.path.exists(bg_path):
            try:
                img = Image.open(bg_path).convert('RGB')
                img = img.resize((width, height), Image.Resampling.LANCZOS)
                img = img.filter(ImageFilter.GaussianBlur(radius=2))
                img = img.convert('RGBA')
                dim = Image.new('RGBA', (width, height), (8, 9, 14, 120))
                return Image.alpha_composite(img, dim)
            except Exception:
                pass
        return self._create_gradient_background(width, height)

    def _draw_card_panel(self, base: Image.Image) -> Image.Image:
        """Frosted card panel with a gold accent stripe."""
        width, height = base.size
        margin_x, margin_y = 28, 28
        card_box = [margin_x, margin_y, width - margin_x, height - margin_y]

        panel = Image.new('RGBA', (width, height), (0, 0, 0, 0))
        panel_draw = ImageDraw.Draw(panel)
        panel_draw.rounded_rectangle(card_box, radius=22, fill=_COLOUR_CARD)

        accent_inset = 18
        panel_draw.rectangle(
            [
                card_box[0] + 2,
                card_box[1] + accent_inset,
                card_box[0] + 7,
                card_box[3] - accent_inset,
            ],
            fill=(*_COLOUR_GOLD, 255),
        )

        return Image.alpha_composite(base, panel)

    async def _fetch_avatar(self, member: discord.Member, size: int) -> Image.Image | None:
        """Download member avatar as RGBA square (masking done when compositing)."""
        try:
            async with aiohttp.ClientSession() as session:
                url = str(member.display_avatar.with_size(256).url)
                async with session.get(url) as resp:
                    if resp.status != 200:
                        return None
                    avatar_data = await resp.read()

            av = Image.open(io.BytesIO(avatar_data)).convert('RGBA')
            return av.resize((size, size), Image.Resampling.LANCZOS)
        except Exception:
            return None

    @staticmethod
    def _smooth_circle_mask(diameter: int, supersample: int = _AVATAR_SUPERSAMPLE) -> Image.Image:
        """Anti-aliased circular alpha mask via supersampling."""
        hi = diameter * supersample
        mask = Image.new('L', (hi, hi), 0)
        inset = max(1, supersample // 2)
        ImageDraw.Draw(mask).ellipse(
            (inset, inset, hi - inset, hi - inset),
            fill=255,
        )
        return mask.resize((diameter, diameter), Image.Resampling.LANCZOS)

    def _compose_avatar_badge(
        self,
        avatar: Image.Image | None,
        size: int,
    ) -> Image.Image:
        """Gold ring + avatar rendered at high resolution for smooth edges."""
        ss = _AVATAR_SUPERSAMPLE
        pad = _RING_WIDTH + _RING_SEP
        total = size + pad * 2
        hi = total * ss

        img = Image.new('RGBA', (hi, hi), (0, 0, 0, 0))
        cx = cy = hi // 2
        draw = ImageDraw.Draw(img)

        outer_r = hi // 2 - ss
        ring_w = _RING_WIDTH * ss
        avatar_hi = size * ss
        avatar_off = pad * ss

        draw.ellipse(
            (cx - outer_r, cy - outer_r, cx + outer_r, cy + outer_r),
            fill=(*_COLOUR_GOLD, 255),
        )
        inner_gold_r = outer_r - ring_w
        draw.ellipse(
            (cx - inner_gold_r, cy - inner_gold_r, cx + inner_gold_r, cy + inner_gold_r),
            fill=(24, 26, 32, 255),
        )

        mask = self._smooth_circle_mask(avatar_hi, ss)
        if avatar:
            av_hi = avatar.resize((avatar_hi, avatar_hi), Image.Resampling.LANCZOS)
            img.paste(av_hi, (avatar_off, avatar_off), mask)
        else:
            placeholder = Image.new('RGBA', (avatar_hi, avatar_hi), (56, 58, 66, 255))
            img.paste(placeholder, (avatar_off, avatar_off), mask)

        return img.resize((total, total), Image.Resampling.LANCZOS)

    def _paste_avatar_with_ring(
        self,
        base: Image.Image,
        avatar: Image.Image | None,
        x: int,
        y: int,
        size: int,
    ) -> Image.Image:
        """Paste supersampled avatar badge onto the card."""
        badge = self._compose_avatar_badge(avatar, size)
        pad = (badge.width - size) // 2
        layer = base.convert('RGBA')
        layer.paste(badge, (x - pad, y - pad), badge)
        return layer

    def _draw_decorative_accent(
        self,
        draw: ImageDraw.ImageDraw,
        width: int,
        height: int,
        *,
        margin_x: int = 28,
    ) -> None:
        """Soft gold rings on the right, kept inside the card bounds."""
        card_right = width - margin_x
        padding = 44
        radii = (48, 34, 22)
        max_r = max(radii)
        cx = card_right - padding - max_r
        cy = height // 2
        for radius, alpha in zip(radii, (28, 40, 55)):
            draw.ellipse(
                [cx - radius, cy - radius, cx + radius, cy + radius],
                outline=(*_COLOUR_GOLD, alpha),
                width=2,
            )

    def _draw_text_block(
        self,
        draw: ImageDraw.ImageDraw,
        *,
        x: int,
        y: int,
        display_name: str,
        server_name: str,
        member_count: int,
    ) -> None:
        """Draw label, username, server line, and member pill."""
        font_label = self._load_font(15)
        font_name = self._load_font(40, bold=True)
        font_server = self._load_font(20)
        font_pill = self._load_font(17)

        draw.text((x, y), 'WELCOME', fill=_COLOUR_GOLD_SOFT, font=font_label)

        name_y = y + 26
        draw.text((x, name_y), display_name, fill=_COLOUR_TEXT, font=font_name)

        name_bbox = draw.textbbox((x, name_y), display_name, font=font_name)
        server_y = name_bbox[3] + 12
        server_line = f'to {server_name}'
        draw.text((x, server_y), server_line, fill=_COLOUR_MUTED, font=font_server)

        server_bbox = draw.textbbox((x, server_y), server_line, font=font_server)
        pill_text = f'Member #{member_count:,}'
        pill_w, pill_h = self._text_size(draw, pill_text, font=font_pill)
        pad_x, pad_y = 14, 7
        pill_x = x
        pill_y = server_bbox[3] + 18
        pill_box = [
            pill_x,
            pill_y,
            pill_x + pill_w + pad_x * 2,
            pill_y + pill_h + pad_y * 2,
        ]
        draw.rounded_rectangle(pill_box, radius=(pill_h + pad_y * 2) // 2, fill=_COLOUR_PILL)
        pill_bbox = draw.textbbox((0, 0), pill_text, font=font_pill)
        draw.text(
            (pill_x + pad_x, pill_y + pad_y - pill_bbox[1]),
            pill_text,
            fill=_COLOUR_MUTED,
            font=font_pill,
        )

    async def generate_welcome_card(self, member: discord.Member) -> io.BytesIO:
        """Generate and return the welcome card as a PNG byte stream."""
        width, height = WELCOME_IMAGE_SIZE
        avatar_size = WELCOME_AVATAR_SIZE

        settings = self.get_welcome_settings(member.guild.id)
        bg_path = settings.get('background_path', WELCOME_BACKGROUND_PATH)

        img = self._load_background(width, height, bg_path)
        img = self._draw_card_panel(img)

        margin_x, margin_y = 28, 28
        ring_pad = _RING_WIDTH + _RING_SEP
        badge_size = avatar_size + ring_pad * 2
        card_inner_x = margin_x + 36
        card_inner_y = margin_y + (height - margin_y * 2 - badge_size) // 2

        avatar = await self._fetch_avatar(member, avatar_size)
        img = self._paste_avatar_with_ring(
            img, avatar, card_inner_x, card_inner_y, avatar_size
        )

        text_x = card_inner_x + avatar_size + ring_pad + 36
        text_y = margin_y + 52
        draw = ImageDraw.Draw(img)

        display_name = self._truncate(member.display_name, 22)
        server_name = self._truncate(member.guild.name, 36)
        member_count = member.guild.member_count or 0

        self._draw_text_block(
            draw,
            x=text_x,
            y=text_y,
            display_name=display_name,
            server_name=server_name,
            member_count=member_count,
        )
        self._draw_decorative_accent(draw, width, height)

        buf = io.BytesIO()
        img.convert('RGB').save(buf, format='PNG')
        buf.seek(0)
        return buf

    # ------------------------------------------------------------------ #
    #  Event listener                                                     #
    # ------------------------------------------------------------------ #

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        """Send the welcome card when a member joins."""
        settings   = self.get_welcome_settings(member.guild.id)
        channel_id = settings.get('channel_id')
        if not channel_id:
            return

        channel = member.guild.get_channel(channel_id)
        if not channel:
            return

        try:
            card = await self.generate_welcome_card(member)
            file = discord.File(card, filename='welcome.png')
            await channel.send(
                content=f'Welcome to **{member.guild.name}**, {member.mention}! 👋',
                file=file,
            )
        except Exception as e:
            print(f'[Welcome] Error sending welcome message: {e}')

    # ------------------------------------------------------------------ #
    #  Slash commands                                                     #
    # ------------------------------------------------------------------ #

    @app_commands.command(
        name='set-welcome-channel',
        description='Set the channel where welcome messages are sent',
    )
    @app_commands.describe(channel='Channel to send welcome messages to')
    @app_commands.default_permissions(administrator=True)
    async def set_welcome_channel(
        self, interaction: discord.Interaction, channel: discord.TextChannel
    ):
        self.set_welcome_setting(interaction.guild.id, 'channel_id', channel.id)
        await interaction.response.send_message(
            f'✅ Welcome messages will now be sent to {channel.mention}'
        )

    @app_commands.command(
        name='set-welcome-background',
        description='Set a custom background image path for welcome cards',
    )
    @app_commands.describe(
        path='File path to the background image (leave blank to reset to the config default)'
    )
    @app_commands.default_permissions(administrator=True)
    async def set_welcome_background(
        self, interaction: discord.Interaction, path: str = None
    ):
        bg_path = path if path else WELCOME_BACKGROUND_PATH
        self.set_welcome_setting(interaction.guild.id, 'background_path', bg_path)

        if path:
            if os.path.exists(path):
                await interaction.response.send_message(
                    f'✅ Welcome background set to: `{path}`'
                )
            else:
                await interaction.response.send_message(
                    f'⚠️ Path saved as `{path}`, but the file was not found. '
                    f'The gradient fallback will be used until the file exists.',
                    ephemeral=True,
                )
        else:
            await interaction.response.send_message(
                f'✅ Welcome background reset to default (`{WELCOME_BACKGROUND_PATH}`)'
            )

    @app_commands.command(
        name='test-welcome',
        description='Preview the welcome message for a member',
    )
    @app_commands.describe(member='Member to use as the preview subject (defaults to you)')
    @app_commands.default_permissions(administrator=True)
    async def test_welcome(
        self, interaction: discord.Interaction, member: discord.Member = None
    ):
        target   = member or interaction.user
        settings = self.get_welcome_settings(interaction.guild.id)

        if not settings.get('channel_id'):
            await interaction.response.send_message(
                '❌ No welcome channel set. Use `/set-welcome-channel` first.',
                ephemeral=True,
            )
            return

        channel = interaction.guild.get_channel(settings['channel_id'])
        if not channel:
            await interaction.response.send_message(
                '❌ The configured welcome channel no longer exists.',
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        try:
            card = await self.generate_welcome_card(target)
            file = discord.File(card, filename='welcome.png')
            await channel.send(
                content=f'Welcome to **{interaction.guild.name}**, {target.mention}! 👋',
                file=file,
            )
            await interaction.followup.send('✅ Test welcome message sent!', ephemeral=True)
        except Exception as e:
            await interaction.followup.send(
                f'❌ Error generating welcome message: `{e}`', ephemeral=True
            )


async def setup(bot):
    await bot.add_cog(Welcome(bot))
