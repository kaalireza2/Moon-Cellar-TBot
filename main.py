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
import gzip
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

# === Configuration ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMINS = [int(x) for x in os.getenv("ADMINS", "").split(",") if x.strip() and x.strip().isdigit()]
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "").strip()
CHANNEL_CHAT_ID = os.getenv("CHANNEL_CHAT_ID", "").strip()
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "")

# ==================== تنظیمات بکاپ ====================
BACKUP_CHANNEL_ID = os.getenv("BACKUP_CHANNEL_ID", "")
BACKUP_ADMIN_ID = os.getenv("BACKUP_ADMIN_ID", "")
BACKUP_TIME = os.getenv("BACKUP_TIME", "03:00")
MAX_BACKUPS = int(os.getenv("MAX_BACKUPS", "30"))

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

# ==================== Spam detection ====================
user_message_times = {}  # {user_id: [list of timestamps]}
SPAM_WINDOW = 10         # seconds
SPAM_LIMIT = 5           # number of messages allowed in window
SPAM_NOTIFIED = {}       # {user_id: bool} to avoid repeated alerts

# ==================== کلاس BackupManager ====================

class BackupManager:
    def __init__(self, supabase_client, bot_instance):
        self.supabase = supabase_client
        self.bot = bot_instance
        self.backup_path = "/tmp/backups"
        
    def create_backup(self) -> dict:
        """ایجاد بکاپ کامل از دیتابیس"""
        try:
            os.makedirs(self.backup_path, exist_ok=True)
            
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_data = {
                "metadata": {
                    "timestamp": datetime.now().isoformat(),
                    "version": "1.0",
                    "type": "full_backup"
                },
                "data": {}
            }
            
            # اضافه کردن جدول users به بکاپ
            tables = ["films", "film_qualities", "series", "episodes", "users"]
            
            for table in tables:
                try:
                    response = self.supabase.table(table).select("*").execute()
                    backup_data["data"][table] = response.data
                    logger.info(f"✅ Backup: {table} - {len(response.data)} records")
                except Exception as e:
                    logger.error(f"❌ Error backing up {table}: {e}")
                    backup_data["data"][table] = []
            
            backup_file = os.path.join(self.backup_path, f"backup_{timestamp}.json")
            with open(backup_file, 'w', encoding='utf-8') as f:
                json.dump(backup_data, f, ensure_ascii=False, indent=2)
            
            compressed_file = f"{backup_file}.gz"
            with open(backup_file, 'rb') as f_in:
                with gzip.open(compressed_file, 'wb') as f_out:
                    f_out.writelines(f_in)
            
            os.remove(backup_file)
            
            file_size = os.path.getsize(compressed_file) / (1024 * 1024)
            
            backup_info = {
                "file_path": compressed_file,
                "timestamp": timestamp,
                "size_mb": round(file_size, 2),
                "tables": backup_data["data"],
                "filename": f"backup_{timestamp}.json.gz"
            }
            
            logger.info(f"✅ Backup created: {backup_info['filename']} ({backup_info['size_mb']} MB)")
            self.cleanup_old_backups()
            
            return backup_info
            
        except Exception as e:
            logger.error(f"❌ Backup failed: {e}")
            return None
    
    def cleanup_old_backups(self):
        """پاکسازی بکاپ‌های قدیمی"""
        if MAX_BACKUPS <= 0:
            return
        try:
            if not os.path.exists(self.backup_path):
                return
            files = [f for f in os.listdir(self.backup_path) if f.endswith('.gz')]
            files.sort()
            
            while len(files) > MAX_BACKUPS:
                old_file = files.pop(0)
                os.remove(os.path.join(self.backup_path, old_file))
                logger.info(f"🗑️ Removed old backup: {old_file}")
        except Exception as e:
            logger.error(f"Error cleaning backups: {e}")
    
    def restore_from_backup(self, backup_file_path: str) -> bool:
        """بازیابی دیتابیس از فایل بکاپ"""
        try:
            if backup_file_path.endswith('.gz'):
                with gzip.open(backup_file_path, 'rb') as f:
                    backup_data = json.loads(f.read().decode('utf-8'))
            else:
                with open(backup_file_path, 'r', encoding='utf-8') as f:
                    backup_data = json.load(f)
            
            tables = ["films", "film_qualities", "series", "episodes", "users"]
            
            for table in tables:
                if table in backup_data["data"]:
                    # پاک کردن اطلاعات قبلی
                    self.supabase.table(table).delete().neq("id", 0).execute()
                    
                    # اضافه کردن اطلاعات جدید بدون فیلد id
                    for record in backup_data["data"][table]:
                        record_copy = {k: v for k, v in record.items() if k != 'id'}
                        self.supabase.table(table).insert(record_copy).execute()
                    
                    logger.info(f"✅ Restored {table}: {len(backup_data['data'][table])} records")
            
            logger.info("✅ Database restored successfully")
            return True
            
        except Exception as e:
            logger.error(f"❌ Restore failed: {e}")
            return False
    
    def send_backup_to_telegram(self, backup_info: dict):
        """ارسال بکاپ به تلگرام"""
        file_path = backup_info["file_path"]
        try:
            stats = []
            for table, data in backup_info["tables"].items():
                if data:
                    stats.append(f"• {table}: {len(data)} رکورد")
            
            stats_text = "\n".join(stats) if stats else "• هیچ داده‌ای موجود نیست"
            
            caption = (
                f"✅ **پشتیبان‌گیری خودکار**\n\n"
                f"🕐 **زمان:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"📁 **نام فایل:** `{backup_info['filename']}`\n"
                f"💾 **حجم:** {backup_info['size_mb']} MB\n\n"
                f"📊 **آمار جداول:**\n{stats_text}"
            )
            
            if BACKUP_CHANNEL_ID:
                try:
                    with open(file_path, 'rb') as f:
                        self.bot.send_document(
                            chat_id=BACKUP_CHANNEL_ID,
                            document=f,
                            caption=caption,
                            parse_mode='Markdown'
                        )
                    logger.info(f"✅ Backup sent to channel: {BACKUP_CHANNEL_ID}")
                except Exception as e:
                    logger.error(f"❌ Failed to send backup to channel {BACKUP_CHANNEL_ID}: {e}")
            
            if BACKUP_ADMIN_ID:
                try:
                    with open(file_path, 'rb') as f:
                        self.bot.send_document(
                            chat_id=BACKUP_ADMIN_ID,
                            document=f,
                            caption=caption,
                            parse_mode='Markdown'
                        )
                    logger.info(f"✅ Backup sent to admin: {BACKUP_ADMIN_ID}")
                except Exception as e:
                    logger.error(f"❌ Failed to send backup to admin {BACKUP_ADMIN_ID}: {e}")
            
        except Exception as e:
            logger.error(f"❌ Error in send_backup_to_telegram: {e}")
        finally:
            if os.path.exists(file_path):
                os.remove(file_path)
                logger.info(f"🗑️ Removed backup file: {file_path}")

backup_manager = None

