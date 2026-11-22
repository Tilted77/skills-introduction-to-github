"""The review loop: one job at a time, form prefilled, you check it and press Submit yourself."""

from __future__ import annotations

import json
from pathlib import Path

from .config import ROOT, ConfigError, Profile, Settings
from .prefill import open_form, prefill
from .store import Store

PROMPT = "[a] I submitted it   [f] fill this page   [s] skip this job   [l] leave for later   [q] quit  > "
WORKDAY_HINT = ("  Workday: press Apply, sign in or create your account for this employer, and when the form\n"
                "  appears press f here to fill it. Press f again on each new page of the form.")


def load_drafts(row) -> tuple[str, Path | None, list[str]]:
    """(cover letter text, cover letter file, things to check) for a job; empty if it was not tailored."""
    if not row["materials_dir"]:
        return "", None, []
    folder = Path(row["materials_dir"])
    try:
        result = json.loads((folder / "application.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "", None, []
    letter_file = folder / "cover_letter.txt"
    # Read the file, not the JSON: you may have edited the letter by hand.
    letter = letter_file.read_text(encoding="utf-8").strip() if letter_file.exists() else result.get("cover_letter", "")
    return letter, (letter_file if letter_file.exists() else None), result.get("check_before_sending", [])


def fill_and_report(page, profile: Profile, letter: str, letter_file: Path | None) -> None:
    report = prefill(page, profile, letter, letter_file)
    print("  Filled: " + (", ".join(report.filled) or "nothing recognised on this page"))
    if report.needs_you:
        print("  Needs you: " + ", ".join(report.needs_you))
    if report.choice_questions:
        print(f"  Plus {report.choice_questions} checkbox or multiple-choice question(s).")


def run_session(store: Store, settings: Settings, profile: Profile, limit: int | None = None, ask=input) -> None:
    if not profile.resume_pdf or not profile.resume_pdf.exists():
        raise ConfigError("profile.toml: resume_pdf must point to your resume file so it can be attached.")
    queue = store.by_status("drafted") + store.by_status("scored")
    room = settings.daily_limit - store.applied_today()
    if room <= 0:
        print(f"You have reached today's limit of {settings.daily_limit} applications ([apply] daily_limit in config.toml).")
        return
    queue = queue[: min(room, limit) if limit else room]
    if not queue:
        print("Nothing in the queue. Run fetch, score and tailor first.")
        return
    try:
        from playwright.sync_api import Error as BrowserError, sync_playwright
    except ImportError as e:
        raise ConfigError("Playwright is missing: pip install -r requirements.txt && playwright install chromium") from e

    print(f"{len(queue)} applications queued. Nothing is submitted unless you press Submit in the browser.\n")
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch_persistent_context(str(ROOT / "browser-profile"), headless=False, no_viewport=True)
        except BrowserError as e:
            raise ConfigError(f"Could not open a browser window ({str(e).splitlines()[0]}). "
                              "Run `playwright install chromium` once, on a computer with a screen.") from e
        submitted = 0
        for i, row in enumerate(queue, 1):
            letter, letter_file, checks = load_drafts(row)
            print(f"[{i}/{len(queue)}] {row['title']} at {row['company_name']}  ({row['location']})")
            print(f"  Score {row['score']}: {row['verdict']}")
            for check in checks:
                print(f"  Check: {check}")
            if row["materials_dir"]:
                print(f"  Drafts: {row['materials_dir']}/summary.md")
            page = browser.new_page()
            try:
                if row["source"] == "workday":
                    page.goto(row["apply_url"], wait_until="domcontentloaded", timeout=60000)
                    print(WORKDAY_HINT)
                else:
                    open_form(page, row["apply_url"])
                    fill_and_report(page, profile, letter, letter_file)
            except BrowserError as e:
                print(f"  Could not load the form: {str(e).splitlines()[0]}\n  Posting: {row['url']}")
            choice = ""
            while choice not in ("a", "s", "l", "q"):
                choice = ask(f"  {PROMPT}").strip().lower()[:1]
                if choice == "f":
                    try:
                        # The form may have moved on to a new page or tab since the job was opened.
                        fill_and_report(browser.pages[-1], profile, letter, letter_file)
                    except BrowserError as e:
                        print(f"  Could not fill this page: {str(e).splitlines()[0]}")
            if choice == "a":
                store.set_applied(row["id"])
                submitted += 1
            elif choice == "s":
                store.set_skipped(row["id"], "skipped by you")
            try:
                page.close()
            except BrowserError:
                pass
            print()
            if choice == "q":
                break
        browser.close()
    print(f"Marked {submitted} as applied this session. `python -m jobhunt export` writes the tracker spreadsheet.")
