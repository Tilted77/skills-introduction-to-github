"""Your answers to the questions that come up on most applications.

Two kinds are kept in answers.json, next to your profile:
  standard  one answer per well-known question (work authorisation, sponsorship, address, the voluntary
            demographic questions...), matched to a form's wording by pattern;
  learned   answers you gave on a form, remembered by the exact question, and reused when that same
            question appears again.

Nothing here is guessed. A question with no saved answer is left for you.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

FILE = "answers.json"
YES_NO = ["Yes", "No"]
DECLINE = "Decline to answer"


@dataclass
class Slot:
    key: str
    ask: str                 # how the question is put to you
    pattern: str             # what it looks like on forms
    choices: list[str] = field(default_factory=list)
    us_only: bool = False    # do not reuse when the form asks about another country


SLOTS = [
    Slot("authorized_us", "Are you legally authorized to work in the United States?",
         r"(authori[sz]ed|eligible|entitled|legally able|legal right|right) to work|work authori[sz]ation|employment eligibility",
         YES_NO, us_only=True),
    Slot("sponsorship", "Will you now or in the future require visa sponsorship to work?", r"sponsor", YES_NO, us_only=True),
    Slot("relocate", "Are you willing to relocate?", r"relocat", YES_NO),
    Slot("onsite", "Are you able to work on site or hybrid if the job requires it?",
         r"(able|willing|comfortable) to (work|commute|be)\b[^?]*(office|on-?site|in[- ]person|hybrid)", YES_NO),
    Slot("travel", "Are you willing to travel for work?", r"(willing|able|open) to travel|travel up to|travel requirement", YES_NO),
    Slot("over18", "Are you at least 18 years old?", r"(at least|over|above) (the age of )?18|18 years (of age|old)|of legal age", YES_NO),
    Slot("worked_here_before", "Have you worked before for the employers you are applying to? (usually No)",
         r"(previously|ever|formerly|currently)[^?]{0,60}(worked|been employed|employed)|former (employee|intern|contractor)", YES_NO),
    Slot("noncompete", "Are you bound by a non-compete or similar agreement?", r"non-?compete|restrictive covenant|non-?solicit", YES_NO),
    Slot("background_check", "Will you consent to a background check if offered the job?", r"background check", YES_NO),
    Slot("how_heard", "How did you hear about the job? (a general answer, e.g. Company careers page)",
         r"how did you (hear|find|learn)|where did you (hear|find|learn)|how you heard|referral source"),
    Slot("salary", "What salary do you expect? (leave blank to answer on each form)",
         r"salary|compensation (expectation|requirement)|(desired|expected) (pay|compensation)|pay expectation"),
    Slot("start", "When could you start a new job? (e.g. Two weeks after an offer)",
         r"start date|when (can|could|would) you (be able to )?start|available to (start|begin)|notice period"),
    Slot("street", "Street address", r"address line 1|street address|^address\b|(mailing|home) address"),
    Slot("zip", "ZIP code", r"\bzip\b|postal code|post code"),
    Slot("state", "State you live in (full name, e.g. Ohio)", r"\bstate\b(?! (your|the|whether|if|of the art))|province|state/region"),
    Slot("country", "Country you live in", r"\bcountry\b"),
    Slot("school", "School or university", r"\bschool\b|university|college|institution"),
    Slot("degree", "Degree (e.g. Bachelor's Degree)", r"\bdegree\b"),
    Slot("major", "Major or field of study", r"discipline|\bmajor\b|field of study|area of study"),
    Slot("grad_year", "Graduation year", r"graduation (year|date)|year of graduation|end date year|year graduated"),
    Slot("pronouns", "Pronouns (leave blank to skip)", r"pronoun"),
    # The questions below are voluntary on every form. Leave any of them blank to answer on each form yourself.
    Slot("lgbt", "Voluntary: do you identify as LGBTQ+?", r"lgbt|sexual orientation", [*YES_NO, DECLINE]),
    Slot("gender", "Voluntary: gender", r"gender", ["Male", "Female", "Non-binary", DECLINE]),
    Slot("hispanic", "Voluntary: are you Hispanic or Latino?", r"hispanic|latin[oax]", [*YES_NO, DECLINE]),
    Slot("race", "Voluntary: race or ethnicity", r"\brace\b|ethnic",
         ["American Indian or Alaska Native", "Asian", "Black or African American", "Hispanic or Latino",
          "Native Hawaiian or Other Pacific Islander", "White", "Two or more races", DECLINE]),
    Slot("veteran", "Voluntary: veteran status", r"veteran|military",
         ["I am not a protected veteran", "I am a protected veteran", DECLINE]),
    Slot("disability", "Voluntary: do you have a disability?", r"disabilit", [*YES_NO, DECLINE]),
]
BY_KEY = {slot.key: slot for slot in SLOTS}

OTHER_COUNTRY = re.compile(r"\b(canada|united kingdom|uk|ireland|germany|france|india|australia|mexico|europe|eu|singapore|"
                           r"japan|netherlands|spain|brazil|china)\b")

# Different forms word the same choice differently; these group the wordings.
CATEGORIES = [
    ("decline", r"decline|prefer not|rather not|don.?t (wish|want)|do not (wish|want)|choose not|not to (say|answer|disclose|identify|specify)"),
    ("not_veteran", r"not a (protected )?veteran|no military|never served"),
    ("veteran", r"\b(am|identify as)\b.*veteran"),
    ("usa", r"^(united states|usa?|u\.s\.a?\.?)( of america)?\b"),
    ("male", r"^(cis(gender)? )?(male|man)\b"),
    ("female", r"^(cis(gender)? )?(female|woman)\b"),
    ("yes", r"^yes\b"),
    ("no", r"^no\b"),
]
STATES = {"alabama": "al", "alaska": "ak", "arizona": "az", "arkansas": "ar", "california": "ca", "colorado": "co",
          "connecticut": "ct", "delaware": "de", "florida": "fl", "georgia": "ga", "hawaii": "hi", "idaho": "id",
          "illinois": "il", "indiana": "in", "iowa": "ia", "kansas": "ks", "kentucky": "ky", "louisiana": "la",
          "maine": "me", "maryland": "md", "massachusetts": "ma", "michigan": "mi", "minnesota": "mn",
          "mississippi": "ms", "missouri": "mo", "montana": "mt", "nebraska": "ne", "nevada": "nv",
          "new hampshire": "nh", "new jersey": "nj", "new mexico": "nm", "new york": "ny", "north carolina": "nc",
          "north dakota": "nd", "ohio": "oh", "oklahoma": "ok", "oregon": "or", "pennsylvania": "pa",
          "rhode island": "ri", "south carolina": "sc", "south dakota": "sd", "tennessee": "tn", "texas": "tx",
          "utah": "ut", "vermont": "vt", "virginia": "va", "washington": "wa", "west virginia": "wv",
          "wisconsin": "wi", "wyoming": "wy"}


def normalize(text: str) -> str:
    """A question or option reduced to lowercase words, so small differences in punctuation do not matter."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9+ ]+", " ", text.lower().replace("’", "'"))).strip()


