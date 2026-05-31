import os
import logging
import re
import requests
import threading
import time
from datetime import datetime, timedelta
from flask import Flask
import telebot
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup
from telebot import types
from supabase import create_client, Client
import json
import platform
import psutil

# === Configuration ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMINS = [int(x) for x in os.getenv("ADMINS", "").split(",") if x.strip() and x.strip().isdigit()]
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "").strip()
CHANNEL_CHAT_ID = os.getenv("CHANNEL_CHAT_ID", "").strip()
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "")

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

# Initialize Supabase client
try:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    logger.info("✅ Connected to Supabase successfully")
except Exception as e:
    logger.error(f"❌ Failed to connect to Supabase: {e}")
    raise

# Initialize bot
bot = telebot.TeleBot(BOT_TOKEN)

# Flask app
app = Flask(__name__)

# Admin sessions storage
admin_sessions = {}

# System stats
start_time = datetime.now()
restart_count = 0

# ==================== IMPROVED KEEP ALIVE MECHANISM ====================

def keep_alive_ping():
    """پینگ کردن سلف برای جلوگیری از sleep شدن Render رایگان"""
    try:
        if RENDER_EXTERNAL_URL:
            # پینگ چندین endpoint برای اطمینان
            endpoints = [
                f"{RENDER_EXTERNAL_URL}/health",
                f"{RENDER_EXTERNAL_URL}/ping", 
                f"{RENDER_EXTERNAL_URL}/",
                f"{RENDER_EXTERNAL_URL}/status"
            ]
            
            success_count = 0
            for endpoint in endpoints:
                try:
                    response = requests.get(endpoint, timeout=5)
                    if response.status_code == 200:
                        success_count += 1
                        logger.debug(f"✅ Ping {endpoint}: {response.status_code}")
                except:
                    continue
            
            if success_count > 0:
                logger.info(f"✅ Keep-alive successful ({success_count}/{len(endpoints)}) at {datetime.now().strftime('%H:%M:%S')}")
            else:
                logger.warning(f"⚠️ All pings failed")
                
        else:
            logger.warning("⚠️ RENDER_EXTERNAL_URL not set")
                
    except Exception as e:
        logger.error(f"❌ Keep-alive ping error: {e}")


def start_keep_alive():
    """شروع keep-alive بهبود یافته"""
    def ping_loop():
        while True:
            try:
                keep_alive_ping()
                # برای Render رایگان: هر 45 ثانیه پینگ کن
                time.sleep(45)  # تغییر از 120 به 45
            except Exception as e:
                logger.error(f"Keep-alive loop error: {e}")
                time.sleep(30)
    
    keep_alive_thread = threading.Thread(target=ping_loop, daemon=True)
    keep_alive_thread.start()
    logger.info("🔄 Keep-alive for FREE RENDER started (45 seconds interval)")  # متن لاگ رو هم تغییر بده

# ==================== SYSTEM MONITORING ====================

def cleanup_old_sessions():
    """پاکسازی sessionهای قدیمی admin"""
    try:
        current_time = time.time()
        keys_to_remove = []
        
        for user_id, session in admin_sessions.items():
            # اگر session بیش از 1 ساعت قدیمی شده
            if current_time - session.get('_timestamp', 0) > 3600:
                keys_to_remove.append(user_id)
        
        for key in keys_to_remove:
            admin_sessions.pop(key, None)
            logger.info(f"🧹 Cleaned up old session for user {key}")
            
    except Exception as e:
        logger.error(f"Error cleaning sessions: {e}")

def get_system_status():
    """دریافت وضعیت سیستم"""
    try:
        # تست اتصال به Supabase
        test_result = supabase.table("films").select("count", count="exact").limit(1).execute()
        
        # تست بات
        bot_info = bot.get_me()
        
        # محاسبه uptime
        uptime_seconds = time.time() - psutil.boot_time()
        uptime_str = str(timedelta(seconds=uptime_seconds))
        
        # حافظه
        memory = psutil.virtual_memory()
        
        return {
            "bot_username": bot_info.username,
            "bot_id": bot_info.id,
            "bot_status": "active",
            "database_status": "connected" if test_result else "disconnected",
            "database_count": test_result.count if test_result else 0,
            "system": f"{platform.system()} {platform.release()}",
            "uptime": uptime_str,
            "memory_percent": memory.percent,
            "start_time": start_time.strftime("%Y-%m-%d %H:%M:%S"),
            "restart_count": restart_count,
            "current_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "admin_sessions": len(admin_sessions)
        }
    except Exception as e:
        logger.error(f"Error getting system status: {e}")
        return {"error": str(e)}

# ==================== HELPER FUNCTIONS ====================

def check_membership(user_id):
    """Check if user is member of channel"""
    if user_id in ADMINS:
        return True
    
    if not CHANNEL_USERNAME and not CHANNEL_CHAT_ID:
        return True
    
    try:
        channel = CHANNEL_CHAT_ID if CHANNEL_CHAT_ID else CHANNEL_USERNAME
        member = bot.get_chat_member(channel, user_id)
        return member.status in ['member', 'administrator', 'creator']
    except Exception as e:
        logger.error(f"Membership check failed: {e}")
        return False

def create_film_caption(description, quality):
    """Create film caption with proper formatting"""
    caption_parts = []
    
    # اضافه کردن متن اول اگر وجود داشت
    if description:
        caption_parts.append(description)
    
    caption_parts.append("#زیرنویس_چسبیده_فارسی🍷")
    caption_parts.append(f"کیفیت {quality} #بدون_سانسور")
    return "\n".join(caption_parts)

