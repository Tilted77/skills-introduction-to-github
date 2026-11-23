"""Run with:  python -m unittest discover tests"""

import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from jobhunt import config, sources
from jobhunt.config import Profile, Search
from jobhunt.llm import Assistant, LLMError, Posting, keyword_score
from jobhunt.prefill import choose_file, choose_option, choose_value, prefill
from jobhunt.store import Store

# Trimmed copies of what each feed really returns.
GREENHOUSE = {"jobs": [{
    "id": 8172510, "title": "Backend Engineer ", "company_name": "Stripe",
    "absolute_url": "https://stripe.com/jobs/search?gh_jid=8172510",
    "location": {"name": "Remote - US"}, "first_published": "2026-09-09T10:50:29-04:00",
    "content": "&lt;h2&gt;Who we are&lt;/h2&gt;\n&lt;ul&gt;&lt;li&gt;Python&amp;nbsp;and SQL&lt;/li&gt;&lt;/ul&gt;",
}]}
LEVER = [{
    "id": "6ed7", "text": "Data Analyst", "categories": {"location": "New York, NY", "team": "Ops"},
    "workplaceType": "hybrid", "createdAt": 1786469891368,
    "hostedUrl": "https://jobs.lever.co/acme/6ed7", "applyUrl": "https://jobs.lever.co/acme/6ed7/apply",
    "descriptionPlain": "About the role", "additionalPlain": "Benefits",
    "lists": [{"text": "What you will do", "content": "<li>Write SQL</li><li>Build dashboards</li>"}],
}]
ASHBY = {"jobs": [
    {"id": "3441", "title": "Software Engineer", "location": "New York, NY (HQ)", "isListed": True,
     "isRemote": True, "workplaceType": "Hybrid", "secondaryLocations": [{"location": "Remote (Canada)"}],
     "publishedAt": "2026-04-07T17:12:35.753+00:00", "jobUrl": "https://jobs.ashbyhq.com/ramp/3441",
     "applyUrl": "https://jobs.ashbyhq.com/ramp/3441/application", "descriptionPlain": "Build things.",
     "compensation": {"compensationTierSummary": "$200K – $250K"}},
    {"id": "hidden", "title": "Unlisted", "isListed": False},
]}


WORKDAY_DETAIL = {
    "jobPostingInfo": {
        "id": "a91b", "title": "Manager, Community Outreach", "location": "Columbus Campus",
        "additionalLocations": ["Newark Campus"], "country": {"descriptor": "United States of America"},
        "jobDescription": "<p>Lead outreach.</p><ul><li>Build partnerships</li></ul>", "startDate": "2026-11-20",
        "jobPostingId": "Manager--Community-Outreach_R141681-2", "remoteType": "Hybrid Remote",
        "externalUrl": "https://osu.wd1.myworkdayjobs.com/OSUCareers/job/Columbus-Campus/Manager--Community-Outreach_R141681-2",
    },
    "hiringOrganization": {"name": "The Ohio State University", "url": ""},
}


def profile(**extra) -> Profile:
    base = dict(first_name="Ada", last_name="Lovelace", email="ada@example.com", phone="+1 555 0100",
                location="London, UK", linkedin="https://linkedin.com/in/ada", current_company="Analytical Engines")
    return Profile(**(base | extra))


def field(label="", **extra) -> dict:
    base = dict(n=0, tag="input", type="text", id="", name="", label=label, near="", placeholder="",
                combobox=False, required=False, empty=True, accept="", options=[])
    return base | extra


class Feeds(unittest.TestCase):
    def test_greenhouse(self):
        job, = sources.parse_greenhouse("stripe", GREENHOUSE)
        self.assertEqual(job.id, "greenhouse:stripe:8172510")
        self.assertEqual(job.title, "Backend Engineer")
        self.assertTrue(job.remote)
        self.assertEqual(job.posted_at, "2026-09-09")
        self.assertEqual(job.apply_url, "https://job-boards.greenhouse.io/embed/job_app?for=stripe&token=8172510")
        self.assertIn("- Python and SQL", job.description)
        self.assertNotIn("<", job.description)

    def test_lever(self):
        job, = sources.parse_lever("acme", LEVER)
        self.assertEqual((job.location, job.remote), ("New York, NY", False))
        self.assertEqual(job.apply_url, "https://jobs.lever.co/acme/6ed7/apply")
        self.assertIn("- Write SQL", job.description)
        self.assertIn("Benefits", job.description)
        self.assertEqual(job.posted_at, "2026-08-11")

    def test_ashby_skips_unlisted_and_reads_all_locations(self):
        job, = sources.parse_ashby("ramp", ASHBY)
        self.assertEqual(job.location, "New York, NY (HQ); Remote (Canada)")
        self.assertIn("Compensation: $200K", job.description)

    def test_ashby_hybrid_is_not_remote(self):
        data = {"jobs": [ASHBY["jobs"][0] | {"secondaryLocations": []}]}
        self.assertFalse(sources.parse_ashby("ramp", data)[0].remote)


