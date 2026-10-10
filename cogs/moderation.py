import discord
from discord.ext import commands
from discord import app_commands
import os
import json
import asyncio
from datetime import datetime, timezone, timedelta

# Discord keeps audit log entries for 45 days, so that's how far back the first import can go
BACKFILL_DAYS = 45
BACKFILL_LIMIT_PER_ACTION = 500
ENTRIES_PER_PAGE = 5

TRACKED_ACTIONS = (
    discord.AuditLogAction.kick,
    discord.AuditLogAction.ban,
    discord.AuditLogAction.unban,
    discord.AuditLogAction.member_update,  # timeouts live here
)

PUNISHMENT_TYPES = {
    'timeout':         {'label': 'Timeout',         'emoji': '⏱️', 'colour': 0xE74C3C},
    'timeout_removed': {'label': 'Timeout Removed', 'emoji': '🔓', 'colour': 0x2ECC71},
    'kick':            {'label': 'Kick',            'emoji': '👢', 'colour': 0xE67E22},
    'ban':             {'label': 'Ban',             'emoji': '🔨', 'colour': 0xC0392B},
    'unban':           {'label': 'Unban',           'emoji': '✅', 'colour': 0x27AE60},
}
COUNTED_TYPES = ('timeout', 'kick', 'ban')


def format_duration(seconds):
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds} second{'s' if seconds != 1 else ''}"
    parts = []
    for unit, size in (('day', 86400), ('hour', 3600), ('minute', 60)):
        amount, seconds = divmod(seconds, size)
        if amount:
            parts.append(f"{amount} {unit}{'s' if amount != 1 else ''}")
    return ' '.join(parts[:2])


def records_from_entry(entry):
    """Turn an audit log entry into (user_id, record) pairs. Most entries give one, some give none."""
    target_id = getattr(entry.target, 'id', None)
    if target_id is None:
        return []

    base = {
        'id': entry.id,
        'moderator_id': entry.user_id,
        'moderator_name': str(entry.user) if entry.user else None,
        'reason': entry.reason,
        'at': entry.created_at.isoformat(),
    }

    if entry.action is discord.AuditLogAction.kick:
        return [(target_id, {**base, 'type': 'kick'})]
    if entry.action is discord.AuditLogAction.ban:
        return [(target_id, {**base, 'type': 'ban'})]
    if entry.action is discord.AuditLogAction.unban:
        return [(target_id, {**base, 'type': 'unban'})]

    # member_update covers nicknames and more, only the timeout change matters here
    try:
        before = getattr(entry.before, 'timed_out_until', None)
        after = getattr(entry.after, 'timed_out_until', None)
    except Exception:
        return []
    if after and after > entry.created_at:
        return [(target_id, {**base, 'type': 'timeout', 'until': after.isoformat()})]
    if before and not after:
        return [(target_id, {**base, 'type': 'timeout_removed'})]
    return []