def create_episode_caption(template, episode_num, quality):
    """Create episode caption with proper formatting"""
    caption_parts = []
    if template:
        caption_parts.append(template)
    caption_parts.append(f"قسمت {episode_num}")
    caption_parts.append("#زیرنویس_چسبیده_فارسی🍷")
    caption_parts.append(f"کیفیت {quality} #بدون_سانسور")
    return "\n".join(caption_parts)

def build_join_keyboard():
    """Build join channel keyboard"""
    keyboard = InlineKeyboardMarkup()
    if CHANNEL_USERNAME:
        keyboard.add(InlineKeyboardButton(
            "🔒 عضویت در کانال", 
            url=f"https://t.me/{CHANNEL_USERNAME.lstrip('@')}"
        ))
    keyboard.add(InlineKeyboardButton("🔄 بررسی عضویت", callback_data="check_join"))
    return keyboard

# ==================== FLASK ROUTES ====================

@app.route('/')
def home():
    return "✅ Bot is running!", 200

@app.route('/health')
def health():
    """Endpoint سلامت برای پینگ کردن"""
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "service": "Telegram Film Bot"
    }, 200

@app.route('/ping')
def ping():
    """Endpoint ساده برای پینگ"""
    return "pong", 200

@app.route('/status')
def status():
    """بررسی وضعیت بات و دیتابیس"""
    try:
        status_info = get_system_status()
        if "error" in status_info:
            return {
                "status": "error",
                "error": status_info["error"],
                "timestamp": datetime.now().isoformat()
            }, 500
        
        return {
            "status": "running",
            "data": status_info,
            "timestamp": datetime.now().isoformat()
        }, 200
    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "timestamp": datetime.now().isoformat()
        }, 500

@app.route('/bot_health')
def bot_health():
    """بررسی سلامت بات"""
    try:
        # چک کن بات زنده هست
        bot_info = bot.get_me()
        return {
            "status": "healthy",
            "bot_username": bot_info.username,
            "bot_id": bot_info.id,
            "timestamp": datetime.now().isoformat(),
            "message": "Bot is running"
        }, 200
    except Exception as e:
        return {
            "status": "unhealthy",
            "error": str(e),
            "timestamp": datetime.now().isoformat(),
            "message": "Bot is not responding"
        }, 500

# ==================== BOT HANDLERS ====================

@bot.message_handler(commands=['start'])
def start_handler(message):
    user = message.from_user
    args = message.text.split()[1:] if len(message.text.split()) > 1 else []
    
    if not args:
        if user.id in ADMINS:
            show_admin_panel(message)
        else:
            show_user_welcome(message)
    else:
        key = args[0]
        handle_deeplink(message, key)

@bot.message_handler(commands=['status', 'health'])
def status_command(message):
    """بررسی وضعیت بات"""
    if message.from_user.id not in ADMINS:
        return
    
    try:
        status_info = get_system_status()
        
        if "error" in status_info:
            bot.send_message(message.chat.id, f"❌ خطا در بررسی وضعیت: {status_info['error']}")
            return
        
        status_text = f"""
📊 **وضعیت سیستم**

🤖 **بات:**
• نام: @{status_info['bot_username']}
• آیدی: {status_info['bot_id']}
• وضعیت: فعال ✅

🗄️ **دیتابیس:**
• وضعیت: {status_info['database_status'].upper()}
• تعداد فیلم‌ها: {status_info['database_count']}

🖥️ **سرور:**
• سیستم: {status_info['system']}
• Uptime: {status_info['uptime']}
• حافظه: {status_info['memory_percent']}%
• شروع: {status_info['start_time']}

📈 **آمار:**
• Restartها: {status_info['restart_count']}
• Sessionها: {status_info['admin_sessions']}
• زمان: {status_info['current_time']}

🔄 **Keep-alive: فعال**
📡 **پینگ: هر 2 دقیقه**
        """
        
        bot.send_message(message.chat.id, status_text, parse_mode='Markdown')
        
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ خطا در بررسی وضعیت: {str(e)}")

def show_admin_panel(message):
    keyboard = InlineKeyboardMarkup()
    keyboard.row(
        InlineKeyboardButton("🎬 افزودن فیلم", callback_data="admin_add_film"),
        InlineKeyboardButton("📺 افزودن سریال", callback_data="admin_add_series")
    )
    keyboard.row(
        InlineKeyboardButton("📋 لیست محتوا", callback_data="admin_list"),
        InlineKeyboardButton("🗑️ حذف محتوا", callback_data="admin_delete")
    )
    keyboard.row(
        InlineKeyboardButton("📊 وضعیت سیستم", callback_data="admin_status")
    )
    
    bot.send_message(
        message.chat.id,
        "🛠️ پنل مدیریت\nلطفاً عملیات مورد نظر را انتخاب کنید:",
        reply_markup=keyboard
    )

def show_user_welcome(message):
    if not check_membership(message.from_user.id):
        bot.send_message(
            message.chat.id,
            "❌ برای استفاده از ربات باید در کانال عضو شوید.",
            reply_markup=build_join_keyboard()
        )
        return
    
    bot.send_message(
        message.chat.id,
        "👋 به ربات خوش آمدید!\nبرای دریافت فایل از لینک‌های ارسالی در کانال استفاده کنید."
    )

