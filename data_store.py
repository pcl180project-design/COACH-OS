"""
data_store.py — Supabase Edition
==================================
All client data is now stored in Supabase.
The dashboard and backend share the same database — fully in sync.

Requires:
  pip install supabase
  SUPABASE_URL and SUPABASE_KEY in your .env file
"""

import os
import logging
from datetime import datetime
from supabase import create_client, Client

log = logging.getLogger("CoachOS.data")

_client: Client = None


def _db() -> Client:
    """Return a cached Supabase client."""
    global _client
    if _client is None:
        url = os.getenv("SUPABASE_URL")
        key = os.getenv("SUPABASE_KEY")
        if not url or not key:
            raise EnvironmentError(
                "SUPABASE_URL and SUPABASE_KEY must be set in your .env file.\n"
                "Find them in: Supabase Dashboard → Settings → API"
            )
        _client = create_client(url, key)
    return _client


# ══════════════════════════════════════════════════════════════════════════
#  SETTINGS
# ══════════════════════════════════════════════════════════════════════════
def get_settings() -> dict:
    res = _db().table("settings").select("*").eq("id", 1).single().execute()
    return res.data or {}


def update_settings(**kwargs) -> None:
    kwargs["updated_at"] = datetime.now().isoformat()
    _db().table("settings").update(kwargs).eq("id", 1).execute()
    log.info(f"Settings updated: {list(kwargs.keys())}")


def get_current_week() -> int:
    return get_settings().get("current_week", 1)


def set_current_week(week: int) -> None:
    update_settings(current_week=week)
    log.info(f"Current week set to {week}")


# ══════════════════════════════════════════════════════════════════════════
#  CLIENTS
# ══════════════════════════════════════════════════════════════════════════
def get_clients() -> list:
    res = _db().table("clients").select("*").order("created_at").execute()
    return res.data or []


def get_client(client_id: int) -> dict | None:
    res = _db().table("clients").select("*").eq("id", client_id).single().execute()
    return res.data


def add_client(name: str, phone: str, week: int = 1, fee: int = 497,
               trigger_onboarding: bool = True) -> dict:
    """Insert a new client and optionally fire onboarding."""
    colors = ['#3dd68c','#f0b429','#5ba4f0','#f26b5b','#a78bfa',
              '#34d399','#fb7185','#38bdf8']
    existing = get_clients()
    color = colors[len(existing) % len(colors)]

    payload = {
        "name":           name,
        "phone":          phone,
        "start_date":     datetime.now().isoformat(),
        "programme_week": week,
        "last_active":    datetime.now().isoformat(),
        "checkin_week":   0,
        "fee":            fee,
        "pay_status":     "due",
        "engagement":     50,
        "avatar_color":   color,
        "onboarding_complete": False,
    }
    res = _db().table("clients").insert(payload).execute()
    client = res.data[0]

    # Seed default milestones
    milestones = [
        {"client_id": client["id"], "label": "Completed Week 1",          "sort_order": 0},
        {"client_id": client["id"], "label": "First check-in submitted",   "sort_order": 1},
        {"client_id": client["id"], "label": "Reached halfway point",      "sort_order": 2},
        {"client_id": client["id"], "label": "Completed full programme",   "sort_order": 3},
        {"client_id": client["id"], "label": "Booked follow-on coaching",  "sort_order": 4},
    ]
    _db().table("client_milestones").insert(milestones).execute()

    # Seed payment record
    _db().table("payments").insert({
        "client_id":    client["id"],
        "amount":       fee,
        "status":       "due",
        "payment_date": datetime.now().date().isoformat(),
        "notes":        "Initial programme fee",
    }).execute()

    log.info(f"Client added: {name} (ID: {client['id']})")

    if trigger_onboarding:
        try:
            from scheduler import job_onboarding_new_client
            job_onboarding_new_client(client)
            _db().table("clients").update({"onboarding_complete": True}).eq("id", client["id"]).execute()
        except Exception as e:
            log.error(f"Onboarding failed for {name}: {e}")

    return client


def update_client(client_id: int, **kwargs) -> None:
    _db().table("clients").update(kwargs).eq("id", client_id).execute()


def mark_client_active(client_id: int) -> None:
    update_client(client_id, last_active=datetime.now().isoformat())
    log.info(f"Client {client_id} marked active")


def mark_checkin(client_id: int, week: int) -> None:
    update_client(client_id,
                  checkin_week=week,
                  last_active=datetime.now().isoformat())
    log.info(f"Check-in recorded — client {client_id}, week {week}")


def remove_client(client_id: int) -> None:
    _db().table("clients").delete().eq("id", client_id).execute()
    log.info(f"Client {client_id} removed")


# ══════════════════════════════════════════════════════════════════════════
#  NOTES & MILESTONES
# ══════════════════════════════════════════════════════════════════════════
def get_notes(client_id: int) -> list:
    res = _db().table("client_notes").select("*").eq("client_id", client_id).order("created_at", desc=True).execute()
    return res.data or []


def add_note(client_id: int, text: str, tag: str = "motivation") -> dict:
    res = _db().table("client_notes").insert({
        "client_id": client_id,
        "note_date":  datetime.now().date().isoformat(),
        "tag":        tag,
        "note_text":  text,
    }).execute()
    return res.data[0]


def get_milestones(client_id: int) -> list:
    res = _db().table("client_milestones").select("*").eq("client_id", client_id).order("sort_order").execute()
    return res.data or []


