import os
import re
import asyncio
import aiosqlite
from datetime import datetime, timedelta, timezone
from dateutil import parser as dtparser
import discord
from discord import app_commands

DB_PATH = "nukezone_actions.sqlite"

intents = discord.Intents.default()
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

# --------- Utilities ---------
DUR_RE = re.compile(r"(?:(?P<days>\d+)d)?\s*(?:(?P<hours>\d+)h)?\s*(?:(?P<mins>\d+)m)?\s*(?:(?P<secs>\d+)s)?", re.I)

def parse_duration_or_time(s: str, now_utc: datetime) -> datetime:
    """
    Accepts either a duration like '8h', '1d2h30m', '45m', or an absolute datetime like '2025-10-22 23:40'.
    Returns a UTC datetime for when it ends.
    """
    s = s.strip()
    # Try absolute time first
    try:
        dt = dtparser.parse(s)
        if dt.tzinfo is None:
            # assume local-ish → treat as UTC to keep it simple; adjust as needed
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt
    except Exception:
        pass

    # Try duration pattern
    m = DUR_RE.fullmatch(s.replace(" ", ""))
    if not m:
        raise ValueError("Could not parse duration. Try formats like '8h', '8h30m', '45m', '1d2h', or a datetime like '2025-10-22 23:40'.")
    days = int(m.group("days") or 0)
    hours = int(m.group("hours") or 0)
    mins = int(m.group("mins") or 0)
    secs = int(m.group("secs") or 0)
    delta = timedelta(days=days, hours=hours, minutes=mins, seconds=secs)
    if delta.total_seconds() <= 0:
        raise ValueError("Duration must be > 0.")
    return now_utc + delta