# ==================== IMPROVED KEEP ALIVE MECHANISM ====================

def keep_alive_ping():
    try:
        if RENDER_EXTERNAL_URL:
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
    def ping_loop():
        while True:
            try:
                keep_alive_ping()
                time.sleep(45)
            except Exception as e:
                logger.error(f"Keep-alive loop error: {e}")
                time.sleep(30)
    keep_alive_thread = threading.Thread(target=ping_loop, daemon=True)
    keep_alive_thread.start()
    logger.info("🔄 Keep-alive for FREE RENDER started (45 seconds interval)")

# ==================== SYSTEM MONITORING ====================

def cleanup_old_sessions():
    try:
        current_time = time.time()
        keys_to_remove = []
        for user_id, session in admin_sessions.items():
            if current_time - session.get('_timestamp', 0) > 3600:
                keys_to_remove.append(user_id)
        for key in keys_to_remove:
            admin_sessions.pop(key, None)
            logger.info(f"🧹 Cleaned up old session for user {key}")
    except Exception as e:
        logger.error(f"Error cleaning sessions: {e}")

def get_system_status():
    try:
        test_result = supabase.table("films").select("count", count="exact").limit(1).execute()
        bot_info = bot.get_me()
        uptime_seconds = time.time() - psutil.boot_time()
        uptime_str = str(timedelta(seconds=uptime_seconds))
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
    caption_parts = []
    if description:
        caption_parts.append(description)
    caption_parts.append("#زیرنویس_چسبیده_فارسی🍷")
    caption_parts.append(f"کیفیت {quality} #بدون_سانسور")
    return "\n".join(caption_parts)

def create_episode_caption(template, episode_num, quality):
    caption_parts = []
    if template:
        caption_parts.append(template)
    caption_parts.append(f"قسمت {episode_num}")
    caption_parts.append("#زیرنویس_چسبیده_فارسی🍷")
    caption_parts.append(f"کیفیت {quality} #بدون_سانسور")
    return "\n".join(caption_parts)

def build_join_keyboard():
    keyboard = InlineKeyboardMarkup()
    if CHANNEL_USERNAME:
        keyboard.add(InlineKeyboardButton("🔒 عضویت در کانال", url=f"https://t.me/{CHANNEL_USERNAME.lstrip('@')}"))
    keyboard.add(InlineKeyboardButton("🔄 بررسی عضویت", callback_data="check_join"))
    return keyboard

# ==================== USER MANAGEMENT FUNCTIONS ====================

def register_user(user):
    """ثبت یا به‌روزرسانی اطلاعات کاربر در دیتابیس"""
    try:
        existing = supabase.table("users").select("user_id").eq("user_id", user.id).execute()
        user_data = {
            "user_id": user.id,
            "username": user.username,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "last_active": datetime.now().isoformat()
        }
        if not existing.data:
            user_data["joined_at"] = datetime.now().isoformat()
            supabase.table("users").insert(user_data).execute()
            logger.info(f"✅ New user registered: {user.id} (@{user.username})")
        else:
            supabase.table("users").update(user_data).eq("user_id", user.id).execute()
            logger.debug(f"🔄 Updated user activity: {user.id}")
    except Exception as e:
        logger.error(f"Error registering user {user.id}: {e}")

def update_user_downloads(user_id):
    try:
        supabase.table("users").update({"total_downloads": supabase.raw("total_downloads + 1")}).eq("user_id", user_id).execute()
    except Exception as e:
        logger.error(f"Error updating downloads for {user_id}: {e}")

def is_user_banned(user_id):
    try:
        result = supabase.table("users").select("is_banned").eq("user_id", user_id).execute()
        if result.data:
            return result.data[0].get("is_banned", False)
        return False
    except:
        return False

def is_user_admin(user_id):
    if user_id in ADMINS:
        return True
    try:
        result = supabase.table("users").select("is_admin").eq("user_id", user_id).execute()
        if result.data:
            return result.data[0].get("is_admin", False)
        return False
    except:
        return False

def get_user_stats():
    try:
        total = supabase.table("users").select("count", count="exact").execute()
        active_today = supabase.table("users").select("count", count="exact").gte("last_active", datetime.now().replace(hour=0, minute=0, second=0).isoformat()).execute()
        banned = supabase.table("users").select("count", count="exact").eq("is_banned", True).execute()
        return {
            "total": total.count,
            "active_today": active_today.count,
            "banned": banned.count
        }
    except Exception as e:
        logger.error(f"Error getting user stats: {e}")
        return {"total": 0, "active_today": 0, "banned": 0}

# ==================== Spam check function ====================
def check_spam(user_id, username=None, first_name=None):
    """بررسی اسپم و ارسال هشدار به ادمین در صورت نیاز"""
    now = time.time()
    if user_id not in user_message_times:
        user_message_times[user_id] = []
    # حذف تایم‌های قدیمی‌تر از پنجره
    user_message_times[user_id] = [t for t in user_message_times[user_id] if now - t < SPAM_WINDOW]
    user_message_times[user_id].append(now)
    
    if len(user_message_times[user_id]) > SPAM_LIMIT:
        # اسپم تشخیص داده شد
        if user_id not in SPAM_NOTIFIED:
            SPAM_NOTIFIED[user_id] = True
            spam_text = (f"🚨 **اسپم تشخیص داده شد!**\n\n"
                         f"🆔 کاربر: `{user_id}`\n"
                         f"👤 نام: {first_name or '?'} (@{username or 'نامشخص'})\n"
                         f"📨 تعداد پیام در {SPAM_WINDOW} ثانیه: {len(user_message_times[user_id])}\n\n"
                         f"لطفاً برخورد لازم انجام دهید.")
            for admin_id in ADMINS:
                try:
                    bot.send_message(admin_id, spam_text, parse_mode='Markdown')
                except:
                    pass
            # می‌توانید کاربر را خودکار مسدود کنید: supabase.table("users").update({"is_banned": True}).eq("user_id", user_id).execute()
            return True
    else:
        # اگر قبلاً هشدار داده شده و الان اسپم متوقف شده، پس از مدتی علامت را پاک کن (اختیاری)
        # برای سادگی فعلاً پاک نمی‌کنیم
        pass
    return False

# ==================== FLASK ROUTES ====================

@app.route('/')
def home():
    return "✅ Bot is running!", 200

@app.route('/health')
def health():
    return {"status": "healthy", "timestamp": datetime.now().isoformat(), "service": "Telegram Film Bot"}, 200

@app.route('/ping')
def ping():
    return "pong", 200

@app.route('/status')
def status():
    try:
        status_info = get_system_status()
        if "error" in status_info:
            return {"status": "error", "error": status_info["error"], "timestamp": datetime.now().isoformat()}, 500
        return {"status": "running", "data": status_info, "timestamp": datetime.now().isoformat()}, 200
    except Exception as e:
        return {"status": "error", "error": str(e), "timestamp": datetime.now().isoformat()}, 500

