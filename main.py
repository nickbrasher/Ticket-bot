import asyncio
import io
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

# Read variables from the .env file into the environment.
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

log = logging.getLogger("ticketbot")

# Each ticket channel's topic records who owns it, e.g. "Ticket owner: 123456789".
# Discord stores the topic for us, so we can find open tickets without a database,
# and it still works after the bot restarts.
TICKET_TOPIC_PREFIX = "Ticket owner: "

# Stops two clicks arriving at the same moment from both passing the
# "already has a ticket?" check and creating two channels.
ticket_lock = asyncio.Lock()

# How long the "Deleting in N seconds..." countdown runs before a closed ticket is deleted.
CLOSE_COUNTDOWN_SECONDS = 5

# IDs of ticket channels whose countdown is running, so a second click doesn't start another.
closing_channels: set[int] = set()


# ---------------------------------------------------------------------------
# Per-server settings, saved by /setup
# ---------------------------------------------------------------------------

# Settings live in a small JSON file next to this script, keyed by server ID:
# {"<guild id>": {"staff_role_id": ..., "category_id": ..., "log_channel_id": ...}}
CONFIG_PATH = Path(__file__).with_name("config.json")


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text())
    except json.JSONDecodeError as e:
        raise SystemExit(f"{CONFIG_PATH.name} is not valid JSON ({e}). Fix or delete it, then start the bot again.")


def save_config():
    # Write to a temporary file first, then swap it in. If the bot crashes
    # mid-write, the old config.json is still intact.
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(config, indent=2))
    tmp.replace(CONFIG_PATH)


config = load_config()


def guild_settings(guild: discord.Guild) -> dict:
    return config.get(str(guild.id), {})


def get_staff_role(guild: discord.Guild) -> discord.Role | None:
    return guild.get_role(guild_settings(guild).get("staff_role_id", 0))


def get_ticket_category(guild: discord.Guild) -> discord.CategoryChannel | None:
    channel = guild.get_channel(guild_settings(guild).get("category_id", 0))
    return channel if isinstance(channel, discord.CategoryChannel) else None


def get_log_channel(guild: discord.Guild) -> discord.TextChannel | None:
    channel = guild.get_channel(guild_settings(guild).get("log_channel_id", 0))
    return channel if isinstance(channel, discord.TextChannel) else None


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


async def reply_privately(interaction: discord.Interaction, text: str):
    """Send a message only the clicker can see, whether or not we've already responded."""
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
    except discord.HTTPException:
        # The interaction expired or Discord is having trouble. The error is already logged.
        pass


async def handle_error(interaction: discord.Interaction, error: Exception):
    """Turn an unexpected error into a clear message for the user, and log the details for us."""
    if isinstance(error, discord.Forbidden):
        await reply_privately(
            interaction,
            "I'm missing a permission I need for that. Ask an admin to make sure my role has "
            "**Manage Channels**, **Manage Roles**, **Send Messages** and **Attach Files**.",
        )
    else:
        log.error("Error handling interaction from %s", interaction.user, exc_info=error)
        await reply_privately(interaction, "Something went wrong on my end. Please try again in a moment.")


class TicketView(discord.ui.View):
    """Base for our buttons: sends errors to handle_error instead of only printing them."""

    def __init__(self):
        # timeout=None plus a fixed custom_id on each button makes the view *persistent*:
        # the buttons keep working even after the bot restarts.
        super().__init__(timeout=None)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item):
        await handle_error(interaction, error)


class TicketPanel(TicketView):
    """The message with the "Open Ticket" button that /panel posts."""

    @discord.ui.button(label="Open Ticket", emoji="🎫", style=discord.ButtonStyle.primary, custom_id="ticket:open")
    async def open_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await open_ticket(interaction)


class TicketControls(TicketView):
    """The "Close Ticket" button posted inside each ticket channel."""

    @discord.ui.button(label="Close Ticket", emoji="🔒", style=discord.ButtonStyle.danger, custom_id="ticket:close")
    async def close_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await close_ticket(interaction)


class TicketBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        # Needed to read message text for transcripts. This is a "privileged" intent,
        # so it must also be switched on in the Developer Portal (Bot > Message Content Intent).
        intents.message_content = True
        # The prefix is unused (we only use slash commands), but commands.Bot requires one.
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        # Runs once, before the bot connects.
        # Re-register the buttons so messages posted before a restart still work.
        self.add_view(TicketPanel())
        self.add_view(TicketControls())
        # Register our slash commands with Discord.
        synced = await self.tree.sync()
        print(f"Synced {len(synced)} slash command(s)")

    async def on_ready(self):
        print(f"Logged in as {self.user} (ID: {self.user.id})")


bot = TicketBot()


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    # Errors raised inside a command arrive wrapped. Unwrap them so Forbidden etc. are recognised.
    if isinstance(error, app_commands.CommandInvokeError):
        error = error.original
    await handle_error(interaction, error)


# ---------------------------------------------------------------------------
# Opening tickets
# ---------------------------------------------------------------------------


def find_open_ticket(guild: discord.Guild, user: discord.abc.User) -> discord.TextChannel | None:
    """Return the user's open ticket channel in this server, if they have one."""
    for channel in guild.text_channels:
        if channel.topic == f"{TICKET_TOPIC_PREFIX}{user.id}":
            return channel
    return None


def ticket_channel_name(user: discord.abc.User) -> str:
    # Channel names may only contain lowercase letters, numbers, "-" and "_".
    slug = re.sub(r"[^a-z0-9_-]", "", user.name.lower())
    return f"ticket-{slug or user.id}"


async def open_ticket(interaction: discord.Interaction):
    """Shared by the /ticket command and the panel button."""
    guild = interaction.guild
    user = interaction.user

    staff_role = get_staff_role(guild)
    if staff_role is None:
        await interaction.response.send_message(
            "Tickets aren't set up yet. An admin needs to run `/setup` and choose a staff role.",
            ephemeral=True,
        )
        return

    # Creating a channel can take a moment, and Discord wants a response within
    # 3 seconds. Deferring shows "Bot is thinking..." and gives us more time.
    await interaction.response.defer(ephemeral=True, thinking=True)

    async with ticket_lock:
        existing = find_open_ticket(guild, user)
        if existing:
            await interaction.followup.send(f"You already have an open ticket: {existing.mention}", ephemeral=True)
            return

        # Hide the channel from everyone, then let in only the user, staff and the bot.
        member_access = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True, attach_files=True
        )
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            user: member_access,
            staff_role: member_access,
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        }

        try:
            channel = await guild.create_text_channel(
                name=ticket_channel_name(user),
                # None (no category set, or it was deleted) puts the channel at the top of the server.
                category=get_ticket_category(guild),
                topic=f"{TICKET_TOPIC_PREFIX}{user.id}",
                overwrites=overwrites,
                reason=f"Ticket opened by {user}",
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "I don't have permission to create channels. Ask an admin to give me Manage Channels and Manage Roles.",
                ephemeral=True,
            )
            return
        except discord.HTTPException as e:
            # Most often the category is full: Discord allows 50 channels per category.
            log.warning("Couldn't create ticket channel: %s", e)
            await interaction.followup.send(
                "Discord wouldn't let me create your ticket channel. If the ticket category is full "
                "(50 channels max), staff need to close some tickets. Otherwise, please try again.",
                ephemeral=True,
            )
            return

    await channel.send(
        f"{user.mention} thanks for opening a ticket! {staff_role.mention} will be with you shortly.\n"
        "Please describe your issue here.",
        view=TicketControls(),
    )
    await interaction.followup.send(f"Your ticket has been created: {channel.mention}", ephemeral=True)


# ---------------------------------------------------------------------------
# Closing tickets
# ---------------------------------------------------------------------------


def ticket_owner_id(channel: discord.abc.GuildChannel) -> int | None:
    """Read the owner's user ID back out of a ticket channel's topic."""
    topic = getattr(channel, "topic", None) or ""
    if topic.startswith(TICKET_TOPIC_PREFIX):
        owner = topic.removeprefix(TICKET_TOPIC_PREFIX)
        if owner.isdigit():
            return int(owner)
    return None


def format_time(when: datetime) -> str:
    return when.strftime("%Y-%m-%d %H:%M:%S UTC")


