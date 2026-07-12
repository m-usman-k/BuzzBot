import discord
from discord.ext import commands
from discord import app_commands
import os
import json
import re
import time
import asyncio
import hashlib
from datetime import datetime, timezone, timedelta

SPAM_THRESHOLD_CHANNELS = 3
SPAM_MAX_REPEATS = 5
SPAM_TIME_WINDOW = 30
SPAM_TIMEOUT_DURATION = 3600
SPAM_DELETE_WINDOW = 1800

DISCORD_INVITE_PATTERN = re.compile(
    r'(?:discord\.(?:gg|com|app)\.?(?:\/invite)?\/)([a-zA-Z0-9\-_]+)',
    re.IGNORECASE
)

FILTER_CHOICES = [
    app_commands.Choice(name='channel_spam', value='channel_spam'),
    app_commands.Choice(name='advertising', value='advertising'),
]


class AutoMod(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.data_dir = 'data'
        self.settings_file = os.path.join(self.data_dir, 'automod_settings.json')
        self.audit_settings_file = os.path.join(self.data_dir, 'audit_settings.json')
        self.init_data_files()

        self.spam_tracking = {}
        self.cleanup_task = self.bot.loop.create_task(self._spam_tracking_cleanup())

    def cog_unload(self):
        self.cleanup_task.cancel()

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
        data = self.load_json(self.settings_file)
        gid = str(guild_id)
        if gid not in data:
            data[gid] = {
                'channel_spam': {
                    'enabled': False,
                    'immune_roles': [],
                    'immune_channels': [],
                    'threshold': SPAM_THRESHOLD_CHANNELS,
                    'max_repeats': SPAM_MAX_REPEATS,
                    'timeout_duration': SPAM_TIMEOUT_DURATION,
                    'delete_window': SPAM_DELETE_WINDOW,
                },
                'advertising': {
                    'enabled': False,
                    'immune_roles': [],
                    'immune_channels': [],
                },
            }
            self.save_json(self.settings_file, data)
        return data[gid]

    def set_filter_setting(self, guild_id, filter_name, key, value):
        data = self.load_json(self.settings_file)
        gid = str(guild_id)
        if gid not in data:
            data[gid] = {}
        if filter_name not in data[gid]:
            data[gid][filter_name] = {}
        data[gid][filter_name][key] = value
        self.save_json(self.settings_file, data)

    def has_immune_role(self, member, filter_settings):
        if not member or not filter_settings.get('immune_roles'):
            return False
        immune_ids = [int(r) for r in filter_settings['immune_roles']]
        return any(r.id in immune_ids for r in member.roles)

    def is_immune_channel(self, channel_id, filter_settings):
        if not filter_settings.get('immune_channels'):
            return False
        return channel_id in [int(c) for c in filter_settings['immune_channels']]

    async def send_mod_log(self, guild, embed):
        data = self.load_json(self.audit_settings_file)
        gid = str(guild.id)
        if gid in data:
            mod_channel_id = data[gid].get('mod_channel_id')
            if mod_channel_id:
                channel = guild.get_channel(mod_channel_id)
                if channel:
                    try:
                        await channel.send(embed=embed)
                    except (discord.Forbidden, discord.HTTPException):
                        pass

    async def _spam_tracking_cleanup(self):
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            await asyncio.sleep(60)
            now = time.time()
            expired = []
            for key, hash_map in self.spam_tracking.items():
                for content_hash, info in list(hash_map.items()):
                    if now - info['first_seen'] > SPAM_TIME_WINDOW * 2:
                        del hash_map[content_hash]
                if not hash_map:
                    expired.append(key)
            for key in expired:
                del self.spam_tracking[key]

    async def handle_channel_spam(self, message):
        if message.author.bot or not message.guild:
            return

        settings = self.get_guild_settings(message.guild.id)
        filter_cfg = settings.get('channel_spam', {})
        if not filter_cfg.get('enabled'):
            return

        if self.has_immune_role(message.author, filter_cfg):
            return
        if self.is_immune_channel(message.channel.id, filter_cfg):
            return

        parts = [message.content.strip()]
        for a in message.attachments:
            parts.append(f"[ATTACHMENT: {a.filename}]")
        signature = '|'.join(parts)
        if not signature:
            return

        content_hash = hashlib.sha256(signature.encode()).hexdigest()
        user_key = f"{message.author.id}_{message.guild.id}"

        if user_key not in self.spam_tracking:
            self.spam_tracking[user_key] = {}

        now = time.time()

        if content_hash not in self.spam_tracking[user_key]:
            self.spam_tracking[user_key][content_hash] = {
                'channels': {},
                'total_count': 0,
                'first_seen': now,
                'triggered': False,
                'detected': signature,
            }

        info = self.spam_tracking[user_key][content_hash]

        if info['triggered']:
            try:
                await message.delete()
            except (discord.NotFound, discord.Forbidden):
                pass
            return

        if now - info['first_seen'] > SPAM_TIME_WINDOW:
            info['channels'] = {}
            info['first_seen'] = now
            info['triggered'] = False

        if message.channel.id not in info['channels']:
            info['channels'][message.channel.id] = []
        info['channels'][message.channel.id].append((message.id, now))
        info['total_count'] += 1

        channel_count = len(info['channels'])
        repeat_count = info['total_count']
        channel_threshold = filter_cfg.get('threshold', SPAM_THRESHOLD_CHANNELS)
        repeat_threshold = filter_cfg.get('max_repeats', SPAM_MAX_REPEATS)

        if channel_count >= channel_threshold or repeat_count >= repeat_threshold:
            info['triggered'] = True
            await self.trigger_spam_action(message, info, filter_cfg)

    async def trigger_spam_action(self, message, spam_info, filter_cfg):
        guild = message.guild
        author = message.author
        channel_ids = list(spam_info['channels'].keys())
        timeout_duration = filter_cfg.get('timeout_duration', SPAM_TIMEOUT_DURATION)
        delete_window = filter_cfg.get('delete_window', SPAM_DELETE_WINDOW)
        detected = spam_info.get('detected', '')

        timeout_until = discord.utils.utcnow() + timedelta(seconds=timeout_duration)
        max_timeout = discord.utils.utcnow() + timedelta(days=28)
        if timeout_until > max_timeout:
            timeout_until = max_timeout

        member = guild.get_member(author.id)
        if not member:
            print(f"[AutoMod] Could not find member {author.id} in guild {guild.id}")
            timed_out = False
        elif member.id == guild.owner_id:
            print(f"[AutoMod] Cannot timeout server owner {author.id}")
            timed_out = False
        else:
            try:
                await member.timeout(timeout_until, reason="Channel spam: identical content in multiple channels")
                timed_out = True
                print(f"[AutoMod] Timed out {author.id} in {guild.id} until {timeout_until}")
            except discord.Forbidden:
                print(f"[AutoMod] Failed to timeout {author.id} in {guild.id}: Missing Moderate Members permission or bot role is not high enough")
                timed_out = False
            except discord.HTTPException as e:
                print(f"[AutoMod] Failed to timeout {author.id} in {guild.id}: {e}")
                timed_out = False

        cutoff = time.time() - delete_window
        deleted_count = 0

        tracked_messages = [(cid, msg_id) for cid, msg_list in spam_info['channels'].items() for msg_id, _ in msg_list]

        for cid, msg_id in tracked_messages:
            channel = guild.get_channel(cid)
            if not channel:
                continue
            try:
                msg_obj = channel.get_partial_message(msg_id)
                await msg_obj.delete()
                deleted_count += 1
            except (discord.NotFound, discord.Forbidden):
                pass
            except discord.HTTPException as e:
                if e.status == 429:
                    retry_after = e.response.headers.get('Retry-After', 1)
                    await asyncio.sleep(float(retry_after) + 0.5)
                    try:
                        await msg_obj.delete()
                        deleted_count += 1
                    except discord.DiscordException:
                        pass
            await asyncio.sleep(0.6)

        for cid in channel_ids:
            channel = guild.get_channel(cid)
            if not channel:
                continue
            try:
                async for msg in channel.history(limit=100):
                    if msg.author.id == author.id and msg.created_at.timestamp() >= cutoff:
                        try:
                            await msg.delete()
                            deleted_count += 1
                        except discord.DiscordException:
                            pass
                        await asyncio.sleep(1.0)
            except (discord.Forbidden, discord.HTTPException):
                pass

        try:
            dm_desc = (
                f"You have been detected sending identical content across multiple channels "
                f"in **{guild.name}**.\n\n"
                f"**Deleted Messages:** {deleted_count}\n\n"
            )
            if timed_out:
                dm_desc += f"**Timeout:** {timeout_duration // 60} minute(s)\n"
            else:
                dm_desc += "**Timeout:** Could not be applied (check bot permissions).\n"
            dm_desc += "\nIf you believe this was a mistake, please contact a server administrator."

            dm_embed = discord.Embed(
                title="Automatic Moderation Action",
                colour=0xE74C3C,
                description=dm_desc,
                timestamp=datetime.now(timezone.utc),
            )
            await author.send(embed=dm_embed)
        except (discord.Forbidden, discord.HTTPException):
            pass

        channel_mentions = ', '.join(f"<#{cid}>" for cid in channel_ids)
        timeout_status = f"{timeout_duration // 60} minute(s)" if timed_out else "Failed (check permissions)"
        log_embed = discord.Embed(
            title="AutoMod: Channel Spam Detected",
            colour=0xE74C3C,
            timestamp=datetime.now(timezone.utc),
        )
        log_embed.set_author(name=str(author), icon_url=author.display_avatar.url)
        log_embed.add_field(name="User", value=author.mention, inline=True)
        log_embed.add_field(name="Channels", value=channel_mentions, inline=True)
        log_embed.add_field(name="Timeout", value=timeout_status, inline=True)
        log_embed.add_field(name="Deleted", value=str(deleted_count), inline=True)
        log_embed.add_field(name="Detected", value=detected[:500] if detected else "*See attachments*", inline=False)
        log_embed.set_footer(text=f"User ID: {author.id}")
        await self.send_mod_log(guild, log_embed)

    async def handle_advertising(self, message):
        if message.author.bot or not message.guild:
            return

        settings = self.get_guild_settings(message.guild.id)
        filter_cfg = settings.get('advertising', {})
        if not filter_cfg.get('enabled'):
            return

        if self.has_immune_role(message.author, filter_cfg):
            return
        if self.is_immune_channel(message.channel.id, filter_cfg):
            return

        content = message.content.strip()
        if not content:
            return

        if not DISCORD_INVITE_PATTERN.search(content):
            return

        try:
            await message.delete()
        except (discord.NotFound, discord.Forbidden):
            return

        try:
            warning = await message.channel.send(
                f"{message.author.mention} Discord invite links are not allowed in this channel. "
                f"Please use the appropriate channel for promotions.",
                delete_after=10,
            )
        except discord.Forbidden:
            pass

        log_embed = discord.Embed(
            title="AutoMod: Advertising Detected",
            colour=0xF39C12,
            timestamp=datetime.now(timezone.utc),
        )
        log_embed.set_author(name=str(message.author), icon_url=message.author.display_avatar.url)
        log_embed.add_field(name="User", value=message.author.mention, inline=True)
        log_embed.add_field(name="Channel", value=message.channel.mention, inline=True)
        log_embed.add_field(name="Content", value=content[:500] if content else "*None*", inline=False)
        log_embed.set_footer(text=f"User ID: {message.author.id}")
        await self.send_mod_log(message.guild, log_embed)

    @commands.Cog.listener()
    async def on_message(self, message):
        await self.handle_advertising(message)
        if not message.author.bot:
            await self.handle_channel_spam(message)

    automod = app_commands.Group(name='automod', description='Configure AutoMod filters')

    @automod.command(name='toggle', description='Enable or disable an AutoMod filter')
    @app_commands.describe(filter='Which filter to toggle')
    @app_commands.choices(filter=FILTER_CHOICES)
    @app_commands.default_permissions(administrator=True)
    async def automod_toggle(self, interaction: discord.Interaction, filter: app_commands.Choice[str]):
        settings = self.get_guild_settings(interaction.guild.id)
        filter_cfg = settings.get(filter.value, {})
        new_state = not filter_cfg.get('enabled', False)
        self.set_filter_setting(interaction.guild.id, filter.value, 'enabled', new_state)
        label = 'Channel spam' if filter.value == 'channel_spam' else 'Advertising'
        await interaction.response.send_message(
            f"{'✅' if new_state else '❌'} {label} filter is now **{'enabled' if new_state else 'disabled'}**."
        )

    @automod.command(name='immunity', description='Configure immune roles and channels for a filter')
    @app_commands.describe(
        filter='Which filter to configure',
        role='Role to toggle immunity (leave empty to view current)',
        channel='Channel to toggle immunity (leave empty to view current)',
    )
    @app_commands.choices(filter=FILTER_CHOICES)
    @app_commands.default_permissions(administrator=True)
    async def automod_immunity(
        self,
        interaction: discord.Interaction,
        filter: app_commands.Choice[str],
        role: discord.Role = None,
        channel: discord.TextChannel = None,
    ):
        settings = self.get_guild_settings(interaction.guild.id)
        filter_cfg = settings.get(filter.value, {})
        label = 'Channel spam' if filter.value == 'channel_spam' else 'Advertising'

        if role is not None:
            current = filter_cfg.get('immune_roles', [])
            role_id_str = str(role.id)
            if role_id_str in current:
                current.remove(role_id_str)
                self.set_filter_setting(interaction.guild.id, filter.value, 'immune_roles', current)
                await interaction.response.send_message(
                    f"Removed {role.mention} from {label} filter immunity."
                )
            else:
                current.append(role_id_str)
                self.set_filter_setting(interaction.guild.id, filter.value, 'immune_roles', current)
                await interaction.response.send_message(
                    f"Added {role.mention} to {label} filter immunity."
                )
            return

        if channel is not None:
            current = filter_cfg.get('immune_channels', [])
            channel_id_str = str(channel.id)
            if channel_id_str in current:
                current.remove(channel_id_str)
                self.set_filter_setting(interaction.guild.id, filter.value, 'immune_channels', current)
                await interaction.response.send_message(
                    f"Removed {channel.mention} from {label} filter immunity."
                )
            else:
                current.append(channel_id_str)
                self.set_filter_setting(interaction.guild.id, filter.value, 'immune_channels', current)
                await interaction.response.send_message(
                    f"Added {channel.mention} to {label} filter immunity."
                )
            return

        immune_roles = []
        for rid in filter_cfg.get('immune_roles', []):
            r = interaction.guild.get_role(int(rid))
            immune_roles.append(r.mention if r else f"Unknown Role ({rid})")

        immune_channels = []
        for cid in filter_cfg.get('immune_channels', []):
            c = interaction.guild.get_channel(int(cid))
            immune_channels.append(c.mention if c else f"Unknown Channel ({cid})")

        embed = discord.Embed(
            title=f"{label} Immunity Settings",
            colour=0x3498DB,
        )
        embed.add_field(
            name="Immune Roles",
            value='\n'.join(immune_roles) if immune_roles else '*None*',
            inline=False,
        )
        embed.add_field(
            name="Immune Channels",
            value='\n'.join(immune_channels) if immune_channels else '*None*',
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @automod.command(name='status', description='View the current AutoMod configuration')
    @app_commands.default_permissions(administrator=True)
    async def automod_status(self, interaction: discord.Interaction):
        settings = self.get_guild_settings(interaction.guild.id)

        embed = discord.Embed(
            title="AutoMod Configuration",
            colour=0x3498DB,
            timestamp=datetime.now(timezone.utc),
        )

        for filter_name, display in [('channel_spam', 'Channel Spam'), ('advertising', 'Advertising')]:
            cfg = settings.get(filter_name, {})
            enabled = cfg.get('enabled', False)
            status = '✅ Enabled' if enabled else '❌ Disabled'

            immune_roles = []
            for rid in cfg.get('immune_roles', []):
                r = interaction.guild.get_role(int(rid))
                immune_roles.append(r.mention if r else f"`{rid}`")
            roles_text = ', '.join(immune_roles) if immune_roles else '*None*'

            immune_channels = []
            for cid in cfg.get('immune_channels', []):
                c = interaction.guild.get_channel(int(cid))
                immune_channels.append(c.mention if c else f"`{cid}`")
            channels_text = ', '.join(immune_channels) if immune_channels else '*None*'

            extra = ''
            if filter_name == 'channel_spam' and enabled:
                threshold = cfg.get('threshold', SPAM_THRESHOLD_CHANNELS)
                timeout_m = cfg.get('timeout_duration', SPAM_TIMEOUT_DURATION) // 60
                repeats = cfg.get('max_repeats', SPAM_MAX_REPEATS)
                extra = f"\nCross channel: `{threshold}` channels | Same channel: `{repeats}` repeats | Timeout: `{timeout_m}`m"

            embed.add_field(
                name=f"{display} — {status}",
                value=f"**Immune Roles:** {roles_text}\n**Immune Channels:** {channels_text}{extra}",
                inline=False,
            )

        me = interaction.guild.me
        can_timeout = me.guild_permissions.moderate_members if me else False
        if not can_timeout:
            embed.add_field(
                name="Permission Warning",
                value="The bot is missing the `Moderate Members` permission. Timeouts from the channel spam filter will silently fail. Grant this permission in Server Settings > Roles.",
                inline=False,
            )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    spam = app_commands.Group(name='spam', description='Channel spam filter settings', parent=automod)

    @spam.command(name='threshold', description='Set cross channel threshold for channel spam filter')
    @app_commands.describe(channels='Number of channels before triggering')
    @app_commands.default_permissions(administrator=True)
    async def spam_threshold(self, interaction: discord.Interaction, channels: int):
        if channels < 2:
            await interaction.response.send_message("Threshold must be at least 2.", ephemeral=True)
            return
        self.set_filter_setting(interaction.guild.id, 'channel_spam', 'threshold', channels)
        await interaction.response.send_message(f"Cross channel threshold set to `{channels}` for channel spam.")

    @spam.command(name='repeats', description='Set max same channel repeats before triggering')
    @app_commands.describe(count='Number of identical messages in the same channel before triggering')
    @app_commands.default_permissions(administrator=True)
    async def spam_repeats(self, interaction: discord.Interaction, count: int):
        if count < 2:
            await interaction.response.send_message("Repeat count must be at least 2.", ephemeral=True)
            return
        self.set_filter_setting(interaction.guild.id, 'channel_spam', 'max_repeats', count)
        await interaction.response.send_message(f"Same channel repeat limit set to `{count}` for channel spam.")

    @spam.command(name='timeout', description='Set timeout duration for channel spam')
    @app_commands.describe(minutes='Duration in minutes')
    @app_commands.default_permissions(administrator=True)
    async def spam_timeout(self, interaction: discord.Interaction, minutes: int):
        if minutes < 1 or minutes > 40320:
            await interaction.response.send_message("Timeout must be between 1 and 40320 minutes (28 days).", ephemeral=True)
            return
        self.set_filter_setting(interaction.guild.id, 'channel_spam', 'timeout_duration', minutes * 60)
        await interaction.response.send_message(f"Timeout set to `{minutes}` minutes for channel spam.")

    @spam.command(name='purge', description='Set message deletion window for channel spam')
    @app_commands.describe(minutes='How far back to delete messages (in minutes)')
    @app_commands.default_permissions(administrator=True)
    async def spam_purge(self, interaction: discord.Interaction, minutes: int):
        if minutes < 1 or minutes > 1440:
            await interaction.response.send_message("Purge window must be between 1 and 1440 minutes (24 hours).", ephemeral=True)
            return
        self.set_filter_setting(interaction.guild.id, 'channel_spam', 'delete_window', minutes * 60)
        await interaction.response.send_message(f"Delete window set to `{minutes}` minutes for channel spam.")


async def setup(bot):
    await bot.add_cog(AutoMod(bot))
