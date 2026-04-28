"""
scheduler.py — Enhanced with distributed locks, token rotation, and monitoring
This is your COMPLETE scheduler.py with all production enhancements.
Replace your existing scheduler.py with this file.
"""
import textwrap
import os
import time
import logging
import schedule
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

# Import existing modules
from data_store import (
    get_clients, get_client, get_settings, update_settings,
    _db, log_event, flag_exists, set_flag, get_progress_weekly,
    log_message
)

from whatsapp_client import send_whatsapp_with_retry, _client_dest
from ai_coach import generate_weekly_feedback, generate_daily_reply

# NEW IMPORTS - For production features
from redis_client import DistributedLock
from validators import ValidationError

load_dotenv()

# Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
log = logging.getLogger("CoachOS")

# Template cache (existing)
_template_cache = {}


# ============================================================================
# HELPER FUNCTIONS (Your Existing Code - Keep As-Is)
# ============================================================================

def _forms_base_url() -> str:
    """Resolve FORMS_BASE_URL from env, with a safe default."""
    base = os.getenv("FORMS_BASE_URL", "").rstrip("/")
    if not base:
        base = "https://coachos.pages.dev"
    return base


def _sign_form_token(client_id: int, week: int, ttl_days: int = 90) -> str:
    """Build a signed form token of shape `cid:week:expiry:sig16`.

    - `expiry` is a Unix timestamp (seconds) after which the link expires.
    - `sig16` is the first 16 hex chars of HMAC-SHA256(FORMS_SECRET, "cid:week:expiry").

    The forms (daily/weekly/intake/portal) verify this format in-browser
    using the same FORMS_SECRET (emitted into config.js as COACHOS_SECRET).

    If FORMS_SECRET is not set, we log a warning and fall back to an
    unsigned expiring token (the forms still require the expiry to be
    in the future, but the signature check is skipped).
    """
    import hmac
    import hashlib

    secret = os.getenv("FORMS_SECRET", "")
    expiry = int((datetime.now(timezone.utc) + timedelta(days=ttl_days)).timestamp())
    payload = f"{client_id}:{week}:{expiry}"

    if not secret:
        log.warning(
            "FORMS_SECRET not set — form links will be unsigned. "
            "Set FORMS_SECRET in .env and run `python cli.py config` before production use."
        )
        return f"{payload}:unsigned"

    sig = hmac.new(
        secret.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:16]
    return f"{payload}:{sig}"


def _build_form_links(client: dict, week: int) -> tuple[str, str]:
    """Build signed, URL-safe weekly + daily form links for a client."""
    from urllib.parse import quote

    cid = client.get("id", "")
    name = client.get("name", "") or ""
    base = _forms_base_url()
    token = _sign_form_token(cid, week)
    qname = quote(name, safe="")

    weekly_url = (
        f"{base}/weekly-form.html?token={token}"
        f"&client_id={cid}&name={qname}&week={week}"
    )
    daily_url = (
        f"{base}/daily-form.html?token={token}"
        f"&client_id={cid}&name={qname}"
    )
    return weekly_url, daily_url


def _build_intake_link(client: dict) -> str:
    """Build a signed intake form link."""
    from urllib.parse import quote

    cid = client.get("id", "")
    name = client.get("name", "") or ""
    base = _forms_base_url()
    # Intake isn't week-scoped, so use week=0 in the token payload.
    token = _sign_form_token(cid, 0)
    qname = quote(name, safe="")
    return (
        f"{base}/intake-form.html?token={token}"
        f"&client_id={cid}&name={qname}"
    )


def _build_portal_link(client: dict) -> str:
    """Build a signed client portal link."""
    from urllib.parse import quote

    cid = client.get("id", "")
    name = client.get("name", "") or ""
    base = _forms_base_url()
    token = _sign_form_token(cid, 0)
    qname = quote(name, safe="")
    return (
        f"{base}/client-portal.html?token={token}"
        f"&client_id={cid}&name={qname}"
    )


