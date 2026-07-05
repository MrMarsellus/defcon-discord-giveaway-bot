"""
Boticana / DeFCoN – Discord Giveaway Bot v2
============================================
No permanent monitoring. The bot is started at the end of the giveaway,
reads the entry channel via /scan, and then draws the winners.
============================================
Required: discord.py >= 2.3  |  Python >= 3.10
Installation: pip install "discord.py>=2.3" python-dotenv
"""

import logging
import os
import random
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

# ── Configuration ──────────────────────────────────────────────────────────────
load_dotenv()

BOT_TOKEN  = os.getenv("DISCORD_TOKEN", "")
GUILD_ID   = int(os.getenv("GUILD_ID", "0"))
DB_PATH    = os.getenv("DB_PATH", "giveaway.db")
LOG_LEVEL  = os.getenv("LOG_LEVEL", "INFO")

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
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                name                TEXT NOT NULL,
                giveaway_channel_id INTEGER NOT NULL,  -- Announcement + winner posts
                entry_channel_id    INTEGER NOT NULL,  -- Channel with user submissions
                start_date          TEXT NOT NULL,     -- ISO-8601 UTC
                end_date            TEXT NOT NULL,     -- ISO-8601 UTC
                total_winners       INTEGER NOT NULL DEFAULT 1,
                winners_drawn       INTEGER NOT NULL DEFAULT 0,
                -- Options
                require_attachment  INTEGER NOT NULL DEFAULT 0,  -- 1 = image/attachment required
                min_account_days    INTEGER NOT NULL DEFAULT 0,  -- 0 = no limit
                required_role_id    INTEGER,                     -- NULL = no role filter
                winner_dm_text      TEXT,                        -- NULL = no DM
                -- Status
                scanned             INTEGER NOT NULL DEFAULT 0,  -- 1 = /scan was executed
                active              INTEGER NOT NULL DEFAULT 1,  -- 0 = completed
                created_at          TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS entries (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                giveaway_id     INTEGER NOT NULL REFERENCES giveaways(id),
                user_id         INTEGER NOT NULL,
                username        TEXT NOT NULL,
                account_age_days INTEGER NOT NULL DEFAULT 0,
                has_attachment  INTEGER NOT NULL DEFAULT 0,
                message_id      INTEGER NOT NULL UNIQUE,
                message_url     TEXT NOT NULL,
                message_content TEXT,
                posted_at       TEXT NOT NULL,
                status          TEXT NOT NULL DEFAULT 'eligible',
                -- eligible | winner | rejected | ineligible | disqualified
                disqualify_reason TEXT,
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
                review_msg_id   INTEGER,
                announce_msg_id INTEGER,
                drawn_at        TEXT NOT NULL DEFAULT (datetime('now')),
                decided_at      TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_winners_giveaway ON winners(giveaway_id);
        """)
    log.info("Database initialized: %s", DB_PATH)


# ── Helper functions ───────────────────────────────────────────────────────────
def parse_dt(dt_str: str) -> datetime:
    """'DD.MM.YYYY HH:MM' → UTC datetime"""
    return datetime.strptime(dt_str.strip(), "%d.%m.%Y %H:%M").replace(tzinfo=timezone.utc)


def fmt_dt(iso: str) -> str:
    return iso[:16].replace("T", " ") + " UTC"


def get_giveaway(giveaway_id: int) -> Optional[sqlite3.Row]:
    with get_db() as conn:
        return conn.execute("SELECT * FROM giveaways WHERE id=?", (giveaway_id,)).fetchone()


def draw_next_winner(giveaway_id: int, excluded_user_ids: list[int]) -> Optional[sqlite3.Row]:
    """
    Weighted random: each eligible entry = 1 ticket.
    Excluded: users who have already won + rejected users.
    """
    with get_db() as conn:
        pool = conn.execute("""
            SELECT id AS entry_id, user_id, username, message_url, message_content
            FROM entries
            WHERE giveaway_id = ? AND status = 'eligible'
        """, (giveaway_id,)).fetchall()

    eligible = [r for r in pool if r["user_id"] not in excluded_user_ids]
    if not eligible:
        return None
    return random.choice(eligible)


def excluded_users(giveaway_id: int) -> list[int]:
    """Returns user_ids that can no longer be drawn (approved + rejected)."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT user_id FROM winners WHERE giveaway_id=? AND status IN ('approved','rejected')",
            (giveaway_id,)
        ).fetchall()
    return [r["user_id"] for r in rows]


