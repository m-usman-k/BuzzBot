import discord
from discord.ext import commands, tasks
from discord import app_commands
import os
import json
import re
import asyncio
import xml.etree.ElementTree as ET
from collections import namedtuple
from datetime import datetime, timezone

import aiohttp

from config import YOUTUBE_POLL_INTERVAL, YOUTUBE_MAX_VIDEO_AGE

FEED_URL = 'https://www.youtube.com/feeds/videos.xml?channel_id={}'
CHANNEL_ID_PATTERN = re.compile(r'^(UC[\w-]{22})$')
PAGE_CHANNEL_ID_PATTERNS = [
    re.compile(r'feeds/videos\.xml\?channel_id=(UC[\w-]{22})'),
    re.compile(r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[\w-]{22})"'),
    re.compile(r'"externalId":"(UC[\w-]{22})"'),
    re.compile(r'itemprop="channelId" content="(UC[\w-]{22})"'),
    re.compile(r'"externalChannelId":"(UC[\w-]{22})"'),  # Video pages
    re.compile(r'"channelId":"(UC[\w-]{22})"'),
]
FEED_NS = {
    'atom': 'http://www.w3.org/2005/Atom',
    'yt': 'http://www.youtube.com/xml/schemas/2015',
}
DEFAULT_ROLE_NAME = 'Upload Notifications'
SEEN_HISTORY = 50

REQUEST_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36',
    'Accept-Language': 'en-GB,en;q=0.9',
}
# Without these, YouTube serves a cookie consent page instead of the channel page to UK/EU visitors
CONSENT_COOKIES = {'CONSENT': 'YES+cb', 'SOCS': 'CAI'}

Video = namedtuple('Video', 'id title url author published')


class YouTubeError(Exception):
    pass