def _load_template(filename: str) -> str:
    """Load message template from file."""
    if filename in _template_cache:
        return _template_cache[filename]
    
    try:
        with open(f"templates/{filename}", "r", encoding="utf-8") as f:
            content = f.read()
            _template_cache[filename] = content
            return content
    except FileNotFoundError:
        log.error(f"Template not found: {filename}")
        return ""


# ============================================================================
# JOB FUNCTIONS (Enhanced with Distributed Locks)
# ============================================================================

def job_group_checkin_reminder():
    """
    Send weekly group check-in reminder.
    Enhanced with distributed locking to prevent duplicate sends.
    """
    with DistributedLock("job:checkin_reminder", timeout=300):
        try:
            log.info("▶ JOB: Group check-in reminder")
            
            settings = get_settings()
            current_week = settings.get("current_week", 1)
            group_chat_id = os.getenv("WHATSAPP_GROUP_CHAT_ID", "")
            
            if not group_chat_id:
                log.warning("  No group chat ID configured")
                return
            
            # Build message
            msg = f"""📊 Weekly Check-In Time!

Week {current_week} check-in is now open.

Please submit your:
✅ Measurements (neck, waist, hip)
✅ Average weight
✅ Progress photos
✅ Weekly reflection

Forms close in 48 hours. Don't miss it! 💪"""
            
            # Send to group
            ok = send_whatsapp_with_retry(group_chat_id, msg)
            
            log_event(
                event_type="group_checkin_reminder",
                description=f"Week {current_week} group reminder",
                status="sent" if ok else "failed"
            )
            
            log.info(f"  Group reminder sent: {'✓' if ok else '✗'}")
            
        except Exception as e:
            log.error(f"  Job failed: {e}")
            raise


def job_personal_dm_followup():
    """
    Send personal DM to clients who haven't checked in.
    Enhanced with distributed locking.
    """
    with DistributedLock("job:personal_followup", timeout=300):
        try:
            log.info("▶ JOB: Personal DM follow-up")
            
            settings = get_settings()
            current_week = settings.get("current_week", 1)
            clients = get_clients()
            
            sent = 0
            for client in clients:
                if not client.get("onboarding_complete"):
                    continue
                
                # Check if already checked in this week
                if client.get("checkin_week", 0) >= current_week:
                    continue
                
                # Send personal follow-up
                first = client["name"].split()[0]
                weekly_url, _ = _build_form_links(client, current_week)
                
                msg = f"""Hi {first}! 👋

I noticed you haven't submitted your Week {current_week} check-in yet.

Please take 5 minutes to fill it out:
{weekly_url}

It really helps me support your progress! 🎯

— Your Coach"""
                
                dest = _client_dest(client)
                if dest is None:
                    log.error(f"  Skipping {client['name']} — bad phone number in DB")
                    continue
                ok = send_whatsapp_with_retry(dest, msg)
                
                if ok:
                    sent += 1
                
                log.info(f"  Follow-up to {client['name']}: {'✓' if ok else '✗'}")
                time.sleep(2)
            
            log.info(f"  Done — {sent} follow-ups sent")
            
        except Exception as e:
            log.error(f"  Job failed: {e}")
            raise


def job_resource_delivery():
    """
    Deliver weekly resources.
    Enhanced with distributed locking.
    """
    with DistributedLock("job:resource_delivery", timeout=300):
        try:
            log.info("▶ JOB: Weekly resource delivery")
            
            settings = get_settings()
            current_week = settings.get("current_week", 1)
            clients = get_clients()
            
            # Load resource for this week
            resource_url = settings.get(f"week_{current_week}_resource", "")
            
            if not resource_url:
                log.info(f"  No resource configured for week {current_week}")
                return
            
            sent = 0
            for client in clients:
                if not client.get("onboarding_complete"):
                    continue
                
                first = client["name"].split()[0]
                msg = f"""Hi {first}! 📚

Here's your Week {current_week} resource:

{resource_url}

Review this to maximize your results this week!

— Your Coach"""
                
                dest = _client_dest(client)
                if dest is None:
                    log.error(f"  Skipping {client['name']} — bad phone number in DB")
                    continue
                ok = send_whatsapp_with_retry(dest, msg)
                
                if ok:
                    sent += 1
                
                log.info(f"  Resource to {client['name']}: {'✓' if ok else '✗'}")
                time.sleep(2)
            
            log.info(f"  Done — {sent} resources delivered")
            
        except Exception as e:
            log.error(f"  Job failed: {e}")
            raise


