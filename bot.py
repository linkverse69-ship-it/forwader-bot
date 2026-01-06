import os
import asyncio
import json
import sys
import time
import subprocess
import tempfile
from typing import Dict, List, Optional
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from motor.motor_asyncio import AsyncIOMotorClient
from telethon import TelegramClient
from telethon.errors import FloodWaitError, SessionPasswordNeededError
from telethon.tl.custom.message import Message
from telethon.sessions import StringSession
from telethon.tl.types import DocumentAttributeVideo
from hachoir.metadata import extractMetadata
from hachoir.parser import createParser


MONGO_URI = "mongodb+srv://leakverse:leakverse@cluster0.vxosxyk.mongodb.net/?appName=Cluster0"
TELEGRAM_BOT_TOKEN = "6863982081:AAF-Xa7S_OgJ5TRYT_Qth_wyQ7AdjuX_eGM"
API_ID = 12380656
API_HASH = "d927c13beaaf5110f25c505b7c071273"

ADMIN_USER_IDS = [7737575998, 987654321]

DOWNLOAD_DIR = "./downloads"
VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff"}

mongo_client = AsyncIOMotorClient(MONGO_URI)
db = mongo_client["media_forwarder"]
users_collection = db["users"]

user_states = {}
user_clients = {}  # Store temporary telethon clients during auth
active_forwarders = {}
bot_app = None


class UserState:
    IDLE = "idle"
    WAITING_BOT_USERNAME = "waiting_bot_username"
    WAITING_CHANNEL_ID = "waiting_channel_id"
    WAITING_SECOND_BOT = "waiting_second_bot"
    WAITING_PHONE = "waiting_phone"
    WAITING_CODE = "waiting_code"
    WAITING_PASSWORD = "waiting_password"
    WAITING_SESSION_STRING = "waiting_session_string"


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_USER_IDS


