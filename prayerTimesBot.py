import asyncio
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import requests
from bs4 import BeautifulSoup
from datetime import datetime, time, timedelta
import pytz
import urllib3
from aiohttp import web
import aiohttp
import os
import logging
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


def fetch_prayer_times():
    if TESTING_MODE:
        # Generate fake prayer times for testing
        now = datetime.now(pytz.timezone("Asia/Amman"))
        fake_times = {
            "Fajr": (now + timedelta(minutes=25)).strftime("%I:%M %p"),
            "Sunrise": (now + timedelta(minutes=35)).strftime("%I:%M %p"),
            "Dhuhr": (now + timedelta(minutes=45)).strftime("%I:%M %p"),
            "Asr": (now + timedelta(minutes=55)).strftime("%I:%M %p"),
            "Maghrib": (now + timedelta(minutes=65)).strftime("%I:%M %p"),
            "Isha": (now + timedelta(minutes=75)).strftime("%I:%M %p"),
        }
        return fake_times
    else:
        url = "https://www.awqaf.gov.jo/ar/Pages/PrayerTime"
        response = requests.get(url, verify=False)  # Disable SSL verification
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

                # Add AM/PM to the times
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


async def send_notification(
    context: ContextTypes.DEFAULT_TYPE, prayer: str, time_str: str, minutes_before: int
):
    if minutes_before > 0:
        message = f"Prayer time reminder: {prayer} prayer is in {minutes_before} minutes (at {time_str})"
    else:
        message = f"It's time for {prayer} prayer now ({time_str})"

    try:
        await context.bot.send_message(chat_id=CHAT_ID, text=message)
        logger.info(f"Sent notification: {message}")
    except Exception as e:
        logger.error(f"Failed to send notification: {str(e)}")


async def schedule_notifications(context: ContextTypes.DEFAULT_TYPE, prayer_times):
    # Clear existing jobs to prevent duplicates
    context.job_queue.clear()

    now = datetime.now(pytz.timezone("Asia/Amman"))
    for prayer, time_str in prayer_times.items():
        prayer_time = datetime.strptime(
            f"{now.strftime('%Y-%m-%d')} {time_str}", "%Y-%m-%d %I:%M %p"
        )
        prayer_time = pytz.timezone("Asia/Amman").localize(prayer_time)

        # Schedule notifications at 20, 10, 5 minutes before, and at prayer time
        for minutes in [20, 10, 5, 0]:
            notify_time = prayer_time - timedelta(minutes=minutes)
            if notify_time > now:
                context.job_queue.run_once(
                    lambda ctx,
                    prayer=prayer,
                    time_str=time_str,
                    mins=minutes: send_notification(ctx, prayer, time_str, mins),
                    when=notify_time,
                )
                logger.info(
                    f"Scheduled {prayer} notification for {notify_time} ({minutes} minutes before)"
                )


async def send_daily_update(context: ContextTypes.DEFAULT_TYPE):
    prayer_times = fetch_prayer_times()
    if prayer_times:
        message = "Prayer times for today:\n"
        for prayer, time in prayer_times.items():
            message += f"{prayer}: {time}\n"
        await context.bot.send_message(chat_id=CHAT_ID, text=message)
        await schedule_notifications(context, prayer_times)
    else:
        logger.error("Failed to fetch prayer times")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Bot started. You'll receive updates and notifications for prayer times."
    )
    try:
        await send_daily_update(context)  # Send update immediately for testing
    except Exception as e:
        logger.error(f"Error in start command: {str(e)}")


async def handle(request):
    return web.Response(text="Your bot is running!")


async def keep_alive():
    while True:
        try:
            hostname = os.environ.get("RENDER_EXTERNAL_HOSTNAME")
            if not hostname:
                logger.warning(
                    "RENDER_EXTERNAL_HOSTNAME is not set. Skipping keep-alive request."
                )
                await asyncio.sleep(540)
                continue

            async with aiohttp.ClientSession() as session:
                async with session.get(f"https://{hostname}") as response:
                    logger.info(f"Keep-alive request sent. Status: {response.status}")
        except Exception as e:
            logger.error(f"Error in keep-alive request: {str(e)}")
        await asyncio.sleep(540)  # 9 minutes


async def web_server():
    app = web.Application()
    app.router.add_get("/", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Web server started on port {port}")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(f"Exception while handling an update: {context.error}")

    if isinstance(context.error, Conflict):
        # Handle the conflict error
        logger.warning("Conflict detected. Waiting before retrying...")
        await asyncio.sleep(30)  # Wait for 30 seconds before retrying
    elif isinstance(context.error, (TimedOut, NetworkError)):
        # Handle timeout errors
        logger.warning(f"Network error: {context.error}. Retrying in 10 seconds...")
        await asyncio.sleep(10)  # Wait for 10 seconds before retrying


def main():
    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_error_handler(error_handler)

    amman_tz = pytz.timezone("Asia/Amman")

    # Schedule daily update at midnight
    application.job_queue.run_daily(
        send_daily_update, time=time(hour=0, minute=0, tzinfo=amman_tz)
    )

    # Start the web server and keep-alive mechanism
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.create_task(web_server())
    loop.create_task(keep_alive())

    # Start the bot with a higher timeout
    application.run_polling(timeout=60, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
