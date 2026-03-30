"""
ai_coach.py — CoachOS AI Layer
================================
Uses Claude (Anthropic) to generate personalised coaching responses
based on client progress data.

Features:
  1. Weekly feedback message — sent to client after Sunday form submission
  2. Daily auto-reply — sent to client after daily form submission
  3. Coach summary — one-liner per client for your morning briefing
  4. Warning detection — flags clients who need your personal attention

Requires:
  pip install anthropic
  ANTHROPIC_API_KEY in your .env file
  Get your key at: https://console.anthropic.com
"""

import os
import logging
from datetime import datetime, date, timedelta

log = logging.getLogger("CoachOS.ai")

# ── Lazy import so system still works if anthropic not installed ──────────
def _get_anthropic():
    try:
        import anthropic
        key = os.getenv("ANTHROPIC_API_KEY", "")
        if not key:
            raise EnvironmentError(
                "ANTHROPIC_API_KEY not set in .env — "
                "get your key at https://console.anthropic.com"
            )
        return anthropic.Anthropic(api_key=key)
    except ImportError:
        raise ImportError(
            "anthropic package not installed. Run: pip install anthropic"
        )


def _call_claude(prompt: str, max_tokens: int = 600) -> str:
    """Make a single call to Claude and return the text response."""
    client = _get_anthropic()
    msg = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()


# ══════════════════════════════════════════════════════════════════════════
#  1. WEEKLY FEEDBACK — sent to client after Sunday form submission
# ══════════════════════════════════════════════════════════════════════════
def generate_weekly_feedback(
    client: dict,
    this_week: dict,
    prev_week: dict | None,
    daily_logs: list,
) -> str:
    """
    Generate a personalised WhatsApp message for a client based on
    their weekly progress data.

    Args:
        client:     Client record from Supabase
        this_week:  This week's progress_weekly row
        prev_week:  Last week's progress_weekly row (or None if first week)
        daily_logs: Last 7 days of progress_daily rows
    """
    first      = client["name"].split()[0]
    week_num   = this_week.get("week_number", 1)

    # Build context string for Claude
    changes = []
    if prev_week:
        for field, label in [
            ("avg_weight_kg", "weight"),
            ("waist", "waist"),
            ("hip", "hip"),
            ("chest", "chest"),
        ]:
            curr = this_week.get(field)
            prev = prev_week.get(field)
            if curr and prev:
                diff = round(curr - prev, 1)
                direction = "down" if diff < 0 else "up"
                changes.append(f"{label} {direction} {abs(diff)} {'kg' if field == 'avg_weight_kg' else 'cm'}")

    compliance_days = sum(1 for d in daily_logs if d.get("compliant_to_diet") is True)
    workout_days    = sum(1 for d in daily_logs if d.get("workout_done") is True)
    avg_stress      = round(
        sum(d["stress_level"] for d in daily_logs if d.get("stress_level")) /
        max(1, sum(1 for d in daily_logs if d.get("stress_level"))), 1
    ) if daily_logs else None
    avg_steps = round(
        sum(d["steps"] for d in daily_logs if d.get("steps")) /
        max(1, sum(1 for d in daily_logs if d.get("steps")))
    ) if daily_logs else None

    remarks         = this_week.get("remarks", "")
    improvements    = next((d.get("improvements_felt") for d in daily_logs if d.get("improvements_felt")), "")
    things_improve  = next((d.get("things_to_improve") for d in daily_logs if d.get("things_to_improve")), "")

    prompt = f"""You are a warm, encouraging, professional health and wellness coach.
Write a personalised WhatsApp message for your client {first} based on their Week {week_num} progress data.

THEIR DATA THIS WEEK:
- Body changes vs last week: {', '.join(changes) if changes else 'First week — no comparison yet'}
- Current weight: {this_week.get('avg_weight_kg', 'not recorded')} kg
- Current waist: {this_week.get('waist', 'not recorded')} cm
- Diet compliance: {compliance_days} out of {len(daily_logs)} days logged
- Workout days completed: {workout_days}
- Average stress level: {avg_stress}/5 (1=low, 5=very high)
- Average daily steps: {avg_steps if avg_steps else 'not recorded'}
- Improvements they noted: {improvements or 'none noted'}
- Things they want to improve: {things_improve or 'none noted'}
- Coach remarks: {remarks or 'none'}

INSTRUCTIONS:
1. Start with their name and something warm
2. Celebrate their biggest win this week (be specific to their numbers)
3. If there is a concern (weight going up 2+ weeks, stress above 4, compliance below 50%), acknowledge it gently and supportively — never make them feel bad
4. Give ONE specific, practical, actionable tip for next week based on their data
5. End with genuine encouragement
6. Keep it under 200 words
7. Use WhatsApp formatting (*bold* for emphasis)
8. Do NOT use generic phrases like "keep up the great work" — be specific to their actual data
9. Sound like a real coach texting them, not a formal report
10. Do NOT add a subject line or "Message:" prefix — just the message itself"""

    try:
        return _call_claude(prompt, max_tokens=400)
    except Exception as e:
        log.error(f"AI weekly feedback failed for {client['name']}: {e}")
        return _fallback_weekly_message(first, week_num, compliance_days, len(daily_logs))