def get_welcome_keyboard():
    keyboard = [
        [InlineKeyboardButton("Login Now", callback_data="login_start")],
        [InlineKeyboardButton("Reset", callback_data="reset_data")],
        [InlineKeyboardButton("Add Session", callback_data="add_session")],
        [InlineKeyboardButton("Reset Settings", callback_data="reset_settings")],
        [InlineKeyboardButton("Extract Session String", callback_data="extract_session")],
        [InlineKeyboardButton("Start Forwarder", callback_data="start_forwarder")],
        [InlineKeyboardButton("Stop Forwarder", callback_data="stop_forwarder")]
    ]
    return InlineKeyboardMarkup(keyboard)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    
    if not is_admin(user_id):
        await update.message.reply_text("Access denied. This bot is restricted to authorized users only.")
        return
    
    welcome_text = """
Welcome to Media Forwarder Bot

This bot automatically forwards media files from one Telegram bot to another through your private channel.

How it works:
The bot monitors a source bot for new media files. When new media is detected, it downloads the file, uploads it to your private channel, then forwards it to a second bot. After forwarding, the media is deleted from your private channel to keep it clean.

Supported Media Types:
• Images are uploaded as photos with preview
• Videos are uploaded with thumbnails and streaming support
• Other files are uploaded as documents

To get started, click Login Now to configure your bot settings.

You can reset your settings anytime or extract your session string for backup.
"""
    await update.message.reply_text(welcome_text, reply_markup=get_welcome_keyboard())


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    
    if not is_admin(user_id):
        await query.message.reply_text("Access denied. This bot is restricted to authorized users only.")
        return
    
    if query.data == "login_start":
        user_states[user_id] = UserState.WAITING_BOT_USERNAME
        await query.message.reply_text(
            "Enter the source bot username.\n"
            "Example: @MediaSourceBot"
        )
    
    elif query.data == "reset_data":
        user_data = await users_collection.find_one({"user_id": user_id})
        
        if user_id in active_forwarders:
            active_forwarders[user_id]["stop"] = True
            del active_forwarders[user_id]
        
        if user_id in user_clients:
            try:
                await user_clients[user_id]["client"].disconnect()
            except:
                pass
            del user_clients[user_id]

        session_string = None
        if user_data and user_data.get("session_string"):
            session_string = user_data["session_string"]

        if session_string:
            await users_collection.replace_one(
                {"user_id": user_id},
                {"user_id": user_id, "session_string": session_string},
                upsert=True
            )
        else:
            await users_collection.delete_one({"user_id": user_id})

        user_states[user_id] = UserState.WAITING_BOT_USERNAME
        await query.message.reply_text(
            "Credentials have been reset. Session preserved (if any).\n"
            "Please enter the source bot username.\n"
            "Example: @MediaSourceBot"
        )

    elif query.data == "add_session":
        user_states[user_id] = UserState.WAITING_SESSION_STRING
        await query.message.reply_text("Please enter your session string.")
    
    elif query.data == "reset_settings":
        # Get existing user data to preserve session_string
        user_data = await users_collection.find_one({"user_id": user_id})
        
        if user_data and user_data.get("session_string"):
            # Preserve only the session_string
            session_string = user_data["session_string"]
            
            # Delete all user data
            await users_collection.delete_one({"user_id": user_id})
            
            # Re-insert with only user_id and session_string
            await users_collection.insert_one({
                "user_id": user_id,
                "session_string": session_string
            })
            
            await query.message.reply_text(
                "Your bot settings have been reset successfully.\n"
                "Your session has been preserved - you won't need to login again.\n\n"
                "Click 'Login Now' to reconfigure your bots and channel.",
                reply_markup=get_welcome_keyboard()
            )
        else:
            # No session exists, just delete everything
            await users_collection.delete_one({"user_id": user_id})
            await query.message.reply_text(
                "Your settings have been reset successfully.\n"
                "Click 'Login Now' to configure new settings.",
                reply_markup=get_welcome_keyboard()
            )
        
        # Reset state
        user_states[user_id] = UserState.IDLE
        
        # Stop active forwarder if running
        if user_id in active_forwarders:
            active_forwarders[user_id]["stop"] = True
            del active_forwarders[user_id]
        
        # Disconnect any active client during auth
        if user_id in user_clients:
            try:
                await user_clients[user_id]["client"].disconnect()
            except:
                pass
            del user_clients[user_id]
    
    elif query.data == "extract_session":
        user_data = await users_collection.find_one({"user_id": user_id})
        
        if not user_data or not user_data.get("session_string"):
            await query.message.reply_text(
                "No session found. Please complete the login process first.",
                reply_markup=get_welcome_keyboard()
            )
            return
        
        session_string = user_data["session_string"]
        await query.message.reply_text(
            f"Your session string for backup:\n\n```\n{session_string}\n```\n\n"
            "Keep this string safe. You can use it to restore your session.",
            parse_mode="Markdown"
        )
    
    elif query.data == "start_forwarder":
        user_data = await users_collection.find_one({"user_id": user_id})
        
        if not user_data or not user_data.get("session_string"):
            await query.message.reply_text(
                "Please complete the login process first.",
                reply_markup=get_welcome_keyboard()
            )
            return
        
        # Check if all required fields are present
        if not all(key in user_data for key in ["bot_username", "private_channel_id", "second_bot_username"]):
            await query.message.reply_text(
                "Configuration incomplete. Please click 'Login Now' to set up your bots and channel.",
                reply_markup=get_welcome_keyboard()
            )
            return
        
        if user_id in active_forwarders:
            await query.message.reply_text("Forwarder is already running!")
            return
        
        await query.message.reply_text("Starting media forwarder...")
        asyncio.create_task(start_media_forwarder(user_id, user_data))
        await query.message.reply_text("Media forwarder started successfully!")
    
    elif query.data == "stop_forwarder":
        if user_id not in active_forwarders:
            await query.message.reply_text("No forwarder is currently running.")
            return
        
        active_forwarders[user_id]["stop"] = True
        await query.message.reply_text("Stopping media forwarder...")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    text = update.message.text
    
    if not is_admin(user_id):
        await update.message.reply_text("Access denied. This bot is restricted to authorized users only.")
        return
    
    if user_id not in user_states or user_states[user_id] == UserState.IDLE:
        await update.message.reply_text(
            "Please use the /start command to begin.",
            reply_markup=get_welcome_keyboard()
        )
        return
    
    if user_states[user_id] == UserState.WAITING_BOT_USERNAME:
        if not text.startswith("@"):
            await update.message.reply_text(
                "Invalid bot username. Please enter a valid username starting with @\n"
                "Example: @MediaSourceBot"
            )
            return
        
        user_data = await users_collection.find_one({"user_id": user_id}) or {}
        user_data["user_id"] = user_id
        user_data["bot_username"] = text
        await users_collection.update_one(
            {"user_id": user_id},
            {"$set": user_data},
            upsert=True
        )
        
        user_states[user_id] = UserState.WAITING_CHANNEL_ID
        await update.message.reply_text(
            "Enter your private channel ID.\n"
            "Example: -1001234567890"
        )
    
    elif user_states[user_id] == UserState.WAITING_CHANNEL_ID:
        try:
            channel_id = int(text)
        except ValueError:
            await update.message.reply_text(
                "Invalid channel ID. Please enter a valid numeric channel ID.\n"
                "Example: -1001234567890"
            )
            return
        
        user_data = await users_collection.find_one({"user_id": user_id})
        user_data["private_channel_id"] = channel_id
        await users_collection.update_one(
            {"user_id": user_id},
            {"$set": user_data}
        )
        
        user_states[user_id] = UserState.WAITING_SECOND_BOT
        await update.message.reply_text(
            "Enter the destination bot username.\n"
            "Example: @DestinationBot"
        )
    
    elif user_states[user_id] == UserState.WAITING_SECOND_BOT:
        if not text.startswith("@"):
            await update.message.reply_text(
                "Invalid bot username. Please enter a valid username starting with @\n"
                "Example: @DestinationBot"
            )
            return
        
        user_data = await users_collection.find_one({"user_id": user_id})
        user_data["second_bot_username"] = text
        user_data["created_at"] = datetime.utcnow()
        
        await users_collection.update_one(
            {"user_id": user_id},
            {"$set": user_data}
        )
        
        # Check if session_string already exists
        if user_data.get("session_string"):
            # Session exists, skip authentication
            await update.message.reply_text(
                f"✅ Configuration Complete!\n\n"
                f"Source: {user_data['bot_username']}\n"
                f"Channel: {user_data['private_channel_id']}\n"
                f"Destination: {user_data['second_bot_username']}\n\n"
                "Using your existing session.\n"
                "Click 'Start Forwarder' to begin.",
                reply_markup=get_welcome_keyboard()
            )
            user_states[user_id] = UserState.IDLE
        else:
            # No session, start Telegram authentication
            user_states[user_id] = UserState.WAITING_PHONE
            await update.message.reply_text("Enter your phone number with country code.\nExample: +1234567890")
    
    elif user_states[user_id] == UserState.WAITING_PHONE:
        phone = text.strip()
        
        client = TelegramClient(StringSession(), API_ID, API_HASH)
        await client.connect()
        
        try:
            await client.send_code_request(phone)
            user_clients[user_id] = {"client": client, "phone": phone}
            user_states[user_id] = UserState.WAITING_CODE
            
            await update.message.reply_text(
                "A code has been sent to your Telegram account. "
                "Please enter the code you received. "
                "Example: 1 2 3 4 5"
            )
        except Exception as e:
            await client.disconnect()
            await update.message.reply_text(f"Error: {str(e)}\nTry again.")
            user_states[user_id] = UserState.WAITING_PHONE
    
    elif user_states[user_id] == UserState.WAITING_CODE:
        code = text.strip().replace(" ", "")
        
        if user_id not in user_clients:
            await update.message.reply_text("Session expired. Start over.")
            user_states[user_id] = UserState.IDLE
            return
        
        client_data = user_clients[user_id]
        client = client_data["client"]
        phone = client_data["phone"]
        
        try:
            await client.sign_in(phone, code)
            
            session_string = client.session.save()
            await users_collection.update_one(
                {"user_id": user_id},
                {"$set": {"session_string": session_string}}
            )
            
            user_data = await users_collection.find_one({"user_id": user_id})
            
            await update.message.reply_text(
                f"✅ Done!\n\n"
                f"Source: {user_data['bot_username']}\n"
                f"Channel: {user_data['private_channel_id']}\n"
                f"Destination: {user_data['second_bot_username']}\n\n"
                "Click 'Start Forwarder' to begin.",
                reply_markup=get_welcome_keyboard()
            )
            
            await client.disconnect()
            del user_clients[user_id]
            user_states[user_id] = UserState.IDLE
            
        except SessionPasswordNeededError:
            user_states[user_id] = UserState.WAITING_PASSWORD
            await update.message.reply_text("Enter your 2FA password:")
        except Exception as e:
            await update.message.reply_text(f"Error: {str(e)}\nTry again.")
    
    elif user_states[user_id] == UserState.WAITING_PASSWORD:
        password = text.strip()
        
        if user_id not in user_clients:
            await update.message.reply_text("Session expired. Start over.")
            user_states[user_id] = UserState.IDLE
            return
        
        client_data = user_clients[user_id]
        client = client_data["client"]
        
        try:
            await client.sign_in(password=password)
            
            session_string = client.session.save()
            await users_collection.update_one(
                {"user_id": user_id},
                {"$set": {"session_string": session_string}}
            )
            
            user_data = await users_collection.find_one({"user_id": user_id})
            
            await update.message.reply_text(
                f"✅ Done!\n\n"
                f"Source: {user_data['bot_username']}\n"
                f"Channel: {user_data['private_channel_id']}\n"
                f"Destination: {user_data['second_bot_username']}\n\n"
                "Click 'Start Forwarder' to begin.",
                reply_markup=get_welcome_keyboard()
            )
            
            await client.disconnect()
            del user_clients[user_id]
            user_states[user_id] = UserState.IDLE
            
        except Exception as e:
            await update.message.reply_text(f"Error: {str(e)}\nTry again.")

    elif user_states[user_id] == UserState.WAITING_SESSION_STRING:
        session_string = text.strip()
        
        try:
            client = TelegramClient(StringSession(session_string), API_ID, API_HASH)
            await client.connect()
            
            if not await client.is_user_authorized():
                await client.disconnect()
                await update.message.reply_text("Invalid or expired session string. Please try again.")
                return

            await users_collection.update_one(
                {"user_id": user_id},
                {"$set": {"session_string": session_string}},
                upsert=True
            )
            
            await client.disconnect()
            
            user_states[user_id] = UserState.IDLE
            await update.message.reply_text(
                "Logged in successfully",
                reply_markup=get_welcome_keyboard()
            )
            
        except Exception as e:
            await update.message.reply_text(f"Error: {str(e)}\nPlease try again.")