class Workday(unittest.TestCase):
    TOKEN = "osu.wd1.myworkdayjobs.com/OSUCareers"

    def test_address_forms(self):
        for text in ("https://osu.wd1.myworkdayjobs.com/en-US/OSUCareers/jobs", "osu.wd1.myworkdayjobs.com/OSUCareers",
                     "https://OSU.wd1.myworkdayjobs.com/OSUCareers/job/Columbus-Campus/Some-Job_R1?q=x"):
            self.assertEqual(sources.workday_token(text), self.TOKEN)
        for bad in ("https://example.com/OSUCareers", "osu.wd1.myworkdayjobs.com", "https://osu.wd1.myworkdayjobs.com/en-US"):
            with self.assertRaises(ValueError):
                sources.workday_token(bad)
        self.assertEqual(config.parse_company_line(f"workday:https://{self.TOKEN}  # OSU"), ("workday", self.TOKEN))

    def test_job_details(self):
        job = sources.parse_workday(self.TOKEN, WORKDAY_DETAIL)
        self.assertEqual(job.id, f"workday:{self.TOKEN}:Manager--Community-Outreach_R141681-2")
        self.assertEqual(job.company_name, "The Ohio State University")
        coded = WORKDAY_DETAIL | {"hiringOrganization": {"name": "10 Nationwide Children's Hospital"}}
        self.assertEqual(sources.parse_workday(self.TOKEN, coded).company_name, "Nationwide Children's Hospital")
        self.assertEqual(sources.parse_workday(self.TOKEN, WORKDAY_DETAIL | {"hiringOrganization": {}}).company_name, "osu")
        self.assertEqual(job.location, "Columbus Campus; Newark Campus; United States of America")
        self.assertFalse(job.remote)  # hybrid is not remote
        self.assertEqual(job.posted_at, "2026-11-20")
        self.assertTrue(job.apply_url.endswith("_R141681-2/apply"))
        self.assertIn("- Build partnerships", job.description)

    def test_only_matching_titles_are_opened_and_slow_down_is_retried(self):
        listing = {"total": 3, "jobPostings": [
            {"title": "Manager, Community Outreach", "externalPath": "/job/a"},
            {"title": "Staff Nurse", "externalPath": "/job/b"},
            {"title": "Community Engineer", "externalPath": "/job/c"}]}
        calls, fail_once = [], [True]

        def fake(url, body=None):
            calls.append(url)
            if fail_once and fail_once.pop():
                error = sources.FeedError("HTTP 429")
                error.code = 429
                raise error
            return listing if url.endswith("/jobs") else WORKDAY_DETAIL

        real = sources._get_json, sources.WORKDAY_GAP, sources.WORKDAY_WAITS
        sources._get_json, sources.WORKDAY_GAP, sources.WORKDAY_WAITS = fake, 0, (0, 0)
        self.addCleanup(lambda: setattr(sources, "_get_json", real[0]) or setattr(sources, "WORKDAY_GAP", real[1])
                        or setattr(sources, "WORKDAY_WAITS", real[2]))
        search = Search(titles=["community"], exclude_titles=["engineer"])
        got = sources.fetch_company("workday", self.TOKEN, search)
        self.assertEqual((got.total, len(got.jobs), got.read, got.resume), (3, 1, ["/job/a", "/job/b", "/job/c"], None))
        api = "https://osu.wd1.myworkdayjobs.com/wday/cxs/osu/OSUCareers"
        self.assertEqual(calls, [f"{api}/jobs", f"{api}/jobs", f"{api}/job/a"])

        # Second run: everything on the page was seen before, so no job page is opened again.
        del calls[:]
        again = sources.fetch_company("workday", self.TOKEN, search, set(got.read))
        self.assertEqual((again.total, again.jobs, again.read), (3, [], []))
        self.assertEqual(calls, [f"{api}/jobs"])

    def test_stops_at_postings_older_than_thirty_days(self):
        pages = {0: [{"title": f"Community Lead {i}", "externalPath": f"/job/new{i}", "postedOn": "Posted 3 Days Ago"}
                     for i in range(20)],
                 20: [{"title": f"Community Lead {i}", "externalPath": f"/job/old{i}", "postedOn": "Posted 30+ Days Ago"}
                      for i in range(20)]}
        offsets = []

        def fake(url, body=None):
            if body is None:
                return WORKDAY_DETAIL
            offsets.append(body["offset"])
            return {"total": 500 if body["offset"] == 0 else 0, "jobPostings": pages[body["offset"]]}

        real = sources._get_json, sources.WORKDAY_GAP
        sources._get_json, sources.WORKDAY_GAP = fake, 0
        self.addCleanup(lambda: setattr(sources, "_get_json", real[0]) or setattr(sources, "WORKDAY_GAP", real[1]))
        got = sources.fetch_workday(self.TOKEN, Search(titles=["community"]), set())
        self.assertEqual((got.total, len(got.jobs), len(got.read), offsets), (500, 20, 40, [0, 20]))

    def test_a_paused_read_keeps_its_progress_and_continues_next_run(self):
        def posting(n):
            return {"title": f"Nurse {n}", "externalPath": f"/job/{n}", "postedOn": "Posted 3 Days Ago"}
        pages = {offset: [posting(offset + i) for i in range(20)] for offset in range(0, 100, 20)}
        blocked, offsets = {40}, []

        def fake(url, body=None):
            offsets.append(body["offset"])
            if body["offset"] in blocked:
                error = sources.FeedError("HTTP 429")
                error.code = 429
                raise error
            return {"total": 100, "jobPostings": pages[body["offset"]]}

        real = sources._get_json, sources.WORKDAY_GAP, sources.WORKDAY_WAITS
        sources._get_json, sources.WORKDAY_GAP, sources.WORKDAY_WAITS = fake, 0, ()
        self.addCleanup(lambda: setattr(sources, "_get_json", real[0]) or setattr(sources, "WORKDAY_GAP", real[1])
                        or setattr(sources, "WORKDAY_WAITS", real[2]))
        search = Search(titles=["community"])
        first = sources.fetch_workday(self.TOKEN, search, set())
        self.assertEqual((len(first.read), first.resume, offsets), (40, 40, [0, 20, 40]))

        # Next run: one look at the newest page, then straight back to where it stopped (one page of overlap).
        blocked.clear()
        del offsets[:]
        second = sources.fetch_workday(self.TOKEN, search, set(first.read), first.resume)
        self.assertEqual((len(second.read), second.resume, offsets), (60, None, [0, 20, 40, 60, 80]))


