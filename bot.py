import os
import re
import random
import asyncio
from typing import Optional, Literal
from datetime import timedelta
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

try:
    from google import genai
except ImportError:
    genai = None

# ==============================================================================
# 1. VIEW COMPONENTS (TICKETS & GAMES)
# ==============================================================================

class TicketButtonView(discord.ui.View):
    """Persistent view for handling support tickets."""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Create Support Ticket", style=discord.ButtonStyle.primary, custom_id="open_ticket_btn", emoji="🎫")
    async def create_ticket_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True)
        }
        ticket_chan = await guild.create_text_channel(name=f"ticket-{interaction.user.name}", overwrites=overwrites)
        await interaction.response.send_message(f"Ticket generated. Navigate to {ticket_chan.mention}", ephemeral=True)


class GameLaunchView(discord.ui.View):
    """View sent to user's DMs to trigger the main server guessing game."""
    def __init__(self, target_channel, secret_number, guild):
        super().__init__(timeout=120)
        self.target_channel = target_channel
        self.secret_number = secret_number
        self.guild = guild

    @discord.ui.button(label="Initialize Server Match Grid", style=discord.ButtonStyle.success)
    async def start_game_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.target_channel.set_permissions(self.guild.default_role, send_messages=True)
        await self.target_channel.send("🎯 **The Secret Guessing Game has started!** Guess the number (1-100) right here in this channel!")
        await interaction.response.edit_message(content="Game engine deployed. Live updates streaming to target channel.", view=None)
        
        bot = interaction.client
        
        def check(m):
            return m.channel.id == self.target_channel.id and not m.author.bot

        while True:
            try:
                msg = await bot.wait_for("message", check=check, timeout=300)
                if msg.content.isdigit() and int(msg.content) == self.secret_number:
                    await msg.add_reaction("✅")
                    await self.target_channel.send(f"🎉 **{msg.author.mention}** guessed the exact correct number: **{self.secret_number}**! Locking channel...")
                    await self.target_channel.set_permissions(self.guild.default_role, send_messages=False)
                    break
            except asyncio.TimeoutError:
                await self.target_channel.send("Game closed due to structural inactivity timeout.")
                break


class ConfirmView(discord.ui.View):
    """Simple confirm/cancel view for user actions."""
    def __init__(self, timeout=30):
        super().__init__(timeout=timeout)
        self.confirmed = None

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success, emoji="✅")
    async def confirm_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = True
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, emoji="❌")
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = False
        await interaction.response.defer()
        self.stop()

# ==============================================================================
# 2. MAIN BOT CLIENT DEFINITION
# ==============================================================================

class AdvancedDiscordBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        intents.reactions = True
        super().__init__(command_prefix="!", intents=intents)
        
        if genai:
            self.ai_client = genai.Client()
        else:
            self.ai_client = None
        
        self.db_welcome = {}
        self.db_afk = {}
        self.db_no_prefix = set()
        self.db_autoresponse = {}
        self.db_autoreaction = {}
        self.db_warns = {}

    async def setup_hook(self):
        self.add_view(TicketButtonView())
        
        await self.add_cog(WelcomeCog(self))
        await self.add_cog(ModerationCog(self))
        await self.add_cog(GamesCog(self))
        await self.add_cog(OwnerCog(self))
        await self.add_cog(UtilityCog(self))
        
        await self.tree.sync()
        print("✅ Application (/) tree layout structures synced cleanly.")

bot = AdvancedDiscordBot()