def _fallback_weekly_message(first, week, compliant, total):
    """Simple fallback if AI call fails."""
    return (
        f"Hi {first}! 💚\n\n"
        f"Your Week {week} check-in has been received — thank you for submitting!\n\n"
        f"I'll review your data and get back to you shortly with personalised feedback.\n\n"
        f"Keep going — you're doing amazing! 🌟\n— Your Coach"
    )


# ══════════════════════════════════════════════════════════════════════════
#  2. DAILY AUTO-REPLY — sent to client after daily form submission
# ══════════════════════════════════════════════════════════════════════════
def generate_daily_reply(client: dict, daily_log: dict) -> str:
    """
    Generate a short, warm auto-reply for a client's daily log submission.
    Keeps it brief — this fires every day so it can't be long.
    """
    first        = client["name"].split()[0]
    compliant    = daily_log.get("compliant_to_diet")
    workout      = daily_log.get("workout_done")
    stress       = daily_log.get("stress_level")
    challenges   = daily_log.get("challenges_today", "")
    remarks      = daily_log.get("personal_remarks", "")
    weight       = daily_log.get("weight_kg")
    steps        = daily_log.get("steps")

    prompt = f"""You are a warm, supportive health coach. Write a very short WhatsApp reply 
(2-4 sentences MAX) to acknowledge {first}'s daily log.

THEIR LOG TODAY:
- Diet compliant: {compliant}
- Workout done: {workout}
- Stress level: {stress}/5
- Steps: {steps if steps else 'not recorded'}
- Weight: {weight if weight else 'not recorded'} kg
- Challenges today: {challenges or 'none mentioned'}
- Personal remarks: {remarks or 'none'}

RULES:
- Maximum 4 sentences, ideally 2-3
- Be specific to ONE thing from their data — don't be generic
- If stress is 4 or 5, briefly acknowledge that and offer a word of support
- If they struggled with diet, be understanding not judgemental
- If they had a great day, be genuinely enthusiastic
- End with one tiny encouragement for tomorrow
- Sound like a real person texting, not a bot
- Do NOT start with "Hi {first}" — vary the opening
- No subject line, no "Message:" prefix"""

    try:
        return _call_claude(prompt, max_tokens=150)
    except Exception as e:
        log.error(f"AI daily reply failed for {client['name']}: {e}")
        return _fallback_daily_reply(first, compliant, workout, stress)


def _fallback_daily_reply(first, compliant, workout, stress):
    if stress and stress >= 4:
        return f"Logged! 📋 Tough day but you still showed up — that counts for everything, {first}. Take it easy tonight 💚"
    if compliant and workout:
        return f"Nailed it today {first}! 🔥 Diet on point and workout done — this is exactly how results happen. Keep it going tomorrow!"
    return f"Daily log received {first}! 📋 Every day you show up is a step forward. See you tomorrow 💚"


# ══════════════════════════════════════════════════════════════════════════
#  3. COACH SUMMARY — one-liner per client for your morning dashboard
# ══════════════════════════════════════════════════════════════════════════
def generate_coach_summary(
    client: dict,
    weekly_data: list,
    daily_data: list,
) -> dict:
    """
    Generate a coach-facing summary for one client.
    Returns a dict with: summary (str), status (green/amber/red), warnings (list)
    """
    first    = client["name"].split()[0]
    warnings = detect_warnings(client, weekly_data, daily_data)

    if not weekly_data and not daily_data:
        return {
            "summary":  f"{first} — no data yet",
            "status":   "grey",
            "warnings": [],
        }

    latest_weekly = weekly_data[-1] if weekly_data else {}
    prev_weekly   = weekly_data[-2] if len(weekly_data) >= 2 else None
    recent_daily  = daily_data[:7]

    compliance_pct = round(
        sum(1 for d in recent_daily if d.get("compliant_to_diet") is True) /
        max(1, len(recent_daily)) * 100
    ) if recent_daily else None

    weight_change = None
    if latest_weekly and prev_weekly and latest_weekly.get("avg_weight_kg") and prev_weekly.get("avg_weight_kg"):
        weight_change = round(latest_weekly["avg_weight_kg"] - prev_weekly["avg_weight_kg"], 1)

    prompt = f"""You are a coaching assistant. Write a ONE-LINE summary (max 20 words) 
for the coach about their client {first}.

DATA:
- Latest weight: {latest_weekly.get('avg_weight_kg', 'N/A')} kg
- Weight change vs last week: {('+' if weight_change > 0 else '') + str(weight_change) + ' kg' if weight_change is not None else 'N/A'}
- Waist: {latest_weekly.get('waist', 'N/A')} cm
- Diet compliance this week: {compliance_pct}%
- Warnings flagged: {', '.join(warnings) if warnings else 'none'}
- Days of data logged: {len(recent_daily)}

Write ONE short sentence (max 20 words) that gives the coach the most important thing 
to know about this client right now. Be specific. No fluff. Coach-facing, not client-facing."""

    try:
        summary = _call_claude(prompt, max_tokens=60)
        # Determine status colour
        if warnings:
            status = "red" if len(warnings) >= 2 else "amber"
        elif compliance_pct and compliance_pct >= 80:
            status = "green"
        else:
            status = "amber"

        return {"summary": summary, "status": status, "warnings": warnings}

    except Exception as e:
        log.error(f"AI summary failed for {client['name']}: {e}")
        return {
            "summary":  f"{first} — {len(recent_daily)} days logged, {compliance_pct}% diet compliance",
            "status":   "amber" if warnings else "green",
            "warnings": warnings,
        }