def handle_deeplink(message, key):
    user_id = message.from_user.id
    
    if not check_membership(user_id):
        bot.send_message(
            message.chat.id,
            "❌ برای دریافت فایل باید در کانال عضو شوید.",
            reply_markup=build_join_keyboard()
        )
        return
    
    # Check if it's a series episode (format: seriesKey_EpisodeNumber)
    if "_E" in key:
        series_key, ep_num = key.split("_E", 1)
        try:
            episode_num = int(ep_num)
            show_episode_qualities(message, series_key, episode_num)
            return
        except ValueError:
            pass

    # Check films from Supabase
    try:
        film_response = supabase.table("films").select("*").eq("key", key).execute()
        if film_response.data:
            film = film_response.data[0]
            qualities_response = supabase.table("film_qualities").select("*").eq("film_key", key).execute()
            qualities = [(q['quality'], q['file_id'], q['caption']) for q in qualities_response.data]
            
            if qualities:
                show_film_qualities(message, film, qualities)
                return
    except Exception as e:
        logger.error(f"Error fetching film: {e}")

    # Check series from Supabase
    try:
        series_response = supabase.table("series").select("*").eq("key", key).execute()
        if series_response.data:
            series = series_response.data[0]
            episodes_response = supabase.table("episodes").select("episode_number").eq("series_key", key).execute()
            episodes = [row['episode_number'] for row in episodes_response.data]
            
            if episodes:
                show_series_episodes(message, series, episodes)
                return
    except Exception as e:
        logger.error(f"Error fetching series: {e}")

    bot.send_message(message.chat.id, "❌ محتوای مورد نظر یافت نشد.")

def show_film_qualities(message, film, qualities):
    film_key = film['key']
    title = film['title']
    text = f"🎬 {title}\n\n"
    text += "لطفاً کیفیت مورد نظر را انتخاب کنید:"
    
    keyboard = InlineKeyboardMarkup()
    for quality, file_id, caption in qualities:
        keyboard.add(InlineKeyboardButton(f"🎬 {quality}", callback_data=f"quality:{film_key}:{quality}"))
    
    bot.send_message(message.chat.id, text, reply_markup=keyboard)

def show_series_episodes(message, series, episodes):
    series_key = series['key']
    title = series['title']
    poster_file_id = series.get('poster_file_id')
    poster_description = series.get('poster_description')
    
    text = f"📺 {title}\n"
    if poster_description:
        text += f"\n{poster_description}"
    
    bot_username = bot.get_me().username
    
    # فقط episode_number های منحصر به فرد رو بگیر
    unique_episodes = list(set(episodes))
    unique_episodes.sort()  # مرتب کردن
    
    keyboard = InlineKeyboardMarkup()
    for ep_num in unique_episodes:
        deeplink = f"https://t.me/{bot_username}?start={series_key}_E{ep_num}"
        keyboard.add(InlineKeyboardButton(f"📺 قسمت {ep_num}", url=deeplink))
    
    try:
        if poster_file_id:
            bot.send_photo(
                message.chat.id,
                poster_file_id,
                caption=text,
                reply_markup=keyboard
            )
        else:
            bot.send_message(message.chat.id, text, reply_markup=keyboard)
    except Exception as e:
        logger.error(f"Error sending series: {e}")
        bot.send_message(message.chat.id, text, reply_markup=keyboard)

def show_episode_qualities(message, series_key, episode_num):
    try:
        # فقط یک رکورد برای این قسمت بگیر (همه کیفیت‌ها رو با هم)
        episode_response = supabase.table("episodes").select("*").eq("series_key", series_key).eq("episode_number", episode_num).execute()
        
        if not episode_response.data:
            bot.send_message(message.chat.id, "❌ این قسمت یافت نشد.")
            return

        # همه کیفیت‌های موجود برای این قسمت رو جمع کن
        qualities = []
        for episode in episode_response.data:
            qualities.append((episode['quality'], episode['file_id'], episode['caption']))
        
        keyboard = InlineKeyboardMarkup()
        for quality, file_id, caption in qualities:
            keyboard.add(InlineKeyboardButton(f"🎥 {quality}", callback_data=f"episode:{series_key}:{episode_num}:{quality}"))
        
        text = f"📺 قسمت {episode_num}\nلطفاً کیفیت مورد نظر را انتخاب کنید:"
        bot.send_message(message.chat.id, text, reply_markup=keyboard)
    except Exception as e:
        logger.error(f"Error fetching episode: {e}")
        bot.send_message(message.chat.id, "❌ خطا در دریافت اطلاعات")

# ==================== ADMIN HANDLERS ====================