class Moderation(commands.Cog):
    """Keeps a punishment history for every member and shows it with /user."""

    def __init__(self, bot):
        self.bot = bot
        self.data_dir = 'data'
        self.history_file = os.path.join(self.data_dir, 'punishments.json')
        os.makedirs(self.data_dir, exist_ok=True)
        if not os.path.exists(self.history_file):
            self.save_json(self.history_file, {})
        self.history = self.load_json(self.history_file)
        self.lock = asyncio.Lock()
        self.backfill_task = self.bot.loop.create_task(self.backfill_all())

    async def cog_unload(self):
        self.backfill_task.cancel()

    # ------------------------------------------------------------------ #
    #  Storage                                                            #
    # ------------------------------------------------------------------ #

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

    def guild_data(self, guild_id):
        return self.history.setdefault(str(guild_id), {'last_audit_id': None, 'users': {}})

    def user_records(self, guild_id, user_id):
        return self.guild_data(guild_id)['users'].get(str(user_id), [])

    def add_records(self, guild_id, pairs):
        """Store (user_id, record) pairs, skipping audit entries already seen. Returns how many were new."""
        data = self.guild_data(guild_id)
        seen = {r['id'] for records in data['users'].values() for r in records}
        added = 0
        for user_id, record in pairs:
            if record['id'] in seen:
                continue
            seen.add(record['id'])
            data['users'].setdefault(str(user_id), []).append(record)
            data['last_audit_id'] = max(data['last_audit_id'] or 0, record['id'])
            added += 1
        if added:
            for records in data['users'].values():
                records.sort(key=lambda r: r['at'])
            self.save_json(self.history_file, self.history)
        return added

    # ------------------------------------------------------------------ #
    #  Collecting punishments                                             #
    # ------------------------------------------------------------------ #

    @commands.Cog.listener()
    async def on_audit_log_entry_create(self, entry: discord.AuditLogEntry):
        if entry.action not in TRACKED_ACTIONS:
            return
        async with self.lock:
            self.add_records(entry.guild.id, records_from_entry(entry))

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        await self.backfill(guild)

    async def backfill_all(self):
        """Import anything that happened while the bot was offline (or before it was added)."""
        await self.bot.wait_until_ready()
        for guild in self.bot.guilds:
            await self.backfill(guild)

    async def backfill(self, guild):
        me = guild.me
        if not me or not me.guild_permissions.view_audit_log:
            print(f"[Moderation] Can't import punishment history for {guild.name}: missing View Audit Log permission")
            return

        last_id = self.guild_data(guild.id)['last_audit_id']
        after = discord.Object(id=last_id) if last_id else datetime.now(timezone.utc) - timedelta(days=BACKFILL_DAYS)

        pairs = []
        for action in TRACKED_ACTIONS:
            try:
                async for entry in guild.audit_logs(action=action, after=after, limit=BACKFILL_LIMIT_PER_ACTION):
                    pairs.extend(records_from_entry(entry))
            except (discord.Forbidden, discord.HTTPException) as e:
                print(f"[Moderation] Failed to read {action.name} audit logs for {guild.name}: {e}")

        async with self.lock:
            added = self.add_records(guild.id, pairs)
        if added:
            print(f"[Moderation] Imported {added} punishment(s) for {guild.name}")

    # ------------------------------------------------------------------ #
    #  Displaying history                                                 #
    # ------------------------------------------------------------------ #

    def describe_moderator(self, guild, record):
        mod_id = record.get('moderator_id')
        if not mod_id:
            return 'Unknown'
        member = guild.get_member(mod_id) or self.bot.get_user(mod_id)
        name = str(member) if member else record.get('moderator_name')
        if self.bot.user and mod_id == self.bot.user.id:
            name = f'{name or self.bot.user} (AutoMod)'
        return f'<@{mod_id}>' + (f' `{name}`' if name else '')

    def build_history_embed(self, guild, user, records, page, total_pages):
        counts = {t: sum(1 for r in records if r['type'] == t) for t in COUNTED_TYPES}
        summary = ' · '.join(f"**{counts[t]}** {PUNISHMENT_TYPES[t]['label'].lower()}{'s' if counts[t] != 1 else ''}" for t in COUNTED_TYPES)

        embed = discord.Embed(
            title='Punishment History',
            colour=0xE74C3C if any(counts.values()) else 0x2ECC71,
            description=f'{user.mention}\n{summary}',
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_author(name=str(user), icon_url=user.display_avatar.url)
        embed.set_thumbnail(url=user.display_avatar.url)

        member = guild.get_member(user.id)
        status = []
        if member is None:
            status.append('Not in the server')
        elif member.timed_out_until and member.timed_out_until > datetime.now(timezone.utc):
            status.append(f'Currently timed out, ends {discord.utils.format_dt(member.timed_out_until, "R")}')
        if status:
            embed.description += '\n' + ' · '.join(status)

        if not records:
            embed.add_field(name='No punishments on record', value='This user has a clean history.', inline=False)
        else:
            start = page * ENTRIES_PER_PAGE
            # Newest first, numbered from the oldest so numbers stay the same as new entries arrive
            for index, record in list(enumerate(records, start=1))[::-1][start:start + ENTRIES_PER_PAGE]:
                embed.add_field(name=self.format_record_name(index, record), value=self.format_record(guild, record), inline=False)

        footer = f'User ID: {user.id}'
        if total_pages > 1:
            footer = f'Page {page + 1}/{total_pages}  •  {len(records)} entries  •  ' + footer
        embed.set_footer(text=footer)
        return embed

    def format_record_name(self, index, record):
        meta = PUNISHMENT_TYPES[record['type']]
        return f"#{index}  {meta['emoji']} {meta['label']}"

    def format_record(self, guild, record):
        when = datetime.fromisoformat(record['at'])
        lines = [f"{discord.utils.format_dt(when, 'f')} ({discord.utils.format_dt(when, 'R')})"]
        lines.append(f"**By:** {self.describe_moderator(guild, record)}")
        if record['type'] == 'timeout' and record.get('until'):
            until = datetime.fromisoformat(record['until'])
            lines.append(f"**Duration:** {format_duration((until - when).total_seconds())}")
        reason = record.get('reason') or '*No reason provided*'
        lines.append(f"**Reason:** {reason[:400]}")
        return '\n'.join(lines)

    @app_commands.command(name='user', description="View a user's punishment history (timeouts, kicks and bans)")
    @app_commands.describe(user='The user to look up')
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.guild_only()
    async def user_history(self, interaction: discord.Interaction, user: discord.User):
        records = self.user_records(interaction.guild.id, user.id)
        view = HistoryView(self, interaction, user, records)
        await interaction.response.send_message(
            embed=view.build_embed(), view=view if view.total_pages > 1 else discord.utils.MISSING, ephemeral=True
        )

        if not interaction.guild.me.guild_permissions.view_audit_log:
            await interaction.followup.send(
                "⚠️ I don't have the `View Audit Log` permission, so new punishments aren't being recorded.",
                ephemeral=True,
            )


class HistoryView(discord.ui.View):
    def __init__(self, cog, interaction, user, records):
        super().__init__(timeout=180)
        self.cog = cog
        self.interaction = interaction
        self.user = user
        self.records = records
        self.page = 0
        self.total_pages = max(1, -(-len(records) // ENTRIES_PER_PAGE))
        self.update_buttons()

    def build_embed(self):
        return self.cog.build_history_embed(self.interaction.guild, self.user, self.records, self.page, self.total_pages)

    def update_buttons(self):
        self.previous_page.disabled = self.page == 0
        self.next_page.disabled = self.page >= self.total_pages - 1

    async def interaction_check(self, interaction: discord.Interaction):
        if interaction.user.id != self.interaction.user.id:
            await interaction.response.send_message('Run `/user` yourself to browse this history.', ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        try:
            await self.interaction.edit_original_response(view=self)
        except discord.HTTPException:
            pass

    async def show_page(self, interaction, page):
        self.page = page
        self.update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label='Previous', emoji='◀️', style=discord.ButtonStyle.secondary)
    async def previous_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.show_page(interaction, self.page - 1)

    @discord.ui.button(label='Next', emoji='▶️', style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.show_page(interaction, self.page + 1)


async def setup(bot):
    await bot.add_cog(Moderation(bot))
