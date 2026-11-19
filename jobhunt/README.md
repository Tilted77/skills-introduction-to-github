# jobhunt

Finds open jobs that fit you, scores them against your resume, drafts a cover letter for the good ones,
and fills in the application form. You read each form, finish it, and press Submit yourself.

It never submits anything on its own.

## How it works

| Step | Command | What happens |
|---|---|---|
| 1 | `fetch` | Reads the public job feeds of the companies in `companies.txt` and keeps postings that match your titles and locations. |
| 2 | `score` | Claude rates each new posting 0-100 against your resume. Anything under your threshold is dropped. |
| 3 | `tailor` | For the best matches, Claude drafts a cover letter, resume bullets to lead with, and a list of things to check. |
| 4 | `apply` | Opens each form in a browser window, fills your details, attaches your resume and cover letter, and waits for you. |

Jobs come from Greenhouse, Lever and Ashby, the systems many companies use for their careers pages.
LinkedIn and Indeed are not used: they forbid automation and restrict accounts that try it.

## Setup (once)

You need Python 3.11 or newer.

```bash
cd jobhunt
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
python -m jobhunt init
```

Then:

1. Put your resume in this folder as `resume.pdf`.
2. Edit `profile.toml`: your name, email, phone, links.
3. Edit `config.toml`: the job titles and locations you want.
4. Edit `companies.txt`: the starter list is tech companies. To add employers in your field:

   ```bash
   python -m jobhunt companies spotify duolingo
   ```

   The name is the one in the company's careers-page URL (`jobs.lever.co/spotify` is `spotify`).
5. Get a Claude API key from https://platform.claude.com and set it in your terminal:

   ```bash
   export ANTHROPIC_API_KEY="sk-ant-..."     # Windows PowerShell: $env:ANTHROPIC_API_KEY="sk-ant-..."
   ```

## Daily use

```bash
python -m jobhunt fetch
python -m jobhunt score
python -m jobhunt tailor
python -m jobhunt apply
```

During `apply`, each job gets a browser tab. Fields the tool filled are outlined in green, required
fields it left for you in orange. Finish the form, press Submit, then answer in the terminal:

- `a` you submitted it
- `s` skip this job for good
- `l` leave it in the queue for later
- `q` stop for now

Other commands:

```bash
python -m jobhunt list                 # everything tracked, best score first
python -m jobhunt list --status scored
python -m jobhunt show stripe:8172510  # one job: score, reasons, links
python -m jobhunt skip stripe:8172510
python -m jobhunt export               # data/tracker.csv, opens in Excel or Google Sheets
```

Before `apply`, you can edit any draft in `data/materials/<job>/cover_letter.txt`. The edited file is what gets used.

## What the form filler does and does not do

It fills: name, email, phone, LinkedIn, GitHub, website, current company and title, resume upload,
cover letter, and any question you saved an answer for under `[answers]` in `profile.toml`.

It leaves for you: location pickers and other search-as-you-type dropdowns, checkboxes and
multiple-choice questions, demographic questions, and free-text questions it has no answer for.
The terminal lists the required fields it could detect; check the form itself for the rest.
Captchas appear when you press Submit and are yours to solve.

One role is tracked once per company even if it is posted for several offices, so you are not
queued to apply to the same job twice.

## Without an API key

`python -m jobhunt score --no-llm` scores by counting the `keywords` from `config.toml` in each posting.
`apply` then works without drafts: it fills your details and attaches your resume. `tailor` needs a key.

## Cost and privacy

- Scoring and tailoring send your resume text and the job posting to the Claude API, billed to your key.
  As a rough estimate at current list prices for the default model: about 2 cents per job scored and
  5 cents per application tailored. `fetch` filters by title and location first, so only matching jobs are scored.
  Change the model under `[scoring]` in `config.toml`.
- `config.toml`, `profile.toml`, `resume.*`, `data/` and `browser-profile/` are in `.gitignore`.
  Git will not upload them. Do not remove those lines if this repository is public.
- `apply` stops at `daily_limit` applications a day (default 25). Fewer, better-matched applications get more replies.

## Tests

```bash
python -m unittest discover tests
```