def toggle_milestone(milestone_id: int, done: bool) -> None:
    _db().table("client_milestones").update({"done": done}).eq("id", milestone_id).execute()


# ══════════════════════════════════════════════════════════════════════════
#  RESOURCES
# ══════════════════════════════════════════════════════════════════════════
def get_resources(week: int = None, unsent_only: bool = False) -> list:
    q = _db().table("resources").select("*")
    if week:
        q = q.eq("week_number", week)
    if unsent_only:
        q = q.eq("sent", False)
    return q.order("week_number").execute().data or []


def add_resource(week: int, name: str, rtype: str, content: str) -> dict:
    res = _db().table("resources").insert({
        "week_number": week, "name": name, "type": rtype, "content": content,
    }).execute()
    return res.data[0]


def mark_resource_sent(resource_id: int) -> None:
    _db().table("resources").update({"sent": True}).eq("id", resource_id).execute()


# ══════════════════════════════════════════════════════════════════════════
#  PAYMENTS
# ══════════════════════════════════════════════════════════════════════════
def get_payments() -> list:
    res = _db().table("payments").select("*, clients(name, avatar_color)").order("created_at", desc=True).execute()
    return res.data or []


def add_payment(client_id: int, amount: int, status: str = "due",
                notes: str = "") -> dict:
    res = _db().table("payments").insert({
        "client_id":    client_id,
        "amount":       amount,
        "status":       status,
        "payment_date": datetime.now().date().isoformat(),
        "notes":        notes,
    }).execute()
    return res.data[0]


def mark_payment_paid(payment_id: int, client_id: int) -> None:
    _db().table("payments").update({"status": "paid"}).eq("id", payment_id).execute()
    _db().table("clients").update({"pay_status": "paid"}).eq("id", client_id).execute()
    log.info(f"Payment {payment_id} marked paid")


# ══════════════════════════════════════════════════════════════════════════
#  MESSAGES
# ══════════════════════════════════════════════════════════════════════════
def get_messages(client_id: int = None) -> list:
    q = _db().table("messages").select("*").order("sent_at", desc=True)
    if client_id:
        q = q.eq("client_id", client_id)
    return q.execute().data or []


def add_message(body: str, direction: str = "out",
                client_id: int = None, status: str = "delivered") -> dict:
    res = _db().table("messages").insert({
        "client_id": client_id,
        "direction": direction,
        "body":      body,
        "status":    status,
        "sent_at":   datetime.now().isoformat(),
    }).execute()
    return res.data[0]


# ══════════════════════════════════════════════════════════════════════════
#  ACTIVITY LOG
# ══════════════════════════════════════════════════════════════════════════
def log_event(event_type: str, description: str,
              client_id: int = None, status: str = "sent") -> None:
    _db().table("activity_log").insert({
        "event_type":  event_type,
        "client_id":   client_id,
        "description": description,
        "status":      status,
        "created_at":  datetime.now().isoformat(),
    }).execute()


def get_activity_log(limit: int = 100) -> list:
    res = _db().table("activity_log").select("*").order("created_at", desc=True).limit(limit).execute()
    return res.data or []


# ══════════════════════════════════════════════════════════════════════════
#  REMINDER FLAGS
# ══════════════════════════════════════════════════════════════════════════
def flag_exists(key: str) -> bool:
    res = _db().table("reminder_flags").select("id").eq("flag_key", key).execute()
    return len(res.data) > 0


def set_flag(key: str) -> None:
    try:
        _db().table("reminder_flags").insert({"flag_key": key}).execute()
    except Exception:
        pass  # already exists — that's fine


# ══════════════════════════════════════════════════════════════════════════
#  DRIP VIDEOS
# ══════════════════════════════════════════════════════════════════════════
def get_drip_videos() -> list:
    res = _db().table("drip_videos").select("*").order("day_number").execute()
    return res.data or []


def update_drip_video(day_number: int, video_url: str) -> None:
    _db().table("drip_videos").update({"video_url": video_url}).eq("day_number", day_number).execute()


# ══════════════════════════════════════════════════════════════════════════
#  PROGRAMME WEEKS
# ══════════════════════════════════════════════════════════════════════════
def get_programme_weeks() -> list:
    res = _db().table("programme_weeks").select("*").order("week_number").execute()
    return res.data or []


def add_programme_week(week_number: int, title: str,
                       description: str = "", items: list = None) -> dict:
    res = _db().table("programme_weeks").insert({
        "week_number": week_number,
        "title":       title,
        "description": description,
        "items":       items or [],
    }).execute()
    return res.data[0]


# ══════════════════════════════════════════════════════════════════════════
#  LEGACY SHIMS (so old scheduler code keeps working during transition)
# ══════════════════════════════════════════════════════════════════════════
def load_data() -> dict:
    """Legacy shim — returns a dict that mimics the old JSON structure."""
    return {
        "current_week":   get_current_week(),
        "clients":        get_clients(),
        "resources":      get_resources(),
        "activity_log":   get_activity_log(),
        "reminder_flags": {r["flag_key"]: True for r in
                           _db().table("reminder_flags").select("flag_key").execute().data or []},
        "settings":       get_settings(),
        "onboarding":     {
            "intro_video_url":       get_settings().get("intro_video_url", ""),
            "programme_overview_url": get_settings().get("programme_overview_url", ""),
        },
    }


def save_data(data: dict) -> None:
    """Legacy shim — pushes dict changes back to Supabase."""
    if "current_week" in data:
        update_settings(current_week=data["current_week"])
    log.debug("save_data() called (Supabase edition — individual updates preferred)")
