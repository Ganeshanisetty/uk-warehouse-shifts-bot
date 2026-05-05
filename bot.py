import asyncio
import sqlite3
import logging
import os
from datetime import datetime, timedelta
from playwright.async_api import async_playwright
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

BOT_TOKEN = os.environ.get('BOT_TOKEN', '')
CHANNEL_USERNAME = os.environ.get('CHANNEL_USERNAME', '@ukwarehousejobs')
SCRAPE_INTERVAL_MINUTES = int(os.environ.get('SCRAPE_INTERVAL', '15'))

CHOOSE_PLAN, CHOOSE_JOB_TYPE, ENTER_CITIES, CONFIRM_CITIES = range(4)


def init_db():
    conn = sqlite3.connect('subscribers.db')
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            plan TEXT DEFAULT 'trial',
            job_type TEXT DEFAULT 'all',
            cities TEXT DEFAULT '',
            expiry TEXT DEFAULT ''
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS seen_jobs (
            job_id TEXT PRIMARY KEY
        )
    ''')
    conn.commit()
    conn.close()


def save_user(user_id, plan, job_type, cities, expiry):
    conn = sqlite3.connect('subscribers.db')
    c = conn.cursor()
    c.execute(
        'INSERT OR REPLACE INTO users (user_id, plan, job_type, cities, expiry) VALUES (?, ?, ?, ?, ?)',
        (user_id, plan, job_type, ','.join(cities), expiry)
    )
    conn.commit()
    conn.close()


def get_active_users():
    conn = sqlite3.connect('subscribers.db')
    c = conn.cursor()
    today = datetime.now().strftime('%Y-%m-%d')
    c.execute('SELECT user_id, job_type, cities FROM users WHERE expiry >= ?', (today,))
    rows = c.fetchall()
    conn.close()
    return rows


def get_user(user_id):
    conn = sqlite3.connect('subscribers.db')
    c = conn.cursor()
    c.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
    row = c.fetchone()
    conn.close()
    return row


def is_job_seen(job_id):
    conn = sqlite3.connect('subscribers.db')
    c = conn.cursor()
    c.execute('SELECT 1 FROM seen_jobs WHERE job_id = ?', (job_id,))
    result = c.fetchone()
    conn.close()
    return result is not None


def mark_job_seen(job_id):
    conn = sqlite3.connect('subscribers.db')
    c = conn.cursor()
    c.execute('INSERT OR IGNORE INTO seen_jobs (job_id) VALUES (?)', (job_id,))
    conn.commit()
    conn.close()


async def scrape_amazon_jobs():
    jobs = []
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage']
            )
            page = await browser.new_page()
            api_jobs = []

            async def handle_response(response):
                url = response.url.lower()
                if 'job' in url and response.status == 200:
                    try:
                        ct = response.headers.get('content-type', '')
                        if 'json' in ct:
                            data = await response.json()
                            if isinstance(data, dict):
                                if 'jobs' in data:
                                    api_jobs.extend(data['jobs'])
                                elif 'results' in data:
                                    api_jobs.extend(data['results'])
                            elif isinstance(data, list):
                                api_jobs.extend(data)
                    except Exception:
                        pass

            page.on('response', handle_response)

            await page.goto(
                'https://www.jobsatamazon.co.uk/app#/jobSearch',
                wait_until='networkidle',
                timeout=60000
            )
            await page.wait_for_timeout(5000)

            if api_jobs:
                for j in api_jobs:
                    jobs.append({
                        'id': j.get('jobId', j.get('id', str(hash(str(j))))),
                        'title': j.get('title', 'Warehouse Operative'),
                        'location': j.get('location', j.get('city', j.get('siteName', ''))),
                        'pay_rate': j.get('payRate', j.get('pay', j.get('salary', 'Competitive'))),
                        'type': j.get('employmentType', j.get('contractType', 'Flex')),
                        'duration': j.get('duration', 'Seasonal'),
                        'schedule': j.get('schedule', j.get('shiftPattern', 'Various')),
                        'hours': str(j.get('hoursPerWeek', j.get('hours', 'Various'))),
                        'start_date': j.get('startDate', j.get('firstDay', 'TBC')),
                        'vacancies': str(j.get('vacancies', j.get('openings', '1'))),
                        'description': j.get('jobDescription', j.get('description', 'Pick, pack and ship customer orders')),
                        'apply_url': f"https://www.jobsatamazon.co.uk/app#/jobDetail?jobId={j.get('jobId', j.get('id', ''))}"
                    })
            else:
                job_cards = await page.query_selector_all('[class*="job"], [data-job-id], .job-card')
                for card in job_cards[:50]:
                    try:
                        title_el = await card.query_selector('h1, h2, h3, [class*="title"]')
                        loc_el = await card.query_selector('[class*="location"], [class*="city"]')
                        pay_el = await card.query_selector('[class*="pay"], [class*="salary"]')
                        link_el = await card.query_selector('a[href]')
                        href = await link_el.get_attribute('href') if link_el else ''
                        job_id = href.split('jobId=')[-1] if 'jobId=' in (href or '') else str(hash(href))
                        title = await title_el.inner_text() if title_el else 'Warehouse Operative'
                        loc = await loc_el.inner_text() if loc_el else 'UK'
                        pay = await pay_el.inner_text() if pay_el else 'Competitive'
                        if title.strip():
                            jobs.append({
                                'id': job_id,
                                'title': title.strip(),
                                'location': loc.strip(),
                                'pay_rate': pay.strip(),
                                'type': 'Flex',
                                'duration': 'Seasonal',
                                'schedule': 'Various',
                                'hours': 'Various',
                                'start_date': 'TBC',
                                'vacancies': '1',
                                'description': 'Pick, pack and ship customer orders',
                                'apply_url': f'https://www.jobsatamazon.co.uk{href}' if href and not href.startswith('http') else (href or 'https://www.jobsatamazon.co.uk/app#/jobSearch')
                            })
                    except Exception as e:
                        logging.warning(f'Card parse error: {e}')

            await browser.close()
    except Exception as e:
        logging.error(f'Scrape error: {e}')
    return jobs


def format_message(job):
    return (
        f"\U0001f4cd {job['location']}\n"
        f"\U0001f3ed {job['title']} | {job['vacancies']} vacancies\n"
        f"\U0001f3ed {job['duration']} | {job['type']}\n"
        f"\U0001f4ac {job['description'][:120]}\n"
        f"\U0001f4b0 {job['pay_rate']} /hr\n"
        f"\U0001f4c5 First Day: {job['start_date']}\n"
        f"\U0001f550 Schedule: {job['schedule']}\n"
        f"\U0001f310 Hours/Week: {job['hours']}\n"
        f"\U0001f517 {job['apply_url']}"
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton('\U0001f193 Free 1-Day Trial', callback_data='plan_trial')],
        [InlineKeyboardButton('\U0001f4b3 \u00a315 / 2 Months', callback_data='plan_2month')],
        [InlineKeyboardButton('\U00002b50 \u00a350 / Year - Best Value', callback_data='plan_annual')],
    ]
    await update.message.reply_text(
        '\U0001f3af *UK Amazon Warehouse Jobs - Premium Alerts*\n\n'
        '\u2705 Alerts ONLY for YOUR chosen cities\n'
        '\u2705 Choose your job type preference\n'
        '\u2705 Delivered straight to your DMs\n\n'
        f'\U0001f4e2 Free channel (all UK jobs): {CHANNEL_USERNAME}\n\n'
        'Choose your plan below \U0001f447',
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )
    return CHOOSE_PLAN


async def plan_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    plan = query.data
    context.user_data['plan'] = plan
    days = {'plan_trial': 1, 'plan_2month': 60, 'plan_annual': 365}
    expiry = (datetime.now() + timedelta(days=days.get(plan, 1))).strftime('%Y-%m-%d')
    context.user_data['expiry'] = expiry
    keyboard = [
        [InlineKeyboardButton('\U0001f3e2 Full-time & Reduced Hours', callback_data='jt_fulltime')],
        [InlineKeyboardButton('\U0001f3af Part-time & Flex', callback_data='jt_parttime')],
        [InlineKeyboardButton('\U0001f4cb All Job Types', callback_data='jt_all')],
    ]
    await query.edit_message_text(
        '\u2705 Plan selected!\n\nNow choose your job type preference:',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return CHOOSE_JOB_TYPE


async def jobtype_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['job_type'] = query.data
    await query.edit_message_text(
        '\U0001f4cd Now enter your city or cities:\n\n'
        'Example: *Birmingham, London, Hull, Bristol*\n\n'
        'Just type the city names separated by commas:',
        parse_mode='Markdown'
    )
    return ENTER_CITIES


async def cities_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    plan = context.user_data.get('plan', 'plan_trial')
    cities = [c.strip() for c in update.message.text.split(',') if c.strip()]
    if not cities:
        await update.message.reply_text('Please enter at least one city.')
        return ENTER_CITIES
    if plan == 'plan_trial' and len(cities) > 2:
        await update.message.reply_text(
            f'\u26a0\ufe0f Trial is limited to 2 cities.\nYou entered {len(cities)}.\nPlease enter up to 2 cities:'
        )
        return ENTER_CITIES
    context.user_data['cities'] = cities
    keyboard = [
        [InlineKeyboardButton('\u2705 Yes, Confirm', callback_data='confirm_yes'),
         InlineKeyboardButton('\u274c Re-enter', callback_data='confirm_no')],
        [InlineKeyboardButton('\u21a9\ufe0f Cancel', callback_data='confirm_cancel')]
    ]
    await update.message.reply_text(
        f'\U0001f4cd Confirm your cities:\n\n\u2705 {", ".join(cities)}\n\nIs this correct?',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return CONFIRM_CITIES


async def confirm_cities(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == 'confirm_yes':
        save_user(
            query.from_user.id,
            context.user_data['plan'],
            context.user_data['job_type'],
            context.user_data['cities'],
            context.user_data['expiry']
        )
        plan_names = {'plan_trial': 'Free 1-Day Trial', 'plan_2month': '\u00a315 / 2 Months', 'plan_annual': '\u00a350 / Year'}
        await query.edit_message_text(
            f'\u2705 *All set!*\n\n'
            f'Plan: {plan_names.get(context.user_data["plan"], "")}\n'
            f'Cities: {", ".join(context.user_data["cities"])}\n'
            f'Expires: {context.user_data["expiry"]}\n\n'
            f'You will now receive job alerts for your chosen cities!',
            parse_mode='Markdown'
        )
        return ConversationHandler.END
    elif query.data == 'confirm_no':
        await query.edit_message_text('Please type your cities again:')
        return ENTER_CITIES
    else:
        await query.edit_message_text('\u21a9\ufe0f Cancelled.')
        return ConversationHandler.END


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)
    if not user:
        await update.message.reply_text(
            'You don\'t have an active subscription.\nSend /start to subscribe.'
        )
        return
    _, plan, job_type, cities, expiry = user
    plan_names = {'plan_trial': 'Free Trial', 'plan_2month': '\u00a315 / 2 Months', 'plan_annual': '\u00a350 / Year'}
    await update.message.reply_text(
        f'\U0001f4ca *Your Subscription*\n\n'
        f'Plan: {plan_names.get(plan, plan)}\n'
        f'Cities: {cities}\n'
        f'Job Type: {job_type.replace("jt_", "").title()}\n'
        f'Expires: {expiry}',
        parse_mode='Markdown'
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        '\U0001f916 *UK Warehouse Shifts Bot*\n\n'
        '/start - Subscribe & set your cities\n'
        '/status - View your subscription\n'
        '/help - Show this message\n\n'
        f'\U0001f4e2 Free channel: {CHANNEL_USERNAME}',
        parse_mode='Markdown'
    )


async def send_job_alerts(app):
    logging.info('Fetching Amazon jobs...')
    try:
        jobs = await scrape_amazon_jobs()
        logging.info(f'Found {len(jobs)} jobs total')
        new_count = 0
        for job in jobs:
            job_id = job.get('id', '')
            if not job_id or is_job_seen(job_id):
                continue
            mark_job_seen(job_id)
            new_count += 1
            msg = format_message(job)
            try:
                await app.bot.send_message(
                    chat_id=CHANNEL_USERNAME,
                    text=msg,
                    disable_web_page_preview=True
                )
                await asyncio.sleep(1)
            except Exception as e:
                logging.error(f'Channel post error: {e}')
            for user_id, job_type, cities_str in get_active_users():
                user_cities = [c.strip().lower() for c in cities_str.split(',') if c.strip()]
                job_loc = job.get('location', '').lower()
                job_title = job.get('title', '').lower()
                if not any(city in job_loc or city in job_title for city in user_cities):
                    continue
                if job_type == 'jt_fulltime':
                    jt = job.get('type', '').lower()
                    if 'full' not in jt and 'reduced' not in jt:
                        continue
                elif job_type == 'jt_parttime':
                    jt = job.get('type', '').lower()
                    if 'part' not in jt and 'flex' not in jt:
                        continue
                try:
                    await app.bot.send_message(
                        chat_id=user_id,
                        text=msg,
                        disable_web_page_preview=True
                    )
                    await asyncio.sleep(0.5)
                except Exception as e:
                    logging.warning(f'DM error user {user_id}: {e}')
        logging.info(f'Posted {new_count} new jobs')
    except Exception as e:
        logging.error(f'send_job_alerts error: {e}')


async def send_expiry_warnings(app):
    conn = sqlite3.connect('subscribers.db')
    c = conn.cursor()
    tomorrow = (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d')
    c.execute('SELECT user_id FROM users WHERE expiry = ?', (tomorrow,))
    users = c.fetchall()
    conn.close()
    for (user_id,) in users:
        try:
            await app.bot.send_message(
                chat_id=user_id,
                text=(
                    '\u26a0\ufe0f *Subscription Expiring Soon!*\n\n'
                    f'\U0001f4c5 Expires: {tomorrow}\n'
                    '\u23f3 1 day remaining\n\n'
                    'Send /start to renew your subscription.'
                ),
                parse_mode='Markdown'
            )
        except Exception as e:
            logging.warning(f'Expiry warning error {user_id}: {e}')


def main():
    init_db()
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    conv = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            CHOOSE_PLAN: [CallbackQueryHandler(plan_chosen, pattern='^plan_')],
            CHOOSE_JOB_TYPE: [CallbackQueryHandler(jobtype_chosen, pattern='^jt_')],
            ENTER_CITIES: [MessageHandler(filters.TEXT & ~filters.COMMAND, cities_entered)],
            CONFIRM_CITIES: [CallbackQueryHandler(confirm_cities, pattern='^confirm_')],
        },
        fallbacks=[CommandHandler('start', start)],
    )
    app.add_handler(conv)
    app.add_handler(CommandHandler('status', status_command))
    app.add_handler(CommandHandler('help', help_command))

    async def scheduler():
        await asyncio.sleep(10)
        while True:
            await send_job_alerts(app)
            await send_expiry_warnings(app)
            await asyncio.sleep(SCRAPE_INTERVAL_MINUTES * 60)

    async def post_init(application):
        asyncio.create_task(scheduler())

    app.post_init = post_init
    app.run_polling(drop_pending_updates=True)


if __name__ == '__main__':
    main()
