"""
Boticana / DeFCoN Discord Giveaway Bot
=======================================
Überwacht einen Channel, speichert Posts in SQLite und zieht zufällig Gewinner.

Benötigt: discord.py >= 2.3  |  Python >= 3.10
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

# ── Konfiguration ──────────────────────────────────────────────────────────────
load_dotenv()

BOT_TOKEN       = os.getenv("DISCORD_TOKEN", "")
ENTRY_CHANNEL   = int(os.getenv("ENTRY_CHANNEL_ID", "0"))   # Channel für Teilnahme-Posts
WINNER_CHANNEL  = int(os.getenv("WINNER_CHANNEL_ID", "0"))  # Channel für Gewinner-Ankündigung
ADMIN_CHANNEL   = int(os.getenv("ADMIN_CHANNEL_ID", "0"))   # Channel für Admin-Review
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

# ── Datenbank ──────────────────────────────────────────────────────────────────
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
                active      INTEGER NOT NULL DEFAULT 1,  -- 1 = läuft, 0 = beendet
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
                review_msg_id   INTEGER,   -- Admin-Review-Message
                announce_msg_id INTEGER,   -- Ankündigung im Winner-Channel
                drawn_at        TEXT NOT NULL DEFAULT (datetime('now')),
                decided_at      TEXT
            );
        """)
    log.info("Datenbank initialisiert: %s", DB_PATH)


# ── Hilfsfunktionen ────────────────────────────────────────────────────────────
def get_active_giveaway(channel_id: int) -> Optional[sqlite3.Row]:
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM giveaways WHERE channel_id=? AND active=1 ORDER BY id DESC LIMIT 1",
            (channel_id,)
        ).fetchone()


def parse_dt(dt_str: str) -> datetime:
    """Erwartet 'DD.MM.YYYY HH:MM' (lokale Eingabe) → UTC datetime."""
    return datetime.strptime(dt_str, "%d.%m.%Y %H:%M").replace(tzinfo=timezone.utc)


def fmt_dt(dt: datetime) -> str:
    return dt.strftime("%d.%m.%Y %H:%M UTC")


def draw_winner(giveaway_id: int, already_won_users: list[int]) -> Optional[sqlite3.Row]:
    """
    Zieht einen Gewinner gewichtet nach Anzahl Einträge.
    Ausgeschlossen: bereits gewonnene User + disqualifizierte Einträge.
    """
    with get_db() as conn:
        rows = conn.execute("""
            SELECT user_id, username, id AS entry_id, message_url, message_content
            FROM entries
            WHERE giveaway_id=?
              AND status='pending'
        """, (giveaway_id,)).fetchall()

    # Alle pending-Einträge → Pool (ein Eintrag = ein Los)
    pool = [r for r in rows if r["user_id"] not in already_won_users]
    if not pool:
        return None
    chosen_entry = random.choice(pool)
    return chosen_entry


# ── Bot-Setup ──────────────────────────────────────────────────────────────────
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
    log.info("Bot online als %s (ID %s)", bot.user, bot.user.id)
    guild = discord.Object(id=GUILD_ID)
    await tree.sync(guild=guild)
    log.info("Slash-Commands synchronisiert für Guild %s", GUILD_ID)
    check_giveaway_end.start()


@bot.event
async def on_message(message: discord.Message):
    """Speichert jede Nachricht im Eintrag-Channel in die DB."""
    if message.author.bot:
        return
    if message.channel.id != ENTRY_CHANNEL:
        return

    giveaway = get_active_giveaway(ENTRY_CHANNEL)
    if not giveaway:
        return

    # Nur innerhalb des Giveaway-Zeitraums
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
    log.debug("Eintrag gespeichert: user=%s msg=%s", message.author, message.id)
    await bot.process_commands(message)


@bot.event
async def on_message_delete(message: discord.Message):
    """Markiert gelöschte Nachrichten als disqualifiziert."""
    if message.channel.id != ENTRY_CHANNEL:
        return
    with get_db() as conn:
        conn.execute(
            "UPDATE entries SET status='disqualified' WHERE message_id=?",
            (message.id,)
        )
    log.info("Nachricht gelöscht → disqualifiziert: msg=%s", message.id)