@app.route('/bot_health')
def bot_health():
    try:
        bot_info = bot.get_me()
        return {"status": "healthy", "bot_username": bot_info.username, "bot_id": bot_info.id, "timestamp": datetime.now().isoformat(), "message": "Bot is running"}, 200
    except Exception as e:
        return {"status": "unhealthy", "error": str(e), "timestamp": datetime.now().isoformat(), "message": "Bot is not responding"}, 500

# ==================== BOT HANDLERS ====================

@bot.message_handler(commands=['start'])
def start_handler(message):
    user = message.from_user
    register_user(user)  # ثبت کاربر
    if is_user_banned(user.id):
        bot.send_message(message.chat.id, "⛔ شما از دسترسی به ربات مسدود شده‌اید.")
        return
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
    if not is_user_admin(message.from_user.id):
        return
    register_user(message.from_user)
    if is_user_banned(message.from_user.id):
        return
    try:
        status_info = get_system_status()
        if "error" in status_info:
            bot.send_message(message.chat.id, f"❌ خطا در بررسی وضعیت: {status_info['error']}")
            return
        status_text = f"""
📊 <b>وضعیت سیستم</b>

🤖 <b>بات:</b>
• نام: @{status_info['bot_username']}
• آیدی: {status_info['bot_id']}
• وضعیت: فعال ✅

🗄️ <b>دیتابیس:</b>
• وضعیت: {status_info['database_status'].upper()}
• تعداد فیلم‌ها: {status_info['database_count']}

🖥️ <b>سرور:</b>
• سیستم: {status_info['system']}
• Uptime: {status_info['uptime']}
• حافظه: {status_info['memory_percent']}%
• شروع: {status_info['start_time']}

📈 <b>آمار:</b>
• Restartها: {status_info['restart_count']}
• Sessionها: {status_info['admin_sessions']}
• زمان: {status_info['current_time']}

🔄 Keep-alive: فعال
📡 پینگ: هر 45 ثانیه
        """
        bot.send_message(message.chat.id, status_text, parse_mode='HTML')
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ خطا در بررسی وضعیت: {str(e)}")
# ==================== دستورات بکاپ ====================

@bot.message_handler(commands=['backup'])
def backup_command(message):
    if not is_user_admin(message.from_user.id):
        return
    register_user(message.from_user)
    if is_user_banned(message.from_user.id):
        return
    bot.send_message(message.chat.id, "🔄 در حال گرفتن پشتیبان...")
    if not backup_manager:
        bot.send_message(message.chat.id, "❌ سیستم پشتیبان‌گیری راه‌اندازی نشده")
        return
    backup_info = backup_manager.create_backup()
    if backup_info:
        try:
            with open(backup_info["file_path"], 'rb') as f:
                bot.send_document(message.chat.id, f, caption=f"✅ پشتیبان دستی\n📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n💾 حجم: {backup_info['size_mb']} MB")
            os.remove(backup_info["file_path"])
        except Exception as e:
            bot.send_message(message.chat.id, f"❌ خطا در ارسال: {e}")
    else:
        bot.send_message(message.chat.id, "❌ خطا در گرفتن پشتیبان")

@bot.message_handler(commands=['restore'])
def restore_command(message):
    if not is_user_admin(message.from_user.id):
        return
    register_user(message.from_user)
    if is_user_banned(message.from_user.id):
        return
    bot.send_message(message.chat.id, "📂 لطفاً فایل بکاپ (json یا json.gz) را ارسال کنید.\n\n⚠️ **هشدار**: این کار تمام اطلاعات فعلی را پاک می‌کند!")

# ==================== USER MANAGEMENT COMMANDS ====================

@bot.message_handler(commands=['users'])
def users_stats_command(message):
    if not is_user_admin(message.from_user.id):
        return
    stats = get_user_stats()
    text = f"📊 **آمار کاربران**\n\n👥 کل کاربران: {stats['total']}\n✅ فعال‌های امروز: {stats['active_today']}\n🚫 مسدود شده‌ها: {stats['banned']}"
    bot.send_message(message.chat.id, text, parse_mode='Markdown')

