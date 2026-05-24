import discord
from discord.ext import commands, tasks
from discord import Embed, app_commands, Permissions
from discord.ext.commands import HelpCommand
import random
import re
from collections import deque
import time
import datetime
import json
import os
import asyncio

# =========================
# CONFIG
# =========================

TOKEN = os.getenv("TOKEN")
MOD_CHANNEL_ID = 1504270186176446464
ADMIN_ROLE_ID = 1504558360979312681
MOD_ROLE_ID = 123456789012345678
GUILD_ID = 1504557877493370900  

DATA_FOLDER = "data"

# =========================
# MEMORY CLEANUP SETTINGS
# =========================

MESSAGE_LINK_TTL = 60 * 60      # 1 hour
ACTIVITY_TTL = 10 * 60         # 10 minutes

BANNED_USERS_FILE = os.path.join(DATA_FOLDER, "banned_users.json")
BANNED_SERVERS_FILE = os.path.join(DATA_FOLDER, "banned_servers.json")
WARNINGS_FILE = os.path.join(DATA_FOLDER, "warnings.json")

os.makedirs(DATA_FOLDER, exist_ok=True)

# =========================
# JSON HELPERS 
# =========================

json_lock = asyncio.Lock()

def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if not content:
                return default
            return json.loads(content)
    except (json.JSONDecodeError, ValueError) as e:
        print(f"[JSON ERROR] {path}: {e}")
        return default

async def save_json(path, data):
    async with json_lock:
        tmp_path = path + ".tmp"
        
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
            f.flush()
            os.fsync(f.fileno())

        os.replace(tmp_path, path)

banned_users = load_json(BANNED_USERS_FILE, {})
banned_servers = load_json(BANNED_SERVERS_FILE, {})
warnings = load_json(WARNINGS_FILE, {})

# =========================
# INTENTS
# =========================

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.reactions = True

bot = commands.Bot(command_prefix=["y.", "Y."], intents=intents)

# =========================
# STATE
# =========================

waiting_channels = []
active_calls = {}
last_call_log = {}
report_messages = {}
webhook_cache = {}
webhook_locks = {}   
search_messages = {}  # channel_id -> message

message_links = {}
reaction_lock = set()
last_activity = {}
INACTIVITY_LIMIT = 300
command_cooldowns = {}  # (channel_id, command_name) -> timestamp

DEFAULT_COOLDOWNS = {
    "call": 7.2,
    "hangup": 7.2,
    "skip": 7.2
}

def cooldown_response(remaining, command_name):
    return f"⏳ Please wait {remaining:.1f}s before using `/{command_name}` again."

LOG_LIMIT = 20

# =========================
# CALL LOGS
# =========================
call_logs = {}  # channel_id -> deque of recent messages

# =========================
# HELPER FUNCTIONS
# =========================
def _is_banned(store, id_):
    ban = store.get(str(id_))
    
    if not ban:
        return False, False
    
    if ban["expiry"] is not None and time.time() > ban["expiry"]:
        store.pop(str(id_), None)
        return False, True # expired and removed

    return True, False    

def is_user_banned(user_id):
    result, changed = _is_banned(banned_users, str(user_id))

    if changed:
        asyncio.create_task(save_json(BANNED_USERS_FILE, banned_users))

    return result

def is_server_banned(guild_id):
    result, _ = _is_banned(banned_servers, str(guild_id))
    return result

# =========================
# duration helper
# =========================
def parse_duration(text: str):
    if not text:
        return None

    text = text.lower().strip()

    if text in ["0", "perm", "permanent", "forever"]:
        return 0

    match = re.match(r"(\d+)\s*([a-z]+)", text)
    if not match:
        return None

    value = int(match.group(1))
    unit = match.group(2)

    units = {
        "s": 1, "sec": 1, "second": 1, "seconds": 1,
        "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
        "h": 3600, "hr": 3600, "hour": 3600, "hours": 3600,
        "d": 86400, "day": 86400, "days": 86400,
    }

    # allow full words like "1 day"
    for k, v in units.items():
        if unit.startswith(k):
            return value * v

    return None

# =========================
# COMMAND COOLDOWN CHECKER
# =========================
command_cooldowns = {}  # (channel_id, command_name) -> timestamp

def check_cooldown(ctx_or_interaction, command_name):
    channel = getattr(ctx_or_interaction, "channel", None)
    if channel is None:
        return True, 0

    key = (channel.id, command_name)
    now = time.time()

    cooldown_time = DEFAULT_COOLDOWNS.get(command_name, 7.2)

    last_used = command_cooldowns.get(key, 0)
    remaining = cooldown_time - (now - last_used)

    if remaining > 0:
        return False, remaining

    command_cooldowns[key] = now
    return True, 0

# =========================
# WEBHOOK CHECKER
# =========================

async def get_or_create_webhook(channel):
    hooks = await channel.webhooks()

    # 1. try find existing webhook
    for w in hooks:
        if w.name == "Yappify":
            return w

    # 2. safety: avoid hitting webhook limit blindly
    if len(hooks) >= 10:
        return hooks[0]  # fallback instead of creating new one

    # 3. create only if missing
    return await channel.create_webhook(name="Yappify")

# =========================
# CLEANUP
# =========================

def cleanup_memory(cid, partner):
    for msg_id in list(message_links.keys()):
        data = message_links[msg_id]
        if data.get("channel") in (cid, partner):
            message_links.pop(msg_id, None)

    # DO NOT delete webhook cache here
    # webhook_cache stays persistent to avoid webhook spam

    # webhook_cache.pop(cid, None)
    # webhook_cache.pop(partner, None)
    last_activity.pop(cid, None)
    last_activity.pop(partner, None)

# =========================
# CLEANUP UNUSED MEMORY
# =========================

def cleanup_expired_memory():
    now = time.time()

    # -------------------------
    # MESSAGE LINKS CLEANUP
    # -------------------------
    for msg_id in list(message_links.keys()):
        data = message_links.get(msg_id, {})
        if now - data.get("timestamp", now) > MESSAGE_LINK_TTL:
            message_links.pop(msg_id, None)

    # -------------------------
    # ACTIVITY CLEANUP
    # -------------------------
    for cid in list(last_activity.keys()):
        if now - last_activity[cid] > ACTIVITY_TTL:
            last_activity.pop(cid, None)

# =========================
# BAN CLEANUP
# =========================

async def cleanup_bans():
    now = time.time()
    grace_period = 30 * 24 * 60 * 60

    changed_users = False
    changed_servers = False

    for user_id, data in list(banned_users.items()):
        expiry = data.get("expiry")
        if expiry is not None and now > expiry + grace_period:
            banned_users.pop(user_id, None)
            changed_users = True

    for guild_id, data in list(banned_servers.items()):
        expiry = data.get("expiry")
        if expiry is not None and now > expiry + grace_period:
            banned_servers.pop(guild_id, None)
            changed_servers = True

    if changed_users:
        await save_json(BANNED_USERS_FILE, banned_users)

    if changed_servers:
        await save_json(BANNED_SERVERS_FILE, banned_servers)

# =========================
# TIME_AGO()
# =========================
def time_ago(timestamp):
    now = time.time()
    diff = int(now - timestamp)

    if diff < 60:
        return f"{diff} seconds ago"
    elif diff < 3600:
        return f"{diff // 60} minutes ago"
    elif diff < 86400:
        return f"{diff // 3600} hours ago"
    else:
        return f"{diff // 86400} days ago"

# =========================
# LOG CALL
# =========================
def log_call_message(message):
    cid = message.channel.id

    if cid not in call_logs:
        call_logs[cid] = deque(maxlen=LOG_LIMIT)

    call_logs[cid].append({
        "server_name": message.guild.name,
        "server_id": message.guild.id,
        "user_name": message.author.display_name,
        "user_id": message.author.id,
        "content": message.content if message.content else "[Attachment]"
    })

# =========================
# Mod Log
# =========================