class YouTube(commands.Cog, name="YouTube"):
    def __init__(self, bot):
        self.bot = bot
        self.data_dir = 'data'
        self.settings_file = os.path.join(self.data_dir, 'youtube_settings.json')
        self.init_data_files()
        self.settings = self.load_json(self.settings_file)
        self.session = None
        self.check_uploads.start()

    async def cog_unload(self):
        self.check_uploads.cancel()
        if self.session and not self.session.closed:
            await self.session.close()

    # ------------------------------------------------------------------ #
    #  Data helpers                                                       #
    # ------------------------------------------------------------------ #

    def init_data_files(self):
        os.makedirs(self.data_dir, exist_ok=True)
        if not os.path.exists(self.settings_file):
            self.save_json(self.settings_file, {})

    def load_json(self, filepath):
        try:
            if os.path.exists(filepath):
                with open(filepath, 'r', encoding='utf-8') as f:
                    return json.load(f)
            return {}
        except (json.JSONDecodeError, IOError):
            return {}

    def save_json(self, filepath, data):
        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            return True
        except IOError:
            return False

    def get_guild_settings(self, guild_id):
        return self.settings.setdefault(str(guild_id), {
            'notify_channel_id': None,
            'ping_role_id': None,
            'subscriptions': {},
        })

    def save(self):
        self.save_json(self.settings_file, self.settings)

    # ------------------------------------------------------------------ #
    #  YouTube                                                            #
    # ------------------------------------------------------------------ #

    async def get_session(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                headers=REQUEST_HEADERS,
                cookies=CONSENT_COOKIES,
                timeout=aiohttp.ClientTimeout(total=20),
            )
        return self.session

    async def resolve_channel_id(self, text):
        """Turn a channel ID, @handle or channel/video URL into a channel ID."""
        text = text.strip().strip('<>')
        match = CHANNEL_ID_PATTERN.match(text)
        if match:
            return match.group(1)
        match = re.search(r'/channel/(UC[\w-]{22})', text)
        if match:
            return match.group(1)

        if text.startswith(('http://', 'https://')):
            url = text
        elif 'youtube.com' in text or 'youtu.be' in text:
            url = 'https://' + text
        else:
            url = 'https://www.youtube.com/@' + text.lstrip('@')

        session = await self.get_session()
        async with session.get(url) as resp:
            if resp.status == 404:
                raise YouTubeError("That YouTube channel doesn't exist.")
            if resp.status >= 400:
                raise YouTubeError(f"YouTube returned HTTP {resp.status}.")
            page = await resp.text()
        for pattern in PAGE_CHANNEL_ID_PATTERNS:
            match = pattern.search(page)
            if match:
                return match.group(1)
        raise YouTubeError("Couldn't find a channel at that link. Try the channel's @handle or full URL.")

    async def fetch_feed(self, channel_id):
        """Return (channel name, videos newest first) from the channel's RSS feed."""
        session = await self.get_session()
        async with session.get(FEED_URL.format(channel_id)) as resp:
            if resp.status == 404:
                raise YouTubeError("That YouTube channel doesn't exist.")
            if resp.status >= 400:
                raise YouTubeError(f"YouTube returned HTTP {resp.status} for the upload feed.")
            body = await resp.read()

        root = ET.fromstring(body)
        channel_name = root.findtext('atom:title', default='', namespaces=FEED_NS)
        videos = []
        for entry in root.findall('atom:entry', FEED_NS):
            video_id = entry.findtext('yt:videoId', namespaces=FEED_NS)
            if not video_id:
                continue
            link = entry.find('atom:link', FEED_NS)
            published = entry.findtext('atom:published', namespaces=FEED_NS)
            try:
                published_at = datetime.fromisoformat(published) if published else None
            except ValueError:
                published_at = None
            videos.append(Video(
                id=video_id,
                title=entry.findtext('atom:title', default='', namespaces=FEED_NS),
                url=link.get('href') if link is not None else f'https://www.youtube.com/watch?v={video_id}',
                author=entry.findtext('atom:author/atom:name', default=channel_name, namespaces=FEED_NS),
                published=published_at,
            ))
        return channel_name, videos

    def find_subscription(self, guild_settings, text):
        """Match autocomplete values (channel IDs) as well as typed channel names."""
        subs = guild_settings.get('subscriptions', {})
        if text in subs:
            return text
        for channel_id, sub in subs.items():
            if sub.get('name', '').lower() == text.strip().lower():
                return channel_id
        return None

    def build_notification(self, guild, guild_settings, video, ping=True):
        role = guild.get_role(guild_settings['ping_role_id']) if guild_settings.get('ping_role_id') else None
        title = discord.utils.escape_mentions(discord.utils.escape_markdown(video.title))
        author = discord.utils.escape_mentions(discord.utils.escape_markdown(video.author))
        content = f"**{author}** just uploaded a new video: **{title}**\n{video.url}"
        if role:
            content = f"{role.mention} {content}"
        allowed = discord.AllowedMentions(roles=[role] if (role and ping) else False, users=False, everyone=False)
        return content, allowed

    async def announce(self, guild, guild_settings, video):
        channel = guild.get_channel(guild_settings.get('notify_channel_id') or 0)
        if not channel:
            return
        content, allowed = self.build_notification(guild, guild_settings, video)
        try:
            await channel.send(content, allowed_mentions=allowed)
        except (discord.Forbidden, discord.HTTPException) as e:
            print(f"[YouTube] Failed to send upload notification in {guild.id}: {e}")

    @tasks.loop(seconds=YOUTUBE_POLL_INTERVAL)
    async def check_uploads(self):
        # An unhandled error would stop the loop for good, so keep it alive whatever happens
        try:
            await self._check_uploads()
        except Exception as e:
            print(f"[YouTube] Upload check failed: {e!r}")

    async def _check_uploads(self):
        guilds_by_channel = {}
        for gid, guild_settings in self.settings.items():
            for channel_id in guild_settings.get('subscriptions', {}):
                guilds_by_channel.setdefault(channel_id, []).append(gid)

        cutoff = datetime.now(timezone.utc).timestamp() - YOUTUBE_MAX_VIDEO_AGE
        changed = False
        for channel_id, guild_ids in guilds_by_channel.items():
            try:
                channel_name, videos = await self.fetch_feed(channel_id)
            except (YouTubeError, aiohttp.ClientError, asyncio.TimeoutError, ET.ParseError) as e:
                print(f"[YouTube] Failed to check {channel_id}: {e}")
                continue

            for gid in guild_ids:
                guild = self.bot.get_guild(int(gid))
                guild_settings = self.settings.get(gid, {})
                sub = guild_settings.get('subscriptions', {}).get(channel_id)
                if not guild or not sub:
                    continue
                seen = set(sub.get('seen', []))
                new_videos = [v for v in videos if v.id not in seen]
                if not new_videos:
                    continue

                sub['seen'] = ([v.id for v in new_videos] + sub.get('seen', []))[:SEEN_HISTORY]
                if channel_name:
                    sub['name'] = channel_name
                changed = True

                # Oldest first so multiple uploads are announced in order
                for video in reversed(new_videos):
                    if video.published and video.published.timestamp() < cutoff:
                        continue
                    await self.announce(guild, guild_settings, video)
            await asyncio.sleep(1)

        if changed:
            self.save()

    @check_uploads.before_loop
    async def before_check_uploads(self):
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------ #
    #  Commands                                                           #
    # ------------------------------------------------------------------ #

    youtube = app_commands.Group(name='youtube', description='YouTube upload notifications')

    async def subscription_autocomplete(self, interaction: discord.Interaction, current: str):
        subs = self.get_guild_settings(interaction.guild.id).get('subscriptions', {})
        return [
            app_commands.Choice(name=sub.get('name') or channel_id, value=channel_id)
            for channel_id, sub in subs.items()
            if current.lower() in (sub.get('name') or channel_id).lower()
        ][:25]

    @youtube.command(name='setup', description='Set where upload notifications are sent and which role is pinged')
    @app_commands.describe(
        channel='Channel to post upload notifications in',
        role=f'Role to ping (defaults to a role called "{DEFAULT_ROLE_NAME}")',
    )
    @app_commands.default_permissions(administrator=True)
    async def youtube_setup(self, interaction: discord.Interaction, channel: discord.TextChannel, role: discord.Role = None):
        guild = interaction.guild
        role = role or discord.utils.get(guild.roles, name=DEFAULT_ROLE_NAME)
        guild_settings = self.get_guild_settings(guild.id)
        guild_settings['notify_channel_id'] = channel.id
        guild_settings['ping_role_id'] = role.id if role else None
        self.save()

        lines = [f"Upload notifications will be posted in {channel.mention}."]
        if role:
            lines.append(f"{role.mention} will be pinged for each upload.")
        else:
            lines.append(f"No role will be pinged. Create a role called **{DEFAULT_ROLE_NAME}** or pass one with `role:`.")

        me = guild.me
        perms = channel.permissions_for(me)
        if not (perms.send_messages and perms.embed_links):
            lines.append(f"⚠️ I need **Send Messages** and **Embed Links** in {channel.mention}.")
        if role and not role.mentionable and not perms.mention_everyone:
            lines.append(
                f"⚠️ {role.mention} isn't mentionable, so the ping won't work. Turn on *Allow anyone to @mention this role* "
                "or give me the **Mention @everyone, @here, and All Roles** permission."
            )
        await interaction.response.send_message(
            '\n'.join(lines), allowed_mentions=discord.AllowedMentions.none()
        )

    @youtube.command(name='subscribe', description='Get notified when a YouTube channel uploads')
    @app_commands.describe(channel='The YouTube channel @handle, link or channel ID')
    @app_commands.default_permissions(administrator=True)
    async def youtube_subscribe(self, interaction: discord.Interaction, channel: str):
        await interaction.response.defer()
        try:
            channel_id = await self.resolve_channel_id(channel)
            channel_name, videos = await self.fetch_feed(channel_id)
        except YouTubeError as e:
            await interaction.followup.send(f"❌ {e}")
            return
        except (aiohttp.ClientError, asyncio.TimeoutError, ET.ParseError):
            await interaction.followup.send("❌ Couldn't reach YouTube right now, please try again in a minute.")
            return

        guild_settings = self.get_guild_settings(interaction.guild.id)
        subs = guild_settings['subscriptions']
        if channel_id in subs:
            await interaction.followup.send(f"Already subscribed to **{channel_name or channel_id}**.")
            return

        # Existing videos count as already seen so subscribing doesn't announce the back catalogue
        subs[channel_id] = {'name': channel_name, 'seen': [v.id for v in videos][:SEEN_HISTORY]}
        self.save()

        text = f"✅ Subscribed to **{discord.utils.escape_markdown(channel_name or channel_id)}**."
        notify_channel = interaction.guild.get_channel(guild_settings.get('notify_channel_id') or 0)
        if notify_channel:
            text += f" New uploads will be posted in {notify_channel.mention}."
        else:
            text += " Use `/youtube setup` to choose where notifications are sent."
        await interaction.followup.send(text)

    @youtube.command(name='unsubscribe', description='Stop notifications for a YouTube channel')
    @app_commands.describe(channel='The subscribed YouTube channel')
    @app_commands.autocomplete(channel=subscription_autocomplete)
    @app_commands.default_permissions(administrator=True)
    async def youtube_unsubscribe(self, interaction: discord.Interaction, channel: str):
        guild_settings = self.get_guild_settings(interaction.guild.id)
        channel_id = self.find_subscription(guild_settings, channel)
        if not channel_id:
            await interaction.response.send_message("That channel isn't subscribed.", ephemeral=True)
            return
        sub = guild_settings['subscriptions'].pop(channel_id)
        self.save()
        await interaction.response.send_message(
            f"Unsubscribed from **{discord.utils.escape_markdown(sub.get('name') or channel_id)}**."
        )

    @youtube.command(name='list', description='View YouTube notification settings and subscriptions')
    @app_commands.default_permissions(administrator=True)
    async def youtube_list(self, interaction: discord.Interaction):
        guild = interaction.guild
        guild_settings = self.get_guild_settings(guild.id)
        notify_channel = guild.get_channel(guild_settings.get('notify_channel_id') or 0)
        role = guild.get_role(guild_settings.get('ping_role_id') or 0)

        embed = discord.Embed(title="YouTube Notifications", colour=0xFF0000)
        embed.add_field(name="Channel", value=notify_channel.mention if notify_channel else "*Not set*", inline=True)
        embed.add_field(name="Ping Role", value=role.mention if role else "*None*", inline=True)
        subs = guild_settings.get('subscriptions', {})
        embed.add_field(
            name=f"Subscriptions ({len(subs)})",
            value='\n'.join(
                f"[{discord.utils.escape_markdown(sub.get('name') or channel_id)}](https://www.youtube.com/channel/{channel_id})"
                for channel_id, sub in subs.items()
            )[:1024] or "*None, use `/youtube subscribe` to add one*",
            inline=False,
        )
        embed.set_footer(text=f"Checked for new uploads every {YOUTUBE_POLL_INTERVAL // 60} minute(s)")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @youtube.command(name='test', description="Post a channel's latest upload as a test notification (no ping)")
    @app_commands.describe(channel='The subscribed YouTube channel')
    @app_commands.autocomplete(channel=subscription_autocomplete)
    @app_commands.default_permissions(administrator=True)
    async def youtube_test(self, interaction: discord.Interaction, channel: str):
        guild = interaction.guild
        guild_settings = self.get_guild_settings(guild.id)
        channel_id = self.find_subscription(guild_settings, channel)
        if not channel_id:
            await interaction.response.send_message("That channel isn't subscribed.", ephemeral=True)
            return
        notify_channel = guild.get_channel(guild_settings.get('notify_channel_id') or 0)
        if not notify_channel:
            await interaction.response.send_message("Use `/youtube setup` to choose a notification channel first.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        try:
            _, videos = await self.fetch_feed(channel_id)
        except (YouTubeError, aiohttp.ClientError, asyncio.TimeoutError, ET.ParseError):
            await interaction.followup.send("❌ Couldn't reach YouTube right now, please try again in a minute.")
            return
        if not videos:
            await interaction.followup.send("That channel hasn't uploaded anything yet.")
            return

        content, allowed = self.build_notification(guild, guild_settings, videos[0], ping=False)
        try:
            await notify_channel.send(content, allowed_mentions=allowed)
        except (discord.Forbidden, discord.HTTPException):
            await interaction.followup.send(f"❌ I couldn't send messages in {notify_channel.mention}.")
            return
        await interaction.followup.send(f"Sent a test notification to {notify_channel.mention}. The role wasn't pinged.")


async def setup(bot):
    await bot.add_cog(YouTube(bot))