def job_inactivity_check():
    """
    Check for inactive clients and send nudge.
    Enhanced with distributed locking.
    """
    with DistributedLock("job:inactivity_check", timeout=300):
        try:
            log.info("▶ JOB: Inactivity check")
            
            clients = get_clients()
            cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
            
            nudged = 0
            for client in clients:
                if not client.get("onboarding_complete"):
                    continue
                
                last_active = client.get("last_active")
                if not last_active or last_active < cutoff:
                    first = client["name"].split()[0]
                    
                    msg = f"""Hi {first}! 👋

I haven't heard from you in a while.

How are things going? Any challenges I can help with?

Just reply to let me know you're still on track! 💪

— Your Coach"""
                    
                    dest = _client_dest(client)
                    if dest is None:
                        log.error(f"  Skipping {client['name']} — bad phone number in DB")
                        continue
                    ok = send_whatsapp_with_retry(dest, msg)
                    
                    if ok:
                        nudged += 1
                    
                    log.info(f"  Nudge to {client['name']}: {'✓' if ok else '✗'}")
                    time.sleep(2)
            
            log.info(f"  Done — {nudged} clients nudged")
            
        except Exception as e:
            log.error(f"  Job failed: {e}")
            raise


def job_daily_video_drip():
    """
    Send daily drip video.
    Enhanced with distributed locking.
    """
    with DistributedLock("job:daily_video", timeout=300):
        try:
            log.info("▶ JOB: Daily video drip")
            
            settings = get_settings()
            current_week = settings.get("current_week", 1)
            clients = get_clients()
            
            # Get today's video
            day_of_week = datetime.now().strftime("%A").lower()
            video_url = settings.get(f"week_{current_week}_{day_of_week}_video", "")
            
            if not video_url:
                log.info(f"  No video configured for {day_of_week}")
                return
            
            sent = 0
            for client in clients:
                programme_week = client.get("programme_week", 1)
                
                # Only send if client is on this week
                if programme_week != current_week:
                    continue
                
                if not client.get("onboarding_complete"):
                    continue
                
                first = client["name"].split()[0]
                msg = f"""Hi {first}! 🎥

Here's today's video:

{video_url}

Watch this to stay on track!

— Your Coach"""
                
                dest = _client_dest(client)
                if dest is None:
                    log.error(f"  Skipping {client['name']} — bad phone number in DB")
                    continue
                ok = send_whatsapp_with_retry(dest, msg)
                
                if ok:
                    sent += 1
                
                log.info(f"  Video to {client['name']}: {'✓' if ok else '✗'}")
                time.sleep(2)
            
            log.info(f"  Done — {sent} videos sent")
            
        except Exception as e:
            log.error(f"  Job failed: {e}")
            raise