async def log_mod_action(action: str, target: str, reason: str, moderator: str):
    channel = bot.get_channel(MOD_CHANNEL_ID)
    if not channel:
        return

    embed = discord.Embed(
        title="🛡️ Moderation Action",
        color=0xE74C3C
    )

    embed.add_field(name="Action", value=action, inline=False)
    embed.add_field(name="Target", value=target, inline=False)
    embed.add_field(name="Reason", value=reason, inline=False)
    embed.add_field(name="Moderator", value=moderator, inline=False)
    embed.timestamp = discord.utils.utcnow()

    await channel.send(embed=embed)

# =========================
# BAN USER CORE (SHARED LOGIC)
# =========================
async def ban_user_core(ctx_or_interaction, user: discord.User, duration: int = 0, reason: str = "No reason"):
    """
    Ban a user globally across all shared servers.
    """
    expiry = None
    if duration is not None and duration > 0:
        expiry = time.time() + duration

    banned_users[str(user.id)] = {
        "expiry": expiry,
        "reason": reason,
        "timestamp": int(time.time())
    }

    await save_json(BANNED_USERS_FILE, banned_users)

    # =========================
    # DM USER ABOUT BAN
    # =========================
    try:

        if duration == 0:
            duration_text = "Permanent"
        else:
            end_timestamp = int(time.time() + duration)
            duration_text = f"<t:{end_timestamp}:R>"

        embed = discord.Embed(
            title="🚫 You Were Banned",
            color=0xE74C3C
        )

        embed.add_field(
            name="Reason",
            value=reason if reason else "No reason provided",
            inline=False
        )

        embed.add_field(
            name="Ban expired in" if duration != 0 else "Ban Type",
            value=duration_text,
            inline=False
        )

        embed.set_footer(text="Yappify Moderation")

        await user.send(embed=embed)

    except:
        pass

    # Optionally kick/ban user from shared servers
    for guild in bot.guilds:
        member = guild.get_member(user.id)
        if member:
            try:
                await guild.ban(member, reason=reason, delete_message_days=0)
            except:
                pass  # fail silently if missing perms

    # Optionally log
    if hasattr(ctx_or_interaction, "send"):
        await ctx_or_interaction.send(f"✅ <@{user.id}> banned. Reason: {reason}")
    elif hasattr(ctx_or_interaction, "response"):
        await ctx_or_interaction.response.send_message(f"✅ <@{user.id}> banned. Reason: {reason}")

# =========================
# BAN SERVER CORE (SHARED LOGIC)
# =========================
async def ban_server_core(ctx_or_interaction, guild: discord.Guild, duration: int = 0, reason: str = "No reason"):
    """
    Ban a server globally: removes it from matchmaking and logs the ban.
    """
    expiry = None if duration == 0 else time.time() + duration

    banned_servers[str(guild.id)] = {
        "expiry": expiry,
        "reason": reason,
        "timestamp": int(time.time())
    }

    await save_json(BANNED_SERVERS_FILE, banned_servers)

    # Optionally, remove active calls from that server
    channels_to_remove = [cid for cid, partner in active_calls.items() if cid in [ch.id for ch in guild.text_channels]]
    for cid in channels_to_remove:
        partner = active_calls.pop(cid, None)
        if partner:
            active_calls.pop(partner, None)
        cleanup_memory(cid, partner)

    # Log or notify
    if hasattr(ctx_or_interaction, "send"):
        await ctx_or_interaction.send(f"🚫 Server {guild.name} banned. Reason: {reason}")
    elif hasattr(ctx_or_interaction, "response"):
        await ctx_or_interaction.response.send_message(f"🚫 Server {guild.name} banned. Reason: {reason}")

# =========================
# WARN CORE (SHARED LOGIC)
# =========================
async def warn_core(ctx_or_interaction, user: discord.User, reason: str):
    """
    Warn a user globally. Returns True if auto-banned after 3 warnings.
    """
    key = str(user.id)
    warns = warnings.get(key, [])

    moderator = getattr(ctx_or_interaction, "author", None) or getattr(ctx_or_interaction, "user", None)
    warns.append({
        "reason": reason,
        "moderator": str(moderator),
        "timestamp": time.time()
    })
    warnings[key] = warns
    await save_json(WARNINGS_FILE, warnings)

    await log_mod_action(
        action="Warn User",
        target=f"{user} ({user.id})",
        reason=reason,
        moderator=str(moderator)
    )
    # =========================
    # DM USER ABOUT WARNING
    # =========================
    try:

        warning_count = len(warns)

        embed = discord.Embed(
            title="⚠️ You Were Warned",
            color=0xF1C40F
        )

        embed.add_field(
            name="Reason",
            value=reason if reason else "No reason provided",
            inline=False
        )

        embed.add_field(
            name="Warning Count",
            value=f"{warning_count}/3",
            inline=False
        )

        if warning_count == 2:
            embed.add_field(
                name="Final Warning",
                value="⚠️ If you are warned again, you will be permanently banned.",
                inline=False
            )

        embed.set_footer(text="Yappify Moderation")

        await user.send(embed=embed)

    except:
        pass

    # Auto-ban if 3 warnings
    if len(warns) >= 3:
        await ban_user_core(ctx_or_interaction, user, 0, "Auto-ban: 3 warnings")
        return True
    return False

# =========================
# Warning cleanup
# =========================

async def trim_warnings():
    changed = False

    for user_id, warns in list(warnings.items()):
        if isinstance(warns, list) and len(warns) > 10:
            warnings[user_id] = warns[-10:]
            changed = True

    if changed:
        await save_json(WARNINGS_FILE, warnings)
# =========================
# UNBAN USER CORE (SHARED LOGIC)
# =========================
async def unban_user_core(ctx_or_interaction, user: discord.User, reason: str = "No reason"):

    # ✅ SAFETY CHECK (NEW)
    if not isinstance(user, discord.User):
        return False

    key = str(user.id)

    if not banned_users.get(key):
        return False

    banned_users.pop(key)

    await save_json(BANNED_USERS_FILE, banned_users)

    for guild in bot.guilds:
        try:
           await guild.unban(user, reason=reason)
        except (discord.NotFound, discord.Forbidden):
            pass
        except Exception as e:
            print(f"[UNBAN ERROR] {guild.id}: {e}")

    try:
        embed = discord.Embed(
            title="✅ You Have Been Unbanned",
            description="Your ban has been removed.",
            color=0x2ECC71
        )

        embed.add_field(
            name="Reason",
            value=reason,
            inline=False
        )

        embed.set_footer(text="Yappify Moderation")

        await user.send(embed=embed)

    except:
        pass

    return True
# =========================
# UNBAN SERVER CORE (SHARED LOGIC)
# =========================
async def unban_server_core(ctx_or_interaction, guild: discord.Guild, reason: str = "No reason"):
    key = str(guild.id)
    if key not in banned_servers:
        return False

    banned_servers.pop(key)
    await save_json(BANNED_SERVERS_FILE, banned_servers)

    # Log or notify
    if hasattr(ctx_or_interaction, "send"):
        await ctx_or_interaction.send(f"✅ Server {guild.name} unbanned. Reason: {reason}")
    elif hasattr(ctx_or_interaction, "response"):
        await ctx_or_interaction.response.send_message(f"✅ Server {guild.name} unbanned. Reason: {reason}")

    return True

# =========================
# FILTERS
# =========================

NSFW_KEYWORDS = [
    "nsfw", "porn", "sex", "sexual", "nude", "horny",
    "dick", "pussy", "penis", "vagina", "cum"
]

INVITE_PATTERN = re.compile(r"(discord\.gg|discord\.com/invite|discordapp\.com/invite)", re.I)
NSFW_PATTERN = re.compile("|".join(map(re.escape, NSFW_KEYWORDS)), re.I)

# =========================
# ADMIN MOD HELPER
# =========================

def get_member(obj):
    return getattr(obj, "author", getattr(obj, "user", obj))


def has_role(member, role_id: int):
    return any(role.id == role_id for role in member.roles)


def is_admin(obj):
    member = get_member(obj)
    return has_role(member, ADMIN_ROLE_ID)


def is_mod(obj):
    member = get_member(obj)
    return has_role(member, MOD_ROLE_ID)