async def build_transcript(channel: discord.TextChannel, owner_id: int, closed_by: discord.abc.User) -> tuple[str, int]:
    """Return the ticket's messages as plain text, oldest first, plus how many there were."""
    lines = [
        f"Transcript of #{channel.name}",
        f"Owner ID: {owner_id}",
        f"Closed by: {closed_by} ({closed_by.id})",
        f"Closed at: {format_time(datetime.now(timezone.utc))}",
        "",
    ]
    count = 0
    async for message in channel.history(limit=None, oldest_first=True):
        count += 1
        lines.append(f"[{format_time(message.created_at)}] {message.author} ({message.author.id}): {message.content}")
        # Files and embeds aren't text, so just note that they were there.
        for attachment in message.attachments:
            lines.append(f"    [attachment: {attachment.filename}]")
        for embed in message.embeds:
            lines.append(f"    [embed: {embed.title or embed.description or 'no text'}]")
    return "\n".join(lines), count


async def save_transcript(
    channel: discord.TextChannel, log_channel: discord.TextChannel, owner_id: int, closed_by: discord.abc.User
):
    text, count = await build_transcript(channel, owner_id, closed_by)
    embed = discord.Embed(title="Ticket closed", color=discord.Color.red(), timestamp=datetime.now(timezone.utc))
    embed.add_field(name="Ticket", value=f"#{channel.name}")
    embed.add_field(name="Owner", value=f"<@{owner_id}>")
    embed.add_field(name="Closed by", value=closed_by.mention)
    embed.add_field(name="Messages", value=str(count))
    file = discord.File(io.BytesIO(text.encode("utf-8")), filename=f"{channel.name}-transcript.txt")
    await log_channel.send(embed=embed, file=file)


async def close_ticket(interaction: discord.Interaction):
    """Called by the Close Ticket button: check who clicked, save a transcript, count down, then delete."""
    channel = interaction.channel
    user = interaction.user
    guild = interaction.guild

    owner_id = ticket_owner_id(channel)
    if owner_id is None:
        await interaction.response.send_message("This doesn't look like a ticket channel.", ephemeral=True)
        return

    staff_role = get_staff_role(guild)
    is_owner = user.id == owner_id
    is_staff = staff_role is not None and staff_role in user.roles
    if not (is_owner or is_staff):
        await interaction.response.send_message("Only staff or the ticket owner can close this ticket.", ephemeral=True)
        return

    # Ignore extra clicks while a close is already in progress.
    if channel.id in closing_channels:
        await interaction.response.send_message("This ticket is already closing.", ephemeral=True)
        return
    closing_channels.add(channel.id)

    try:
        # Reading a long ticket's history can take more than Discord's 3-second limit, so respond first.
        await interaction.response.send_message(f"🔒 Ticket closed by {user.mention}. Saving transcript...")

        log_channel = get_log_channel(guild)
        if log_channel is None:
            note = "No log channel is set up, so no transcript was saved. "
        else:
            try:
                await save_transcript(channel, log_channel, owner_id, user)
                note = f"Transcript saved to {log_channel.mention}. "
            except discord.HTTPException as e:
                # Don't delete the ticket if we couldn't save it. Nothing gets lost this way.
                log.warning("Couldn't save transcript for #%s: %s", channel.name, e)
                await interaction.edit_original_response(
                    content=f"⚠️ I couldn't save the transcript to {log_channel.mention}, so I haven't deleted "
                    "this ticket. Ask an admin to check that I can **Send Messages** and **Attach Files** "
                    "there, then try closing again."
                )
                return

        # Edit the message once a second so everyone sees the countdown.
        for seconds in range(CLOSE_COUNTDOWN_SECONDS, 0, -1):
            await interaction.edit_original_response(
                content=f"🔒 Ticket closed by {user.mention}. {note}Deleting in {seconds} seconds..."
            )
            await asyncio.sleep(1)
        await channel.delete(reason=f"Ticket closed by {user}")
    except discord.NotFound:
        # Someone deleted the channel by hand during the countdown. Nothing left to do.
        pass
    except discord.Forbidden:
        await channel.send("I don't have permission to delete this channel. Ask an admin to give me Manage Channels.")
    finally:
        closing_channels.discard(channel.id)


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------