# ==============================================================================
# 3. INTERCEPTIVE EVENTS
# ==============================================================================

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user.name} ({bot.user.id})")
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="/help"))


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    content = message.content
    content_lower = content.lower()

    is_owner = message.guild.owner_id == message.author.id
    has_np = message.author.id in bot.db_no_prefix or is_owner

    if has_np and content_lower.startswith("ai ") and bot.ai_client:
        prompt = content[3:]
        try:
            response = bot.ai_client.models.generate_content(
                model='gemini-1.5-flash',
                contents=prompt
            )
            await message.reply(response.text)
            return
        except Exception as e:
            await message.reply(f"❌ Failed to parse analytical sequence response through Gemini AI.\n**Error**: {str(e)}")
            return

    if message.author.id in bot.db_afk:
        del bot.db_afk[message.author.id]
        await message.reply("👋 Welcome back! Your AFK status has been completely cleared.", delete_after=5)

    for mention in message.mentions:
        if mention.id in bot.db_afk:
            data = bot.db_afk[mention.id]
            await message.reply(f"⚠️ {mention.name} is currently AFK: **{data['reason']}**")

    if content_lower in bot.db_autoresponse:
        await message.reply(bot.db_autoresponse[content_lower])
    if content_lower in bot.db_autoreaction:
        try:
            await message.add_reaction(bot.db_autoreaction[content_lower])
        except Exception:
            pass

    if len(content) > 10 and content.isupper():
        await message.delete()
        await message.channel.send(f"{message.author.mention}, disable caps-lock layout strings.", delete_after=3)
        return

    invite_regex = r"(https?://)?(www\.)?(discord\.(gg|io|me|li)|discordapp\.com/invite)/[^\s]+"
    link_regex = r"https?://[^\s]+"

    if re.search(invite_regex, content) or re.search(link_regex, content):
        await message.delete()
        await message.channel.send(f"{message.author.mention}, payload links/invites are blocked here.", delete_after=3)
        return

    if len(message.mentions) > 4:
        await message.delete()
        await message.channel.send(f"{message.author.mention}, do not exceed safe server user mention thresholds.", delete_after=3)
        return

    await bot.process_commands(message)

# ==============================================================================
# 4. EXECUTABLE COG MODULE ARRAYS
# ==============================================================================

class WelcomeCog(commands.GroupCog, name="welcome"):
    """Welcome system configuration commands."""
    def __init__(self, bot: AdvancedDiscordBot):
        self.bot = bot

    def _get_config(self, guild_id):
        if guild_id not in self.bot.db_welcome:
            self.bot.db_welcome[guild_id] = {"channel": None, "text": "Welcome {user} to {server}!", "image": None}
        return self.bot.db_welcome[guild_id]

    @app_commands.command(name="channel", description="Configure active landing greeting text-channel grid")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def set_channel(self, interaction: discord.Interaction, target: discord.TextChannel):
        """Set the welcome message channel."""
        cfg = self._get_config(interaction.guild_id)
        cfg["channel"] = target.id
        embed = discord.Embed(
            title="✅ Welcome Channel Updated",
            description=f"Welcome messages will now be sent to {target.mention}",
            color=discord.Color.green()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="text", description="Configure customized text layout greetings payload array")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def set_text(self, interaction: discord.Interaction, message: str):
        """Set the welcome message text. Use {user} and {server} as placeholders."""
        cfg = self._get_config(interaction.guild_id)
        cfg["text"] = message
        embed = discord.Embed(
            title="✅ Welcome Text Updated",
            description=f"New welcome text:\n```{message}```",
            color=discord.Color.green()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="image", description="Bind custom image canvas graphics element via direct asset hyperlink")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def set_image(self, interaction: discord.Interaction, url: str):
        """Set the welcome message image URL."""
        cfg = self._get_config(interaction.guild_id)
        cfg["image"] = url
        embed = discord.Embed(
            title="✅ Welcome Image Updated",
            description="Welcome image has been set!",
            color=discord.Color.green()
        )
        if url:
            embed.set_image(url=url)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="test", description="Instantly dispatch structural rendering preview layout blocks")
    async def test_welcome(self, interaction: discord.Interaction):
        """Preview the current welcome message."""
        cfg = self._get_config(interaction.guild_id)
        parsed_text = cfg["text"].replace("{user}", interaction.user.mention).replace("{server}", interaction.guild.name)
        
        embed = discord.Embed(description=parsed_text, color=discord.Color.green())
        if cfg["image"]:
            embed.set_image(url=cfg["image"])
        embed.set_author(name="Welcome Preview", icon_url=interaction.user.display_avatar.url)
        await interaction.response.send_message(embed=embed, ephemeral=True)