def can_moderate(obj):
    member = get_member(obj)
    return (
        has_role(member, ADMIN_ROLE_ID) or
        has_role(member, MOD_ROLE_ID)
    )

def can_target(actor, target):
    actor_member = get_member(actor)
    target_member = target

    # prevent acting on bots (optional but recommended)
    if target_member.bot:
        return False  

    # admin can target anyone
    if has_role(actor_member, ADMIN_ROLE_ID):
        return True

    # mods cannot target admins or other mods
    if has_role(target_member, ADMIN_ROLE_ID):
        return False

    if has_role(target_member, MOD_ROLE_ID):
        return False

    return True

# =========================
# webhook helper
# =========================

async def check_webhook_permissions(guild: discord.Guild):
    me = guild.me  # bot member in that guild
    if not me:
        return False

    perms = me.guild_permissions
    return perms.manage_webhooks and perms.send_messages

# =========================
# CALL CORE
# =========================

async def start_call(channel, user, status_message=None):
    cid = channel.id

    if is_server_banned(channel.guild.id):
        return await channel.send("🚫 Your server is banned.")

    if is_user_banned(user.id):
        return await channel.send("🚫 You are banned.")

    if cid in active_calls:
        if status_message:
            await status_message.edit(content="❌ Already in a call.")
        return

    if cid in waiting_channels:
        if status_message:
            await status_message.edit(
                content="⏳ Already searching for a server...\nUse `/hangup` or `y.h` to leave the queue."
            )
        return

    candidates = [c for c in waiting_channels if c != cid]

    if not candidates:
        if cid not in waiting_channels:
            waiting_channels.append(cid)

        #store "searching message" if provided
        if status_message:
            search_messages[cid] = status_message

        return

    partner = random.choice(candidates)

    if partner in waiting_channels:
        waiting_channels.remove(partner)

    partner_channel = bot.get_channel(partner)

    if partner_channel is None:
        waiting_channels.append(cid)
        return await channel.send("📞 Partner unavailable, re-queueing...")

    if is_server_banned(partner_channel.guild.id):
        waiting_channels.append(cid)
        return await channel.send("📞 Partner banned, searching again...")

    active_calls[cid] = partner
    active_calls[partner] = cid

    try:
        # 🔥 EDIT original "Searching..." message
        if status_message:
            try:
                await status_message.edit(
                    content=f"☎️ Connected to {partner_channel.guild.name}",
                    allowed_mentions=discord.AllowedMentions.none()
                )
            except Exception as e:
                print("[EDIT ERROR]", e)

        # clean stored reference
        search_messages.pop(cid, None)

        # notify partner side (ONLY ONE extra message, correct side)
        await partner_channel.send(
            f"☎️ Connected to {channel.guild.name}"
        )

    except Exception as e:
        active_calls.pop(cid, None)
        active_calls.pop(partner, None)

        if cid not in waiting_channels:
            waiting_channels.append(cid)

        if partner in active_calls:
            active_calls.pop(partner, None)

        print("[CALL ERROR]", e)

        # ✅ show failure properly
        if status_message:
            try:
                await status_message.edit(
                    content="❌ Failed to establish call. Re-queueing...",
                    allowed_mentions=discord.AllowedMentions.none()
                )
            except:
                await channel.send("❌ Failed to establish call. Re-queueing...")
        else:
            await channel.send("❌ Failed to establish call. Re-queueing...")

# =========================
# PROCESSING QUEUE
# =========================

async def process_queue():
    while len(waiting_channels) >= 2:
        a = waiting_channels.pop(0)
        b = waiting_channels.pop(0)

        # safety checks
        if a == b:
            waiting_channels.append(a)
            continue

        ch1 = bot.get_channel(a)
        ch2 = bot.get_channel(b)

        if not ch1 or not ch2:
            continue

        # create call
        active_calls[a] = b
        active_calls[b] = a

        await ch1.send(f"☎️ Connected to {ch2.guild.name}")
        await ch2.send(f"☎️ Connected to {ch1.guild.name}")

# =========================
# END CALL
# =========================

async def end_call(ctx):
    cid = ctx.channel.id

    if cid not in active_calls:
        await ctx.send("❌ Not in a call. Use s.call to start one.")
        return

    partner = active_calls.get(cid)
    partner_channel = bot.get_channel(partner)

    try:
        if partner_channel:
            await partner_channel.send("🛑 Call ended")
        await ctx.send("🛑 Call ended")
    except:
        pass

    active_calls.pop(cid, None)
    active_calls.pop(partner, None)

    cleanup_memory(cid, partner)

# =========================
# INACTIVITY
# =========================

@tasks.loop(seconds=60)
async def inactivity_checker():
    now = time.time()

    for cid in list(active_calls.keys()):
        last = last_activity.get(cid, now)

        if now - last >= INACTIVITY_LIMIT:
            # Get partner channel
            partner = active_calls.get(cid)
            partner_channel = bot.get_channel(partner) if partner else None
            channel = bot.get_channel(cid)

            # Notify the inactive server
            if channel:
                try:
                    await channel.send("🛑 You were inactive, call ended")
                except:
                    pass

            # Notify the partner server
            if partner_channel:
                try:
                    await partner_channel.send("🛑 Call ended because the other server was inactive")
                except:
                    pass

            # Remove both from active calls
            active_calls.pop(cid, None)
            if partner:
                active_calls.pop(partner, None)

            # Cleanup memory
            cleanup_memory(cid, partner)

            # Remove from last_activity tracking
            last_activity.pop(cid, None)
            if partner:
                last_activity.pop(partner, None)

# =========================
# BAN CLEANUP LOOP
# =========================
@tasks.loop(hours=24)
async def ban_cleanup_loop():
    await cleanup_bans()

# =========================
# memory loop
# =========================

@tasks.loop(minutes=1)
async def memory_cleanup_loop():
    cleanup_expired_memory()

# =========================
# READY
# =========================

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")

    # =========================
    # 🔥 RESET RUNTIME STATE (NEW)
    # =========================
    active_calls.clear()
    waiting_channels.clear()
    last_activity.clear()
    search_messages.clear()

    print("🧹 Runtime state cleared after restart")

    try:
        # 🔥 IMPORTANT: sync slash commands
        synced = await bot.tree.sync(guild=discord.Object(id=GUILD_ID))

        print(f"✅ Synced {len(synced)} guild commands")

    except Exception as e:
        print(f"❌ Sync failed: {e}")

    # start loops safely
    if not inactivity_checker.is_running():
        inactivity_checker.start()

    if not ban_cleanup_loop.is_running():
        ban_cleanup_loop.start()

    if not memory_cleanup_loop.is_running():
        memory_cleanup_loop.start()

# =========================
# webhook required
# =========================

@bot.event
async def on_guild_join(guild: discord.Guild):
    if not await check_webhook_permissions(guild):

        embed = discord.Embed(
            title="⚠️ Setup Required for Yappify",
            description=(
                "I can’t fully function in this server.\n\n"
                "**Missing permissions:**\n"
                "• Manage Webhooks\n"
                "• Send Messages\n\n"
                "Without these, I cannot relay messages between servers."
            ),
            color=0xE74C3C
        )

        embed.add_field(
            name="Fix",
            value="Re-invite the bot with Manage Webhooks enabled.",
            inline=False
        )

        embed.set_footer(text="Yappify Setup Check")

        channel = guild.system_channel

        if channel and channel.permissions_for(guild.me).send_messages:
            await channel.send(embed=embed)
        else:
            for ch in guild.text_channels:
                if ch.permissions_for(guild.me).send_messages:
                    await ch.send(embed=embed)
                    break