# ── Background Task: automatisches Ende ───────────────────────────────────────
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
                f"⏰ **Giveaway `{gw['name']}` ist abgelaufen!**\n"
                f"Verwende `/draw giveaway_id:{gw['id']}` um den ersten Gewinner zu ziehen."
            )
        with get_db() as conn:
            # Giveaway NICHT automatisch schließen – Admin zieht manuell
            pass
        log.info("Giveaway %s (%s) abgelaufen", gw["id"], gw["name"])


# ── Slash Commands ─────────────────────────────────────────────────────────────
guild_obj = discord.Object(id=GUILD_ID)


@tree.command(name="giveaway_start", description="Startet ein neues Giveaway", guild=guild_obj)
@app_commands.describe(
    name        = "Name / Titel des Giveaways",
    start       = "Startdatum und -zeit (DD.MM.YYYY HH:MM)",
    end         = "Enddatum und -zeit (DD.MM.YYYY HH:MM)",
    winners     = "Anzahl der Gewinner (Standard: 1)",
    channel_id  = "Channel-ID für Einträge (leer = konfigurierten Channel nutzen)",
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
        await interaction.followup.send("❌ Ungültiges Datumsformat. Bitte `DD.MM.YYYY HH:MM` verwenden.", ephemeral=True)
        return

    if end_dt <= start_dt:
        await interaction.followup.send("❌ Enddatum muss nach dem Startdatum liegen.", ephemeral=True)
        return
    if winners < 1:
        await interaction.followup.send("❌ Mindestens 1 Gewinner erforderlich.", ephemeral=True)
        return

    ch_id = int(channel_id) if channel_id.strip() else ENTRY_CHANNEL

    # Prüfe ob schon ein aktives Giveaway in diesem Channel läuft
    existing = get_active_giveaway(ch_id)
    if existing:
        await interaction.followup.send(
            f"❌ In <#{ch_id}> läuft bereits Giveaway **{existing['name']}** (ID {existing['id']}).\n"
            f"Beende es zuerst mit `/giveaway_end`.", ephemeral=True
        )
        return

    with get_db() as conn:
        cursor = conn.execute("""
            INSERT INTO giveaways (name, channel_id, start_date, end_date, total_winners)
            VALUES (?,?,?,?,?)
        """, (name, ch_id, start_dt.isoformat(), end_dt.isoformat(), winners))
        gw_id = cursor.lastrowid

    embed = discord.Embed(
        title="🎉 Neues Giveaway gestartet!",
        color=discord.Color.green(),
        description=f"**{name}**"
    )
    embed.add_field(name="Giveaway-ID", value=str(gw_id), inline=True)
    embed.add_field(name="Channel",     value=f"<#{ch_id}>", inline=True)
    embed.add_field(name="Gewinner",    value=str(winners), inline=True)
    embed.add_field(name="Start",       value=fmt_dt(start_dt), inline=True)
    embed.add_field(name="Ende",        value=fmt_dt(end_dt),   inline=True)

    await interaction.followup.send(embed=embed, ephemeral=False)
    log.info("Giveaway gestartet: ID=%s Name=%s", gw_id, name)


@tree.command(name="giveaway_end", description="Beendet ein aktives Giveaway manuell", guild=guild_obj)
@app_commands.describe(giveaway_id="ID des Giveaways")
@app_commands.checks.has_permissions(administrator=True)
async def giveaway_end(interaction: discord.Interaction, giveaway_id: int):
    await interaction.response.defer(ephemeral=True)
    with get_db() as conn:
        gw = conn.execute("SELECT * FROM giveaways WHERE id=?", (giveaway_id,)).fetchone()
        if not gw:
            await interaction.followup.send("❌ Giveaway nicht gefunden.", ephemeral=True)
            return
        conn.execute("UPDATE giveaways SET active=0 WHERE id=?", (giveaway_id,))
    await interaction.followup.send(f"✅ Giveaway **{gw['name']}** (ID {giveaway_id}) beendet.", ephemeral=True)
    log.info("Giveaway beendet: ID=%s", giveaway_id)


@tree.command(name="draw", description="Zieht den nächsten Gewinner", guild=guild_obj)
@app_commands.describe(giveaway_id="ID des Giveaways")
@app_commands.checks.has_permissions(administrator=True)
async def draw(interaction: discord.Interaction, giveaway_id: int):
    await interaction.response.defer(ephemeral=False)

    with get_db() as conn:
        gw = conn.execute("SELECT * FROM giveaways WHERE id=?", (giveaway_id,)).fetchone()
    if not gw:
        await interaction.followup.send("❌ Giveaway nicht gefunden.")
        return

    # Bereits gewonnene User ermitteln
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
            f"🏆 Alle {gw['total_winners']} Gewinner wurden bereits gezogen und bestätigt!"
        )
        return

    # Auch bereits als rejected gemeldete User ausschließen
    excluded = list(set(won_users + rejected_users))
    entry = draw_winner(giveaway_id, excluded)

    if not entry:
        await interaction.followup.send(
            "😔 Keine weiteren berechtigten Einträge vorhanden. "
            "Alle Teilnehmer wurden entweder bereits gewählt, disqualifiziert oder abgelehnt."
        )
        return

    round_num = len(won_users) + len(rejected_users) + 1

    with get_db() as conn:
        cursor = conn.execute("""
            INSERT INTO winners (giveaway_id, user_id, entry_id, draw_round)
            VALUES (?,?,?,?)
        """, (giveaway_id, entry["user_id"], entry["entry_id"], round_num))
        winner_id = cursor.lastrowid

    # Admin-Review Embed
    admin_ch = bot.get_channel(ADMIN_CHANNEL)
    embed = discord.Embed(
        title=f"🎲 Gewinner gezogen – Runde {round_num}",
        color=discord.Color.gold(),
        description=f"**Giveaway:** {gw['name']} (ID {giveaway_id})\n"
                    f"**Gezogen:** {round_num} von {gw['total_winners']} Gewinnern"
    )
    embed.add_field(name="User",       value=f"<@{entry['user_id']}> ({entry['username']})", inline=False)
    embed.add_field(name="Post-Link",  value=entry["message_url"], inline=False)
    embed.add_field(name="Inhalt",     value=(entry["message_content"] or "_(kein Text)_")[:512], inline=False)
    embed.add_field(
        name="Aktion",
        value=f"Prüfe den Post und verwende dann:\n"
              f"`/approve winner_id:{winner_id}` – berechtigt ✅\n"
              f"`/reject winner_id:{winner_id}` – nicht berechtigt ❌",
        inline=False
    )
    embed.set_footer(text=f"Winner-DB-ID: {winner_id} | Eintrag-ID: {entry['entry_id']}")

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
            title="✅ Gewinner gezogen",
            description=f"Review wurde in <#{ADMIN_CHANNEL}> gepostet.",
            color=discord.Color.blue()
        )
    )
    log.info("Gewinner gezogen: winner_id=%s user=%s entry=%s", winner_id, entry["user_id"], entry["entry_id"])


