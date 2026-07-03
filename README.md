# DeFCoN Discord Giveaway Bot

A Discord bot for running transparent, fair community giveaways. Winners are drawn randomly from user submissions in a designated entry channel — weighted by number of posts (more posts = more entries), with no user able to win more than once per giveaway.

## Concept

The bot does **not run continuously in the background**.
It is started at the **end of the giveaway**, reads the entry channel retroactively, applies filters, and draws winners.

---

## 1. Requirements

- Ubuntu 20.04 / 22.04 / 24.04
- Python 3.10+
- Discord Bot Token (Developer Portal)

---

## 2. Creating the Discord Bot

1. Go to [https://discord.com/developers/applications](https://discord.com/developers/applications)
2. → **New Application** → enter a name
3. → **Bot** → **Add Bot**
4. Copy the token (needed for `.env`)
5. Under **Privileged Gateway Intents**, enable:
   - ✅ **Server Members Intent**
   - ✅ **Message Content Intent**
6. → **OAuth2 → URL Generator**:
   - Scopes: `bot`, `applications.commands`
   - Bot Permissions: `Send Messages`, `Read Message History`, `Manage Messages`, `Embed Links`, `Mention Everyone`
7. Open the generated link → invite the bot to your server

---

## 3. Installation on VPS

```bash
# Clone the repository
git clone https://github.com/YOUR-USERNAME/discord-giveaway-bot.git /opt/giveaway_bot
cd /opt/giveaway_bot

# Create Python virtual environment
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# Configure environment
cp .env.example .env
nano .env   # fill in your values
```

---

## 4. Configuration (`.env`)

| Variable | Description |
|---|---|
| `DISCORD_TOKEN` | Your bot token from the Developer Portal |
| `GUILD_ID` | Your Discord server ID |
| `DB_PATH` | Path to the SQLite database file (default: `giveaway.db`) |
| `LOG_LEVEL` | Logging level: `DEBUG`, `INFO`, `WARNING`, `ERROR` |

To find your Guild ID: enable **Developer Mode** in Discord (Settings → Advanced → Developer Mode), then right-click your server → **Copy ID**.

---

## 5. Starting the Bot

```bash
cd /opt/giveaway_bot
source venv/bin/activate
python bot.py
```

On first start the bot logs "Slash commands synced". Wait 1–2 minutes for the commands to appear in Discord.

For background operation without a full systemd service, use `screen`:

```bash
screen -S giveaway
source /opt/giveaway_bot/venv/bin/activate
python bot.py
# Ctrl+A, D → detach session
# screen -r giveaway → reattach
```

---

## 6. Optional: systemd Service (always-on)

```bash
cp giveaway_bot.service /etc/systemd/system/

# Adjust the user if needed (default: ubuntu)
nano /etc/systemd/system/giveaway_bot.service

systemctl daemon-reload
systemctl enable giveaway_bot
systemctl start giveaway_bot

# Check status
systemctl status giveaway_bot
journalctl -u giveaway_bot -f   # live logs
```

---

## 7. Giveaway Workflow

### Step 1 — Post the announcement (start of giveaway)

```
/giveaway_announce
  giveaway_channel: #giveaway
  entry_channel: #submissions
  title: DeFCoN Spring Giveaway
  description: Share our tweet and post the link here to enter!
  end_date: 31.07.2026 23:59
  color: green
```

The bot posts an announcement embed in the giveaway channel. The bot does **not** need to keep running after this.

### Step 2 — Create the giveaway record (end of giveaway)

```
/giveaway_create
  name: DeFCoN Spring Giveaway
  giveaway_channel: #giveaway
  entry_channel: #submissions
  start: 01.07.2026 00:00
  end: 31.07.2026 23:59
  winners: 3
  require_attachment: True        (optional: only posts with image/screenshot count)
  min_account_days: 30            (optional: account must be at least 30 days old)
  required_role: @Verified        (optional: only users with this role can enter)
  winner_dm_text: Congratulations {user}! You won the {giveaway}! Please DM us within 48 hours.
```

The bot replies with a summary and assigns a giveaway ID (e.g. `1`).

### Step 3 — Scan the entry channel

```
/scan giveaway_id:1
```

The bot reads all messages in the entry channel between the start and end date.
Filters are applied automatically (attachment, account age, role).
Results show how many entries were found, how many unique participants are eligible, and how many were filtered out.

### Step 4 — Draw a winner

```
/draw giveaway_id:1
```

The bot randomly selects a winner, weighted by number of entries (more posts = more lottery tickets).
You see the user tag, a direct link to their post, a content preview, and the `winner_id` for the next steps.

### Step 5 — Review and decide

**Eligible:**
```
/approve winner_id:1 note:"You win a plant voucher worth €30!"
```
→ Winner announcement is posted in the giveaway channel, user is tagged, DM is sent automatically (if configured).

**Not eligible:**
```
/reject winner_id:1 reason:"Post does not show our content"
```
→ User is excluded from this giveaway, `/draw` will pick the next candidate.

### Step 6 — Draw remaining winners

Repeat `/draw giveaway_id:1` until all winner slots are filled.
Already approved and rejected users are automatically excluded from future draws.

---

## 8. Commands Overview

| Command | Permission | Description |
|---|---|---|
| `/giveaway_announce` | Administrator | Post a giveaway announcement embed |
| `/giveaway_create` | Administrator | Create a giveaway record for evaluation |
| `/giveaway_list` | Administrator | List all giveaways |
| `/scan` | Administrator | Read entry channel and apply filters |
| `/draw` | Administrator | Draw the next winner |
| `/approve` | Administrator | Confirm winner → announcement + DM |
| `/reject` | Administrator | Reject winner → next draw becomes possible |
| `/stats` | Manage Messages | Statistics, top participants, winner overview |
| `/disqualify` | Administrator | Manually disqualify a single entry |

---

## 9. How the Draw Works

- Every post in the entry channel counts as **one lottery ticket**
- A user with 5 posts has 5× the chance of winning compared to a user with 1 post
- Within one giveaway, **no user can win more than once**
- Rejected users are also excluded from future draws in the same giveaway
- The draw uses Python's `random.choice()` over the full weighted pool — verifiable in the source code

---

## 10. DM Text Placeholders

| Placeholder | Replaced with |
|---|---|
| `{user}` | The winner's display name |
| `{giveaway}` | The name of the giveaway |

**Example:**
```
Congratulations {user}! 🎉 You won the {giveaway}! Please DM us within 48 hours to claim your prize.
```

---

## 11. Direct Database Access (optional)

```bash
sqlite3 /opt/giveaway_bot/giveaway.db

-- All eligible entries with ticket count:
SELECT username, COUNT(*) AS tickets
FROM entries WHERE giveaway_id=1 AND status='eligible'
GROUP BY user_id ORDER BY tickets DESC;

-- All winners:
SELECT * FROM winners WHERE giveaway_id=1;

.quit
```

---

## 12. Updating the Bot

```bash
cd /opt/giveaway_bot
git pull
# Restart the bot (or the systemd service)
```