class Filtering(unittest.TestCase):
    def job(self, title, location="New York", remote=False, posted="2026-11-01"):
        return sources.Job("x:y:1", "x", "y", "Y", title, location, remote, "", "", "", posted)

    def check(self, job, **search) -> bool:
        return sources.matches(job, Search(**({"titles": ["engineer"]} | search)),
                               today=datetime(2026, 11, 19, tzinfo=timezone.utc))

    def test_title_must_match_whole_words(self):
        self.assertTrue(self.check(self.job("Senior Engineer, Payments")))
        self.assertTrue(self.check(self.job("Engineers wanted")))
        self.assertFalse(self.check(self.job("Engineering Manager")))

    def test_exclusions(self):
        self.assertFalse(self.check(self.job("Engineer Intern"), exclude_titles=["intern"]))
        self.assertTrue(self.check(self.job("Engineer, Internal Tools"), exclude_titles=["intern"]))

    def test_location(self):
        self.assertFalse(self.check(self.job("Engineer", "Berlin"), locations=["new york", "remote"]))
        self.assertTrue(self.check(self.job("Engineer", "Berlin", remote=True), locations=["remote"]))
        self.assertTrue(self.check(self.job("Engineer", "Berlin"), locations=[]))

    def test_age(self):
        self.assertFalse(self.check(self.job("Engineer", posted="2026-08-01"), max_age_days=60))
        self.assertTrue(self.check(self.job("Engineer", posted=""), max_age_days=60))


