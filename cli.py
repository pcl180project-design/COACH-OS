"""
cli.py — CoachOS Command Line Tool
===================================
Use this to manually trigger jobs, manage clients,
and test your setup without waiting for the scheduler.

Usage:
  python cli.py test          → verify Twilio credentials
  python cli.py status        → show all client statuses
  python cli.py remind        → send check-in reminder now
  python cli.py dms           → send personal DMs to missing clients
  python cli.py resources     → deliver this week's resources now
  python cli.py inactivity    → run inactivity check now
  python cli.py add           → add a new client interactively
  python cli.py checkin <id>  → manually mark a client as checked in
  python cli.py week <n>      → set the current programme week
  python cli.py onboard <id>  → re-send onboarding to a client
  python cli.py drip          → show daily video drip status
"""

import sys
from datetime import datetime
from data_store import (
    get_clients,
    get_settings,
    set_current_week,
    add_client,
    mark_checkin,
    get_client,
    flag_exists,
)
from twilio_client import verify_credentials
from scheduler import (
    job_group_checkin_reminder,
    job_personal_dm_followup,
    job_resource_delivery,
    job_inactivity_check,
    job_onboarding_new_client,
)

BOLD  = "\033[1m"
GREEN = "\033[92m"
AMBER = "\033[93m"
RED   = "\033[91m"
BLUE  = "\033[94m"
DIM   = "\033[2m"
RESET = "\033[0m"


def cmd_test():
    print(f"\n{BOLD}Testing Twilio connection...{RESET}")
    ok = verify_credentials()
    if ok:
        print(f"{GREEN}✓ Credentials valid — ready to send messages!{RESET}\n")
    else:
        print(f"{RED}✗ Credential check failed — check your .env file{RESET}\n")


def cmd_status():
    # FIX: use get_clients() / get_settings() directly (no more load_data dict)
    clients      = get_clients()
    settings     = get_settings()
    current_week = settings.get("current_week", 1)
    now          = datetime.now()

    print(f"\n{BOLD}CoachOS Status — Week {current_week}{RESET}")
    print("─" * 60)
    print(f"{'Name':<20} {'Phone':<16} {'CI':<5} {'Active':<12} {'Pay'}")
    print("─" * 60)

    for c in clients:
        last = datetime.fromisoformat(c.get("last_active", now.isoformat()))
        days = (now - last).days

        # FIX: was c.get("ci_week") — Supabase column is checkin_week
        ci_ok = c.get("checkin_week", 0) >= current_week

        ci_str  = f"{GREEN}✓{RESET}" if ci_ok else f"{RED}✗{RESET}"
        act_col = GREEN if days < 3 else AMBER if days < 5 else RED
        pay_col = GREEN if c.get("pay_status") == "paid" else RED

        print(
            f"{c['name']:<20} "
            f"{c['phone']:<16} "
            f"{ci_str}     "
            f"{act_col}{days}d ago{RESET:<12} "
            f"{pay_col}{c.get('pay_status','?')}{RESET}"
        )

    # FIX: same field key fix for the summary line
    missing = [c for c in clients if c.get("checkin_week", 0) < current_week]
    print("─" * 60)
    print(f"{GREEN}{len(clients)-len(missing)}{RESET} checked in  "
          f"{RED}{len(missing)}{RESET} missing\n")


def cmd_remind():
    print(f"\n{BOLD}Sending group check-in reminder...{RESET}")
    job_group_checkin_reminder()
    print(f"{GREEN}Done!{RESET}\n")


def cmd_dms():
    print(f"\n{BOLD}Sending personal DMs to missing clients...{RESET}")
    job_personal_dm_followup()
    print(f"{GREEN}Done!{RESET}\n")


def cmd_resources():
    print(f"\n{BOLD}Delivering this week's resources...{RESET}")
    job_resource_delivery()
    print(f"{GREEN}Done!{RESET}\n")


def cmd_inactivity():
    print(f"\n{BOLD}Running inactivity check...{RESET}")
    job_inactivity_check()
    print(f"{GREEN}Done!{RESET}\n")