@bot.callback_query_handler(func=lambda call: call.data.startswith('admin_'))
def admin_callback_handler(call):
    user_id = call.from_user.id
    if user_id not in ADMINS:
        bot.answer_callback_query(call.id, "❌ دسترسی denied")
        return
    
    data = call.data
    
    if data == "admin_add_film":
        start_add_film(call)
    elif data == "admin_add_series":
        start_add_series(call)
    elif data == "admin_list":
        show_content_list(call)
    elif data == "admin_delete":
        show_delete_options(call)
    elif data == "admin_status":
        status_command(call.message)
        bot.answer_callback_query(call.id, "✅ وضعیت بررسی شد")
        return
    elif data == "admin_done":
        admin_sessions.pop(user_id, None)
        bot.edit_message_text(
            "✅ عملیات با موفقیت завер شد.",
            call.message.chat.id,
            call.message.message_id
        )
    elif data == "admin_add_another_quality":
        user_id = call.from_user.id
        session = admin_sessions.get(user_id)
        if session and session.get("mode") == "add_film":
            session["step"] = "quality"
            bot.edit_message_text(
                "لطفاً کیفیت جدید را وارد کنید (مثال: 1080p):",
                call.message.chat.id,
                call.message.message_id
            )
    elif data == "admin_add_another_episode":
        user_id = call.from_user.id
        session = admin_sessions.get(user_id)
        if session and session.get("mode") == "add_series":
            session["step"] = "episode_number"
            bot.edit_message_text(
                "لطفاً شماره قسمت جدید را وارد کنید:",
                call.message.chat.id,
                call.message.message_id
            )
    elif data == "admin_add_episode_quality":
        user_id = call.from_user.id
        session = admin_sessions.get(user_id)
        if session and session.get("mode") == "add_series":
            session["step"] = "episode_quality"
            bot.edit_message_text(
                "لطفاً کیفیت جدید برای این قسمت را وارد کنید:",
                call.message.chat.id,
                call.message.message_id
            )
    elif data == "admin_done_series":
        user_id = call.from_user.id
        session = admin_sessions.get(user_id)
        if session and session.get("mode") == "add_series":
            bot_username = bot.get_me().username
            deeplink = f"https://t.me/{bot_username}?start={session['series_key']}"
            
            bot.edit_message_text(
                f"✅ سریال با موفقیت تکمیل شد!\n\n"
                f"🔑 کلید: `{session['series_key']}`\n"
                f"📺 عنوان: {session['series_title']}\n\n"
                f"🔗 دیپ‌لینک:\n`{deeplink}`",
                call.message.chat.id,
                call.message.message_id,
                parse_mode='Markdown'
            )
            admin_sessions.pop(user_id, None)

def start_add_film(call):
    user_id = call.from_user.id
    admin_sessions[user_id] = {
        "mode": "add_film", 
        "step": "key",
        "_timestamp": time.time()
    }
    
    bot.edit_message_text(
        "🎬 افزودن فیلم جدید\n\nلطفاً کلید فیلم را وارد کنید (مثال: the_matrix):",
        call.message.chat.id,
        call.message.message_id
    )

def start_add_series(call):
    user_id = call.from_user.id
    admin_sessions[user_id] = {
        "mode": "add_series", 
        "step": "key",
        "_timestamp": time.time()
    }
    
    bot.edit_message_text(
        "📺 افزودن سریال جدید\n\nلطفاً کلید سریال را وارد کنید (مثال: breaking_bad):",
        call.message.chat.id,
        call.message.message_id
    )

def show_content_list(call):
    try:
        # Get films from Supabase
        films_response = supabase.table("films").select("key, title").execute()
        films = [(f['key'], f['title']) for f in films_response.data]
        
        # Get series from Supabase
        series_response = supabase.table("series").select("key, title").execute()
        series = [(s['key'], s['title']) for s in series_response.data]
        
        text = "📋 لیست محتوا\n\n"
        
        if films:
            text += "🎬 فیلم‌ها:\n"
            for film_key, title in films:
                bot_username = bot.get_me().username
                deeplink = f"https://t.me/{bot_username}?start={film_key}"
                text += f"• {title} (`{film_key}`)\n🔗 `{deeplink}`\n\n"
        
        if series:
            text += "📺 سریال‌ها:\n"
            for series_key, title in series:
                bot_username = bot.get_me().username
                deeplink = f"https://t.me/{bot_username}?start={series_key}"
                text += f"• {title} (`{series_key}`)\n🔗 `{deeplink}`\n\n"
        
        if not films and not series:
            text += "❌ هیچ محتوایی وجود ندارد."
        
        if len(text) > 4000:
            parts = [text[i:i+4000] for i in range(0, len(text), 4000)]
            for part in parts:
                bot.send_message(call.message.chat.id, part, parse_mode='Markdown')
        else:
            bot.edit_message_text(
                text,
                call.message.chat.id,
                call.message.message_id,
                parse_mode='Markdown'
            )
    except Exception as e:
        logger.error(f"Error showing content list: {e}")
        bot.answer_callback_query(call.id, "❌ خطا در دریافت لیست")

def show_delete_options(call):
    keyboard = InlineKeyboardMarkup()
    keyboard.row(
        InlineKeyboardButton("🎬 حذف فیلم", callback_data="delete_films"),
        InlineKeyboardButton("📺 حذف سریال", callback_data="delete_series")
    )
    keyboard.add(InlineKeyboardButton("🔙 بازگشت", callback_data="admin_back"))
    
    bot.edit_message_text(
        "🗑️ حذف محتوا\nلطفاً نوع محتوای مورد نظر برای حذف را انتخاب کنید:",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=keyboard
    )

# ==================== DELETE HANDLERS (FIXED) ====================

@bot.callback_query_handler(func=lambda call: call.data in ['delete_films', 'delete_series'])
def delete_options_handler(call):
    user_id = call.from_user.id
    if user_id not in ADMINS:
        bot.answer_callback_query(call.id, "❌ دسترسی denied")
        return
    
    data = call.data
    
    if data == "delete_films":
        show_films_for_deletion(call)
    elif data == "delete_series":
        show_series_for_deletion(call)

