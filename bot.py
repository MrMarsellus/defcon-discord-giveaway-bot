"""
Boticana / DeFCoN Discord Giveaway Bot
=======================================
Monitors a channel, stores posts in SQLite, and randomly draws winners.

Required: discord.py >= 2.3  |  Python >= 3.10
Installation: pip install "discord.py>=2.3" python-dotenv
"""

import asyncio
import logging
import os
import random
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

# ── Configuration ──────────────────────────────────────────────────────────────
load_dotenv()

BOT_TOKEN       = os.getenv("DISCORD_TOKEN", "")
ENTRY_CHANNEL   = int(os.getenv("ENTRY_CHANNEL_ID", "0"))   # Channel for entry posts
WINNER_CHANNEL  = int(os.getenv("WINNER_CHANNEL_ID", "0"))  # Channel for winner announcement
ADMIN_CHANNEL   = int(os.getenv("ADMIN_CHANNEL_ID", "0"))   # Channel for admin review
GUILD_ID        = int(os.getenv("GUILD_ID", "0"))
DB_PATH         = os.getenv("DB_PATH", "giveaway.db")
LOG_LEVEL       = os.getenv("LOG_LEVEL", "INFO")

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("giveaway_bot.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("giveaway_bot")

# ── Database ───────────────────────────────────────────────────────────────────
@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS giveaways (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL,
                channel_id  INTEGER NOT NULL,
                start_date  TEXT NOT NULL,   -- ISO-8601 UTC
                end_date    TEXT NOT NULL,   -- ISO-8601 UTC
                total_winners INTEGER NOT NULL DEFAULT 1,
                winners_drawn INTEGER NOT NULL DEFAULT 0,
                active      INTEGER NOT NULL DEFAULT 1,  -- 1 = running, 0 = ended
                created_at  TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS entries (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                giveaway_id     INTEGER NOT NULL REFERENCES giveaways(id),
                user_id         INTEGER NOT NULL,
                username        TEXT NOT NULL,
                message_id      INTEGER NOT NULL UNIQUE,
                message_url     TEXT NOT NULL,
                message_content TEXT,
                posted_at       TEXT NOT NULL,   -- ISO-8601 UTC
                status          TEXT NOT NULL DEFAULT 'pending',
                -- pending | winner | rejected | disqualified | ineligible
                created_at      TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_entries_giveaway ON entries(giveaway_id);
            CREATE INDEX IF NOT EXISTS idx_entries_user     ON entries(giveaway_id, user_id);

            CREATE TABLE IF NOT EXISTS winners (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                giveaway_id     INTEGER NOT NULL REFERENCES giveaways(id),
                user_id         INTEGER NOT NULL,
                entry_id        INTEGER NOT NULL REFERENCES entries(id),
                draw_round      INTEGER NOT NULL DEFAULT 1,
                status          TEXT NOT NULL DEFAULT 'pending',
                -- pending | approved | rejected
                review_msg_id   INTEGER,   -- Admin review message
                announce_msg_id INTEGER,   -- Announcement in the winner channel
                drawn_at        TEXT NOT NULL DEFAULT (datetime('now')),
                decided_at      TEXT
            );
        """)
    log.info("Database initialized: %s", DB_PATH)


# ── Helper functions ───────────────────────────────────────────────────────────
def get_active_giveaway(channel_id: int) -> Optional[sqlite3.Row]:
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM giveaways WHERE channel_id=? AND active=1 ORDER BY id DESC LIMIT 1",
            (channel_id,)
        ).fetchone()


def parse_dt(dt_str: str) -> datetime:
    """Expects 'DD.MM.YYYY HH:MM' (local input) → UTC datetime."""
    return datetime.strptime(dt_str, "%d.%m.%Y %H:%M").replace(tzinfo=timezone.utc)


def fmt_dt(dt: datetime) -> str:
    return dt.strftime("%d.%m.%Y %H:%M UTC")


def draw_winner(giveaway_id: int, already_won_users: list[int]) -> Optional[sqlite3.Row]:
    """
    Draws a winner weighted by number of entries.
    Excluded: users who already won + disqualified entries.
    """
    with get_db() as conn:
        rows = conn.execute("""
            SELECT user_id, username, id AS entry_id, message_url, message_content
            FROM entries
            WHERE giveaway_id=?
              AND status='pending'
        """, (giveaway_id,)).fetchall()

    # All pending entries → pool (one entry = one ticket)
    pool = [r for r in rows if r["user_id"] not in already_won_users]
    if not pool:
        return None
    chosen_entry = random.choice(pool)
    return chosen_entry


# ── Bot setup ──────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.messages         = True
intents.message_content  = True
intents.guilds           = True
intents.members          = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree


# ── Events ─────────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    log.info("Bot online as %s (ID %s)", bot.user, bot.user.id)
    guild = discord.Object(id=GUILD_ID)
    await tree.sync(guild=guild)
    log.info("Slash commands synchronized for Guild %s", GUILD_ID)
    check_giveaway_end.start()


@bot.event
async def on_message(message: discord.Message):
    """Stores every message in the entry channel in the DB."""
    if message.author.bot:
        return
    if message.channel.id != ENTRY_CHANNEL:
        return

    giveaway = get_active_giveaway(ENTRY_CHANNEL)
    if not giveaway:
        return

    # Only within the giveaway period
    now = datetime.now(timezone.utc)
    start = datetime.fromisoformat(giveaway["start_date"])
    end   = datetime.fromisoformat(giveaway["end_date"])
    if not (start <= now <= end):
        return

    msg_url = f"https://discord.com/channels/{message.guild.id}/{message.channel.id}/{message.id}"

    with get_db() as conn:
        existing = conn.execute(
            "SELECT id FROM entries WHERE message_id=?", (message.id,)
        ).fetchone()
        if existing:
            return
        conn.execute("""
            INSERT INTO entries (giveaway_id, user_id, username, message_id,
                                 message_url, message_content, posted_at)
            VALUES (?,?,?,?,?,?,?)
        """, (
            giveaway["id"],
            message.author.id,
            str(message.author),
            message.id,
            msg_url,
            message.content[:2000],
            now.isoformat(),
        ))
    log.debug("Entry saved: user=%s msg=%s", message.author, message.id)
    await bot.process_commands(message)


@bot.event
async def on_message_delete(message: discord.Message):
    """Marks deleted messages as disqualified."""
    if message.channel.id != ENTRY_CHANNEL:
        return
    with get_db() as conn:
        conn.execute(
            "UPDATE entries SET status='disqualified' WHERE message_id=?",
            (message.id,)
        )
    log.info("Message deleted → disqualified: msg=%s", message.id)


# ── Background Task: automatic end ─────────────────────────────────────────────
@tasks.loop(minutes=5)
async def check_giveaway_end():
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        ended = conn.execute(
            "SELECT * FROM giveaways WHERE active=1 AND end_date <= ?", (now,)
        ).fetchall()
    for gw in ended:
        ch = bot.get_channel(gw["channel_id"])
        admin_ch = bot.get_channel(ADMIN_CHANNEL)
        if admin_ch:
            await admin_ch.send(
                f"⏰ **Giveaway `{gw['name']}` has ended!**\n"
                f"Use `/draw giveaway_id:{gw['id']}` to draw the first winner."
            )
        with get_db() as conn:
            # Do NOT automatically close the giveaway – admin draws manually
            pass
        log.info("Giveaway %s (%s) ended", gw["id"], gw["name"])


# ── Slash Commands ─────────────────────────────────────────────────────────────
guild_obj = discord.Object(id=GUILD_ID)


@tree.command(name="giveaway_start", description="Starts a new giveaway", guild=guild_obj)
@app_commands.describe(
    name        = "Name / title of the giveaway",
    start       = "Start date and time (DD.MM.YYYY HH:MM)",
    end         = "End date and time (DD.MM.YYYY HH:MM)",
    winners     = "Number of winners (default: 1)",
    channel_id  = "Channel ID for entries (leave empty = use configured channel)",
)
@app_commands.checks.has_permissions(administrator=True)
async def giveaway_start(
    interaction: discord.Interaction,
    name: str,
    start: str,
    end: str,
    winners: int = 1,
    channel_id: str = "",
):
    await interaction.response.defer(ephemeral=True)
    try:
        start_dt = parse_dt(start)
        end_dt   = parse_dt(end)
    except ValueError:
        await interaction.followup.send("❌ Invalid date format. Please use `DD.MM.YYYY HH:MM`.", ephemeral=True)
        return

    if end_dt <= start_dt:
        await interaction.followup.send("❌ End date must be after the start date.", ephemeral=True)
        return
    if winners < 1:
        await interaction.followup.send("❌ At least 1 winner is required.", ephemeral=True)
        return

    ch_id = int(channel_id) if channel_id.strip() else ENTRY_CHANNEL

    # Check if there is already an active giveaway in this channel
    existing = get_active_giveaway(ch_id)
    if existing:
        await interaction.followup.send(
            f"❌ A giveaway is already running in <#{ch_id}>: **{existing['name']}** (ID {existing['id']}).\n"
            f"End it first with `/giveaway_end`.", ephemeral=True
        )
        return

    with get_db() as conn:
        cursor = conn.execute("""
            INSERT INTO giveaways (name, channel_id, start_date, end_date, total_winners)
            VALUES (?,?,?,?,?)
        """, (name, ch_id, start_dt.isoformat(), end_dt.isoformat(), winners))
        gw_id = cursor.lastrowid

    embed = discord.Embed(
        title="🎉 New giveaway started!",
        color=discord.Color.green(),
        description=f"**{name}**"
    )
    embed.add_field(name="Giveaway ID", value=str(gw_id), inline=True)
    embed.add_field(name="Channel",     value=f"<#{ch_id}>", inline=True)
    embed.add_field(name="Winners",     value=str(winners), inline=True)
    embed.add_field(name="Start",       value=fmt_dt(start_dt), inline=True)
    embed.add_field(name="End",         value=fmt_dt(end_dt),   inline=True)

    await interaction.followup.send(embed=embed, ephemeral=False)
    log.info("Giveaway started: ID=%s Name=%s", gw_id, name)


@tree.command(name="giveaway_end", description="Ends an active giveaway manually", guild=guild_obj)
@app_commands.describe(giveaway_id="Giveaway ID")
@app_commands.checks.has_permissions(administrator=True)
async def giveaway_end(interaction: discord.Interaction, giveaway_id: int):
    await interaction.response.defer(ephemeral=True)
    with get_db() as conn:
        gw = conn.execute("SELECT * FROM giveaways WHERE id=?", (giveaway_id,)).fetchone()
        if not gw:
            await interaction.followup.send("❌ Giveaway not found.", ephemeral=True)
            return
        conn.execute("UPDATE giveaways SET active=0 WHERE id=?", (giveaway_id,))
    await interaction.followup.send(f"✅ Giveaway **{gw['name']}** (ID {giveaway_id}) ended.", ephemeral=True)
    log.info("Giveaway ended: ID=%s", giveaway_id)


@tree.command(name="draw", description="Draws the next winner", guild=guild_obj)
@app_commands.describe(giveaway_id="Giveaway ID")
@app_commands.checks.has_permissions(administrator=True)
async def draw(interaction: discord.Interaction, giveaway_id: int):
    await interaction.response.defer(ephemeral=False)

    with get_db() as conn:
        gw = conn.execute("SELECT * FROM giveaways WHERE id=?", (giveaway_id,)).fetchone()
    if not gw:
        await interaction.followup.send("❌ Giveaway not found.")
        return

    # Determine users who already won
    with get_db() as conn:
        won_rows = conn.execute(
            "SELECT user_id FROM winners WHERE giveaway_id=? AND status='approved'",
            (giveaway_id,)
        ).fetchall()
        rejected_rows = conn.execute(
            "SELECT user_id FROM winners WHERE giveaway_id=? AND status='rejected'",
            (giveaway_id,)
        ).fetchall()
    won_users = [r["user_id"] for r in won_rows]
    rejected_users = [r["user_id"] for r in rejected_rows]

    if len(won_users) >= gw["total_winners"]:
        await interaction.followup.send(
            f"🏆 All {gw['total_winners']} winners have already been drawn and approved!"
        )
        return

    # Also exclude users already marked as rejected
    excluded = list(set(won_users + rejected_users))
    entry = draw_winner(giveaway_id, excluded)

    if not entry:
        await interaction.followup.send(
            "😔 No more eligible entries available. "
            "All participants have either already been selected, disqualified, or rejected."
        )
        return

    round_num = len(won_users) + len(rejected_users) + 1

    with get_db() as conn:
        cursor = conn.execute("""
            INSERT INTO winners (giveaway_id, user_id, entry_id, draw_round)
            VALUES (?,?,?,?)
        """, (giveaway_id, entry["user_id"], entry["entry_id"], round_num))
        winner_id = cursor.lastrowid

    # Admin review embed
    admin_ch = bot.get_channel(ADMIN_CHANNEL)
    embed = discord.Embed(
        title=f"🎲 Winner drawn – Round {round_num}",
        color=discord.Color.gold(),
        description=f"**Giveaway:** {gw['name']} (ID {giveaway_id})\n"
                    f"**Drawn:** {round_num} of {gw['total_winners']} winners"
    )
    embed.add_field(name="User",       value=f"<@{entry['user_id']}> ({entry['username']})", inline=False)
    embed.add_field(name="Post Link",  value=entry["message_url"], inline=False)
    embed.add_field(name="Content",    value=(entry["message_content"] or "_(no text)_")[:512], inline=False)
    embed.add_field(
        name="Action",
        value=f"Review the post and then use:\n"
              f"`/approve winner_id:{winner_id}` – eligible ✅\n"
              f"`/reject winner_id:{winner_id}` – not eligible ❌",
        inline=False
    )
    embed.set_footer(text=f"Winner DB ID: {winner_id} | Entry ID: {entry['entry_id']}")

    review_msg = None
    if admin_ch:
        review_msg = await admin_ch.send(embed=embed)
        with get_db() as conn:
            conn.execute(
                "UPDATE winners SET review_msg_id=? WHERE id=?",
                (review_msg.id, winner_id)
            )

    await interaction.followup.send(
        embed=embed if not admin_ch else discord.Embed(
            title="✅ Winner drawn",
            description=f"Review was posted in <#{ADMIN_CHANNEL}>.",
            color=discord.Color.blue()
        )
    )
    log.info("Winner drawn: winner_id=%s user=%s entry=%s", winner_id, entry["user_id"], entry["entry_id"])


@tree.command(name="approve", description="Approves a drawn winner", guild=guild_obj)
@app_commands.describe(
    winner_id="Winner ID (from /draw)",
    note="Optional note for the announcement"
)
@app_commands.checks.has_permissions(administrator=True)
async def approve(interaction: discord.Interaction, winner_id: int, note: str = ""):
    await interaction.response.defer(ephemeral=False)

    with get_db() as conn:
        w = conn.execute("""
            SELECT w.*, e.message_url, e.message_content, e.username,
                   g.name AS giveaway_name, g.total_winners, g.id AS giveaway_id
            FROM winners w
            JOIN entries e ON e.id = w.entry_id
            JOIN giveaways g ON g.id = w.giveaway_id
            WHERE w.id=?
        """, (winner_id,)).fetchone()

    if not w:
        await interaction.followup.send("❌ Winner ID not found.")
        return
    if w["status"] != "pending":
        await interaction.followup.send(f"⚠️ Status is already `{w['status']}` – no change.")
        return

    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            "UPDATE winners SET status='approved', decided_at=? WHERE id=?",
            (now, winner_id)
        )
        conn.execute(
            "UPDATE entries SET status='winner' WHERE id=?",
            (w["entry_id"],)
        )
        conn.execute(
            "UPDATE giveaways SET winners_drawn=winners_drawn+1 WHERE id=?",
            (w["giveaway_id"],)
        )

    # Winner announcement
    winner_ch = bot.get_channel(WINNER_CHANNEL)
    announce_embed = discord.Embed(
        title="🏆 Winner approved!",
        color=discord.Color.gold(),
        description=f"**{w['giveaway_name']}**"
    )
    announce_embed.add_field(name="🎉 Winner", value=f"<@{w['user_id']}>", inline=False)
    announce_embed.add_field(name="Winning Post", value=w["message_url"], inline=False)
    if note:
        announce_embed.add_field(name="Note", value=note, inline=False)

    # Check if all winners have been drawn
    with get_db() as conn:
        gw = conn.execute("SELECT * FROM giveaways WHERE id=?", (w["giveaway_id"],)).fetchone()

    remaining = gw["total_winners"] - gw["winners_drawn"] - 1  # -1 because not yet committed
    # Simplification: after commit
    with get_db() as conn:
        gw = conn.execute("SELECT * FROM giveaways WHERE id=?", (w["giveaway_id"],)).fetchone()
    remaining = gw["total_winners"] - gw["winners_drawn"]

    if remaining > 0:
        announce_embed.set_footer(text=f"{remaining} winner(s) still pending. Next draw: /draw giveaway_id:{w['giveaway_id']}")
    else:
        announce_embed.set_footer(text="All winners have been determined! 🎊")
        with get_db() as conn:
            conn.execute("UPDATE giveaways SET active=0 WHERE id=?", (w["giveaway_id"],))

    announce_msg = None
    if winner_ch:
        announce_msg = await winner_ch.send(f"<@{w['user_id']}>", embed=announce_embed)
        with get_db() as conn:
            conn.execute(
                "UPDATE winners SET announce_msg_id=? WHERE id=?",
                (announce_msg.id, winner_id)
            )

    await interaction.followup.send(
        f"✅ <@{w['user_id']}> was approved as winner and announced in <#{WINNER_CHANNEL}>!\n"
        + (f"**{remaining}** winner(s) still pending. Use `/draw giveaway_id:{w['giveaway_id']}`" if remaining > 0 else "🎊 All winners determined!")
    )
    log.info("Winner approved: winner_id=%s user=%s", winner_id, w["user_id"])


@tree.command(name="reject", description="Rejects a drawn winner and allows a redraw", guild=guild_obj)
@app_commands.describe(
    winner_id="Winner ID (from /draw)",
    reason="Reason for rejection"
)
@app_commands.checks.has_permissions(administrator=True)
async def reject(interaction: discord.Interaction, winner_id: int, reason: str = "Conditions not met"):
    await interaction.response.defer(ephemeral=False)

    with get_db() as conn:
        w = conn.execute("""
            SELECT w.*, e.username, g.name AS giveaway_name, g.id AS giveaway_id
            FROM winners w
            JOIN entries e ON e.id = w.entry_id
            JOIN giveaways g ON g.id = w.giveaway_id
            WHERE w.id=?
        """, (winner_id,)).fetchone()

    if not w:
        await interaction.followup.send("❌ Winner ID not found.")
        return
    if w["status"] != "pending":
        await interaction.followup.send(f"⚠️ Status is already `{w['status']}` – no change.")
        return

    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            "UPDATE winners SET status='rejected', decided_at=? WHERE id=?",
            (now, winner_id)
        )
        conn.execute(
            "UPDATE entries SET status='ineligible' WHERE id=?",
            (w["entry_id"],)
        )

    await interaction.followup.send(
        f"❌ <@{w['user_id']}> ({w['username']}) was rejected.\n"
        f"**Reason:** {reason}\n"
        f"The user will be excluded from the next draw.\n"
        f"Use `/draw giveaway_id:{w['giveaway_id']}` for the next draw attempt."
    )
    log.info("Winner rejected: winner_id=%s user=%s", winner_id, w["user_id"])


@tree.command(name="giveaway_stats", description="Shows statistics for a giveaway", guild=guild_obj)
@app_commands.describe(giveaway_id="Giveaway ID (leave empty = current)")
@app_commands.checks.has_permissions(manage_messages=True)
async def giveaway_stats(interaction: discord.Interaction, giveaway_id: int = 0):
    await interaction.response.defer(ephemeral=True)

    with get_db() as conn:
        if giveaway_id:
            gw = conn.execute("SELECT * FROM giveaways WHERE id=?", (giveaway_id,)).fetchone()
        else:
            gw = conn.execute(
                "SELECT * FROM giveaways WHERE active=1 ORDER BY id DESC LIMIT 1"
            ).fetchone()

    if not gw:
        await interaction.followup.send("❌ No active giveaway found.", ephemeral=True)
        return

    gw_id = gw["id"]

    with get_db() as conn:
        total_entries  = conn.execute("SELECT COUNT(*) FROM entries WHERE giveaway_id=?", (gw_id,)).fetchone()[0]
        unique_users   = conn.execute(
            "SELECT COUNT(DISTINCT user_id) FROM entries WHERE giveaway_id=? AND status='pending'",
            (gw_id,)
        ).fetchone()[0]
        top_users = conn.execute("""
            SELECT username, COUNT(*) AS cnt
            FROM entries
            WHERE giveaway_id=? AND status='pending'
            GROUP BY user_id
            ORDER BY cnt DESC LIMIT 5
        """, (gw_id,)).fetchall()
        winners = conn.execute(
            "SELECT * FROM winners WHERE giveaway_id=? ORDER BY draw_round",
            (gw_id,)
        ).fetchall()

    embed = discord.Embed(
        title=f"📊 Stats: {gw['name']}",
        color=discord.Color.blurple()
    )
    embed.add_field(name="Status",          value="✅ Active" if gw["active"] else "🔴 Ended", inline=True)
    embed.add_field(name="Total Entries", value=str(total_entries), inline=True)
    embed.add_field(name="Unique Participants", value=str(unique_users), inline=True)
    embed.add_field(name="Winners",
                    value=f"{gw['winners_drawn']} / {gw['total_winners']}", inline=True)
    embed.add_field(name="Period",
                    value=f"{gw['start_date'][:16]} → {gw['end_date'][:16]}", inline=False)

    if top_users:
        top_str = "\n".join(f"{i+1}. **{r['username']}** – {r['cnt']} entries"
                            for i, r in enumerate(top_users))
        embed.add_field(name="🔝 Top Participants (by entries)", value=top_str, inline=False)

    if winners:
        def status_icon(s):
            return {"approved": "✅", "rejected": "❌", "pending": "⏳"}.get(s, "❓")
        w_str = "\n".join(
            f"Round {w['draw_round']}: <@{w['user_id']}> {status_icon(w['status'])}"
            for w in winners
        )
        embed.add_field(name="🏆 Winner Overview", value=w_str, inline=False)

    await interaction.followup.send(embed=embed, ephemeral=True)


@tree.command(name="giveaway_list", description="Lists all giveaways", guild=guild_obj)
@app_commands.checks.has_permissions(administrator=True)
async def giveaway_list(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM giveaways ORDER BY id DESC LIMIT 10"
        ).fetchall()

    if not rows:
        await interaction.followup.send("No giveaways found yet.", ephemeral=True)
        return

    lines = []
    for r in rows:
        status = "✅ Active" if r["active"] else "🔴 Ended"
        lines.append(f"**ID {r['id']}** – {r['name']} [{status}] ({r['winners_drawn']}/{r['total_winners']} winners)")

    embed = discord.Embed(title="📋 Giveaway List", description="\n".join(lines), color=discord.Color.blurple())
    await interaction.followup.send(embed=embed, ephemeral=True)


@tree.command(name="scan_history", description="Scans past messages in the entry channel (backfill)", guild=guild_obj)
@app_commands.describe(
    giveaway_id="Giveaway ID",
    limit="Maximum number of messages (default: 1000)"
)
@app_commands.checks.has_permissions(administrator=True)
async def scan_history(interaction: discord.Interaction, giveaway_id: int, limit: int = 1000):
    """Reads past messages that were posted before the bot started."""
    await interaction.response.defer(ephemeral=True)

    with get_db() as conn:
        gw = conn.execute("SELECT * FROM giveaways WHERE id=?", (giveaway_id,)).fetchone()
    if not gw:
        await interaction.followup.send("❌ Giveaway not found.", ephemeral=True)
        return

    channel = bot.get_channel(gw["channel_id"])
    if not channel:
        await interaction.followup.send("❌ Channel not found.", ephemeral=True)
        return

    start_dt = datetime.fromisoformat(gw["start_date"])
    end_dt   = datetime.fromisoformat(gw["end_date"])

    count = 0
    skipped = 0
    async for msg in channel.history(limit=limit, oldest_first=True, after=start_dt, before=end_dt):
        if msg.author.bot:
            continue
        msg_url = f"https://discord.com/channels/{msg.guild.id}/{channel.id}/{msg.id}"
        with get_db() as conn:
            existing = conn.execute("SELECT id FROM entries WHERE message_id=?", (msg.id,)).fetchone()
            if existing:
                skipped += 1
                continue
            conn.execute("""
                INSERT INTO entries (giveaway_id, user_id, username, message_id,
                                     message_url, message_content, posted_at)
                VALUES (?,?,?,?,?,?,?)
            """, (
                giveaway_id, msg.author.id, str(msg.author), msg.id,
                msg_url, msg.content[:2000],
                msg.created_at.replace(tzinfo=timezone.utc).isoformat()
            ))
            count += 1

    await interaction.followup.send(
        f"✅ Scan completed:\n"
        f"• **{count}** new entries imported\n"
        f"• **{skipped}** already existed (skipped)",
        ephemeral=True
    )
    log.info("Scan completed: giveaway_id=%s new=%s skipped=%s", giveaway_id, count, skipped)


@tree.command(name="disqualify_entry", description="Disqualifies a single entry manually", guild=guild_obj)
@app_commands.describe(
    message_id="Discord message ID of the entry",
    reason="Reason for disqualification"
)
@app_commands.checks.has_permissions(administrator=True)
async def disqualify_entry(interaction: discord.Interaction, message_id: str, reason: str = ""):
    await interaction.response.defer(ephemeral=True)
    mid = int(message_id)
    with get_db() as conn:
        entry = conn.execute("SELECT * FROM entries WHERE message_id=?", (mid,)).fetchone()
        if not entry:
            await interaction.followup.send("❌ Entry not found.", ephemeral=True)
            return
        conn.execute("UPDATE entries SET status='disqualified' WHERE message_id=?", (mid,))
    await interaction.followup.send(
        f"✅ Entry from <@{entry['user_id']}> disqualified."
        + (f"\n**Reason:** {reason}" if reason else ""),
        ephemeral=True
    )


# ── Error Handler ──────────────────────────────────────────────────────────────
@tree.error
async def on_app_command_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.errors.MissingPermissions):
        await interaction.response.send_message(
            "❌ You do not have permission to use this command.", ephemeral=True
        )
    else:
        log.error("Command error: %s", error, exc_info=True)
        if not interaction.response.is_done():
            await interaction.response.send_message("❌ An error occurred.", ephemeral=True)


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    bot.run(BOT_TOKEN)