# =========================
# WEBHOOK MESSAGE SYSTEM
# =========================

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    cid = message.channel.id

    # =========================
    # Ignore specific Yappify commands so they are not relayed
    # =========================
    ignore_commands = [
        "y.s", "y.skip", "y.S", 
        "y.h", "y.hangup", "y.H",
        "y.fr", "y.friend",   # added friend commands
        "y.report", "y.r",
    ]
    if any(message.content.startswith(cmd) for cmd in ignore_commands):
        await bot.process_commands(message)  # still process them locally
        return  # DO NOT try to relay, prevents KeyError

    # =========================
    # Only continue if this channel is in an active call
    # =========================
    if cid not in active_calls:
        await bot.process_commands(message)
        return

    # Safe to access active_calls[cid] now
    target_id = active_calls[cid]
    target = bot.get_channel(target_id)
    if not target:
        return

    # =========================
    # Update last activity
    # =========================
    last_activity[cid] = time.time()
    log_call_message(message)


    # =========================
    # WEBHOOK SAFE FETCH (NO DUPES)
    # =========================

    lock = webhook_locks.setdefault(target.id, asyncio.Lock())

    async with lock:
        webhook = webhook_cache.get(target.id)

        # 1. check cache validity
        if webhook:
            try:
                await webhook.fetch()
            except:
                webhook = None

        if not target.permissions_for(target.guild.me).manage_webhooks:
            return await channel.send("🚫 Missing Manage Webhooks permission in target server.")

        # 2. rebuild if missing/invalid
        if webhook is None:
            webhook = await get_or_create_webhook(target)
            webhook_cache[target.id] = webhook

    content = message.content

    # =========================
    # FILTER SYSTEM
    # =========================

    content_lower = message.content.lower()

    # Invite filter
    if INVITE_PATTERN.search(message.content):
        await message.channel.send("❌ Discord invites are not allowed.")
        return

    # NSFW filter
    if NSFW_PATTERN.search(content_lower):
        await message.channel.send("❌ NSFW content is not allowed.")
        return

    # =========================
    # ATTACHMENT FILTER SYSTEM
    # =========================
    files = []

    image_count = 0
    video_count = 0

    MAX_VIDEO_SIZE = 24 * 1024 * 1024  # 24MB

    # Validate first
    for attachment in message.attachments:

        filename = attachment.filename.lower()
        content_type = attachment.content_type or ""

        is_gif = filename.endswith(".gif")

        is_image = (
            content_type.startswith("image/")
            or filename.endswith((".png", ".jpg", ".jpeg", ".webp"))
        )

        is_video = (
            content_type.startswith("video/")
            or filename.endswith((".mp4", ".mov", ".webm", ".mkv"))
        )


        # ❌ BLOCK GIF FILE UPLOADS
        if is_gif:
            await message.channel.send("❌ GIF uploads are disabled. Use Discord GIF picker (Tenor/Giphy).")
            return

        # ❌ BLOCK VIDEOS
        if is_video:
            if attachment.size > MAX_VIDEO_SIZE:
                await message.channel.send("❌ Video exceeds 24MB limit.")
                return
            video_count += 1
            continue

        if is_image:
            image_count += 1
            continue

        # BLOCK EVERYTHING ELSE
        await message.channel.send("❌ Unsupported file type.")
        return

    # Hard limits
    if image_count > 2:
        await message.channel.send(
            "❌ A maximum of 2 images allowed.")
        return

    if video_count > 1:
        await message.channel.send(
            "❌ Only 1 video can be sent per message.")
        return

    # Convert files only AFTER validation passes
    for attachment in message.attachments:
        try:
            files.append(await attachment.to_file())
        except Exception as e:
            print(f"[ATTACHMENT ERROR] {e}")

    # =========================
    # REPLY SYSTEM
    # =========================
    content = message.content

    if message.reference and message.reference.message_id:
        try:
            ref_msg = await message.channel.fetch_message(message.reference.message_id)

            embed = discord.Embed(
                description=ref_msg.content or "*No text*",
                color=0x1ABC9C
            )
            embed.set_author(
                name=ref_msg.author.display_name,
                icon_url=ref_msg.author.display_avatar.url
            )

            sent = await webhook.send(
                content=content,
                embed=embed,
                username=message.author.display_name,
                avatar_url=message.author.display_avatar.url,
                wait=True,
                allowed_mentions=discord.AllowedMentions.none()
            )

            # =========================
            # MESSAGE LINK TRACKING (FIXED)
            # =========================
            message_links[message.id] = {
                "channel": target.id,
                "message": sent.id,
                "timestamp": time.time()
            }

            message_links[sent.id] = {
                "channel": cid,
                "message": message.id,
                "timestamp": time.time()
            }

            await bot.process_commands(message)
            return

        except Exception as e:
            print("[REPLY ERROR]", e)

    # =========================
    # NORMAL MESSAGE RELAY
    # =========================
    try:
        sent = await webhook.send(
            content=message.content,
            username=message.author.display_name,
            avatar_url=message.author.display_avatar.url,
            files=files,
            wait=True,
            allowed_mentions=discord.AllowedMentions.none()
        )

    except discord.Forbidden:
        await message.channel.send(
           "🚫 I don't have webhook permissions in this server. Please enable **Manage Webhooks** and **Send Messages**."
        )
        return

    except discord.HTTPException as e:
        await message.channel.send(
            "🚫 Webhook failed. This usually means missing permissions or the webhook was deleted. Please re-enable permissions."
        )
        return

    # =========================
    # MESSAGE LINK TRACKING (FIXED)
    # =========================
    message_links[message.id] = {
        "channel": target.id,
        "message": sent.id,
        "timestamp": time.time()
    }

    message_links[sent.id] = {
        "channel": cid,
        "message": message.id,
        "timestamp": time.time()
    }

    await bot.process_commands(message)

# =========================
# REACTIONS (SYNC ACROSS SERVERS)
# =========================

@bot.event
async def on_raw_reaction_add(payload):
    if payload.user_id == bot.user.id:
        return

    data = message_links.get(payload.message_id)
    if not data:
        return

    try:
        channel = bot.get_channel(data["channel"])
        if not channel:
            return

        message = await channel.fetch_message(data["message"])
        await message.add_reaction(payload.emoji)

    except Exception as e:
        print(f"[RAW REACTION ADD ERROR]: {e}")

# =========================
# REACTION REMOVE
# =========================

@bot.event
async def on_raw_reaction_remove(payload):
    if payload.user_id == bot.user.id:
        return

    data = message_links.get(payload.message_id)
    if not data:
        return

    try:
        channel = bot.get_channel(data["channel"])
        if not channel:
            return

        message = await channel.fetch_message(data["message"])
        await message.remove_reaction(payload.emoji, bot.get_user(payload.user_id))

    except Exception as e:
        print(f"[RAW REACTION REMOVE ERROR]: {e}")

# =========================
# USER HELPER
# =========================
def get_user(obj):
    """Return user object from ctx or interaction"""
    if hasattr(obj, "author"):  # prefix ctx
        return obj.author
    elif hasattr(obj, "user"):  # slash interaction
        return obj.user
    return obj

# =========================
# FINDING USER HELPER
# =========================

async def find_shared_user(user_id: int):
    # First try cache
    user = bot.get_user(user_id)

    if user:
        return user

    # Search shared guilds
    for guild in bot.guilds:
        member = guild.get_member(user_id)
        if member:
            return member

    # Final API fetch
    try:
        return await bot.fetch_user(user_id)
    except:
        return None
# =========================
# MSG DELETE HELPER
# =========================

async def send_temp_message(interaction, channel, content, delete_after=3):
    if interaction:
        await interaction.response.send_message(content)
        msg = await interaction.original_response()
        await asyncio.sleep(delete_after)
        await msg.delete()
    else:
        msg = await channel.send(content)
        await asyncio.sleep(delete_after)
        await msg.delete()

# =========================
# CALL HELPER
# =========================
async def start_call_action(channel_id, user=None, interaction=None):
    """Handles starting a call."""
    ok, wait = check_cooldown(user or interaction, "call")

    if not ok:
        msg = cooldown_response(wait, "call")
        channel = bot.get_channel(channel_id)
        return await send_temp_message(interaction, channel, msg)

    channel = bot.get_channel(channel_id)
    if not channel:
        return

    # ✅ IMPORTANT: show searching message
    msg = await channel.send("📞 Searching for a server...")

    await start_call(
        channel,
        get_user(user or interaction),
        status_message=msg
    )