def job_ai_weekly_feedback():
    """
    Generate and send AI weekly feedback.
    Enhanced with distributed locking.
    """
    with DistributedLock("job:ai_weekly_feedback", timeout=300):
        try:
            log.info("▶ JOB: AI weekly feedback")
            
            # Get submissions without feedback
            result = _db().table("progress_weekly") \
                .select("*") \
                .eq("ai_feedback_sent", False) \
                .execute()
            
            pending = result.data or []
            
            if not pending:
                log.info("  No pending weekly submissions")
                return
            
            sent = 0
            for submission in pending:
                client_id = submission.get("client_id")
                client = get_client(client_id)

                if not client:
                    continue

                # Fetch previous week's row (for delta comparisons)
                this_week_num = submission.get("week_number", 0)
                prev_week = None
                if this_week_num and this_week_num > 1:
                    prev_res = _db().table("progress_weekly") \
                        .select("*") \
                        .eq("client_id", client_id) \
                        .eq("week_number", this_week_num - 1) \
                        .limit(1) \
                        .execute()
                    prev_week = prev_res.data[0] if prev_res.data else None

                # Fetch last 7 days of daily logs for this client
                daily_cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
                daily_res = _db().table("progress_daily") \
                    .select("*") \
                    .eq("client_id", client_id) \
                    .gte("log_date", daily_cutoff) \
                    .order("log_date", desc=True) \
                    .execute()
                daily_logs = daily_res.data or []

                # Generate feedback (correct arg order: client, this_week, prev_week, daily_logs)
                feedback = generate_weekly_feedback(client, submission, prev_week, daily_logs)

                if not feedback:
                    continue

                # Send to client
                dest = _client_dest(client)
                if dest is None:
                    log.error(f"  Skipping {client['name']} — bad phone number in DB")
                    continue
                ok = send_whatsapp_with_retry(dest, feedback)

                if ok:
                    # Mark as sent
                    _db().table("progress_weekly") \
                        .update({"ai_feedback_sent": True}) \
                        .eq("id", submission["id"]) \
                        .execute()
                    sent += 1

                log.info(f"  Feedback to {client['name']}: {'✓' if ok else '✗'}")
                time.sleep(3)
            
            log.info(f"  Done — {sent} feedbacks sent")
            
        except Exception as e:
            log.error(f"  Job failed: {e}")
            raise


def job_ai_daily_replies():
    """
    Generate and send AI daily replies.
    Enhanced with distributed locking.
    """
    with DistributedLock("job:ai_daily_replies", timeout=300):
        try:
            log.info("▶ JOB: AI daily replies")
            
            # Get submissions without replies
            result = _db().table("progress_daily") \
                .select("*") \
                .eq("ai_reply_sent", False) \
                .execute()
            
            pending = result.data or []
            
            if not pending:
                log.info("  No pending daily logs")
                return
            
            sent = 0
            for submission in pending:
                client_id = submission.get("client_id")
                client = get_client(client_id)
                
                if not client:
                    continue
                
                # Generate reply (correct arg order: client, daily_log)
                reply = generate_daily_reply(client, submission)
                
                if not reply:
                    continue
                
                # Send to client
                dest = _client_dest(client)
                if dest is None:
                    log.error(f"  Skipping {client['name']} — bad phone number in DB")
                    continue
                ok = send_whatsapp_with_retry(dest, reply)
                
                if ok:
                    # Mark as sent
                    _db().table("progress_daily") \
                        .update({"ai_reply_sent": True}) \
                        .eq("id", submission["id"]) \
                        .execute()
                    sent += 1
                
                log.info(f"  Reply to {client['name']}: {'✓' if ok else '✗'}")
                time.sleep(3)
            
            log.info(f"  Done — {sent} replies sent")
            
        except Exception as e:
            log.error(f"  Job failed: {e}")
            raise


def job_auto_onboard_new_clients():
    """
    Auto-onboard clients added from dashboard.
    Enhanced with distributed locking.
    """
    with DistributedLock("job:auto_onboard", timeout=300):
        try:
            # Get clients not yet onboarded
            result = _db().table("clients") \
                .select("*") \
                .eq("onboarding_complete", False) \
                .execute()
            
            pending = result.data or []
            
            for client in pending:
                job_onboarding_new_client(client)
                time.sleep(5)
            
        except Exception as e:
            log.error(f"  Auto-onboard failed: {e}")
            raise


# ============================================================================
# ONBOARDING — Full 7-Step Sequence
# ============================================================================