def show_films_for_deletion(call):
    try:
        films_response = supabase.table("films").select("id, key, title").execute()
        films = [(f['id'], f['key'], f['title']) for f in films_response.data]
        
        if not films:
            bot.answer_callback_query(call.id, "❌ هیچ فیلمی برای حذف وجود ندارد")
            return
        
        keyboard = InlineKeyboardMarkup()
        for film_id, film_key, title in films:
            # استفاده از ID به جای کلید برای جلوگیری از طولانی شدن callback data
            # و کوتاه کردن عنوان اگر طولانی باشه
            short_title = title[:30] + "..." if len(title) > 30 else title
            keyboard.add(InlineKeyboardButton(
                f"🗑️ {short_title}", 
                callback_data=f"delf:{film_id}"  # استفاده از فرمت کوتاه
            ))
        
        keyboard.add(InlineKeyboardButton("🔙 بازگشت", callback_data="admin_delete"))
        
        bot.edit_message_text(
            "🎬 انتخاب فیلم برای حذف:",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=keyboard
        )
    except Exception as e:
        logger.error(f"Error showing films for deletion: {e}")
        bot.answer_callback_query(call.id, "❌ خطا در دریافت لیست")

def show_series_for_deletion(call):
    try:
        series_response = supabase.table("series").select("id, key, title").execute()
        series = [(s['id'], s['key'], s['title']) for s in series_response.data]
        
        if not series:
            bot.answer_callback_query(call.id, "❌ هیچ سریالی برای حذف وجود ندارد")
            return
        
        keyboard = InlineKeyboardMarkup()
        for series_id, series_key, title in series:
            # استفاده از ID به جای کلید برای جلوگیری از طولانی شدن callback data
            short_title = title[:30] + "..." if len(title) > 30 else title
            keyboard.add(InlineKeyboardButton(
                f"🗑️ {short_title}", 
                callback_data=f"dels:{series_id}"  # استفاده از فرمت کوتاه
            ))
        
        keyboard.add(InlineKeyboardButton("🔙 بازگشت", callback_data="admin_delete"))
        
        bot.edit_message_text(
            "📺 انتخاب سریال برای حذف:",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=keyboard
        )
    except Exception as e:
        logger.error(f"Error showing series for deletion: {e}")
        bot.answer_callback_query(call.id, "❌ خطا در دریافت لیست")

@bot.callback_query_handler(func=lambda call: call.data.startswith(('delf:', 'dels:')))
def delete_callback_handler(call):
    user_id = call.from_user.id
    if user_id not in ADMINS:
        bot.answer_callback_query(call.id, "❌ دسترسی denied")
        return
    
    data = call.data
    
    if data.startswith("delf:"):
        film_id = data.split(":")[1]
        delete_film(call, film_id)
    elif data.startswith("dels:"):
        series_id = data.split(":")[1]
        delete_series(call, series_id)

def delete_film(call, film_id):
    try:
        # ابتدا film_key رو پیدا کن
        film_response = supabase.table("films").select("key").eq("id", film_id).execute()
        if not film_response.data:
            bot.answer_callback_query(call.id, "❌ فیلم مورد نظر یافت نشد")
            return
            
        film_key = film_response.data[0]['key']
        
        # Delete film qualities first (foreign key constraint)
        supabase.table("film_qualities").delete().eq("film_key", film_key).execute()
        # Delete film
        supabase.table("films").delete().eq("id", film_id).execute()
        
        bot.answer_callback_query(call.id, "✅ فیلم با موفقیت حذف شد")
        show_admin_panel(call.message)
    except Exception as e:
        logger.error(f"Error deleting film: {e}")
        bot.answer_callback_query(call.id, "❌ خطا در حذف فیلم")

def delete_series(call, series_id):
    try:
        # ابتدا series_key رو پیدا کن
        series_response = supabase.table("series").select("key").eq("id", series_id).execute()
        if not series_response.data:
            bot.answer_callback_query(call.id, "❌ سریال مورد نظر یافت نشد")
            return
            
        series_key = series_response.data[0]['key']
        
        # Delete episodes first (foreign key constraint)
        supabase.table("episodes").delete().eq("series_key", series_key).execute()
        # Delete series
        supabase.table("series").delete().eq("id", series_id).execute()
        
        bot.answer_callback_query(call.id, "✅ سریال با موفقیت حذف شد")
        show_admin_panel(call.message)
    except Exception as e:
        logger.error(f"Error deleting series: {e}")
        bot.answer_callback_query(call.id, "❌ خطا در حذف سریال")

@bot.callback_query_handler(func=lambda call: call.data == "admin_back")
def admin_back_handler(call):
    show_admin_panel(call.message)

# ==================== ADMIN FLOW HANDLERS ====================

@bot.message_handler(func=lambda message: message.from_user.id in ADMINS and message.text)
def admin_message_handler(message):
    user_id = message.from_user.id
    session = admin_sessions.get(user_id)
    
    if not session:
        return
    
    text = message.text.strip()
    mode = session["mode"]
    step = session["step"]
    
    try:
        if mode == "add_film":
            handle_film_flow(message, text, step, session)
        elif mode == "add_series":
            handle_series_flow(message, text, step, session)
    except Exception as e:
        logger.error(f"Admin flow error: {e}")
        bot.send_message(message.chat.id, "❌ خطا در پردازش")
        admin_sessions.pop(user_id, None)