# ══════════════════════════════════════════════════════════════════════════
#  4. WARNING DETECTION — no AI needed, pure logic
# ══════════════════════════════════════════════════════════════════════════
def detect_warnings(
    client: dict,
    weekly_data: list,
    daily_data: list,
) -> list[str]:
    """
    Detect warning signs and return a list of human-readable flag strings.
    Pure logic — no AI call needed for this.
    """
    warnings = []
    recent_daily = daily_data[:7]

    # Weight rising 2 weeks in a row
    if len(weekly_data) >= 3:
        w = weekly_data
        if (w[-1].get("avg_weight_kg") and w[-2].get("avg_weight_kg") and
                w[-3].get("avg_weight_kg")):
            if w[-1]["avg_weight_kg"] > w[-2]["avg_weight_kg"] > w[-3]["avg_weight_kg"]:
                rise = round(w[-1]["avg_weight_kg"] - w[-3]["avg_weight_kg"], 1)
                warnings.append(f"Weight rising 2 weeks in a row (+{rise}kg)")

    # Diet compliance below 50% this week
    if recent_daily:
        compliant = sum(1 for d in recent_daily if d.get("compliant_to_diet") is True)
        pct = round(compliant / len(recent_daily) * 100)
        if pct < 50:
            warnings.append(f"Diet compliance low this week ({pct}%)")

    # Average stress above 3.5 this week
    stress_vals = [d["stress_level"] for d in recent_daily if d.get("stress_level")]
    if stress_vals:
        avg_stress = sum(stress_vals) / len(stress_vals)
        if avg_stress >= 3.5:
            warnings.append(f"High average stress this week ({round(avg_stress, 1)}/5)")

    # No daily logs in last 3 days
    if daily_data:
        latest_log = date.fromisoformat(daily_data[0]["log_date"])
        days_since = (date.today() - latest_log).days
        if days_since >= 3:
            warnings.append(f"No daily log for {days_since} days")
    elif client.get("start_date"):
        start = datetime.fromisoformat(client["start_date"]).date()
        if (date.today() - start).days >= 3:
            warnings.append("Never submitted a daily log")

    # Waist measurement going up 2 weeks in a row
    if len(weekly_data) >= 3:
        waists = [w.get("waist") for w in weekly_data[-3:]]
        if all(waists) and waists[2] > waists[1] > waists[0]:
            rise = round(waists[2] - waists[0], 1)
            warnings.append(f"Waist increasing 2 weeks in a row (+{rise}cm)")

    # No workout logged majority of days
    if recent_daily:
        workout_days = sum(1 for d in recent_daily if d.get("workout_done") is True)
        if workout_days < len(recent_daily) // 2:
            warnings.append(f"Low workout consistency ({workout_days}/{len(recent_daily)} days)")

    return warnings


# ══════════════════════════════════════════════════════════════════════════
#  5. MORNING BRIEFING — WhatsApp message to YOU with client alerts
# ══════════════════════════════════════════════════════════════════════════
def generate_coach_briefing(summaries: list[dict]) -> str:
    """
    Generate a WhatsApp message to send to YOU (the coach) on Monday morning
    summarising who needs attention this week.

    summaries: list of dicts with keys: name, summary, status, warnings
    """
    red    = [s for s in summaries if s["status"] == "red"]
    amber  = [s for s in summaries if s["status"] == "amber"]
    green  = [s for s in summaries if s["status"] == "green"]

    lines = [
        f"🌅 *CoachOS Monday Briefing*",
        f"_{date.today().strftime('%A, %d %B %Y')}_",
        f"",
        f"*{len(summaries)} clients — {len(green)} ✅ on track, {len(amber)} ⚠️ watch, {len(red)} 🔴 action needed*",
        "",
    ]

    if red:
        lines.append("🔴 *Needs your attention:*")
        for s in red:
            lines.append(f"• *{s['name']}* — {s['summary']}")
            if s["warnings"]:
                for w in s["warnings"]:
                    lines.append(f"  ↳ {w}")
        lines.append("")

    if amber:
        lines.append("⚠️ *Keep an eye on:*")
        for s in amber:
            lines.append(f"• *{s['name']}* — {s['summary']}")
        lines.append("")

    if green:
        lines.append("✅ *On track:*")
        lines.append(", ".join(s["name"].split()[0] for s in green))

    lines.append("")
    lines.append("_Open your CoachOS dashboard for full details._")

    return "\n".join(lines)
