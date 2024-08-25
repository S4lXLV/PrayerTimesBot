import asyncio
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import requests
from bs4 import BeautifulSoup
from datetime import datetime, time, timedelta
import pytz
import urllib3
from aiohttp import web
import os

# Disable SSL warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)



# Replace with your actual bot token
TOKEN = '7445923368:AAFH9UPTjo0k9kU_Bp9PeNnoTCl48y3VHeg'
# Replace with your actual chat ID
CHAT_ID = '651307921'

# Set this to True for testing, False for production
TESTING_MODE = False

def fetch_prayer_times():
    if TESTING_MODE:
        # Generate fake prayer times for testing
        now = datetime.now(pytz.timezone('Asia/Amman'))
        fake_times = {
            'Fajr': (now + timedelta(minutes=2)).strftime('%I:%M %p'),
            'Sunrise': (now + timedelta(minutes=4)).strftime('%I:%M %p'),
            'Dhuhr': (now + timedelta(minutes=6)).strftime('%I:%M %p'),
            'Asr': (now + timedelta(minutes=8)).strftime('%I:%M %p'),
            'Maghrib': (now + timedelta(minutes=10)).strftime('%I:%M %p'),
            'Isha': (now + timedelta(minutes=12)).strftime('%I:%M %p')
        }
        return fake_times
    else:
        url = 'https://www.awqaf.gov.jo/ar/Pages/PrayerTime'
        response = requests.get(url, verify=False)  # Disable SSL verification
        soup = BeautifulSoup(response.content, 'html.parser')
        
        table = soup.find('table', {'id': 'MainContent_gvWebparts'})
        rows = table.find_all('tr')[1:]  # Skip the header row
        
        today = datetime.now().strftime('%d/%m/%Y')
        for row in rows:
            columns = row.find_all('td')
            date = columns[0].text.strip()
            if date == today:
                return {
                    'Fajr': convert_to_12h(columns[1].text.strip()),
                    'Sunrise': convert_to_12h(columns[2].text.strip()),
                    'Dhuhr': convert_to_12h(columns[3].text.strip()),
                    'Asr': convert_to_12h(columns[4].text.strip()),
                    'Maghrib': convert_to_12h(columns[5].text.strip()),
                    'Isha': convert_to_12h(columns[6].text.strip())
                }
        return None

def convert_to_12h(time_str):
    time_obj = datetime.strptime(time_str, '%H:%M')
    return time_obj.strftime('%I:%M %p')

async def send_daily_update(context: ContextTypes.DEFAULT_TYPE):
    prayer_times = fetch_prayer_times()
    if prayer_times:
        message = "Prayer times for today:\n"
        for prayer, time in prayer_times.items():
            message += f"{prayer}: {time}\n"
        await context.bot.send_message(chat_id=CHAT_ID, text=message)
        await schedule_notifications(context, prayer_times)

async def send_notification(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    await context.bot.send_message(chat_id=CHAT_ID, text=job.data)

async def schedule_notifications(context: ContextTypes.DEFAULT_TYPE, prayer_times):
    now = datetime.now(pytz.timezone('Asia/Amman'))
    for prayer, time_str in prayer_times.items():
        prayer_time = datetime.strptime(f"{now.strftime('%Y-%m-%d')} {time_str}", "%Y-%m-%d %I:%M %p")
        prayer_time = pytz.timezone('Asia/Amman').localize(prayer_time)
        notify_time = prayer_time - timedelta(minutes=1)  # Reduced to 1 minute for testing
        
        if notify_time > now:
            message = f"Prayer time reminder: {prayer} prayer is in 1 minute (at {time_str})"
            context.job_queue.run_once(send_notification, notify_time - now, data=message)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bot started. You'll receive updates and notifications for prayer times.")
    await send_daily_update(context)  # Send update immediately for testing

async def handle(request):
    return web.Response(text="Your bot is running!")

async def web_server():
    app = web.Application()
    app.router.add_get("/", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"Web server started on port {port}")

def main():
    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))

    if not TESTING_MODE:
        # Schedule daily update at 8 AM (only in production mode)
        amman_tz = pytz.timezone('Asia/Amman')
        application.job_queue.run_daily(send_daily_update, 
                                        time=time(hour=8, minute=0, tzinfo=amman_tz))

    # Start the web server
    loop = asyncio.get_event_loop()
    loop.create_task(web_server())

    # Start the bot
    application.run_polling()

if __name__ == '__main__':
    main()