def handle_film_flow(message, text, step, session):
    if step == "key":
        session["film_key"] = text
        session["step"] = "title"
        session["_timestamp"] = time.time()
        bot.send_message(message.chat.id, "لطفاً عنوان فیلم را وارد کنید:")
        
    elif step == "title":
        session["film_title"] = text
        session["step"] = "description"
        session["_timestamp"] = time.time()
        bot.send_message(
            message.chat.id,
            "لطفاً توضیحات فیلم را وارد کنید (متن اول کپشن):\nیا /skip برای رد کردن"
        )
        
    elif step == "description":
        session["film_description"] = None if text == "/skip" else text
        session["step"] = "quality"
        session["_timestamp"] = time.time()
        bot.send_message(message.chat.id, "لطفاً کیفیت اول را وارد کنید (مثال: 720p):")
        
    elif step == "quality":
        session["current_quality"] = text
        session["step"] = "file"
        session["_timestamp"] = time.time()
        bot.send_message(
            message.chat.id,
            f"✅ کیفیت '{text}' ثبت شد\n\nلطفاً فایل را ارسال کنید:"
        )

def handle_series_flow(message, text, step, session):
    if step == "key":
        session["series_key"] = text
        session["step"] = "title"
        session["_timestamp"] = time.time()
        bot.send_message(message.chat.id, "لطفاً عنوان سریال را وارد کنید:")
        
    elif step == "title":
        session["series_title"] = text
        session["step"] = "poster_desc"
        session["_timestamp"] = time.time()
        bot.send_message(
            message.chat.id,
            "لطفاً توضیحات پوستر را وارد کنید:\nیا /skip برای رد کردن"
        )
        
    elif step == "poster_desc":
        session["poster_description"] = None if text == "/skip" else text
        session["step"] = "poster_file"
        session["_timestamp"] = time.time()
        bot.send_message(
            message.chat.id,
            "لطفاً عکس پوستر را ارسال کنید:\nیا /skip برای رد کردن"
        )
        
    elif step == "poster_file":
        if text == "/skip":
            session["poster_file_id"] = None
            session["step"] = "caption_template"
            session["_timestamp"] = time.time()
            bot.send_message(
                message.chat.id,
                "لطفاً قالب کپشن قسمت‌ها را وارد کنید:\nیا /skip برای استفاده از قالب پیش‌فرض"
            )
        else:
            bot.send_message(message.chat.id, "❌ لطفاً یک عکس ارسال کنید یا /skip بزنید")
            
    elif step == "caption_template":
        session["caption_template"] = None if text == "/skip" else text
        session["_timestamp"] = time.time()
        
        # Create series in Supabase
        try:
            supabase.table("series").insert({
                "key": session["series_key"],
                "title": session["series_title"],
                "poster_file_id": session.get("poster_file_id"),
                "poster_description": session.get("poster_description"),
                "caption_template": session.get("caption_template")
            }).execute()
            
            session["step"] = "episode_number"
            session["_timestamp"] = time.time()
            bot.send_message(
                message.chat.id,
                "✅ سریال ایجاد شد!\n\nلطفاً شماره قسمت اول را وارد کنید:\n(یا /done برای اتمام)"
            )
        except Exception as e:
            logger.error(f"Error creating series: {e}")
            bot.send_message(message.chat.id, "❌ خطا در ایجاد سریال")
            admin_sessions.pop(message.from_user.id, None)
        
    elif step == "episode_number":
        if text.lower() in ['/done', 'done', 'اتمام']:
            # Finish series
            bot_username = bot.get_me().username
            deeplink = f"https://t.me/{bot_username}?start={session['series_key']}"
            
            bot.send_message(
                message.chat.id,
                f"✅ سریال با موفقیت ایجاد شد!\n\n"
                f"🔑 کلید: `{session['series_key']}`\n"
                f"📝 توضیحات: {session.get('poster_description', 'بدون توضیح')}\n\n"
                f"دیپ‌لینک:\n`{deeplink}`",
                parse_mode='Markdown'
            )
            
            admin_sessions.pop(message.from_user.id, None)
            return
            
        try:
            episode_num = int(text)
            session["current_episode"] = episode_num
            session["step"] = "episode_quality"
            session["_timestamp"] = time.time()
            bot.send_message(
                message.chat.id,
                f"✅ قسمت {episode_num} انتخاب شد\n\nلطفاً کیفیت این قسمت را وارد کنید:"
            )
        except ValueError:
            bot.send_message(message.chat.id, "❌ لطفاً یک عدد معتبر وارد کنید")
            
    elif step == "episode_quality":
        session["current_quality"] = text
        session["step"] = "episode_file"
        session["_timestamp"] = time.time()
        bot.send_message(
            message.chat.id,
            f"✅ کیفیت '{text}' ثبت شد\n\nلطفاً فایل این قسمت را ارسال کنید:"
        )

# ==================== FILE HANDLERS ====================

@bot.message_handler(
    func=lambda message: message.from_user.id in ADMINS and message.photo,
    content_types=['photo']
)
def admin_photo_handler(message):
    user_id = message.from_user.id
    session = admin_sessions.get(user_id)
    
    if not session or session.get("step") != "poster_file":
        return
    
    file_id = message.photo[-1].file_id
    session["poster_file_id"] = file_id
    session["step"] = "caption_template"
    session["_timestamp"] = time.time()
    
    bot.send_message(
        message.chat.id,
        "✅ پوستر ثبت شد\n\nلطفاً قالب کپشن قسمت‌ها را وارد کنید (متن اول):\nیا /skip برای قالب پیش‌فرض"
    )

