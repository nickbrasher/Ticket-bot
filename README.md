# Ticket Bot

A Discord support-ticket bot written in Python with [discord.py](https://discordpy.readthedocs.io/) 2.x, using slash commands and buttons.

Members open a private ticket channel that only they and your staff can see. When the ticket is closed, the bot saves a text transcript to a log channel and then deletes the ticket channel.

## Features

- **Open tickets** with `/ticket`, or with an "Open Ticket" button that admins post using `/panel`
- **Private channels** named `ticket-<username>`, visible only to the member, the staff role and the bot
- **One open ticket per member**, so duplicate tickets are refused
- **Close Ticket button**, usable only by the ticket owner or staff, with a short countdown before the channel is deleted
- **Transcripts**: every closed ticket is posted to a log channel as a `.txt` file. If saving fails, the ticket isn't deleted.
- **Per-server settings** with `/setup`: staff role, ticket category and log channel
- **Buttons keep working after a restart** (persistent views)
- **Clear error messages** for missing permissions and other problems, so users don't just see "This interaction failed"

## Commands

| Command | Who can use it | What it does |
| --- | --- | --- |
| `/ticket` | Everyone | Open a private ticket |
| `/panel` | Manage Server | Post a message with an "Open Ticket" button in the current channel |
| `/setup` | Administrator | Show the current settings, or set `staff_role`, `category` and `log_channel` |
| `/ping` | Everyone | Check that the bot is responding |

## Setup

You need Python 3.10 or newer.

1. **Create the bot.** At the [Discord Developer Portal](https://discord.com/developers/applications), create an application and open its **Bot** page:
   - Click **Reset Token** and copy the token.
   - Under **Privileged Gateway Intents**, turn on **Message Content Intent**. Transcripts need it.

2. **Install.**

   ```bash
   git clone https://github.com/nickbrasher/Ticket-bot.git
   cd Ticket-bot
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   cp .env.example .env
   ```

   Then put your token in `.env`:

   ```
   DISCORD_TOKEN=your-bot-token-here
   ```

3. **Invite the bot** to your server. Replace `YOUR_CLIENT_ID` with your application ID, shown on the portal's **General Information** page:

   ```
   https://discord.com/oauth2/authorize?client_id=YOUR_CLIENT_ID&scope=bot+applications.commands&permissions=268553232
   ```

   This link asks for these permissions: Manage Channels, Manage Roles, View Channels, Send Messages, Embed Links, Attach Files and Read Message History.

4. **Run it.**

   ```bash
   python main.py
   ```

5. **Configure it** in Discord. Run `/setup` and pick a staff role, a category for tickets and a log channel. Then run `/panel` in the channel where members should open tickets.

## How it works

- **No database.** Each ticket channel's topic stores its owner (`Ticket owner: <user id>`), so the bot can find open tickets by looking at channel topics. This keeps working after a restart.
- **Settings file.** `/setup` saves each server's settings to `config.json`. The bot writes a temporary file first and then swaps it in, so a crash can't leave a half-written file.
- **Two quick clicks can't create two tickets.** An `asyncio.Lock` makes ticket creation run one at a time.
- **Persistent buttons.** The buttons have fixed `custom_id`s and are registered again at startup, so panels posted before a restart still work.

## Project files

```
main.py           the bot
requirements.txt  Python dependencies
.env.example      template for .env (your token goes in .env, which is not committed)
config.json       created by /setup (not committed)
```