def format_size(bytes_value: int) -> str:
    mb = bytes_value / (1024 * 1024)
    return f"{mb:.2f}MB"


def format_time(seconds: int) -> str:
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    return f"{hours}h{minutes}m{secs}s"


def create_progress_bar(percentage: float, length: int = 13) -> str:
    filled = int(length * percentage / 100)
    empty = length - filled
    return "■" * filled + "□" * empty


async def update_progress_message(
    message,
    filename: str,
    current: int,
    total: int,
    status: str,
    start_time: float
):
    percentage = (current / total * 100) if total > 0 else 0
    elapsed = time.time() - start_time
    speed = current / elapsed if elapsed > 0 else 0
    eta_seconds = int((total - current) / speed) if speed > 0 else 0
    
    progress_bar = create_progress_bar(percentage)
    
    text = f"""Name: {filename}
┠[{progress_bar}] {percentage:.2f}%
┠Process: {format_size(current)} of {format_size(total)}
┠Status: {status} | ETA: {format_time(eta_seconds)}
┠Speed: {format_size(int(speed))}/s | Elapsed: {format_time(int(elapsed))}"""
    
    try:
        await message.edit_text(text)
    except Exception:
        pass


def safe_remove(path: str) -> None:
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def get_media_type(path: str, src_msg: Optional[Message] = None) -> str:
    if src_msg is not None:
        try:
            mt = (src_msg.file.mime_type or "").lower()
            if mt.startswith("video/"):
                return "video"
            elif mt.startswith("image/"):
                return "image"
        except Exception:
            pass
    
    ext = os.path.splitext(path)[1].lower()
    if ext in VIDEO_EXTS:
        return "video"
    elif ext in IMAGE_EXTS:
        return "image"
    
    return "document"


