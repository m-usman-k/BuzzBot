import discord
from discord.ext import commands
from discord import app_commands
import os
import json
import re
import time
import asyncio
import hashlib
from collections import deque, namedtuple
from datetime import datetime, timezone, timedelta
from urllib.parse import urlsplit

import aiohttp

from config import AUTOMOD_WORDLIST_PATH, AUTOMOD_LINK_WHITELIST_PATH, VIRUSTOTAL_REQUESTS_PER_MINUTE
from utils.word_filter import WordFilter
from utils.virustotal import VirusTotalClient, VirusTotalError

# Fast message spam
FAST_SPAM_MAX_MESSAGES = 6
FAST_SPAM_INTERVAL = 5
FAST_SPAM_MAX_DUPLICATES = 5
DUPLICATE_WINDOW = 30

# Multi channel spam
SPAM_THRESHOLD_CHANNELS = 3
SPAM_TIME_WINDOW = 30
SPAM_TIMEOUT_DURATION = 3600
SPAM_DELETE_WINDOW = 1800

# Once a spam filter fires, the user's next messages are deleted silently for this long
SPAM_COOLDOWN = 15

# Word spam (the same text pasted over and over inside one message)
WORD_SPAM_MAX_REPEATS = 5
WORD_SPAM_MIN_LENGTH = 30

# Malicious links
LINK_MALICIOUS_THRESHOLD = 2
LINK_MAX_PER_MESSAGE = 5
LINK_REPORT_CHANNEL_NAME = 'link-checker'

INVITE_CACHE_TTL = 3600

NATIVE_RULE_NAME = 'BuzzBot Language Filter'
NATIVE_BLOCK_MESSAGE = "Your message was blocked because it contains language that isn't allowed in this server."

DISCORD_INVITE_PATTERN = re.compile(
    r'(?:discord(?:app)?\.com/invite|discord\.gg)/([a-zA-Z0-9\-]+)',
    re.IGNORECASE
)
THIRD_PARTY_INVITE_PATTERN = re.compile(r'(?:discord\.me|discord\.io|dsc\.gg|invite\.gg)/[a-zA-Z0-9\-]+', re.IGNORECASE)
URL_PATTERN = re.compile(r'https?://[^\s<>"\'`|]+', re.IGNORECASE)

SettingField = namedtuple('SettingField', 'key label minimum maximum scale', defaults=(1,))
TIMEOUT_FIELD = SettingField('timeout_duration', 'Timeout in minutes (0 = no timeout)', 0, 40320, 60)

FILTERS = {
    'fast_spam': {
        'label': 'Fast Message Spam',
        'emoji': '⚡',
        'colour': 0xE74C3C,
        'summary': 'Catches users sending lots of messages very quickly, or the same message over and over.',
        'defaults': {
            'max_messages': FAST_SPAM_MAX_MESSAGES,
            'interval': FAST_SPAM_INTERVAL,
            'max_duplicates': FAST_SPAM_MAX_DUPLICATES,
            'timeout_duration': 600,
        },
        'fields': [
            SettingField('max_messages', 'Messages allowed in the time window', 2, 50),
            SettingField('interval', 'Time window in seconds', 1, 60),
            SettingField('max_duplicates', f'Identical messages allowed ({DUPLICATE_WINDOW}s)', 2, 50),
            TIMEOUT_FIELD,
        ],
    },
    'multi_channel_spam': {
        'label': 'Multi-Channel Spam',
        'emoji': '📢',
        'colour': 0xE74C3C,
        'summary': 'Catches users posting the same content across several channels.',
        'defaults': {
            'threshold': SPAM_THRESHOLD_CHANNELS,
            'timeout_duration': SPAM_TIMEOUT_DURATION,
            'delete_window': SPAM_DELETE_WINDOW,
        },
        'fields': [
            SettingField('threshold', 'Channels before triggering', 2, 25),
            TIMEOUT_FIELD,
            SettingField('delete_window', 'Delete their messages from last X minutes', 1, 1440, 60),
        ],
    },
    'word_spam': {
        'label': 'Word Spam',
        'emoji': '🔁',
        'colour': 0xE67E22,
        'summary': 'Catches messages that repeat the same text over and over, like copy and paste spam.',
        'defaults': {
            'max_repeats': WORD_SPAM_MAX_REPEATS,
            'timeout_duration': 0,
        },
        'fields': [
            SettingField('max_repeats', 'Repeats in one message before triggering', 3, 100),
            TIMEOUT_FIELD,
        ],
    },
    'promotion': {
        'label': 'Promotion',
        'emoji': '📣',
        'colour': 0xF39C12,
        'summary': 'Removes Discord invite links to other servers. Invites to this server are allowed.',
        'defaults': {
            'timeout_duration': 0,
        },
        'fields': [TIMEOUT_FIELD],
    },
    'language': {
        'label': 'Language',
        'emoji': '🤬',
        'colour': 0x9B59B6,
        'summary': 'Removes messages containing words from the wordlist, including disguised spellings.',
        'defaults': {
            'timeout_duration': 0,
            'use_native': True,
            'native_rule_id': None,
        },
        'fields': [TIMEOUT_FIELD],
    },
    'malicious_links': {
        'label': 'Malicious Links',
        'emoji': '🔗',
        'colour': 0xC0392B,
        'summary': 'Scans links with VirusTotal, reports the results and removes malicious links.',
        'defaults': {
            'report_channel_id': None,
            'malicious_threshold': LINK_MALICIOUS_THRESHOLD,
            'timeout_duration': 0,
        },
        'fields': [
            SettingField('malicious_threshold', 'Engines flagging a link before removal', 1, 20),
            TIMEOUT_FIELD,
        ],
    },
}

FILTER_CHOICES = [app_commands.Choice(name=meta['label'], value=key) for key, meta in FILTERS.items()]