# =========================
# HANGUP HELPER
# =========================
async def hangup_action(channel_id, user=None, interaction=None):

    cid = channel_id
    channel = bot.get_channel(cid)

    if not channel:
        return

    # ✅ ADD THIS HERE (MOST IMPORTANT)
    if not await assert_not_banned(channel):
        return

    ok, wait = check_cooldown(user or interaction, "hangup")

    if not ok:
        msg = cooldown_response(wait, "hangup")

        channel = bot.get_channel(channel_id)
        return await send_temp_message(interaction, channel, msg)

    # =========================
    # LEFT MATCHMAKING QUEUE
    # =========================
    if cid in waiting_channels:

        while cid in waiting_channels:
            waiting_channels.remove(cid)

        msg_text = "✅ Left the matchmaking queue."

        if interaction:
            return await interaction.response.send_message(msg_text)

        return await channel.send(msg_text)

    # =========================
    # END ACTIVE CALL
    # =========================
    if cid in active_calls:

        partner = active_calls.pop(cid, None)

        if partner:
            active_calls.pop(partner, None)

        cleanup_memory(cid, partner)

        partner_channel = bot.get_channel(partner)

        if partner_channel:
            try:
                await partner_channel.send("🛑 Call ended")
            except:
                pass

        msg_text = "🛑 Call ended"

        if interaction:
            return await interaction.response.send_message(msg_text)

        return await channel.send(msg_text)

    # =========================
    # NOT IN CALL OR QUEUE
    # =========================
    msg_text = "❌ You are not searching or in a call."

    if interaction:
        await interaction.response.send_message(msg_text)
    else:
        await channel.send(msg_text)

# =========================
# SKIP HELPER
# =========================
async def skip_action(channel_id, user=None, interaction=None):

    cid = channel_id
    channel = bot.get_channel(cid)

    if not channel:
        return

    # ✅ ADD THIS HERE (MOST IMPORTANT)
    if not await assert_not_banned(channel):
        return

    ok, wait = check_cooldown(user or interaction, "skip")

    if not ok:
        msg = cooldown_response(wait, "skip")

        channel = bot.get_channel(channel_id)
        return await send_temp_message(interaction, channel, msg)

    # =========================
    # ALREADY SEARCHING
    # =========================
    if cid in waiting_channels:

        msg_text = (
            "⏳ Already searching. "
            "Use `/hangup` or `y.h` to leave the queue."
        )

        if interaction:
            return await interaction.response.send_message(msg_text)

        return await channel.send(msg_text)

    # =========================
    # SKIP ACTIVE CALL
    # =========================
    if cid in active_calls:

        partner = active_calls.pop(cid, None)

        if partner:
            active_calls.pop(partner, None)

        cleanup_memory(cid, partner)

        while cid in waiting_channels:
            waiting_channels.remove(cid)

        waiting_channels.append(cid)

        msg_text = "📞 Call skipped, searching for another server..."

        if interaction:
            await interaction.response.send_message(msg_text)
        else:
            await channel.send(msg_text)

        partner_channel = bot.get_channel(partner)

        if partner_channel:
            try:
                await partner_channel.send(
                    "🛑 The other server skipped the call."
                )
            except:
                pass

        await process_queue()
        return

    # =========================
    # NOT IN ANYTHING
    # =========================
    msg_text = (
        "❌ You are not in a call.\n"
        "Use `/call` or `y.call` to start searching."
    )

    if interaction:
        await interaction.response.send_message(msg_text)
    else:
        await channel.send(msg_text)

# =========================
# REPORT HELPER
# =========================
async def report_action(channel_id, reporter=None, interaction=None):
    cid = channel_id
    channel = bot.get_channel(cid)
    if not channel:
        return

    if not await assert_not_banned(channel):
        return

    if cid not in active_calls:
        msg_text = "❌ You can only report during an active call."
        if interaction:
            await interaction.response.send_message(msg_text)
        else:
            await reporter.send(msg_text)
        return

    partner_id = active_calls[cid]
    reported_channel = bot.get_channel(partner_id)
    if not reported_channel:
        msg_text = "❌ Could not find reported server."
        if interaction:
            await interaction.response.send_message(msg_text)
        else:
            await reporter.send(msg_text)
        return

    mod_channel = bot.get_channel(MOD_CHANNEL_ID)
    if not mod_channel:
        msg_text = "❌ Mod channel not found."
        if interaction:
            await interaction.response.send_message(msg_text)
        else:
            await reporter.send(msg_text)
        return

    reporter_server = reporter.guild if reporter else interaction.guild
    reporter_logs = list(call_logs.get(cid, []))[-7:]
    reported_logs = list(call_logs.get(partner_id, []))[-7:]
    merged_logs = reporter_logs + reported_logs

    embed = discord.Embed(title="🚨 Call Report", color=0xE74C3C)
    embed.add_field(name="Reporter Server", value=f"{reporter_server.name} ({reporter_server.id})", inline=False)
    embed.add_field(name="Reported Server", value=f"{reported_channel.guild.name} ({reported_channel.guild.id})", inline=False)
    embed.add_field(name="Reporter", value=f"{reporter.display_name if reporter else interaction.user.display_name} ({reporter.id if reporter else interaction.user.id})", inline=False)

    log_text = ""
    for msg in merged_logs[:14]:
        content = msg["content"]
        if len(content) > 120:
            content = content[:120] + "..."
        log_text += f"**[{msg['server_name']}]** `{msg['user_id']}` | {msg['user_name']}: {content}\n"

    if log_text:
        embed.add_field(name="Last Call Messages", value=log_text[:1024], inline=False)

    await mod_channel.send(embed=embed)

    msg_text = "✅ Your report has been submitted, our moderators will review it shortly."
    if interaction:
        await interaction.response.send_message(msg_text)
    else:
        await reporter.send(msg_text)


# =========================
# FRIEND REQUEST HELPER
# =========================
async def friend_request_action(channel_id, user=None, interaction=None):
    cid = channel_id
    channel = bot.get_channel(cid)
    if not channel:
        return

    if not await assert_not_banned(channel):
        return

    partner_id = active_calls.get(cid)
    if not partner_id:
        msg_text = "❌ You are not in an active call."
        if interaction:
            await interaction.response.send_message(msg_text)
        else:
            await user.send(msg_text)
        return

    target_channel = bot.get_channel(partner_id)
    if not target_channel:
        msg_text = "❌ Could not find the connected server."
        if interaction:
            await interaction.response.send_message(msg_text)
        else:
            await user.send(msg_text)
        return

    webhook = webhook_cache.get(partner_id)
    if webhook is None:
        hooks = await target_channel.webhooks()
        webhook = next((w for w in hooks if w.name == "Yappify"), None)
        if webhook is None:
            webhook = await target_channel.create_webhook(name="Yappify")
        webhook_cache[partner_id] = webhook

    actor = get_user(user or interaction)

    actor_name = actor.display_name
    actor_id = actor.id

    await webhook.send(
        content=f"💌 Friend Request: {actor_name} (`{actor_id}`) wants to add you!",
        username="Yappify",
        avatar_url=bot.user.display_avatar.url,
        allowed_mentions=discord.AllowedMentions.none(),
        wait=True
    )

    msg_text = "✅ Friend request sent!"
    if interaction:
        await interaction.response.send_message(msg_text)
    else:
        await user.send(msg_text)

# =========================
# CLASS HELP
# =========================

class CleanHelp(HelpCommand):

    async def send_bot_help(self, mapping):
        embed = build_help_embed()
        await self.get_destination().send(embed=embed)

    async def send_command_help(self, command):
        embed = discord.Embed(
            title=f"📖 {command.name}",
            description=command.help or "No description",
            color=0x1ABC9C
        )
        await self.get_destination().send(embed=embed)

    async def send_cog_help(self, cog):
        embed = discord.Embed(
            title=f"📂 {cog.qualified_name}",
            description=cog.description or "No description",
            color=0x1ABC9C
        )
        await self.get_destination().send(embed=embed)

bot.help_command = CleanHelp()

# =========================
# HELP HELPER
# =========================

