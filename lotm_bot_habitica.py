import os
import asyncio
import aiosqlite
from aiohttp import web
import json
import logging
import secrets
from typing import Optional, Tuple, List
import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

# ---------- CONFIG ----------
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "REPLACE_WITH_YOUR_TOKEN")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "supersecret")
WEB_HOST = os.getenv("WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.getenv("WEB_PORT", "8080"))
DATABASE_PATH = os.getenv("DATABASE_PATH", "lotm.db")

# XP mapping (can be changed with !setxp)
DEFAULT_XP_MAP = {
    "habit": {"trivial": 5, "easy": 7, "medium": 20, "hard": 30},
    "daily": {"trivial": 5, "easy": 10, "medium": 25, "hard": 50},
    "todo":  {"trivial": 5, "easy": 15, "medium": 50, "hard": 100},
}

# Exponential thresholds (Option B)
DEFAULT_SEQUENCE_THRESHOLDS = {
    9: 900,
    8: 1100,
    7: 1500,
    6: 1800,
    5: 2400,
    4: 3200,
    3: 4200,
    2: 5500,
    1: 7000,
    0: 10000,
    -1: 50000
}

MAX_SEQUENCE = 9
MIN_SEQUENCE = -1  # ascended top
NUM_PATHWAYS = 22

# Logging
logger = logging.getLogger("lotm")
logging.basicConfig(level=logging.INFO)

# intents
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ---------- DATABASE INIT ----------
async def init_db():
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            discord_id TEXT PRIMARY KEY,
            xp INTEGER NOT NULL DEFAULT 0,
            pathway INTEGER NOT NULL DEFAULT 1,
            sequence INTEGER NOT NULL DEFAULT 9
        );

        CREATE TABLE IF NOT EXISTS pathway_role_map (
            guild_id INTEGER,
            pathway INTEGER,
            role_id INTEGER,
            PRIMARY KEY (guild_id, pathway)
        );

        CREATE TABLE IF NOT EXISTS sequence_role_map (
            guild_id INTEGER,
            pathway INTEGER,
            sequence INTEGER,
            role_id INTEGER,
            PRIMARY KEY (guild_id, pathway, sequence)
        );

        CREATE TABLE IF NOT EXISTS habitica_link (
            habitica_user_id TEXT PRIMARY KEY,
            discord_id TEXT
        );

        CREATE TABLE IF NOT EXISTS xp_map (
            task_type TEXT,
            difficulty TEXT,
            xp INTEGER,
            PRIMARY KEY (task_type, difficulty)
        );

        CREATE TABLE IF NOT EXISTS sequence_thresholds (
            sequence INTEGER PRIMARY KEY,
            xp_required INTEGER
        );

        CREATE TABLE IF NOT EXISTS role_map (
            pathway INTEGER,
            sequence INTEGER,
            role_id INTEGER,
            PRIMARY KEY (pathway, sequence)
        );

        CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        """)

        # Populate XP map (first time)
        for t, m in DEFAULT_XP_MAP.items():
            for d, xp in m.items():
                await db.execute("""
                    INSERT OR IGNORE INTO xp_map (task_type, difficulty, xp)
                    VALUES (?, ?, ?)
                """, (t, d, xp))

        # Populate thresholds
        for seq, xp_req in DEFAULT_SEQUENCE_THRESHOLDS.items():
            await db.execute("""
                INSERT OR IGNORE INTO sequence_thresholds (sequence, xp_required)
                VALUES (?, ?)
            """, (seq, xp_req))

        await db.commit()

# ---------- DB HELPERS ----------
async def get_config_value(key: str) -> Optional[str]:
    async with aiosqlite.connect(DATABASE_PATH) as db:  # âœ… FIXED: was DDATABASE_PATH
        cur = await db.execute("SELECT value FROM config WHERE key = ?", (key,))
        row = await cur.fetchone()
        return row[0] if row else None

async def set_config_value(key: str, value: str):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (key, value))
        await db.commit()

async def get_xp_for(task_type: str, difficulty: str) -> int:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cur = await db.execute("SELECT xp FROM xp_map WHERE task_type = ? AND difficulty = ?", (task_type, difficulty))
        row = await cur.fetchone()
        return int(row[0]) if row else 0

async def set_pathway_role(guild_id: int, pathway: int, role_id: int):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("""
        INSERT OR REPLACE INTO pathway_role_map (guild_id, pathway, role_id)
        VALUES (?, ?, ?)
        """, (guild_id, pathway, role_id))
        await db.commit()

async def get_pathway_role(guild_id: int, pathway: int) -> Optional[int]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cur = await db.execute("""
        SELECT role_id FROM pathway_role_map WHERE guild_id = ? AND pathway = ?
        """, (guild_id, pathway))
        row = await cur.fetchone()
        return int(row[0]) if row else None

async def set_xp_map(task_type: str, difficulty: str, xp: int):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("""
        INSERT OR REPLACE INTO xp_map (task_type, difficulty, xp)
        VALUES (?, ?, ?)
        """, (task_type, difficulty, xp))
        await db.commit()

async def get_threshold(sequence: int) -> int:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cur = await db.execute("SELECT xp_required FROM sequence_thresholds WHERE sequence = ?", (sequence,))
        r = await cur.fetchone()
        if r:
            return int(r[0])
        return DEFAULT_SEQUENCE_THRESHOLDS.get(sequence, 1000)

async def set_threshold(sequence: int, xp_required: int):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("""
        INSERT OR REPLACE INTO sequence_thresholds (sequence, xp_required)
        VALUES (?, ?)
        """, (sequence, xp_required))
        await db.commit()

async def link_habitica(hid: str, discord_id: str):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("""
        INSERT OR REPLACE INTO habitica_link (habitica_user_id, discord_id)
        VALUES (?, ?)
        """, (hid, discord_id))
        await db.commit()

async def resolve_habitica(hid: str) -> Optional[str]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cur = await db.execute("""
        SELECT discord_id FROM habitica_link WHERE habitica_user_id = ?
        """, (hid,))
        row = await cur.fetchone()
        return row[0] if row else None

async def get_user(discord_id: str) -> Optional[dict]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cur = await db.execute("""
        SELECT xp, pathway, sequence FROM users WHERE discord_id = ?
        """, (discord_id,))
        row = await cur.fetchone()
        if not row:
            return None
        return {"xp": int(row[0]), "pathway": int(row[1]), "sequence": int(row[2])}

async def set_user(discord_id: str, xp: int, pathway: int, sequence: int):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("""
        INSERT INTO users (discord_id, xp, pathway, sequence)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(discord_id)
        DO UPDATE SET xp=excluded.xp, pathway=excluded.pathway, sequence=excluded.sequence
        """, (discord_id, xp, pathway, sequence))
        await db.commit()

async def add_xp(discord_id: str, xp_change: int) -> dict:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO users (discord_id) VALUES (?)", (discord_id,))
        await db.execute("UPDATE users SET xp = xp + ? WHERE discord_id = ?", (xp_change, discord_id))
        await db.commit()
        cur = await db.execute("SELECT xp, pathway, sequence FROM users WHERE discord_id = ?", (discord_id,))
        row = await cur.fetchone()
        return {"xp": int(row[0]), "pathway": int(row[1]), "sequence": int(row[2])}

async def get_role(pathway: int, sequence: int) -> Optional[int]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cur = await db.execute("""
        SELECT role_id FROM role_map WHERE pathway = ? AND sequence = ?
        """, (pathway, sequence))
        row = await cur.fetchone()
        return int(row[0]) if row else None

async def map_role(pathway: int, sequence: int, role_id: int):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("""
        INSERT OR REPLACE INTO role_map (pathway, sequence, role_id)
        VALUES (?, ?, ?)
        """, (pathway, sequence, role_id))
        await db.commit()

# ---------- DIFFICULTY CONVERSION ----------
def priority_to_difficulty(priority: float) -> str:
    if priority <= 1:
        return "trivial"
    elif priority <= 1.5:
        return "easy"
    elif priority <= 2:
        return "medium"
    return "hard"

# ---------- WEBHOOK HANDLER (HABITICA ONLY) ----------
async def handle_habitica(request: web.Request):
    # Habitica webhooks do not send a secret header, so we skip validation
    
    try:
        data = await request.json()
        logger.info(f"RAW WEBHOOK DATA: {json.dumps(data)[:500]}")
    except Exception as e:
        logger.error(f"Error parsing webhook JSON: {e}")
        return web.Response(status=400, text="Invalid JSON")

    # Extract Habitica fields
    task = data.get("task", {})
    user_id = task.get("userId") 
    direction = data.get("direction")
    
    logger.info(f"Webhook received: user_id={user_id}, task={task}, direction={direction}")
    
    if not user_id or not task:
        logger.warning(f"Invalid webhook: missing user_id or task")
        return web.Response(status=400, text="Invalid Webhook")

    discord_id = await resolve_habitica(user_id)
    if not discord_id:
        logger.warning(f"Habitica user {user_id} not linked to Discord")
        return web.Response(status=404, text="Habitica user not linked")

    task_type = task.get("type")  # habit / daily / todo
    priority = float(task.get("priority", 1))
    difficulty = priority_to_difficulty(priority)

    xp = await get_xp_for(task_type, difficulty)
    if direction == "down":
        xp = -abs(xp)

    logger.info(f"Processing: {user_id} â†’ Discord {discord_id}, {xp} XP ({task_type}, {difficulty})")

    # Apply XP change
    result = await add_xp(discord_id, xp)
    announce_id = await get_config_value("announce_channel_id")
    announcement = f"<@{discord_id}> {'gained' if xp>0 else 'lost'} {abs(xp)} XP ({task_type}, {difficulty})"

    # Send XP announcement
    if announce_id:
        channel = bot.get_channel(int(announce_id))
        if channel:
            await channel.send(announcement)
            logger.info(f"Announcement sent to channel {announce_id}")
        else:
            logger.warning(f"Announce channel {announce_id} not found")
    else:
        logger.warning("No announce channel configured")

    # Fetch updated user
    user = await get_user(discord_id)

    # ---------- DEMOTION (Mode B) ----------
    if user["xp"] < 0:
        old_seq = user["sequence"]
        new_seq = min(old_seq + 1, MAX_SEQUENCE)
        await set_user(discord_id, 0, user["pathway"], new_seq)

        # Update roles
        for g in bot.guilds:
            member = g.get_member(int(discord_id))
            if member:
                old_role_id = await get_role(user["pathway"], old_seq)
                new_role_id = await get_role(user["pathway"], new_seq)
                if old_role_id:
                    old_role = g.get_role(old_role_id)
                    if old_role in member.roles:
                        await member.remove_roles(old_role, reason="Demotion")
                if new_role_id:
                    new_role = g.get_role(new_role_id)
                    await member.add_roles(new_role, reason="Demotion")

        if announce_id:
            channel = bot.get_channel(int(announce_id))
            if channel:
                await channel.send(f"<@{discord_id}> has been demoted ({old_seq} â†’ {new_seq}).")

        user = await get_user(discord_id)

    # ---------- PROMOTION LOOP ----------
    leveled = []
    while True:
        seq = user["sequence"]
        if seq <= MIN_SEQUENCE:
            break

        thresh = await get_threshold(seq)
        if user["xp"] >= thresh:
            user["xp"] -= thresh
            new_seq = seq - 1
            await set_user(discord_id, user["xp"], user["pathway"], new_seq)
            leveled.append((seq, new_seq))

            # Role update
            for g in bot.guilds:
                member = g.get_member(int(discord_id))
                if member:
                    old_role = await get_role(user["pathway"], seq)
                    new_role = await get_role(user["pathway"], new_seq)
                    if old_role:
                        r = g.get_role(old_role)
                        if r in member.roles:
                            await member.remove_roles(r)
                    if new_role:
                        await member.add_roles(g.get_role(new_role))

            if announce_id:
                ch = bot.get_channel(int(announce_id))
                if ch:
                    await ch.send(f"<@{discord_id}> advanced from {seq} â†’ {new_seq}!")

            user = await get_user(discord_id)
        else:
            break

    return web.json_response({"ok": True, "xp": xp, "leveled": leveled})

# ---------- Start aiohttp server ----------
async def start_webserver():
    app = web.Application()
    app.router.add_post("/webhook/habitica", handle_habitica)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, WEB_HOST, WEB_PORT)
    await site.start()
    logger.info(f"Webhook server running on {WEB_HOST}:{WEB_PORT}")

# ---------- BOT COMMANDS ----------
def is_admin():
    async def predicate(ctx):
        return ctx.author.guild_permissions.manage_guild or ctx.author.guild_permissions.administrator
    return commands.check(predicate)

@bot.event
async def on_ready():
    await init_db()
    logger.info("Database initialized")
    print("LOTMBot Ready.")

@bot.command()
@is_admin()
async def setannounce(ctx, ch: discord.TextChannel):
    await set_config_value("announce_channel_id", str(ch.id))
    await ctx.send(f"Announcements channel set to {ch.mention}")

@bot.command()
async def link(ctx, habitica_user_id: str):
    await link_habitica(habitica_user_id, str(ctx.author.id))
    await ctx.send("Habitica account linked.")

@bot.command()
@is_admin()
async def setxp(ctx, task_type: str, difficulty: str, xp: int):
    await set_xp_map(task_type, difficulty, xp)
    await ctx.send("XP updated.")

@bot.command()
@is_admin()
async def setthreshold(ctx, sequence: int, xp_required: int):
    await set_threshold(sequence, xp_required)
    await ctx.send("Threshold updated.")

# NEW: Map pathway role
@bot.command()
@is_admin()
async def setpathwayrole(ctx, pathway: int, role: discord.Role):
    """Set the pathway role for this guild. Usage: !setpathwayrole 1 @PathwayRole"""
    if pathway < 1 or pathway > NUM_PATHWAYS:
        await ctx.send(f"Pathway must be between 1 and {NUM_PATHWAYS}.")
        return
    await set_pathway_role(ctx.guild.id, pathway, role.id)
    await ctx.send(f"Pathway {pathway} → {role.mention}")

@bot.command()
@is_admin()
async def maprole(ctx, pathway: int, sequence: int, role: discord.Role):
    await map_role(pathway, sequence, role.id)
    await ctx.send("Role mapped.")

@bot.command()
@is_admin()
async def resetuser(ctx, member: discord.Member):
    await set_user(str(member.id), 0, 1, MAX_SEQUENCE)
    await ctx.send("User reset.")

@bot.command()
async def xp(ctx, member: Optional[discord.Member]):
    m = member or ctx.author
    u = await get_user(str(m.id))
    if not u:
        await ctx.send("No data.")
        return
    await ctx.send(f"{m.mention} â†’ XP: {u['xp']}, Pathway: {u['pathway']}, Sequence: {u['sequence']}")

@bot.command()
async def leaderboard(ctx, top: int = 10):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cur = await db.execute("""
        SELECT discord_id, xp FROM users ORDER BY xp DESC LIMIT ?
        """, (top,))
        rows = await cur.fetchall()
    if not rows:
        await ctx.send("No data.")
        return
    text = "\n".join([f"{i+1}. <@{r[0]}> â€” {r[1]} XP" for i, r in enumerate(rows)])
    await ctx.send(text)

# ---------- MAIN ASYNC RUNNER ----------
async def main():
    # Start webhook server
    await start_webserver()
    
    # Run Discord bot
    async with bot:
        await bot.start(DISCORD_TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
