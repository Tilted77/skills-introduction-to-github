"""Fills the parts of an application form that are the same every time, and reads back what you answered.

It types your contact details, attaches your resume, pastes the drafted cover letter, and answers the
standard questions you have saved answers for, whether they are text boxes, dropdowns or multiple choice.
It never clicks Submit, never ticks a lone consent box, and leaves anything it is unsure of for you.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .answers import Answers, pick_option
from .config import Profile

# Describes every control on the form. With tag=true it also marks each one (data-jobhunt="<n>", and
# data-jobhunt-opt="<n>:<i>" on each choice) so it can be found again to fill it.
FORM_JS = r"""
(tag) => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const shown = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const starred = s => /[*✱]/.test(s);
  const CONTROLS = 'input:not([type=hidden]), textarea, select, button[aria-pressed]';
  const escape = s => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  // The wording around a control that is not the control's own text: the question it belongs to.
  // `mine` are the elements of this control; the search stops at a block that holds another question.
  // Text with a control's own option wording taken out of it.
  const without = (text, own) => {
    let t = clean(text);
    // Buttons side by side read as one word ("YesNo"), so the options are also tried joined together.
    for (const piece of [own.join(''), own.join(' '), ...own]) if (piece) t = clean(t.replace(new RegExp('(^|\\s)' + escape(piece) + '(?=\\s|$)', 'g'), ' '));
    return clean(t.replace(/select\s?(\.\.\.|…)?\s*$/i, ''));
  };
  const around = (el, own, levels, mine) => {
    for (let p = el.parentElement, i = 0; p && i < levels; p = p.parentElement, i++) {
      if ([...p.querySelectorAll(CONTROLS)].some(c => shown(c) && !mine.includes(c))) return '';
      const t = without(p.innerText, own);
      if (t.length >= 3 && t.length <= 300 && !/^select\b/i.test(t)) return t;
    }
    return '';
  };
  // Some forms mark a required question with a class on its label instead of an asterisk.
  const flagged = (el, levels) => {
    for (let p = el.parentElement, i = 0; p && i < levels; p = p.parentElement, i++) {
      if ([...p.querySelectorAll(CONTROLS)].some(c => shown(c) && c !== el && c.name !== el.name && !p.querySelector('label'))) return false;
      const label = p.querySelector('label, legend');
      if (label) return /required/i.test(label.className.toString());
    }
    return false;
  };
  const fields = [];
  let n = 0;
  const add = (f, el) => { if (tag && el) el.setAttribute('data-jobhunt', String(n)); fields.push(Object.assign({n: n++}, f)); };
  const base = {tag: 'input', type: 'text', id: '', name: '', label: '', near: '', placeholder: '', combobox: false,
                required: false, empty: true, accept: '', options: [], kind: 'text', value: ''};

  // Text boxes, dropdowns, file uploads.
  for (const el of document.querySelectorAll('input, textarea, select')) {
    const type = (el.getAttribute('type') || el.type || 'text').toLowerCase();
    if (['hidden', 'submit', 'button', 'image', 'reset', 'search', 'checkbox', 'radio', 'password'].includes(type)) continue;
    if (el.disabled || el.readOnly || (type !== 'file' && !shown(el))) continue;
    let label = el.labels && el.labels.length ? el.labels[0].innerText : '';
    const by = el.getAttribute('aria-labelledby');
    if (!clean(label) && by) label = by.split(/\s+/).map(id => (document.getElementById(id) || {}).innerText || '').join(' ');
    if (!clean(label)) label = el.getAttribute('aria-label') || '';
    // Pick-from-a-list fields discard typed text. Lever's location box is one but does not say so.
    const combobox = el.getAttribute('role') === 'combobox' || el.hasAttribute('aria-autocomplete') || el.hasAttribute('list')
      || el.id === 'location-input';
    const isSelect = el.tagName === 'SELECT';
    const own = isSelect ? [...el.options].map(o => clean(o.text)) : [];
    label = without(label, own);  // a label wrapped around a dropdown contains the dropdown's options
    let value = type === 'file' ? ((el.files && el.files.length) ? el.files[0].name : '') : el.value;
    if (isSelect) value = el.selectedIndex > 0 ? clean(el.options[el.selectedIndex].text) : '';
    if (combobox && !value) {  // some dropdowns show the choice beside the input instead of in it
      for (let p = el.parentElement, i = 0; p && i < 4 && !value; p = p.parentElement, i++) {
        const chosen = [...p.querySelectorAll('[class*="single-value"], [class*="singleValue"], [class*="multi-value__label"]')];
        if (chosen.length) value = chosen.map(c => clean(c.innerText)).join('; ');
      }
    }
    const near = around(el, own, 4, [el]);
    add(Object.assign({}, base, {
      tag: el.tagName.toLowerCase(), type, id: el.id || '', name: el.getAttribute('name') || '',
      label: clean(label), near,
      placeholder: el.getAttribute('placeholder') || '',
      combobox, required: el.required || el.getAttribute('aria-required') === 'true' || starred(label)
        || (!clean(label) && starred(near)) || flagged(el, 3),
      empty: !value, value: clean(value), accept: el.getAttribute('accept') || '',
      options: isSelect ? [...el.options].map(o => clean(o.text)) : [],
      kind: type === 'file' ? 'file' : isSelect ? 'select' : combobox ? 'combobox' : 'text',
    }), el);
  }

  // Multiple choice: radio buttons and checkboxes that share a name are one question.
  const groups = new Map();
  for (const el of document.querySelectorAll('input[type=radio], input[type=checkbox]')) {
    const target = (el.labels && el.labels[0]) || el;
    if (el.disabled || !(shown(el) || shown(target))) continue;
    const key = el.type + ':' + (el.name || el.id);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(el);
  }
  for (const inputs of groups.values()) {
    const labels = inputs.map(el => clean(el.labels && el.labels[0] ? el.labels[0].innerText : el.value));
    const question = around(inputs[0], labels, 6, inputs);
    const single = inputs.length === 1 && inputs[0].type === 'checkbox';
    if (tag) inputs.forEach((el, i) => ((el.labels && el.labels[0]) || el).setAttribute('data-jobhunt-opt', n + ':' + i));
    const picked = labels.filter((_, i) => inputs[i].checked);
    add(Object.assign({}, base, {
      tag: 'group', type: inputs[0].type, name: inputs[0].name || '', label: single ? (question || labels[0]) : question,
      options: labels, required: inputs.some(el => el.required) || starred(question) || (single && starred(labels[0]))
        || flagged(inputs[0], 5), empty: !picked.length,
      value: picked.join('; '), kind: single ? 'consent' : inputs[0].type,
    }));
  }

  // Yes/No style buttons that stay pressed.
  const rows = new Map();
  for (const el of document.querySelectorAll('button[aria-pressed]')) {
    if (!shown(el) || el.disabled) continue;
    if (!rows.has(el.parentElement)) rows.set(el.parentElement, []);
    rows.get(el.parentElement).push(el);
  }
  for (const buttons of rows.values()) {
    if (buttons.length < 2) continue;
    const labels = buttons.map(el => clean(el.innerText));
    const question = around(buttons[0], labels, 5, buttons);
    if (tag) buttons.forEach((el, i) => el.setAttribute('data-jobhunt-opt', n + ':' + i));
    const picked = labels.filter((_, i) => buttons[i].getAttribute('aria-pressed') === 'true');
    add(Object.assign({}, base, {tag: 'group', type: 'buttons', label: question, options: labels,
        required: starred(question) || flagged(buttons[0], 4), empty: !picked.length, value: picked.join('; '), kind: 'buttons'}));
  }
  return {fields};
}
"""

# The options of an open dropdown: those in the list it controls, else whatever option-like items are showing.
OPTIONS_JS = r"""
(el) => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const list = document.getElementById(el.getAttribute('aria-controls') || el.getAttribute('aria-owns') || '');
  const found = [...(list || document).querySelectorAll('[role=option]')].filter(o => o.offsetParent !== null);
  found.forEach((o, i) => o.setAttribute('data-jobhunt-choice', String(i)));
  return found.map(o => clean(o.innerText));
}
"""

MARK_JS = """
([filled, left]) => {
  const paint = (selector, colour) => document.querySelectorAll(selector).forEach(el => {
    el.style.outline = `2px solid ${colour}`; el.style.outlineOffset = '2px';
  });
  filled.forEach(n => paint(`[data-jobhunt="${n}"], [data-jobhunt-opt^="${n}:"]`, '#2e9e5b'));
  left.forEach(n => paint(`[data-jobhunt="${n}"]`, '#e08a00'));
}
"""

# Sends the page's current answers to the tool whenever they change, so they can be remembered
# even though the form disappears when you press Submit.
WATCH_JS = "(() => { const collect = " + FORM_JS + """;
  let last = '';
  const send = () => {
    try {
      const answered = collect(false).fields.filter(f => f.value && f.kind !== 'file' && f.kind !== 'consent');
      const now = JSON.stringify(answered);
      if (now !== last && window.jobhuntAnswers) { last = now; window.jobhuntAnswers(now); }
    } catch (e) { }
  };
  setInterval(send, 1500);
  document.addEventListener('submit', send, true);
  document.addEventListener('click', send, true);
})();
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
    choice_questions: int = 0   # multiple-choice questions left for you


