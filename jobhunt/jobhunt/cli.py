"""Command line: init, companies, fetch, score, tailor, list, show, apply, export."""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import config, sources
from .config import ConfigError
from .llm import Assistant, LLMError, Posting, keyword_score
from .store import Store

ROOT = config.ROOT


def store() -> Store:
    return Store(ROOT / "data" / "jobs.db")


def posting_of(row) -> Posting:
    return Posting(title=row["title"], company=row["company_name"], location=row["location"],
                   description=row["description"])


def cmd_init(args) -> None:
    for name in ("config", "profile"):
        target = ROOT / f"{name}.toml"
        if target.exists():
            print(f"{target.name} already exists, left as is.")
        else:
            shutil.copy(ROOT / f"{name}.example.toml", target)
            print(f"Created {target.name}.")
    print("Next: edit config.toml (what you are looking for) and profile.toml (your details), "
          "put your resume next to them, then run `python -m jobhunt fetch`.")


def cmd_companies(args) -> None:
    path = ROOT / "companies.txt"
    known = config.load_companies()
    if not args.names:
        for source, token in known:
            print(f"{source}:{token}")
        print(f"{len(known)} boards. Add more with `python -m jobhunt companies stripe figma ...`.")
        return
    for name in args.names:
        if "myworkdayjobs.com" in name.lower():
            # Workday sites are added by address: there is no way to look one up by company name.
            try:
                token = sources.workday_token(name)
                hits = [("workday", sources.workday_total(token))]
            except ValueError:
                print(f"{name}: paste the careers-site address, like https://osu.wd1.myworkdayjobs.com/OSUCareers")
                continue
            except sources.FeedError as e:
                print(f"{name}: could not read that Workday site ({e}). Check the address.")
                continue
        else:
            token = name.strip().lower()
            hits = []
            for source in ("greenhouse", "lever", "ashby"):
                try:
                    count = sources.fetch_company(source, token)[0]
                except sources.FeedError:
                    continue
                if count:
                    hits.append((source, count))
            if not hits:
                print(f"{token}: no board found on Greenhouse, Lever or Ashby. Try the name from their careers-page "
                      "URL, or paste the address if their careers site is on myworkdayjobs.com.")
        for source, count in hits:
            if (source, token) in known:
                print(f"{source}:{token} already listed ({count} open jobs).")
            else:
                with path.open("a", encoding="utf-8") as f:
                    f.write(f"{source}:{token}\n")
                known.append((source, token))
                print(f"Added {source}:{token} ({count} open jobs).")


def cmd_fetch(args) -> None:
    settings = config.load_settings()
    companies = config.load_companies()
    db = store()

    search_key = json.dumps([settings.search.titles, settings.search.exclude_titles])
    read_before = db.workday_seen(search_key)
    if any(source == "workday" for source, _ in companies):
        print("Workday sites limit how fast they can be read. The first run takes several minutes; later runs are quick.")

    def pull(pair):
        try:
            return pair, *sources.fetch_company(*pair, settings.search, read_before.get(pair[1])), None
        except sources.FeedError as e:
            return pair, 0, [], [], str(e)

    seen = matched = added = repeats = 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        for (source, token), total, jobs, read, error in pool.map(pull, companies):
            if error:
                print(f"  {source}:{token}  failed: {error}", flush=True)
                continue
            keep = [j for j in jobs if sources.matches(j, settings.search)]
            new, same_role = db.add_jobs(keep)
            if source == "workday":
                db.add_workday_seen(token, read, search_key)
            seen, matched, added, repeats = seen + total, matched + len(keep), added + new, repeats + same_role
            print(f"  {source}:{token}  {total} open, {len(keep)} match, {new} new", flush=True)
    print(f"Read {seen} postings from {len(companies)} boards: {matched} match your search, {added} are new."
          + (f" Ignored {repeats} repeat listings of a role already tracked." if repeats else ""))
    if added:
        print("Next: `python -m jobhunt score`")


def cmd_score(args) -> None:
    settings = config.load_settings()
    db = store()
    rows = db.by_status("new")[: args.limit]
    if not rows:
        print("Nothing new to score. Run `python -m jobhunt fetch` first.")
        return
    if args.no_llm:
        def judge(row):
            return keyword_score(posting_of(row), settings.search.keywords)
        workers = 1
    else:
        assistant = Assistant(settings.model, config.load_resume_text(config.load_profile()), settings.preferences)

        def judge(row):
            return assistant.score(posting_of(row))
        workers = 4

    def run(row):
        try:
            return row, judge(row), None
        except LLMError as e:
            return row, None, str(e)

    kept = failed = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for row, result, error in pool.map(run, rows):
            label = f"{row['title']} at {row['company_name']}"
            if error:
                failed += 1
                print(f"  --  {label}: {error}")
                continue
            status = db.set_score(row["id"], result["score"], result["verdict"], result, settings.min_score)
            kept += status == "scored"
            print(f"  {result['score']:>3}  {label}{'' if status == 'scored' else '  (below threshold)'}")
    print(f"Scored {len(rows) - failed}: {kept} at or above {settings.min_score}."
          + (f" {failed} failed and stay in the queue." if failed else ""))
    if kept:
        print("Next: `python -m jobhunt tailor`, or `python -m jobhunt list` to look first.")


