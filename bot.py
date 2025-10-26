import os
import asyncio
import re
import sqlite3
from collections import defaultdict, deque
from datetime import datetime, timedelta

import discord
from discord.ext import commands, tasks
from discord import app_commands

# Load config from environment variables (see README)
TOKEN = os.getenv("DISCORD_TOKEN", "<YOUR_TOKEN_HERE>")
PREFIX = os.getenv("BOT_PREFIX", "!")
GUILD_WHITELIST = os.getenv("GUILD_WHITELIST", "")  # comma-separated guild IDs allowed (optional)

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True
intents.messages = True
intents.reactions = True
intents.presences = False

bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)

# --- Simple persistence (SQLite) ---
DB_PATH = os.path.join(os.path.dirname(__file__), "modbot.sqlite")
conn = sqlite3.connect(DB_PATH)
c = conn.cursor()
c.execute("""
CREATE TABLE IF NOT EXISTS warns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER,
    user_id INTEGER,
    moderator_id INTEGER,
    reason TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
""")
c.execute("""
CREATE TABLE IF NOT EXISTS automod_settings (
    guild_id INTEGER PRIMARY KEY,
    banned_words TEXT DEFAULT '',
    max_links INTEGER DEFAULT 3,
    anti_nuke_threshold INTEGER DEFAULT 3,
    anti_nuke_window INTEGER DEFAULT 10 -- seconds
)
""")
conn.commit()

# --- In-memory trackers for anti-nuke ---
# Track actions per executor: {guild_id: {executor_id: deque([timestamps])}}
action_trackers = defaultdict(lambda: defaultdict(deque))

# Helper DB functions
def get_automod_settings(guild_id):
    cur = conn.cursor()
    cur.execute("SELECT banned_words, max_links, anti_nuke_threshold, anti_nuke_window FROM automod_settings WHERE guild_id = ?", (guild_id,))
    row = cur.fetchone()
    if row:
        banned_words, max_links, threshold, window = row
        banned = banned_words.split(",") if banned_words else []
        return {"banned_words": banned, "max_links": int(max_links), "threshold": int(threshold), "window": int(window)}
    # defaults
    return {"banned_words": [], "max_links": 3, "threshold": 3, "window": 10}

def set_banned_words(guild_id, words_list):
    s = ",".join([w.strip().lower() for w in words_list])
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO automod_settings (guild_id, banned_words) VALUES (?, COALESCE((SELECT banned_words FROM automod_settings WHERE guild_id = ?), ?))", (guild_id, s, guild_id, s))
    conn.commit()

# --- Utility ---
def is_guild_allowed(guild):
    if not GUILD_WHITELIST:
        return True
    allowed = [int(x) for x in GUILD_WHITELIST.split(",") if x.strip()]
    return guild.id in allowed

async def try_remove_permissions(role_or_member):
    try:
        if isinstance(role_or_member, discord.Role):
            await role_or_member.edit(permissions=discord.Permissions.none(), reason="Anti-nuke automatic lockdown")
        elif isinstance(role_or_member, discord.Member):
            await role_or_member.edit(roles=[r for r in role_or_member.roles if r.is_default()], reason="Anti-nuke automatic lockdown")
    except Exception as e:
        print("Failed to remove permissions:", e)

# --- Events & Automod ---
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id: {bot.user.id})")
    check_action_queues.start()

@bot.event
async def on_message(message):
    if message.author.bot:
        return
    if not message.guild or not is_guild_allowed(message.guild):
        return

    settings = get_automod_settings(message.guild.id)
    content = message.content.lower()

    # 1) Banned words
    for bad in settings["banned_words"]:
        if bad and bad in content:
            try:
                await message.delete()
            except:
                pass
            await message.channel.send(f"{message.author.mention} — your message was removed (prohibited word).")
            return

    # 2) Link flood
    links = re.findall(r"https?://\S+", message.content)
    if len(links) > settings["max_links"]:
        try:
            await message.delete()
        except:
            pass
        await message.channel.send(f"{message.author.mention} — too many links in a single message.")
        return

    await bot.process_commands(message)

# Anti-nuke tracker helper
def record_action(guild_id, user_id):
    now = datetime.utcnow()
    dq = action_trackers[guild_id][user_id]
    dq.append(now)
    # don't let deque grow unbounded: pop older than window (we will prune in task)
    return dq

@tasks.loop(seconds=3)
async def check_action_queues():
    # prune deques older than longest window ~ 5 minutes
    cutoff = datetime.utcnow() - timedelta(minutes=5)
    for guild_id, m in list(action_trackers.items()):
        for user_id, dq in list(m.items()):
            while dq and dq[0] < cutoff:
                dq.popleft()

# Helper to audit logs to find executor
async def find_audit_executor(guild, action_type, target):
    # action_type: "role_delete" | "channel_delete" | "member_ban" etc.
    try:
        async for entry in guild.audit_logs(limit=25):
            if action_type == "role_delete" and entry.action == discord.AuditLogAction.role_delete:
                return entry.user
            if action_type == "channel_delete" and entry.action == discord.AuditLogAction.channel_delete:
                return entry.user
            if action_type == "role_update" and entry.action == discord.AuditLogAction.role_update:
                return entry.user
            if action_type == "member_ban" and entry.action == discord.AuditLogAction.ban:
                return entry.user
    except Exception as e:
        print("Audit log error:", e)
    return None

