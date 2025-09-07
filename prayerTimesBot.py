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
from telegram.error import TimedOut, NetworkError, Conflict

# Set up logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Disable SSL warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Read from environment variables only. No fallbacks in production.
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

if not TOKEN:
    logger.error("Environment variable TELEGRAM_BOT_TOKEN is not set. Exiting.")
    raise SystemExit(1)
if not CHAT_ID:
    logger.error("Environment variable TELEGRAM_CHAT_ID is not set. Exiting.")
    raise SystemExit(1)

# Cast CHAT_ID to int (Telegram expects a numeric chat id, may be negative for groups)
try:
    CHAT_ID = int(CHAT_ID)
except ValueError:
    logger.error("TELEGRAM_CHAT_ID must be a numeric value (e.g., 651307921 or -1001234567890). Exiting.")
    raise SystemExit(1)

# Webhook configuration
WEBHOOK_PATH = os.getenv("TELEGRAM_WEBHOOK_PATH", "/webhook")
if not WEBHOOK_PATH.startswith("/"):
    WEBHOOK_PATH = "/" + WEBHOOK_PATH

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
        # Generate fake prayer times for testing
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
    if prayer == "Sunrise":
        if minutes_before > 0:
            message = f"☀️ Sunrise reminder: The sun will rise in {minutes_before} minutes (at {time_str})"
        else:
            # Delete previous Sunrise messages
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

        # Store the message ID in PRAYER_MESSAGE_IDS
        if prayer not in PRAYER_MESSAGE_IDS:
            PRAYER_MESSAGE_IDS[prayer] = []
        PRAYER_MESSAGE_IDS[prayer].append(message_obj.message_id)

    except Exception as e:
        logger.error(f"Failed to send notification: {str(e)}")


def remove_existing_jobs(job_queue):
    current_jobs = job_queue.jobs()
    for job in current_jobs:
        if job.name and job.name.startswith("prayer_notification_"):
            job.schedule_removal()
    logger.info("Removed existing prayer notification jobs")


async def schedule_notifications(context: ContextTypes.DEFAULT_TYPE, prayer_times):
    remove_existing_jobs(context.job_queue)

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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_message = (
        "🕌 Welcome to the Prayer Times Bot! 🕌\n\n"
        "I'll send you daily updates and reminders for prayer times.\n"
        "Use /today to see today's prayer times.\n"
        "Use /remaining to check the time until the next prayer."
    )
    await update.message.reply_text(welcome_message)
    await send_daily_update(context)  # Send update immediately for testing


async def handle(request):
    return web.Response(text="Your bot is running!")


async def telegram_webhook(request: web.Request):
    """Handles incoming Telegram webhook updates and forwards them to PTB."""
    application: Application = request.app.get("application")
    if application is None:
        return web.Response(status=500, text="Application not initialized")

    # Optional: verify Telegram secret token header if provided via env
    secret = os.getenv("TELEGRAM_WEBHOOK_SECRET")
    if secret:
        header_token = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if header_token != secret:
            logger.warning("Invalid Telegram secret token header")
            return web.Response(status=403, text="Forbidden")

    try:
        data = await request.json()
        update = Update.de_json(data, application.bot)
        await application.process_update(update)
    except Exception as e:
        logger.error(f"Error processing webhook update: {e}")
        return web.Response(status=500, text="Internal Server Error")

    return web.Response(text="OK")

async def keep_alive():
    """Keeps the bot alive by periodically sending a request to Render."""
    try:
        while True:
            try:
                hostname = os.environ.get("RENDER_EXTERNAL_HOSTNAME")
                if not hostname:
                    logger.warning(
                        "RENDER_EXTERNAL_HOSTNAME is not set. Skipping keep-alive request."
                    )
                    await asyncio.sleep(540)  # Sleep for 9 minutes
                    continue

                # Use aiohttp for asynchronous requests
                async with aiohttp.ClientSession() as session:
                    # Send a GET request to your Render app's URL
                    async with session.get(f"https://{hostname}") as response:
                        if response.status == 200:
                            logger.info(
                                f"Keep-alive request successful. Status: {response.status}"
                            )
                        else:
                            logger.warning(
                                f"Keep-alive request returned unexpected status: {response.status}"
                            )
            except Exception as e:
                logger.error(f"Error in keep-alive request: {str(e)}")

            # Sleep for ~6 minutes before sending the next request
            await asyncio.sleep(340)
    except asyncio.CancelledError:
        logger.info("Keep-alive task cancelled.")
        raise