class Tracker(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "jobs.db")
        self.addCleanup(self.store.db.close)
        self.jobs = sources.parse_greenhouse("stripe", GREENHOUSE) + sources.parse_lever("acme", LEVER)

    def test_refetch_keeps_status_and_adds_nothing(self):
        self.assertEqual(self.store.add_jobs(self.jobs), (2, 0))
        self.store.set_score(self.jobs[0].id, 90, "good", {}, min_score=70)
        self.assertEqual(self.store.add_jobs(self.jobs), (0, 0))
        self.assertEqual(self.store.counts(), {"new": 1, "scored": 1})

    def test_same_role_posted_again_is_not_tracked_twice(self):
        self.store.add_jobs(self.jobs)
        other_office = sources.parse_greenhouse("stripe", {"jobs": [GREENHOUSE["jobs"][0] | {"id": 999}]})
        self.assertEqual(self.store.add_jobs(other_office), (0, 1))
        self.assertEqual(self.store.counts(), {"new": 2})

    def test_workday_memory_is_cleared_when_the_title_filter_changes(self):
        self.store.save_workday_progress("site", ["/job/a", "/job/b"], 40, "titles-v1")
        self.assertEqual(self.store.workday_progress("titles-v1"), ({"site": {"/job/a", "/job/b"}}, {"site": 40}))
        self.store.save_workday_progress("site", ["/job/c"], None, "titles-v1")  # finished: nothing to resume
        self.assertEqual(self.store.workday_progress("titles-v1")[1], {})
        self.assertEqual(self.store.workday_progress("titles-v2"), ({}, {}))

    def test_low_score_is_skipped_and_applied_counts_today(self):
        self.store.add_jobs(self.jobs)
        self.assertEqual(self.store.set_score(self.jobs[1].id, 40, "weak", {}, min_score=70), "skipped")
        self.store.set_applied(self.jobs[0].id)
        self.assertEqual(self.store.applied_today(), 1)
        self.assertEqual(len(self.store.find("stripe")), 1)


class FetchCommand(unittest.TestCase):
    def test_fetch_stores_matches_and_remembers_workday_postings(self):
        import contextlib, io
        from unittest import mock
        from jobhunt import cli
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = Store(Path(tmp.name) / "jobs.db")
        self.addCleanup(db.db.close)
        site = Workday.TOKEN
        board = {"jobs": [GREENHOUSE["jobs"][0] | {"title": "Community Manager"}, GREENHOUSE["jobs"][0] | {"id": 2}]}
        asked = []

        def fake(source, token, search=None, seen=None, resume=None):
            asked.append((source, seen, resume))
            if source == "workday":
                return sources.Fetched(5, [sources.parse_workday(site, WORKDAY_DETAIL)], ["/job/a", "/job/b"], resume=40)
            return sources.Fetched(2, sources.parse_greenhouse(token, board))

        settings = config.Settings(search=Search(titles=["community"], max_age_days=0))
        with mock.patch.object(cli.config, "load_settings", lambda: settings), \
             mock.patch.object(cli.config, "load_companies", lambda: [("greenhouse", "stripe"), ("workday", site)]), \
             mock.patch.object(cli, "store", lambda: db), mock.patch.object(cli.sources, "fetch_company", fake):
            for _ in range(2):
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    self.assertEqual(cli.main(["fetch"]), 0)
        self.assertIn("7 postings from 2 boards: 2 match", out.getvalue())
        self.assertIn("paused by Workday", out.getvalue())
        self.assertEqual(db.counts(), {"new": 2})
        # The second run was told what the first one had read and where it was paused.
        self.assertEqual([a[1:] for a in asked if a[0] == "workday"], [(None, None), ({"/job/a", "/job/b"}, 40)])