def cmd_add():
    print(f"\n{BOLD}Add New Client{RESET}")
    name  = input("Full name: ").strip()
    phone = input("WhatsApp number (e.g. +447700123456): ").strip()
    week  = int(input("Starting programme week [1]: ").strip() or "1")
    fee   = int(input("Programme fee in £ [497]: ").strip() or "497")

    # FIX: add_client() no longer takes a 'data' dict as first argument
    client = add_client(name, phone, week, fee)
    print(f"{GREEN}✓ {name} added (ID: {client['id']}){RESET}\n")


def cmd_checkin(args):
    if len(args) < 2:
        print(f"{RED}Usage: python cli.py checkin <client_id>{RESET}")
        return
    client_id    = int(args[1])
    settings     = get_settings()
    current_week = settings.get("current_week", 1)

    # FIX: mark_checkin() no longer takes a 'data' dict as first argument
    mark_checkin(client_id, current_week)

    c    = get_client(client_id)
    name = c["name"] if c else f"Client {client_id}"
    print(f"{GREEN}✓ Check-in recorded for {name} (Week {current_week}){RESET}\n")


def cmd_week(args):
    if len(args) < 2:
        print(f"{RED}Usage: python cli.py week <number>{RESET}")
        return
    week = int(args[1])
    # FIX: use set_current_week() directly instead of mutating a local dict
    set_current_week(week)
    print(f"{GREEN}✓ Current programme week set to {week}{RESET}\n")


def cmd_onboard(args):
    """Manually re-trigger onboarding for a client."""
    if len(args) < 2:
        print(f"{RED}Usage: python cli.py onboard <client_id>{RESET}")
        return
    client_id = int(args[1])
    c = get_client(client_id)
    if not c:
        print(f"{RED}Client {client_id} not found{RESET}")
        return
    print(f"\n{BOLD}Sending onboarding to {c['name']}...{RESET}")
    # FIX: job_onboarding_new_client() no longer takes a 'data' dict
    job_onboarding_new_client(c)
    print(f"{GREEN}Done!{RESET}\n")


def cmd_drip():
    """Show today's drip status for all clients in their first 10 days."""
    clients = get_clients()
    now     = datetime.now()
    in_drip = []

    for c in clients:
        # FIX: was c.get("start","") — Supabase column is start_date
        try:
            start = datetime.fromisoformat(c.get("start_date", ""))
            day   = (now.date() - start.date()).days + 1
            if 1 <= day <= 10:
                sent = flag_exists(f"drip_{c['id']}_day_{day}")
                in_drip.append((c, day, sent))
        except Exception:
            continue

    if not in_drip:
        print(f"\n{DIM}No clients currently in their 10-day drip window{RESET}\n")
        return

    print(f"\n{BOLD}Daily Video Drip Status{RESET}")
    print("─" * 50)
    for c, day, sent in in_drip:
        status = f"{GREEN}✓ Sent{RESET}" if sent else f"{AMBER}⏳ Pending{RESET}"
        print(f"  {c['name']:<20} Day {day}/10   {status}")
    print()


COMMANDS = {
    "test":       (cmd_test,       "Verify Twilio credentials"),
    "status":     (cmd_status,     "Show all client statuses"),
    "remind":     (cmd_remind,     "Send group check-in reminder now"),
    "dms":        (cmd_dms,        "Send personal DMs to missing clients"),
    "resources":  (cmd_resources,  "Deliver this week's resources now"),
    "inactivity": (cmd_inactivity, "Run inactivity check now"),
    "add":        (cmd_add,        "Add a new client (triggers onboarding)"),
    "checkin":    (cmd_checkin,    "Mark client check-in: checkin <id>"),
    "week":       (cmd_week,       "Set current week: week <n>"),
    "onboard":    (cmd_onboard,    "Re-send onboarding to client: onboard <id>"),
    "drip":       (cmd_drip,       "Show daily video drip status"),
}


def print_help():
    print(f"\n{BOLD}CoachOS CLI{RESET}")
    print("─" * 40)
    for cmd, (_, desc) in COMMANDS.items():
        print(f"  {BLUE}python cli.py {cmd:<12}{RESET} {desc}")
    print()


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or args[0] == "help":
        print_help()
    elif args[0] in COMMANDS:
        fn, _ = COMMANDS[args[0]]
        if fn.__code__.co_varnames[:1] == ('args',):
            fn(args)
        else:
            fn()
    else:
        print(f"{RED}Unknown command: {args[0]}{RESET}")
        print_help()
