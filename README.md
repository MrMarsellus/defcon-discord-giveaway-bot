# DeFCoN Discord Giveaway Bot

A Discord bot for running transparent, fair community giveaways. Winners are drawn randomly from user submissions in a designated entry channel — weighted by number of posts (more posts = more entries), with no user able to win more than once per giveaway.

## Concept

The bot does **not run continuously in the background**.  
It is started when needed, typically at the **end of the giveaway**, reads the entry channel retroactively, applies filters, and draws winners.

An always-on setup via `systemd` is optional, but not required for the intended workflow.

---

## 1. Requirements

- Ubuntu 20.04 / 22.04 / 24.04
- Python 3.10+
- `discord.py >= 2.3`
- `python-dotenv`
- Discord Bot Token (Developer Portal)

---

## 2. Creating the Discord Bot

1. Go to [https://discord.com/developers/applications](https://discord.com/developers/applications)
2. Select **New Application** and enter a name
3. Open **Bot** and click **Add Bot**
4. Copy the bot token (needed for `.env`)
5. Under **Privileged Gateway Intents**, enable:
   - **Server Members Intent**
   - **Message Content Intent**
6. Open **OAuth2 → URL Generator**
   - Scopes: `bot`, `applications.commands`
   - Bot Permissions: `Send Messages`, `Read Message History`, `Manage Messages`, `Embed Links`, `Mention Everyone`
7. Open the generated link and invite the bot to your server

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

# Install dependencies
pip install "discord.py>=2.3" python-dotenv

# Configure environment
cp .env.example .env
nano .env   # fill in your values
```

If you use a `requirements.txt`, you can install dependencies with:

```bash
pip install -r requirements.txt
```

---

## 4. Configuration (`.env`)

| Variable | Description |
|---|---|
| `DISCORD_TOKEN` | Your bot token from the Developer Portal |
| `GUILD_ID` | Your Discord server ID |
| `DB_PATH` | Path to the SQLite database file (default: `giveaway.db`) |
| `LOG_LEVEL` | Logging level: `DEBUG`, `INFO`, `WARNING`, `ERROR` |

To find your Guild ID, enable **Developer Mode** in Discord (`Settings → Advanced → Developer Mode`), then right-click your server and select **Copy ID**.

---

## 5. Starting the Bot

```bash
cd /opt/giveaway_bot
source venv/bin/activate
python bot.py
```

On first start, the bot logs that slash commands were synchronized. Wait 1–2 minutes for the commands to appear in Discord.

For background operation without a full `systemd` service, use `screen`:

```bash
screen -S giveaway
cd /opt/giveaway_bot
source venv/bin/activate
python bot.py
# Ctrl+A, D  -> detach session
# screen -r giveaway  -> reattach
```

---

## 6. Optional: systemd Service

A `systemd` service is optional. The bot does **not** need to run 24/7 for the intended giveaway workflow.

```bash
cp giveaway_bot.service /etc/systemd/system/

# Adjust the user and paths if needed
nano /etc/systemd/system/giveaway_bot.service

systemctl daemon-reload
systemctl enable giveaway_bot
systemctl start giveaway_bot

# Check status
systemctl status giveaway_bot
journalctl -u giveaway_bot -f
```

Make sure the service file matches your actual installation path, virtual environment path, bot filename, and Linux user.

---

## 7. Giveaway Workflow

### Step 1 — Post the announcement

```text
/giveaway_announce
  giveaway_channel: #giveaway
  entry_channel: #submissions
  title: DeFCoN Spring Giveaway
  description: Share our tweet and post the link here to enter!
  end_date: 31.07.2026 23:59
  color: green
```

The bot posts an announcement embed in the giveaway channel.  
The bot does **not** need to keep running after this.

### Step 2 — Create the giveaway record

```text
/giveaway_create
  name: DeFCoN Spring Giveaway
  giveaway_channel: #giveaway
  entry_channel: #submissions
  start: 01.07.2026 00:00
  end: 31.07.2026 23:59
  winners: 3
  require_attachment: True
  min_account_days: 30
  required_role: @Verified
  winner_dm_text: Congratulations {user}! You won the {giveaway}! Please DM us within 48 hours.
```

The bot replies with a summary and assigns a giveaway ID, for example `1`.

Notes:
- Date format must always be `DD.MM.YYYY HH:MM`
- Dates are parsed and stored as UTC
- Optional filters can be left empty or set to default values

### Step 3 — Scan the entry channel

```text
/scan giveaway_id:1
```

The bot reads all messages in the entry channel between the configured start and end date.  
Filters are applied automatically, including attachment requirement, minimum account age, and required role.

The result shows:
- how many new entries were stored
- how many messages were skipped because they already existed in the database
- how many entries were filtered out
- how many eligible entries remain
- how many unique participants are eligible

### Step 4 — Draw a winner

```text
/draw giveaway_id:1
```

The bot randomly selects the next winner, weighted by number of eligible entries.  
You see the user mention, a direct link to the post, a preview of the post content, and the `winner_id` for the next step.

### Step 5 — Review and decide

**Eligible:**
```text
/approve winner_id:1 note:"You win a plant voucher worth €30!"
```

The winner is confirmed, announced in the giveaway channel, and optionally contacted by DM if `winner_dm_text` was configured.

**Not eligible:**
```text
/reject winner_id:1 reason:"Post does not show our content"
```

The user is rejected for this giveaway and will be skipped in future draws for the same giveaway.

### Step 6 — Draw remaining winners

Repeat:

```text
/draw giveaway_id:1
```

until all winner slots are filled.

Approved and rejected users are automatically excluded from future draws in the same giveaway.

---

## 8. Commands Overview

| Command | Permission | Description |
|---|---|---|
| `/giveaway_announce` | Administrator | Post a giveaway announcement embed |
| `/giveaway_create` | Administrator | Create a giveaway record for evaluation |
| `/giveaway_list` | Administrator | List all giveaways |
| `/scan` | Administrator | Read the entry channel and apply filters |
| `/draw` | Administrator | Draw the next winner |
| `/approve` | Administrator | Confirm winner and post announcement |
| `/reject` | Administrator | Reject winner and allow the next draw |
| `/stats` | Manage Messages | Show statistics, top participants, and winner overview |
| `/disqualify` | Administrator | Manually disqualify a single entry |

---

## 9. How the Draw Works

- Every eligible post in the entry channel counts as **one lottery ticket**
- A user with 5 eligible posts has 5 times the chance of winning compared to a user with 1 eligible post
- Within one giveaway, **no user can win more than once**
- Rejected users are also excluded from future draws in the same giveaway
- Disqualified entries are not part of the draw pool
- The draw uses Python's `random.choice()` over the full weighted pool of eligible entries

---

## 10. Filters and Eligibility

The bot can apply the following filters during `/scan`:

- **Attachment required**: only posts with an attachment or embed are eligible
- **Minimum account age**: users must have an account age equal to or above the configured number of days
- **Required role**: only users with a specific Discord role are eligible

Entries that fail a filter are stored in the database with status `disqualified` and a reason.

An entry can also be disqualified manually later with:

```text
/disqualify message_id:123456789012345678 reason:"Spam or irrelevant post"
```

---

## 11. DM Text Placeholders

| Placeholder | Replaced with |
|---|---|
| `{user}` | The winner's display name |
| `{giveaway}` | The name of the giveaway |

Example:

```text
Congratulations {user}! 🎉 You won the {giveaway}! Please DM us within 48 hours to claim your prize.
```

---

## 12. Statistics and Review

Use:

```text
/stats giveaway_id:1
```

to view:

- giveaway status
- whether the giveaway has already been scanned
- winners drawn vs. total winners
- total entries
- eligible entries
- disqualified entries
- unique eligible participants
- configured options
- top participants by number of eligible tickets
- all drawn winners with status and post links

---

## 13. Direct Database Access (optional)

```bash
sqlite3 /opt/giveaway_bot/giveaway.db
```

Example queries:

```sql
-- All eligible entries with ticket count
SELECT username, COUNT(*) AS tickets
FROM entries
WHERE giveaway_id=1 AND status='eligible'
GROUP BY user_id
ORDER BY tickets DESC;

-- All winners
SELECT * FROM winners WHERE giveaway_id=1;
```

Exit SQLite:

```sql
.quit
```

---

## 14. Updating the Bot

```bash
cd /opt/giveaway_bot
git pull

# Restart the bot manually
# or restart the systemd service if you use one
```

If you changed slash commands or command descriptions, restart the bot and allow a short delay for Discord to refresh the command list.