@bot.message_handler(commands=['userinfo'])
def userinfo_command(message):
    if not is_user_admin(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 2:
        bot.send_message(message.chat.id, "❌ لطفاً آیدی یا یوزرنیم کاربر را وارد کنید.\nمثال: <code>/userinfo 123456789</code> یا <code>/userinfo @username</code>", parse_mode='HTML')
        return
    target = args[1]
    try:
        if target.startswith('@'):
            username = target.lstrip('@')
            result = supabase.table("users").select("*").ilike("username", username).execute()
        else:
            user_id = int(target)
            result = supabase.table("users").select("*").eq("user_id", user_id).execute()
        if not result.data:
            bot.send_message(message.chat.id, "❌ کاربر یافت نشد.")
            return
        u = result.data[0]
        text = f"""👤 <b>اطلاعات کاربر</b>

🆔 آیدی: <code>{u['user_id']}</code>
📛 نام: {u.get('first_name', '?')} {u.get('last_name', '')}
🔖 یوزرنیم: @{u.get('username', 'ندارد')}
📅 عضو شده: {u['joined_at'][:19]}
🕘 آخرین فعالیت: {u['last_active'][:19]}
📥 دانلودها: {u['total_downloads']}
🚫 مسدود: {'بله' if u['is_banned'] else 'خیر'}
👑 ادمین: {'بله' if u['is_admin'] else 'خیر'}"""
        bot.send_message(message.chat.id, text, parse_mode='HTML')
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ خطا: {e}")
        
@bot.message_handler(commands=['ban'])
def ban_user_command(message):
    if not is_user_admin(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 2:
        bot.send_message(message.chat.id, "❌ لطفاً آیدی عددی کاربر را وارد کنید.\nمثال: `/ban 123456789`", parse_mode='Markdown')
        return
    try:
        user_id = int(args[1])
        supabase.table("users").update({"is_banned": True}).eq("user_id", user_id).execute()
        bot.send_message(message.chat.id, f"✅ کاربر `{user_id}` مسدود شد.", parse_mode='Markdown')
        try:
            bot.send_message(user_id, "⛔ شما توسط ادمین مسدود شده‌اید.")
        except:
            pass
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ خطا: {e}")

@bot.message_handler(commands=['unban'])
def unban_user_command(message):
    if not is_user_admin(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 2:
        bot.send_message(message.chat.id, "❌ لطفاً آیدی عددی کاربر را وارد کنید.\nمثال: `/unban 123456789`", parse_mode='Markdown')
        return
    try:
        user_id = int(args[1])
        supabase.table("users").update({"is_banned": False}).eq("user_id", user_id).execute()
        bot.send_message(message.chat.id, f"✅ مسدودی کاربر `{user_id}` لغو شد.", parse_mode='Markdown')
        try:
            bot.send_message(user_id, "✅ شما توسط ادمین رفع مسدود شدید.")
        except:
            pass
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ خطا: {e}")

@bot.message_handler(commands=['broadcast'])
def broadcast_command(message):
    if not is_user_admin(message.from_user.id):
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        bot.send_message(message.chat.id, "❌ لطفاً متن پیام را وارد کنید.\nمثال: `/broadcast سلام به همه`\n\n⚠️ می‌توانید از HTML استفاده کنید:\n`<b>bold</b>`, `<i>italic</i>`, `<tg-spoiler>spoiler</tg-spoiler>`, `<code>code</code>`, `<pre>pre</pre>`, `<a href=\"url\">link</a>`", parse_mode='Markdown')
        return
    text = args[1]
    try:
        users = supabase.table("users").select("user_id").eq("is_banned", False).execute()
        if not users.data:
            bot.send_message(message.chat.id, "❌ هیچ کاربر فعالی یافت نشد.")
            return
        sent = 0
        failed = 0
        bot.send_message(message.chat.id, f"🔄 در حال ارسال پیام به {len(users.data)} کاربر...")
        for user in users.data:
            try:
                bot.send_message(user['user_id'], text, parse_mode='HTML')
                sent += 1
                time.sleep(0.05)
            except:
                failed += 1
        bot.send_message(message.chat.id, f"✅ ارسال شد: {sent}\n❌ ناموفق: {failed}")
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ خطا: {e}")

# ==================== USERS LIST (PAGINATED) ====================

@bot.message_handler(commands=['userslist'])
def userslist_command(message):
    if not is_user_admin(message.from_user.id):
        return
    page = 1
    args = message.text.split()
    if len(args) > 1:
        try:
            page = int(args[1])
        except:
            page = 1
    show_users_page(message.chat.id, page, message.message_id)

def show_users_page(chat_id, page, message_id=None):
    """نمایش صفحه از لیست کاربران با صفحه‌بندی - با HTML"""
    per_page = 10
    offset = (page - 1) * per_page
    try:
        total_count = supabase.table("users").select("count", count="exact").execute().count
        response = supabase.table("users").select("user_id, username, first_name, last_name, last_active, total_downloads, is_banned, is_admin").order("last_active", desc=True).range(offset, offset + per_page - 1).execute()
        users = response.data
        if not users:
            text = "❌ هیچ کاربری یافت نشد."
            if message_id:
                bot.edit_message_text(text, chat_id, message_id)
            else:
                bot.send_message(chat_id, text)
            return
        text = f"📋 <b>لیست کاربران (صفحه {page})</b>\n\n"
        for u in users:
            status = "🚫" if u.get('is_banned') else "✅"
            admin_flag = "👑 " if u.get('is_admin') else ""
            username_str = f" @{u['username']}" if u.get('username') else ""
            name = f"{u.get('first_name', '')} {u.get('last_name', '')}".strip() or "بدون نام"
            last_active = u['last_active'][:19] if u.get('last_active') else "نامشخص"
            downloads = u.get('total_downloads', 0)
            text += f"{status} {admin_flag}<code>{u['user_id']}</code>{username_str}\n📛 {name}\n🕒 {last_active}\n📥 {downloads}\n\n"
        total_pages = (total_count + per_page - 1) // per_page
        keyboard = InlineKeyboardMarkup()
        if page > 1:
            keyboard.add(InlineKeyboardButton("◀️ قبلی", callback_data=f"users_page_{page-1}"))
        if page < total_pages:
            if keyboard.keyboard:
                keyboard.add(InlineKeyboardButton("بعدی ▶️", callback_data=f"users_page_{page+1}"))
            else:
                keyboard.add(InlineKeyboardButton("بعدی ▶️", callback_data=f"users_page_{page+1}"))
        if message_id:
            bot.edit_message_text(text, chat_id, message_id, reply_markup=keyboard, parse_mode='HTML')
        else:
            bot.send_message(chat_id, text, reply_markup=keyboard, parse_mode='HTML')
    except Exception as e:
        logger.error(f"Error in userslist: {e}")
        bot.send_message(chat_id, f"❌ خطا: {e}")

@bot.callback_query_handler(func=lambda call: call.data.startswith('users_page_'))
def users_page_callback(call):
    user_id = call.from_user.id
    if not is_user_admin(user_id):
        bot.answer_callback_query(call.id, "❌ دسترسی denied")
        return
    page = int(call.data.split('_')[2])
    show_users_page(call.message.chat.id, page, call.message.message_id)
    bot.answer_callback_query(call.id)

# ==================== ADMIN PANEL ====================

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
        InlineKeyboardButton("📊 وضعیت سیستم", callback_data="admin_status"),
        InlineKeyboardButton("💾 پشتیبان", callback_data="admin_backup_menu")
    )
    keyboard.row(
        InlineKeyboardButton("👥 مدیریت کاربران", callback_data="admin_users_menu")
    )
    bot.send_message(message.chat.id, "🛠️ پنل مدیریت\nلطفاً عملیات مورد نظر را انتخاب کنید:", reply_markup=keyboard)

def show_user_welcome(message):
    if not check_membership(message.from_user.id):
        bot.send_message(message.chat.id, "❌ برای استفاده از ربات باید در کانال عضو شوید.", reply_markup=build_join_keyboard())
        return
    bot.send_message(message.chat.id, "👋 به ربات خوش آمدید!\nبرای دریافت فایل از لینک‌های ارسالی در کانال استفاده کنید.")

def handle_deeplink(message, key):
    user_id = message.from_user.id
    if not check_membership(user_id):
        bot.send_message(message.chat.id, "❌ برای دریافت فایل باید در کانال عضو شوید.", reply_markup=build_join_keyboard())
        return
    if is_user_banned(user_id):
        bot.send_message(message.chat.id, "⛔ شما از دسترسی به ربات مسدود شده‌اید.")
        return
    if "_E" in key:
        series_key, ep_num = key.split("_E", 1)
        try:
            episode_num = int(ep_num)
            show_episode_qualities(message, series_key, episode_num)
            return
        except ValueError:
            pass
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
    text = f"🎬 {title}\n\nلطفاً کیفیت مورد نظر را انتخاب کنید:"
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
    unique_episodes = list(set(episodes))
    unique_episodes.sort()
    keyboard = InlineKeyboardMarkup()
    for ep_num in unique_episodes:
        deeplink = f"https://t.me/{bot_username}?start={series_key}_E{ep_num}"
        keyboard.add(InlineKeyboardButton(f"📺 قسمت {ep_num}", url=deeplink))
    try:
        if poster_file_id:
            bot.send_photo(message.chat.id, poster_file_id, caption=text, reply_markup=keyboard)
        else:
            bot.send_message(message.chat.id, text, reply_markup=keyboard)
    except Exception as e:
        logger.error(f"Error sending series: {e}")
        bot.send_message(message.chat.id, text, reply_markup=keyboard)

def show_episode_qualities(message, series_key, episode_num):
    try:
        episode_response = supabase.table("episodes").select("*").eq("series_key", series_key).eq("episode_number", episode_num).execute()
        if not episode_response.data:
            bot.send_message(message.chat.id, "❌ این قسمت یافت نشد.")
            return
        qualities = [(ep['quality'], ep['file_id'], ep['caption']) for ep in episode_response.data]
        keyboard = InlineKeyboardMarkup()
        for quality, file_id, caption in qualities:
            keyboard.add(InlineKeyboardButton(f"🎥 {quality}", callback_data=f"episode:{series_key}:{episode_num}:{quality}"))
        bot.send_message(message.chat.id, f"📺 قسمت {episode_num}\nلطفاً کیفیت مورد نظر را انتخاب کنید:", reply_markup=keyboard)
    except Exception as e:
        logger.error(f"Error fetching episode: {e}")
        bot.send_message(message.chat.id, "❌ خطا در دریافت اطلاعات")

# ==================== ADMIN CALLBACK HANDLERS ====================

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
    elif data == "admin_backup_menu":
        show_backup_menu(call)
    elif data == "admin_backup_now":
        bot.answer_callback_query(call.id, "🔄 در حال گرفتن بکاپ...")
        backup_command(call.message)
    elif data == "admin_users_menu":
        show_users_admin_menu(call)
    elif data == "admin_user_stats":
        stats = get_user_stats()
        text = f"📊 **آمار کاربران**\n\n👥 کل: {stats['total']}\n✅ فعال امروز: {stats['active_today']}\n🚫 مسدود: {stats['banned']}"
        bot.answer_callback_query(call.id)
        bot.send_message(call.message.chat.id, text, parse_mode='Markdown')
    elif data == "admin_users_list":
        userslist_command(call.message)
    elif data == "admin_broadcast":
        bot.answer_callback_query(call.id)
        bot.send_message(call.message.chat.id, "📢 لطفاً متن پیام همگانی را با دستور `/broadcast متن` ارسال کنید.\n\n⚠️ می‌توانید از HTML استفاده کنید: `<b>bold</b>`, `<i>italic</i>`, `<tg-spoiler>spoiler</tg-spoiler>`", parse_mode='Markdown')
    elif data == "admin_done":
        admin_sessions.pop(user_id, None)
        bot.edit_message_text("✅ عملیات با موفقیت تمام شد.", call.message.chat.id, call.message.message_id)
    elif data == "admin_add_another_quality":
        session = admin_sessions.get(user_id)
        if session and session.get("mode") == "add_film":
            session["step"] = "quality"
            bot.edit_message_text("لطفاً کیفیت جدید را وارد کنید (مثال: 1080p):", call.message.chat.id, call.message.message_id)
    elif data == "admin_add_another_episode":
        session = admin_sessions.get(user_id)
        if session and session.get("mode") == "add_series":
            session["step"] = "episode_number"
            bot.edit_message_text("لطفاً شماره قسمت جدید را وارد کنید:", call.message.chat.id, call.message.message_id)
    elif data == "admin_add_episode_quality":
        session = admin_sessions.get(user_id)
        if session and session.get("mode") == "add_series":
            session["step"] = "episode_quality"
            bot.edit_message_text("لطفاً کیفیت جدید برای این قسمت را وارد کنید:", call.message.chat.id, call.message.message_id)
    elif data == "admin_done_series":
        session = admin_sessions.get(user_id)
        if session and session.get("mode") == "add_series":
            bot_username = bot.get_me().username
            deeplink = f"https://t.me/{bot_username}?start={session['series_key']}"
            bot.edit_message_text(f"✅ سریال با موفقیت تکمیل شد!\n\n🔑 کلید: `{session['series_key']}`\n📺 عنوان: {session['series_title']}\n\n🔗 دیپ‌لینک:\n`{deeplink}`", call.message.chat.id, call.message.message_id, parse_mode='Markdown')
            admin_sessions.pop(user_id, None)

def show_backup_menu(call):
    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("💾 گرفتن بکاپ دستی", callback_data="admin_backup_now"))
    keyboard.add(InlineKeyboardButton("🔙 بازگشت", callback_data="admin_back"))
    bot.edit_message_text(f"💾 **پشتیبان‌گیری**\n\n• بکاپ خودکار هر روز ساعت {BACKUP_TIME}\n• برای بکاپ دستی روی دکمه زیر کلیک کنید\n• برای بازیابی از دستور /restore استفاده کنید", call.message.chat.id, call.message.message_id, reply_markup=keyboard, parse_mode='Markdown')

def show_users_admin_menu(call):
    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("📊 آمار کاربران", callback_data="admin_user_stats"))
    keyboard.add(InlineKeyboardButton("📋 لیست کاربران", callback_data="admin_users_list"))
    keyboard.add(InlineKeyboardButton("📢 ارسال همگانی", callback_data="admin_broadcast"))
    keyboard.add(InlineKeyboardButton("🔙 بازگشت", callback_data="admin_back"))
    bot.edit_message_text("👥 **مدیریت کاربران**\nلطفاً یک گزینه را انتخاب کنید:", call.message.chat.id, call.message.message_id, reply_markup=keyboard, parse_mode='Markdown')