def find_repeated_content(text, max_repeats):
    """Return (repeated text, count) if a chunk of text is repeated back to back at least max_repeats times."""
    normalised = re.sub(r'\s+', ' ', text.casefold()).strip() + ' '
    if len(normalised) < WORD_SPAM_MIN_LENGTH:
        return None
    pattern = re.compile(r'(.{3,200}?)\1{%d,}' % (max_repeats - 1))
    for match in pattern.finditer(normalised):
        unit = match.group(1)
        if len(match.group(0)) < WORD_SPAM_MIN_LENGTH:
            continue
        # Ignore laughter and keyboard mashing like "hahaha" or "lol lol lol"
        if len(set(unit.replace(' ', ''))) < 3:
            continue
        return unit.strip(), len(match.group(0)) // len(unit)
    return None


def extract_urls(text):
    urls = []
    for match in URL_PATTERN.finditer(text):
        url = match.group(0).rstrip('.,;:!?)]}*_~>')
        if url not in urls:
            urls.append(url)
    return urls


def defang(url):
    return url.replace('http', 'hxxp', 1).replace('.', '[.]')


def format_minutes(seconds):
    if not seconds:
        return 'Off'
    minutes = seconds // 60
    if minutes >= 1440 and minutes % 1440 == 0:
        return f"{minutes // 1440}d"
    if minutes >= 60 and minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"