# ── Bot setup ──────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.guilds          = True
intents.members         = True

bot   = commands.Bot(command_prefix="!", intents=intents)
tree  = bot.tree
guild_obj = discord.Object(id=GUILD_ID)


@bot.event
async def on_ready():
    log.info("Bot online as %s (ID %s)", bot.user, bot.user.id)
    await tree.sync(guild=guild_obj)
    log.info("Slash commands synchronized for guild %s", GUILD_ID)


# ══════════════════════════════════════════════════════════════════════════════
# COMMAND: /giveaway_announce
# Posts an announcement embed in the giveaway channel. One-time only, no monitoring.
# ══════════════════════════════════════════════════════════════════════════════
@tree.command(
    name="giveaway_announce",
    description="Posts a giveaway announcement in the giveaway channel",
    guild=guild_obj
)
@app_commands.describe(
    giveaway_channel = "Channel for announcement and winners",
    entry_channel    = "Channel where users submit their posts",
    title            = "Title of the giveaway",
    description      = "Description / participation requirements (max. 2000 characters)",
    end_date         = "Submission deadline (DD.MM.YYYY HH:MM)",
    color            = "Embed color: green | gold | blue | red (default: green)",
)
@app_commands.checks.has_permissions(administrator=True)
async def giveaway_announce(
    interaction: discord.Interaction,
    giveaway_channel: discord.TextChannel,
    entry_channel: discord.TextChannel,
    title: str,
    description: str,
    end_date: str,
    color: str = "green",
):
    await interaction.response.defer(ephemeral=True)

    try:
        end_dt = parse_dt(end_date)
    except ValueError:
        await interaction.followup.send("❌ Invalid date format. Please use `DD.MM.YYYY HH:MM`.", ephemeral=True)
        return

    color_map = {
        "green": discord.Color.green(),
        "gold":  discord.Color.gold(),
        "blue":  discord.Color.blue(),
        "red":   discord.Color.red(),
    }
    embed_color = color_map.get(color.lower(), discord.Color.green())

    description = description.replace("\\n", "\n")

    embed = discord.Embed(title=f"🎉 {title}", description=description, color=embed_color)
    embed.add_field(name="📬 Submissions", value=entry_channel.mention, inline=True)
    embed.add_field(name="⏰ Deadline", value=fmt_dt(end_dt.isoformat()), inline=True)
    embed.set_footer(text="Good luck! ✨")

    await giveaway_channel.send(embed=embed)
    await interaction.followup.send(
        f"✅ Announcement has been posted in {giveaway_channel.mention}.", ephemeral=True
    )
    log.info("Giveaway announcement posted in #%s", giveaway_channel.name)