def start_add_film(call):
    user_id = call.from_user.id
    admin_sessions[user_id] = {"mode": "add_film", "step": "key", "_timestamp": time.time()}
    bot.edit_message_text("🎬 افزودن فیلم جدید\n\nلطفاً کلید فیلم را وارد کنید (مثال: the_matrix):", call.message.chat.id, call.message.message_id)

def start_add_series(call):
    user_id = call.from_user.id
    admin_sessions[user_id] = {"mode": "add_series", "step": "key", "_timestamp": time.time()}
    bot.edit_message_text("📺 افزودن سریال جدید\n\nلطفاً کلید سریال را وارد کنید (مثال: breaking_bad):", call.message.chat.id, call.message.message_id)

def show_content_list(call):
    try:
        films_response = supabase.table("films").select("key, title").execute()
        films = [(f['key'], f['title']) for f in films_response.data]
        series_response = supabase.table("series").select("key, title").execute()
        series = [(s['key'], s['title']) for s in series_response.data]
        text = "📋 لیست محتوا\n\n"
        bot_username = bot.get_me().username
        if films:
            text += "🎬 فیلم‌ها:\n"
            for film_key, title in films:
                deeplink = f"https://t.me/{bot_username}?start={film_key}"
                text += f"• {title} (`{film_key}`)\n🔗 `{deeplink}`\n\n"
        if series:
            text += "📺 سریال‌ها:\n"
            for series_key, title in series:
                deeplink = f"https://t.me/{bot_username}?start={series_key}"
                text += f"• {title} (`{series_key}`)\n🔗 `{deeplink}`\n\n"
        if not films and not series:
            text += "❌ هیچ محتوایی وجود ندارد."
        if len(text) > 4000:
            for i in range(0, len(text), 4000):
                bot.send_message(call.message.chat.id, text[i:i+4000], parse_mode='Markdown')
        else:
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id, parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Error showing content list: {e}")
        bot.answer_callback_query(call.id, "❌ خطا در دریافت لیست")