def fmt_dt(dt_utc: datetime) -> str:
    return dt_utc.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            action_type TEXT NOT NULL,
            target TEXT NOT NULL,
            note TEXT,
            created_at TEXT NOT NULL,
            ends_at TEXT NOT NULL,
            done INTEGER NOT NULL DEFAULT 0
        );
        """)
        await db.commit()

async def schedule_task(row: dict):
    # Create an asyncio task that sleeps until due, then posts the reminder
    now = datetime.now(timezone.utc)
    ends_at = datetime.fromisoformat(row["ends_at"])
    delay = (ends_at - now).total_seconds()
    if delay < 0:
        delay = 0

    async def worker():
        await asyncio.sleep(delay)
        # double-check not done/canceled
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM actions WHERE id=? AND done=0", (row["id"],))
            rec = await cur.fetchone()
            if not rec:
                return  # already done/canceled
            # mark done
            await db.execute("UPDATE actions SET done=1 WHERE id=?", (row["id"],))
            await db.commit()

        channel = bot.get_channel(rec["channel_id"])
        if channel is None:
            # Fallback: try fetch
            try:
                channel = await bot.fetch_channel(rec["channel_id"])
            except Exception:
                channel = None

        mention = f"<@{rec['user_id']}>"
        msg = (
            f"{mention} **NukeZone action complete!**\n"
            f"• **Type:** {rec['action_type']}\n"
            f"• **Target:** {rec['target']}\n"
            f"• **Finished:** {fmt_dt(datetime.fromisoformat(rec['ends_at']))}"
        )
        if rec["note"]:
            msg += f"\n• **Note:** {rec['note']}"

        if channel:
            try:
                await channel.send(msg)
            except Exception:
                pass

    asyncio.create_task(worker())

async def load_and_schedule_all():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM actions WHERE done=0")
        rows = await cur.fetchall()
        for r in rows:
            await schedule_task(dict(r))

# --------- Commands ---------
@tree.command(name="action_start", description="Start a NukeZone action timer.")
@app_commands.describe(
    action_type="e.g., 'Spy', 'Scout', 'Raid', 'Build'",
    target="Target name or identifier",
    duration="Examples: '8h', '1d2h30m', '45m', or '2025-10-22 23:40'",
    channel="Channel to ping when done (defaults to current channel)",
    note="Optional note (loadout, links, etc.)"
)
async def action_start(
    interaction: discord.Interaction,
    action_type: str,
    target: str,
    duration: str,
    channel: discord.TextChannel | None = None,
    note: str | None = None
):
    await interaction.response.defer(ephemeral=True)
    now = datetime.now(timezone.utc)
    try:
        ends_at = parse_duration_or_time(duration, now)
    except ValueError as e:
        return await interaction.followup.send(f"❌ {e}", ephemeral=True)

    channel_id = channel.id if channel else interaction.channel_id

    async with aiosqlite.connect(DB_PATH) as db:
        created_at = now.isoformat()
        ends_at_iso = ends_at.isoformat()
        await db.execute("""
            INSERT INTO actions (guild_id, user_id, channel_id, action_type, target, note, created_at, ends_at, done)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
        """, (interaction.guild_id, interaction.user.id, channel_id, action_type, target, note, created_at, ends_at_iso))
        await db.commit()
        cur = await db.execute("SELECT last_insert_rowid()")
        row_id = (await cur.fetchone())[0]

    # schedule the reminder
    await schedule_task({
        "id": row_id,
        "guild_id": interaction.guild_id,
        "user_id": interaction.user.id,
        "channel_id": channel_id,
        "action_type": action_type,
        "target": target,
        "note": note,
        "created_at": now.isoformat(),
        "ends_at": ends_at.isoformat(),
        "done": 0
    })

    await interaction.followup.send(
        f"✅ Timer set (ID `{row_id}`): **{action_type}** → **{target}**\n"
        f"• Ends: {fmt_dt(ends_at)}\n"
        f"• Ping channel: <#{channel_id}>\n"
        f"{'• Note: ' + note if note else ''}",
        ephemeral=True
    )

@tree.command(name="action_list", description="List your pending NukeZone action timers.")
async def action_list(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    now = datetime.now(timezone.utc)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT * FROM actions
            WHERE guild_id=? AND user_id=? AND done=0
            ORDER BY ends_at ASC
        """, (interaction.guild_id, interaction.user.id))
        rows = await cur.fetchall()

    if not rows:
        return await interaction.followup.send("You have no pending actions.", ephemeral=True)

    lines = []
    for r in rows:
        ends = datetime.fromisoformat(r["ends_at"])
        remaining = ends - now
        remain_str = f"{int(remaining.total_seconds()//3600)}h {int((remaining.total_seconds()%3600)//60)}m"
        lines.append(
            f"• ID `{r['id']}` — **{r['action_type']}** → **{r['target']}** | Ends: {fmt_dt(ends)} | ~{remain_str} | <#{r['channel_id']}>"
        )
    await interaction.followup.send("\n".join(lines), ephemeral=True)

@tree.command(name="action_cancel", description="Cancel a pending action timer by ID.")
@app_commands.describe(action_id="The ID shown in /action_list or returned when created.")
async def action_cancel(interaction: discord.Interaction, action_id: int):
    await interaction.response.defer(ephemeral=True)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            UPDATE actions SET done=1
            WHERE id=? AND user_id=? AND guild_id=? AND done=0
        """, (action_id, interaction.user.id, interaction.guild_id))
        await db.commit()
        if cur.rowcount == 0:
            return await interaction.followup.send("Could not cancel—check the ID or it may already be done.", ephemeral=True)

    await interaction.followup.send(f"🛑 Canceled timer `{action_id}`.", ephemeral=True)

# --------- Lifecycle ---------
@bot.event
async def on_ready():
    await init_db()
    await load_and_schedule_all()
    try:
        await tree.sync()
    except Exception:
        pass
    print(f"Logged in as {bot.user} (id: {bot.user.id})")

# --------- Entry ---------
if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit("Set DISCORD_TOKEN env var.")
    bot.run(token)
