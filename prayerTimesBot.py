import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler
import requests
from bs4 import BeautifulSoup
from datetime import datetime, time, timedelta
import pytz
import urllib3
from aiohttp import web
import aiohttp
import os
import logging
import psutil  # For resource monitoring
from telegram.error import TimedOut, NetworkError, Conflict

# Set up logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Disable SSL warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Replace with your actual bot token
TOKEN = "7445923368:AAFH9UPTjo0k9kU_Bp9PeNnoTCl48y3VHeg"
# Replace with your actual chat ID
CHAT_ID = "651307921"
# Set this to True for testing, False for production
TESTING_MODE = False

MAX_RETRIES = 5
RETRY_DELAY = 300  # 5 minutes

# Prayer emoji mappings
PRAYER_EMOJIS = {
    "Fajr": "🌅",
    "Sunrise": "☀️",
    "Dhuhr": "🕑",
    "Asr": "🕓",
    "Maghrib": "🌇",
    "Isha": "🌙",
}

PRAYER_MESSAGE_IDS = {}


async def fetch_prayer_times(retries=0):
    if TESTING_MODE:
        now = datetime.now(pytz.timezone("Asia/Amman"))
        fake_times = {
            "Fajr": (now + timedelta(minutes=6)).strftime("%I:%M %p"),
            "Sunrise": (now + timedelta(minutes=35)).strftime("%I:%M %p"),
            "Dhuhr": (now + timedelta(minutes=45)).strftime("%I:%M %p"),
            "Asr": (now + timedelta(minutes=55)).strftime("%I:%M %p"),
            "Maghrib": (now + timedelta(minutes=65)).strftime("%I:%M %p"),
            "Isha": (now + timedelta(minutes=75)).strftime("%I:%M %p"),
        }
        return fake_times
    else:
        try:
            url = "https://www.awqaf.gov.jo/ar/Pages/PrayerTime"
            response = await asyncio.get_event_loop().run_in_executor(
                None, lambda: requests.get(url, verify=False, timeout=10)
            )
            response.raise_for_status()
            soup = BeautifulSoup(response.content, "html.parser")

            table = soup.find("table", {"id": "MainContent_gvWebparts"})
            rows = table.find_all("tr")[1:]  # Skip the header row

            today = datetime.now().strftime("%d/%m/%Y")
            for row in rows:
                columns = row.find_all("td")
                date = columns[0].text.strip()
                if date == today:
                    times = {
                        "Fajr": columns[1].text.strip(),
                        "Sunrise": columns[2].text.strip(),
                        "Dhuhr": columns[3].text.strip(),
                        "Asr": columns[4].text.strip(),
                        "Maghrib": columns[5].text.strip(),
                        "Isha": columns[6].text.strip(),
                    }

                    for prayer, time_str in times.items():
                        hour, minute = map(int, time_str.split(":"))
                        if prayer in ["Fajr", "Sunrise"]:
                            times[prayer] = f"{time_str} AM"
                        elif prayer == "Dhuhr" and hour != 12:
                            times[prayer] = f"{time_str} PM"
                        elif hour < 12:
                            times[prayer] = f"{time_str} PM"
                        else:
                            times[prayer] = f"{time_str} PM"

                    return times
            return None
        except Exception as e:
            logger.error(f"Error fetching prayer times: {str(e)}")
            if retries < MAX_RETRIES:
                logger.info(
                    f"Retrying in {RETRY_DELAY} seconds... (Attempt {retries + 1}/{MAX_RETRIES})"
                )
                await asyncio.sleep(RETRY_DELAY)
                return await fetch_prayer_times(retries + 1)
            else:
                logger.error("Max retries reached. Unable to fetch prayer times.")
                return None


