import discord
from discord.ext import commands
from discord import app_commands
import os
import json
import asyncio
from datetime import datetime, timezone


class AuditLog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.data_dir = 'data'
        self.settings_file = os.path.join(self.data_dir, 'audit_settings.json')
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

    def get_audit_settings(self, guild_id):
        """Get audit log settings for a guild."""
        data = self.load_json(self.settings_file)
        guild_id_str = str(guild_id)
        if guild_id_str in data:
            return data[guild_id_str]
        return {'channel_id': None}

    def set_audit_setting(self, guild_id, key, value):
        """Set a single audit log setting for a guild."""
        data = self.load_json(self.settings_file)
        guild_id_str = str(guild_id)
        if guild_id_str not in data:
            data[guild_id_str] = {}
        data[guild_id_str][key] = value
        self.save_json(self.settings_file, data)

    # ------------------------------------------------------------------ #
    #  Shared utilities                                                   #
    # ------------------------------------------------------------------ #

    async def send_log(self, guild: discord.Guild, embed: discord.Embed):
        """Send an embed to the guild's configured audit log channel."""
        settings   = self.get_audit_settings(guild.id)
        channel_id = settings.get('channel_id')
        if not channel_id:
            return
        channel = guild.get_channel(channel_id)
        if channel:
            try:
                await channel.send(embed=embed)
            except (discord.Forbidden, discord.HTTPException):
                pass

    async def fetch_audit_entry(
        self,
        guild: discord.Guild,
        action: discord.AuditLogAction,
        target_id: int = None,
        limit: int = 5,
        delay: float = 0.5,
    ):
        """Fetch the most recent audit log entry for an action, optionally filtered by target."""
        try:
            if delay > 0:
                await asyncio.sleep(delay)
            async for entry in guild.audit_logs(limit=limit, action=action):
                if target_id is None:
                    return entry
                if hasattr(entry.target, 'id') and entry.target.id == target_id:
                    return entry
        except (discord.Forbidden, discord.HTTPException):
            pass
        return None

    def is_recent(self, entry, seconds: int = 10) -> bool:
        """Return True if the audit log entry was created within the last N seconds."""
        if entry is None:
            return False
        age = (datetime.now(timezone.utc) - entry.created_at).total_seconds()
        return age <= seconds

    def truncate(self, text: str, max_len: int = 1000) -> str:
        """Truncate text to a maximum length, appending '...' if needed."""
        if not text:
            return '*None*'
        text = str(text)
        if len(text) > max_len:
            return text[: max_len - 3] + '...'
        return text

    # ------------------------------------------------------------------ #
    #  MESSAGE EVENTS                                                     #
    # ------------------------------------------------------------------ #

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        if not message.guild or message.author.bot:
            return

        embed = discord.Embed(
            title='🗑️ Message Deleted',
            color=0xE74C3C,
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_author(
            name=str(message.author), icon_url=message.author.display_avatar.url
        )
        embed.add_field(name='Author',   value=message.author.mention,                          inline=True)
        embed.add_field(name='Channel',  value=message.channel.mention,                         inline=True)
        embed.add_field(name='Sent At',  value=discord.utils.format_dt(message.created_at, 'F'), inline=True)

        content = self.truncate(message.content or '*No text content*', 1000)
        embed.add_field(name='Content', value=content, inline=False)

        if message.attachments:
            attach = '\n'.join(f'`{a.filename}`' for a in message.attachments)
            embed.add_field(name='Attachments', value=attach, inline=False)

        embed.set_footer(text=f'Message ID: {message.id}  •  User ID: {message.author.id}')
        await self.send_log(message.guild, embed)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if not before.guild or before.author.bot:
            return
        if before.content == after.content:
            return

        embed = discord.Embed(
            title='✏️ Message Edited',
            color=0xF39C12,
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_author(
            name=str(before.author), icon_url=before.author.display_avatar.url
        )
        embed.add_field(name='Author',          value=before.author.mention,      inline=True)
        embed.add_field(name='Channel',         value=before.channel.mention,     inline=True)
        embed.add_field(name='Jump to Message', value=f'[Click here]({after.jump_url})', inline=True)
        embed.add_field(name='Before', value=self.truncate(before.content or '*No content*', 500), inline=False)
        embed.add_field(name='After',  value=self.truncate(after.content  or '*No content*', 500), inline=False)
        embed.set_footer(text=f'Message ID: {before.id}  •  User ID: {before.author.id}')
        await self.send_log(before.guild, embed)

    # ------------------------------------------------------------------ #
    #  MEMBER JOIN / LEAVE / KICK / BAN / UNBAN                         #
    # ------------------------------------------------------------------ #

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        embed = discord.Embed(
            title='✅ Member Joined',
            color=0x2ECC71,
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_author(name=str(member), icon_url=member.display_avatar.url)
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name='User',            value=f'{member.mention}\n`{member}`',                                     inline=True)
        embed.add_field(name='Account Created', value=discord.utils.format_dt(member.created_at, 'F'), inline=True)
        embed.add_field(name='Account Age',     value=discord.utils.format_dt(member.created_at, 'R'), inline=True)
        embed.add_field(name='Member Count',    value=f'`{member.guild.member_count:,}`',               inline=True)
        embed.set_footer(text=f'User ID: {member.id}')
        await self.send_log(member.guild, embed)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        """Handles member leave, distinguishing between a voluntary leave and a kick.
        Bans are excluded here — on_member_ban handles those separately."""
        # Give Discord's audit log a moment to populate
        await asyncio.sleep(0.8)

        kick_entry = await self.fetch_audit_entry(
            member.guild, discord.AuditLogAction.kick, member.id, delay=0
        )
        ban_entry = await self.fetch_audit_entry(
            member.guild, discord.AuditLogAction.ban, member.id, delay=0
        )

        is_recent_kick = kick_entry and self.is_recent(kick_entry, 15)
        is_recent_ban  = ban_entry  and self.is_recent(ban_entry,  15)

        # on_member_ban already handles bans — don't double-log
        if is_recent_ban:
            return

        if is_recent_kick:
            embed = discord.Embed(
                title='👢 Member Kicked',
                color=0xE67E22,
                timestamp=datetime.now(timezone.utc),
            )
            embed.set_author(name=str(member), icon_url=member.display_avatar.url)
            embed.set_thumbnail(url=member.display_avatar.url)
            embed.add_field(name='Member',    value=f'{member.mention}\n`{member}`',                   inline=True)
            embed.add_field(name='Kicked By', value=f'{kick_entry.user.mention}\n`{kick_entry.user}`', inline=True)
            embed.add_field(name='Reason',    value=kick_entry.reason or '*No reason provided*',       inline=False)
            embed.set_footer(
                text=f'User ID: {member.id}  •  Moderator ID: {kick_entry.user.id}'
            )
        else:
            embed = discord.Embed(
                title='👋 Member Left',
                color=0x95A5A6,
                timestamp=datetime.now(timezone.utc),
            )
            embed.set_author(name=str(member), icon_url=member.display_avatar.url)
            embed.set_thumbnail(url=member.display_avatar.url)
            embed.add_field(name='User', value=f'{member.mention}\n`{member}`', inline=True)

            if member.joined_at:
                embed.add_field(
                    name='Joined',          value=discord.utils.format_dt(member.joined_at, 'F'), inline=True
                )
                embed.add_field(
                    name='Time in Server',  value=discord.utils.format_dt(member.joined_at, 'R'), inline=True
                )

            if member.roles[1:]:  # exclude @everyone
                roles_text = ', '.join(r.mention for r in reversed(member.roles[1:]))
                embed.add_field(
                    name='Roles', value=self.truncate(roles_text, 1000), inline=False
                )

            embed.set_footer(text=f'User ID: {member.id}')

        await self.send_log(member.guild, embed)

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User):
        entry = await self.fetch_audit_entry(guild, discord.AuditLogAction.ban, user.id)

        embed = discord.Embed(
            title='🔨 Member Banned',
            color=0xC0392B,
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_author(name=str(user), icon_url=user.display_avatar.url)
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.add_field(name='User', value=f'{user.mention}\n`{user}`', inline=True)

        if entry:
            embed.add_field(name='Banned By', value=f'{entry.user.mention}\n`{entry.user}`', inline=True)
            embed.add_field(name='Reason',    value=entry.reason or '*No reason provided*',  inline=False)
            embed.set_footer(text=f'User ID: {user.id}  •  Moderator ID: {entry.user.id}')
        else:
            embed.set_footer(text=f'User ID: {user.id}')

        await self.send_log(guild, embed)

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User):
        entry = await self.fetch_audit_entry(guild, discord.AuditLogAction.unban, user.id)

        embed = discord.Embed(
            title='✅ Member Unbanned',
            color=0x27AE60,
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_author(name=str(user), icon_url=user.display_avatar.url)
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.add_field(name='User', value=f'{user.mention}\n`{user}`', inline=True)

        if entry:
            embed.add_field(name='Unbanned By', value=f'{entry.user.mention}\n`{entry.user}`', inline=True)
            embed.add_field(name='Reason',      value=entry.reason or '*No reason provided*',  inline=False)
            embed.set_footer(text=f'User ID: {user.id}  •  Moderator ID: {entry.user.id}')
        else:
            embed.set_footer(text=f'User ID: {user.id}')

        await self.send_log(guild, embed)

    # ------------------------------------------------------------------ #
    #  MEMBER UPDATE  (roles · nickname · timeout)                        #
    # ------------------------------------------------------------------ #

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):

        # ── Role changes ─────────────────────────────────────────────────
        added_roles   = [r for r in after.roles   if r not in before.roles]
        removed_roles = [r for r in before.roles  if r not in after.roles]

        if added_roles:
            entry = await self.fetch_audit_entry(
                after.guild, discord.AuditLogAction.member_role_update, after.id
            )
            embed = discord.Embed(
                title='🎭 Role(s) Added',
                color=0x3498DB,
                timestamp=datetime.now(timezone.utc),
            )
            embed.set_author(name=str(after), icon_url=after.display_avatar.url)
            embed.add_field(name='Member',       value=f'{after.mention}\n`{after}`',            inline=True)
            embed.add_field(name='Role(s) Added', value=', '.join(r.mention for r in added_roles), inline=True)
            if entry and self.is_recent(entry):
                embed.add_field(name='Updated By', value=entry.user.mention, inline=True)
            embed.set_footer(text=f'User ID: {after.id}')
            await self.send_log(after.guild, embed)

        if removed_roles:
            entry = await self.fetch_audit_entry(
                after.guild, discord.AuditLogAction.member_role_update, after.id
            )
            embed = discord.Embed(
                title='🎭 Role(s) Removed',
                color=0x2471A3,
                timestamp=datetime.now(timezone.utc),
            )
            embed.set_author(name=str(after), icon_url=after.display_avatar.url)
            embed.add_field(name='Member',         value=f'{after.mention}\n`{after}`',               inline=True)
            embed.add_field(name='Role(s) Removed', value=', '.join(r.mention for r in removed_roles), inline=True)
            if entry and self.is_recent(entry):
                embed.add_field(name='Updated By', value=entry.user.mention, inline=True)
            embed.set_footer(text=f'User ID: {after.id}')
            await self.send_log(after.guild, embed)

        # ── Nickname change ───────────────────────────────────────────────
        if before.nick != after.nick:
            entry = await self.fetch_audit_entry(
                after.guild, discord.AuditLogAction.member_update, after.id
            )
            embed = discord.Embed(
                title='📝 Nickname Changed',
                color=0x9B59B6,
                timestamp=datetime.now(timezone.utc),
            )
            embed.set_author(name=str(after), icon_url=after.display_avatar.url)
            embed.add_field(name='Member', value=f'{after.mention}\n`{after}`', inline=True)
            if entry and self.is_recent(entry):
                embed.add_field(name='Changed By', value=entry.user.mention, inline=True)
            embed.add_field(name='Before', value=before.nick or '*None*', inline=True)
            embed.add_field(name='After',  value=after.nick  or '*None*', inline=True)
            embed.set_footer(text=f'User ID: {after.id}')
            await self.send_log(after.guild, embed)

        # ── Timeout add / remove ──────────────────────────────────────────
        if before.timed_out_until != after.timed_out_until:
            entry = await self.fetch_audit_entry(
                after.guild, discord.AuditLogAction.member_update, after.id
            )
            now = datetime.now(timezone.utc)

            if after.timed_out_until and after.timed_out_until > now:
                # Timeout added
                embed = discord.Embed(
                    title='⏱️ Member Timed Out',
                    color=0xE74C3C,
                    timestamp=datetime.now(timezone.utc),
                )
                embed.set_author(name=str(after), icon_url=after.display_avatar.url)
                embed.add_field(name='Member', value=f'{after.mention}\n`{after}`', inline=True)
                if entry and self.is_recent(entry):
                    embed.add_field(name='Timed Out By', value=entry.user.mention,                      inline=True)
                    embed.add_field(name='Reason',       value=entry.reason or '*No reason provided*',  inline=False)
                embed.add_field(name='Expires',  value=discord.utils.format_dt(after.timed_out_until, 'F'), inline=True)
                embed.add_field(name='Duration', value=discord.utils.format_dt(after.timed_out_until, 'R'), inline=True)
                embed.set_footer(text=f'User ID: {after.id}')
            else:
                # Timeout removed / expired
                embed = discord.Embed(
                    title='✅ Timeout Removed',
                    color=0x2ECC71,
                    timestamp=datetime.now(timezone.utc),
                )
                embed.set_author(name=str(after), icon_url=after.display_avatar.url)
                embed.add_field(name='Member', value=f'{after.mention}\n`{after}`', inline=True)
                if entry and self.is_recent(entry):
                    embed.add_field(name='Removed By', value=entry.user.mention, inline=True)
                embed.set_footer(text=f'User ID: {after.id}')

            await self.send_log(after.guild, embed)

    # ------------------------------------------------------------------ #
    #  USER UPDATE  (global username · avatar)                           #
    # ------------------------------------------------------------------ #

    @commands.Cog.listener()
    async def on_user_update(self, before: discord.User, after: discord.User):

        # ── Username change ────────────────────────────────
        if str(before) != str(after):
            embed = discord.Embed(
                title='📛 Username Changed',
                color=0x8E44AD,
                timestamp=datetime.now(timezone.utc),
            )
            embed.set_author(name=str(after), icon_url=after.display_avatar.url)
            embed.add_field(name='Before', value=f'`{before}`', inline=True)
            embed.add_field(name='After',  value=f'`{after}`',  inline=True)
            embed.set_footer(text=f'User ID: {after.id}')

            for guild in self.bot.guilds:
                if guild.get_member(after.id):
                    await self.send_log(guild, embed)

        # ── Display name change ────────────────────────────────
        before_global = getattr(before, 'global_name', None)
        after_global  = getattr(after, 'global_name', None)

        if before_global != after_global:
            embed = discord.Embed(
                title='🏷️ Display Name Changed',
                color=0x9B59B6,
                timestamp=datetime.now(timezone.utc),
            )
            embed.set_author(name=str(after), icon_url=after.display_avatar.url)
            
            b_val = f"`{before_global}`" if before_global else '*None*'
            a_val = f"`{after_global}`" if after_global else '*None*'
            
            embed.add_field(name='Before', value=b_val, inline=True)
            embed.add_field(name='After',  value=a_val, inline=True)
            embed.set_footer(text=f'User ID: {after.id}')

            for guild in self.bot.guilds:
                if guild.get_member(after.id):
                    await self.send_log(guild, embed)

        # ── Avatar change ─────────────────────────────────────────────────
        before_av = str(before.avatar.url) if before.avatar else None
        after_av  = str(after.avatar.url)  if after.avatar  else None

        if before_av != after_av:
            embed = discord.Embed(
                title='🖼️ Avatar Changed',
                color=0x7D3C98,
                timestamp=datetime.now(timezone.utc),
            )
            embed.set_author(name=str(after), icon_url=after.display_avatar.url)
            embed.add_field(name='User', value=f'{after.mention}\n`{after}`', inline=True)
            if before_av:
                embed.add_field(name='Previous Avatar', value=f'[View old avatar]({before_av})', inline=True)
            embed.set_thumbnail(url=after.display_avatar.url)
            embed.set_footer(text=f'User ID: {after.id}')

            for guild in self.bot.guilds:
                if guild.get_member(after.id):
                    await self.send_log(guild, embed)

    # ------------------------------------------------------------------ #
    #  VOICE EVENTS                                                       #
    # ------------------------------------------------------------------ #

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
        if member.bot:
            return

        # Joined a voice channel
        if not before.channel and after.channel:
            embed = discord.Embed(
                title='🔊 Joined Voice Channel',
                color=0x1ABC9C,
                timestamp=datetime.now(timezone.utc),
            )
            embed.set_author(name=str(member), icon_url=member.display_avatar.url)
            embed.add_field(name='Member',  value=member.mention,        inline=True)
            embed.add_field(name='Channel', value=after.channel.mention, inline=True)
            embed.set_footer(text=f'User ID: {member.id}')
            await self.send_log(member.guild, embed)

        # Left a voice channel
        elif before.channel and not after.channel:
            embed = discord.Embed(
                title='🔇 Left Voice Channel',
                color=0x16A085,
                timestamp=datetime.now(timezone.utc),
            )
            embed.set_author(name=str(member), icon_url=member.display_avatar.url)
            embed.add_field(name='Member',  value=member.mention,         inline=True)
            embed.add_field(name='Channel', value=before.channel.mention, inline=True)
            embed.set_footer(text=f'User ID: {member.id}')
            await self.send_log(member.guild, embed)

        # Moved between voice channels
        elif before.channel and after.channel and before.channel != after.channel:
            embed = discord.Embed(
                title='🔀 Switched Voice Channel',
                color=0x1ABC9C,
                timestamp=datetime.now(timezone.utc),
            )
            embed.set_author(name=str(member), icon_url=member.display_avatar.url)
            embed.add_field(name='Member', value=member.mention,         inline=True)
            embed.add_field(name='From',   value=before.channel.mention, inline=True)
            embed.add_field(name='To',     value=after.channel.mention,  inline=True)
            embed.set_footer(text=f'User ID: {member.id}')
            await self.send_log(member.guild, embed)

    # ------------------------------------------------------------------ #
    #  SLASH COMMANDS                                                     #
    # ------------------------------------------------------------------ #

    @app_commands.command(
        name='set-audit-channel',
        description='Set the channel where audit log messages are sent',
    )
    @app_commands.describe(channel='Channel to send audit log messages to')
    @app_commands.default_permissions(administrator=True)
    async def set_audit_channel(
        self, interaction: discord.Interaction, channel: discord.TextChannel
    ):
        self.set_audit_setting(interaction.guild.id, 'channel_id', channel.id)
        await interaction.response.send_message(
            f'✅ Audit log messages will now be sent to {channel.mention}'
        )


async def setup(bot):
    await bot.add_cog(AuditLog(bot))
