import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import time
import json
import os
from PIL import Image, ImageDraw, ImageFont
import io
import aiohttp
import random

from config import DEFAULT_XP_PER_MESSAGE, DEFAULT_VC_XP_PER_MINUTE, MIN_MESSAGE_LENGTH, MAX_MESSAGES_PER_WINDOW, TIME_WINDOW

class Levelling(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.data_dir = 'data'
        self.xp_file = os.path.join(self.data_dir, 'xp_data.json')
        self.settings_file = os.path.join(self.data_dir, 'guild_settings.json')
        self.rewards_file = os.path.join(self.data_dir, 'role_rewards.json')
        
        # Initialize data directory and files
        self.init_data_files()
        
        # Spam protection tracking
        self.message_history = {}  # {user_id_guild_id: [list of message timestamps]}
        self.voice_tracking = {}  # {user_id_guild_id: start_time}
        
        # Default settings
        self.default_xp_per_message = DEFAULT_XP_PER_MESSAGE
        self.default_vc_xp_per_minute = DEFAULT_VC_XP_PER_MINUTE
        self.min_message_length = MIN_MESSAGE_LENGTH
        self.max_messages_per_window = MAX_MESSAGES_PER_WINDOW
        self.time_window = TIME_WINDOW  # seconds
        
        # Start background tasks
        self.bot.loop.create_task(self.voice_xp_loop())
        self.bot.loop.create_task(self.cleanup_message_history())
    
    def init_data_files(self):
        """Initialize JSON data files"""
        os.makedirs(self.data_dir, exist_ok=True)
        
        # Initialize XP data file
        if not os.path.exists(self.xp_file):
            self.save_json(self.xp_file, {})
        
        # Initialize guild settings file
        if not os.path.exists(self.settings_file):
            self.save_json(self.settings_file, {})
        
        # Initialize role rewards file
        if not os.path.exists(self.rewards_file):
            self.save_json(self.rewards_file, {})
        
        # Fix any negative XP values
        self.fix_all_negative_xp()
    
    def load_json(self, filepath):
        """Load JSON data from file"""
        try:
            if os.path.exists(filepath):
                with open(filepath, 'r', encoding='utf-8') as f:
                    return json.load(f)
            return {}
        except (json.JSONDecodeError, IOError):
            return {}
    
    def save_json(self, filepath, data):
        """Save JSON data to file"""
        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            return True
        except IOError:
            return False
    
    def fix_all_negative_xp(self):
        """Fix all negative XP values in the data"""
        data = self.load_json(self.xp_file)
        fixed = False
        
        for guild_id, users in data.items():
            for user_id, xp_data in users.items():
                if isinstance(xp_data, dict):
                    if xp_data.get('text_xp', 0) < 0:
                        xp_data['text_xp'] = 0
                        fixed = True
                    if xp_data.get('voice_xp', 0) < 0:
                        xp_data['voice_xp'] = 0
                        fixed = True
        
        if fixed:
            self.save_json(self.xp_file, data)
    
    def get_user_xp(self, user_id, guild_id):
        """Get user's XP data"""
        data = self.load_json(self.xp_file)
        guild_id_str = str(guild_id)
        user_id_str = str(user_id)
        
        if guild_id_str in data and user_id_str in data[guild_id_str]:
            xp_data = data[guild_id_str][user_id_str]
            return {
                'text_xp': max(0, int(xp_data.get('text_xp', 0))),
                'voice_xp': max(0, int(xp_data.get('voice_xp', 0)))
            }
        return {'text_xp': 0, 'voice_xp': 0}
    
    def add_xp(self, user_id, guild_id, text_xp=0, voice_xp=0):
        """Add XP to a user - ensures values are never negative"""
        # Validate input - only allow positive values
        text_xp_to_add = max(0, abs(int(text_xp))) if text_xp else 0
        voice_xp_to_add = max(0, abs(int(voice_xp))) if voice_xp else 0
        
        if text_xp_to_add == 0 and voice_xp_to_add == 0:
            current = self.get_user_xp(user_id, guild_id)
            return current['text_xp'], current['voice_xp']
        
        # Load current data
        data = self.load_json(self.xp_file)
        guild_id_str = str(guild_id)
        user_id_str = str(user_id)
        
        # Initialize guild if needed
        if guild_id_str not in data:
            data[guild_id_str] = {}
        
        # Get current XP
        if user_id_str in data[guild_id_str]:
            current_text = max(0, int(data[guild_id_str][user_id_str].get('text_xp', 0)))
            current_voice = max(0, int(data[guild_id_str][user_id_str].get('voice_xp', 0)))
        else:
            current_text = 0
            current_voice = 0
        
        # Calculate new XP (only addition, never subtraction)
        new_text_xp = max(0, current_text + text_xp_to_add)
        new_voice_xp = max(0, current_voice + voice_xp_to_add)
        
        # Update data
        if user_id_str not in data[guild_id_str]:
            data[guild_id_str][user_id_str] = {}
        
        data[guild_id_str][user_id_str]['text_xp'] = new_text_xp
        data[guild_id_str][user_id_str]['voice_xp'] = new_voice_xp
        
        # Save to file
        self.save_json(self.xp_file, data)
        
        return new_text_xp, new_voice_xp
    
    def remove_xp(self, user_id, guild_id, text_xp=0, voice_xp=0):
        """Remove XP from a user - ensures values never go negative"""
        text_xp_to_remove = max(0, abs(int(text_xp))) if text_xp else 0
        voice_xp_to_remove = max(0, abs(int(voice_xp))) if voice_xp else 0
        
        if text_xp_to_remove == 0 and voice_xp_to_remove == 0:
            current = self.get_user_xp(user_id, guild_id)
            return current['text_xp'], current['voice_xp']
        
        # Load current data
        data = self.load_json(self.xp_file)
        guild_id_str = str(guild_id)
        user_id_str = str(user_id)
        
        # Get current XP
        current = self.get_user_xp(user_id, guild_id)
        current_text = current['text_xp']
        current_voice = current['voice_xp']
        
        # Calculate new XP (subtract but never go below 0)
        new_text_xp = max(0, current_text - text_xp_to_remove)
        new_voice_xp = max(0, current_voice - voice_xp_to_remove)
        
        # Initialize if needed
        if guild_id_str not in data:
            data[guild_id_str] = {}
        if user_id_str not in data[guild_id_str]:
            data[guild_id_str][user_id_str] = {}
        
        # Update data
        data[guild_id_str][user_id_str]['text_xp'] = new_text_xp
        data[guild_id_str][user_id_str]['voice_xp'] = new_voice_xp
        
        # Save to file
        self.save_json(self.xp_file, data)
        
        return new_text_xp, new_voice_xp
    
    def get_guild_settings(self, guild_id):
        """Get guild settings"""
        data = self.load_json(self.settings_file)
        guild_id_str = str(guild_id)
        
        if guild_id_str in data:
            settings = data[guild_id_str]
            return {
                'level_channel_id': settings.get('level_channel_id'),
                'xp_per_message_min': settings.get('xp_per_message_min', self.default_xp_per_message[0]),
                'xp_per_message_max': settings.get('xp_per_message_max', self.default_xp_per_message[1]),
                'vc_xp_per_minute': settings.get('vc_xp_per_minute', self.default_vc_xp_per_minute)
            }
        
        return {
            'level_channel_id': None,
            'xp_per_message_min': self.default_xp_per_message[0],
            'xp_per_message_max': self.default_xp_per_message[1],
            'vc_xp_per_minute': self.default_vc_xp_per_minute
        }
    
    def set_guild_setting(self, guild_id, key, value):
        """Set a guild setting"""
        data = self.load_json(self.settings_file)
        guild_id_str = str(guild_id)
        
        if guild_id_str not in data:
            data[guild_id_str] = {}
        
        data[guild_id_str][key] = value
        self.save_json(self.settings_file, data)
    
    def calculate_xp_for_level(self, level):
        """Calculate XP required to reach a specific level"""
        if level <= 0:
            return 0
        # ProBot formula: XP = (level / 0.55) ^ (1 / 0.55) * 100
        return int((level / 0.55) ** (1 / 0.55) * 100)
    
    def calculate_level(self, xp):
        """Calculate level from XP (ProBot formula) - finds highest level where required XP <= user XP"""
        if xp <= 0:
            return 0
        
        # Find the highest level where the required XP is <= user's XP
        level = 0
        while True:
            xp_needed = self.calculate_xp_for_level(level + 1)
            if xp_needed > xp:
                return level
            level += 1
            # Safety check to prevent infinite loops
            if level > 10000:
                return level
    
    def get_xp_in_level(self, xp, level):
        """Get XP progress within current level"""
        # XP required to reach current level
        xp_for_current_level = self.calculate_xp_for_level(level)
        # XP required to reach next level
        xp_for_next_level = self.calculate_xp_for_level(level + 1)
        
        # XP progress in current level (how much XP they have beyond the level requirement)
        xp_in_current = max(0, xp - xp_for_current_level)
        # XP needed to reach next level
        xp_needed_for_next = max(1, xp_for_next_level - xp_for_current_level)
        
        return xp_in_current, xp_needed_for_next
    
    async def check_level_up(self, user, guild, old_text_xp, new_text_xp, old_voice_xp, new_voice_xp):
        """Check if user leveled up and handle role rewards"""
        old_text_level = self.calculate_level(old_text_xp)
        new_text_level = self.calculate_level(new_text_xp)
        old_voice_level = self.calculate_level(old_voice_xp)
        new_voice_level = self.calculate_level(new_voice_xp)
        
        text_leveled_up = new_text_level > old_text_level
        voice_leveled_up = new_voice_level > old_voice_level
        
        if text_leveled_up or voice_leveled_up:
            # Apply role rewards
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
        data = self.load_json(self.rewards_file)
        guild_id_str = str(guild.id)
        
        if guild_id_str not in data:
            return
        
        rewards = data[guild_id_str]
        for role_id_str, reward_data in rewards.items():
            req_text = reward_data.get('text_level', 0)
            req_voice = reward_data.get('voice_level', 0)
            
            if text_level >= req_text and voice_level >= req_voice:
                role_id = int(role_id_str)
                role = guild.get_role(role_id)
                if role and role not in user.roles:
                    try:
                        await user.add_roles(role, reason="Level reward")
                    except:
                        pass
    
    async def cleanup_message_history(self):
        """Periodically clean up old message history"""
        await self.bot.wait_until_ready()
        
        while not self.bot.is_closed():
            await asyncio.sleep(60)
            
            current_time = time.time()
            cutoff_time = current_time - (self.time_window * 2)
            
            keys_to_remove = []
            for key, message_times in self.message_history.items():
                self.message_history[key] = [t for t in message_times if t > cutoff_time]
                if not self.message_history[key]:
                    keys_to_remove.append(key)
            
            for key in keys_to_remove:
                del self.message_history[key]
    
    @commands.Cog.listener()
    async def on_message(self, message):
        """Handle messages and award XP"""
        if message.author.bot:
            return
        if not message.guild:
            return
        
        # Ignore short messages
        if len(message.content.strip()) < self.min_message_length:
            return
        
        settings = self.get_guild_settings(message.guild.id)
        
        # Spam protection
        cooldown_key = f"{message.author.id}_{message.guild.id}"
        current_time = time.time()
        
        if cooldown_key not in self.message_history:
            self.message_history[cooldown_key] = []
        
        # Remove old timestamps
        cutoff_time = current_time - self.time_window
        self.message_history[cooldown_key] = [
            t for t in self.message_history[cooldown_key] if t > cutoff_time
        ]
        
        # Check spam limit
        recent_messages = len(self.message_history[cooldown_key])
        if recent_messages >= self.max_messages_per_window:
            # Spam detected - don't award XP, just track message
            self.message_history[cooldown_key].append(current_time)
            return
        
        # User is eligible - add message to history
        self.message_history[cooldown_key].append(current_time)
        
        # Get current XP before adding
        old_data = self.get_user_xp(message.author.id, message.guild.id)
        
        # Calculate XP to award (always positive)
        xp_gain = random.randint(settings['xp_per_message_min'], settings['xp_per_message_max'])
        xp_gain = max(1, int(xp_gain))
        
        # Add XP
        new_text_xp, new_voice_xp = self.add_xp(message.author.id, message.guild.id, text_xp=xp_gain)
        
        # Verify result is non-negative
        if new_text_xp < 0 or new_voice_xp < 0:
            print(f"ERROR: Negative XP detected! Fixing...")
            new_text_xp = max(0, new_text_xp)
            new_voice_xp = max(0, new_voice_xp)
            # Force update
            data = self.load_json(self.xp_file)
            guild_id_str = str(message.guild.id)
            user_id_str = str(message.author.id)
            if guild_id_str not in data:
                data[guild_id_str] = {}
            if user_id_str not in data[guild_id_str]:
                data[guild_id_str][user_id_str] = {}
            data[guild_id_str][user_id_str]['text_xp'] = new_text_xp
            data[guild_id_str][user_id_str]['voice_xp'] = new_voice_xp
            self.save_json(self.xp_file, data)
        
        # Check for level up
        await self.check_level_up(message.author, message.guild,
                                  old_data['text_xp'], new_text_xp,
                                  old_data['voice_xp'], new_voice_xp)
    
    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        """Track voice channel activity"""
        if member.bot:
            return
        
        guild_id = member.guild.id
        user_id = member.id
        key = f"{user_id}_{guild_id}"
        
        # User joined voice channel
        if after.channel and not before.channel:
            self.voice_tracking[key] = time.time()
        
        # User left voice channel
        elif before.channel and not after.channel:
            if key in self.voice_tracking:
                del self.voice_tracking[key]
    
    async def voice_xp_loop(self):
        """Award voice XP every minute"""
        await self.bot.wait_until_ready()
        
        while not self.bot.is_closed():
            await asyncio.sleep(60)
            
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
        """Generate rank card image"""
        # Ensure XP values are integers and non-negative
        text_xp = max(0, int(text_xp))
        voice_xp = max(0, int(voice_xp))
        
        text_level = self.calculate_level(text_xp)
        voice_level = self.calculate_level(voice_xp)
        
        text_xp_in_level, text_xp_needed = self.get_xp_in_level(text_xp, text_level)
        voice_xp_in_level, voice_xp_needed = self.get_xp_in_level(voice_xp, voice_level)
        
        # Ensure progress values are valid (0.0 to 1.0)
        text_progress = max(0.0, min(1.0, text_xp_in_level / text_xp_needed if text_xp_needed > 0 else 0.0))
        voice_progress = max(0.0, min(1.0, voice_xp_in_level / voice_xp_needed if voice_xp_needed > 0 else 0.0))
        
        # Create image
        width, height = 600, 200
        img = Image.new('RGB', (width, height), color=(44, 47, 51))
        draw = ImageDraw.Draw(img)
        
        # Load fonts
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
                        
                        # Apply mask
                        output = Image.new('RGB', (avatar_size, avatar_size), (44, 47, 51))
                        output.paste(avatar_img, (0, 0), mask)
                        img.paste(output, (avatar_x, avatar_y))
                        
                        # Draw border
                        draw.ellipse([avatar_x, avatar_y, avatar_x + avatar_size, avatar_y + avatar_size],
                                   outline=(255, 255, 255), width=3)
        except:
            # Fallback circle
            draw.ellipse([avatar_x, avatar_y, avatar_x + avatar_size, avatar_y + avatar_size],
                        fill=(114, 137, 218), outline=(255, 255, 255), width=3)
        
        # Draw username
        username = user.display_name[:20]
        draw.text((160, 30), username, fill=(255, 255, 255), font=title_font)
        
        # Draw text level
        y_offset = 70
        draw.text((160, y_offset), f"Text Level: {text_level}", fill=(255, 255, 255), font=normal_font)
        
        # Text XP bar
        bar_x, bar_y = 160, y_offset + 30
        bar_width, bar_height = 400, 20
        draw.rectangle([bar_x, bar_y, bar_x + bar_width, bar_y + bar_height],
                      fill=(35, 39, 42), outline=(255, 255, 255), width=2)
        
        fill_width = int(bar_width * text_progress)
        if fill_width > 0:
            fill_width = max(1, fill_width)
            draw.rectangle([bar_x, bar_y, bar_x + fill_width, bar_y + bar_height],
                          fill=(114, 137, 218))
        
        xp_text = f"{text_xp_in_level}/{text_xp_needed} XP"
        text_bbox = draw.textbbox((0, 0), xp_text, font=small_font)
        text_width = text_bbox[2] - text_bbox[0]
        draw.text((bar_x + bar_width // 2 - text_width // 2, bar_y + 2), xp_text,
                 fill=(255, 255, 255), font=small_font)
        
        # Draw voice level
        y_offset = 130
        draw.text((160, y_offset), f"Voice Level: {voice_level}", fill=(255, 255, 255), font=normal_font)
        
        # Voice XP bar
        bar_x, bar_y = 160, y_offset + 30
        draw.rectangle([bar_x, bar_y, bar_x + bar_width, bar_y + bar_height],
                      fill=(35, 39, 42), outline=(255, 255, 255), width=2)
        
        fill_width = int(bar_width * voice_progress)
        if fill_width > 0:
            fill_width = max(1, fill_width)
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
        
        try:
            card = await self.generate_rank_card(member, interaction.guild,
                                                 xp_data['text_xp'], xp_data['voice_xp'])
            file = discord.File(card, filename="rank.png")
            await interaction.response.send_message(file=file)
        except Exception as e:
            print(f"Error generating rank card: {e}")
            import traceback
            traceback.print_exc()
            
            # Fallback embed
            text_xp_in_level, text_xp_needed = self.get_xp_in_level(text_xp, text_level)
            voice_xp_in_level, voice_xp_needed = self.get_xp_in_level(voice_xp, voice_level)
            
            embed = discord.Embed(title=f"{member.display_name}'s Rank", color=discord.Color.blue())
            embed.add_field(name="Text Level", value=f"`Level {text_level}`\n`{text_xp_in_level}/{text_xp_needed} XP`", inline=True)
            embed.add_field(name="Voice Level", value=f"`Level {voice_level}`\n`{voice_xp_in_level}/{voice_xp_needed} XP`", inline=True)
            embed.add_field(name="Total Text XP", value=f"`{text_xp}`", inline=False)
            embed.add_field(name="Total Voice XP", value=f"`{voice_xp}`", inline=False)
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
        
        # Get all users for this guild
        data = self.load_json(self.xp_file)
        guild_id_str = str(interaction.guild.id)
        
        if guild_id_str not in data:
            await interaction.response.send_message(f"No users found for page {page}.", ephemeral=True)
            return
        
        # Build leaderboard
        users = []
        xp_key = 'text_xp' if type == 'text' else 'voice_xp'
        
        for user_id_str, xp_data in data[guild_id_str].items():
            if isinstance(xp_data, dict):
                xp = max(0, int(xp_data.get(xp_key, 0)))
                if xp > 0:
                    users.append((int(user_id_str), xp))
        
        # Sort by XP descending
        users.sort(key=lambda x: x[1], reverse=True)
        
        # Paginate
        per_page = 10
        start_idx = (page - 1) * per_page
        end_idx = start_idx + per_page
        page_users = users[start_idx:end_idx]
        
        if not page_users:
            await interaction.response.send_message(f"No users found for page {page}.", ephemeral=True)
            return
        
        embed = discord.Embed(
            title=f"{type.capitalize()} Leaderboard - Page {page}",
            color=discord.Color.gold()
        )
        
        description = ""
        for idx, (user_id, xp) in enumerate(page_users, start=start_idx + 1):
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
        new_text_xp, new_voice_xp = self.remove_xp(member.id, interaction.guild.id, text_xp, voice_xp)
        
        await interaction.response.send_message(
            f"Removed `{text_xp}` text XP and `{voice_xp}` voice XP from {member.mention}.\n"
            f"New totals: `{new_text_xp}` text XP, `{new_voice_xp}` voice XP"
        )
    
    @app_commands.command(name="set-level-channel", description="Set the channel for level-up messages")
    @app_commands.describe(channel="The channel to send level-up messages to")
    @app_commands.default_permissions(administrator=True)
    async def set_level_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        self.set_guild_setting(interaction.guild.id, 'level_channel_id', channel.id)
        await interaction.response.send_message(f"Level-up messages will now be sent to {channel.mention}")
    
    @app_commands.command(name="add-role-reward", description="Add a role reward for reaching certain levels")
    @app_commands.describe(role="The role to give", text_level="Required text level", voice_level="Required voice level")
    @app_commands.default_permissions(administrator=True)
    async def add_role_reward(self, interaction: discord.Interaction, role: discord.Role,
                             text_level: int, voice_level: int):
        if text_level < 0 or voice_level < 0:
            await interaction.response.send_message("Levels must be 0 or greater.", ephemeral=True)
            return
        
        data = self.load_json(self.rewards_file)
        guild_id_str = str(interaction.guild.id)
        
        if guild_id_str not in data:
            data[guild_id_str] = {}
        
        data[guild_id_str][str(role.id)] = {
            'text_level': text_level,
            'voice_level': voice_level
        }
        
        self.save_json(self.rewards_file, data)
        
        await interaction.response.send_message(
            f"Added role reward: {role.mention} will be given at `Text Level {text_level}` and `Voice Level {voice_level}`"
        )
    
    @app_commands.command(name="remove-role-reward", description="Remove a role reward")
    @app_commands.describe(role="The role to remove from rewards")
    @app_commands.default_permissions(administrator=True)
    async def remove_role_reward(self, interaction: discord.Interaction, role: discord.Role):
        data = self.load_json(self.rewards_file)
        guild_id_str = str(interaction.guild.id)
        
        if guild_id_str in data and str(role.id) in data[guild_id_str]:
            del data[guild_id_str][str(role.id)]
            self.save_json(self.rewards_file, data)
            await interaction.response.send_message(f"Removed role reward for {role.mention}")
        else:
            await interaction.response.send_message(f"No role reward found for {role.mention}.", ephemeral=True)
    
    @app_commands.command(name="list-role-rewards", description="List all role rewards")
    async def list_role_rewards(self, interaction: discord.Interaction):
        data = self.load_json(self.rewards_file)
        guild_id_str = str(interaction.guild.id)
        
        if guild_id_str not in data or not data[guild_id_str]:
            await interaction.response.send_message("No role rewards configured.", ephemeral=True)
            return
        
        embed = discord.Embed(title="Role Rewards", color=discord.Color.blue())
        description = ""
        
        for role_id_str, reward_data in data[guild_id_str].items():
            role = interaction.guild.get_role(int(role_id_str))
            role_name = role.mention if role else f"Unknown Role ({role_id_str})"
            text_level = reward_data.get('text_level', 0)
            voice_level = reward_data.get('voice_level', 0)
            description += f"{role_name} - `Text Level {text_level}`, `Voice Level {voice_level}`\n"
        
        embed.description = description
        await interaction.response.send_message(embed=embed)
    
    @app_commands.command(name="fix-xp", description="Fix negative XP values for a user or all users")
    @app_commands.describe(member="The user to fix (leave empty to fix all users)", fix_all="Fix all users with negative XP")
    @app_commands.default_permissions(administrator=True)
    async def fix_xp(self, interaction: discord.Interaction, member: discord.Member = None, fix_all: bool = False):
        if fix_all or member is None:
            self.fix_all_negative_xp()
            await interaction.response.send_message("Fixed all negative XP values in the database.")
        else:
            old_data = self.get_user_xp(member.id, interaction.guild.id)
            new_text_xp = max(0, old_data['text_xp'])
            new_voice_xp = max(0, old_data['voice_xp'])
            
            if old_data['text_xp'] < 0 or old_data['voice_xp'] < 0:
                data = self.load_json(self.xp_file)
                guild_id_str = str(interaction.guild.id)
                user_id_str = str(member.id)
                
                if guild_id_str not in data:
                    data[guild_id_str] = {}
                if user_id_str not in data[guild_id_str]:
                    data[guild_id_str][user_id_str] = {}
                
                data[guild_id_str][user_id_str]['text_xp'] = new_text_xp
                data[guild_id_str][user_id_str]['voice_xp'] = new_voice_xp
                self.save_json(self.xp_file, data)
                
                await interaction.response.send_message(
                    f"Fixed {member.mention}'s XP:\n"
                    f"Text XP: `{old_data['text_xp']}` → `{new_text_xp}`\n"
                    f"Voice XP: `{old_data['voice_xp']}` → `{new_voice_xp}`"
                )
            else:
                await interaction.response.send_message(
                    f"{member.mention}'s XP is already valid: `{new_text_xp}` text XP, `{new_voice_xp}` voice XP",
                    ephemeral=True
                )
    
    @app_commands.command(name="help", description="View all available commands and their usage")
    async def help_command(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="BuzzBot Commands",
            description="A comprehensive levelling system for Discord servers",
            color=discord.Color.blue()
        )
        
        user_commands = """`/rank [member]`
View your or another member's level and XP with a visual rank card showing text and voice progress.

`/top text [page]`
View the text XP leaderboard. Shows 10 users per page. Default page is 1.

`/top voice [page]`
View the voice XP leaderboard. Shows 10 users per page. Default page is 1."""
        
        embed.add_field(name="User Commands", value=user_commands, inline=False)
        
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
View all configured role rewards for this server.

`/fix-xp [member] [fix_all]`
Fix negative XP values for a user or all users."""
        
        embed.add_field(name="Administrator Commands", value=admin_commands, inline=False)
        
        how_it_works = """**XP from Messages**
Gain `15-25 XP` per eligible message. Multiple messages can give XP, allowing natural conversations. Messages must be `3+ characters` long.

**Spam Protection**
Up to `5 messages per 10 seconds` can give XP. This prevents rapid-fire spam while allowing normal chat. Messages beyond this limit won't award XP.

**XP from Voice Chat**
Gain `1 XP per minute` while in voice channels. Automatically tracked and awarded.

**Leveling System**
Text and Voice XP are separate with independent levels. Formula: `Level = (XP / 100) ^ 0.55` (ProBot style).

**Role Rewards**
Roles stack automatically. Reach both required text AND voice levels to unlock. Multiple roles can be active simultaneously."""
        
        embed.add_field(name="How It Works", value=how_it_works, inline=False)
        
        embed.set_footer(text="BuzzBot - ProBot-style Levelling System")
        
        await interaction.response.send_message(embed=embed)

async def setup(bot):
    await bot.add_cog(Levelling(bot))