def job_onboarding_new_client(client: dict):
    """Onboard a single new client — full 7-step sequence."""
    try:
        log.info(f"▶ Onboarding: {client['name']}")

        first = client["name"].split()[0]
        dest = _client_dest(client)
        if dest is None:
            log.error(f"  Onboarding skipped for {client['name']} — bad phone number in DB. "
                      f"Fix with: python cli.py edit {client['id']} --phone +COUNTRYCODE...")
            return
        cid = client["id"]
        settings = get_settings()
        current_week = settings.get("current_week", 1)

        # ── Step 1: Welcome message ──────────────────────────────
        if not flag_exists(f"onboard_welcome_{cid}"):
            msg = textwrap.dedent(f"""\
Hi {first}! 🎉 Welcome to the programme!
I'm SO excited to have you here. You've made an incredible decision and I can't wait to support you on this journey 💚

Here's what's coming your way right now:

📄 What to expect — so you know exactly how this works
🎥 My personal introduction video — who I am and why I do this
📘 Your programme overview — the full roadmap ahead

Take your time with each one. This is the start of something really special ✨

Any questions at all — just message me here. I've got you!

— Your Coach""")
            ok = send_whatsapp_with_retry(dest, msg)
            try:
                log_message(cid, "welcome", ok)
            except Exception as e:
                log.error(f"Logging failed: {e}")
            if not ok:
                log.error(f"❌ Welcome message failed for {client['name']} — stopping onboarding")
                return
            set_flag(f"onboard_welcome_{cid}")
            log.info(f"  1/7 Welcome: ✓")
            time.sleep(3)

        # ── Step 2: Intake form ──────────────────────────────────
        if not flag_exists(f"onboard_intake_{cid}"):
            intake_url = _build_intake_link(client)
            msg = (
                f"📋 First, please fill out your intake questionnaire.\n\n"
                f"This helps me understand your background, goals, and any health considerations "
                f"so I can personalise your programme.\n\n"
                f"👉 {intake_url}\n\n"
                f"Take your time — there are no wrong answers! 💚\n"
                f"— Your Coach"
            )
            ok = send_whatsapp_with_retry(dest, msg)
            try:
                log_message(cid, "intake", ok)
            except Exception as e:
                log.error(f"Logging failed: {e}")
            if not ok:
                log.error(f"❌ Intake message failed for {client['name']} — stopping onboarding")
                return
            set_flag(f"onboard_intake_{cid}")
            log.info(f"  2/7 Intake form: ✓")
            time.sleep(3)

        # ── Step 3: What to expect ───────────────────────────────
        if not flag_exists(f"onboard_expect_{cid}"):
            msg = textwrap.dedent(f"""\
📋 *What to Expect*

Here's how the programme works, {first}:

✅ *Daily videos* — every morning for the first 10 days
✅ *Weekly resources* — PDFs, workbooks and prompts
✅ *Weekly check-ins* — share your progress every Sunday
✅ *Personal support* — message me directly any time
✅ *Daily accountability* — quick daily form to track your journey

This is YOUR journey. Go at your own pace — I'm here every step of the way 🙌

— Your Coach""")
            ok = send_whatsapp_with_retry(dest, msg)
            try:
                log_message(cid, "what_to_expect", ok)
            except Exception as e:
                log.error(f"Logging failed: {e}")
            if not ok:
                log.error(f"❌ What-to-expect failed for {client['name']}")
                return
            set_flag(f"onboard_expect_{cid}")
            log.info(f"  3/7 What to expect: ✓")
            time.sleep(3)

        # ── Step 4: Coach intro video ────────────────────────────
        if not flag_exists(f"onboard_video_{cid}"):
            intro_url = settings.get("intro_video_url", "")
            if intro_url and "your-storage.com" not in intro_url:
                msg = (
                    f"🎥 Here's my personal introduction video — "
                    f"I want you to know who I am and why I'm passionate about this work.\n\n"
                    f"👉 {intro_url}\n\n"
                    f"— Your Coach"
                )
                ok = send_whatsapp_with_retry(dest, msg)
                try:
                    log_message(cid, "intro_video", ok)
                except Exception as e:
                    log.error(f"Logging failed: {e}")
                if not ok:
                    log.error(f"❌ Intro video failed for {client['name']}")
                    return
                log.info(f"  4/7 Intro video: ✓")
            else:
                log.info(f"  4/7 Intro video: ⏭ skipped (no URL configured)")
            set_flag(f"onboard_video_{cid}")
            time.sleep(3)

        # ── Step 5: Programme overview PDF ───────────────────────
        if not flag_exists(f"onboard_overview_{cid}"):
            overview_url = settings.get("programme_overview_url", "")
            if overview_url and "your-storage.com" not in overview_url:
                msg = (
                    f"📘 Here's your programme overview — "
                    f"this is your complete roadmap for the weeks ahead.\n\n"
                    f"👉 {overview_url}\n\n"
                    f"Save this for reference. We'll go through it together! 💚\n"
                    f"— Your Coach"
                )
                ok = send_whatsapp_with_retry(dest, msg)
                try:
                    log_message(cid, "programme_overview", ok)
                except Exception as e:
                    log.error(f"Logging failed: {e}")
                if not ok:
                    log.error(f"❌ Programme overview failed for {client['name']}")
                    return
                log.info(f"  5/7 Programme overview: ✓")
            else:
                log.info(f"  5/7 Programme overview: ⏭ skipped (no URL configured)")
            set_flag(f"onboard_overview_{cid}")
            time.sleep(3)

        # ── Step 6: Daily accountability form link ───────────────
        if not flag_exists(f"onboard_daily_{cid}"):
            _, daily_url = _build_form_links(client, current_week)
            msg = (
                f"📝 Here's your daily accountability form.\n\n"
                f"Fill this in every day — it takes less than 2 minutes and "
                f"helps me track your progress and support you better.\n\n"
                f"👉 {daily_url}\n\n"
                f"⭐ *Bookmark this link* — you'll use it every day!\n"
                f"— Your Coach"
            )
            ok = send_whatsapp_with_retry(dest, msg)
            try:
                log_message(cid, "daily_form", ok)
            except Exception as e:
                log.error(f"Logging failed: {e}")
            if not ok:
                log.error(f"❌ Daily form link failed for {client['name']}")
                return
            set_flag(f"onboard_daily_{cid}")
            log.info(f"  6/7 Daily form: ✓")
            time.sleep(3)

        # ── Step 7: Client portal link ───────────────────────────
        if not flag_exists(f"onboard_portal_{cid}"):
            portal_url = _build_portal_link(client)
            msg = (
                f"🏠 And finally — here's your personal client portal!\n\n"
                f"This is where you can see your progress, charts, milestones, "
                f"and resources all in one place.\n\n"
                f"👉 {portal_url}\n\n"
                f"⭐ *Bookmark this too!*\n\n"
                f"You're all set, {first}! Let's do this 💪🔥\n"
                f"— Your Coach"
            )
            ok = send_whatsapp_with_retry(dest, msg)
            try:
                log_message(cid, "portal_link", ok)
            except Exception as e:
                log.error(f"Logging failed: {e}")
            if not ok:
                log.error(f"❌ Portal link failed for {client['name']}")
                return
            set_flag(f"onboard_portal_{cid}")
            log.info(f"  7/7 Client portal: ✓")
            time.sleep(3)

        # ── Mark onboarding complete ─────────────────────────────
        _db().table("clients") \
            .update({"onboarding_complete": True}) \
            .eq("id", cid) \
            .execute()

        log_event(
            event_type="onboarding_complete",
            description=f"Full onboarding completed for {client['name']}",
            client_id=cid,
            status="sent"
        )

        log.info(f"  ✓ {client['name']} fully onboarded (7/7 steps)")

    except Exception as e:
        log.error(f"  Onboarding failed for {client['name']}: {e}")