class ModerationCog(commands.GroupCog, name="mod"):
    """Moderation management commands."""
    def __init__(self, bot: AdvancedDiscordBot):
        self.bot = bot

    @app_commands.command(name="kick", description="Execute drop operation kick against target node context mapping")
    @app_commands.checks.has_permissions(kick_members=True)
    async def kick_user(self, interaction: discord.Interaction, target: discord.Member, reason: Optional[str] = "No reason specified"):
        """Kick a user from the server."""
        try:
            await target.kick(reason=reason)
            embed = discord.Embed(
                title="✅ User Kicked",
                description=f"**User**: {target.mention}\n**Reason**: {reason}",
                color=discord.Color.orange()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("❌ I don't have permission to kick this user.", ephemeral=True)

    @app_commands.command(name="ban", description="Execute ban operation structure")
    @app_commands.checks.has_permissions(ban_members=True)
    async def ban_user(self, interaction: discord.Interaction, target: discord.Member, reason: Optional[str] = "No reason specified"):
        """Ban a user from the server."""
        try:
            await target.ban(reason=reason)
            embed = discord.Embed(
                title="✅ User Banned",
                description=f"**User**: {target.mention}\n**Reason**: {reason}",
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("❌ I don't have permission to ban this user.", ephemeral=True)

    @app_commands.command(name="mute", description="Mute a user by adding a muted role")
    @app_commands.checks.has_permissions(moderate_members=True)
    async def mute_user(self, interaction: discord.Interaction, target: discord.Member, duration: int, reason: Optional[str] = "No reason specified"):
        """Mute a user for a specified number of minutes."""
        try:
            mute_duration = timedelta(minutes=duration)
            await target.timeout(mute_duration, reason=reason)
            embed = discord.Embed(
                title="🔇 User Muted",
                description=f"**User**: {target.mention}\n**Duration**: {duration} minutes\n**Reason**: {reason}",
                color=discord.Color.yellow()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("❌ I don't have permission to mute this user.", ephemeral=True)

    @app_commands.command(name="warn", description="Issue a warning to a user")
    @app_commands.checks.has_permissions(moderate_members=True)
    async def warn_user(self, interaction: discord.Interaction, target: discord.Member, reason: str):
        """Warn a user with a logged reason."""
        if target.id not in self.bot.db_warns:
            self.bot.db_warns[target.id] = []
        
        self.bot.db_warns[target.id].append({
            "reason": reason,
            "mod": interaction.user.name,
            "time": interaction.created_at
        })
        
        warn_count = len(self.bot.db_warns[target.id])
        embed = discord.Embed(
            title="⚠️ User Warned",
            description=f"**User**: {target.mention}\n**Reason**: {reason}\n**Total Warnings**: {warn_count}",
            color=discord.Color.orange()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="warns", description="Check warnings for a user")
    @app_commands.checks.has_permissions(moderate_members=True)
    async def check_warns(self, interaction: discord.Interaction, target: discord.Member):
        """View all warnings for a user."""
        if target.id not in self.bot.db_warns or not self.bot.db_warns[target.id]:
            await interaction.response.send_message(f"✅ {target.mention} has no warnings.", ephemeral=True)
            return
        
        warns = self.bot.db_warns[target.id]
        embed = discord.Embed(
            title=f"⚠️ Warnings for {target.name}",
            color=discord.Color.orange()
        )
        
        for i, warn in enumerate(warns, 1):
            embed.add_field(
                name=f"Warning #{i}",
                value=f"**Reason**: {warn['reason']}\n**Moderator**: {warn['mod']}\n**Date**: {warn['time'].strftime('%Y-%m-%d %H:%M:%S')}",
                inline=False
            )
        
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="purge", description="Execute custom batch purging deletion array operations")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def purge_messages(self, interaction: discord.Interaction, amount: int, filter_type: Optional[Literal["bots", "links", "mentions"]] = None):
        """Delete a batch of messages with optional filtering."""
        await interaction.response.defer(ephemeral=True)
        
        def filter_check(m):
            if filter_type == "bots": return m.author.bot
            if filter_type == "links": return "http" in m.content
            if filter_type == "mentions": return len(m.mentions) > 0
            return True

        deleted = await interaction.channel.purge(limit=amount, check=filter_check)
        embed = discord.Embed(
            title="🧹 Messages Purged",
            description=f"Swept **{len(deleted)}** messages clean." + (f" (Filter: {filter_type})" if filter_type else ""),
            color=discord.Color.blue()
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="lock", description="Isolate channel routing to block standard group send configurations")
    @app_commands.checks.has_permissions(manage_channels=True)
    async def lock_channel(self, interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
        """Lock a channel to prevent messages."""
        target_ch = channel or interaction.channel
        await target_ch.set_permissions(interaction.guild.default_role, send_messages=False)
        embed = discord.Embed(
            title="🔒 Channel Locked",
            description=f"{target_ch.mention} has been secured and locked.",
            color=discord.Color.red()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="unlock", description="Restore baseline interaction capability attributes to public text fields")
    @app_commands.checks.has_permissions(manage_channels=True)
    async def unlock_channel(self, interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
        """Unlock a channel to allow messages."""
        target_ch = channel or interaction.channel
        await target_ch.set_permissions(interaction.guild.default_role, send_messages=True)
        embed = discord.Embed(
            title="🔓 Channel Unlocked",
            description=f"{target_ch.mention} write permissions returned to normal.",
            color=discord.Color.green()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class GamesCog(commands.GroupCog, name="game"):
    """Gaming and entertainment commands."""
    def __init__(self, bot: AdvancedDiscordBot):
        self.bot = bot

    @app_commands.command(name="guessgame", description="Initialize customized secret value DM bounding match setups")
    async def run_guessgame(self, interaction: discord.Interaction):
        """Start a guessing game (1-100)."""
        secret_number = random.randint(1, 100)
        target_channel = interaction.channel

        try:
            view = GameLaunchView(target_channel, secret_number, interaction.guild)
            await interaction.user.send(
                content=f"🎮 **[Game Manager Frame Engine]** Generated Target Vector Hidden: || **{secret_number}** ||\n\nConfirm configuration below:",
                view=view
            )
            embed = discord.Embed(
                title="🎮 Game Started!",
                description="Check your DMs to initialize the game!",
                color=discord.Color.blurple()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("❌ Unable to connect via DM. Verify authorization profile privacy blocks.", ephemeral=True)

    @app_commands.command(name="roll", description="Roll a dice with custom sides")
    async def roll_dice(self, interaction: discord.Interaction, sides: int = 6):
        """Roll a dice. Default 6 sides."""
        if sides < 2 or sides > 1000:
            await interaction.response.send_message("❌ Dice sides must be between 2 and 1000.", ephemeral=True)
            return
        
        result = random.randint(1, sides)
        embed = discord.Embed(
            title="🎲 Dice Roll",
            description=f"**Rolled {sides}-sided dice**: **{result}**",
            color=discord.Color.blurple()
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="flipcoin", description="Flip a coin")
    async def flip_coin(self, interaction: discord.Interaction):
        """Flip a coin (Heads/Tails)."""
        result = random.choice(["Heads", "Tails"])
        emoji = "🪙"
        embed = discord.Embed(
            title=f"{emoji} Coin Flip",
            description=f"Result: **{result}**",
            color=discord.Color.blurple()
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="rps", description="Play Rock-Paper-Scissors against the bot")
    async def rock_paper_scissors(self, interaction: discord.Interaction, choice: Literal["rock", "paper", "scissors"]):
        """Play Rock-Paper-Scissors."""
        bot_choice = random.choice(["rock", "paper", "scissors"])
        
        if choice == bot_choice:
            result = "It's a tie! 🤝"
            color = discord.Color.yellow()
        elif (choice == "rock" and bot_choice == "scissors") or \
             (choice == "paper" and bot_choice == "rock") or \
             (choice == "scissors" and bot_choice == "paper"):
            result = "You win! 🎉"
            color = discord.Color.green()
        else:
            result = "I win! 🤖"
            color = discord.Color.red()
        
        embed = discord.Embed(
            title="🎮 Rock-Paper-Scissors",
            description=f"**Your choice**: {choice}\n**My choice**: {bot_choice}\n\n{result}",
            color=color
        )
        await interaction.response.send_message(embed=embed)


class UtilityCog(commands.GroupCog, name="util"):
    """Utility and configuration commands."""
    def __init__(self, bot: AdvancedDiscordBot):
        self.bot = bot

    @app_commands.command(name="afk", description="Set yourself as AFK with a reason")
    async def set_afk(self, interaction: discord.Interaction, reason: str):
        """Mark yourself as AFK."""
        self.bot.db_afk[interaction.user.id] = {
            "reason": reason,
            "time": interaction.created_at
        }
        embed = discord.Embed(
            title="✌️ AFK Status Set",
            description=f"You're now AFK: **{reason}**",
            color=discord.Color.greyple()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="afklist", description="View all AFK users")
    async def afk_list(self, interaction: discord.Interaction):
        """List all users currently AFK."""
        if not self.bot.db_afk:
            await interaction.response.send_message("✅ No one is AFK right now!", ephemeral=True)
            return
        
        embed = discord.Embed(
            title="👤 AFK Users",
            color=discord.Color.greyple()
        )
        
        for user_id, data in self.bot.db_afk.items():
            user = interaction.guild.get_member(user_id)
            if user:
                embed.add_field(
                    name=user.name,
                    value=f"**Reason**: {data['reason']}",
                    inline=False
                )
        
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="autoresponse-add", description="Add an auto-response trigger")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def add_autoresponse(self, interaction: discord.Interaction, trigger: str, response: str):
        """Add a custom auto-response."""
        self.bot.db_autoresponse[trigger.lower()] = response
        embed = discord.Embed(
            title="✅ Auto-Response Added",
            description=f"**Trigger**: `{trigger}`\n**Response**: {response}",
            color=discord.Color.green()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="autoresponse-remove", description="Remove an auto-response trigger")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def remove_autoresponse(self, interaction: discord.Interaction, trigger: str):
        """Remove an auto-response."""
        if trigger.lower() in self.bot.db_autoresponse:
            del self.bot.db_autoresponse[trigger.lower()]
            await interaction.response.send_message(f"✅ Auto-response for `{trigger}` removed.", ephemeral=True)
        else:
            await interaction.response.send_message(f"❌ No auto-response found for `{trigger}`.", ephemeral=True)

    @app_commands.command(name="autoresponse-list", description="List all auto-responses")
    async def list_autoresponses(self, interaction: discord.Interaction):
        """View all auto-responses."""
        if not self.bot.db_autoresponse:
            await interaction.response.send_message("❌ No auto-responses configured.", ephemeral=True)
            return
        
        embed = discord.Embed(
            title="📋 Auto-Responses",
            color=discord.Color.blue()
        )
        
        for trigger, response in self.bot.db_autoresponse.items():
            embed.add_field(
                name=f"`{trigger}`",
                value=response,
                inline=False
            )
        
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="ping", description="Check bot latency")
    async def ping(self, interaction: discord.Interaction):
        """Check the bot's ping/latency."""
        latency = round(self.bot.latency * 1000)
        embed = discord.Embed(
            title="🏓 Pong!",
            description=f"**Latency**: {latency}ms",
            color=discord.Color.blurple()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="userinfo", description="Get information about a user")
    async def user_info(self, interaction: discord.Interaction, user: Optional[discord.Member] = None):
        """Get detailed information about a user."""
        user = user or interaction.user
        
        embed = discord.Embed(
            title=f"���� User Info: {user.name}",
            color=user.color,
            timestamp=interaction.created_at
        )
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.add_field(name="Username", value=user.mention, inline=True)
        embed.add_field(name="User ID", value=user.id, inline=True)
        embed.add_field(name="Account Created", value=user.created_at.strftime("%Y-%m-%d"), inline=True)
        embed.add_field(name="Joined Server", value=user.joined_at.strftime("%Y-%m-%d"), inline=True)
        embed.add_field(name="Top Role", value=user.top_role.mention, inline=True)
        embed.add_field(name="Bot?", value="✅ Yes" if user.bot else "❌ No", inline=True)
        
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="serverinfo", description="Get information about the server")
    async def server_info(self, interaction: discord.Interaction):
        """Get detailed information about the server."""
        guild = interaction.guild
        
        embed = discord.Embed(
            title=f"🏢 {guild.name}",
            color=discord.Color.blue(),
            timestamp=interaction.created_at
        )
        embed.set_thumbnail(url=guild.icon.url if guild.icon else None)
        embed.add_field(name="Server ID", value=guild.id, inline=True)
        embed.add_field(name="Owner", value=guild.owner.mention, inline=True)
        embed.add_field(name="Members", value=guild.member_count, inline=True)
        embed.add_field(name="Channels", value=len(guild.channels), inline=True)
        embed.add_field(name="Roles", value=len(guild.roles), inline=True)
        embed.add_field(name="Created", value=guild.created_at.strftime("%Y-%m-%d"), inline=True)
        
        await interaction.response.send_message(embed=embed, ephemeral=True)


class OwnerCog(commands.GroupCog, name="owner"):
    """Owner-only administrative commands."""
    def __init__(self, bot: AdvancedDiscordBot):
        self.bot = bot

    async def cog_check(self, interaction: discord.Interaction) -> bool:
        """Ensure only server owner can use these commands."""
        return interaction.guild.owner_id == interaction.user.id

    @app_commands.command(name="np-give", description="Grant No-Prefix execution validation rights over general server structures")
    async def np_give(self, interaction: discord.Interaction, target: discord.User):
        """Give no-prefix AI access to a user."""
        self.bot.db_no_prefix.add(target.id)
        embed = discord.Embed(
            title="✅ Permission Granted",
            description=f"User **{target.name}** was granted bypass execution permission overrides.",
            color=discord.Color.green()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="np-remove", description="Revoke target administrative bypass profile credentials blocks")
    async def np_remove(self, interaction: discord.Interaction, target: discord.User):
        """Remove no-prefix AI access from a user."""
        self.bot.db_no_prefix.discard(target.id)
        embed = discord.Embed(
            title="✅ Permission Revoked",
            description=f"Bypass configuration profiles safely unmapped for member structural node: **{target.name}**",
            color=discord.Color.orange()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="np-list", description="List all users with no-prefix access")
    async def np_list(self, interaction: discord.Interaction):
        """View all users with no-prefix access."""
        if not self.bot.db_no_prefix:
            await interaction.response.send_message("❌ No users have no-prefix access.", ephemeral=True)
            return
        
        embed = discord.Embed(
            title="📋 No-Prefix Users",
            color=discord.Color.blue()
        )
        
        users = []
        for user_id in self.bot.db_no_prefix:
            try:
                user = await self.bot.fetch_user(user_id)
                users.append(user.mention)
            except:
                pass
        
        embed.description = "\n".join(users) if users else "None"
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="ticket-setup", description="Deploy the server ticket setup interface panel")
    async def setup_ticket_panel(self, interaction: discord.Interaction):
        """Setup the ticket system panel."""
        embed = discord.Embed(
            title="🎫 Assistance Portal Hub", 
            description="Interact with the action matrix below to spin up a private support thread.", 
            color=0x2F3136
        )
        await interaction.response.send_message(embed=embed, view=TicketButtonView())

# ==============================================================================
# 5. RUNTIME INTERACTION INITIATOR
# ==============================================================================

if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        print("❌ ERROR: DISCORD_TOKEN not found in environment variables!")
        print("Make sure you have a .env file with DISCORD_TOKEN set")
        exit(1)
    
    try:
        bot.run(token)
    except Exception as e:
        print(f"❌ Failed to start bot: {e}")