def show_delete_options(call):
    keyboard = InlineKeyboardMarkup()
    keyboard.row(InlineKeyboardButton("🎬 حذف فیلم", callback_data="delete_films"), InlineKeyboardButton("📺 حذف سریال", callback_data="delete_series"))
    keyboard.add(InlineKeyboardButton("🔙 بازگشت", callback_data="admin_back"))
    bot.edit_message_text("🗑️ حذف محتوا\nلطفاً نوع محتوای مورد نظر را انتخاب کنید:", call.message.chat.id, call.message.message_id, reply_markup=keyboard)

# ==================== DELETE HANDLERS ====================

@bot.callback_query_handler(func=lambda call: call.data in ['delete_films', 'delete_series'])
def delete_options_handler(call):
    user_id = call.from_user.id
    if user_id not in ADMINS:
        bot.answer_callback_query(call.id, "❌ دسترسی denied")
        return
    if call.data == "delete_films":
        show_films_for_deletion(call)
    elif call.data == "delete_series":
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
            short_title = title[:30] + "..." if len(title) > 30 else title
            keyboard.add(InlineKeyboardButton(f"🗑️ {short_title}", callback_data=f"delf:{film_id}"))
        keyboard.add(InlineKeyboardButton("🔙 بازگشت", callback_data="admin_delete"))
        bot.edit_message_text("🎬 انتخاب فیلم برای حذف:", call.message.chat.id, call.message.message_id, reply_markup=keyboard)
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
            short_title = title[:30] + "..." if len(title) > 30 else title
            keyboard.add(InlineKeyboardButton(f"🗑️ {short_title}", callback_data=f"dels:{series_id}"))
        keyboard.add(InlineKeyboardButton("🔙 بازگشت", callback_data="admin_delete"))
        bot.edit_message_text("📺 انتخاب سریال برای حذف:", call.message.chat.id, call.message.message_id, reply_markup=keyboard)
    except Exception as e:
        logger.error(f"Error showing series for deletion: {e}")
        bot.answer_callback_query(call.id, "❌ خطا در دریافت لیست")

@bot.callback_query_handler(func=lambda call: call.data.startswith(('delf:', 'dels:')))
def delete_callback_handler(call):
    user_id = call.from_user.id
    if user_id not in ADMINS:
        bot.answer_callback_query(call.id, "❌ دسترسی denied")
        return
    if call.data.startswith("delf:"):
        film_id = call.data.split(":")[1]
        delete_film(call, film_id)
    elif call.data.startswith("dels:"):
        series_id = call.data.split(":")[1]
        delete_series(call, series_id)

def delete_film(call, film_id):
    try:
        film_response = supabase.table("films").select("key").eq("id", film_id).execute()
        if not film_response.data:
            bot.answer_callback_query(call.id, "❌ فیلم مورد نظر یافت نشد")
            return
        film_key = film_response.data[0]['key']
        supabase.table("film_qualities").delete().eq("film_key", film_key).execute()
        supabase.table("films").delete().eq("id", film_id).execute()
        bot.answer_callback_query(call.id, "✅ فیلم با موفقیت حذف شد")
        show_admin_panel(call.message)
    except Exception as e:
        logger.error(f"Error deleting film: {e}")
        bot.answer_callback_query(call.id, "❌ خطا در حذف فیلم")

def delete_series(call, series_id):
    try:
        series_response = supabase.table("series").select("key").eq("id", series_id).execute()
        if not series_response.data:
            bot.answer_callback_query(call.id, "❌ سریال مورد نظر یافت نشد")
            return
        series_key = series_response.data[0]['key']
        supabase.table("episodes").delete().eq("series_key", series_key).execute()
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
        bot.send_message(message.chat.id, "لطفاً توضیحات فیلم را وارد کنید:\nیا /skip برای رد کردن")
    elif step == "description":
        session["film_description"] = None if text == "/skip" else text
        session["step"] = "quality"
        session["_timestamp"] = time.time()
        bot.send_message(message.chat.id, "لطفاً کیفیت اول را وارد کنید (مثال: 720p):")
    elif step == "quality":
        session["current_quality"] = text
        session["step"] = "file"
        session["_timestamp"] = time.time()
        bot.send_message(message.chat.id, f"✅ کیفیت '{text}' ثبت شد\n\nلطفاً فایل را ارسال کنید:")

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
        bot.send_message(message.chat.id, "لطفاً توضیحات پوستر را وارد کنید:\nیا /skip برای رد کردن")
    elif step == "poster_desc":
        session["poster_description"] = None if text == "/skip" else text
        session["step"] = "poster_file"
        session["_timestamp"] = time.time()
        bot.send_message(message.chat.id, "لطفاً عکس پوستر را ارسال کنید:\nیا /skip برای رد کردن")
    elif step == "poster_file":
        if text == "/skip":
            session["poster_file_id"] = None
            session["step"] = "caption_template"
            session["_timestamp"] = time.time()
            bot.send_message(message.chat.id, "لطفاً قالب کپشن قسمت‌ها را وارد کنید:\nیا /skip برای استفاده از قالب پیش‌فرض")
        else:
            bot.send_message(message.chat.id, "❌ لطفاً یک عکس ارسال کنید یا /skip بزنید")
    elif step == "caption_template":
        session["caption_template"] = None if text == "/skip" else text
        try:
            supabase.table("series").insert({
                "key": session["series_key"],
                "title": session["series_title"],
                "poster_file_id": session.get("poster_file_id"),
                "poster_description": session.get("poster_description"),
                "caption_template": session.get("caption_template")
            }).execute()
            session["step"] = "episode_number"
            bot.send_message(message.chat.id, "✅ سریال ایجاد شد!\n\nلطفاً شماره قسمت اول را وارد کنید:\n(یا /done برای اتمام)")
        except Exception as e:
            logger.error(f"Error creating series: {e}")
            bot.send_message(message.chat.id, "❌ خطا در ایجاد سریال")
            admin_sessions.pop(message.from_user.id, None)
    elif step == "episode_number":
        if text.lower() in ['/done', 'done', 'اتمام']:
            bot_username = bot.get_me().username
            deeplink = f"https://t.me/{bot_username}?start={session['series_key']}"
            bot.send_message(message.chat.id, f"✅ سریال با موفقیت ایجاد شد!\n\n🔑 کلید: `{session['series_key']}`\n🔗 دیپ‌لینک:\n`{deeplink}`", parse_mode='Markdown')
            admin_sessions.pop(message.from_user.id, None)
            return
        try:
            episode_num = int(text)
            session["current_episode"] = episode_num
            session["step"] = "episode_quality"
            bot.send_message(message.chat.id, f"✅ قسمت {episode_num} انتخاب شد\n\nلطفاً کیفیت این قسمت را وارد کنید:")
        except ValueError:
            bot.send_message(message.chat.id, "❌ لطفاً یک عدد معتبر وارد کنید")
    elif step == "episode_quality":
        session["current_quality"] = text
        session["step"] = "episode_file"
        bot.send_message(message.chat.id, f"✅ کیفیت '{text}' ثبت شد\n\nلطفاً فایل این قسمت را ارسال کنید:")

# ==================== FILE HANDLERS ====================
# ترتیب: اول هندلر بکاپ، سپس هندلر اصلی