@bot.tree.command(name="ping", description="Check that the bot is responding")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("Pong!")


@bot.tree.command(name="ticket", description="Open a private support ticket")
@app_commands.guild_only()
async def ticket(interaction: discord.Interaction):
    await open_ticket(interaction)


@bot.tree.command(name="panel", description="Post a message with an Open Ticket button in this channel")
@app_commands.guild_only()
# Only members with Manage Server see this command by default (admins can change it in Server Settings > Integrations).
@app_commands.default_permissions(manage_guild=True)
async def panel(interaction: discord.Interaction):
    if get_staff_role(interaction.guild) is None:
        await interaction.response.send_message("Run `/setup` first so tickets have a staff role.", ephemeral=True)
        return
    embed = discord.Embed(
        title="Need help?",
        description="Click the button below to open a private ticket with our staff.",
        color=discord.Color.blurple(),
    )
    await interaction.channel.send(embed=embed, view=TicketPanel())
    await interaction.response.send_message("Ticket panel posted.", ephemeral=True)


def describe_settings(guild: discord.Guild) -> str:
    staff_role = get_staff_role(guild)
    category = get_ticket_category(guild)
    log_channel = get_log_channel(guild)
    return (
        "**Ticket settings**\n"
        f"Staff role: {staff_role.mention if staff_role else 'not set (tickets can’t be opened)'}\n"
        f"Ticket category: {category.mention if category else 'not set (tickets go at the top of the server)'}\n"
        f"Log channel: {log_channel.mention if log_channel else 'not set (transcripts aren’t saved)'}"
    )


@bot.tree.command(name="setup", description="Choose the staff role, ticket category and transcript log channel")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.describe(
    staff_role="Role that can see and close every ticket",
    category="Category new ticket channels are created in",
    log_channel="Channel where transcripts of closed tickets are posted",
)
async def setup(
    interaction: discord.Interaction,
    staff_role: discord.Role | None = None,
    category: discord.CategoryChannel | None = None,
    log_channel: discord.TextChannel | None = None,
):
    guild = interaction.guild
    me = guild.me

    # Check everything before saving anything, so a bad option doesn't leave half the settings changed.
    problems = []
    if staff_role is not None and staff_role.is_default():
        problems.append("The staff role can't be @everyone, or every member would see every ticket.")
    if category is not None and not category.permissions_for(me).manage_channels:
        problems.append(f"I need **Manage Channels** in {category.mention} to create tickets there.")
    if log_channel is not None:
        perms = log_channel.permissions_for(me)
        if not (perms.view_channel and perms.send_messages and perms.attach_files and perms.embed_links):
            problems.append(
                f"I need **View Channel**, **Send Messages**, **Embed Links** and **Attach Files** in "
                f"{log_channel.mention} to post transcripts."
            )
    if problems:
        await interaction.response.send_message(
            "Nothing was saved:\n- " + "\n- ".join(problems), ephemeral=True
        )
        return

    settings = config.setdefault(str(guild.id), {})
    if staff_role is not None:
        settings["staff_role_id"] = staff_role.id
    if category is not None:
        settings["category_id"] = category.id
    if log_channel is not None:
        settings["log_channel_id"] = log_channel.id
    save_config()

    message = describe_settings(guild)
    if staff_role is None and category is None and log_channel is None:
        message += (
            "\n\nTo change these, type `/setup` and pick **staff_role**, **category** or **log_channel** "
            "from the options above the message box before pressing Enter."
        )
    else:
        message = "Saved.\n\n" + message
    if not (me.guild_permissions.manage_channels and me.guild_permissions.manage_roles):
        message += "\n\n⚠️ My role is missing **Manage Channels** or **Manage Roles**, so I can't create tickets yet."
    # Show the role and channels as mentions without pinging anyone.
    await interaction.response.send_message(message, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN is not set. Copy .env.example to .env and add your token.")
    try:
        bot.run(TOKEN)
    except discord.PrivilegedIntentsRequired:
        raise SystemExit(
            "Discord refused the connection because the Message Content Intent is off.\n"
            "Turn it on at https://discord.com/developers/applications -> your app -> Bot -> "
            "Privileged Gateway Intents -> Message Content Intent, then start the bot again."
        )