class Drafts(unittest.TestCase):
    def test_drafts_are_found_relative_to_the_jobhunt_folder(self):
        from jobhunt import apply
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "data" / "materials" / "job1"
            folder.mkdir(parents=True)
            (folder / "application.json").write_text(json.dumps({"cover_letter": "old", "check_before_sending": ["x"]}))
            (folder / "cover_letter.txt").write_text("Edited by hand\n")
            real = apply.ROOT
            apply.ROOT = Path(tmp)
            self.addCleanup(setattr, apply, "ROOT", real)
            letter, letter_file, checks = apply.load_drafts({"materials_dir": "data/materials/job1"})
            self.assertEqual((letter, letter_file, checks), ("Edited by hand", folder / "cover_letter.txt", ["x"]))
            self.assertEqual(apply.load_drafts({"materials_dir": None}), ("", None, []))


class MenuAndKey(unittest.TestCase):
    KEY = "sk-ant-" + "x" * 40

    def test_key_is_saved_to_a_file_and_read_back(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict("os.environ", {}, clear=False) as env:
            env.pop("ANTHROPIC_API_KEY", None)
            root = Path(tmp)
            self.assertIsNone(config.load_api_key(root))
            for bad in ("hello", "sk-ant-short", "sk-ant-" + "x" * 30 + " oops"):
                with self.assertRaises(config.ConfigError):
                    config.save_api_key(bad, root)
            config.save_api_key(f'  "{self.KEY}"  ', root)
            self.assertEqual(config.load_api_key(root), self.KEY)
            env["ANTHROPIC_API_KEY"] = "sk-ant-from-environment"
            self.assertEqual(config.load_api_key(root), "sk-ant-from-environment")

    def test_menu_runs_the_chosen_commands_until_closed(self):
        import contextlib, io
        from unittest import mock
        from jobhunt import cli
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        answers = iter(["9", "abc", "5", "6", "0"])
        with mock.patch.object(cli, "ROOT", Path(tmp.name)), contextlib.redirect_stdout(io.StringIO()) as out:
            cli.store().add_jobs(sources.parse_greenhouse("stripe", GREENHOUSE))
            cli.cmd_menu(None, ask=lambda prompt: next(answers))
        text = out.getvalue()
        self.assertEqual(text.count("Please type one of the numbers shown."), 2)
        self.assertIn("New, not scored yet: 1", text)
        self.assertIn("Backend Engineer", text)                       # 5: the list
        self.assertTrue((Path(tmp.name) / "data" / "tracker.csv").exists())  # 6: the spreadsheet


class Installer(unittest.TestCase):
    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("jobhunt_install", config.ROOT / "install.py")
        self.install = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.install)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_takes_only_your_files_from_the_newest_package(self):
        import os, zipfile
        downloads, root = self.dir / "Downloads", self.dir / "jobhunt"
        downloads.mkdir()
        root.mkdir()
        self.assertIsNone(self.install.find_personal_package([downloads, self.dir / "missing"]))
        for name, stamp in (("jobhunt-personal.zip", 1000), ("jobhunt-personal (1).zip", 2000)):
            with zipfile.ZipFile(downloads / name, "w") as z:
                z.writestr("profile.toml", name)
                z.writestr("config.toml", "c")
                z.writestr("resume.pdf", "r")
                z.writestr("data/materials/job/cover_letter.txt", "letter")
                z.writestr("jobhunt/cli.py", "must not overwrite code")
                z.writestr("../outside.txt", "must not escape")
                z.writestr("data/../../outside2.txt", "must not escape")
            os.utime(downloads / name, (stamp, stamp))
        package = self.install.find_personal_package([downloads])
        self.assertEqual(package.name, "jobhunt-personal (1).zip")
        taken = self.install.unpack_personal(package, root)
        self.assertEqual(sorted(taken), ["config.toml", "data/materials/job/cover_letter.txt", "profile.toml", "resume.pdf"])
        self.assertEqual((root / "profile.toml").read_text(), "jobhunt-personal (1).zip")
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["Downloads", "jobhunt"])
        self.assertFalse((root / "jobhunt").exists())

    def test_shortcut_opens_the_menu_from_the_install_folder(self):
        shortcut = self.install.write_shortcut(Path("C:/Users/ada/jobhunt"), self.dir)
        text = shortcut.read_bytes().decode()
        self.assertEqual(shortcut.name, "Job Hunt.bat")
        self.assertIn('cd /d "', text)
        self.assertIn('"\r\n".venv\\Scripts\\python.exe" -m jobhunt menu\r\n', text)


