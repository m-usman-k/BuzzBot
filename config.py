DEFAULT_XP_PER_MESSAGE = (15, 25)
DEFAULT_VC_XP_PER_MINUTE = 10
MIN_MESSAGE_LENGTH = 3
MAX_MESSAGES_PER_WINDOW = 5
TIME_WINDOW = 10

# Welcome Image Settings
# WELCOME_BACKGROUND_PATH: path to a background image file (any aspect ratio works, it will be resized).
# Set to None or point to a non-existent file to use the built-in gradient fallback.
# Recommended image size: 900x320 pixels (banner aspect ratio)
WELCOME_IMAGE_SIZE = (900, 320)        # Width x Height
WELCOME_BACKGROUND_PATH = './data/welcome_bg.png'  # Customise this path
WELCOME_AVATAR_SIZE = 120              # Avatar circle diameter in pixels
# AutoMod Settings
# AUTOMOD_WORDLIST_PATH: banned words for the language filter (instructions are inside the file).
# AUTOMOD_LINK_WHITELIST_PATH: domains that are never sent to VirusTotal by the malicious link filter.
# Both files are reloaded automatically within a minute of being edited.
# The VirusTotal API key goes in your .env file as VIRUSTOTAL_API_KEY=your_key_here
AUTOMOD_WORDLIST_PATH = './data/banned_words.txt'
AUTOMOD_LINK_WHITELIST_PATH = './data/link_whitelist.txt'
VIRUSTOTAL_REQUESTS_PER_MINUTE = 4     # Free VirusTotal keys allow 4 requests a minute (and 500 a day)

# YouTube Notification Settings
YOUTUBE_POLL_INTERVAL = 300            # Seconds between checks for new uploads
YOUTUBE_MAX_VIDEO_AGE = 2 * 24 * 3600  # Ignore videos older than this (seconds), e.g. old videos resurfacing in the feed