@tree.command(name="approve", description="Bestätigt einen gezogenen Gewinner", guild=guild_obj)
@app_commands.describe(
    winner_id="ID des Gewinners (aus /draw)",
    note="Optionale Notiz zur Ankündigung"
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
        await interaction.followup.send("❌ Gewinner-ID nicht gefunden.")
        return
    if w["status"] != "pending":
        await interaction.followup.send(f"⚠️ Status ist bereits `{w['status']}` – keine Änderung.")
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

    # Gewinner-Ankündigung
    winner_ch = bot.get_channel(WINNER_CHANNEL)
    announce_embed = discord.Embed(
        title="🏆 Gewinner bestätigt!",
        color=discord.Color.gold(),
        description=f"**{w['giveaway_name']}**"
    )
    announce_embed.add_field(name="🎉 Gewinner", value=f"<@{w['user_id']}>", inline=False)
    announce_embed.add_field(name="Gewinner-Post", value=w["message_url"], inline=False)
    if note:
        announce_embed.add_field(name="Hinweis", value=note, inline=False)

    # Prüfe ob alle Gewinner gezogen wurden
    with get_db() as conn:
        gw = conn.execute("SELECT * FROM giveaways WHERE id=?", (w["giveaway_id"],)).fetchone()

    remaining = gw["total_winners"] - gw["winners_drawn"] - 1  # -1 weil noch nicht committed
    # Vereinfachung: nach Commit
    with get_db() as conn:
        gw = conn.execute("SELECT * FROM giveaways WHERE id=?", (w["giveaway_id"],)).fetchone()
    remaining = gw["total_winners"] - gw["winners_drawn"]

    if remaining > 0:
        announce_embed.set_footer(text=f"Noch {remaining} Gewinner ausstehend. Nächster Zug: /draw giveaway_id:{w['giveaway_id']}")
    else:
        announce_embed.set_footer(text="Alle Gewinner wurden ermittelt! 🎊")
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
        f"✅ <@{w['user_id']}> wurde als Gewinner bestätigt und in <#{WINNER_CHANNEL}> angekündigt!\n"
        + (f"Noch **{remaining}** Gewinner ausstehend. Verwende `/draw giveaway_id:{w['giveaway_id']}`" if remaining > 0 else "🎊 Alle Gewinner ermittelt!")
    )
    log.info("Gewinner bestätigt: winner_id=%s user=%s", winner_id, w["user_id"])