def describe(f: dict) -> str:
    """Everything we know about a control, lowercased, with name/id in brackets."""
    return f"{f['label'] or f['near']} {f['placeholder']} [{f['name']}] [{f['id']}]".lower()


def display_name(f: dict) -> str:
    """The question as the user sees it, or "" when the control has no visible wording."""
    text = f["label"] or f["near"] or f["placeholder"]
    return re.sub(r"\s*[*✱]\s*", " ", text).strip()[:70]


def question_of(f: dict) -> str:
    return re.sub(r"\s*[*✱]\s*", " ", f["label"] or f["near"]).strip()


def is_contact_field(f: dict) -> bool:
    text = describe(f)
    return f["type"] == "tel" or any(pattern.search(text) for _, pattern in RULES)


def saved_answer(f: dict, profile: Profile, answers: Answers | None) -> str | None:
    """Your saved answer to this question: one typed into profile.toml first, then answers.json."""
    question = question_of(f)
    for fragment, answer in profile.answers.items():
        if fragment in question.lower():
            return answer
    return answers.find(question) if answers and question else None


DATE_BOX = re.compile(r"\bdate\b|mm\s?/\s?dd|dd\s?/\s?mm|yyyy")


def choose_value(f: dict, profile: Profile, cover_letter: str = "", answers: Answers | None = None) -> str | None:
    """The text to type into a text field or textarea, or None to leave it for the user."""
    value = _choose_value(f, profile, cover_letter, answers)
    is_date_box = f["type"] in ("date", "month", "datetime-local") or DATE_BOX.search(f["placeholder"].lower())
    if value and is_date_box and not re.search(r"\d{1,4}[-/.]\d{1,2}", value):
        return None  # "Two weeks after an offer" is not something a date picker accepts
    return value