async def web_server(application: Application):
    """Starts a simple web server for handling keep-alive requests."""
    app = web.Application()
    app.router.add_get("/", handle)
    # Telegram webhook endpoint (static path; authenticate via secret header)
    app.router.add_post(WEBHOOK_PATH, telegram_webhook)
    # Store application reference for webhook handler
    app["application"] = application
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    # Store runner & site for graceful shutdown
    application.bot_data["web_runner"] = runner
    application.bot_data["web_site"] = site
    logger.info(f"Web server started on port {port}")

async def start_web_server_job(context: ContextTypes.DEFAULT_TYPE):
    """JobQueue task to start the web server within the bot's event loop."""
    application = context.application
    await web_server(application)

async def keep_alive_job(context: ContextTypes.DEFAULT_TYPE):
    """JobQueue repeating task to ping the Render app and prevent idling."""
    try:
        hostname = os.environ.get("RENDER_EXTERNAL_HOSTNAME")
        if not hostname:
            logger.warning(
                "RENDER_EXTERNAL_HOSTNAME is not set. Skipping keep-alive request."
            )
            return

        async with aiohttp.ClientSession() as session:
            async with session.get(f"https://{hostname}") as response:
                if response.status == 200:
                    logger.info(
                        f"Keep-alive request successful. Status: {response.status}"
                    )
                else:
                    logger.warning(
                        f"Keep-alive request returned unexpected status: {response.status}"
                    )
    except Exception as e:
        logger.error(f"Error in keep-alive job: {str(e)}")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles Telegram API errors."""
    logger.error(f"Exception while handling an update: {context.error}")

    # Implement retry logic for specific error types
    if isinstance(context.error, Conflict):
        logger.warning("Conflict detected. Waiting before retrying...")
        await asyncio.sleep(30)  # Wait for 30 seconds before retrying
    elif isinstance(context.error, (TimedOut, NetworkError)):
        logger.warning(f"Network error: {context.error}. Retrying in 10 seconds...")
        await asyncio.sleep(10)  # Wait for 10 seconds before retrying

async def get_next_prayer(prayer_times):
    now = datetime.now(pytz.timezone("Asia/Amman"))
    today = now.date()
    prayer_datetimes = {}

    for prayer, time_str in prayer_times.items():
        prayer_time = datetime.strptime(f"{today} {time_str}", "%Y-%m-%d %I:%M %p")
        prayer_time = pytz.timezone("Asia/Amman").localize(prayer_time)
        prayer_datetimes[prayer] = prayer_time

    next_prayer = None
    for prayer, prayer_time in prayer_datetimes.items():
        if prayer_time > now:
            next_prayer = (prayer, prayer_time)
            break

    if next_prayer is None:
        # If no next prayer found today, get the first prayer of tomorrow
        tomorrow = today + timedelta(days=1)
        next_prayer = (
            "Fajr",
            pytz.timezone("Asia/Amman").localize(datetime.combine(tomorrow, time.min)),
        )

    return next_prayer


async def remaining_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prayer_times = await fetch_prayer_times()
    if prayer_times:
        next_prayer, next_prayer_time = await get_next_prayer(prayer_times)
        now = datetime.now(pytz.timezone("Asia/Amman"))
        time_remaining = next_prayer_time - now

        hours, remainder = divmod(time_remaining.seconds, 3600)
        minutes, _ = divmod(remainder, 60)

        emoji = PRAYER_EMOJIS.get(next_prayer, "🕰")
        message = (
            f"{emoji} Time remaining until {next_prayer}:\n\n{hours:02d}:{minutes:02d}"
        )
        
        # Send the message and store the message object
        bot_message = await update.message.reply_text(message)
        
        # Schedule deletion of both messages after 20 seconds
        context.job_queue.run_once(
            delete_messages,
            20,
            data={
                'chat_id': update.effective_chat.id,
                'message_ids': [update.message.message_id, bot_message.message_id]
            }
        )
    else:
        await update.message.reply_text(
            "Sorry, I couldn't fetch the prayer times. Please try again later."
        )

async def delete_messages(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    try:
        for message_id in job.data['message_ids']:
            await context.bot.delete_message(
                chat_id=job.data['chat_id'],
                message_id=message_id
            )
    except Exception as e:
        logger.error(f"Error deleting messages: {e}")

        
async def today_prayer_times(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prayer_times = await fetch_prayer_times()
    if prayer_times:
        message = "📅 Prayer times for today:\n\n"
        for prayer, time in prayer_times.items():
            emoji = PRAYER_EMOJIS.get(prayer, "🕰")
            message += f"{emoji} {prayer}: {time}\n"
        await update.message.reply_text(message)
    else:
        await update.message.reply_text(
            "Sorry, I couldn't fetch the prayer times. Please try again later."
        )


async def prayer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    prayer = query.data.split("_")[1]
    current_message_id = (
        query.message.message_id
    )  # Get the message ID of the current message (the one with the button)

    # Delete past notifications for this prayer, except for the current message
    if prayer in PRAYER_MESSAGE_IDS:
        for message_id in PRAYER_MESSAGE_IDS[prayer]:
            if message_id != current_message_id:  # Skip the current message
                try:
                    await context.bot.delete_message(
                        chat_id=CHAT_ID, message_id=message_id
                    )
                except Exception as e:
                    logger.error(f"Failed to delete message {message_id}: {str(e)}")

        # Clear the list after deleting messages
        del PRAYER_MESSAGE_IDS[prayer]

    # Edit the current message to indicate that the prayer was marked as prayed
    await query.edit_message_text(
        f"✅ {prayer} prayer marked as completed. May Allah accept your prayers!"
    )


def main():
    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("remaining", remaining_time))
    application.add_handler(CommandHandler("today", today_prayer_times))
    application.add_handler(CallbackQueryHandler(prayer_callback, pattern="^prayed_"))
    application.add_error_handler(error_handler)

    amman_tz = pytz.timezone("Asia/Amman")

    # Schedule daily update at 4 AM
    application.job_queue.run_daily(
        send_daily_update,
        time=time(hour=3, minute=0, tzinfo=amman_tz),
        name="daily_update",
    )

    # Schedule keep-alive every ~6 minutes
    application.job_queue.run_repeating(
        keep_alive_job, interval=340, first=10, name="keep_alive"
    )

    # Run the application using webhooks
    asyncio.run(run_webhook(application))


async def run_webhook(application: Application):
    """Initialize PTB Application, start aiohttp server and set Telegram webhook."""
    # Start PTB internals (bot, job queue, etc.)
    await application.initialize()

    # Start our aiohttp web server with health and webhook endpoints
    await web_server(application)

    # Configure Telegram webhook to point to this service
    hostname = os.environ.get("RENDER_EXTERNAL_HOSTNAME")
    if not hostname:
        logger.error(
            "RENDER_EXTERNAL_HOSTNAME is not set. Cannot configure Telegram webhook."
        )
        raise SystemExit(1)

    webhook_url = f"https://{hostname}{WEBHOOK_PATH}"
    secret = os.getenv("TELEGRAM_WEBHOOK_SECRET")
    try:
        await application.bot.set_webhook(
            url=webhook_url,
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES,
            secret_token=secret if secret else None,
        )
        logger.info(f"Webhook set to {webhook_url}")
    except Exception as e:
        logger.error(f"Failed to set webhook: {e}")
        raise

    # Start the application (starts the JobQueue scheduler)
    await application.start()

    # Keep running forever
    try:
        await asyncio.Event().wait()
    finally:
        await application.stop()
        await application.shutdown()


if __name__ == "__main__":
    main()