async def send_notification(
    context: ContextTypes.DEFAULT_TYPE, prayer: str, time_str: str, minutes_before: int
):
    message = ""
    if prayer == "Sunrise":
        if minutes_before > 0:
            message = f"☀️ Sunrise reminder: The sun will rise in {minutes_before} minutes (at {time_str})"
        else:
            if "Sunrise" in PRAYER_MESSAGE_IDS:
                for message_id in PRAYER_MESSAGE_IDS["Sunrise"]:
                    try:
                        await context.bot.delete_message(chat_id=CHAT_ID, message_id=message_id)
                    except Exception as e:
                        logger.error(f"Failed to delete Sunrise message {message_id}: {str(e)}")
                PRAYER_MESSAGE_IDS["Sunrise"] = []  # Clear the list after deleting messages
            message = f"☀️ The sun is rising now ({time_str})"
    else:
        if minutes_before > 0:
            message = f"{PRAYER_EMOJIS[prayer]} Prayer time reminder: {prayer} prayer is in {minutes_before} minutes (at {time_str})"
        else:
            message = f"{PRAYER_EMOJIS[prayer]} It's time for {prayer} prayer now ({time_str})"

    reply_markup = None
    if minutes_before == 0 and prayer != "Sunrise":
        keyboard = [
            [
                InlineKeyboardButton(
                    "Mark as prayed ✅", callback_data=f"prayed_{prayer}"
                )
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        message_obj = await context.bot.send_message(
            chat_id=CHAT_ID, text=message, reply_markup=reply_markup
        )
        logger.info(f"Sent notification: {message}")

        if prayer not in PRAYER_MESSAGE_IDS:
            PRAYER_MESSAGE_IDS[prayer] = []
        PRAYER_MESSAGE_IDS[prayer].append(message_obj.message_id)

    except Exception as e:
        logger.error(f"Failed to send notification: {str(e)}")


async def schedule_notifications(context: ContextTypes.DEFAULT_TYPE, prayer_times):
    now = datetime.now(pytz.timezone("Asia/Amman"))
    for prayer, time_str in prayer_times.items():
        prayer_time = datetime.strptime(
            f"{now.strftime('%Y-%m-%d')} {time_str}", "%Y-%m-%d %I:%M %p"
        )
        prayer_time = pytz.timezone("Asia/Amman").localize(prayer_time)

        for minutes in [20, 10, 5, 0]:
            notify_time = prayer_time - timedelta(minutes=minutes)
            if notify_time > now:
                context.job_queue.run_once(
                    lambda ctx, prayer=prayer, time_str=time_str, mins=minutes:
                    send_notification(ctx, prayer, time_str, mins),
                    when=notify_time,
                    name=f"prayer_notification_{prayer}_{minutes}",
                )
                logger.info(
                    f"Scheduled {prayer} notification for {notify_time} ({minutes} minutes before)"
                )


async def send_daily_update(context: ContextTypes.DEFAULT_TYPE):
    prayer_times = await fetch_prayer_times()
    if prayer_times:
        message = "📅 Prayer times for today:\n\n"
        for prayer, time in prayer_times.items():
            emoji = PRAYER_EMOJIS.get(prayer, "🕰")
            message += f"{emoji} {prayer}: {time}\n"
        await context.bot.send_message(chat_id=CHAT_ID, text=message)
        await schedule_notifications(context, prayer_times)
    else:
        logger.error("Failed to fetch prayer times")


# Add more robust error handling
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(f"Exception while handling an update: {context.error}")

    try:
        raise context.error
    except Conflict:
        logger.warning("Conflict detected. Waiting before retrying...")
        await asyncio.sleep(30)
    except TimedOut:
        logger.warning(f"Request timed out. Retrying in 10 seconds...")
        await asyncio.sleep(10)
    except NetworkError:
        logger.warning(f"Network error. Retrying in 15 seconds...")
        await asyncio.sleep(15)
    except Exception as e:
        logger.error(f"Unhandled error: {e}")


async def keep_alive():
    while True:
        try:
            hostname = os.environ.get("RENDER_EXTERNAL_HOSTNAME")
            if not hostname:
                logger.warning(
                    "RENDER_EXTERNAL_HOSTNAME is not set. Skipping keep-alive request."
                )
                await asyncio.sleep(300)
                continue

            async with aiohttp.ClientSession() as session:
                async with session.get(f"https://{hostname}") as response:
                    logger.info(f"Keep-alive request sent. Status: {response.status}")
        except Exception as e:
            logger.error(f"Error in keep-alive request: {str(e)}")
        await asyncio.sleep(300)  # Ping every 5 minutes


# Monitor system resource usage
async def monitor_resources():
    while True:
        process = psutil.Process(os.getpid())
        mem_info = process.memory_info()
        cpu_percent = psutil.cpu_percent(interval=1)
        logger.info(f"Memory usage: {mem_info.rss / (1024 ** 2):.2f} MB, CPU usage: {cpu_percent}%")
        await asyncio.sleep(600)  # Log resource usage every 10 minutes


async def web_server():
    app = web.Application()
    app.router.add_get("/", lambda request: web.Response(text="Bot is running!"))
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Web server started on port {port}")


def main():
    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", send_daily_update))
    application.add_error_handler(error_handler)

    amman_tz = pytz.timezone("Asia/Amman")

    application.job_queue.run_daily(
        send_daily_update,
        time=time(hour=4, minute=0, tzinfo=amman_tz),
        name="daily_update",
    )

    loop = asyncio.get_event_loop()
    loop.create_task(web_server())
    loop.create_task(keep_alive())
    loop.create_task(monitor_resources())  # Add resource monitoring

    application.run_polling(timeout=60, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