# ============================================================================
# NEW JOB: Token Rotation (Production Security Feature)
# ============================================================================

def job_rotate_expired_tokens():
    """
    Rotate client access tokens that have expired.
    Runs daily at 3 AM.
    """
    with DistributedLock("job:token_rotation", timeout=300):
        try:
            log.info("▶ JOB: Token rotation")
            
            cutoff = datetime.now(timezone.utc).isoformat()
            
            result = _db().table("clients") \
                .select("id, name, phone, token_expires_at, token_version") \
                .lt("token_expires_at", cutoff) \
                .execute()
            
            expired = result.data or []
            
            if not expired:
                log.info("  No expired tokens to rotate")
                return
            
            rotated = 0
            for client in expired:
                import secrets
                new_token = secrets.token_hex(24)
                new_expiry = (datetime.now(timezone.utc) + timedelta(days=90)).isoformat()
                
                # Update token
                _db().table("clients").update({
                    "client_access_token": new_token,
                    "token_expires_at": new_expiry,
                    "token_version": client.get("token_version", 1) + 1
                }).eq("id", client["id"]).execute()
                
                # Send new form links to client
                first = client["name"].split()[0]
                settings = get_settings()
                current_week = settings.get("current_week", 1)
                
                weekly_url, daily_url = _build_form_links(client, current_week)
                
                msg = (
                    f"Hi {first}! 🔐\n\n"
                    f"Your form links have been refreshed for security. "
                    f"Please bookmark these new links:\n\n"
                    f"📊 Weekly form: {weekly_url}\n"
                    f"📝 Daily form: {daily_url}\n\n"
                    f"Your old links will no longer work.\n"
                    f"— Your Coach"
                )
                
                dest = _client_dest(client)
                if dest is None:
                    log.error(f"  Skipping {client['name']} — bad phone number in DB")
                    continue
                ok = send_whatsapp_with_retry(dest, msg)
                
                log_event(
                    event_type="token_rotation",
                    description=f"Access token rotated for {client['name']}",
                    client_id=client["id"],
                    status="sent" if ok else "failed"
                )
                
                log.info(f"  Token rotated for {client['name']} {'✓' if ok else '✗'}")
                rotated += 1
                time.sleep(2)
            
            log.info(f"  Done — {rotated} tokens rotated")
            
        except Exception as e:
            log.error(f"  Token rotation FAILED: {e}")
            log_event(event_type="token_rotation", description=f"Token rotation failed: {e}", status="failed")
            raise