@bot.message_handler(
    func=lambda message: message.from_user.id in ADMINS and 
    (message.document or message.video or message.audio),
    content_types=['document', 'video', 'audio']
)
def admin_file_handler(message):
    user_id = message.from_user.id
    session = admin_sessions.get(user_id)
    
    if not session:
        return
    
    step = session.get("step")
    mode = session.get("mode")
    
    # Handle film files
    if mode == "add_film" and step == "file":
        handle_film_file(message, session, user_id)
    
    # Handle series episode files
    elif mode == "add_series" and step == "episode_file":
        handle_episode_file(message, session, user_id)

def handle_film_file(message, session, user_id):
    # Get file_id based on content type
    if message.document:
        file_id = message.document.file_id
    elif message.video:
        file_id = message.video.file_id
    elif message.audio:
        file_id = message.audio.file_id
    else:
        bot.send_message(message.chat.id, "❌ لطفاً یک فایل معتبر ارسال کنید")
        return
    
    # Create caption با متن اول
    caption = create_film_caption(
        session.get("film_description"), 
        session["current_quality"]
    )
    
    # Save to Supabase
    try:
        # فقط چک کن فیلم وجود داره یا نه، اگر نه ایجادش کن
        film_exists = supabase.table("films").select("key").eq("key", session["film_key"]).execute()
        
        if not film_exists.data:
            # فیلم وجود نداره، ایجادش کن
            supabase.table("films").insert({
                "key": session["film_key"],
                "title": session["film_title"],
                "description": session.get("film_description")
            }).execute()
            logger.info(f"✅ Film created: {session['film_key']}")
        else:
            logger.info(f"✅ Film already exists: {session['film_key']}")
        
        # اضافه کردن کیفیت جدید
        quality_result = supabase.table("film_qualities").upsert({
            "film_key": session["film_key"],
            "quality": session["current_quality"],
            "file_id": file_id,
            "caption": caption,
            "added_by": user_id
        }).execute()
        
        logger.info(f"✅ Quality added - Film: {session['film_key']}, Quality: {session['current_quality']}")
        
        # Generate deeplink
        bot_username = bot.get_me().username
        deeplink = f"https://t.me/{bot_username}?start={session['film_key']}"
        
        keyboard = InlineKeyboardMarkup()
        keyboard.row(
            InlineKeyboardButton("➕ کیفیت دیگر", callback_data="admin_add_another_quality"),
            InlineKeyboardButton("✅ اتمام", callback_data="admin_done")
        )
        
        bot.send_message(
            message.chat.id,
            f"✅ کیفیت '{session['current_quality']}' اضافه شد!\n\n"
            f"🔑 کلید: `{session['film_key']}`\n"
            f"🎬 فیلم: {session['film_title']}\n\n"
            f"🔗 دیپ‌لینک:\n`{deeplink}`",
            reply_markup=keyboard,
            parse_mode='Markdown'
        )
        
        session["step"] = "complete"
        
    except Exception as e:
        logger.error(f"❌ Error saving film to Supabase: {str(e)}")
        bot.send_message(message.chat.id, f"❌ خطا در ذخیره فیلم: {str(e)}")

def handle_episode_file(message, session, user_id):
    # Get file_id based on content type
    if message.document:
        file_id = message.document.file_id
    elif message.video:
        file_id = message.video.file_id
    elif message.audio:
        file_id = message.audio.file_id
    else:
        bot.send_message(message.chat.id, "❌ لطفاً یک فایل معتبر ارسال کنید")
        return
    
    # Create episode caption
    caption = create_episode_caption(
        session.get("caption_template"),
        session["current_episode"],
        session["current_quality"]
    )
    
    # Add episode to Supabase
    try:
        supabase.table("episodes").insert({
            "series_key": session["series_key"],
            "episode_number": session["current_episode"],
            "quality": session["current_quality"],
            "file_id": file_id,
            "caption": caption,
            "added_by": user_id
        }).execute()
        
        # Generate deeplink
        bot_username = bot.get_me().username
        series_deeplink = f"https://t.me/{bot_username}?start={session['series_key']}"
        
        keyboard = InlineKeyboardMarkup()
        keyboard.row(
            InlineKeyboardButton("➕ قسمت دیگر", callback_data="admin_add_another_episode"),
            InlineKeyboardButton("➕ کیفیت دیگر", callback_data="admin_add_episode_quality")
        )
        keyboard.add(InlineKeyboardButton("✅ اتمام", callback_data="admin_done_series"))
        
        bot.send_message(
            message.chat.id,
            f"✅ قسمت {session['current_episode']} با کیفیت {session['current_quality']} اضافه شد!\n\n"
            f"🔗 دیپ‌لینک سریال:\n`{series_deeplink}`",
            reply_markup=keyboard,
            parse_mode='Markdown'
        )
        
        session["step"] = "episode_complete"
    except Exception as e:
        logger.error(f"Error saving episode to Supabase: {e}")
        bot.send_message(message.chat.id, "❌ خطا در ذخیره قسمت")

# ==================== QUALITY SELECTION HANDLERS ====================

