#!/usr/bin/env python3
"""
Slack stand-up rota bot

What it does:
- Rotates through a list of people in order
- Chooses a facilitator and a backup each weekday
- Skips anyone marked as off for the day
- Posts the assignment into a Slack channel
- Persists rotation state in a local JSON file

Requirements:
    pip install slack-bolt slack-sdk apscheduler python-dotenv

Environment variables:
    SLACK_BOT_TOKEN=...
    SLACK_SIGNING_SECRET=...
    STANDUP_CHANNEL_ID=C0123456789
    STANDUP_POST_HOUR=9
    STANDUP_POST_MINUTE=0
    TZ=Europe/London

Important:
- For reliable Slack mentions, use Slack USER IDs, not display names.
- Replace the placeholder IDs below with your real Slack user IDs.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv
from slack_bolt import App
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError


# ----------------------------
# Configuration
# ----------------------------

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

STATE_FILE = Path("standup_state.json")
ABSENCES_FILE = Path("standup_absences.json")

SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_SIGNING_SECRET = os.environ["SLACK_SIGNING_SECRET"]
STANDUP_CHANNEL_ID = os.environ["STANDUP_CHANNEL_ID"]
TZ = os.getenv("TZ", "Europe/London")
STANDUP_POST_HOUR = int(os.getenv("STANDUP_POST_HOUR", "9"))
STANDUP_POST_MINUTE = int(os.getenv("STANDUP_POST_MINUTE", "0"))

# Replace these USER IDs with the real Slack IDs from your workspace.
# You can find them by clicking a profile in Slack or via the users.list API.
ROTATION = [
    {"name": "Luke MacCalman", "user_id": "UNMJTM2A2"},
    {"name": "Jonathan Wheatley", "user_id": "U03DN6X3UBG"},
    {"name": "Mohit Kataria", "user_id": "U0689SR9UR4"},
    {"name": "Aleksei Agafonov", "user_id": "U085RSM7JR4"},
    {"name": "Hugh Mc Guinness", "user_id": "U07UEJR2DPH"},
    {"name": "Rajani Desu", "user_id": "U02EERU3VF1"},
    {"name": "Tom Hewitt", "user_id": "ULPFN8NEB"},
    {"name": "Kieran White", "user_id": "U05012NM35W"},
    {"name": "Michael Gittens", "user_id": "U6T3BQ5PH"},
]


# ----------------------------
# Models / helpers
# ----------------------------

@dataclass(frozen=True)
class Person:
    name: str
    user_id: str

    @property
    def mention(self) -> str:
        return f"<@{self.user_id}>"


PEOPLE: List[Person] = [Person(**p) for p in ROTATION]


def today_iso() -> str:
    return date.today().isoformat()


def load_json(path: Path, default):
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, payload) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)


def ensure_state() -> Dict:
    state = load_json(
        STATE_FILE,
        {
            "next_index": 0,
            "last_run_date": None,
        },
    )
    if "next_index" not in state:
        state["next_index"] = 0
    if "last_run_date" not in state:
        state["last_run_date"] = None
    return state


def ensure_absences() -> Dict[str, List[str]]:
    """
    Example format:
    {
      "2026-04-17": ["U01LUKE0001", "U01TOMH0007"],
      "2026-04-18": ["U01JONA0002"]
    }
    """
    return load_json(ABSENCES_FILE, {})


def get_off_user_ids_for_day(day_iso: str) -> Set[str]:
    absences = ensure_absences()
    return set(absences.get(day_iso, []))


def add_absence(day_iso: str, user_id: str) -> None:
    absences = ensure_absences()
    existing = set(absences.get(day_iso, []))
    existing.add(user_id)
    absences[day_iso] = sorted(existing)
    save_json(ABSENCES_FILE, absences)


def remove_absence(day_iso: str, user_id: str) -> None:
    absences = ensure_absences()
    existing = set(absences.get(day_iso, []))
    existing.discard(user_id)
    if existing:
        absences[day_iso] = sorted(existing)
    else:
        absences.pop(day_iso, None)
    save_json(ABSENCES_FILE, absences)


def list_absences(day_iso: str) -> List[str]:
    absences = ensure_absences()
    return absences.get(day_iso, [])


def find_user_by_id(user_id: str) -> Optional[Person]:
    for person in PEOPLE:
        if person.user_id == user_id:
            return person
    return None


def find_user_by_mention_or_id(raw: str) -> Optional[Person]:
    """
    Accepts:
    - <@U12345>
    - U12345
    """
    cleaned = raw.strip()
    if cleaned.startswith("<@") and cleaned.endswith(">"):
        cleaned = cleaned[2:-1]
    return find_user_by_id(cleaned)


def pick_facilitator_and_backup(
    people: List[Person],
    start_index: int,
    off_user_ids: Set[str],
) -> Tuple[Person, Person, int]:
    """
    Returns:
        facilitator, backup, next_index_after_assignment
    """
    available = [p for p in people if p.user_id not in off_user_ids]

    if len(available) < 2:
        raise ValueError("Need at least two available people to assign facilitator and backup.")

    facilitator = None
    backup = None
    facilitator_idx = None

    n = len(people)

    # Find facilitator from current rotation position.
    for offset in range(n):
        idx = (start_index + offset) % n
        candidate = people[idx]
        if candidate.user_id not in off_user_ids:
            facilitator = candidate
            facilitator_idx = idx
            break

    if facilitator is None or facilitator_idx is None:
        raise ValueError("Could not find an available facilitator.")

    # Find next available person after facilitator for backup.
    for offset in range(1, n + 1):
        idx = (facilitator_idx + offset) % n
        candidate = people[idx]
        if candidate.user_id not in off_user_ids and candidate.user_id != facilitator.user_id:
            backup = candidate
            break

    if backup is None:
        raise ValueError("Could not find an available backup.")

    next_index = (facilitator_idx + 1) % n
    return facilitator, backup, next_index


def build_message(facilitator: Person, backup: Person, off_people: List[Person]) -> str:
    off_line = ""
    if off_people:
        off_mentions = ", ".join(p.mention for p in off_people)
        off_line = f"\nPeople marked off today: {off_mentions}"

    return (
        f"Good morning team 👋\n\n"
        f"*Today's stand-up rota*\n"
        f"• Facilitator: {facilitator.mention}\n"
        f"• Backup: {backup.mention}\n"
        f"{off_line}\n\n"
        f"Stand-up format:\n"
        f"• Yesterday\n"
        f"• Today\n"
        f"• Blockers"
    )


# ----------------------------
# Slack app
# ----------------------------

app = App(token=SLACK_BOT_TOKEN, signing_secret=SLACK_SIGNING_SECRET)
client: WebClient = app.client


def post_daily_assignment() -> None:
    state = ensure_state()
    run_date = today_iso()

    if state.get("last_run_date") == run_date:
        logger.info("Daily rota already posted for %s; skipping.", run_date)
        return

    off_user_ids = get_off_user_ids_for_day(run_date)
    off_people = [p for p in PEOPLE if p.user_id in off_user_ids]

    facilitator, backup, next_index = pick_facilitator_and_backup(
        people=PEOPLE,
        start_index=state["next_index"],
        off_user_ids=off_user_ids,
    )

    message = build_message(facilitator, backup, off_people)

    try:
        client.chat_postMessage(
            channel=STANDUP_CHANNEL_ID,
            text=message,
        )
        logger.info(
            "Posted stand-up rota: facilitator=%s backup=%s",
            facilitator.name,
            backup.name,
        )
    except SlackApiError as exc:
        logger.exception("Failed to post stand-up rota: %s", exc.response.get("error"))
        raise

    state["next_index"] = next_index
    state["last_run_date"] = run_date
    save_json(STATE_FILE, state)


@app.command("/standup-run")
def handle_run_command(ack, respond):
    ack()
    try:
        post_daily_assignment()
        respond("Stand-up rota posted.")
    except Exception as exc:
        logger.exception("Manual run failed.")
        respond(f"Failed to post stand-up rota: {exc}")


@app.command("/standup-who")
def handle_who_command(ack, respond):
    ack()
    state = ensure_state()
    off_user_ids = get_off_user_ids_for_day(today_iso())

    try:
        facilitator, backup, _ = pick_facilitator_and_backup(
            people=PEOPLE,
            start_index=state["next_index"],
            off_user_ids=off_user_ids,
        )
        respond(
            f"Next stand-up assignment:\n"
            f"• Facilitator: {facilitator.mention}\n"
            f"• Backup: {backup.mention}"
        )
    except Exception as exc:
        respond(f"Could not calculate stand-up rota: {exc}")


@app.command("/standup-off")
def handle_off_command(ack, command, respond):
    """
    Usage:
      /standup-off @person
      /standup-off @person 2026-04-17
    """
    ack()
    text = (command.get("text") or "").strip()
    parts = text.split()

    if not parts:
        respond("Usage: /standup-off @person [YYYY-MM-DD]")
        return

    user = find_user_by_mention_or_id(parts[0])
    if not user:
        respond("I could not match that person. Use a Slack mention like @person.")
        return

    day_iso = parts[1] if len(parts) > 1 else today_iso()
    add_absence(day_iso, user.user_id)
    respond(f"Marked {user.mention} as off on {day_iso}.")


@app.command("/standup-on")
def handle_on_command(ack, command, respond):
    """
    Usage:
      /standup-on @person
      /standup-on @person 2026-04-17
    """
    ack()
    text = (command.get("text") or "").strip()
    parts = text.split()

    if not parts:
        respond("Usage: /standup-on @person [YYYY-MM-DD]")
        return

    user = find_user_by_mention_or_id(parts[0])
    if not user:
        respond("I could not match that person. Use a Slack mention like @person.")
        return

    day_iso = parts[1] if len(parts) > 1 else today_iso()
    remove_absence(day_iso, user.user_id)
    respond(f"Removed off marker for {user.mention} on {day_iso}.")


@app.command("/standup-off-list")
def handle_off_list_command(ack, command, respond):
    """
    Usage:
      /standup-off-list
      /standup-off-list 2026-04-17
    """
    ack()
    text = (command.get("text") or "").strip()
    day_iso = text if text else today_iso()

    user_ids = list_absences(day_iso)
    if not user_ids:
        respond(f"No one is marked off on {day_iso}.")
        return

    mentions = []
    for user_id in user_ids:
        person = find_user_by_id(user_id)
        mentions.append(person.mention if person else user_id)

    respond(f"People marked off on {day_iso}: {', '.join(mentions)}")


# ----------------------------
# Scheduler
# ----------------------------

def main() -> None:
    scheduler = BlockingScheduler(timezone=TZ)

    # Monday to Friday
    scheduler.add_job(
        post_daily_assignment,
        CronTrigger(
            day_of_week="mon-fri",
            hour=STANDUP_POST_HOUR,
            minute=STANDUP_POST_MINUTE,
            timezone=TZ,
        ),
        id="daily-standup-rota",
        replace_existing=True,
    )

    logger.info(
        "Stand-up rota bot started. Posts Mon-Fri at %02d:%02d %s",
        STANDUP_POST_HOUR,
        STANDUP_POST_MINUTE,
        TZ,
    )

    # Start Slack command listener in a simple way.
    # For production, run behind a proper web server if preferred.
    from threading import Thread

    def run_slack_app():
        app.start(port=int(os.getenv("PORT", "3000")))

    slack_thread = Thread(target=run_slack_app, daemon=True)
    slack_thread.start()

    scheduler.start()


if __name__ == "__main__":
    main()