@bot.message_handler(content_types=['document'])
def handle_backup_file(message):
    if not is_user_admin(message.from_user.id):
        return
    register_user(message.from_user)
    if is_user_banned(message.from_user.id):
        return
    if not message.document:
        return
    filename = message.document.file_name
    if not (filename.endswith('.json') or filename.endswith('.json.gz')):
        return
    keyboard = InlineKeyboardMarkup()
    keyboard.row(InlineKeyboardButton("✅ بله، بازیابی کن", callback_data="confirm_restore"), InlineKeyboardButton("❌ لغو", callback_data="cancel_restore"))
    bot.send_message(message.chat.id, f"⚠️ **تأیید بازیابی**\n\nفایل: `{filename}`\n\nآیا مطمئن هستید؟", reply_markup=keyboard, parse_mode='Markdown')
    file_info = bot.get_file(message.document.file_id)
    downloaded_file = bot.download_file(file_info.file_path)
    temp_path = f"/tmp/restore_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json.gz"
    with open(temp_path, 'wb') as f:
        f.write(downloaded_file)
    admin_sessions[message.from_user.id] = {"mode": "restore", "backup_file": temp_path, "_timestamp": time.time()}

@bot.callback_query_handler(func=lambda call: call.data in ['confirm_restore', 'cancel_restore'])
def restore_callback_handler(call):
    user_id = call.from_user.id
    if not is_user_admin(user_id):
        bot.answer_callback_query(call.id, "❌ دسترسی denied")
        return
    session = admin_sessions.get(user_id, {})
    if call.data == "cancel_restore":
        bot.edit_message_text("❌ عملیات بازیابی لغو شد.", call.message.chat.id, call.message.message_id)
        if session.get("backup_file") and os.path.exists(session["backup_file"]):
            os.remove(session["backup_file"])
        admin_sessions.pop(user_id, None)
    elif call.data == "confirm_restore":
        bot.edit_message_text("🔄 در حال بازیابی اطلاعات...", call.message.chat.id, call.message.message_id)
        if not backup_manager:
            bot.send_message(call.message.chat.id, "❌ سیستم پشتیبان‌گیری راه‌اندازی نشده")
            return
        backup_file = session.get("backup_file")
        if not backup_file or not os.path.exists(backup_file):
            bot.send_message(call.message.chat.id, "❌ فایل بکاپ یافت نشد")
            return
        if backup_manager.restore_from_backup(backup_file):
            bot.send_message(call.message.chat.id, "✅ دیتابیس با موفقیت بازیابی شد!")
        else:
            bot.send_message(call.message.chat.id, "❌ خطا در بازیابی دیتابیس")
        os.remove(backup_file)
        admin_sessions.pop(user_id, None)

@bot.message_handler(func=lambda message: message.from_user.id in ADMINS and (message.document or message.video or message.audio), content_types=['document', 'video', 'audio'])
def admin_file_handler(message):
    user_id = message.from_user.id
    session = admin_sessions.get(user_id)
    if not session:
        return
    step = session.get("step")
    mode = session.get("mode")
    if mode == "add_film" and step == "file":
        handle_film_file(message, session, user_id)
    elif mode == "add_series" and step == "episode_file":
        handle_episode_file(message, session, user_id)

@bot.message_handler(func=lambda message: message.from_user.id in ADMINS and message.photo, content_types=['photo'])
def admin_photo_handler(message):
    user_id = message.from_user.id
    session = admin_sessions.get(user_id)
    if not session or session.get("step") != "poster_file":
        return
    file_id = message.photo[-1].file_id
    session["poster_file_id"] = file_id
    session["step"] = "caption_template"
    session["_timestamp"] = time.time()
    bot.send_message(message.chat.id, "✅ پوستر ثبت شد\n\nلطفاً قالب کپشن قسمت‌ها را وارد کنید:\nیا /skip برای قالب پیش‌فرض")

def handle_film_file(message, session, user_id):
    if message.document:
        file_id = message.document.file_id
    elif message.video:
        file_id = message.video.file_id
    elif message.audio:
        file_id = message.audio.file_id
    else:
        bot.send_message(message.chat.id, "❌ لطفاً یک فایل معتبر ارسال کنید")
        return
    caption = create_film_caption(session.get("film_description"), session["current_quality"])
    try:
        film_exists = supabase.table("films").select("key").eq("key", session["film_key"]).execute()
        if not film_exists.data:
            supabase.table("films").insert({"key": session["film_key"], "title": session["film_title"], "description": session.get("film_description")}).execute()
            logger.info(f"✅ Film created: {session['film_key']}")
        supabase.table("film_qualities").upsert({"film_key": session["film_key"], "quality": session["current_quality"], "file_id": file_id, "caption": caption, "added_by": user_id}).execute()
        logger.info(f"✅ Quality added - Film: {session['film_key']}, Quality: {session['current_quality']}")
        bot_username = bot.get_me().username
        deeplink = f"https://t.me/{bot_username}?start={session['film_key']}"
        keyboard = InlineKeyboardMarkup()
        keyboard.row(InlineKeyboardButton("➕ کیفیت دیگر", callback_data="admin_add_another_quality"), InlineKeyboardButton("✅ اتمام", callback_data="admin_done"))
        bot.send_message(message.chat.id, f"✅ کیفیت '{session['current_quality']}' اضافه شد!\n\n🔑 کلید: `{session['film_key']}`\n🎬 فیلم: {session['film_title']}\n\n🔗 دیپ‌لینک:\n`{deeplink}`", reply_markup=keyboard, parse_mode='Markdown')
        session["step"] = "complete"
    except Exception as e:
        logger.error(f"❌ Error saving film to Supabase: {str(e)}")
        bot.send_message(message.chat.id, f"❌ خطا در ذخیره فیلم: {str(e)}")

def handle_episode_file(message, session, user_id):
    if message.document:
        file_id = message.document.file_id
    elif message.video:
        file_id = message.video.file_id
    elif message.audio:
        file_id = message.audio.file_id
    else:
        bot.send_message(message.chat.id, "❌ لطفاً یک فایل معتبر ارسال کنید")
        return
    caption = create_episode_caption(session.get("caption_template"), session["current_episode"], session["current_quality"])
    try:
        supabase.table("episodes").insert({"series_key": session["series_key"], "episode_number": session["current_episode"], "quality": session["current_quality"], "file_id": file_id, "caption": caption, "added_by": user_id}).execute()
        bot_username = bot.get_me().username
        series_deeplink = f"https://t.me/{bot_username}?start={session['series_key']}"
        keyboard = InlineKeyboardMarkup()
        keyboard.row(InlineKeyboardButton("➕ قسمت دیگر", callback_data="admin_add_another_episode"), InlineKeyboardButton("➕ کیفیت دیگر", callback_data="admin_add_episode_quality"))
        keyboard.add(InlineKeyboardButton("✅ اتمام", callback_data="admin_done_series"))
        bot.send_message(message.chat.id, f"✅ قسمت {session['current_episode']} با کیفیت {session['current_quality']} اضافه شد!\n\n🔗 دیپ‌لینک سریال:\n`{series_deeplink}`", reply_markup=keyboard, parse_mode='Markdown')
        session["step"] = "episode_complete"
    except Exception as e:
        logger.error(f"Error saving episode to Supabase: {e}")
        bot.send_message(message.chat.id, "❌ خطا در ذخیره قسمت")