class Config(unittest.TestCase):
    def test_companies_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "companies.txt").write_text("# note\ngreenhouse:stripe  # big\n\nlever:acme\ngreenhouse:stripe\n")
            self.assertEqual(config.load_companies(Path(tmp)), [("greenhouse", "stripe"), ("lever", "acme")])
            Path(tmp, "companies.txt").write_text("workday:acme\n")
            with self.assertRaises(config.ConfigError):
                config.load_companies(Path(tmp))

    def test_resume_text_cleanup(self):
        raw = "Ada   Lovelace  |  London\n\n\n\n      ❖    Handle  staﬃng and ﬁeld  work.   "
        self.assertEqual(config.clean_resume_text(raw), "Ada Lovelace | London\n\n❖ Handle staffing and field work.")

    def test_examples_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("config", "profile"):
                Path(tmp, f"{name}.toml").write_text((config.ROOT / f"{name}.example.toml").read_text())
            settings = config.load_settings(Path(tmp))
            self.assertEqual(settings.min_score, 70)
            self.assertIn("software engineer", settings.search.titles)
            person = config.load_profile(Path(tmp))
            self.assertEqual(person.resume_pdf, Path(tmp) / "resume.pdf")
            self.assertEqual(person.answers["how did you hear"], "Company careers page")


class FieldMatching(unittest.TestCase):
    def test_contact_fields(self):
        p = profile()
        for label, expected in [("First Name*", "Ada"), ("Last Name", "Lovelace"), ("Full name ✱", "Ada Lovelace"),
                                ("Name", "Ada Lovelace"), ("Email", "ada@example.com"), ("Phone Number", "+1 555 0100"),
                                ("LinkedIn URL", "https://linkedin.com/in/ada"), ("Current location", "London, UK"),
                                ("City", "London"), ("Location (City)", "London, UK"),
                                ("Preferred First Name", "Ada"), ("Most Recent Employer", "Analytical Engines"),
                                ("Who is your current or previous employer?", "Analytical Engines")]:
            self.assertEqual(choose_value(field(label), p), expected, label)
        self.assertEqual(choose_value(field("", name="name"), p), "Ada Lovelace")
        self.assertEqual(choose_value(field("", name="org"), p), "Analytical Engines")

    def test_leaves_alone_what_it_should(self):
        p = profile()
        self.assertIsNone(choose_value(field("Company name"), p))
        self.assertIsNone(choose_value(field("Emergency contact phone"), p))
        self.assertIsNone(choose_value(field("Who referred you? (email)"), p))
        self.assertIsNone(choose_value(field("GitHub URL"), p))  # nothing saved for it
        self.assertIsNone(choose_value(field("Why do you want to work here?", tag="textarea"), p, "letter"))

    def test_cover_letter_and_saved_answers(self):
        p = profile(answers={"require sponsorship": "No"})
        self.assertEqual(choose_value(field("Cover Letter", tag="textarea"), p, "Dear team"), "Dear team")
        self.assertEqual(choose_value(field("Will you require sponsorship?"), p), "No")
        dropdown = field("Will you require sponsorship?", tag="select", options=["Select...", "Yes", "No"])
        self.assertEqual(choose_option(dropdown, p), "No")
        self.assertIsNone(choose_option(field("Gender", tag="select", options=["Man", "Woman"]), p))

    def test_files(self):
        resume, letter = Path("resume.pdf"), Path("cover_letter.txt")
        self.assertEqual(choose_file(field("Attach", type="file", id="resume"), resume, letter), resume)
        self.assertEqual(choose_file(field("Attach", type="file", id="cover_letter"), resume, letter), letter)
        self.assertIsNone(choose_file(field("Attach", type="file", id="cover_letter", accept=".pdf"), resume, letter))
        self.assertIsNone(choose_file(field("", type="file", near="Autofill from resume"), resume, letter))
        self.assertIsNone(choose_file(field("Transcript", type="file"), resume, letter))