def build_help_embed():
    embed = discord.Embed(
        title="📖 Help Menu",
        description="Quick guide to using the bot",
        color=0x1ABC9C
    )

    embed.add_field(
        name="⌨️ Prefix Commands (y.)",
        value=(
            "`y.call` → Start call\n"
            "`y.skip` → Skip call\n"
            "`y.h` → Hang up / leave queue\n"
            "`y.fr` → Friend request\n"
            "`y.r` → Report\n"
            "`y.help` → Show menu"
        ),
        inline=False
    )

    embed.add_field(
        name="⚡ Slash Commands (/)",
        value=(
            "`/call` → Start call\n"
            "`/skip` → Skip call\n"
            "`/hangup` → Leave call\n"
            "`/friend` → Friend request\n"
            "`/report` → Report\n"
            "`/help` → Show menu"
        ),
        inline=False
    )

    embed.set_footer(text="Yappify Bot • Help Menu")
    return embed

# =========================
# Ban server helper
# =========================

async def assert_not_banned(channel):
    if is_server_banned(channel.guild.id):
        msg = await channel.send("🚫 Your server is banned.")
        await asyncio.sleep(2)
        try:
            await msg.delete()
        except:
            pass
        return False
    return True

# =========================
# FORMATTER HELPER
# =========================