def _choose_value(f: dict, profile: Profile, cover_letter: str, answers: Answers | None) -> str | None:
    text = describe(f)
    if OTHER_PERSON.search(text):
        return None
    label = f["label"].lower()
    for question, answer in profile.answers.items():
        if question in label:
            return answer
    if f["tag"] == "textarea":
        if re.search(r"cover[ _-]?letter", text):
            return cover_letter or None
        return saved_answer(f, profile, answers)
    if f["type"] == "tel" and profile.phone:
        return profile.phone
    for key, pattern in RULES:
        if pattern.search(text):
            return getattr(profile, key) or None
    return saved_answer(f, profile, answers)


def choose_option(f: dict, profile: Profile, answers: Answers | None = None) -> str | None:
    """For a dropdown or multiple-choice question: the option that matches your saved answer."""
    if OTHER_PERSON.search(describe(f)):
        return None
    answer = saved_answer(f, profile, answers)
    return pick_option(answer, f["options"]) if answer else None


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


def fill_combobox(frame, target, answer: str) -> str | None:
    """Open a search-as-you-type dropdown and pick the option matching `answer`. Returns the option chosen."""
    target.click()
    for typed in ("", answer):
        if typed:
            target.fill(typed)
        choice = None
        for _ in range(8):  # options can take a moment to load
            frame.page.wait_for_timeout(250)
            options = target.evaluate(OPTIONS_JS)
            choice = pick_option(answer, options)
            if choice or (options and not typed):
                break
        if choice:
            frame.locator(f'[data-jobhunt-choice="{options.index(choice)}"]').first.click()
            return choice
    target.fill("")
    target.press("Escape")
    return None