# ==================== QUALITY SELECTION HANDLERS ====================

@bot.callback_query_handler(func=lambda call: call.data.startswith('quality:'))
def quality_callback_handler(call):
    user_id = call.from_user.id
    register_user(call.from_user)  # ثبت کاربر
    if is_user_banned(user_id):
        bot.answer_callback_query(call.id, "⛔ شما مسدود هستید")
        return
    parts = call.data.split(':')
    if len(parts) == 3:
        film_key, quality = parts[1], parts[2]
        try:
            result = supabase.table("film_qualities").select("file_id, caption").eq("film_key", film_key).eq("quality", quality).execute()
            if result.data:
                r = result.data[0]
                bot.send_document(call.message.chat.id, r['file_id'], caption=r['caption'])
                update_user_downloads(user_id)  # افزایش شمارش دانلود
                bot.answer_callback_query(call.id, "✅ فایل ارسال شد")
            else:
                bot.answer_callback_query(call.id, "❌ فایل یافت نشد")
        except Exception as e:
            logger.error(f"Error: {e}")
            bot.answer_callback_query(call.id, "❌ خطا در دریافت فایل")

@bot.callback_query_handler(func=lambda call: call.data.startswith('episode:'))
def episode_callback_handler(call):
    user_id = call.from_user.id
    register_user(call.from_user)
    if is_user_banned(user_id):
        bot.answer_callback_query(call.id, "⛔ شما مسدود هستید")
        return
    parts = call.data.split(':')
    if len(parts) == 4:
        series_key, episode_num, quality = parts[1], int(parts[2]), parts[3]
        try:
            result = supabase.table("episodes").select("file_id, caption").eq("series_key", series_key).eq("episode_number", episode_num).eq("quality", quality).execute()
            if result.data:
                r = result.data[0]
                bot.send_document(call.message.chat.id, r['file_id'], caption=r['caption'])
                update_user_downloads(user_id)
                bot.answer_callback_query(call.id, "✅ فایل ارسال شد")
            else:
                bot.answer_callback_query(call.id, "❌ فایل یافت نشد")
        except Exception as e:
            logger.error(f"Error: {e}")
            bot.answer_callback_query(call.id, "❌ خطا در دریافت فایل")

# ==================== OTHER HANDLERS ====================

@bot.callback_query_handler(func=lambda call: call.data == 'check_join')
def check_join_handler(call):
    user_id = call.from_user.id
    register_user(call.from_user)
    if is_user_banned(user_id):
        bot.answer_callback_query(call.id, "⛔ شما مسدود هستید")
        return
    if check_membership(user_id):
        bot.answer_callback_query(call.id, "✅ عضویت شما تایید شد!")
        bot.edit_message_text("✅ عضویت شما تایید شد!\nاکنون می‌توانید از لینک‌ها استفاده کنید.", call.message.chat.id, call.message.message_id)
    else:
        bot.answer_callback_query(call.id, "❌ هنوز عضو نشدید")
        bot.edit_message_text("❌ هنوز عضو نشدید. لطفاً در کانال عضو شوید:", call.message.chat.id, call.message.message_id, reply_markup=build_join_keyboard())

@bot.message_handler(func=lambda message: True)
def all_messages(message):
    user_id = message.from_user.id
    register_user(message.from_user)
    if is_user_banned(user_id):
        bot.send_message(message.chat.id, "⛔ شما از دسترسی به ربات مسدود شده‌اید.")
        return
    
    # چک اسپم فقط برای کاربران غیر ادمین
    if user_id not in ADMINS:
        check_spam(user_id, message.from_user.username, message.from_user.first_name)
        # (اختیاری) می‌توانیم در صورت اسپم شدید، پاسخ ندهیم یا مسدود کنیم
    
    if message.from_user.id in ADMINS:
        show_admin_panel(message)
    else:
        show_user_welcome(message)

# ==================== FUNCTIONS FOR BACKUP SCHEDULER ====================

def setup_auto_backup(bot_instance):
    global backup_manager
    backup_manager = BackupManager(supabase, bot_instance)
    scheduler = BackgroundScheduler()
    hour, minute = map(int, BACKUP_TIME.split(':'))
    scheduler.add_job(func=lambda: scheduled_backup(bot_instance), trigger=CronTrigger(hour=hour, minute=minute), id="daily_backup", replace_existing=True)
    scheduler.start()
    logger.info(f"✅ Auto-backup scheduled at {BACKUP_TIME} daily")
    return scheduler

def scheduled_backup(bot_instance):
    logger.info("🔄 Starting scheduled backup...")
    if not backup_manager:
        logger.error("Backup manager not initialized")
        return
    backup_info = backup_manager.create_backup()
    if backup_info:
        backup_manager.send_backup_to_telegram(backup_info)
    else:
        logger.error("❌ Scheduled backup failed")

# ==================== MAIN EXECUTION ====================

def run_flask():
    port = int(os.getenv("PORT", 10000))
    app.run(host='0.0.0.0', port=port, debug=False)

def run_bot():
    global restart_count
    logger.info("Starting bot polling with health monitoring...")
    last_restart = datetime.now()
    while True:
        try:
            logger.info(f"🚀 Starting bot polling (attempt {restart_count + 1})")
            cleanup_old_sessions()
            bot.infinity_polling(timeout=60, long_polling_timeout=60, skip_pending=True)
        except Exception as e:
            restart_count += 1
            current_time = datetime.now()
            time_since_last_restart = (current_time - last_restart).total_seconds()
            last_restart = current_time
            logger.error(f"Bot polling error: {e}")
            wait_time = 300 if (restart_count > 5 and time_since_last_restart < 300) else 30
            logger.info(f"Restarting bot in {wait_time} seconds...")
            time.sleep(wait_time)
            if restart_count > 20:
                logger.critical(f"🚨 CRITICAL: Bot restarted {restart_count} times!")

if __name__ == "__main__":
    logger.info("🚀 Starting bot with BACKUP system, USER MANAGEMENT, SPAM DETECTION...")
    start_time = datetime.now()
    logger.info(f"📅 Start time: {start_time}")
    try:
        setup_auto_backup(bot)
        start_keep_alive()
        bot_thread = threading.Thread(target=run_bot, daemon=True)
        bot_thread.start()
        logger.info("✅ Bot thread started successfully!")
        bot_info = bot.get_me()
        logger.info(f"🤖 Bot info: @{bot_info.username} (ID: {bot_info.id})")
        port = int(os.getenv("PORT", 10000))
        logger.info(f"🌐 Starting Flask on port {port}")
        run_flask()
    except Exception as e:
        logger.critical(f"🚨 CRITICAL ERROR in main: {e}")
        raise