# ══════════════════════════════════════════════════════════════════════════════
# COMMAND: /giveaway_create
# Creates the giveaway in the DB (with all options).
# ══════════════════════════════════════════════════════════════════════════════
@tree.command(
    name="giveaway_create",
    description="Creates a new giveaway (used for evaluation at the end)",
    guild=guild_obj
)
@app_commands.describe(
    name                = "Name / title of the giveaway",
    giveaway_channel    = "Channel for announcement + winner announcement",
    entry_channel       = "Channel with user submissions",
    start               = "Submission start date (DD.MM.YYYY HH:MM)",
    end                 = "Submission end date (DD.MM.YYYY HH:MM)",
    winners             = "Number of winners",
    require_attachment  = "Should only posts with image/attachment count?",
    min_account_days    = "Minimum account age in days (0 = no limit)",
    required_role       = "Only users with this role can participate (empty = all)",
    winner_dm_text      = "DM text for winners (empty = no DM). Placeholders: {user}, {giveaway}",
)
@app_commands.checks.has_permissions(administrator=True)
async def giveaway_create(
    interaction: discord.Interaction,
    name: str,
    giveaway_channel: discord.TextChannel,
    entry_channel: discord.TextChannel,
    start: str,
    end: str,
    winners: int = 1,
    require_attachment: bool = False,
    min_account_days: int = 0,
    required_role: Optional[discord.Role] = None,
    winner_dm_text: str = "",
):
    await interaction.response.defer(ephemeral=True)

    try:
        start_dt = parse_dt(start)
        end_dt   = parse_dt(end)
    except ValueError:
        await interaction.followup.send("❌ Invalid date format. Please use `DD.MM.YYYY HH:MM`.", ephemeral=True)
        return
    if end_dt <= start_dt:
        await interaction.followup.send("❌ End date must be after start date.", ephemeral=True)
        return
    if winners < 1:
        await interaction.followup.send("❌ At least 1 winner is required.", ephemeral=True)
        return

    with get_db() as conn:
        cursor = conn.execute("""
            INSERT INTO giveaways (
                name, giveaway_channel_id, entry_channel_id,
                start_date, end_date, total_winners,
                require_attachment, min_account_days, required_role_id, winner_dm_text
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (
            name,
            giveaway_channel.id,
            entry_channel.id,
            start_dt.isoformat(),
            end_dt.isoformat(),
            winners,
            int(require_attachment),
            min_account_days,
            required_role.id if required_role else None,
            winner_dm_text.strip() or None,
        ))
        gw_id = cursor.lastrowid

    # Summary
    embed = discord.Embed(title="✅ Giveaway created", color=discord.Color.green())
    embed.add_field(name="ID",              value=str(gw_id),                   inline=True)
    embed.add_field(name="Name",            value=name,                          inline=True)
    embed.add_field(name="Winners",         value=str(winners),                  inline=True)
    embed.add_field(name="Giveaway channel",value=giveaway_channel.mention,      inline=True)
    embed.add_field(name="Entry channel",   value=entry_channel.mention,         inline=True)
    embed.add_field(name="Period",          value=f"{start} – {end} UTC",        inline=False)
    embed.add_field(name="Attachment required",  value="✅ Yes" if require_attachment else "❌ No", inline=True)
    embed.add_field(name="Min. account age", value=f"{min_account_days} days" if min_account_days else "—", inline=True)
    embed.add_field(name="Role filter",    value=required_role.mention if required_role else "—", inline=True)
    embed.add_field(name="Winner DM",     value="✅ Yes" if winner_dm_text else "❌ No", inline=True)
    embed.set_footer(text=f"Next step: /scan giveaway_id:{gw_id}")

    await interaction.followup.send(embed=embed, ephemeral=True)
    log.info("Giveaway created: ID=%s Name=%s", gw_id, name)


# ══════════════════════════════════════════════════════════════════════════════
# COMMAND: /scan
# Reads all posts from the entry channel during the giveaway period.
# Applies filters: attachment, account age, role.
# ══════════════════════════════════════════════════════════════════════════════
@tree.command(
    name="scan",
    description="Reads all posts from the entry channel and applies filters",
    guild=guild_obj
)
@app_commands.describe(
    giveaway_id = "ID of the giveaway (from /giveaway_create)",
    limit       = "Max. number of messages to read (default: 5000)",
)
@app_commands.checks.has_permissions(administrator=True)
async def scan(interaction: discord.Interaction, giveaway_id: int, limit: int = 5000):
    await interaction.response.defer(ephemeral=False)

    gw = get_giveaway(giveaway_id)
    if not gw:
        await interaction.followup.send("❌ Giveaway not found.")
        return

    channel = interaction.guild.get_channel(gw["entry_channel_id"])
    if not channel:
        await interaction.followup.send("❌ Entry channel not found. Check the configuration.")
        return

    start_dt = datetime.fromisoformat(gw["start_date"])
    end_dt   = datetime.fromisoformat(gw["end_date"])
    now      = datetime.now(timezone.utc)

    # Filter options from the giveaway configuration
    require_attachment = bool(gw["require_attachment"])
    min_days           = gw["min_account_days"] or 0
    role_id            = gw["required_role_id"]

    # If role filter is active, load member list
    role_member_ids: Optional[set[int]] = None
    if role_id:
        role = interaction.guild.get_role(role_id)
        if role:
            role_member_ids = {m.id for m in role.members}
        else:
            await interaction.followup.send(f"⚠️ Role with ID {role_id} not found – role filter will be ignored.")

    status_msg = await interaction.followup.send(
        f"🔍 Scanning <#{gw['entry_channel_id']}> … (max. {limit} messages, this may take a moment)"
    )

    count_new        = 0
    count_skipped    = 0  # already in DB
    count_filtered   = 0  # removed by filters

    async for msg in channel.history(limit=limit, oldest_first=True, after=start_dt, before=end_dt):
        if msg.author.bot:
            continue

        # Already in DB?
        with get_db() as conn:
            if conn.execute("SELECT 1 FROM entries WHERE message_id=?", (msg.id,)).fetchone():
                count_skipped += 1
                continue

        # Check account age
        created_at   = msg.author.created_at.replace(tzinfo=timezone.utc)
        account_days = (now - created_at).days
        has_attach   = bool(msg.attachments or msg.embeds)
        msg_url      = f"https://discord.com/channels/{msg.guild.id}/{channel.id}/{msg.id}"

        disqualify_reason = None
        status            = "eligible"

        if require_attachment and not has_attach:
            status            = "disqualified"
            disqualify_reason = "No image/attachment"
            count_filtered   += 1
        elif min_days > 0 and account_days < min_days:
            status            = "disqualified"
            disqualify_reason = f"Account too young ({account_days} days, minimum: {min_days})"
            count_filtered   += 1
        elif role_member_ids is not None and msg.author.id not in role_member_ids:
            status            = "disqualified"
            disqualify_reason = "Missing role"
            count_filtered   += 1

        with get_db() as conn:
            conn.execute("""
                INSERT INTO entries (
                    giveaway_id, user_id, username, account_age_days,
                    has_attachment, message_id, message_url,
                    message_content, posted_at, status, disqualify_reason
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, (
                giveaway_id,
                msg.author.id,
                str(msg.author),
                account_days,
                int(has_attach),
                msg.id,
                msg_url,
                msg.content[:2000],
                msg.created_at.replace(tzinfo=timezone.utc).isoformat(),
                status,
                disqualify_reason,
            ))
        count_new += 1

    # Mark scan as completed
    with get_db() as conn:
        conn.execute("UPDATE giveaways SET scanned=1 WHERE id=?", (giveaway_id,))

    # Statistics
    with get_db() as conn:
        eligible_entries = conn.execute(
            "SELECT COUNT(*) FROM entries WHERE giveaway_id=? AND status='eligible'",
            (giveaway_id,)
        ).fetchone()[0]
        unique_eligible = conn.execute(
            "SELECT COUNT(DISTINCT user_id) FROM entries WHERE giveaway_id=? AND status='eligible'",
            (giveaway_id,)
        ).fetchone()[0]

    embed = discord.Embed(title="✅ Scan completed", color=discord.Color.green())
    embed.add_field(name="New entries saved",  value=str(count_new),       inline=True)
    embed.add_field(name="Already exists (skip)",   value=str(count_skipped),   inline=True)
    embed.add_field(name="Filtered out", value=str(count_filtered),  inline=True)
    embed.add_field(name="✅ Eligible entries",     value=str(eligible_entries), inline=True)
    embed.add_field(name="👤 Unique participants",        value=str(unique_eligible),  inline=True)
    embed.set_footer(text=f"Next step: /draw giveaway_id:{giveaway_id}")
    await status_msg.edit(content=None, embed=embed)
    log.info("Scan completed: giveaway_id=%s new=%s filtered=%s eligible=%s",
             giveaway_id, count_new, count_filtered, eligible_entries)


# ══════════════════════════════════════════════════════════════════════════════
# COMMAND: /draw
# Draws the next winner. Admin then reviews and decides with /approve or /reject.
# ══════════════════════════════════════════════════════════════════════════════
@tree.command(name="draw", description="Draws the next winner", guild=guild_obj)
@app_commands.describe(giveaway_id="ID of the giveaway")
@app_commands.checks.has_permissions(administrator=True)
async def draw(interaction: discord.Interaction, giveaway_id: int):
    await interaction.response.defer(ephemeral=False)

    gw = get_giveaway(giveaway_id)
    if not gw:
        await interaction.followup.send("❌ Giveaway not found.")
        return
    if not gw["scanned"]:
        await interaction.followup.send(
            f"⚠️ Giveaway has not been scanned yet. Please run `/scan giveaway_id:{giveaway_id}` first."
        )
        return

    excluded = excluded_users(giveaway_id)
    approved_count = sum(
        1 for uid in excluded
        if any(
            True
            for _ in [None]  # count approved separately
        )
    )
    # Correct approved count
    with get_db() as conn:
        approved_count = conn.execute(
            "SELECT COUNT(*) FROM winners WHERE giveaway_id=? AND status='approved'",
            (giveaway_id,)
        ).fetchone()[0]

    if approved_count >= gw["total_winners"]:
        await interaction.followup.send(
            f"🏆 All **{gw['total_winners']}** winners have already been confirmed!"
        )
        return

    entry = draw_next_winner(giveaway_id, excluded)
    if not entry:
        await interaction.followup.send(
            "😔 No more eligible participants available.\n"
            "All have either already been selected, rejected, or disqualified by filters."
        )
        return

    round_num = len(excluded) + 1

    with get_db() as conn:
        cursor = conn.execute("""
            INSERT INTO winners (giveaway_id, user_id, entry_id, draw_round)
            VALUES (?,?,?,?)
        """, (giveaway_id, entry["user_id"], entry["entry_id"], round_num))
        winner_id = cursor.lastrowid

    embed = discord.Embed(
        title=f"🎲 Winner drawn – Attempt {round_num}",
        color=discord.Color.gold(),
        description=f"**Giveaway:** {gw['name']}"
    )
    embed.add_field(name="👤 User",         value=f"<@{entry['user_id']}> (`{entry['username']}`)", inline=False)
    embed.add_field(name="🔗 Post link",    value=entry["message_url"], inline=False)

    content_preview = (entry["message_content"] or "_(no text)_")[:400]
    embed.add_field(name="📝 Post content (preview)", value=content_preview, inline=False)

    embed.add_field(
        name="⚡ Next steps",
        value=(
            f"`/approve winner_id:{winner_id}` → Confirm winner\n"
            f"`/reject winner_id:{winner_id} reason:\"...\"` → Reject & allow a new draw"
        ),
        inline=False
    )
    embed.add_field(
        name="📊 Status",
        value=f"{approved_count} / {gw['total_winners']} winners confirmed",
        inline=True
    )
    embed.set_footer(text=f"winner_id: {winner_id}  |  entry_id: {entry['entry_id']}")

    await interaction.followup.send(embed=embed)
    log.info("Winner drawn: winner_id=%s user_id=%s", winner_id, entry["user_id"])


# ══════════════════════════════════════════════════════════════════════════════
# COMMAND: /approve
# ══════════════════════════════════════════════════════════════════════════════
@tree.command(name="approve", description="Confirms the drawn winner and posts the announcement", guild=guild_obj)
@app_commands.describe(
    winner_id   = "winner_id from /draw",
    note        = "Optional text for the announcement (e.g. prize description)",
)
@app_commands.checks.has_permissions(administrator=True)
async def approve(interaction: discord.Interaction, winner_id: int, note: str = ""):
    await interaction.response.defer(ephemeral=False)

    with get_db() as conn:
        w = conn.execute("""
            SELECT w.*, e.message_url, e.username,
                   g.name AS gname, g.total_winners, g.id AS gid,
                   g.giveaway_channel_id, g.winner_dm_text
            FROM winners w
            JOIN entries  e ON e.id = w.entry_id
            JOIN giveaways g ON g.id = w.giveaway_id
            WHERE w.id = ?
        """, (winner_id,)).fetchone()

    if not w:
        await interaction.followup.send("❌ winner_id not found.")
        return
    if w["status"] != "pending":
        await interaction.followup.send(f"⚠️ Status is already `{w['status']}`.")
        return

    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute("UPDATE winners  SET status='approved', decided_at=? WHERE id=?", (now, winner_id))
        conn.execute("UPDATE entries  SET status='winner'                    WHERE id=?", (w["entry_id"],))
        conn.execute("UPDATE giveaways SET winners_drawn=winners_drawn+1    WHERE id=?", (w["gid"],))

    gw = get_giveaway(w["gid"])
    remaining = gw["total_winners"] - gw["winners_drawn"]

    # Announcement in the giveaway channel
    gw_channel = interaction.guild.get_channel(w["giveaway_channel_id"])
    announce_embed = discord.Embed(
        title="🏆 Winner!",
        color=discord.Color.gold(),
        description=f"**{w['gname']}**"
    )
    announce_embed.add_field(name="🎉 Congratulations", value=f"<@{w['user_id']}>", inline=False)
    announce_embed.add_field(name="📌 Winning post",          value=w["message_url"],     inline=False)
    if note:
        announce_embed.add_field(name="ℹ️ Note", value=note, inline=False)
    if remaining > 0:
        announce_embed.set_footer(text=f"{remaining} winner(s) still pending.")
    else:
        announce_embed.set_footer(text="All winners have been determined! 🎊")

    announce_msg = None
    if gw_channel:
        announce_msg = await gw_channel.send(f"<@{w['user_id']}>", embed=announce_embed)
        with get_db() as conn:
            conn.execute("UPDATE winners SET announce_msg_id=? WHERE id=?", (announce_msg.id, winner_id))

    # Automatic DM to winner
    dm_text = w["winner_dm_text"]
    dm_status = ""
    if dm_text:
        try:
            member = interaction.guild.get_member(w["user_id"]) or await interaction.guild.fetch_member(w["user_id"])
            filled_dm = dm_text.replace("{user}", member.display_name).replace("{giveaway}", w["gname"])
            await member.send(filled_dm)
            dm_status = "\n✉️ DM sent to winner."
        except discord.Forbidden:
            dm_status = "\n⚠️ DM could not be sent (user has DMs disabled)."
        except Exception as e:
            dm_status = f"\n⚠️ DM error: {e}"

    # All winners determined → close giveaway
    if remaining <= 0:
        with get_db() as conn:
            conn.execute("UPDATE giveaways SET active=0 WHERE id=?", (w["gid"],))

    next_hint = (
        f"Next draw: `/draw giveaway_id:{w['gid']}`" if remaining > 0
        else "🎊 All winners determined! Giveaway completed."
    )
    await interaction.followup.send(
        f"✅ <@{w['user_id']}> confirmed and announced in {gw_channel.mention if gw_channel else '#giveaway-channel'}.\n"
        f"{next_hint}{dm_status}"
    )
    log.info("Winner approved: winner_id=%s user=%s", winner_id, w["user_id"])


# ══════════════════════════════════════════════════════════════════════════════
# COMMAND: /reject
# ══════════════════════════════════════════════════════════════════════════════
@tree.command(name="reject", description="Rejects the drawn winner – enables the next draw", guild=guild_obj)
@app_commands.describe(
    winner_id = "winner_id from /draw",
    reason    = "Reason for rejection",
)
@app_commands.checks.has_permissions(administrator=True)
async def reject(interaction: discord.Interaction, winner_id: int, reason: str = "Participation requirements not met"):
    await interaction.response.defer(ephemeral=False)

    with get_db() as conn:
        w = conn.execute("""
            SELECT w.*, e.username, g.name AS gname, g.id AS gid
            FROM winners w
            JOIN entries  e ON e.id = w.entry_id
            JOIN giveaways g ON g.id = w.giveaway_id
            WHERE w.id = ?
        """, (winner_id,)).fetchone()

    if not w:
        await interaction.followup.send("❌ winner_id not found.")
        return
    if w["status"] != "pending":
        await interaction.followup.send(f"⚠️ Status is already `{w['status']}`.")
        return

    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute("UPDATE winners SET status='rejected', decided_at=? WHERE id=?", (now, winner_id))
        conn.execute("UPDATE entries SET status='ineligible' WHERE id=?", (w["entry_id"],))

    await interaction.followup.send(
        f"❌ <@{w['user_id']}> (`{w['username']}`) rejected.\n"
        f"**Reason:** {reason}\n"
        f"This user will be skipped in the next draw.\n"
        f"→ `/draw giveaway_id:{w['gid']}` for the next candidate."
    )
    log.info("Winner rejected: winner_id=%s user=%s", winner_id, w["user_id"])


# ══════════════════════════════════════════════════════════════════════════════
# COMMAND: /stats
# ══════════════════════════════════════════════════════════════════════════════
@tree.command(name="stats", description="Shows statistics and participant list for a giveaway", guild=guild_obj)
@app_commands.describe(giveaway_id="ID of the giveaway")
@app_commands.checks.has_permissions(manage_messages=True)
async def stats(interaction: discord.Interaction, giveaway_id: int):
    await interaction.response.defer(ephemeral=True)

    gw = get_giveaway(giveaway_id)
    if not gw:
        await interaction.followup.send("❌ Giveaway not found.", ephemeral=True)
        return

    with get_db() as conn:
        total_entries   = conn.execute("SELECT COUNT(*) FROM entries WHERE giveaway_id=?", (giveaway_id,)).fetchone()[0]
        eligible        = conn.execute("SELECT COUNT(*) FROM entries WHERE giveaway_id=? AND status='eligible'", (giveaway_id,)).fetchone()[0]
        disqualified    = conn.execute("SELECT COUNT(*) FROM entries WHERE giveaway_id=? AND status='disqualified'", (giveaway_id,)).fetchone()[0]
        unique_eligible = conn.execute("SELECT COUNT(DISTINCT user_id) FROM entries WHERE giveaway_id=? AND status='eligible'", (giveaway_id,)).fetchone()[0]
        top5 = conn.execute("""
            SELECT username, COUNT(*) AS cnt
            FROM entries WHERE giveaway_id=? AND status='eligible'
            GROUP BY user_id ORDER BY cnt DESC LIMIT 5
        """, (giveaway_id,)).fetchall()
        winners_rows = conn.execute("""
            SELECT w.draw_round, w.status, w.user_id, e.message_url
            FROM winners w JOIN entries e ON e.id=w.entry_id
            WHERE w.giveaway_id=? ORDER BY w.draw_round
        """, (giveaway_id,)).fetchall()

    embed = discord.Embed(title=f"📊 {gw['name']}", color=discord.Color.blurple())
    embed.add_field(name="Status",    value="✅ Active" if gw["active"] else "🔴 Completed", inline=True)
    embed.add_field(name="Scanned", value="✅ Yes"    if gw["scanned"] else "⏳ No",          inline=True)
    embed.add_field(name="Winners",  value=f"{gw['winners_drawn']} / {gw['total_winners']}",   inline=True)
    embed.add_field(name="Total entries",     value=str(total_entries),   inline=True)
    embed.add_field(name="Eligible tickets",    value=str(eligible),        inline=True)
    embed.add_field(name="Disqualified",     value=str(disqualified),    inline=True)
    embed.add_field(name="Unique participants",   value=str(unique_eligible), inline=True)
    embed.add_field(name="Period",
        value=f"{fmt_dt(gw['start_date'])} → {fmt_dt(gw['end_date'])}", inline=False)

    options_lines = []
    if gw["require_attachment"]: options_lines.append("📎 Attachment required")
    if gw["min_account_days"]:   options_lines.append(f"📅 Min. account age: {gw['min_account_days']} days")
    if gw["required_role_id"]:   options_lines.append(f"🎭 Role filter: <@&{gw['required_role_id']}>")
    if gw["winner_dm_text"]:     options_lines.append("✉️ Automatic DM enabled")
    if options_lines:
        embed.add_field(name="⚙️ Options", value="\n".join(options_lines), inline=False)

    if top5:
        embed.add_field(
            name="🔝 Top participants (by tickets)",
            value="\n".join(f"{i+1}. **{r['username']}** – {r['cnt']} tickets" for i, r in enumerate(top5)),
            inline=False
        )

    if winners_rows:
        icons = {"approved": "✅", "rejected": "❌", "pending": "⏳"}
        embed.add_field(
            name="🏆 Drawn winners",
            value="\n".join(
                f"Draw {r['draw_round']}: <@{r['user_id']}> {icons.get(r['status'], '❓')} – [Post]({r['message_url']})"
                for r in winners_rows
            ),
            inline=False
        )

    await interaction.followup.send(embed=embed, ephemeral=True)


# ══════════════════════════════════════════════════════════════════════════════
# COMMAND: /giveaway_list
# ══════════════════════════════════════════════════════════════════════════════
@tree.command(name="giveaway_list", description="Lists all giveaways", guild=guild_obj)
@app_commands.checks.has_permissions(administrator=True)
async def giveaway_list(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM giveaways ORDER BY id DESC LIMIT 15").fetchall()
    if not rows:
        await interaction.followup.send("No giveaways yet.", ephemeral=True)
        return
    lines = []
    for r in rows:
        status = "✅ Active" if r["active"] else "🔴 Finished"
        scanned = "📋" if r["scanned"] else "⏳"
        lines.append(f"**ID {r['id']}** {scanned} [{status}] – {r['name']} ({r['winners_drawn']}/{r['total_winners']} winners)")
    embed = discord.Embed(title="📋 Giveaway list", description="\n".join(lines), color=discord.Color.blurple())
    await interaction.followup.send(embed=embed, ephemeral=True)


# ══════════════════════════════════════════════════════════════════════════════
# COMMAND: /disqualify
# Manually disqualify a post (e.g. spam, irrelevant post)
# ══════════════════════════════════════════════════════════════════════════════
@tree.command(name="disqualify", description="Manually disqualifies a single entry", guild=guild_obj)
@app_commands.describe(
    message_id = "Discord message ID of the entry",
    reason     = "Reason",
)
@app_commands.checks.has_permissions(administrator=True)
async def disqualify(interaction: discord.Interaction, message_id: str, reason: str = "Manually disqualified"):
    await interaction.response.defer(ephemeral=True)
    mid = int(message_id)
    with get_db() as conn:
        entry = conn.execute("SELECT * FROM entries WHERE message_id=?", (mid,)).fetchone()
        if not entry:
            await interaction.followup.send("❌ Entry not found. Has the scan already been run?", ephemeral=True)
            return
        conn.execute(
            "UPDATE entries SET status='disqualified', disqualify_reason=? WHERE message_id=?",
            (reason, mid)
        )
    await interaction.followup.send(
        f"✅ Entry from <@{entry['user_id']}> disqualified.\n**Reason:** {reason}",
        ephemeral=True
    )


# ── Error handler ──────────────────────────────────────────────────────────────
@tree.error
async def on_app_command_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.errors.MissingPermissions):
        msg = "❌ You do not have permission to use this command."
    else:
        log.error("Command error: %s", error, exc_info=True)
        msg = f"❌ Error: `{error}`"
    if not interaction.response.is_done():
        await interaction.response.send_message(msg, ephemeral=True)
    else:
        await interaction.followup.send(msg, ephemeral=True)


# ── Start ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    bot.run(BOT_TOKEN)