def prefill(page, profile: Profile, cover_letter: str = "", cover_letter_file: Path | None = None,
            answers: Answers | None = None) -> Report:
    """Fill what we recognise on an already-loaded page. Never submits."""
    report = Report()
    for frame in page.frames:
        try:
            found = frame.evaluate(FORM_JS, True)
        except Exception:
            continue  # cross-origin widgets (captcha, analytics) that we have no business in
        filled, left = [], []
        for f in found["fields"]:
            target = frame.locator(f'[data-jobhunt="{f["n"]}"]')
            name = display_name(f)
            done = False
            try:
                if not f["empty"]:
                    continue  # already has a value (an earlier fill, browser autofill, the site's resume parser)
                if f["kind"] == "consent":
                    pass      # agreeing to something is yours to do
                elif f["kind"] == "file":
                    file = choose_file(f, profile.resume_pdf, cover_letter_file)
                    if not file and "autofill" in f"{f['label']} {f['near']}".lower():
                        continue  # the form's own "fill from resume" helper: nothing to do or to report
                    if file:
                        target.set_input_files(str(file))
                        kind = "Cover letter" if file == cover_letter_file else "Resume"
                        name, done = f"{kind} ({file.name})", True
                elif f["kind"] == "select":
                    option = choose_option(f, profile, answers)
                    if option:
                        target.select_option(label=option)
                        done = True
                elif f["tag"] == "group":
                    option = choose_option(f, profile, answers)
                    if option:
                        frame.locator(f'[data-jobhunt-opt="{f["n"]}:{f["options"].index(option)}"]').click()
                        done = True
                elif f["kind"] == "combobox":
                    answer = None if is_contact_field(f) else saved_answer(f, profile, answers)
                    if answer and not OTHER_PERSON.search(describe(f)):
                        done = bool(fill_combobox(frame, target, answer))
                else:
                    value = choose_value(f, profile, cover_letter, answers)
                    if value:
                        target.fill(value)
                        done = True
            except Exception:
                done = False
            if done:
                filled.append(f["n"])
                report.filled.append(name or f["name"] or f["id"])
            elif f["tag"] == "group":
                report.choice_questions += 1
                if f["required"] and name and name not in report.needs_you:
                    report.needs_you.append(name)
            elif f["required"] and name:
                left.append(f["n"])
                if name not in report.needs_you:
                    report.needs_you.append(name)
        try:
            frame.evaluate(MARK_JS, [filled, left])
        except Exception:
            pass
    return report


def answers_worth_keeping(fields: list[dict], profile: Profile) -> list[tuple[str, str]]:
    """From what was on a completed form: the (question, answer) pairs that are reusable elsewhere."""
    profile_values = {v.strip().lower() for v in (profile.first_name, profile.last_name, profile.full_name, profile.email,
                                                   profile.phone, profile.linkedin, profile.github, profile.website) if v}
    pairs = []
    for f in fields:
        question, answer = question_of(f), (f.get("value") or "").strip()
        if not question or not answer or f["kind"] in ("file", "consent"):
            continue
        if is_contact_field(f) or answer.lower() in profile_values or OTHER_PERSON.search(describe(f)):
            continue
        if f["kind"] == "text" and (f["tag"] == "textarea" or len(answer) > 120):
            continue  # an essay written for one employer
        if not re.search(r"[A-Za-z]", answer) and len(answer) < 6:
            continue  # a phone dialling code or similar fragment, not an answer
        pairs.append((question, answer))
    return pairs


def open_form(page, url: str) -> None:
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_selector("input, textarea", state="attached", timeout=30000)
    page.wait_for_timeout(1500)  # let the form finish rendering its custom questions