def format_duration(expiry):
    if expiry is None:
        return "Permanent"

    if expiry == 0:
        return "Permanent"

    remaining = expiry - time.time()
    if remaining <= 0:
        return "Expired"

    days = int(remaining // 86400)
    hours = int((remaining % 86400) // 3600)
    minutes = int((remaining % 3600) // 60)

    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"

# =========================
# Resolve user helper
# =========================

async def resolve_user(ctx, input_value: str):
    # 1. Mention support
    if hasattr(ctx, "message") and ctx.message.mentions:
        return ctx.message.mentions[0]

    # 2. ID support
    if input_value.isdigit():
        user = await find_shared_user(int(input_value))
        if user:
            return user
        try:
            return await bot.fetch_user(int(input_value))
        except:
            return None

    # 3. Username match (best effort)
    input_value_lower = input_value.lower()

    for guild in bot.guilds:
        for member in guild.members:
            if member.name.lower() == input_value_lower:
                return member
            if str(member).lower() == input_value_lower:
                return member

    return None

# =========================
# PREFIX SLASH COMMANDS
# =========================

# Call
@bot.command(aliases=["c"])
async def call(ctx):

    # ✅ ADD THIS (FIRST LINE)
    if not await assert_not_banned(ctx.channel):
        return

    ok, wait = check_cooldown(ctx, "call")
    if not ok:
        return await ctx.send(cooldown_response(wait, "call"))

    cid = ctx.channel.id

    if cid in waiting_channels:
        return await ctx.send("⏳ Already searching.")

    if cid in active_calls:
        return await ctx.send("☎️ You're already in a call.")

    msg = await ctx.send("📞 Searching for a server...")

    await start_call(
        ctx.channel,
        ctx.author,
        status_message=msg
    )

@bot.tree.command(name="call", description="Start a call")
async def call_slash(interaction: discord.Interaction):

    if not await assert_not_banned(interaction.channel):
        return

    if interaction.channel.id in waiting_channels:
        return await interaction.response.send_message(
            "⏳ Already searching.\nUse `y.h` or `/hangup` to leave the queue.",
            ephemeral=True
        )

    ok, wait = check_cooldown(interaction, "call")

    if not ok:
        return await interaction.response.send_message(
            f"⏳ Please wait {wait:.1f}s before using this command again.",
            ephemeral=True
        )

    await interaction.response.send_message("📞 Searching for a server...")

    msg = await interaction.original_response()

    await start_call(
        interaction.channel,
        interaction.user,
        status_message=msg
    )

# Hangup
@bot.command(aliases=["h"])
async def hangup(ctx):
    await hangup_action(ctx.channel.id, user=ctx)

@bot.tree.command(name="hangup", description="End your current call")
async def hangup_slash(interaction: discord.Interaction):
    await hangup_action(interaction.channel.id, interaction=interaction)

# Skip
@bot.command(aliases=["s"])
async def skip(ctx):
    await skip_action(ctx.channel.id, user=ctx)

@bot.tree.command(name="skip", description="Skip your current call")
async def skip_slash(interaction: discord.Interaction):
    await skip_action(interaction.channel.id, interaction=interaction)

# Report
@bot.command(aliases=["r"])
async def report(ctx):
    await report_action(ctx.channel.id, reporter=ctx.author, interaction=None)

@bot.tree.command(name="report", description="Report the server you are in a call with")
async def report_slash(interaction: discord.Interaction):
    await report_action(interaction.channel.id, interaction=interaction)

# Friend Request
@bot.command(aliases=["fr"])
async def friend(ctx):
    await friend_request_action(ctx.channel.id, user=ctx)

@bot.tree.command(name="friend", description="Send a friend request to your call partner")
async def friend_slash(interaction: discord.Interaction):
    await friend_request_action(interaction.channel.id, interaction=interaction)

# Help

@bot.tree.command(name="help", description="Show bot commands")
async def help_slash(interaction: discord.Interaction):
    await interaction.response.send_message(
        embed=build_help_embed()
    )

# =========================
# PREFIX COMMANDS (ADMINS & MODS)
# =========================

# ----- BAN USER (PREFIX) -----
@bot.command(name="ban_user")
async def ban_user(ctx, user_input: str, duration: str = "0", *, reason="No reason"):

    if ctx.guild.id != GUILD_ID or not can_moderate(ctx):
        return await ctx.send("❌ Mods/Admins only.")

    duration_seconds = parse_duration(duration)

    if duration_seconds is None:
        return await ctx.send("❌ Invalid duration.\nExample: 10m, 1h, 1d")

    user = await resolve_user(ctx, user_input)

    if not user:
        return await ctx.send("❌ User not found.")

    await ban_user_core(ctx, user, duration_seconds, reason)

    await log_mod_action(
        action="Ban User",
        target=f"{user} ({user.id})",
        reason=reason,
        moderator=str(ctx.author)
    )

    await ctx.send(f"✅ {user} has been banned.")

# ----- UNBAN USER (PREFIX) -----
@bot.command(name="unban_user")
async def unban_user(ctx, user_input: str, *, reason="No reason"):

    if ctx.guild.id != GUILD_ID or not can_moderate(ctx):
        return await ctx.send("❌ Mods/Admins only.")

    user = await resolve_user(ctx, user_input)

    if not user:
        return await ctx.send("❌ User not found.")

    success = await unban_user_core(ctx, user, reason)

    await log_mod_action(
        action="Unban User",
        target=f"{user} ({user.id})",
        reason=reason,
        moderator=str(ctx.author)
    )

    await ctx.send("✅ Unbanned globally." if success else "❌ User not banned.")

# ----- BAN SERVER (PREFIX) -----
@bot.command(name="ban_server")
async def ban_server(ctx, guild_id: str, duration: str = "0", *, reason="No reason"):

    if ctx.guild.id != GUILD_ID or not is_admin(ctx):
        return await ctx.send("❌ Admins only.")

    # ✅ parse duration properly
    duration_seconds = parse_duration(duration)

    if duration_seconds is None:
        return await ctx.send("❌ Invalid duration...")

    guild = bot.get_guild(int(guild_id))

    if not guild:
        return await ctx.send(
            "❌ Server not found or bot not in server."
        )

    await ban_server_core(ctx, guild, duration_seconds, reason)

    await log_mod_action(
        action="Ban Server",
        target=f"{guild.name} ({guild.id})",
        reason=reason,
        moderator=str(ctx.author)
    )

    await ctx.send(
        f"🚫 Server {guild.name} banned."
    )


# ----- UNBAN SERVER (PREFIX) -----

@bot.command(name="unban_server")
async def unban_server(ctx, guild_id: int, *, reason="No reason"):

    if ctx.guild.id != GUILD_ID or not is_admin(ctx):
        return await ctx.send("❌ Admins only.")

    guild = bot.get_guild(guild_id)

    if not guild:
        return await ctx.send("❌ Server not found or bot not in it.")

    success = await unban_server_core(ctx, guild, reason)

    await log_mod_action(
        action="Unban Server",
        target=f"{guild.name} ({guild.id})",
        reason=reason,
        moderator=str(ctx.author)
    )

    if success:
        await ctx.send(f"✅ Server **{guild.name}** unbanned.")
    else:
        await ctx.send("❌ Server was not banned.")

# ----- BANLIST (PREFIX) -----

@bot.command(name="banlist")
async def banlist(ctx):

    if not can_moderate(ctx):
        return await ctx.send("❌ Mods/Admins only.")

    embed = discord.Embed(
        title="🚫 Ban List",
        color=0xE74C3C
    )

    # =========================
    # USER BANS
    # =========================
    if banned_users:
        user_text = ""

        for user_id, data in banned_users.items():
            expiry = data.get("expiry")
            reason = data.get("reason", "No reason")
            timestamp = data.get("timestamp", time.time())

            user = await find_shared_user(int(user_id))
            name = user.display_name if user else "Unknown User"

            user_text += (
                f"👤 **{name}** • `{user_id}`\n"
                f"• Reason: {reason}\n"
                f"• Duration: {format_duration(expiry)}\n"
                f"• Banned: {time_ago(timestamp)}\n\n"
            )


        embed.add_field(name="👤 User Bans", value=user_text[:1024], inline=False)
    else:
        embed.add_field(name="👤 User Bans", value="None", inline=False)

    # =========================
    # SERVER BANS
    # =========================
    if banned_servers:
        server_text = ""

        for guild_id, data in banned_servers.items():
            expiry = data.get("expiry")
            reason = data.get("reason", "No reason")
            timestamp = data.get("timestamp", time.time())

            guild = bot.get_guild(int(guild_id))
            name = guild.name if guild else "Unknown Server"

            server_text += (
                f"🏠 **{name}** • `{guild_id}`\n"
                f"• Reason: {reason}\n"
                f"• Duration: {format_duration(expiry)}\n"
                f"• Banned: {time_ago(timestamp)}\n\n"
            )

        embed.add_field(name="🏠 Server Bans", value=server_text[:1024], inline=False)
    else:
        embed.add_field(name="🏠 Server Bans", value="None", inline=False)

    await ctx.send(embed=embed)

# ----- WARN USER (PREFIX) -----
@bot.command(name="warn")
async def warn(ctx, user_id: int, *, reason="No reason"):
    if ctx.guild.id != GUILD_ID or not can_moderate(ctx):
        return await ctx.send("❌ Mods/Admins only.")

    user = await find_shared_user(user_id)

    if not user:
        return await ctx.send("❌ User not found or does not share a server with the bot.")

    banned = await warn_core(ctx, user, reason)
    if banned:
        await ctx.send("🚫 Auto-banned (3 warnings).")
    else:
        await ctx.send("⚠️ Warned.")


# ----- VIEW WARNINGS (PREFIX) -----
@bot.command(name="warninglist")
async def warnings_list(ctx, user_id: int):
    if ctx.guild.id != GUILD_ID or not can_moderate(ctx):
        return await ctx.send("❌ Mods/Admins only.")

    warns = warnings.get(str(user_id), [])
    if not warns:
        return await ctx.send("✅ No warnings.")

    msg = f"⚠️ Warnings for <@{user_id}>:\n\n"
    for i, w in enumerate(warns, 1):
        when = time_ago(w.get("timestamp", time.time()))
        msg += f"{i}. {w['reason']} | by {w.get('moderator','Unknown')} | {when}\n"

    await ctx.send(msg)


# ----- REMOVE WARNING (PREFIX) -----
@bot.command(name="removewarning")
async def removewarning(ctx, user_id: int, index: str = "last"):
    if ctx.guild.id != GUILD_ID or not can_moderate(ctx):
        return await ctx.send("❌ Mods/Admins only.")

    warns = warnings.get(str(user_id), [])
    if not warns:
        return await ctx.send("❌ No warnings found.")

    if index.lower() == "last":
        removed = warns.pop()
    else:
        try:
            i = int(index) - 1
            if i < 0 or i >= len(warns):
                return await ctx.send("❌ Invalid number.")
            removed = warns.pop(i)
        except ValueError:
            return await ctx.send("❌ Use 'last' or a number.")

    if warns:
        warnings[str(user_id)] = warns
    else:
        warnings.pop(str(user_id), None)

    await save_json(WARNINGS_FILE, warnings)

    await log_mod_action(
        action="Remove Warning",
        target=f"<@{user_id}>",
        reason=f"Removed: {removed['reason']}",
        moderator=str(ctx.author)
    )

    await ctx.send(f"🗑️ Removed: {removed['reason']}")


# ----- CLEAR WARNINGS (PREFIX) -----
@bot.command(name="clearwarnings")
async def clearwarnings(ctx, user_id: int):
    if ctx.guild.id != GUILD_ID or not is_admin(ctx):
        return await ctx.send("❌ Admins only.")

    warnings.pop(str(user_id), None)
    await save_json(WARNINGS_FILE, warnings)

    await log_mod_action(
        action="Clear Warnings",
        target=f"<@{user_id}>",
        reason="All warnings cleared",
        moderator=str(ctx.author)
    )

    await ctx.send(f"🧹 Cleared all warnings for <@{user_id}>.")

#=========================
# SLASH COMMANDS ADMINS
#=========================

# =========================
# TOP-LEVEL SLASH COMMANDS (USER & SERVER MODERATION)
# =========================

# ----- BAN USER -----
@bot.tree.command(name="ban_user", description="Ban a user")
@app_commands.guilds(discord.Object(id=GUILD_ID))
async def ban_user_slash(
    interaction: discord.Interaction,
    user_id: str,
    duration: str = "0",
    reason: str = "No reason"
):

    if interaction.guild.id != GUILD_ID or not can_moderate(interaction):
        return await interaction.response.send_message("❌ Mods/Admins only.")

    # validate duration
    duration_seconds = parse_duration(duration)
    if duration_seconds is None:
        return await interaction.response.send_message(
            "❌ Invalid duration.\nExamples: 10m, 1h, 1d"
        )

    # resolve user safely (GLOBAL FIX)
    if not user_id.isdigit():
        return await interaction.response.send_message("❌ Invalid user ID.")

    user = await find_shared_user(int(user_id))

    if user is None:
        try:
            user = await bot.fetch_user(int(user_id))
        except discord.NotFound:
            return await interaction.response.send_message("❌ User not found.")

    await ban_user_core(interaction, user, duration_seconds, reason)

    await log_mod_action(
        action="Ban User",
        target=f"{user} ({user.id})",
        reason=reason,
        moderator=str(interaction.user)
    )

    await interaction.response.send_message(f"✅ {user} has been banned.")


# ----- UNBAN USER -----
@bot.tree.command(name="unban_user")
@app_commands.guilds(discord.Object(id=GUILD_ID))
async def unban_user_slash(
    interaction: discord.Interaction,
    user_id: str,
    reason: str = "No reason"
):

    if interaction.guild.id != GUILD_ID or not can_moderate(interaction):
        return await interaction.response.send_message("❌ Mods/Admins only.")

    if not user_id.isdigit():
        return await interaction.response.send_message("❌ Invalid user ID.")

    user = await find_shared_user(int(user_id))

    if not user:
        try:
            user = await bot.fetch_user(int(user_id))
        except:
            return await interaction.response.send_message("❌ User not found.")

    success = await unban_user_core(interaction, user, reason)

    await log_mod_action(
        action="Unban User",
        target=f"{user} ({user.id})",
        reason=reason,
        moderator=str(interaction.user)
    )

    await interaction.response.send_message(
        "✅ Unbanned." if success else "❌ User not banned."
    )

# ----- BAN LIST -----

@bot.tree.command(name="banlist", description="View all banned users and servers")
@app_commands.guilds(discord.Object(id=GUILD_ID))
async def banlist_slash(interaction: discord.Interaction):

    if not can_moderate(interaction):
        return await interaction.response.send_message("❌ Mods/Admins only.")

    embed = discord.Embed(
        title="🚫 Ban List",
        color=0xE74C3C
    )

    # USERS
    if banned_users:
        user_text = ""

        for user_id, data in banned_users.items():
            expiry = data.get("expiry")
            reason = data.get("reason", "No reason")
            timestamp = data.get("timestamp", time.time())

            user = await find_shared_user(int(user_id))
            name = user.display_name if user else "Unknown User"

            user_text += (
                f"👤 **{name}** • `{user_id}`\n"
                f"• Reason: {reason}\n"
                f"• Duration: {format_duration(expiry)}\n"
                f"• Banned: {time_ago(timestamp)}\n\n"
            )

        embed.add_field(name="👤 User Bans", value=user_text[:1024], inline=False)
    else:
        embed.add_field(name="👤 User Bans", value="None", inline=False)

    # SERVERS
    if banned_servers:
        server_text = ""

        for guild_id, data in banned_servers.items():
            expiry = data.get("expiry")
            reason = data.get("reason", "No reason")
            timestamp = data.get("timestamp", time.time())

            guild = bot.get_guild(int(guild_id))
            name = guild.name if guild else "Unknown Server"

            server_text += (
                f"🏠 **{name}** • `{guild_id}`\n"
                f"• Reason: {reason}\n"
                f"• Duration: {format_duration(expiry)}\n"
                f"• Banned: {time_ago(timestamp)}\n\n"
            )

        embed.add_field(name="🏠 Server Bans", value=server_text[:1024], inline=False)
    else:
        embed.add_field(name="🏠 Server Bans", value="None", inline=False)

    await interaction.response.send_message(embed=embed)

# ----- WARN USER -----
@bot.tree.command(name="warn", description="Warn a user")
@app_commands.guilds(discord.Object(id=GUILD_ID))
async def warn_slash(
    interaction: discord.Interaction,
    user_id: str,
    reason: str = "No reason"
):

    try:
        user_id = int(user_id)
    except ValueError:
        return await interaction.response.send_message("❌ Invalid user ID.")

    if interaction.guild.id != GUILD_ID or not can_moderate(interaction):
        return await interaction.response.send_message("❌ Mods/Admins only.")

    user = await find_shared_user(user_id)

    if not user:
        return await interaction.response.send_message("❌ User not found or does not share a server with the bot.")

    banned = await warn_core(interaction, user, reason)

    await log_mod_action(
        action="Warn User",
        target=f"{user} ({user.id})",
        reason=reason,
        moderator=str(interaction.user)
    )

    if banned:
        await interaction.response.send_message("🚫 Auto-banned (3 warnings).")
    else:
        await interaction.response.send_message(f"⚠️ Warned <@{user_id}>.")

# ----- VIEW WARNINGS -----
@bot.tree.command(name="warnings", description="View a user's warnings")
@app_commands.guilds(discord.Object(id=GUILD_ID))
async def warnings_slash(interaction: discord.Interaction, user_id: str):

    try:
        user_id = int(user_id)
    except ValueError:
        return await interaction.response.send_message("❌ Invalid user ID.")

    if interaction.guild.id != GUILD_ID or not can_moderate(interaction):
        return await interaction.response.send_message("❌ Mods/Admins only.")

    warns = warnings.get(str(user_id), [])
    if not warns:
        return await interaction.response.send_message("✅ No warnings.")

    msg = "\n".join(
        f"{i}. {w['reason']} | by {w.get('moderator','Unknown')} | {time_ago(w.get('timestamp', time.time()))}"
        for i, w in enumerate(warns, 1)
    )
    await interaction.response.send_message(msg)


# ----- REMOVE WARNING -----
@bot.tree.command(name="removewarning", description="Remove a warning")
@app_commands.guilds(discord.Object(id=GUILD_ID))
async def removewarning_slash(interaction: discord.Interaction, user_id: str, index: str = "last"):

    try:
        user_id = int(user_id)
    except ValueError:
        return await interaction.response.send_message("❌ Invalid user ID.")

    if interaction.guild.id != GUILD_ID or not can_moderate(interaction):
        return await interaction.response.send_message("❌ Mods/Admins only.")

    warns = warnings.get(str(user_id), [])
    if not warns:
        return await interaction.response.send_message("❌ No warnings found.")

    if index.lower() == "last":
        removed = warns.pop()
    else:
        try:
            i = int(index) - 1
            if i < 0 or i >= len(warns):
                return await interaction.response.send_message("❌ Invalid number.")
            removed = warns.pop(i)
        except ValueError:
            return await interaction.response.send_message("❌ Use 'last' or a number.")

    if warns:
        warnings[str(user_id)] = warns
    else:
        warnings.pop(str(user_id), None)

    await save_json(WARNINGS_FILE, warnings)

    await log_mod_action(
        action="Remove Warning",
        target=f"<@{user_id}>",
        reason=f"Removed warning: {removed['reason']}",
        moderator=str(interaction.user)
    )

    await interaction.response.send_message(f"🗑️ Removed: {removed['reason']}")


# ----- CLEAR WARNINGS -----
@bot.tree.command(name="clearwarnings", description="Clear all warnings for a user")
@app_commands.guilds(discord.Object(id=GUILD_ID))
async def clearwarnings_slash(interaction: discord.Interaction, user_id: str):

    try:
        user_id = int(user_id)
    except ValueError:
        return await interaction.response.send_message("❌ Invalid user ID.")

    if interaction.guild.id != GUILD_ID or not is_admin(interaction):
        return await interaction.response.send_message("❌ Admins only.")

    warnings.pop(str(user_id), None)
    await save_json(WARNINGS_FILE, warnings)

    await log_mod_action(
        action="Clear Warnings",
        target=f"<@{user_id}>",
        reason="All warnings cleared",
        moderator=str(interaction.user)
    )

    await interaction.response.send_message(f"🧹 Cleared warnings for <@{user_id}>.")


# ----- BAN SERVER -----
@bot.tree.command(name="ban_server", description="Ban a server globally")
@app_commands.guilds(discord.Object(id=GUILD_ID))
async def ban_server_slash(
    interaction: discord.Interaction,
    guild_id: str,
    duration: str = "0",
    reason: str = "No reason"
):

    if interaction.guild.id != GUILD_ID or not is_admin(interaction):
        return await interaction.response.send_message("❌ Admins only.")

    try:
        guild_obj_id = int(guild_id)
    except ValueError:
        return await interaction.response.send_message("❌ Invalid guild ID.")

    guild = bot.get_guild(guild_obj_id)

    if not guild:
        return await interaction.response.send_message("❌ Server not found or bot not in it.")

    duration_seconds = parse_duration(duration)

    if duration_seconds is None:
        return await interaction.response.send_message(
            "❌ Invalid duration.\nExamples: 10 minutes, 1 hour, 1 day"
        )

    await ban_server_core(interaction, guild, duration_seconds, reason)

    await log_mod_action(
        action="Ban Server",
        target=f"{guild.name} ({guild.id})",
        reason=reason,
        moderator=str(interaction.user)
    )

    await interaction.response.send_message(f"🚫 Server **{guild.name}** banned.")

# ----- UNBAN SERVER -----
@bot.tree.command(name="unban_server", description="Unban a server globally")
@app_commands.guilds(discord.Object(id=GUILD_ID))
async def unban_server_slash(interaction: discord.Interaction, guild_id: str, reason: str = "No reason"):
    if interaction.guild.id != GUILD_ID or not is_admin(interaction):
        return await interaction.response.send_message("❌ Admins only.")

    guild = bot.get_guild(int(guild_id))
    if not guild:
        return await interaction.response.send_message("❌ Server not found or bot not in it.")

    success = await unban_server_core(interaction, guild, reason)

    await log_mod_action(
        action="Unban Server",
        target=f"{guild.name} ({guild.id})",
        reason=reason,
        moderator=str(interaction.user)
    )

    await interaction.response.send_message("✅ Unbanned." if success else "❌ Server not banned.")

# =========================
# RUN
# =========================
bot.run(TOKEN)