def write_materials(row, result: dict) -> Path:
    folder = ROOT / "data" / "materials" / row["id"].replace(":", "_")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "application.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    (folder / "cover_letter.txt").write_text(result["cover_letter"].strip() + "\n", encoding="utf-8")
    bullets = "\n".join(f"- {b}" for b in result["resume_bullets"])
    checks = "\n".join(f"- {c}" for c in result["check_before_sending"]) or "- Nothing flagged."
    (folder / "summary.md").write_text(
        f"# {row['title']} at {row['company_name']}\n\n{row['url']}\n\n"
        f"## Check before sending\n\n{checks}\n\n## Cover letter\n\n{result['cover_letter'].strip()}\n\n"
        f"## Resume bullets to lead with\n\n{bullets}\n\n## Why this company\n\n{result['why_this_company'].strip()}\n",
        encoding="utf-8")
    return folder


def cmd_tailor(args) -> None:
    settings = config.load_settings()
    db = store()
    rows = db.by_status("scored")[: args.limit]
    if not rows:
        print("No scored jobs waiting for drafts. Run `python -m jobhunt score` first.")
        return
    assistant = Assistant(settings.model, config.load_resume_text(config.load_profile()), settings.preferences)

    def run(row):
        try:
            return row, assistant.tailor(posting_of(row)), None
        except LLMError as e:
            return row, None, str(e)

    done = 0
    with ThreadPoolExecutor(max_workers=4) as pool:
        for row, result, error in pool.map(run, rows):
            label = f"{row['title']} at {row['company_name']}"
            if error:
                print(f"  failed  {label}: {error}")
                continue
            folder = write_materials(row, result)
            db.set_drafted(row["id"], folder)
            done += 1
            flags = len(result["check_before_sending"])
            print(f"  drafted  {label}" + (f"  ({flags} to check)" if flags else ""))
    print(f"Drafted {done} of {len(rows)}. Drafts are in data/materials/. Next: `python -m jobhunt apply`")


def cmd_list(args) -> None:
    db = store()
    rows = db.by_status(args.status) if args.status else db.all()
    for row in rows[: args.limit]:
        score = "" if row["score"] is None else row["score"]
        print(f"{score:>3}  {row['status']:<8} {row['title'][:44]:<44} {row['company_name'][:16]:<16} "
              f"{row['location'][:26]:<26} {row['id']}")
    counts = db.counts()
    print(", ".join(f"{n} {s}" for s, n in sorted(counts.items())) or "No jobs yet. Run `python -m jobhunt fetch`.")


def pick(db: Store, fragment: str):
    found = db.find(fragment)
    if len(found) != 1:
        raise ConfigError(f"{fragment!r} matches {len(found)} jobs; use a full id from `python -m jobhunt list`.")
    return found[0]


def cmd_show(args) -> None:
    row = pick(store(), args.job)
    print(f"{row['title']} at {row['company_name']}  [{row['status']}]\n{row['location']}  posted {row['posted_at']}")
    print(f"Posting: {row['url']}\nApply:   {row['apply_url']}")
    if row["score"] is not None:
        detail = json.loads(row["score_detail"] or "{}")
        print(f"\nScore {row['score']}: {row['verdict']}")
        for key in ("matches", "gaps"):
            if detail.get(key):
                print(f"  {key}: " + "; ".join(detail[key]))
    if row["materials_dir"]:
        print(f"\nDrafts: {row['materials_dir']}")
    if args.full:
        print("\n" + row["description"])


def cmd_skip(args) -> None:
    db = store()
    row = pick(db, args.job)
    db.set_skipped(row["id"], "skipped by you")
    print(f"Skipped {row['title']} at {row['company_name']}.")


def cmd_export(args) -> None:
    out = ROOT / "data" / "tracker.csv"
    columns = ["status", "score", "title", "company_name", "location", "posted_at", "applied_at", "url", "verdict", "note", "id"]
    rows = store().all()
    with out.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(columns)
        writer.writerows([row[c] for c in columns] for row in rows)
    print(f"Wrote {len(rows)} jobs to {out}")


def cmd_apply(args) -> None:
    from .apply import run_session
    run_session(store(), config.load_settings(), config.load_profile(), limit=args.limit)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m jobhunt", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name, func, help):
        p = sub.add_parser(name, help=help)
        p.set_defaults(func=func)
        return p

    add("init", cmd_init, "create config.toml and profile.toml from the examples")
    add("companies", cmd_companies, "list boards, or look companies up and add them").add_argument("names", nargs="*")
    add("fetch", cmd_fetch, "pull open jobs from every board and keep the ones matching your search")
    p = add("score", cmd_score, "rate each new job against your resume")
    p.add_argument("--no-llm", action="store_true", help="keyword scoring, no API key needed")
    p.add_argument("--limit", type=int, default=500)
    add("tailor", cmd_tailor, "draft a cover letter and talking points for the best matches").add_argument(
        "--limit", type=int, default=20)
    p = add("list", cmd_list, "show tracked jobs, best score first")
    p.add_argument("--status", choices=["new", "scored", "drafted", "applied", "skipped"])
    p.add_argument("--limit", type=int, default=50)
    p = add("show", cmd_show, "details for one job")
    p.add_argument("job")
    p.add_argument("--full", action="store_true", help="include the posting text")
    add("skip", cmd_skip, "drop a job from the queue").add_argument("job")
    add("apply", cmd_apply, "open each drafted job, prefill the form, and wait for you to submit").add_argument(
        "--limit", type=int, default=None)
    add("export", cmd_export, "write the tracker to data/tracker.csv")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except ConfigError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nStopped.")
        return 130
    except BrokenPipeError:
        sys.stderr.close()  # output was piped into something like `head` that stopped reading
    return 0