def job_cleanup_old_flags():
    """Clean up old reminder flags (older than 90 days)."""
    with DistributedLock("job:cleanup_flags", timeout=300):
        try:
            log.info("▶ JOB: Cleanup old flags")
            
            cutoff = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
            
            result = _db().table("reminder_flags") \
                .delete() \
                .lt("created_at", cutoff) \
                .execute()
            
            log.info("  ✓ Old flags cleaned up")
            
        except Exception as e:
            log.error(f"  Cleanup failed: {e}")
            raise


# ============================================================================
# SCHEDULER SETUP
# ============================================================================

def _schedule_weekly(day: str, hhmm: str, job):
    """Attach `job` to `schedule` for a given day-of-week string + HH:MM."""
    d = (day or "").strip().lower()
    attr = getattr(schedule.every(), d, None)
    if attr is None:
        log.warning(f"  ⚠ Unknown weekday '{day}', falling back to sunday")
        attr = schedule.every().sunday
    attr.at(hhmm).do(job)


def setup_schedule():
    """Configure all scheduled jobs. Honours env-var overrides for days/times."""
    log.info("Setting up schedule...")

    # Group check-in reminder (configurable day + time)
    checkin_day  = os.getenv("CHECKIN_REMINDER_DAY",  "sunday")
    checkin_time = os.getenv("CHECKIN_REMINDER_TIME", "09:00")
    _schedule_weekly(checkin_day, checkin_time, job_group_checkin_reminder)
    log.info(f"  ✓ Group check-in: {checkin_day} at {checkin_time}")

    # Personal DM follow-up (fixed — day after check-in is business-defined)
    followup_time = os.getenv("CHECKIN_REMINDER_TIME", "09:00")
    schedule.every().monday.at(followup_time).do(job_personal_dm_followup)
    log.info(f"  ✓ Personal follow-up: monday at {followup_time}")

    # Daily video drip
    drip_time = os.getenv("ONBOARDING_SEND_TIME", "09:00")
    schedule.every().day.at(drip_time).do(job_daily_video_drip)
    log.info(f"  ✓ Daily video: daily at {drip_time}")

    # Inactivity check
    inactivity_time = os.getenv("INACTIVITY_CHECK_TIME", "10:00")
    schedule.every().day.at(inactivity_time).do(job_inactivity_check)
    log.info(f"  ✓ Inactivity check: daily at {inactivity_time}")

    # Resource delivery (configurable day + time)
    resource_day  = os.getenv("RESOURCE_DELIVERY_DAY",  "wednesday")
    resource_time = os.getenv("RESOURCE_DELIVERY_TIME", "09:00")
    _schedule_weekly(resource_day, resource_time, job_resource_delivery)
    log.info(f"  ✓ Resource delivery: {resource_day} at {resource_time}")

    # AI weekly feedback — runs every 30 minutes on the check-in day
    # (catches forms submitted throughout the day)
    for hh in range(9, 22):
        for mm in ("00", "30"):
            _schedule_weekly(checkin_day, f"{hh:02d}:{mm}", job_ai_weekly_feedback)
    log.info(f"  ✓ AI weekly feedback: {checkin_day} every 30min, 09:00–21:30")

    # AI daily replies (every 30 minutes)
    schedule.every().hour.at(":00").do(job_ai_daily_replies)
    schedule.every().hour.at(":30").do(job_ai_daily_replies)
    log.info("  ✓ AI daily replies: every 30min")

    # Auto-onboard (every minute)
    schedule.every(5).minutes.do(job_auto_onboard_new_clients)
    log.info("  ✓ Auto-onboard: every 5 minutes")

    # Token rotation (daily at 3 AM)
    schedule.every().day.at("03:00").do(job_rotate_expired_tokens)
    log.info("  ✓ Token rotation: daily at 03:00")

    # Cleanup old flags (daily at 2 AM)
    schedule.every().day.at("02:00").do(job_cleanup_old_flags)
    log.info("  ✓ Cleanup old flags: daily at 02:00")

    log.info("Schedule configured successfully!")


# ============================================================================
# MAIN LOOP
# ============================================================================

def main():
    """Main scheduler loop."""
    # Optional: Initialize Sentry for error tracking
    if os.getenv("SENTRY_DSN"):
        try:
            import sentry_sdk
            sentry_sdk.init(
                dsn=os.getenv("SENTRY_DSN"),
                environment=os.getenv("ENVIRONMENT", "production"),
                traces_sample_rate=0.1,
            )
            log.info("✓ Sentry initialized")
        except ImportError:
            log.warning("Sentry SDK not installed, skipping error tracking")
    
    log.info("=" * 50)
    log.info("CoachOS Scheduler Starting (Production Mode)")
    log.info("=" * 50)
    
    setup_schedule()
    
    log.info("")
    log.info("Scheduler running. Press Ctrl+C to stop.")
    log.info("=" * 50)
    
    while True:
        try:
            schedule.run_pending()
            time.sleep(1)
        except KeyboardInterrupt:
            log.info("\nScheduler stopped by user")
            break
        except Exception as e:
            log.error(f"Scheduler error: {e}")
            # Continue running even if a job fails
            time.sleep(60)


if __name__ == "__main__":
    main()
