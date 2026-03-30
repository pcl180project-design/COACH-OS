"""
CoachOS — Automated WhatsApp Scheduler
=======================================
Runs in the background and automatically sends:
  1. Weekly group check-in reminder (your chosen day + time)
  2. Personal DM follow-ups 24hrs later to anyone still missing
  3. Inactivity reminders if a client hasn't posted in 3+ days
  4. Weekly resource delivery to all clients
  5. Onboarding sequence — welcome + files on Day 1
  6. Daily video drip — one video per day for first 10 days
  7. Weekly form link — sent to each client personally on check-in day
  8. Daily form link — sent once during onboarding (client bookmarks it)

Setup:
  pip install -r requirements.txt
  cp .env.example .env        ← fill in your Twilio credentials
  python scheduler.py

Keep it running on a server (Railway, Render, or your own machine).
"""

import os
import time
import logging
import schedule
from datetime import datetime, timedelta
from dotenv import load_dotenv
from twilio_client import send_whatsapp, send_whatsapp_media
try:
    from ai_coach import (
        generate_weekly_feedback,
        generate_daily_reply,
        generate_coach_summary,
        generate_coach_briefing,
        detect_warnings,
    )
    AI_AVAILABLE = True
except ImportError:
    AI_AVAILABLE = False
from data_store import (
    load_data,
    get_clients,
    get_client,
    get_settings,
    get_resources,
    mark_resource_sent,
    get_drip_videos,
    flag_exists,
    set_flag,
    log_event,
    update_client,
)

