"""Fills the parts of an application form that are the same every time.

It types your contact details, attaches your resume and pastes the drafted cover letter. It never
clicks Submit and it leaves dropdowns, checkboxes and anything it does not recognise for you.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .config import Profile

# Tags every fillable control with data-jobhunt="<n>" and describes it.
COLLECT_JS = r"""
() => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const skip = ['hidden', 'submit', 'button', 'image', 'reset', 'search'];
  const fields = [];
  const choiceGroups = new Set();
  let n = 0;
  for (const el of document.querySelectorAll('input, textarea, select')) {
    const type = (el.getAttribute('type') || el.type || 'text').toLowerCase();
    const visible = !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
    if (type === 'checkbox' || type === 'radio') {
      if (visible) choiceGroups.add(el.name || el.id);
      continue;
    }
    if (skip.includes(type) || el.disabled || el.readOnly) continue;
    if (type !== 'file' && !visible) continue;
    let label = el.labels && el.labels.length ? el.labels[0].innerText : '';
    const by = el.getAttribute('aria-labelledby');
    if (!clean(label) && by) {
      label = by.split(/\s+/).map(id => (document.getElementById(id) || {}).innerText || '').join(' ');
    }
    if (!clean(label)) label = el.getAttribute('aria-label') || '';
    // Fallback for forms that put the question in a sibling element instead of a <label>.
    let near = '';
    const own = clean(el.innerText);  // a dropdown's own option list is not its question
    for (let p = el.parentElement, i = 0; p && i < 4; p = p.parentElement, i++) {
      const t = clean(clean(p.innerText).replace(own, '').replace(/select(\.\.\.|…)?\s*$/i, ''));
      if (t.length >= 3 && t.length <= 120 && !/^select\b/i.test(t)) { near = t; break; }
    }
    el.setAttribute('data-jobhunt', String(n));
    fields.push({
      n: n++, tag: el.tagName.toLowerCase(), type,
      id: el.id || '', name: el.getAttribute('name') || '',
      label: clean(label), near, placeholder: el.getAttribute('placeholder') || '',
      // Pick-from-a-list fields discard typed text. Lever's location box is one but does not say so.
      combobox: el.getAttribute('role') === 'combobox' || el.hasAttribute('aria-autocomplete') || el.hasAttribute('list')
        || el.id === 'location-input',
      required: el.required || el.getAttribute('aria-required') === 'true' || /[*✱]/.test(label),
      // A dropdown still on its first option ("Select...") counts as unanswered.
      empty: type === 'file' ? !(el.files && el.files.length)
        : el.tagName === 'SELECT' ? el.selectedIndex <= 0 : !el.value,
      accept: el.getAttribute('accept') || '',
      options: el.tagName === 'SELECT' ? [...el.options].map(o => clean(o.text)) : [],
    });
  }
  return {fields, choiceGroups: choiceGroups.size};
}
"""

MARK_JS = """
([filled, left]) => {
  const paint = (n, colour) => {
    const el = document.querySelector(`[data-jobhunt="${n}"]`);
    if (el) { el.style.outline = `2px solid ${colour}`; el.style.outlineOffset = '2px'; }
  };
  filled.forEach(n => paint(n, '#2e9e5b'));
  left.forEach(n => paint(n, '#e08a00'));
}
"""

# A field about someone else (a referrer, an emergency contact) must not get your details.
OTHER_PERSON = re.compile(r"emergency|\breference|\breferr|recruiter|hiring manager")

# (profile attribute, pattern). First match wins, so the more specific patterns come first.
RULES: list[tuple[str, re.Pattern]] = [(key, re.compile(pattern)) for key, pattern in [
    ("first_name", r"\bfirst[ _-]?name|\bgiven[ _-]?name|preferred (first )?name"),
    ("last_name", r"\blast[ _-]?name|\bfamily[ _-]?name|\bsurname"),
    ("linkedin", r"linkedin"),
    ("github", r"github"),
    ("website", r"portfolio|personal (web)?site|\bwebsite\b|\bother url"),
    ("email", r"\be-?mail"),
    ("phone", r"\bphone|\bmobile\b|telephone"),
    ("current_company", r"(current|most recent)( or (previous|most recent))? (company|employer)|\[org\]"),
    ("current_title", r"(current|most recent)( or (previous|most recent))? (job )?title"),
    ("city", r"^city\b"),
    ("location", r"current location|\blocation\b|\bcity\b|where are you (currently )?(based|located)"),
    ("full_name", r"\bfull[ _-]?name|\blegal name|^name\b|\byour name|\[name\]|_systemfield_name"),
]]


@dataclass
class Report:
    filled: list[str] = field(default_factory=list)
    needs_you: list[str] = field(default_factory=list)
    choice_questions: int = 0


def describe(f: dict) -> str:
    """Everything we know about a control, lowercased, with name/id in brackets."""
    return f"{f['label']} {f['placeholder']} [{f['name']}] [{f['id']}]".lower()


def display_name(f: dict) -> str:
    """The question as the user sees it, or "" when the control has no visible wording."""
    text = f["label"] or f["placeholder"] or f["near"]
    return re.sub(r"\s*[*✱]\s*", " ", text).strip()[:70]


def choose_value(f: dict, profile: Profile, cover_letter: str = "") -> str | None:
    """The text to type into a text field or textarea, or None to leave it for the user."""
    text = describe(f)
    if OTHER_PERSON.search(text):
        return None
    label = f["label"].lower()
    for question, answer in profile.answers.items():
        if question in label:
            return answer
    if f["tag"] == "textarea":
        return cover_letter or None if re.search(r"cover[ _-]?letter", text) else None
    if f["type"] == "tel" and profile.phone:
        return profile.phone
    for key, pattern in RULES:
        if pattern.search(text):
            return getattr(profile, key) or None
    return None


def choose_option(f: dict, profile: Profile) -> str | None:
    """For a native dropdown: the option that matches one of your saved answers."""
    label = f["label"].lower()
    for question, answer in profile.answers.items():
        if question in label:
            wanted = answer.lower()
            exact = [o for o in f["options"] if o.lower() == wanted]
            starts = [o for o in f["options"] if o.lower().startswith(wanted)]
            return (exact or starts or [None])[0]
    return None


def choose_file(f: dict, resume: Path | None, cover_letter_file: Path | None) -> Path | None:
    own = f"{f['label']} [{f['name']}] [{f['id']}]".lower()
    text = own if re.search(r"resume|\bcv\b|cover", own) else f"{own} {f['near'].lower()}"
    if "autofill" in text:
        return None  # a "fill this form from your resume" helper, not the resume field itself
    if re.search(r"cover", text):
        accepts_text = not f["accept"] or re.search(r"\.txt|text/plain", f["accept"])
        return cover_letter_file if accepts_text else None
    if re.search(r"resume|\bcv\b|curriculum", text):
        return resume
    return None


def prefill(page, profile: Profile, cover_letter: str = "", cover_letter_file: Path | None = None) -> Report:
    """Fill what we recognise on an already-loaded page. Never submits."""
    report = Report()
    for frame in page.frames:
        try:
            found = frame.evaluate(COLLECT_JS)
        except Exception:
            continue  # cross-origin widgets (captcha, analytics) that we have no business in
        report.choice_questions += found["choiceGroups"]
        filled, left = [], []
        for f in found["fields"]:
            target = frame.locator(f'[data-jobhunt="{f["n"]}"]')
            name = display_name(f)
            done = False
            try:
                if not f["empty"]:
                    continue  # already has a value (an earlier fill, browser autofill, the site's resume parser)
                if f["type"] == "file":
                    file = choose_file(f, profile.resume_pdf, cover_letter_file)
                    if file:
                        target.set_input_files(str(file))
                        kind = "Cover letter" if file == cover_letter_file else "Resume"
                        name, done = f"{kind} ({file.name})", True
                elif f["tag"] == "select":
                    option = choose_option(f, profile)
                    if option:
                        target.select_option(label=option)
                        done = True
                elif not f["combobox"]:
                    value = choose_value(f, profile, cover_letter)
                    if value:
                        target.fill(value)
                        done = True
            except Exception:
                done = False
            if done:
                filled.append(f["n"])
                report.filled.append(name or f["name"] or f["id"])
            elif f["required"] and name:
                left.append(f["n"])
                if name not in report.needs_you:
                    report.needs_you.append(name)
        try:
            frame.evaluate(MARK_JS, [filled, left])
        except Exception:
            pass
    return report


def open_form(page, url: str) -> None:
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_selector("input, textarea", state="attached", timeout=30000)
    page.wait_for_timeout(1500)  # let the form finish rendering its custom questions