# Watch for destructive actions
@bot.event
async def on_guild_role_delete(role):
    guild = role.guild
    if not is_guild_allowed(guild):
        return
    executor = await find_audit_executor(guild, "role_delete", role)
    if not executor:
        return
    dq = record_action(guild.id, executor.id)
    settings = get_automod_settings(guild.id)
    # prune by window
    window = timedelta(seconds=settings["window"])
    now = datetime.utcnow()
    while dq and dq[0] < now - window:
        dq.popleft()
    if len(dq) >= settings["threshold"]:
        # trigger anti-nuke actions
        await guild.system_channel.send(f"Anti-nuke: high rate of role deletions detected from {executor} — taking action.")
        try:
            await guild.ban(executor, reason="Detected mass destructive behaviour (auto anti-nuke)", delete_message_days=0)
        except Exception as e:
            print("Could not ban executor:", e)
        # lockdown: remove admin perms from @everyone role
        try:
            everyone = guild.default_role
            await everyone.edit(permissions=discord.Permissions.none(), reason="Auto-lockdown after suspected raid")
        except Exception as e:
            print("Error locking down:", e)

@bot.event
async def on_guild_channel_delete(channel):
    guild = channel.guild
    if not is_guild_allowed(guild):
        return
    executor = await find_audit_executor(guild, "channel_delete", channel)
    if not executor:
        return
    dq = record_action(guild.id, executor.id)
    settings = get_automod_settings(guild.id)
    window = timedelta(seconds=settings["window"])
    now = datetime.utcnow()
    while dq and dq[0] < now - window:
        dq.popleft()
    if len(dq) >= settings["threshold"]:
        await guild.system_channel.send(f"Anti-nuke: high rate of channel deletions detected from {executor} — taking action.")
        try:
            await guild.ban(executor, reason="Detected mass destructive behaviour (auto anti-nuke)", delete_message_days=0)
        except Exception as e:
            print("Could not ban executor:", e)
        try:
            everyone = guild.default_role
            await everyone.edit(permissions=discord.Permissions.none(), reason="Auto-lockdown after suspected raid")
        except Exception as e:
            print("Error locking down:", e)

# --- Moderation Commands ---
@commands.has_permissions(kick_members=True)
@bot.command(name="kick")
async def cmd_kick(ctx, member: discord.Member, *, reason: str = "No reason provided"):
    await ctx.guild.kick(member, reason=reason)
    await ctx.send(f"{member} has been kicked. Reason: {reason}")

@commands.has_permissions(ban_members=True)
@bot.command(name="ban")
async def cmd_ban(ctx, member: discord.Member, *, reason: str = "No reason provided"):
    await ctx.guild.ban(member, reason=reason, delete_message_days=0)
    await ctx.send(f"{member} has been banned. Reason: {reason}")

@commands.has_permissions(ban_members=True)
@bot.command(name="unban")
async def cmd_unban(ctx, user: discord.User):
    try:
        await ctx.guild.unban(user)
        await ctx.send(f"Unbanned {user}")
    except Exception as e:
        await ctx.send(f"Failed to unban: {e}")

@commands.has_permissions(manage_messages=True)
@bot.command(name="purge")
async def cmd_purge(ctx, limit: int = 10):
    deleted = await ctx.channel.purge(limit=limit)
    await ctx.send(f"Deleted {len(deleted)} messages.", delete_after=5)

@commands.has_permissions(manage_roles=True)
@bot.command(name="setbannedwords")
async def cmd_set_banned_words(ctx, *, words: str):
    words_list = [w.strip() for w in words.split(",") if w.strip()]
    cur = conn.cursor()
    joined = ",".join(words_list)
    cur.execute("INSERT OR REPLACE INTO automod_settings (guild_id, banned_words) VALUES (?, ?)", (ctx.guild.id, joined))
    conn.commit()
    await ctx.send(f"Saved banned words: {', '.join(words_list)}")

@commands.has_permissions(manage_messages=True)
@bot.command(name="warn")
async def cmd_warn(ctx, member: discord.Member, *, reason: str = "No reason provided"):
    cur = conn.cursor()
    cur.execute("INSERT INTO warns (guild_id, user_id, moderator_id, reason) VALUES (?, ?, ?, ?)", (ctx.guild.id, member.id, ctx.author.id, reason))
    conn.commit()
    await ctx.send(f"{member.mention} has been warned. Reason: {reason}")

@commands.has_permissions(administrator=True)
@bot.command(name="setantithreshold")
async def cmd_set_anti_threshold(ctx, threshold: int = 3, window_seconds: int = 10):
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO automod_settings (guild_id, anti_nuke_threshold, anti_nuke_window) VALUES (?, ?, ?)", (ctx.guild.id, threshold, window_seconds))
    conn.commit()
    await ctx.send(f"Anti-nuke threshold set to {threshold} actions in {window_seconds} seconds.")

@bot.command(name="modhelp")
async def cmd_help(ctx):
    em = discord.Embed(title="ModBot Commands", color=discord.Color.blue())
    em.add_field(name=f"{PREFIX}kick @member", value="Kick a member", inline=False)
    em.add_field(name=f"{PREFIX}ban @member", value="Ban a member", inline=False)
    em.add_field(name=f"{PREFIX}purge <count>", value="Delete messages", inline=False)
    em.add_field(name=f"{PREFIX}setbannedwords a,b,c", value="Set banned words (comma separated)", inline=False)
    em.add_field(name=f"{PREFIX}setantithreshold <count> <seconds>", value="Set anti-nuke sensitivity", inline=False)
    await ctx.send(embed=em)

# Run
if __name__ == "__main__":
    bot.run(TOKEN)