@bot.callback_query_handler(func=lambda call: call.data.startswith('quality:'))
def quality_callback_handler(call):
    data = call.data
    parts = data.split(':')
    
    if len(parts) == 3:
        film_key = parts[1]
        quality = parts[2]
        
        try:
            result_response = supabase.table("film_qualities").select("file_id, caption").eq("film_key", film_key).eq("quality", quality).execute()
            if result_response.data:
                result = result_response.data[0]
                file_id = result['file_id']
                caption = result['caption']
                
                try:
                    bot.send_document(call.message.chat.id, file_id, caption=caption)
                    bot.answer_callback_query(call.id, "✅ فایل ارسال شد")
                except Exception as e:
                    logger.error(f"Error sending file: {e}")
                    bot.answer_callback_query(call.id, "❌ خطا در ارسال فایل")
            else:
                bot.answer_callback_query(call.id, "❌ فایل یافت نشد")
        except Exception as e:
            logger.error(f"Error fetching file: {e}")
            bot.answer_callback_query(call.id, "❌ خطا در دریافت فایل")

@bot.callback_query_handler(func=lambda call: call.data.startswith('episode:'))
def episode_callback_handler(call):
    data = call.data
    parts = data.split(':')
    
    if len(parts) == 4:
        series_key = parts[1]
        episode_num = int(parts[2])
        quality = parts[3]
        
        try:
            result_response = supabase.table("episodes").select("file_id, caption").eq("series_key", series_key).eq("episode_number", episode_num).eq("quality", quality).execute()
            if result_response.data:
                result = result_response.data[0]
                file_id = result['file_id']
                caption = result['caption']
                
                try:
                    bot.send_document(call.message.chat.id, file_id, caption=caption)
                    bot.answer_callback_query(call.id, "✅ فایل ارسال شد")
                except Exception as e:
                    logger.error(f"Error sending file: {e}")
                    bot.answer_callback_query(call.id, "❌ خطا در ارسال فایل")
            else:
                bot.answer_callback_query(call.id, "❌ فایل یافت نشد")
        except Exception as e:
            logger.error(f"Error fetching file: {e}")
            bot.answer_callback_query(call.id, "❌ خطا در دریافت فایل")

# ==================== OTHER HANDLERS ====================

@bot.callback_query_handler(func=lambda call: call.data == 'check_join')
def check_join_handler(call):
    if check_membership(call.from_user.id):
        bot.answer_callback_query(call.id, "✅ عضویت شما تایید شد!")
        bot.edit_message_text(
            "✅ عضویت شما تایید شد!\nاکنون می‌توانید از لینک‌ها استفاده کنید.",
            call.message.chat.id,
            call.message.message_id
        )
    else:
        bot.answer_callback_query(call.id, "❌ هنوز عضو نشدید")
        bot.edit_message_text(
            "❌ هنوز عضو نشدید. لطفاً در کانال عضو شوید:",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=build_join_keyboard()
        )

@bot.message_handler(func=lambda message: True)
def all_messages(message):
    if message.from_user.id in ADMINS:
        show_admin_panel(message)
    else:
        show_user_welcome(message)

# ==================== MAIN EXECUTION ====================

def run_flask():
    port = int(os.getenv("PORT", 10000))
    app.run(host='0.0.0.0', port=port, debug=False)

def run_bot():
    """تابع بهبود یافته برای اجرای بات"""
    global restart_count
    logger.info("Starting bot polling with health monitoring...")
    
    # متغیرهای مانیتورینگ
    last_restart = datetime.now()
    
    while True:
        try:
            logger.info(f"🚀 Starting bot polling (attempt {restart_count + 1})")
            
            # پاکسازی sessionهای قدیمی
            cleanup_old_sessions()
            
            # شروع polling با تنظیمات بهتر
            bot.infinity_polling(
                timeout=60, 
                long_polling_timeout=60, 
                skip_pending=True
            )
            
        except Exception as e:
            restart_count += 1
            current_time = datetime.now()
            time_since_last_restart = (current_time - last_restart).total_seconds()
            last_restart = current_time
            
            logger.error(f"Bot polling error: {e}")
            
            # اگر بیش از 5 بار در 5 دقیقه restart کرد، بیشتر صبر کن
            if restart_count > 5 and time_since_last_restart < 300:
                wait_time = 300  # 5 دقیقه
                logger.warning(f"⚠️ Too many restarts. Waiting {wait_time} seconds...")
            else:
                wait_time = 30
            
            logger.info(f"Restarting bot in {wait_time} seconds...")
            time.sleep(wait_time)
            
            # اگر بیش از 20 بار restart کرد، لاگ ویژه
            if restart_count > 20:
                logger.critical(f"🚨 CRITICAL: Bot restarted {restart_count} times!")

if __name__ == "__main__":
    logger.info("🚀 Starting bot with IMPROVED stability...")
    
    # ثبت زمان شروع
    start_time = datetime.now()
    logger.info(f"📅 Start time: {start_time}")
    
    try:
        # Start keep-alive mechanism (بهبود یافته)
        start_keep_alive()
        
        # Start bot in a separate thread
        bot_thread = threading.Thread(target=run_bot, daemon=True)
        bot_thread.start()
        
        logger.info("✅ Bot thread started successfully!")
        
        # نمایش اطلاعات
        bot_info = bot.get_me()
        logger.info(f"🤖 Bot info: @{bot_info.username} (ID: {bot_info.id})")
        
        # Start Flask in main thread
        port = int(os.getenv("PORT", 10000))
        logger.info(f"🌐 Starting Flask on port {port}")
        run_flask()
        
    except Exception as e:
        logger.critical(f"🚨 CRITICAL ERROR in main: {e}")
        raise