@tree.command(name="reject", description="Lehnt einen gezogenen Gewinner ab und ermöglicht Neuzug", guild=guild_obj)
@app_commands.describe(
    winner_id="ID des Gewinners (aus /draw)",
    reason="Grund für die Ablehnung"
)
@app_commands.checks.has_permissions(administrator=True)
async def reject(interaction: discord.Interaction, winner_id: int, reason: str = "Bedingungen nicht erfüllt"):
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
        await interaction.followup.send("❌ Gewinner-ID nicht gefunden.")
        return
    if w["status"] != "pending":
        await interaction.followup.send(f"⚠️ Status ist bereits `{w['status']}` – keine Änderung.")
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
        f"❌ <@{w['user_id']}> ({w['username']}) wurde abgelehnt.\n"
        f"**Grund:** {reason}\n"
        f"Der User wird beim nächsten Zug ausgeschlossen.\n"
        f"Verwende `/draw giveaway_id:{w['giveaway_id']}` für den nächsten Ziehversuch."
    )
    log.info("Gewinner abgelehnt: winner_id=%s user=%s", winner_id, w["user_id"])


@tree.command(name="giveaway_stats", description="Zeigt Statistiken eines Giveaways", guild=guild_obj)
@app_commands.describe(giveaway_id="ID des Giveaways (leer = aktuelles)")
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
        await interaction.followup.send("❌ Kein aktives Giveaway gefunden.", ephemeral=True)
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
    embed.add_field(name="Status",          value="✅ Aktiv" if gw["active"] else "🔴 Beendet", inline=True)
    embed.add_field(name="Gesamt-Einträge", value=str(total_entries), inline=True)
    embed.add_field(name="Unique Teilnehmer", value=str(unique_users), inline=True)
    embed.add_field(name="Gewinner",
                    value=f"{gw['winners_drawn']} / {gw['total_winners']}", inline=True)
    embed.add_field(name="Zeitraum",
                    value=f"{gw['start_date'][:16]} → {gw['end_date'][:16]}", inline=False)

    if top_users:
        top_str = "\n".join(f"{i+1}. **{r['username']}** – {r['cnt']} Einträge"
                            for i, r in enumerate(top_users))
        embed.add_field(name="🔝 Top-Teilnehmer (nach Einträgen)", value=top_str, inline=False)

    if winners:
        def status_icon(s):
            return {"approved": "✅", "rejected": "❌", "pending": "⏳"}.get(s, "❓")
        w_str = "\n".join(
            f"Runde {w['draw_round']}: <@{w['user_id']}> {status_icon(w['status'])}"
            for w in winners
        )
        embed.add_field(name="🏆 Gewinner-Übersicht", value=w_str, inline=False)

    await interaction.followup.send(embed=embed, ephemeral=True)


