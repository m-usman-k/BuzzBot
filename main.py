import discord
from discord.ext import commands
import asyncio
import os
from dotenv import load_dotenv

load_dotenv()

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True

bot = commands.Bot(command_prefix='!', intents=intents)

@bot.event
async def on_ready():
    print(f'{bot.user} has logged in!')
    try:
        await bot.load_extension('cogs.levelling')
        print('Levelling cog loaded successfully!')
        
        # Sync commands
        try:
            synced = await bot.tree.sync()
            print(f'Synced {len(synced)} command(s)')
        except Exception as e:
            print(f'Failed to sync commands: {e}')
    except Exception as e:
        print(f'Failed to load levelling cog: {e}')

if __name__ == '__main__':
    token = os.getenv('BOT_TOKEN')
    if not token:
        print("Error: BOT_TOKEN not found in environment variables!")
        print("Please create a .env file with BOT_TOKEN=your_token_here")
    else:
        bot.run(token)