# ── Setup ──────────────────────────────────────────────────────────────────
load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler("coachos.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("CoachOS")

# ── Config (edit these or set them in .env) ────────────────────────────────
CHECKIN_REMINDER_DAY   = os.getenv("CHECKIN_REMINDER_DAY",  "sunday")    # day of week
CHECKIN_REMINDER_TIME  = os.getenv("CHECKIN_REMINDER_TIME", "09:00")     # 24-hr HH:MM
RESOURCE_DELIVERY_DAY  = os.getenv("RESOURCE_DELIVERY_DAY", "wednesday") # day of week
RESOURCE_DELIVERY_TIME = os.getenv("RESOURCE_DELIVERY_TIME","09:00")
INACTIVITY_CHECK_TIME  = os.getenv("INACTIVITY_CHECK_TIME", "10:00")     # daily check
INACTIVITY_DAYS        = int(os.getenv("INACTIVITY_DAYS",   "3"))        # days before reminder
GROUP_NUMBER           = os.getenv("WHATSAPP_GROUP_NUMBER", "")          # your WA group ID
ONBOARDING_SEND_TIME   = os.getenv("ONBOARDING_SEND_TIME",  "09:00")     # daily drip time
AI_ENABLED             = os.getenv("ANTHROPIC_API_KEY", "") != ""             # auto-enabled if key is set
COACH_PHONE            = os.getenv("COACH_PHONE", "")                         # YOUR WhatsApp number for briefings
FORMS_BASE_URL         = os.getenv("FORMS_BASE_URL", "")                   # URL where your forms are hosted
SUPABASE_URL           = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY           = os.getenv("SUPABASE_KEY", "")

# ── Message templates ──────────────────────────────────────────────────────
_template_cache = {}

def get_template(key: str, default: str) -> str:
    """
    Fetch a message template from Supabase.
    Falls back to the hardcoded default if not found.
    Caches templates for 5 minutes to avoid excessive DB calls.
    """
    import time
    cache_key = f"tpl_{key}"
    cached = _template_cache.get(cache_key)
    if cached and time.time() - cached[1] < 300:
        return cached[0]
    try:
        from data_store import _db
        res = _db().table("message_templates").select("content").eq("key", key).single().execute()
        if res.data and res.data.get("content"):
            _template_cache[cache_key] = (res.data["content"], time.time())
            return res.data["content"]
    except Exception:
        pass
    return default


GROUP_CHECKIN_MSG = """Hey everyone! 👋✨

It's check-in day! 📝

This is your moment to reflect on the week — your wins, challenges, and how far you've come. Don't skip this — it's one of the most powerful parts of the programme!

Drop your check-in below. Even just a few sentences is perfect. We're all rooting for you! 💚🙌

— Your Coach"""

PERSONAL_DM_TEMPLATE = """Hi {first_name}! 💚

Just a personal nudge — we haven't seen your Week {week} check-in yet and we'd LOVE to hear from you!

Even a quick update is totally fine. This is your journey and your voice matters in this group. 🌟

Whenever you're ready — we're here! 🙏
— Your Coach"""

INACTIVITY_TEMPLATE = """Hi {first_name}! 👋

Just checking in — we haven't heard from you in the group for {days} days and want to make sure you're okay! 💚

Remember, this journey is yours and we're here to support you every step of the way. Drop us a message when you're ready!

You've got this! 💪
— Your Coach"""

RESOURCE_GROUP_TEMPLATE = """📦 Week {week} resources are now live!

{resource_list}

Check the group files or click the links above. Let us know what you think! 💬

— Your Coach"""

# ── Onboarding templates ───────────────────────────────────────────────────
ONBOARDING_WELCOME_MSG = """Hi {first_name}! 🎉 Welcome to the programme!

I'm SO excited to have you here. You've made an incredible decision and I can't wait to support you on this journey. 💚

Here's what's coming your way right now:
📄 What to expect — so you know exactly how this works
🎥 My personal introduction video — who I am and why I do this
📋 Your programme overview — the full roadmap ahead

Take your time with each one. This is the start of something really special. 🌟

Any questions at all — just message me here. I've got you!

— Your Coach"""

ONBOARDING_WHAT_TO_EXPECT_MSG = """📋 *What to Expect*

Here's how the next {total_days} days will work:

✅ *Daily videos* — every morning for the first 10 days you'll receive a short video to watch at your own pace
✅ *Weekly resources* — PDFs, workbooks and prompts delivered each week
✅ *Weekly check-ins* — share your progress every Sunday in the group
✅ *Community support* — this group is your safe space, use it!
✅ *Personal support* — message me directly any time

A few group guidelines:
🙏 Be kind and supportive to everyone
🔒 What's shared here stays here
📱 Engage as much as you can — the more you put in, the more you get out

Let's do this! 💪
— Your Coach"""



# ── Form link builder ──────────────────────────────────────────────────────
def _build_form_links(client: dict, week: int) -> tuple[str, str]:
    """
    Build personalised weekly check-in and daily form URLs for a client.
    Returns (weekly_url, daily_url)
    """
    from urllib.parse import quote
    base     = FORMS_BASE_URL.rstrip("/")
    name_enc = quote(client["name"])
    url_enc  = quote(SUPABASE_URL)
    key_enc  = quote(SUPABASE_KEY)
    cid      = client["id"]

    weekly = f"{base}/weekly-form.html?url={url_enc}&key={key_enc}&client_id={cid}&name={name_enc}&week={week}"
    daily  = f"{base}/daily-form.html?url={url_enc}&key={key_enc}&client_id={cid}&name={name_enc}"
    return weekly, daily

# Daily video drip message templates (Days 1–10)
# Video URLs are fetched live from Supabase drip_videos table
DAILY_VIDEO_TEMPLATES = [
    # Day 1
    """🎥 *Day 1 — Why You're Here*

Hi {first_name}! Your first video is ready. 🌟

Today we're talking about the most important thing: your WHY. Understanding this will carry you through the whole programme.

Watch here 👇
{url}

Let me know one word that describes how you're feeling today! 💬
— Your Coach""",

    # Day 2
    """🎥 *Day 2 — Your Mindset Is Everything*

Good morning {first_name}! ☀️

Day 2 is all about mindset — the foundation everything else is built on. This one's a game changer.

Watch here 👇
{url}

— Your Coach""",

    # Day 3
    """🎥 *Day 3 — Breaking Old Patterns*

Hey {first_name}! 👋

Today we're getting real about the patterns that have been holding you back — and exactly how to break them.

Watch here 👇
{url}

— Your Coach""",

    # Day 4
    """🎥 *Day 4 — Building Your Foundation*

Morning {first_name}! 💚

Day 4 is about setting the daily foundations that make everything easier. Short video, massive impact.

Watch here 👇
{url}

— Your Coach""",

    # Day 5
    """🎥 *Day 5 — Halfway Through Week 1!*

You're doing amazing, {first_name}! 🙌

5 days in — that's something to be proud of. Today's video is about momentum and why consistency beats perfection every time.

Watch here 👇
{url}

— Your Coach""",

    # Day 6
    """🎥 *Day 6 — Nutrition Made Simple*

Hi {first_name}! 🥗

No complicated rules today — just a simple, sustainable approach to nourishing your body that actually fits your life.

Watch here 👇
{url}

— Your Coach""",

    # Day 7
    """🎥 *Day 7 — Movement You Actually Enjoy*

One week in, {first_name}! 🎉

Today we're talking about movement — not punishment, not rules, just finding what lights you up and makes you feel good.

Watch here 👇
{url}

— Your Coach""",

    # Day 8
    """🎥 *Day 8 — The Power of Rest*

Good morning {first_name}! 😴

Rest isn't laziness — it's where transformation actually happens. Today's video will change how you think about recovery.

Watch here 👇
{url}

— Your Coach""",

    # Day 9
    """🎥 *Day 9 — Your Support System*

Hey {first_name}! 💛

You don't have to do this alone. Day 9 is about building the support system around you that makes success inevitable.

Watch here 👇
{url}

— Your Coach""",

    # Day 10
    """🎥 *Day 10 — You Made It! What's Next*

{first_name}, you've completed your first 10 days! 🏆✨

I am SO proud of you. Today's final video is about everything that's coming next and how to make the most of the rest of the programme.

Watch here 👇
{url}

Keep going — the best is yet to come! 💚
— Your Coach""",
]


# ══════════════════════════════════════════════════════════════════════════
#  JOB 5 — Onboarding sequence (triggered when a new client is added)
# ══════════════════════════════════════════════════════════════════════════
def job_onboarding_new_client(client: dict):
    """
    Send the Day 1 onboarding burst to a single new client:
      1. Welcome message
      2. What to expect message
      3. Coach intro video
      4. Programme overview PDF
    All sent immediately when the client is added.
    """
    log.info(f"▶ JOB: Onboarding — {client['name']}")
    first    = client["name"].split()[0]
    phone    = client["phone"]
    settings = get_settings()
    total_d  = len(DAILY_VIDEO_TEMPLATES)

    results = []
    # Send all onboarding messages to client's personal group (or phone if no group set)
    dest = _client_dest(client)

    # 1 — Welcome message
    msg1 = get_template("onboarding_welcome", ONBOARDING_WELCOME_MSG).format(first_name=first)
    ok1  = send_whatsapp(dest, msg1)
    results.append(("Welcome message", ok1))
    time.sleep(2)

    # 2 — What to expect
    msg2 = get_template("onboarding_what_to_expect", ONBOARDING_WHAT_TO_EXPECT_MSG).format(
        first_name=first, total_days=total_d
    )
    ok2 = send_whatsapp(dest, msg2)
    results.append(("What to expect", ok2))
    time.sleep(2)

    # 3 — Coach intro video
    intro_url = settings.get("intro_video_url", "")
    if intro_url:
        msg3 = (
            f"🎥 *Coach Introduction*\n\n"
            f"Hi {first}! Here's a short video from me — who I am, my story, "
            f"and why I'm so passionate about this work. 💚\n\n{intro_url}"
        )
        ok3 = send_whatsapp(dest, msg3)
        results.append(("Coach intro video", ok3))
        time.sleep(2)

    # 4 — Programme overview PDF
    overview_url = settings.get("programme_overview_url", "")
    if overview_url:
        msg4 = (
            f"📄 *Programme Overview*\n\n"
            f"Here's your full programme roadmap, {first}. "
            f"Keep this handy — it's your guide for the whole journey! 🗺️\n\n{overview_url}"
        )
        ok4 = send_whatsapp(dest, msg4)
        results.append(("Programme overview PDF", ok4))
        time.sleep(2)

    # Log results to Supabase activity log
    for label, ok in results:
        log_event(
            event_type="onboarding",
            description=f"Onboarding — {label} → {client['name']}",
            client_id=client["id"],
            status="sent" if ok else "failed",
        )
        log.info(f"  {label} → {client['name']} {'✓' if ok else '✗'}")

    # Send daily accountability form link (client bookmarks this)
    if FORMS_BASE_URL:
        settings     = get_settings()
        current_week = settings.get("current_week", 1)
        _, daily_url = _build_form_links(client, current_week)
        daily_msg = (
            f"📋 *Your Daily Accountability Form*\n\n"
            f"Hi {first}! Please bookmark this link — you'll use it every day to log "
            f"your weight, steps, sleep, diet and how you're feeling:\n\n"
            f"👉 {daily_url}\n\n"
            f"Takes just 2 minutes a day and helps me track your progress properly. "
            f"The more consistent you are, the better I can support you! 💚\n"
            f"— Your Coach"
        )
        ok_daily = send_whatsapp(phone, daily_msg)
        log_event(
            event_type="onboarding",
            description=f"Daily form link sent to {client['name']}",
            client_id=client["id"],
            status="sent" if ok_daily else "failed",
        )
        log.info(f"  Daily form link → {client['name']} {'✓' if ok_daily else '✗'}")

    # Mark Day 1 drip as sent so the daily job doesn't duplicate it
    set_flag(f"drip_{client['id']}_day_1")
    log.info(f"  Onboarding complete for {client['name']} ✓")


# ══════════════════════════════════════════════════════════════════════════
#  JOB 6 — Daily video drip (Days 1–10 per client)
# ══════════════════════════════════════════════════════════════════════════
def job_daily_video_drip():
    """
    Runs every morning. For each client still within their first 10 days,
    sends the next video in the drip sequence.
    Day 1 video is sent as part of onboarding — this job handles Days 2–10.
    """
    log.info("▶ JOB: Daily video drip")

    clients    = get_clients()
    drip_videos = {v["day_number"]: v["video_url"] for v in get_drip_videos()}
    now        = datetime.now()
    sent       = 0

    for client in clients:
        # FIX: was client.get("start") — Supabase column is start_date
        start_str = client.get("start_date")
        if not start_str:
            continue

        try:
            start_date = datetime.fromisoformat(start_str)
        except ValueError:
            continue

        day_number = (now.date() - start_date.date()).days + 1

        if day_number < 1 or day_number > 10:
            continue

        flag_key = f"drip_{client['id']}_day_{day_number}"

        # FIX: use Supabase flag_exists() instead of local dict lookup
        if flag_exists(flag_key):
            log.info(f"  Skipping {client['name']} Day {day_number} — already sent")
            continue

        idx = day_number - 1
        if idx >= len(DAILY_VIDEO_TEMPLATES):
            continue

        video_url = drip_videos.get(day_number, "")
        default_tpl = DAILY_VIDEO_TEMPLATES[idx]
        template    = get_template(f"daily_video_{day_number}", default_tpl)
        msg         = template.format(
            first_name=client["name"].split()[0],
            url=video_url,
        )

        ok = send_whatsapp(client["phone"], msg)

        # FIX: persist flag to Supabase, not local dict
        set_flag(flag_key)

        # FIX: log directly to Supabase activity log
        log_event(
            event_type="video_drip",
            description=f"Day {day_number} video sent to {client['name']}",
            client_id=client["id"],
            status="sent" if ok else "failed",
        )
        log.info(f"  Day {day_number} video → {client['name']} {'✓' if ok else '✗'}")
        sent += 1
        time.sleep(2)

    log.info(f"  Done — {sent} drip videos sent today")


# ══════════════════════════════════════════════════════════════════════════
#  JOB 1 — Weekly group check-in reminder
# ══════════════════════════════════════════════════════════════════════════
def job_group_checkin_reminder():
    log.info("▶ JOB: Group check-in reminder")
    settings     = get_settings()
    current_week = settings.get("current_week", 1)

    # 1 — Send group reminder message
    if GROUP_NUMBER:
        ok = send_whatsapp(GROUP_NUMBER, get_template("group_checkin", GROUP_CHECKIN_MSG).format(first_name="everyone"))
        log_event(
            event_type="group_reminder",
            description=f"Group check-in reminder sent (Week {current_week})",
            status="sent" if ok else "failed",
        )
        log.info(f"  Group message → {'✓ sent' if ok else '✗ failed'}")
    else:
        log.warning("  GROUP_NUMBER not set — skipping group message")

    # 2 — Send a second group message telling everyone to check their DMs
    if FORMS_BASE_URL and GROUP_NUMBER:
        group_form_msg = (
            f"📏 *Week {current_week} Progress Check-in*\n\n"
            f"Your personal measurement form has been sent to each of you directly — "
            f"check your DMs! 📬\n\n"
            f"Takes just 2 minutes to fill in your measurements. "
            f"Please complete it today so I can track everyone's progress! 💚\n\n"
            f"— Your Coach"
        )
        send_whatsapp(GROUP_NUMBER, group_form_msg)
        log.info("  Group form announcement sent")

    # 3 — DM each client their personal weekly progress form link
    if FORMS_BASE_URL:
        clients    = get_clients()
        sent_forms = 0
        time.sleep(3)  # brief pause after group messages

        for client in clients:
            flag_key = f"week_{current_week}_form_link_{client['id']}"
            if flag_exists(flag_key):
                log.info(f"  Skipping form link for {client['name']} — already sent this week")
                continue

            weekly_url, _ = _build_form_links(client, current_week)
            first = client["name"].split()[0]
            form_tpl = get_template("weekly_form_link",
                "Hi {first_name}! \n\nIt's check-in day! Here's your Week {week} progress form "
                "— tap the link and fill in your measurements. Takes 2 minutes:\n\n{url}\n\n"
                "Please fill this today so I can track your progress!\n— Your Coach")
            msg = form_tpl.format(first_name=first, week=current_week, url=weekly_url)
            # Send form link to client's personal group
            dest = _client_dest(client)
            ok = send_whatsapp(dest, msg)
            set_flag(flag_key)
            log_event(
                event_type="form_link_sent",
                description=f"Weekly form link sent to {client['name']} (Week {current_week})",
                client_id=client["id"],
                status="sent" if ok else "failed",
            )
            log.info(f"  Weekly form link → {client['name']} {'✓' if ok else '✗'}")
            sent_forms += 1
            time.sleep(2)

        log.info(f"  Weekly form links sent to {sent_forms} clients")
    else:
        log.info("  FORMS_BASE_URL not set — skipping form links (add to .env to enable)")

    set_flag(f"week_{current_week}_group_sent")
    log.info("  Scheduled personal DM follow-up for 24 hrs from now")

# ══════════════════════════════════════════════════════════════════════════
#  JOB 2 — Personal DM follow-up (runs 24hrs after group reminder)
# ══════════════════════════════════════════════════════════════════════════
def job_personal_dm_followup():
    log.info("▶ JOB: Personal DM follow-ups")
    settings     = get_settings()
    current_week = settings.get("current_week", 1)

    # FIX: was c.get("ci_week") — Supabase column is checkin_week
    clients = get_clients()
    missing = [c for c in clients if c.get("checkin_week", 0) < current_week]

    if not missing:
        log.info("  No missing check-ins — skipping DMs 🎉")
        return

    sent_count = 0
    for client in missing:
        flag_key = f"week_{current_week}_dm_{client['id']}"

        # FIX: use Supabase flag_exists() / set_flag()
        if flag_exists(flag_key):
            log.info(f"  Skipping {client['name']} — DM already sent this week")
            continue

        msg = get_template("personal_dm", PERSONAL_DM_TEMPLATE).format(
            first_name=client["name"].split()[0],
            week=current_week,
        )
        dest = _client_dest(client)
        ok = send_whatsapp(dest, msg)
        set_flag(flag_key)

        log_event(
            event_type="personal_dm",
            description=f"Personal DM sent to {client['name']} (Week {current_week} missing)",
            client_id=client["id"],
            status="sent" if ok else "failed",
        )
        log.info(f"  DM → {client['name']} ({client['phone']}) {'✓' if ok else '✗'}")
        sent_count += 1
        time.sleep(2)

    log.info(f"  Done — {sent_count} DMs sent, {len(missing)-sent_count} already sent")


# ══════════════════════════════════════════════════════════════════════════
#  JOB 3 — Daily inactivity check
# ══════════════════════════════════════════════════════════════════════════
def job_inactivity_check():
    log.info("▶ JOB: Daily inactivity check")
    clients = get_clients()
    now     = datetime.now()
    flagged = 0

    for client in clients:
        last_active_str = client.get("last_active")
        if not last_active_str:
            continue

        try:
            last_active = datetime.fromisoformat(last_active_str)
        except ValueError:
            continue

        days_inactive = (now - last_active).days

        if days_inactive >= INACTIVITY_DAYS:
            today_key = f"inactivity_{client['id']}_{now.date()}"

            # FIX: use Supabase flag_exists() / set_flag()
            if flag_exists(today_key):
                continue

            msg = get_template("inactivity", INACTIVITY_TEMPLATE).format(
                first_name=client["name"].split()[0],
                days=days_inactive,
            )
            dest = _client_dest(client)
            ok = send_whatsapp(dest, msg)
            set_flag(today_key)

            log_event(
                event_type="inactivity",
                description=f"Inactivity reminder sent to {client['name']} ({days_inactive} days)",
                client_id=client["id"],
                status="sent" if ok else "failed",
            )
            log.info(f"  Inactivity → {client['name']} ({days_inactive}d inactive) {'✓' if ok else '✗'}")
            flagged += 1
            time.sleep(2)

    log.info(f"  Done — {flagged} inactivity reminders sent")


# ══════════════════════════════════════════════════════════════════════════
#  JOB 4 — Weekly resource delivery
# ══════════════════════════════════════════════════════════════════════════
def job_resource_delivery():
    log.info("▶ JOB: Weekly resource delivery")
    settings     = get_settings()
    current_week = settings.get("current_week", 1)

    # FIX: was r.get("week") — Supabase column is week_number; use unsent_only flag
    resources = get_resources(week=current_week, unsent_only=True)

    if not resources:
        log.info(f"  No pending resources for Week {current_week}")
        return

    type_icons = {"pdf": "📄", "video": "🎥", "prompt": "💬", "motivation": "✨"}
    lines = []
    for r in resources:
        icon    = type_icons.get(r.get("type", "pdf"), "📌")
        content = r.get("content", "")
        lines.append(f"{icon} {r['name']}: {content}")

    resource_list = "\n".join(lines)
    msg = get_template("resource_delivery", RESOURCE_GROUP_TEMPLATE).format(week=current_week, resource_list=resource_list)

    if GROUP_NUMBER:
        ok = send_whatsapp(GROUP_NUMBER, msg)
        log.info(f"  Resource group message → {'✓ sent' if ok else '✗ failed'}")
    else:
        clients = get_clients()
        for client in clients:
            dest = _client_dest(client)
            ok = send_whatsapp(dest, msg)
            log.info(f"  Resources → {client['name']} {'✓' if ok else '✗'}")
            time.sleep(2)

    # FIX: use mark_resource_sent() to update each resource in Supabase
    for r in resources:
        mark_resource_sent(r["id"])

    # FIX: log directly to Supabase
    log_event(
        event_type="resources",
        description=f"Week {current_week} resources delivered ({len(resources)} items)",
        status="sent",
    )
    log.info(f"  Done — {len(resources)} resources marked sent")



# ══════════════════════════════════════════════════════════════════════════
#  JOB 7 — AI Weekly Feedback (fires when client submits Sunday form)
# ══════════════════════════════════════════════════════════════════════════
def job_ai_weekly_feedback():
    """
    Checks for weekly progress submissions that haven't received AI feedback yet.
    Generates a personalised message and sends it via WhatsApp.
    Runs every 30 minutes on check-in day.
    """
    if not AI_AVAILABLE or not AI_ENABLED:
        return

    log.info("▶ JOB: AI weekly feedback check")

    clients = get_clients()
    sent    = 0

    for client in clients:
        try:
            # Get this week's submission without feedback sent
            res = _db_client().table("progress_weekly") \
                .select("*") \
                .eq("client_id", client["id"]) \
                .eq("ai_feedback_sent", False) \
                .order("week_number", desc=True) \
                .limit(1) \
                .execute()

            if not res.data:
                continue

            this_week = res.data[0]
            week_num  = this_week["week_number"]

            # Get previous week for comparison
            prev_res = _db_client().table("progress_weekly") \
                .select("*") \
                .eq("client_id", client["id"]) \
                .eq("week_number", week_num - 1) \
                .execute()
            prev_week = prev_res.data[0] if prev_res.data else None

            # Get last 7 days of daily logs
            week_ago = (datetime.now() - timedelta(days=7)).date().isoformat()
            daily_res = _db_client().table("progress_daily") \
                .select("*") \
                .eq("client_id", client["id"]) \
                .gte("log_date", week_ago) \
                .order("log_date", desc=True) \
                .execute()
            daily_logs = daily_res.data or []

            log.info(f"  Generating AI weekly feedback for {client['name']} (Week {week_num})")
            message = generate_weekly_feedback(client, this_week, prev_week, daily_logs)

            # Send AI weekly feedback to client's personal group
            dest = _client_dest(client)
            ok = send_whatsapp(dest, message)

            # Mark feedback as sent
            _db_client().table("progress_weekly") \
                .update({"ai_feedback_sent": True}) \
                .eq("id", this_week["id"]) \
                .execute()

            log_event(
                event_type="ai_weekly_feedback",
                description=f"AI weekly feedback sent to {client['name']} (Week {week_num})",
                client_id=client["id"],
                status="sent" if ok else "failed",
            )
            log.info(f"  AI feedback → {client['name']} {'✓' if ok else '✗'}")
            sent += 1
            time.sleep(3)

        except Exception as e:
            log.error(f"  AI weekly feedback error for {client['name']}: {e}")

    if sent:
        log.info(f"  Done — AI feedback sent to {sent} clients")


# ══════════════════════════════════════════════════════════════════════════
#  JOB 8 — AI Daily Auto-Reply (fires when client submits daily form)
# ══════════════════════════════════════════════════════════════════════════
def job_ai_daily_replies():
    """
    Checks for daily logs submitted today without an AI reply.
    Generates a short personalised reply and sends it.
    Runs every 30 minutes throughout the day.
    """
    if not AI_AVAILABLE or not AI_ENABLED:
        return

    log.info("▶ JOB: AI daily reply check")

    today   = datetime.now().date().isoformat()
    clients = get_clients()
    sent    = 0

    for client in clients:
        try:
            res = _db_client().table("progress_daily") \
                .select("*") \
                .eq("client_id", client["id"]) \
                .eq("log_date", today) \
                .eq("ai_reply_sent", False) \
                .execute()

            if not res.data:
                continue

            daily_log = res.data[0]

            log.info(f"  Generating AI daily reply for {client['name']}")
            message = generate_daily_reply(client, daily_log)

            # Send AI daily reply to client's personal group
            dest = _client_dest(client)
            ok = send_whatsapp(dest, message)

            _db_client().table("progress_daily") \
                .update({"ai_reply_sent": True}) \
                .eq("id", daily_log["id"]) \
                .execute()

            # Auto-mark client as active since they submitted a log
            _db_client().table("clients") \
                .update({"last_active": datetime.now().isoformat()}) \
                .eq("id", client["id"]) \
                .execute()

            log_event(
                event_type="ai_daily_reply",
                description=f"AI daily reply sent to {client['name']}",
                client_id=client["id"],
                status="sent" if ok else "failed",
            )
            log.info(f"  AI reply → {client['name']} {'✓' if ok else '✗'}")
            sent += 1
            time.sleep(2)

        except Exception as e:
            log.error(f"  AI daily reply error for {client['name']}: {e}")

    if sent:
        log.info(f"  Done — AI replies sent to {sent} clients")


# ══════════════════════════════════════════════════════════════════════════
#  JOB 9 — Monday AI Coach Briefing
# ══════════════════════════════════════════════════════════════════════════
def job_monday_coach_briefing():
    """
    Runs Monday morning. For every client:
      1. Generates an AI summary
      2. Detects warnings
      3. Saves to ai_coach_summaries table (dashboard reads this)
      4. Sends YOU a WhatsApp briefing of who needs attention
    """
    if not AI_AVAILABLE or not AI_ENABLED:
        log.info("▶ JOB: Monday briefing skipped — ANTHROPIC_API_KEY not set")
        return

    log.info("▶ JOB: Monday AI coach briefing")
    clients   = get_clients()
    summaries = []

    for client in clients:
        try:
            # Get all weekly data
            weekly_res = _db_client().table("progress_weekly") \
                .select("*") \
                .eq("client_id", client["id"]) \
                .order("week_number") \
                .execute()
            weekly_data = weekly_res.data or []

            # Get last 14 days of daily data
            two_weeks_ago = (datetime.now() - timedelta(days=14)).date().isoformat()
            daily_res = _db_client().table("progress_daily") \
                .select("*") \
                .eq("client_id", client["id"]) \
                .gte("log_date", two_weeks_ago) \
                .order("log_date", desc=True) \
                .execute()
            daily_data = daily_res.data or []

            log.info(f"  Generating AI summary for {client['name']}")
            result = generate_coach_summary(client, weekly_data, daily_data)

            # Save to Supabase
            _db_client().table("ai_coach_summaries").upsert({
                "client_id":    client["id"],
                "summary":      result["summary"],
                "status":       result["status"],
                "warnings":     result["warnings"],
                "generated_at": datetime.now().isoformat(),
            }, on_conflict="client_id").execute()

            summaries.append({
                "name":     client["name"],
                "summary":  result["summary"],
                "status":   result["status"],
                "warnings": result["warnings"],
            })
            log.info(f"  Summary saved for {client['name']} [{result['status']}]")
            time.sleep(2)  # rate limit

        except Exception as e:
            log.error(f"  AI summary error for {client['name']}: {e}")

    # Send briefing WhatsApp to the coach
    if summaries and COACH_PHONE:
        briefing = generate_coach_briefing(summaries)
        ok = send_whatsapp(COACH_PHONE, briefing)
        log.info(f"  Coach briefing → {COACH_PHONE} {'✓' if ok else '✗'}")

    log_event(
        event_type="ai_briefing",
        description=f"Monday AI briefing generated for {len(summaries)} clients",
        status="sent",
    )
    log.info(f"  Done — briefing generated for {len(summaries)} clients")


def _db_client():
    """Get Supabase client directly for AI jobs."""
    from data_store import _db
    return _db()


# ══════════════════════════════════════════════════════════════════════════
#  JOB 10 — Auto-onboard new clients added from dashboard
# ══════════════════════════════════════════════════════════════════════════
def job_auto_onboard_new_clients():
    """
    Runs every minute. Checks for clients where onboarding_complete = false
    and fires onboarding for them automatically.
    This means coaches can add clients from the dashboard and onboarding
    fires within 60 seconds — no terminal needed.
    """
    try:
        res = _db_client().table("clients") \
            .select("*") \
            .eq("onboarding_complete", False) \
            .execute()

        pending = res.data or []
        if not pending:
            return

        log.info(f"▶ JOB: Auto-onboard — {len(pending)} client(s) pending")

        for client in pending:
            log.info(f"  Auto-onboarding {client['name']}...")
            try:
                job_onboarding_new_client(client)
                # Mark as onboarded
                _db_client().table("clients") \
                    .update({"onboarding_complete": True}) \
                    .eq("id", client["id"]) \
                    .execute()
                log.info(f"  Onboarding complete for {client['name']} — marked done")
            except Exception as e:
                log.error(f"  Auto-onboard failed for {client['name']}: {e}")

    except Exception as e:
        log.error(f"  Auto-onboard job error: {e}")

# ══════════════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════════════
def _client_dest(client: dict) -> str:
    """
    Get the WhatsApp destination for a client.
    Uses their personal group chatId if set, otherwise falls back to their phone number.
    """
    group_chat_id = client.get("group_chat_id", "").strip()
    if group_chat_id:
        return group_chat_id
    return client["phone"]


def _schedule_dm_followup():
    """Schedule the DM follow-up for exactly 24hrs after group reminder."""
    followup_time = datetime.now() + timedelta(hours=24)
    followup_str  = followup_time.strftime("%H:%M")
    schedule.every().day.at(followup_str).do(_run_once, job_personal_dm_followup)
    log.info(f"  DM follow-up scheduled for tomorrow at {followup_str}")


def _run_once(job_fn):
    """Run a job once and cancel it."""
    job_fn()
    return schedule.CancelJob


# ══════════════════════════════════════════════════════════════════════════
#  SCHEDULE SETUP
# ══════════════════════════════════════════════════════════════════════════
def setup_schedule():
    log.info("Setting up scheduled jobs...")

    def checkin_and_schedule_dm():
        job_group_checkin_reminder()
        _schedule_dm_followup()

    getattr(schedule.every(), CHECKIN_REMINDER_DAY).at(CHECKIN_REMINDER_TIME).do(
        checkin_and_schedule_dm
    )
    log.info(f"  ✓ Check-in reminder: every {CHECKIN_REMINDER_DAY.title()} at {CHECKIN_REMINDER_TIME}")

    getattr(schedule.every(), RESOURCE_DELIVERY_DAY).at(RESOURCE_DELIVERY_TIME).do(
        job_resource_delivery
    )
    log.info(f"  ✓ Resource delivery: every {RESOURCE_DELIVERY_DAY.title()} at {RESOURCE_DELIVERY_TIME}")

    schedule.every().day.at(INACTIVITY_CHECK_TIME).do(job_inactivity_check)
    log.info(f"  ✓ Inactivity check: daily at {INACTIVITY_CHECK_TIME}")

    schedule.every().day.at(ONBOARDING_SEND_TIME).do(job_daily_video_drip)
    log.info(f"  ✓ Daily video drip: every day at {ONBOARDING_SEND_TIME} (Days 1–10)")

    # Auto-onboard new clients added from dashboard (runs every minute)
    schedule.every(1).minutes.do(job_auto_onboard_new_clients)
    log.info("  ✓ Auto-onboard check: every 1 minute (detects new dashboard clients)")

    # AI jobs (only scheduled if ANTHROPIC_API_KEY is set)
    if AI_AVAILABLE and AI_ENABLED:
        # Check for new weekly form submissions every 30 mins (sends client feedback)
        schedule.every(30).minutes.do(job_ai_weekly_feedback)
        log.info("  ✓ AI weekly feedback: every 30 mins (fires when client submits form)")

        # Check for new daily form submissions every 30 mins (sends auto-reply)
        schedule.every(30).minutes.do(job_ai_daily_replies)
        log.info("  ✓ AI daily auto-reply: every 30 mins (fires when client submits log)")

        # Monday morning coach briefing
        schedule.every().monday.at("08:00").do(job_monday_coach_briefing)
        log.info("  ✓ AI coach briefing: every Monday at 08:00")
    else:
        log.info("  ℹ AI jobs disabled — set ANTHROPIC_API_KEY in .env to enable")

    log.info("All jobs scheduled ✓")


# ══════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    log.info("=" * 55)
    log.info("  CoachOS Scheduler starting up")
    log.info("=" * 55)
    setup_schedule()

    log.info("Running — press Ctrl+C to stop\n")
    while True:
        schedule.run_pending()
        time.sleep(30)