FORM = """<form onsubmit="document.title='SUBMITTED'; return false">
<label for="fn">First Name*</label><input id="fn">
<label for="ln">Last Name*</label><input id="ln">
<label for="em">Email*</label><input id="em" type="email" required>
<label for="city">Location (City)*</label><input id="city" role="combobox">
<label for="kept">Phone</label><input id="kept" value="already here">
<label for="cv">Attach</label><input id="resume" type="file">
<label for="why">Why us?*</label><textarea id="why"></textarea>
<label for="visa">Will you require sponsorship?*</label>
<select id="visa"><option>Select...</option><option>Yes</option><option>No</option></select>
<input type="radio" name="q1"> Yes <input type="radio" name="q1"> No
<button type="submit">Submit</button></form>"""


class FormFilling(unittest.TestCase):
    def test_fills_known_fields_and_never_submits(self):
        try:
            from playwright.sync_api import Error, sync_playwright
        except ImportError:
            self.skipTest("playwright not installed")
        with tempfile.TemporaryDirectory() as tmp, sync_playwright() as p:
            resume = Path(tmp, "resume.pdf")
            resume.write_bytes(b"%PDF-1.4 test")
            try:
                browser = p.chromium.launch()
            except Error:
                self.skipTest("no browser installed (playwright install chromium)")
            page = browser.new_page()
            page.set_content(FORM)
            report = prefill(page, profile(resume_pdf=resume, answers={"require sponsorship": "No"}))
            value = lambda sel: page.eval_on_selector(sel, "e => e.value")
            self.assertEqual((value("#fn"), value("#ln"), value("#em")), ("Ada", "Lovelace", "ada@example.com"))
            self.assertEqual(value("#visa"), "No")
            self.assertEqual(value("#kept"), "already here")
            self.assertEqual(value("#city"), "")
            self.assertEqual(page.eval_on_selector("#resume", "e => e.files[0].name"), "resume.pdf")
            self.assertEqual(report.needs_you, ["Location (City)", "Why us?"])
            self.assertEqual(report.choice_questions, 1)
            self.assertNotEqual(page.title(), "SUBMITTED")
            browser.close()


class FakeClaude(BaseHTTPRequestHandler):
    """Stands in for the Messages API so the request the SDK sends can be inspected."""
    requests: list = []
    reply: dict = {}

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        FakeClaude.requests.append((self.path, self.headers.get("anthropic-beta"), body))
        payload = json.dumps({"id": "msg_1", "type": "message", "role": "assistant", "model": body["model"],
                              "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}} | FakeClaude.reply)
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(payload.encode())

    def log_message(self, *args):
        pass


class ClaudeCalls(unittest.TestCase):
    def setUp(self):
        import anthropic
        server = HTTPServer(("127.0.0.1", 0), FakeClaude)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        FakeClaude.requests = []
        client = anthropic.Anthropic(api_key="test", base_url=f"http://127.0.0.1:{server.server_port}", max_retries=0)
        self.assistant = Assistant("claude-opus-5-5", "RESUME TEXT", "Remote only", client=client)
        self.posting = Posting("Engineer", "Acme", "Remote", "Python role")

    def test_score_request_and_reply(self):
        answer = {"score": 140, "verdict": "Strong", "matches": ["python"], "gaps": []}
        FakeClaude.reply = {"stop_reason": "end_turn", "content": [{"type": "text", "text": json.dumps(answer)}]}
        result = self.assistant.score(self.posting)
        self.assertEqual(result["score"], 100)  # clamped
        path, beta, body = FakeClaude.requests[0]
        self.assertTrue(path.startswith("/v1/messages"))
        self.assertIn("server-side-fallback-2026-07-01", beta)
        self.assertEqual(body["fallbacks"], "default")
        self.assertEqual(body["output_config"]["effort"], "low")
        self.assertEqual(body["output_config"]["format"]["type"], "json_schema")
        self.assertIn("RESUME TEXT", body["system"][1]["text"])
        self.assertIn("Python role", body["messages"][0]["content"])
        self.assertNotIn("thinking", body)

    def test_refusal_is_reported_not_parsed(self):
        FakeClaude.reply = {"stop_reason": "refusal", "content": []}
        with self.assertRaises(LLMError):
            self.assistant.tailor(self.posting)

    def test_keyword_fallback(self):
        result = keyword_score(Posting("Engineer", "Acme", "", "We use Python and JavaScript."), ["python", "java", "sql"])
        self.assertEqual((result["score"], result["matches"]), (33, ["python"]))


if __name__ == "__main__":
    unittest.main()