def category(text: str) -> str | None:
    text = text.lower().replace("’", "'").strip()
    return next((name for name, pattern in CATEGORIES if re.search(pattern, text)), None)


def pick_option(answer: str, options: list[str]) -> str | None:
    """The option on this form that means the same as the saved answer, or None when unsure."""
    wanted = normalize(answer)
    if not wanted:
        return None
    by_text = {normalize(o): o for o in reversed(options) if normalize(o)}
    if wanted in by_text:
        return by_text[wanted]
    if STATES.get(wanted) in by_text:
        return by_text[STATES[wanted]]
    kind = category(answer)
    if kind:
        same = [o for o in options if category(o) == kind]
        if same:
            return same[0]
        if kind in ("yes", "no", "decline"):
            return None  # never fall through to a loose match for a yes/no answer
    if len(wanted) >= 3:
        close = [o for text, o in by_text.items() if text.startswith(wanted) or (len(text) >= 3 and wanted.startswith(text))]
        if len(close) == 1:
            return close[0]
        inside = [o for text, o in by_text.items() if re.search(rf"(^| ){re.escape(wanted)}( |$)", text)]
        if len(inside) == 1:
            return inside[0]  # "(US) Ohio" for "Ohio"
    return None


def slot_for(label: str) -> Slot | None:
    """The standard question this label is asking, if any."""
    text = label.lower()
    if "sponsor" in text and re.search(BY_KEY["authorized_us"].pattern, text) and "without" not in text:
        return None  # two questions in one; either answer could be wrong
    for slot in SLOTS:
        if re.search(slot.pattern, text):
            if slot.us_only and OTHER_COUNTRY.search(text):
                return None
            return slot
    return None


class Answers:
    def __init__(self, root: Path):
        self.path = root / FILE
        self.standard: dict[str, str] = {}
        self.learned: dict[str, dict] = {}
        self.reviewed = False  # whether you have been through the standard questions once
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8-sig"))
            self.reviewed = bool(data.get("reviewed"))
            self.standard = {k: str(v) for k, v in data.get("standard", {}).items() if k in BY_KEY and str(v).strip()}
            self.learned = dict(data.get("learned", {}))

    def save(self) -> None:
        data = {"reviewed": self.reviewed, "standard": self.standard, "learned": self.learned}
        self.path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def find(self, label: str) -> str | None:
        """Your saved answer for a question worded like `label`. An exact remembered question wins."""
        known = self.learned.get(normalize(label))
        if known:
            return known["answer"]
        slot = slot_for(label)
        return self.standard.get(slot.key) if slot else None

    def unanswered(self) -> list[Slot]:
        return [slot for slot in SLOTS if slot.key not in self.standard]

    def learn(self, pairs: list[tuple[str, str]]) -> int:
        """Remember (question, answer) pairs from a form you completed. Returns how many were new or changed."""
        changed = 0
        for question, answer in pairs:
            key, answer = normalize(question), answer.strip()
            if not key or not answer:
                continue
            slot = slot_for(question)
            if slot and slot.key not in self.standard:
                self.standard[slot.key] = answer
                changed += 1
            saved = self.find(question)
            if saved != answer and not (saved and pick_option(saved, [answer])):  # not already covered
                self.learned[key] = {"question": question.strip(), "answer": answer}
                changed += 1
        if changed:
            self.save()
        return changed