class DomainWhitelist:
    """Domains that are never sent to VirusTotal, reloaded when the file changes."""

    def __init__(self, path):
        self.path = path
        self._mtime = None
        self.domains = set()
        self.reload_if_changed()

    def __len__(self):
        return len(self.domains)

    def reload_if_changed(self):
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            mtime = None
        if mtime == self._mtime:
            return False
        self._mtime = mtime
        domains = set()
        if mtime is not None:
            try:
                with open(self.path, 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.split('#', 1)[0].strip().lower()
                        if line:
                            domains.add(line.removeprefix('*.').removeprefix('www.'))
            except (IOError, UnicodeDecodeError) as e:
                print(f"[AutoMod] Could not read link whitelist {self.path}: {e}")
        self.domains = domains
        print(f"[AutoMod] Loaded {len(domains)} whitelisted domain(s)")
        return True

    def allows(self, url):
        try:
            host = (urlsplit(url).hostname or '').lower().rstrip('.')
        except ValueError:
            return False
        return any(host == d or host.endswith('.' + d) for d in self.domains)


class AutoMod(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.data_dir = 'data'
        self.settings_file = os.path.join(self.data_dir, 'automod_settings.json')
        self.audit_settings_file = os.path.join(self.data_dir, 'audit_settings.json')
        self.init_data_files()
        self.settings = self.load_settings()

        self.word_filter = WordFilter(AUTOMOD_WORDLIST_PATH)
        self.link_whitelist = DomainWhitelist(AUTOMOD_LINK_WHITELIST_PATH)
        api_key = os.getenv('VIRUSTOTAL_API_KEY')
        self.virustotal = VirusTotalClient(api_key, VIRUSTOTAL_REQUESTS_PER_MINUTE) if api_key else None

        self.message_history = {}    # (guild_id, user_id) -> deque of (time, channel_id, message_id, content_hash)
        self.spam_tracking = {}      # (guild_id, user_id) -> {content_hash: {'channels': {...}, 'first_seen': ...}}
        self.spam_cooldowns = {}     # (guild_id, user_id) -> time the cooldown ends
        self.invite_cache = {}       # invite code -> (expires, guild_id or None)
        self.native_status = {}      # guild_id -> description of the Discord AutoMod rule state
        self.native_locks = {}
        self.link_tasks = set()

        self.cleanup_task = self.bot.loop.create_task(self._maintenance_loop())
        self.native_sync_task = self.bot.loop.create_task(self._initial_native_sync())

    async def cog_unload(self):
        self.cleanup_task.cancel()
        self.native_sync_task.cancel()
        for task in self.link_tasks:
            task.cancel()
        if self.virustotal:
            await self.virustotal.close()

    # ------------------------------------------------------------------ #
    #  Settings                                                           #
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

    def load_settings(self):
        data = self.load_json(self.settings_file)
        changed = False
        for guild_cfg in data.values():
            changed |= self.migrate_guild_settings(guild_cfg)
        if changed:
            self.save_json(self.settings_file, data)
        return data

    @staticmethod
    def migrate_guild_settings(guild_cfg):
        """Split the old channel_spam/advertising filters and fill in defaults. Returns True if anything changed."""
        changed = False

        def shared(old):
            return {k: (list(old[k]) if isinstance(old[k], list) else old[k])
                    for k in ('enabled', 'immune_roles', 'immune_channels') if k in old}

        old_spam = guild_cfg.pop('channel_spam', None)
        if old_spam is not None:
            multi = shared(old_spam)
            for key in ('threshold', 'timeout_duration', 'delete_window'):
                if key in old_spam:
                    multi[key] = old_spam[key]
            guild_cfg.setdefault('multi_channel_spam', multi)

            fast = shared(old_spam)
            if 'max_repeats' in old_spam:
                fast['max_duplicates'] = old_spam['max_repeats']
            if 'timeout_duration' in old_spam:
                fast['timeout_duration'] = old_spam['timeout_duration']
            guild_cfg.setdefault('fast_spam', fast)
            changed = True

        old_ads = guild_cfg.pop('advertising', None)
        if old_ads is not None:
            guild_cfg.setdefault('promotion', shared(old_ads))
            changed = True

        for key, meta in FILTERS.items():
            filter_cfg = guild_cfg.setdefault(key, {})
            defaults = {'enabled': False, 'immune_roles': [], 'immune_channels': [], **meta['defaults']}
            for setting, value in defaults.items():
                if setting not in filter_cfg:
                    filter_cfg[setting] = list(value) if isinstance(value, list) else value
                    changed = True
        return changed

    def get_guild_settings(self, guild_id):
        gid = str(guild_id)
        if gid not in self.settings:
            self.settings[gid] = {}
            self.migrate_guild_settings(self.settings[gid])
            self.save_json(self.settings_file, self.settings)
        return self.settings[gid]

    def get_filter(self, guild_id, filter_name):
        return self.get_guild_settings(guild_id)[filter_name]

    def set_filter_setting(self, guild_id, filter_name, key, value):
        self.get_filter(guild_id, filter_name)[key] = value
        self.save_json(self.settings_file, self.settings)

    async def on_filter_changed(self, guild, filter_name):
        if filter_name == 'language':
            await self.sync_native_rule(guild)

    def is_immune(self, member, channel, filter_cfg):
        immune_roles = filter_cfg.get('immune_roles')
        if immune_roles and isinstance(member, discord.Member):
            if any(str(r.id) in immune_roles for r in member.roles):
                return True
        immune_channels = filter_cfg.get('immune_channels')
        if immune_channels and channel is not None:
            # Immunity on a channel also covers its threads, and on a category covers its channels
            related = {channel.id, getattr(channel, 'parent_id', None), getattr(channel, 'category_id', None)}
            parent = getattr(channel, 'parent', None)
            if parent is not None:
                related.add(getattr(parent, 'category_id', None))
            if any(str(c) in immune_channels for c in related if c):
                return True
        return False

    def active_filter(self, message, filter_name):
        """Return the filter's settings if it should run on this message, otherwise None."""
        filter_cfg = self.get_filter(message.guild.id, filter_name)
        if not filter_cfg.get('enabled'):
            return None
        if self.is_immune(message.author, message.channel, filter_cfg):
            return None
        return filter_cfg

    # ------------------------------------------------------------------ #
    #  Actions                                                            #
    # ------------------------------------------------------------------ #

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

    async def delete_messages(self, guild, message_refs):
        """Delete (channel_id, message_id) pairs, bulk deleting where possible."""
        by_channel = {}
        for cid, mid in message_refs:
            by_channel.setdefault(cid, set()).add(mid)

        deleted = 0
        for cid, mids in by_channel.items():
            channel = guild.get_channel_or_thread(cid)
            if not channel:
                continue
            if len(mids) > 1 and hasattr(channel, 'delete_messages'):
                try:
                    await channel.delete_messages([discord.Object(id=m) for m in mids], reason="AutoMod")
                    deleted += len(mids)
                    continue
                except (discord.Forbidden, discord.HTTPException):
                    pass
            for mid in mids:
                try:
                    await channel.get_partial_message(mid).delete()
                    deleted += 1
                except (discord.NotFound, discord.Forbidden):
                    pass
                except discord.HTTPException as e:
                    print(f"[AutoMod] Failed to delete message {mid}: {e}")
        return deleted

    async def purge_recent(self, guild, member, channel_ids, window_seconds):
        """Delete the member's recent messages in the given channels."""
        after = discord.utils.utcnow() - timedelta(seconds=window_seconds)
        deleted = 0
        for cid in channel_ids:
            channel = guild.get_channel_or_thread(cid)
            if not channel or not hasattr(channel, 'purge'):
                continue
            try:
                removed = await channel.purge(
                    limit=100, after=after, check=lambda m: m.author.id == member.id, reason="AutoMod: spam cleanup"
                )
                deleted += len(removed)
            except (discord.Forbidden, discord.HTTPException):
                pass
        return deleted

    async def timeout_member(self, member, seconds, reason):
        """Returns True if the timeout was applied."""
        if member.id == member.guild.owner_id:
            print(f"[AutoMod] Cannot timeout server owner {member.id}")
            return False
        until = discord.utils.utcnow() + timedelta(seconds=min(seconds, 28 * 24 * 3600))
        try:
            await member.timeout(until, reason=reason)
            return True
        except discord.Forbidden:
            print(f"[AutoMod] Failed to timeout {member.id} in {member.guild.id}: Missing Moderate Members permission or bot role is not high enough")
        except discord.HTTPException as e:
            print(f"[AutoMod] Failed to timeout {member.id} in {member.guild.id}: {e}")
        return False

    async def take_action(self, filter_name, filter_cfg, member, *, channel, reason, detected,
                          messages=(), purge_channels=(), warning=None, note=None):
        """Delete, time out, warn and log in one place so every filter behaves the same way."""
        guild = member.guild
        meta = FILTERS[filter_name]

        deleted = await self.delete_messages(guild, messages)
        if purge_channels:
            deleted += await self.purge_recent(
                guild, member, purge_channels, filter_cfg.get('delete_window', SPAM_DELETE_WINDOW)
            )

        timeout_seconds = filter_cfg.get('timeout_duration', 0)
        timed_out = None
        if timeout_seconds and isinstance(member, discord.Member):
            timed_out = await self.timeout_member(member, timeout_seconds, f"AutoMod: {reason}")

        if warning and channel:
            try:
                await channel.send(
                    f"{member.mention} {warning}",
                    delete_after=10,
                    allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
                )
            except (discord.Forbidden, discord.HTTPException):
                pass

        if timed_out:
            try:
                dm_embed = discord.Embed(
                    title="Automatic Moderation Action",
                    colour=meta['colour'],
                    description=(
                        f"You have been timed out in **{guild.name}** for **{format_minutes(timeout_seconds)}**.\n\n"
                        f"**Reason:** {reason}\n\n"
                        "If you believe this was a mistake, please contact a server administrator."
                    ),
                    timestamp=datetime.now(timezone.utc),
                )
                await member.send(embed=dm_embed)
            except (discord.Forbidden, discord.HTTPException):
                pass

        actions = []
        if deleted:
            actions.append(f"Deleted {deleted} message(s)")
        if timed_out:
            actions.append(f"Timed out for {format_minutes(timeout_seconds)}")
        elif timed_out is False:
            actions.append("Timeout failed (check permissions)")
        if note:
            actions.append(note)

        log_embed = discord.Embed(
            title=f"AutoMod: {meta['label']}",
            colour=meta['colour'],
            description=reason,
            timestamp=datetime.now(timezone.utc),
        )
        log_embed.set_author(name=str(member), icon_url=member.display_avatar.url)
        log_embed.add_field(name="User", value=member.mention, inline=True)
        if channel:
            log_embed.add_field(name="Channel", value=channel.mention, inline=True)
        log_embed.add_field(name="Action", value='\n'.join(actions) if actions else "Logged only", inline=True)
        log_embed.add_field(name="Detected", value=detected[:1000] if detected else "*None*", inline=False)
        log_embed.set_footer(text=f"User ID: {member.id}")
        await self.send_mod_log(guild, log_embed)

    def start_spam_cooldown(self, key):
        self.spam_cooldowns[key] = time.time() + SPAM_COOLDOWN
        self.message_history.pop(key, None)
        self.spam_tracking.pop(key, None)

    # ------------------------------------------------------------------ #
    #  Content filters                                                    #
    # ------------------------------------------------------------------ #

    async def check_language(self, message):
        filter_cfg = self.active_filter(message, 'language')
        if not filter_cfg or not message.content:
            return False
        match = self.word_filter.find(message.content)
        if not match:
            return False
        entry, offending = match
        await self.take_action(
            'language', filter_cfg, message.author,
            channel=message.channel,
            reason="Message contained a filtered word",
            detected=f"`{offending[:200]}` (matched wordlist entry `{entry}`)\n\n{message.content[:700]}",
            messages=[(message.channel.id, message.id)],
            warning="please watch your language.",
        )
        return True

    async def resolve_invite_guild(self, code):
        """Return the guild ID an invite points to, or None if it can't be resolved."""
        cached = self.invite_cache.get(code)
        if cached and cached[0] > time.time():
            return cached[1]
        try:
            invite = await self.bot.fetch_invite(code, with_counts=False)
            guild_id = invite.guild.id if invite.guild else None
        except discord.NotFound:
            guild_id = None
        except discord.HTTPException:
            return None
        self.invite_cache[code] = (time.time() + INVITE_CACHE_TTL, guild_id)
        return guild_id

    async def check_promotion(self, message):
        filter_cfg = self.active_filter(message, 'promotion')
        content = message.content.strip()
        if not filter_cfg or not content:
            return False

        codes = DISCORD_INVITE_PATTERN.findall(content)
        third_party = THIRD_PARTY_INVITE_PATTERN.search(content)
        if not codes and not third_party:
            return False
        if not third_party:
            targets = [await self.resolve_invite_guild(code) for code in dict.fromkeys(codes)]
            if all(t == message.guild.id for t in targets):
                return False

        await self.take_action(
            'promotion', filter_cfg, message.author,
            channel=message.channel,
            reason="Posted an invite link to another server",
            detected=content[:1000],
            messages=[(message.channel.id, message.id)],
            warning="Discord invite links are not allowed in this channel. Please use the appropriate channel for promotions.",
        )
        return True

    async def check_word_spam(self, message):
        filter_cfg = self.active_filter(message, 'word_spam')
        if not filter_cfg or not message.content:
            return False
        repeated = find_repeated_content(message.content, filter_cfg.get('max_repeats', WORD_SPAM_MAX_REPEATS))
        if not repeated:
            return False
        unit, count = repeated
        await self.take_action(
            'word_spam', filter_cfg, message.author,
            channel=message.channel,
            reason=f"Repeated the same text {count} times in one message",
            detected=f"`{unit[:200]}` x{count}",
            messages=[(message.channel.id, message.id)],
            warning="please don't spam the same thing over and over.",
        )
        return True

    def check_links(self, message, already_scanned=()):
        """Queue a VirusTotal scan for any non-whitelisted links. Scans run in the background."""
        filter_cfg = self.active_filter(message, 'malicious_links')
        if not filter_cfg or not self.virustotal or not message.content:
            return
        urls = [
            u for u in extract_urls(message.content)
            if u not in already_scanned and not self.link_whitelist.allows(u)
        ]
        if not urls:
            return
        task = asyncio.create_task(self.scan_message_links(message, urls[:LINK_MAX_PER_MESSAGE]))
        self.link_tasks.add(task)
        task.add_done_callback(self.link_tasks.discard)

    def get_link_report_channel(self, guild, filter_cfg):
        channel_id = filter_cfg.get('report_channel_id')
        if channel_id:
            return guild.get_channel(int(channel_id))
        return discord.utils.get(guild.text_channels, name=LINK_REPORT_CHANNEL_NAME)

    async def scan_message_links(self, message, urls):
        guild = message.guild
        removed = False
        for url in urls:
            try:
                result = await self.virustotal.scan(url)
            except (VirusTotalError, aiohttp.ClientError, asyncio.TimeoutError, KeyError) as e:
                print(f"[AutoMod] VirusTotal scan failed for {url}: {e}")
                continue

            filter_cfg = self.get_filter(guild.id, 'malicious_links')
            threshold = filter_cfg.get('malicious_threshold', LINK_MALICIOUS_THRESHOLD)
            if result.malicious >= threshold:
                verdict, colour = "⛔ Malicious", 0xE74C3C
            elif result.malicious or result.suspicious:
                verdict, colour = "⚠️ Suspicious", 0xF39C12
            else:
                verdict, colour = "✅ Clean", 0x2ECC71
            is_malicious = result.malicious >= threshold

            report = discord.Embed(title=f"Link Scan: {verdict}", colour=colour, timestamp=datetime.now(timezone.utc))
            report.set_author(name=str(message.author), icon_url=message.author.display_avatar.url)
            report.add_field(
                name="Link",
                value=f"`{defang(url)[:1000]}`" if (result.malicious or result.suspicious) else f"`{url[:1000]}`",
                inline=False,
            )
            report.add_field(name="User", value=message.author.mention, inline=True)
            report.add_field(name="Channel", value=message.channel.mention, inline=True)
            report.add_field(
                name="Detections",
                value=(
                    f"**{result.malicious}** malicious, **{result.suspicious}** suspicious, "
                    f"**{result.harmless + result.undetected}** clean of {result.total} engines"
                ),
                inline=False,
            )
            if result.flagged_by:
                flagged = ', '.join(result.flagged_by[:15])
                if len(result.flagged_by) > 15:
                    flagged += f" and {len(result.flagged_by) - 15} more"
                report.add_field(name="Flagged By", value=flagged, inline=False)
            if is_malicious:
                report.add_field(name="Action", value="Message removed", inline=False)
            elif not removed:
                report.add_field(name="Message", value=f"[Jump to message]({message.jump_url})", inline=False)
            report.add_field(name="Full Report", value=f"[View on VirusTotal]({result.permalink})", inline=False)
            report.set_footer(text=f"User ID: {message.author.id}")

            report_channel = self.get_link_report_channel(guild, filter_cfg)
            if report_channel:
                try:
                    await report_channel.send(embed=report)
                except (discord.Forbidden, discord.HTTPException):
                    pass

            if is_malicious and not removed:
                removed = True
                await self.take_action(
                    'malicious_links', filter_cfg, message.author,
                    channel=message.channel,
                    reason=f"Posted a link flagged as malicious by {result.malicious} security vendors",
                    detected=f"`{defang(url)[:900]}`",
                    messages=[(message.channel.id, message.id)],
                    warning="that link was flagged as malicious and has been removed.",
                )

    async def run_content_filters(self, message, already_scanned=()):
        """Filters that only look at the message itself. Returns True if the message was removed."""
        for check in (self.check_language, self.check_promotion, self.check_word_spam):
            if await check(message):
                return True
        self.check_links(message, already_scanned)
        return False

    # ------------------------------------------------------------------ #
    #  Spam filters                                                       #
    # ------------------------------------------------------------------ #

    @staticmethod
    def message_signature(message):
        parts = [message.content.strip()]
        for a in message.attachments:
            parts.append(f"[ATTACHMENT: {a.filename}]")
        for s in message.stickers:
            parts.append(f"[STICKER: {s.id}]")
        signature = '|'.join(p for p in parts if p)
        return signature, (hashlib.sha256(signature.encode()).hexdigest() if signature else None)

    async def check_multi_channel_spam(self, message, signature, content_hash):
        filter_cfg = self.active_filter(message, 'multi_channel_spam')
        if not filter_cfg or not content_hash:
            return False

        key = (message.guild.id, message.author.id)
        now = time.time()
        tracked = self.spam_tracking.setdefault(key, {})
        info = tracked.get(content_hash)
        if not info or now - info['first_seen'] > SPAM_TIME_WINDOW:
            info = tracked[content_hash] = {'channels': {}, 'first_seen': now}
        info['channels'].setdefault(message.channel.id, []).append(message.id)

        if len(info['channels']) < filter_cfg.get('threshold', SPAM_THRESHOLD_CHANNELS):
            return False

        channel_ids = list(info['channels'])
        refs = [(cid, mid) for cid, mids in info['channels'].items() for mid in mids]
        self.start_spam_cooldown(key)
        await self.take_action(
            'multi_channel_spam', filter_cfg, message.author,
            channel=message.channel,
            reason=f"Posted identical content in {len(channel_ids)} channels",
            detected=f"**Channels:** {', '.join(f'<#{c}>' for c in channel_ids)}\n\n{signature[:800]}",
            messages=refs,
            purge_channels=channel_ids,
        )
        return True

    async def check_fast_spam(self, message, signature, content_hash):
        filter_cfg = self.active_filter(message, 'fast_spam')
        if not filter_cfg:
            return False

        key = (message.guild.id, message.author.id)
        now = time.time()
        interval = filter_cfg.get('interval', FAST_SPAM_INTERVAL)
        history = self.message_history.setdefault(key, deque())
        history.append((now, message.channel.id, message.id, content_hash))
        while history and now - history[0][0] > max(interval, DUPLICATE_WINDOW):
            history.popleft()

        recent = [h for h in history if now - h[0] <= interval]
        duplicates = [h for h in history if content_hash and h[3] == content_hash]
        if len(recent) >= filter_cfg.get('max_messages', FAST_SPAM_MAX_MESSAGES):
            offending, reason = recent, f"Sent {len(recent)} messages in {interval} seconds"
        elif len(duplicates) >= filter_cfg.get('max_duplicates', FAST_SPAM_MAX_DUPLICATES):
            offending, reason = duplicates, f"Sent the same message {len(duplicates)} times"
        else:
            return False

        self.start_spam_cooldown(key)
        await self.take_action(
            'fast_spam', filter_cfg, message.author,
            channel=message.channel,
            reason=reason,
            detected=signature[:1000] if signature else "*No text content*",
            messages=[(cid, mid) for _, cid, mid, _ in offending],
            warning="please slow down.",
        )
        return True

    # ------------------------------------------------------------------ #
    #  Listeners                                                          #
    # ------------------------------------------------------------------ #

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or not message.guild or not isinstance(message.author, discord.Member):
            return

        key = (message.guild.id, message.author.id)
        if self.spam_cooldowns.get(key, 0) > time.time():
            try:
                await message.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
            return

        if await self.run_content_filters(message):
            return

        signature, content_hash = self.message_signature(message)
        if await self.check_multi_channel_spam(message, signature, content_hash):
            return
        await self.check_fast_spam(message, signature, content_hash)

    @commands.Cog.listener()
    async def on_message_edit(self, before, after):
        # Stops people sending a clean message and editing a filtered word in afterwards
        if after.author.bot or not after.guild or not isinstance(after.author, discord.Member):
            return
        if before.content == after.content:
            return
        await self.run_content_filters(after, already_scanned=extract_urls(before.content))

    @commands.Cog.listener()
    async def on_automod_action(self, execution):
        """Log and punish messages blocked by the Discord AutoMod rule this bot manages."""
        if execution.action.type is not discord.AutoModRuleActionType.block_message:
            return
        guild = execution.guild
        filter_cfg = self.get_filter(guild.id, 'language')
        if not filter_cfg.get('native_rule_id') or execution.rule_id != int(filter_cfg['native_rule_id']):
            return
        member = guild.get_member(execution.user_id)
        if not member:
            return
        matched = execution.matched_content or execution.matched_keyword or ''
        await self.take_action(
            'language', filter_cfg, member,
            channel=execution.channel,
            reason="Message contained a filtered word",
            detected=f"`{matched[:200]}`\n\n{execution.content[:700]}" if execution.content else f"`{matched[:200]}`",
            note="Blocked by Discord AutoMod before it was sent",
        )

    # ------------------------------------------------------------------ #
    #  Discord AutoMod rule for the language filter                       #
    # ------------------------------------------------------------------ #

    async def sync_native_rule(self, guild):
        """Create, update or remove the Discord AutoMod rule that mirrors the wordlist."""
        lock = self.native_locks.setdefault(guild.id, asyncio.Lock())
        async with lock:
            self.native_status[guild.id] = await self._sync_native_rule(guild)
            return self.native_status[guild.id]

    async def _sync_native_rule(self, guild):
        filter_cfg = self.get_filter(guild.id, 'language')
        keywords, regexes, allow = self.word_filter.native_rule_data()
        wanted = filter_cfg.get('enabled') and filter_cfg.get('use_native', True) and (keywords or regexes)

        me = guild.me
        if not me or not me.guild_permissions.manage_guild:
            return "⚠️ Needs the `Manage Server` permission" if wanted else "Off"

        rule = None
        rule_id = filter_cfg.get('native_rule_id')
        try:
            if rule_id:
                try:
                    rule = await guild.fetch_automod_rule(int(rule_id))
                except discord.NotFound:
                    rule = None
            if rule is None:
                # Settings may have been lost, so look for a rule this bot made earlier
                try:
                    existing_rules = await guild.fetch_automod_rules()
                except discord.NotFound:
                    existing_rules = []
                for existing in existing_rules:
                    if existing.name == NATIVE_RULE_NAME and existing.creator_id == self.bot.user.id:
                        rule = existing
                        break

            if not wanted:
                if rule:
                    await rule.delete(reason="BuzzBot language filter disabled")
                if filter_cfg.get('native_rule_id'):
                    self.set_filter_setting(guild.id, 'language', 'native_rule_id', None)
                if filter_cfg.get('enabled') and filter_cfg.get('use_native', True):
                    return "⚠️ Wordlist is empty"
                return "Off"

            options = dict(
                name=NATIVE_RULE_NAME,
                event_type=discord.AutoModRuleEventType.message_send,
                actions=[discord.AutoModRuleAction(custom_message=NATIVE_BLOCK_MESSAGE)],
                enabled=True,
                exempt_roles=[discord.Object(id=int(r)) for r in filter_cfg.get('immune_roles', [])][:20],
                exempt_channels=[discord.Object(id=int(c)) for c in filter_cfg.get('immune_channels', [])][:50],
                reason="BuzzBot language filter sync",
            )

            async def apply(trigger):
                if rule:
                    return await rule.edit(trigger=trigger, **options)
                return await guild.create_automod_rule(trigger=trigger, **options)

            try:
                rule = await apply(discord.AutoModTrigger(
                    type=discord.AutoModRuleTriggerType.keyword,
                    keyword_filter=keywords, regex_patterns=regexes, allow_list=allow,
                ))
                pattern_note = f", {len(regexes)} bypass pattern(s)"
            except discord.HTTPException as e:
                if not keywords:
                    raise
                # Fall back to plain keywords if Discord rejects the generated patterns
                print(f"[AutoMod] Discord rejected the language filter patterns, retrying without them: {e}")
                rule = await apply(discord.AutoModTrigger(
                    type=discord.AutoModRuleTriggerType.keyword, keyword_filter=keywords, allow_list=allow,
                ))
                pattern_note = ""

            if str(rule.id) != str(filter_cfg.get('native_rule_id')):
                self.set_filter_setting(guild.id, 'language', 'native_rule_id', rule.id)
            return f"✅ Active ({len(keywords)} keyword(s){pattern_note})"
        except discord.Forbidden:
            return "⚠️ Needs the `Manage Server` permission"
        except discord.HTTPException as e:
            print(f"[AutoMod] Failed to sync Discord AutoMod rule in {guild.id}: {e}")
            return f"⚠️ Sync failed: {e.text[:100] if e.text else e.status}"

    async def _initial_native_sync(self):
        await self.bot.wait_until_ready()
        for guild in self.bot.guilds:
            filter_cfg = self.settings.get(str(guild.id), {}).get('language', {})
            if filter_cfg.get('enabled') or filter_cfg.get('native_rule_id'):
                await self.sync_native_rule(guild)

    async def _maintenance_loop(self):
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            await asyncio.sleep(60)
            now = time.time()

            for key, history in list(self.message_history.items()):
                while history and now - history[0][0] > 60:
                    history.popleft()
                if not history:
                    del self.message_history[key]
            for key, tracked in list(self.spam_tracking.items()):
                for content_hash, info in list(tracked.items()):
                    if now - info['first_seen'] > SPAM_TIME_WINDOW * 2:
                        del tracked[content_hash]
                if not tracked:
                    del self.spam_tracking[key]
            for key, until in list(self.spam_cooldowns.items()):
                if until <= now:
                    del self.spam_cooldowns[key]
            for code, (expires, _) in list(self.invite_cache.items()):
                if expires <= now:
                    del self.invite_cache[code]

            self.link_whitelist.reload_if_changed()
            if self.word_filter.reload_if_changed():
                for guild in self.bot.guilds:
                    filter_cfg = self.settings.get(str(guild.id), {}).get('language', {})
                    if filter_cfg.get('enabled') or filter_cfg.get('native_rule_id'):
                        await self.sync_native_rule(guild)

    # ------------------------------------------------------------------ #
    #  Status panel embeds                                                #
    # ------------------------------------------------------------------ #

    @staticmethod
    def describe_settings(filter_name, filter_cfg):
        lines = []
        for field in FILTERS[filter_name]['fields']:
            value = filter_cfg.get(field.key, FILTERS[filter_name]['defaults'].get(field.key))
            if field is TIMEOUT_FIELD:
                lines.append(f"Timeout: `{format_minutes(value)}`")
            elif field.scale == 60:
                lines.append(f"{field.label}: `{format_minutes(value)}`")
            else:
                lines.append(f"{field.label}: `{value}`")
        return lines

    def permission_warnings(self, guild):
        me = guild.me
        if not me:
            return []
        perms = me.guild_permissions
        warnings = []
        if not perms.manage_messages:
            warnings.append("Missing `Manage Messages`, so filtered messages can't be deleted.")
        if not perms.moderate_members:
            warnings.append("Missing `Moderate Members`, so timeouts will fail.")
        return warnings

    def build_overview_embed(self, guild):
        settings = self.get_guild_settings(guild.id)
        embed = discord.Embed(
            title="AutoMod Configuration",
            colour=0x3498DB,
            description="Pick a filter from the menu below to change its settings.",
            timestamp=datetime.now(timezone.utc),
        )
        for key, meta in FILTERS.items():
            cfg = settings[key]
            status = '✅ Enabled' if cfg.get('enabled') else '❌ Disabled'
            immune = []
            if cfg.get('immune_roles'):
                immune.append(f"{len(cfg['immune_roles'])} immune role(s)")
            if cfg.get('immune_channels'):
                immune.append(f"{len(cfg['immune_channels'])} immune channel(s)")
            value = ' | '.join(self.describe_settings(key, cfg))
            if immune:
                value += '\n' + ', '.join(immune)
            embed.add_field(name=f"{meta['emoji']} {meta['label']}: {status}", value=value, inline=False)

        warnings = self.permission_warnings(guild)
        audit = self.load_json(self.audit_settings_file).get(str(guild.id), {})
        if not audit.get('mod_channel_id'):
            warnings.append("No mod log channel is set, so AutoMod actions aren't logged. Use `/logs channel mod logs`.")
        if warnings:
            embed.add_field(name="⚠️ Warnings", value='\n'.join(warnings), inline=False)
        return embed

    def build_filter_embed(self, guild, filter_name):
        meta = FILTERS[filter_name]
        cfg = self.get_filter(guild.id, filter_name)
        embed = discord.Embed(
            title=f"{meta['emoji']} {meta['label']}",
            colour=meta['colour'] if cfg.get('enabled') else 0x95A5A6,
            description=meta['summary'],
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Status", value='✅ Enabled' if cfg.get('enabled') else '❌ Disabled', inline=True)
        embed.add_field(name="Settings", value='\n'.join(self.describe_settings(filter_name, cfg)), inline=False)

        if filter_name == 'language':
            embed.add_field(
                name="Wordlist",
                value=f"{len(self.word_filter)} word(s) in `{AUTOMOD_WORDLIST_PATH}`\nChanges are picked up within a minute.",
                inline=False,
            )
            native = self.native_status.get(guild.id, "Not synced yet") if cfg.get('use_native', True) else "Off"
            embed.add_field(
                name="Discord AutoMod",
                value=f"{native}\nBlocks messages before anyone can see them. The bot still catches anything it misses.",
                inline=False,
            )
        elif filter_name == 'malicious_links':
            report_channel = self.get_link_report_channel(guild, cfg)
            embed.add_field(
                name="VirusTotal",
                value="✅ API key loaded" if self.virustotal else "⚠️ Add `VIRUSTOTAL_API_KEY` to your `.env` file",
                inline=True,
            )
            embed.add_field(
                name="Report Channel",
                value=report_channel.mention if report_channel else f"⚠️ Not set (or create #{LINK_REPORT_CHANNEL_NAME})",
                inline=True,
            )
            embed.add_field(
                name="Whitelist",
                value=f"{len(self.link_whitelist)} domain(s) in `{AUTOMOD_LINK_WHITELIST_PATH}`",
                inline=False,
            )

        embed.add_field(name="Immunity", value="Use the menus below to choose immune roles and channels.", inline=False)
        warnings = self.permission_warnings(guild)
        if warnings:
            embed.add_field(name="⚠️ Warnings", value='\n'.join(warnings), inline=False)
        return embed

    # ------------------------------------------------------------------ #
    #  Commands                                                           #
    # ------------------------------------------------------------------ #

    automod = app_commands.Group(name='automod', description='Configure AutoMod filters')

    @automod.command(name='status', description='View and change AutoMod settings')
    @app_commands.default_permissions(administrator=True)
    async def automod_status(self, interaction: discord.Interaction):
        view = AutoModPanel(self, interaction.guild)
        await interaction.response.send_message(embed=view.build_embed(), view=view, ephemeral=True)

    @automod.command(name='toggle', description='Enable or disable an AutoMod filter')
    @app_commands.describe(filter='Which filter to toggle')
    @app_commands.choices(filter=FILTER_CHOICES)
    @app_commands.default_permissions(administrator=True)
    async def automod_toggle(self, interaction: discord.Interaction, filter: app_commands.Choice[str]):
        filter_cfg = self.get_filter(interaction.guild.id, filter.value)
        new_state = not filter_cfg.get('enabled', False)
        self.set_filter_setting(interaction.guild.id, filter.value, 'enabled', new_state)
        await interaction.response.send_message(
            f"{'✅' if new_state else '❌'} {FILTERS[filter.value]['label']} filter is now **{'enabled' if new_state else 'disabled'}**."
        )
        await self.on_filter_changed(interaction.guild, filter.value)

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
        filter_cfg = self.get_filter(interaction.guild.id, filter.value)
        label = FILTERS[filter.value]['label']

        if role is not None or channel is not None:
            setting, target = ('immune_roles', role) if role is not None else ('immune_channels', channel)
            current = list(filter_cfg.get(setting, []))
            if str(target.id) in current:
                current.remove(str(target.id))
                text = f"Removed {target.mention} from {label} filter immunity."
            else:
                current.append(str(target.id))
                text = f"Added {target.mention} to {label} filter immunity."
            self.set_filter_setting(interaction.guild.id, filter.value, setting, current)
            await interaction.response.send_message(text)
            await self.on_filter_changed(interaction.guild, filter.value)
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


# ── Status Panel UI ────────────────────────────────────────────────────────────

OVERVIEW_PAGE = '__overview__'


class FilterSelect(discord.ui.Select):
    def __init__(self, panel):
        self.panel = panel
        options = [discord.SelectOption(
            label="Overview", value=OVERVIEW_PAGE, emoji="🏠",
            description="All filters at a glance", default=panel.page == OVERVIEW_PAGE,
        )]
        for key, meta in FILTERS.items():
            options.append(discord.SelectOption(
                label=meta['label'], value=key, emoji=meta['emoji'],
                description=meta['summary'][:100], default=panel.page == key,
            ))
        super().__init__(placeholder="Choose a filter to configure...", options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        self.panel.page = self.values[0]
        await self.panel.refresh(interaction)


class ImmuneRoleSelect(discord.ui.RoleSelect):
    def __init__(self, panel, filter_cfg):
        guild = panel.guild
        current = [r for r in (guild.get_role(int(rid)) for rid in filter_cfg.get('immune_roles', [])) if r]
        super().__init__(
            placeholder="Immune roles (none)", min_values=0, max_values=25,
            default_values=current[:25], row=1,
        )
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        ids = [str(r.id) for r in self.values]
        self.panel.cog.set_filter_setting(self.panel.guild.id, self.panel.page, 'immune_roles', ids)
        await self.panel.refresh(interaction, sync=True)


class ImmuneChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, panel, filter_cfg):
        guild = panel.guild
        current = [c for c in (guild.get_channel(int(cid)) for cid in filter_cfg.get('immune_channels', [])) if c]
        super().__init__(
            placeholder="Immune channels or categories (none)", min_values=0, max_values=25,
            channel_types=[
                discord.ChannelType.text, discord.ChannelType.news, discord.ChannelType.voice,
                discord.ChannelType.forum, discord.ChannelType.stage_voice, discord.ChannelType.category,
            ],
            default_values=current[:25], row=2,
        )
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        ids = [str(c.id) for c in self.values]
        self.panel.cog.set_filter_setting(self.panel.guild.id, self.panel.page, 'immune_channels', ids)
        await self.panel.refresh(interaction, sync=True)


class ReportChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, panel, filter_cfg):
        channel = panel.guild.get_channel(int(filter_cfg['report_channel_id'])) if filter_cfg.get('report_channel_id') else None
        super().__init__(
            placeholder="Link scan report channel", min_values=0, max_values=1,
            channel_types=[discord.ChannelType.text, discord.ChannelType.news],
            default_values=[channel] if channel else [], row=4,
        )
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        value = self.values[0].id if self.values else None
        self.panel.cog.set_filter_setting(self.panel.guild.id, 'malicious_links', 'report_channel_id', value)
        await self.panel.refresh(interaction)


class SettingsModal(discord.ui.Modal):
    def __init__(self, panel):
        meta = FILTERS[panel.page]
        super().__init__(title=f"{meta['label']} Settings"[:45])
        self.panel = panel
        self.filter_name = panel.page
        filter_cfg = panel.cog.get_filter(panel.guild.id, panel.page)
        self.inputs = []
        for field in meta['fields']:
            current = filter_cfg.get(field.key, meta['defaults'].get(field.key)) or 0
            text_input = discord.ui.TextInput(
                label=field.label[:45],
                default=str(current // field.scale),
                placeholder=f"{field.minimum} to {field.maximum}",
                max_length=6,
            )
            self.add_item(text_input)
            self.inputs.append((field, text_input))

    async def on_submit(self, interaction: discord.Interaction):
        errors = []
        for field, text_input in self.inputs:
            raw = text_input.value.strip()
            try:
                value = int(raw)
            except ValueError:
                errors.append(f"**{field.label}**: `{raw}` isn't a whole number.")
                continue
            if not field.minimum <= value <= field.maximum:
                errors.append(f"**{field.label}**: must be between {field.minimum} and {field.maximum}.")
                continue
            self.panel.cog.set_filter_setting(self.panel.guild.id, self.filter_name, field.key, value * field.scale)
        await self.panel.refresh(interaction)
        if errors:
            await interaction.followup.send("Some settings weren't saved:\n" + '\n'.join(errors), ephemeral=True)


class AutoModPanel(discord.ui.View):
    """Interactive version of the status embed. Lives on an ephemeral message, so only the admin who opened it can use it."""

    def __init__(self, cog, guild, page=OVERVIEW_PAGE):
        super().__init__(timeout=840)  # Interaction tokens expire after 15 minutes
        self.cog = cog
        self.guild = guild
        self.page = page
        self.build()

    def build_embed(self):
        if self.page == OVERVIEW_PAGE:
            return self.cog.build_overview_embed(self.guild)
        return self.cog.build_filter_embed(self.guild, self.page)

    def build(self):
        self.clear_items()
        self.add_item(FilterSelect(self))
        if self.page == OVERVIEW_PAGE:
            return

        filter_cfg = self.cog.get_filter(self.guild.id, self.page)
        self.add_item(ImmuneRoleSelect(self, filter_cfg))
        self.add_item(ImmuneChannelSelect(self, filter_cfg))

        enabled = filter_cfg.get('enabled')
        toggle = discord.ui.Button(
            label="Disable" if enabled else "Enable",
            style=discord.ButtonStyle.danger if enabled else discord.ButtonStyle.success,
            row=3,
        )
        toggle.callback = self.toggle_enabled
        self.add_item(toggle)

        edit = discord.ui.Button(label="Edit Settings", emoji="⚙️", style=discord.ButtonStyle.secondary, row=3)
        edit.callback = self.edit_settings
        self.add_item(edit)

        if self.page == 'language':
            use_native = filter_cfg.get('use_native', True)
            native = discord.ui.Button(
                label=f"Discord AutoMod: {'On' if use_native else 'Off'}",
                style=discord.ButtonStyle.primary if use_native else discord.ButtonStyle.secondary,
                row=3,
            )
            native.callback = self.toggle_native
            self.add_item(native)
        elif self.page == 'malicious_links':
            self.add_item(ReportChannelSelect(self, filter_cfg))

    async def refresh(self, interaction, sync=False):
        """Re-render the panel. With sync=True, also re-sync the Discord AutoMod rule and update again once done."""
        self.build()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)
        if sync and self.page == 'language':
            await self.cog.sync_native_rule(self.guild)
            try:
                await interaction.edit_original_response(embed=self.build_embed(), view=self)
            except discord.HTTPException:
                pass

    async def toggle_enabled(self, interaction: discord.Interaction):
        filter_cfg = self.cog.get_filter(self.guild.id, self.page)
        self.cog.set_filter_setting(self.guild.id, self.page, 'enabled', not filter_cfg.get('enabled'))
        await self.refresh(interaction, sync=True)

    async def toggle_native(self, interaction: discord.Interaction):
        filter_cfg = self.cog.get_filter(self.guild.id, 'language')
        self.cog.set_filter_setting(self.guild.id, 'language', 'use_native', not filter_cfg.get('use_native', True))
        await self.refresh(interaction, sync=True)

    async def edit_settings(self, interaction: discord.Interaction):
        await interaction.response.send_modal(SettingsModal(self))


async def setup(bot):
    await bot.add_cog(AutoMod(bot))