def make_video_thumb_ffmpeg(video_path: str) -> Optional[str]:
    try:
        fd, thumb_path = tempfile.mkstemp(suffix=".jpg")
        os.close(fd)

        cmd = [
            "ffmpeg", "-y",
            "-ss", "00:00:01.000",
            "-i", video_path,
            "-vframes", "1",
            "-vf", "scale=320:-1",
            thumb_path
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

        if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
            return thumb_path
    except Exception:
        pass
    return None


def get_video_metadata(video_path: str):
    width = 0
    height = 0
    duration = 0
    try:
        parser = createParser(video_path)
        metadata = extractMetadata(parser)
        if metadata.has("duration"):
            duration = metadata.get('duration').seconds
        if metadata.has("width"):
            width = metadata.get("width")
        if metadata.has("height"):
            height = metadata.get("height")
    except Exception as e:
        print(f"Error getting metadata: {e}")
    return width, height, duration


async def download_media(
    client: TelegramClient,
    msg: Message,
    out_dir: str,
    user_id: int,
    filename: str,
    progress_message
) -> Optional[str]:
    os.makedirs(out_dir, exist_ok=True)
    
    start_time = time.time()
    last_update = 0
    
    async def progress_callback(current: int, total: int):
        nonlocal last_update
        current_time = time.time()
        if current_time - last_update >= 3:
            await update_progress_message(
                progress_message,
                filename,
                current,
                total,
                "Download",
                start_time
            )
            last_update = current_time

    try:
        path = await client.download_media(msg, file=out_dir, progress_callback=progress_callback)
        
        await update_progress_message(
            progress_message,
            filename,
            msg.file.size,
            msg.file.size,
            "Download Complete",
            start_time
        )
        
        return path
    except FloodWaitError as e:
        await asyncio.sleep(e.seconds)
        return await download_media(client, msg, out_dir, user_id, filename, progress_message)
    except Exception as e:
        await progress_message.edit_text(f"Download failed: {str(e)}")
        return None


async def upload_media(
    client: TelegramClient,
    channel_id: int,
    path: str,
    user_id: int,
    filename: str,
    progress_message,
    src_msg: Optional[Message] = None
) -> List[int]:
    media_type = get_media_type(path, src_msg)
    
    thumb_path = None
    if media_type == "video":
        thumb_path = make_video_thumb_ffmpeg(path)
    
    start_time = time.time()
    last_update = 0
    file_size = os.path.getsize(path)
    
    async def progress_callback(current: int, total: int):
        nonlocal last_update
        current_time = time.time()
        if current_time - last_update >= 3:
            await update_progress_message(
                progress_message,
                filename,
                current,
                total,
                "Upload",
                start_time
            )
            last_update = current_time

    try:
        if media_type == "image":
            sent = await client.send_file(
                channel_id,
                path,
                progress_callback=progress_callback,
                force_document=False,
            )
        elif media_type == "video":
            width, height, duration = get_video_metadata(path)
            sent = await client.send_file(
                channel_id,
                path,
                progress_callback=progress_callback,
                force_document=False,
                supports_streaming=True,
                attributes=[DocumentAttributeVideo(
                    duration=duration,
                    w=width,
                    h=height,
                    supports_streaming=True
                )],
                thumb=thumb_path if thumb_path else None,
            )
        else:
            sent = await client.send_file(
                channel_id,
                path,
                progress_callback=progress_callback,
                force_document=True,
            )

        await update_progress_message(
            progress_message,
            filename,
            file_size,
            file_size,
            "Upload Complete",
            start_time
        )

        if isinstance(sent, list):
            return [m.id for m in sent]
        return [sent.id]

    except FloodWaitError as e:
        await asyncio.sleep(e.seconds)
        return await upload_media(client, channel_id, path, user_id, filename, progress_message, src_msg)

    finally:
        if thumb_path:
            safe_remove(thumb_path)


async def forward_messages(client: TelegramClient, from_channel_id: int, to_entity: str, msg_ids: List[int]) -> None:
    if not msg_ids:
        return
    chunk_size = 50
    for i in range(0, len(msg_ids), chunk_size):
        chunk = msg_ids[i:i + chunk_size]
        try:
            await client.forward_messages(entity=to_entity, messages=chunk, from_peer=from_channel_id)
        except FloodWaitError as e:
            await asyncio.sleep(e.seconds)
            await client.forward_messages(entity=to_entity, messages=chunk, from_peer=from_channel_id)


async def delete_messages(client: TelegramClient, channel_id: int, msg_ids: List[int]) -> None:
    if not msg_ids:
        return
    chunk_size = 100
    for i in range(0, len(msg_ids), chunk_size):
        chunk = msg_ids[i:i + chunk_size]
        try:
            await client.delete_messages(entity=channel_id, message_ids=chunk, revoke=True)
        except FloodWaitError as e:
            await asyncio.sleep(e.seconds)
            await client.delete_messages(entity=channel_id, message_ids=chunk, revoke=True)


async def start_media_forwarder(user_id: int, config: Dict):
    if user_id in active_forwarders:
        return
    
    active_forwarders[user_id] = {"stop": False}
    
    session_string = config.get("session_string", "")
    if not session_string:
        await bot_app.bot.send_message(user_id, "Error: No session found. Please login first.")
        return
    
    client = TelegramClient(StringSession(session_string), API_ID, API_HASH)
    
    try:
        await client.connect()
        
        if not await client.is_user_authorized():
            await bot_app.bot.send_message(user_id, "Session expired. Please login again.")
            del active_forwarders[user_id]
            return
    except Exception as e:
        await bot_app.bot.send_message(user_id, f"Connection error: {str(e)}")
        del active_forwarders[user_id]
        return
    
    source_bot = config["bot_username"]
    private_channel_id = config["private_channel_id"]
    second_bot = config["second_bot_username"]
    
    last_msg_id = 0
    messages = await client.get_messages(source_bot, limit=1)
    if messages:
        last_msg_id = messages[0].id
    
    await bot_app.bot.send_message(user_id, f"✅ Media forwarder started!\nMonitoring: {source_bot}")
    print(f"Started forwarder for user {user_id}")
    
    while not active_forwarders[user_id]["stop"]:
        try:
            new_media_msgs = []
            
            async for msg in client.iter_messages(source_bot, min_id=last_msg_id):
                if msg.media:
                    new_media_msgs.append(msg)
            
            if not new_media_msgs:
                await asyncio.sleep(5)
                continue
            
            new_media_msgs.sort(key=lambda m: m.id)
            
            downloaded_paths = []
            uploaded_msg_ids = []
            
            for msg in new_media_msgs:
                filename = getattr(msg.file, 'name', None) or f"file_{msg.id}"
                
                progress_message = await bot_app.bot.send_message(
                    chat_id=user_id,
                    text=f"Name: {filename}\n┠Preparing download..."
                )
                
                path = await download_media(client, msg, DOWNLOAD_DIR, user_id, filename, progress_message)
                if path:
                    downloaded_paths.append(path)
                    
                    ids = await upload_media(
                        client,
                        private_channel_id,
                        path,
                        user_id,
                        filename,
                        progress_message,
                        src_msg=msg
                    )
                    uploaded_msg_ids.extend(ids)
                
                last_msg_id = max(last_msg_id, msg.id)
            
            await forward_messages(client, private_channel_id, second_bot, uploaded_msg_ids)
            await delete_messages(client, private_channel_id, uploaded_msg_ids)
            
            for p in downloaded_paths:
                safe_remove(p)
            
            if uploaded_msg_ids:
                await bot_app.bot.send_message(user_id, "✅ Forwarded")
            
            await asyncio.sleep(5)
            
        except Exception as e:
            print(f"Forwarder error for user {user_id}: {e}")
            await asyncio.sleep(5)
    
    await client.disconnect()
    del active_forwarders[user_id]
    await bot_app.bot.send_message(user_id, "Media forwarder stopped.")
    print(f"Stopped forwarder for user {user_id}")


def main():
    global bot_app
    
    bot_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    bot_app.add_handler(CommandHandler("start", start_command))
    bot_app.add_handler(CallbackQueryHandler(button_callback))
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    print("Bot started...")
    bot_app.run_polling()


if __name__ == "__main__":
    main()