@tree.command(name="giveaway_list", description="Listet alle Giveaways auf", guild=guild_obj)
@app_commands.checks.has_permissions(administrator=True)
async def giveaway_list(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM giveaways ORDER BY id DESC LIMIT 10"
        ).fetchall()

    if not rows:
        await interaction.followup.send("Noch keine Giveaways vorhanden.", ephemeral=True)
        return

    lines = []
    for r in rows:
        status = "✅ Aktiv" if r["active"] else "🔴 Beendet"
        lines.append(f"**ID {r['id']}** – {r['name']} [{status}] ({r['winners_drawn']}/{r['total_winners']} Gewinner)")

    embed = discord.Embed(title="📋 Giveaway-Liste", description="\n".join(lines), color=discord.Color.blurple())
    await interaction.followup.send(embed=embed, ephemeral=True)


@tree.command(name="scan_history", description="Scannt vergangene Nachrichten im Entry-Channel (Backfill)", guild=guild_obj)
@app_commands.describe(
    giveaway_id="ID des Giveaways",
    limit="Maximale Anzahl Nachrichten (Standard: 1000)"
)
@app_commands.checks.has_permissions(administrator=True)
async def scan_history(interaction: discord.Interaction, giveaway_id: int, limit: int = 1000):
    """Liest vergangene Nachrichten ein, die vor Bot-Start gepostet wurden."""
    await interaction.response.defer(ephemeral=True)

    with get_db() as conn:
        gw = conn.execute("SELECT * FROM giveaways WHERE id=?", (giveaway_id,)).fetchone()
    if not gw:
        await interaction.followup.send("❌ Giveaway nicht gefunden.", ephemeral=True)
        return

    channel = bot.get_channel(gw["channel_id"])
    if not channel:
        await interaction.followup.send("❌ Channel nicht gefunden.", ephemeral=True)
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
        f"✅ Scan abgeschlossen:\n"
        f"• **{count}** neue Einträge importiert\n"
        f"• **{skipped}** bereits vorhanden (übersprungen)",
        ephemeral=True
    )
    log.info("Scan abgeschlossen: giveaway_id=%s neu=%s übersprungen=%s", giveaway_id, count, skipped)


@tree.command(name="disqualify_entry", description="Disqualifiziert einen einzelnen Eintrag manuell", guild=guild_obj)
@app_commands.describe(
    message_id="Discord-Message-ID des Eintrags",
    reason="Grund für Disqualifizierung"
)
@app_commands.checks.has_permissions(administrator=True)
async def disqualify_entry(interaction: discord.Interaction, message_id: str, reason: str = ""):
    await interaction.response.defer(ephemeral=True)
    mid = int(message_id)
    with get_db() as conn:
        entry = conn.execute("SELECT * FROM entries WHERE message_id=?", (mid,)).fetchone()
        if not entry:
            await interaction.followup.send("❌ Eintrag nicht gefunden.", ephemeral=True)
            return
        conn.execute("UPDATE entries SET status='disqualified' WHERE message_id=?", (mid,))
    await interaction.followup.send(
        f"✅ Eintrag von <@{entry['user_id']}> disqualifiziert."
        + (f"\n**Grund:** {reason}" if reason else ""),
        ephemeral=True
    )


# ── Error Handler ──────────────────────────────────────────────────────────────
@tree.error
async def on_app_command_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.errors.MissingPermissions):
        await interaction.response.send_message(
            "❌ Du hast keine Berechtigung für diesen Befehl.", ephemeral=True
        )
    else:
        log.error("Command-Fehler: %s", error, exc_info=True)
        if not interaction.response.is_done():
            await interaction.response.send_message("❌ Ein Fehler ist aufgetreten.", ephemeral=True)


# ── Einstiegspunkt ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    bot.run(BOT_TOKEN)
