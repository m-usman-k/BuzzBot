import discord
from discord.ext import commands
from discord import app_commands
import sqlite3
import asyncio
import time
from datetime import datetime, timedelta
from PIL import Image, ImageDraw, ImageFont
from PIL import ImageFilter
import io
import os
import aiohttp

class Levelling(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db_path = 'levelling.db'
        self.init_database()
        self.voice_tracking = {}  # {user_id: {guild_id: start_time}}
        self.message_cooldowns = {}  # {user_id: {guild_id: last_message_time}}
        
        # Default settings (can be configured per guild)
        self.default_xp_per_message = (15, 25)  # min, max XP per message
        self.default_xp_cooldown = 60  # seconds between XP gains
        self.default_vc_xp_per_minute = 1
        
        # Start voice tracking task
        self.bot.loop.create_task(self.voice_xp_loop())
    
    def init_database(self):
        """Initialize the database with required tables"""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        
        # User XP table
        c.execute('''CREATE TABLE IF NOT EXISTS user_xp
                     (user_id INTEGER, guild_id INTEGER, text_xp INTEGER DEFAULT 0,
                      voice_xp INTEGER DEFAULT 0, PRIMARY KEY (user_id, guild_id))''')
        
        # Guild settings table
        c.execute('''CREATE TABLE IF NOT EXISTS guild_settings
                     (guild_id INTEGER PRIMARY KEY, level_channel_id INTEGER,
                      xp_per_message_min INTEGER, xp_per_message_max INTEGER,
                      xp_cooldown INTEGER, vc_xp_per_minute INTEGER)''')
        
        # Role rewards table
        c.execute('''CREATE TABLE IF NOT EXISTS role_rewards
                     (guild_id INTEGER, role_id INTEGER, text_level INTEGER,
                      voice_level INTEGER, PRIMARY KEY (guild_id, role_id))''')
        
        conn.commit()
        conn.close()
    
    def get_db_connection(self):
        """Get a database connection"""
        return sqlite3.connect(self.db_path)
    
    def get_guild_settings(self, guild_id):
        """Get guild settings, return defaults if not set"""
        conn = self.get_db_connection()
        c = conn.cursor()
        c.execute('SELECT * FROM guild_settings WHERE guild_id = ?', (guild_id,))
        result = c.fetchone()
        conn.close()
        
        if result:
            return {
                'level_channel_id': result[1],
                'xp_per_message_min': result[2] or self.default_xp_per_message[0],
                'xp_per_message_max': result[3] or self.default_xp_per_message[1],
                'xp_cooldown': result[4] or self.default_xp_cooldown,
                'vc_xp_per_minute': result[5] or self.default_vc_xp_per_minute
            }
        return {
            'level_channel_id': None,
            'xp_per_message_min': self.default_xp_per_message[0],
            'xp_per_message_max': self.default_xp_per_message[1],
            'xp_cooldown': self.default_xp_cooldown,
            'vc_xp_per_minute': self.default_vc_xp_per_minute
        }
    
    def calculate_level(self, xp):
        """Calculate level from XP (same formula as ProBot)"""
        return int((xp / 100) ** 0.55)
    
    def calculate_xp_for_level(self, level):
        """Calculate XP required for a specific level"""
        return int((level / 0.55) ** (1 / 0.55) * 100)
    
    def get_xp_in_level(self, xp, level):
        """Get XP progress within current level"""
        xp_for_current = self.calculate_xp_for_level(level)
        xp_for_next = self.calculate_xp_for_level(level + 1)
        return xp - xp_for_current, xp_for_next - xp_for_current
    
    def get_user_xp(self, user_id, guild_id):
        """Get user's XP data"""
        conn = self.get_db_connection()
        c = conn.cursor()
        c.execute('SELECT text_xp, voice_xp FROM user_xp WHERE user_id = ? AND guild_id = ?',
                  (user_id, guild_id))
        result = c.fetchone()
        conn.close()
        
        if result:
            return {'text_xp': result[0], 'voice_xp': result[1]}
        return {'text_xp': 0, 'voice_xp': 0}
    
    def add_xp(self, user_id, guild_id, text_xp=0, voice_xp=0):
        """Add XP to a user"""
        conn = self.get_db_connection()
        c = conn.cursor()
        
        # Get current XP
        c.execute('SELECT text_xp, voice_xp FROM user_xp WHERE user_id = ? AND guild_id = ?',
                  (user_id, guild_id))
        result = c.fetchone()
        
        if result:
            new_text_xp = result[0] + text_xp
            new_voice_xp = result[1] + voice_xp
            c.execute('UPDATE user_xp SET text_xp = ?, voice_xp = ? WHERE user_id = ? AND guild_id = ?',
                      (new_text_xp, new_voice_xp, user_id, guild_id))
        else:
            c.execute('INSERT INTO user_xp (user_id, guild_id, text_xp, voice_xp) VALUES (?, ?, ?, ?)',
                      (user_id, guild_id, text_xp, voice_xp))
            new_text_xp = text_xp
            new_voice_xp = voice_xp
        
        conn.commit()
        conn.close()
        
        return new_text_xp, new_voice_xp
    
    async def check_level_up(self, user, guild, old_text_xp, new_text_xp, old_voice_xp, new_voice_xp):
        """Check if user leveled up and handle role rewards"""
        old_text_level = self.calculate_level(old_text_xp)
        new_text_level = self.calculate_level(new_text_xp)
        old_voice_level = self.calculate_level(old_voice_xp)
        new_voice_level = self.calculate_level(new_voice_xp)
        
        text_leveled_up = new_text_level > old_text_level
        voice_leveled_up = new_voice_level > old_voice_level
        
        if text_leveled_up or voice_leveled_up:
            # Check for role rewards
            await self.apply_role_rewards(user, guild, new_text_level, new_voice_level)
            
            # Send level up message
            settings = self.get_guild_settings(guild.id)
            if settings['level_channel_id']:
                channel = guild.get_channel(settings['level_channel_id'])
                if channel:
                    level_msg = f"**{user.mention}** leveled up!"
                    if text_leveled_up:
                        level_msg += f"\n`Text Level: {old_text_level} → {new_text_level}`"
                    if voice_leveled_up:
                        level_msg += f"\n`Voice Level: {old_voice_level} → {new_voice_level}`"
                    await channel.send(level_msg)
    
    async def apply_role_rewards(self, user, guild, text_level, voice_level):
        """Apply role rewards based on levels"""
        conn = self.get_db_connection()
        c = conn.cursor()
        c.execute('''SELECT role_id, text_level, voice_level FROM role_rewards
                     WHERE guild_id = ? AND text_level <= ? AND voice_level <= ?''',
                  (guild.id, text_level, voice_level))
        roles_to_add = c.fetchall()
        conn.close()
        
        for role_id, req_text, req_voice in roles_to_add:
            if text_level >= req_text and voice_level >= req_voice:
                role = guild.get_role(role_id)
                if role and role not in user.roles:
                    try:
                        await user.add_roles(role, reason="Level reward")
                    except:
                        pass
    
    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot:
            return
        if not message.guild:
            return
        
        settings = self.get_guild_settings(message.guild.id)
        
        # Check cooldown
        cooldown_key = f"{message.author.id}_{message.guild.id}"
        current_time = time.time()
        
        if cooldown_key in self.message_cooldowns:
            time_since_last = current_time - self.message_cooldowns[cooldown_key]
            if time_since_last < settings['xp_cooldown']:
                return
        
        self.message_cooldowns[cooldown_key] = current_time
        
        # Award XP
        import random
        xp_gain = random.randint(settings['xp_per_message_min'], settings['xp_per_message_max'])
        
        old_data = self.get_user_xp(message.author.id, message.guild.id)
        new_text_xp, new_voice_xp = self.add_xp(message.author.id, message.guild.id, text_xp=xp_gain)
        
        await self.check_level_up(message.author, message.guild,
                                  old_data['text_xp'], new_text_xp,
                                  old_data['voice_xp'], new_voice_xp)
    
    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if member.bot:
            return
        
        guild_id = member.guild.id
        user_id = member.id
        
        # User joined a voice channel
        if after.channel and not before.channel:
            self.voice_tracking[f"{user_id}_{guild_id}"] = time.time()
        
        # User left a voice channel
        elif before.channel and not after.channel:
            key = f"{user_id}_{guild_id}"
            if key in self.voice_tracking:
                del self.voice_tracking[key]
        
        # User moved between channels
        elif before.channel and after.channel and before.channel != after.channel:
            # Continue tracking, just update the channel
            pass
    
    async def voice_xp_loop(self):
        """Background task to award voice XP every minute"""
        await self.bot.wait_until_ready()
        
        while not self.bot.is_closed():
            await asyncio.sleep(60)  # Check every minute
            
            current_time = time.time()
            to_remove = []
            
            for key, start_time in self.voice_tracking.items():
                user_id, guild_id = map(int, key.split('_'))
                
                # Check if user is still in VC
                guild = self.bot.get_guild(guild_id)
                if not guild:
                    to_remove.append(key)
                    continue
                
                member = guild.get_member(user_id)
                if not member or not member.voice or not member.voice.channel:
                    to_remove.append(key)
                    continue
                
                # Award XP
                settings = self.get_guild_settings(guild_id)
                old_data = self.get_user_xp(user_id, guild_id)
                new_text_xp, new_voice_xp = self.add_xp(user_id, guild_id,
                                                        voice_xp=settings['vc_xp_per_minute'])
                
                await self.check_level_up(member, guild,
                                         old_data['text_xp'], new_text_xp,
                                         old_data['voice_xp'], new_voice_xp)
            
            for key in to_remove:
                if key in self.voice_tracking:
                    del self.voice_tracking[key]
    
    async def generate_rank_card(self, user, guild, text_xp, voice_xp):
        """Generate a rank card image"""
        text_level = self.calculate_level(text_xp)
        voice_level = self.calculate_level(voice_xp)
        
        text_xp_in_level, text_xp_needed = self.get_xp_in_level(text_xp, text_level)
        voice_xp_in_level, voice_xp_needed = self.get_xp_in_level(voice_xp, voice_level)
        
        text_progress = text_xp_in_level / text_xp_needed if text_xp_needed > 0 else 1.0
        voice_progress = voice_xp_in_level / voice_xp_needed if voice_xp_needed > 0 else 1.0
        
        # Create image
        width, height = 600, 200
        img = Image.new('RGB', (width, height), color=(44, 47, 51))
        draw = ImageDraw.Draw(img)
        
        # Try to load fonts, fallback to default if not available
        try:
            title_font = ImageFont.truetype("arial.ttf", 24)
            normal_font = ImageFont.truetype("arial.ttf", 18)
            small_font = ImageFont.truetype("arial.ttf", 14)
        except:
            try:
                title_font = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 24)
                normal_font = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 18)
                small_font = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 14)
            except:
                title_font = ImageFont.load_default()
                normal_font = ImageFont.load_default()
                small_font = ImageFont.load_default()
        
        # Download and draw avatar
        avatar_size = 120
        avatar_x, avatar_y = 20, 40
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(str(user.display_avatar.url)) as resp:
                    if resp.status == 200:
                        avatar_data = await resp.read()
                        avatar_img = Image.open(io.BytesIO(avatar_data))
                        avatar_img = avatar_img.resize((avatar_size, avatar_size), Image.Resampling.LANCZOS)
                        
                        # Create circular mask
                        mask = Image.new('L', (avatar_size, avatar_size), 0)
                        mask_draw = ImageDraw.Draw(mask)
                        mask_draw.ellipse([0, 0, avatar_size, avatar_size], fill=255)
                        
                        # Apply mask and paste
                        output = Image.new('RGB', (avatar_size, avatar_size), (44, 47, 51))
                        output.paste(avatar_img, (0, 0), mask)
                        img.paste(output, (avatar_x, avatar_y))
                        
                        # Draw border
                        draw.ellipse([avatar_x, avatar_y, avatar_x + avatar_size, avatar_y + avatar_size],
                                   outline=(255, 255, 255), width=3)
        except:
            # Fallback to colored circle if avatar download fails
            draw.ellipse([avatar_x, avatar_y, avatar_x + avatar_size, avatar_y + avatar_size],
                        fill=(114, 137, 218), outline=(255, 255, 255), width=3)
        
        # Draw username
        username = user.display_name[:20]
        draw.text((160, 30), username, fill=(255, 255, 255), font=title_font)
        
        # Draw text level info
        y_offset = 70
        draw.text((160, y_offset), f"Text Level: {text_level}", fill=(255, 255, 255), font=normal_font)
        
        # XP bar for text
        bar_x, bar_y = 160, y_offset + 30
        bar_width, bar_height = 400, 20
        draw.rectangle([bar_x, bar_y, bar_x + bar_width, bar_y + bar_height],
                      fill=(35, 39, 42), outline=(255, 255, 255), width=2)
        
        fill_width = int(bar_width * text_progress)
        draw.rectangle([bar_x, bar_y, bar_x + fill_width, bar_y + bar_height],
                      fill=(114, 137, 218))
        
        xp_text = f"{text_xp_in_level}/{text_xp_needed} XP"
        text_bbox = draw.textbbox((0, 0), xp_text, font=small_font)
        text_width = text_bbox[2] - text_bbox[0]
        draw.text((bar_x + bar_width // 2 - text_width // 2, bar_y + 2), xp_text,
                 fill=(255, 255, 255), font=small_font)
        
        # Draw voice level info
        y_offset = 130
        draw.text((160, y_offset), f"Voice Level: {voice_level}", fill=(255, 255, 255), font=normal_font)
        
        # XP bar for voice
        bar_x, bar_y = 160, y_offset + 30
        draw.rectangle([bar_x, bar_y, bar_x + bar_width, bar_y + bar_height],
                      fill=(35, 39, 42), outline=(255, 255, 255), width=2)
        
        fill_width = int(bar_width * voice_progress)
        draw.rectangle([bar_x, bar_y, bar_x + fill_width, bar_y + bar_height],
                      fill=(46, 204, 113))
        
        xp_text = f"{voice_xp_in_level}/{voice_xp_needed} XP"
        text_bbox = draw.textbbox((0, 0), xp_text, font=small_font)
        text_width = text_bbox[2] - text_bbox[0]
        draw.text((bar_x + bar_width // 2 - text_width // 2, bar_y + 2), xp_text,
                 fill=(255, 255, 255), font=small_font)
        
        # Convert to bytes
        img_bytes = io.BytesIO()
        img.save(img_bytes, format='PNG')
        img_bytes.seek(0)
        
        return img_bytes
    
    @app_commands.command(name="rank", description="View your level and XP")
    async def rank(self, interaction: discord.Interaction, member: discord.Member = None):
        if member is None:
            member = interaction.user
        
        xp_data = self.get_user_xp(member.id, interaction.guild.id)
        text_level = self.calculate_level(xp_data['text_xp'])
        voice_level = self.calculate_level(xp_data['voice_xp'])
        
        # Generate rank card
        try:
            card = await self.generate_rank_card(member, interaction.guild,
                                                 xp_data['text_xp'], xp_data['voice_xp'])
            file = discord.File(card, filename="rank.png")
            await interaction.response.send_message(file=file)
        except Exception as e:
            # Fallback to text if image generation fails
            text_xp_in_level, text_xp_needed = self.get_xp_in_level(xp_data['text_xp'], text_level)
            voice_xp_in_level, voice_xp_needed = self.get_xp_in_level(xp_data['voice_xp'], voice_level)
            
            embed = discord.Embed(title=f"{member.display_name}'s Rank", color=discord.Color.blue())
            embed.add_field(name="Text Level", value=f"`Level {text_level}`\n`{text_xp_in_level}/{text_xp_needed} XP`", inline=True)
            embed.add_field(name="Voice Level", value=f"`Level {voice_level}`\n`{voice_xp_in_level}/{voice_xp_needed} XP`", inline=True)
            embed.add_field(name="Total Text XP", value=f"`{xp_data['text_xp']}`", inline=False)
            embed.add_field(name="Total Voice XP", value=f"`{xp_data['voice_xp']}`", inline=False)
            await interaction.response.send_message(embed=embed)
    
    @app_commands.command(name="top", description="View the leaderboard")
    @app_commands.describe(type="Choose text or voice leaderboard", page="Page number (default: 1)")
    @app_commands.choices(type=[
        app_commands.Choice(name="text", value="text"),
        app_commands.Choice(name="voice", value="voice")
    ])
    async def top(self, interaction: discord.Interaction, type: str, page: int = 1):
        if type not in ['text', 'voice']:
            await interaction.response.send_message("Invalid type. Use `text` or `voice`.", ephemeral=True)
            return
        
        if page < 1:
            page = 1
        
        conn = self.get_db_connection()
        c = conn.cursor()
        
        xp_column = 'text_xp' if type == 'text' else 'voice_xp'
        c.execute(f'''SELECT user_id, {xp_column} FROM user_xp
                     WHERE guild_id = ? AND {xp_column} > 0
                     ORDER BY {xp_column} DESC LIMIT 10 OFFSET ?''',
                  (interaction.guild.id, (page - 1) * 10))
        
        results = c.fetchall()
        conn.close()
        
        if not results:
            await interaction.response.send_message(f"No users found for page {page}.", ephemeral=True)
            return
        
        embed = discord.Embed(
            title=f"{type.capitalize()} Leaderboard - Page {page}",
            color=discord.Color.gold()
        )
        
        description = ""
        for idx, (user_id, xp) in enumerate(results, start=(page - 1) * 10 + 1):
            user = interaction.guild.get_member(user_id)
            username = user.display_name if user else f"Unknown User ({user_id})"
            level = self.calculate_level(xp)
            description += f"`{idx}.` **{username}** - `Level {level}` - `{xp} XP`\n"
        
        embed.description = description
        await interaction.response.send_message(embed=embed)
    
    @app_commands.command(name="add-xp", description="Add XP to a user")
    @app_commands.describe(member="The user to add XP to", text_xp="Text XP to add", voice_xp="Voice XP to add")
    @app_commands.default_permissions(administrator=True)
    async def add_xp_cmd(self, interaction: discord.Interaction, member: discord.Member,
                        text_xp: int = 0, voice_xp: int = 0):
        if text_xp == 0 and voice_xp == 0:
            await interaction.response.send_message("Please specify at least one XP value to add.", ephemeral=True)
            return
        
        old_data = self.get_user_xp(member.id, interaction.guild.id)
        new_text_xp, new_voice_xp = self.add_xp(member.id, interaction.guild.id, text_xp, voice_xp)
        
        await self.check_level_up(member, interaction.guild,
                                 old_data['text_xp'], new_text_xp,
                                 old_data['voice_xp'], new_voice_xp)
        
        await interaction.response.send_message(
            f"Added `{text_xp}` text XP and `{voice_xp}` voice XP to {member.mention}.\n"
            f"New totals: `{new_text_xp}` text XP, `{new_voice_xp}` voice XP"
        )
    
    @app_commands.command(name="del-xp", description="Remove XP from a user")
    @app_commands.describe(member="The user to remove XP from", text_xp="Text XP to remove", voice_xp="Voice XP to remove")
    @app_commands.default_permissions(administrator=True)
    async def del_xp_cmd(self, interaction: discord.Interaction, member: discord.Member,
                        text_xp: int = 0, voice_xp: int = 0):
        if text_xp == 0 and voice_xp == 0:
            await interaction.response.send_message("Please specify at least one XP value to remove.", ephemeral=True)
            return
        
        old_data = self.get_user_xp(member.id, interaction.guild.id)
        new_text_xp = max(0, old_data['text_xp'] - text_xp)
        new_voice_xp = max(0, old_data['voice_xp'] - voice_xp)
        
        conn = self.get_db_connection()
        c = conn.cursor()
        c.execute('UPDATE user_xp SET text_xp = ?, voice_xp = ? WHERE user_id = ? AND guild_id = ?',
                  (new_text_xp, new_voice_xp, member.id, interaction.guild.id))
        conn.commit()
        conn.close()
        
        await interaction.response.send_message(
            f"Removed `{text_xp}` text XP and `{voice_xp}` voice XP from {member.mention}.\n"
            f"New totals: `{new_text_xp}` text XP, `{new_voice_xp}` voice XP"
        )
    
    @app_commands.command(name="set-level-channel", description="Set the channel for level-up messages")
    @app_commands.describe(channel="The channel to send level-up messages to")
    @app_commands.default_permissions(administrator=True)
    async def set_level_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        conn = self.get_db_connection()
        c = conn.cursor()
        
        # Get existing settings to preserve them
        c.execute('SELECT * FROM guild_settings WHERE guild_id = ?', (interaction.guild.id,))
        existing = c.fetchone()
        
        if existing:
            # Update only the level_channel_id
            c.execute('''UPDATE guild_settings SET level_channel_id = ? WHERE guild_id = ?''',
                      (channel.id, interaction.guild.id))
        else:
            # Insert new row with defaults
            c.execute('''INSERT INTO guild_settings
                         (guild_id, level_channel_id, xp_per_message_min, xp_per_message_max,
                          xp_cooldown, vc_xp_per_minute)
                         VALUES (?, ?, ?, ?, ?, ?)''',
                      (interaction.guild.id, channel.id,
                       self.default_xp_per_message[0], self.default_xp_per_message[1],
                       self.default_xp_cooldown, self.default_vc_xp_per_minute))
        
        conn.commit()
        conn.close()
        
        await interaction.response.send_message(f"Level-up messages will now be sent to {channel.mention}")
    
    @app_commands.command(name="add-role-reward", description="Add a role reward for reaching certain levels")
    @app_commands.describe(role="The role to give", text_level="Required text level", voice_level="Required voice level")
    @app_commands.default_permissions(administrator=True)
    async def add_role_reward(self, interaction: discord.Interaction, role: discord.Role,
                             text_level: int, voice_level: int):
        if text_level < 0 or voice_level < 0:
            await interaction.response.send_message("Levels must be 0 or greater.", ephemeral=True)
            return
        
        conn = self.get_db_connection()
        c = conn.cursor()
        c.execute('''INSERT OR REPLACE INTO role_rewards
                     (guild_id, role_id, text_level, voice_level) VALUES (?, ?, ?, ?)''',
                  (interaction.guild.id, role.id, text_level, voice_level))
        conn.commit()
        conn.close()
        
        await interaction.response.send_message(
            f"Added role reward: {role.mention} will be given at `Text Level {text_level}` and `Voice Level {voice_level}`"
        )
    
    @app_commands.command(name="remove-role-reward", description="Remove a role reward")
    @app_commands.describe(role="The role to remove from rewards")
    @app_commands.default_permissions(administrator=True)
    async def remove_role_reward(self, interaction: discord.Interaction, role: discord.Role):
        conn = self.get_db_connection()
        c = conn.cursor()
        c.execute('DELETE FROM role_rewards WHERE guild_id = ? AND role_id = ?',
                  (interaction.guild.id, role.id))
        conn.commit()
        conn.close()
        
        await interaction.response.send_message(f"Removed role reward for {role.mention}")
    
    @app_commands.command(name="list-role-rewards", description="List all role rewards")
    async def list_role_rewards(self, interaction: discord.Interaction):
        conn = self.get_db_connection()
        c = conn.cursor()
        c.execute('SELECT role_id, text_level, voice_level FROM role_rewards WHERE guild_id = ?',
                  (interaction.guild.id,))
        results = c.fetchall()
        conn.close()
        
        if not results:
            await interaction.response.send_message("No role rewards configured.", ephemeral=True)
            return
        
        embed = discord.Embed(title="Role Rewards", color=discord.Color.blue())
        description = ""
        for role_id, text_level, voice_level in results:
            role = interaction.guild.get_role(role_id)
            role_name = role.mention if role else f"Unknown Role ({role_id})"
            description += f"{role_name} - `Text Level {text_level}`, `Voice Level {voice_level}`\n"
        
        embed.description = description
        await interaction.response.send_message(embed=embed)
    
    @app_commands.command(name="help", description="View all available commands and their usage")
    async def help_command(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="BuzzBot Commands",
            description="A comprehensive levelling system for Discord servers",
            color=discord.Color.blue()
        )
        
        # User Commands Section
        user_commands = """`/rank [member]`
View your or another member's level and XP with a visual rank card showing text and voice progress.

`/top text [page]`
View the text XP leaderboard. Shows 10 users per page. Default page is 1.

`/top voice [page]`
View the voice XP leaderboard. Shows 10 users per page. Default page is 1."""
        
        embed.add_field(name="User Commands", value=user_commands, inline=False)
        
        # Administrator Commands Section
        admin_commands = """`/add-xp <member> [text_xp] [voice_xp]`
Manually add XP to a user. Specify at least one XP value.

`/del-xp <member> [text_xp] [voice_xp]`
Manually remove XP from a user. Specify at least one XP value.

`/set-level-channel <channel>`
Set the channel where level-up messages will be sent.

`/add-role-reward <role> <text_level> <voice_level>`
Add a role that will be automatically given when users reach the specified text and voice levels.

`/remove-role-reward <role>`
Remove a role from the reward system.

`/list-role-rewards`
View all configured role rewards for this server."""
        
        embed.add_field(name="Administrator Commands", value=admin_commands, inline=False)
        
        # How It Works Section
        how_it_works = """**XP from Messages**
Users gain `15-25 XP` (randomized) per message with a `60 second` cooldown to prevent spam.

**XP from Voice Chat**
Users gain `1 XP per minute` while connected to a voice channel.

**Leveling System**
Text XP and Voice XP are tracked separately, each with their own levels. The level formula matches ProBot: `Level = (XP / 100) ^ 0.55`

**Role Rewards**
Roles stack automatically. When a user reaches both the required text level AND voice level, they receive the role."""
        
        embed.add_field(name="How It Works", value=how_it_works, inline=False)
        
        embed.set_footer(text="BuzzBot - ProBot-style Levelling System")
        
        await interaction.response.send_message(embed=embed)

async def setup(bot):
    await bot.add_cog(Levelling(bot))

