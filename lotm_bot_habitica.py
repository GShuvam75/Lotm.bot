import os
import asyncio
import asyncpg  # NEW: install with `pip install asyncpg`
from aiohttp import web
import json
import logging
from typing import Optional
import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

# ---------- CONFIG ----------
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "REPLACE_WITH_YOUR_TOKEN")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "supersecret")
WEB_HOST = os.getenv("WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.getenv("WEB_PORT", "8080"))

# PostgreSQL connection (Railway provides this)
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://user:password@localhost/lotm")

# XP mapping (can be changed with !setxp)
DEFAULT_XP_MAP = {
    "habit": {"trivial": 5, "easy": 7, "medium": 20, "hard": 30},
    "daily": {"trivial": 5, "easy": 10, "medium": 25, "hard": 50},
    "todo":  {"trivial": 5, "easy": 15, "medium": 50, "hard": 100},
}

DEFAULT_SEQUENCE_THRESHOLDS = {
    9: 900, 8: 1100, 7: 1500, 6: 1800, 5: 2400,
    4: 3200, 3: 4200, 2: 5500, 1: 7000, 0: 10000, -1: 50000
}

MAX_SEQUENCE = 9
MIN_SEQUENCE = -1
NUM_PATHWAYS = 22

logger = logging.getLogger("lotm")
logging.basicConfig(level=logging.INFO)

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ---------- GLOBAL DB POOL ----------
db_pool = None

async def init_db():
    """Initialize PostgreSQL connection pool and create tables."""
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=5, max_size=10)
    
    async with db_pool.acquire() as conn:
        await conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            discord_id TEXT PRIMARY KEY,
            xp INTEGER NOT NULL DEFAULT 0,
            pathway INTEGER NOT NULL DEFAULT 1,
            sequence INTEGER NOT NULL DEFAULT 9
        );

        CREATE TABLE IF NOT EXISTS pathway_role_map (
            guild_id BIGINT,
            pathway INTEGER,
            role_id BIGINT,
            PRIMARY KEY (guild_id, pathway)
        );

        CREATE TABLE IF NOT EXISTS sequence_role_map (
            guild_id BIGINT,
            pathway INTEGER,
            sequence INTEGER,
            role_id BIGINT,
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
            role_id BIGINT,
            PRIMARY KEY (pathway, sequence)
        );

        CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        """)

        # Populate defaults (first time only)
        for t, m in DEFAULT_XP_MAP.items():
            for d, xp in m.items():
                await conn.execute("""
                    INSERT INTO xp_map (task_type, difficulty, xp)
                    VALUES ($1, $2, $3)
                    ON CONFLICT DO NOTHING
                """, t, d, xp)

        for seq, xp_req in DEFAULT_SEQUENCE_THRESHOLDS.items():
            await conn.execute("""
                INSERT INTO sequence_thresholds (sequence, xp_required)
                VALUES ($1, $2)
                ON CONFLICT DO NOTHING
            """, seq, xp_req)

# ---------- DB HELPERS (PostgreSQL versions) ----------
async def get_config_value(key: str) -> Optional[str]:
    async with db_pool.acquire() as conn:
        result = await conn.fetchval("SELECT value FROM config WHERE key = $1", key)
        return result

async def set_config_value(key: str, value: str):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO config (key, value) VALUES ($1, $2) ON CONFLICT (key) DO UPDATE SET value = $2",
            key, value
        )

async def get_xp_for(task_type: str, difficulty: str) -> int:
    async with db_pool.acquire() as conn:
        result = await conn.fetchval(
            "SELECT xp FROM xp_map WHERE task_type = $1 AND difficulty = $2",
            task_type, difficulty
        )
        return int(result) if result else 0

async def set_pathway_role(guild_id: int, pathway: int, role_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO pathway_role_map (guild_id, pathway, role_id) VALUES ($1, $2, $3) ON CONFLICT (guild_id, pathway) DO UPDATE SET role_id = $3",
            guild_id, pathway, role_id
        )

async def get_pathway_role(guild_id: int, pathway: int) -> Optional[int]:
    async with db_pool.acquire() as conn:
        result = await conn.fetchval(
            "SELECT role_id FROM pathway_role_map WHERE guild_id = $1 AND pathway = $2",
            guild_id, pathway
        )
        return int(result) if result else None

async def get_user(discord_id: str) -> Optional[dict]:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT xp, pathway, sequence FROM users WHERE discord_id = $1",
            discord_id
        )
        if not row:
            return None
        return {"xp": int(row['xp']), "pathway": int(row['pathway']), "sequence": int(row['sequence'])}

async def set_user(discord_id: str, xp: int, pathway: int, sequence: int):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (discord_id, xp, pathway, sequence) VALUES ($1, $2, $3, $4) ON CONFLICT (discord_id) DO UPDATE SET xp = $2, pathway = $3, sequence = $4",
            discord_id, xp, pathway, sequence
        )

async def add_xp(discord_id: str, xp_change: int) -> dict:
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO users (discord_id) VALUES ($1) ON CONFLICT DO NOTHING", discord_id)
        await conn.execute("UPDATE users SET xp = xp + $1 WHERE discord_id = $2", xp_change, discord_id)
        row = await conn.fetchrow("SELECT xp, pathway, sequence FROM users WHERE discord_id = $1", discord_id)
        return {"xp": int(row['xp']), "pathway": int(row['pathway']), "sequence": int(row['sequence'])}

async def get_threshold(sequence: int) -> int:
    async with db_pool.acquire() as conn:
        result = await conn.fetchval("SELECT xp_required FROM sequence_thresholds WHERE sequence = $1", sequence)
        return int(result) if result else DEFAULT_SEQUENCE_THRESHOLDS.get(sequence, 1000)

async def set_threshold(sequence: int, xp_required: int):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO sequence_thresholds (sequence, xp_required) VALUES ($1, $2) ON CONFLICT (sequence) DO UPDATE SET xp_required = $2",
            sequence, xp_required
        )

async def link_habitica(hid: str, discord_id: str):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO habitica_link (habitica_user_id, discord_id) VALUES ($1, $2) ON CONFLICT (habitica_user_id) DO UPDATE SET discord_id = $2",
            hid, discord_id
        )

async def resolve_habitica(hid: str) -> Optional[str]:
    async with db_pool.acquire() as conn:
        result = await conn.fetchval("SELECT discord_id FROM habitica_link WHERE habitica_user_id = $1", hid)
        return result

async def get_role(pathway: int, sequence: int) -> Optional[int]:
    async with db_pool.acquire() as conn:
        result = await conn.fetchval(
            "SELECT role_id FROM role_map WHERE pathway = $1 AND sequence = $2",
            pathway, sequence
        )
        return int(result) if result else None

async def map_role(pathway: int, sequence: int, role_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO role_map (pathway, sequence, role_id) VALUES ($1, $2, $3) ON CONFLICT (pathway, sequence) DO UPDATE SET role_id = $3",
            pathway, sequence, role_id
        )

# ---------- PROMOTION LOGIC ----------
async def apply_promotions(discord_id: str):
    """Re-run promotion logic for a single user based on current XP."""
    user = await get_user(discord_id)
    if not user:
        return user

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
            user = await get_user(discord_id)
        else:
            break

    return user

# ========== ROLE SYNC HELPER ==========
async def sync_user_roles(discord_id: str, new_sequence: int):
    """Remove all old sequence roles and add the new sequence role."""
    user = await get_user(discord_id)
    if not user:
        logger.warning(f"sync_user_roles: User {discord_id} not found in DB")
        return

    pathway = user["pathway"]

    for guild in bot.guilds:
        member = guild.get_member(int(discord_id))
        if not member:
            try:
                member = await guild.fetch_member(int(discord_id))
            except discord.NotFound:
                continue
            except discord.HTTPException:
                continue

        # Remove ALL old sequence roles for this pathway
        for seq in range(MIN_SEQUENCE, MAX_SEQUENCE + 1):
            role_id = await get_role(pathway, seq)
            if not role_id:
                continue
            role = guild.get_role(role_id)
            if role and role in member.roles:
                await member.remove_roles(role, reason="Sequence change")
                logger.info(f"Removed role {role.name} from {member.id}")

        # Add new role for the new sequence
        new_role_id = await get_role(pathway, new_sequence)
        if new_role_id:
            new_role = guild.get_role(new_role_id)
            if new_role:
                await member.add_roles(new_role, reason="Sequence change")
                logger.info(f"Added role {new_role.name} to {member.id}")

# ---------- DIFFICULTY CONVERSION ----------
def priority_to_difficulty(priority: float) -> str:
    if priority <= 1:
        return "trivial"
    elif priority <= 1.5:
        return "easy"
    elif priority <= 2:
        return "medium"
    return "hard"

# ========== WEBHOOK HANDLER ==========
async def handle_habitica(request: web.Request):
    """Webhook handler for Habitica updates."""
    try:
        data = await request.json()
        logger.info(f"RAW WEBHOOK DATA: {json.dumps(data)[:500]}")
    except Exception as e:
        logger.error(f"Error parsing webhook JSON: {e}")
        return web.Response(status=400, text="Invalid JSON")

    task = data.get("task", {})
    user_id = task.get("userId") 
    direction = data.get("direction")
    
    logger.info(f"Webhook received: user_id={user_id}, direction={direction}")
    
    if not user_id or not task:
        return web.Response(status=400, text="Invalid Webhook")

    discord_id = await resolve_habitica(user_id)
    if not discord_id:
        return web.Response(status=404, text="Habitica user not linked")

    task_type = task.get("type")
    priority = float(task.get("priority", 1))
    difficulty = priority_to_difficulty(priority)

    xp = await get_xp_for(task_type, difficulty)
    if direction == "down":
        xp = -abs(xp)

    result = await add_xp(discord_id, xp)
    announce_id = await get_config_value("announce_channel_id")
    announcement = f"<@{discord_id}> {'gained' if xp>0 else 'lost'} {abs(xp)} XP ({task_type}, {difficulty})"

    if announce_id:
        channel = bot.get_channel(int(announce_id))
        if channel:
            await channel.send(announcement)

    user = await get_user(discord_id)
    old_sequence = user["sequence"]

    # DEMOTION
    if user["xp"] < 0:
        new_seq = min(old_sequence + 1, MAX_SEQUENCE)
        await set_user(discord_id, 0, user["pathway"], new_seq)
        await sync_user_roles(discord_id, new_seq)

        if announce_id:
            channel = bot.get_channel(int(announce_id))
            if channel:
                await channel.send(f"<@{discord_id}> has been demoted ({old_sequence} → {new_seq}).")

        user = await get_user(discord_id)

    # PROMOTION LOOP
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

            if announce_id:
                channel = bot.get_channel(int(announce_id))
                if channel:
                    await channel.send(f"<@{discord_id}> advanced from sequence {seq} → {new_seq}!")

            user = await get_user(discord_id)
        else:
            break

    await sync_user_roles(discord_id, user["sequence"])

    return web.json_response({"ok": True, "xp": xp, "leveled": leveled})

# ---------- Webserver ----------
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
async def setuserxp(ctx, member: discord.Member, xp: int):
    discord_id = str(member.id)
    u = await get_user(discord_id)
    if not u:
        await set_user(discord_id, xp, 1, MAX_SEQUENCE)
    else:
        await set_user(discord_id, xp, u["pathway"], u["sequence"])

    u = await apply_promotions(discord_id)
    await sync_user_roles(discord_id, u["sequence"])

    await ctx.send(f"Set {member.mention}'s XP to {u['xp']}. Sequence is now {u['sequence']}.")

@bot.command()
@is_admin()
async def addxp(ctx, member: discord.Member, amount: int):
    discord_id = str(member.id)
    u = await get_user(discord_id)
    if not u:
        await set_user(discord_id, amount, 1, MAX_SEQUENCE)
    else:
        new_xp = u["xp"] + amount
        await set_user(discord_id, new_xp, u["pathway"], u["sequence"])

    u = await apply_promotions(discord_id)
    await sync_user_roles(discord_id, u["sequence"])

    await ctx.send(f"Added {amount} XP to {member.mention}. XP: {u['xp']}, Sequence: {u['sequence']}.")

@bot.command()
@is_admin()
async def subtractxp(ctx, member: discord.Member, amount: int):
    discord_id = str(member.id)
    u = await get_user(discord_id)

    if not u:
        await set_user(discord_id, 0, 1, MAX_SEQUENCE)
        u = await get_user(discord_id)

    new_xp = u["xp"] - amount

    if new_xp < 0:
        new_xp = 0
        new_seq = min(u["sequence"] + 1, MAX_SEQUENCE)
        await set_user(discord_id, new_xp, u["pathway"], new_seq)
    else:
        await set_user(discord_id, new_xp, u["pathway"], u["sequence"])

    u = await apply_promotions(discord_id)
    await sync_user_roles(discord_id, u["sequence"])

    await ctx.send(f"Subtracted {amount} XP from {member.mention}. XP: {u['xp']}, Sequence: {u['sequence']}.")

@bot.command()
@is_admin()
async def setxp(ctx, task_type: str, difficulty: str, xp: int):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO xp_map (task_type, difficulty, xp) VALUES ($1, $2, $3) ON CONFLICT (task_type, difficulty) DO UPDATE SET xp = $3",
            task_type, difficulty, xp
        )
    await ctx.send("XP updated.")

@bot.command()
@is_admin()
async def setthreshold(ctx, sequence: int, xp_required: int):
    await set_threshold(sequence, xp_required)
    await ctx.send("Threshold updated.")

@bot.command()
@is_admin()
async def setpathwayrole(ctx, pathway: int, role: discord.Role):
    if pathway < 1 or pathway > NUM_PATHWAYS:
        await ctx.send(f"Pathway must be between 1 and {NUM_PATHWAYS}.")
        return
    await set_pathway_role(ctx.guild.id, pathway, role.id)
    await ctx.send(f"Pathway {pathway} → {role.mention}")

@bot.command()
@is_admin()
async def maprole(ctx, pathway: int, sequence: int, role: discord.Role):
    await map_role(pathway, sequence, role.id)
    await ctx.send(f"Pathway {pathway}, Sequence {sequence} → {role.mention}")

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

    guild = ctx.guild
    pathway_num = u["pathway"]
    seq_num = u["sequence"]

    pathway_label = f"{pathway_num}"
    pathway_role_id = await get_pathway_role(guild.id, pathway_num)
    if pathway_role_id:
        r = guild.get_role(pathway_role_id)
        if r:
            pathway_label = r.name

    await ctx.send(f"{m.mention} → XP: {u['xp']}, Pathway: {pathway_label}, Sequence: {seq_num}")

@bot.command()
async def leaderboard(ctx, top: int = 10):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT discord_id, xp FROM users ORDER BY xp DESC LIMIT $1", top)
    if not rows:
        await ctx.send("No data.")
        return
    text = "\n".join([f"{i+1}. <@{r['discord_id']}> – {r['xp']} XP" for i, r in enumerate(rows)])
    await ctx.send(text)

# ---------- MAIN ----------
async def main():
    await start_webserver()
    async with bot:
        await bot.start(DISCORD_TOKEN)

if __name__ == "__main__":
    asyncio.run(main())