"""Loads config.toml, profile.toml, companies.txt and the resume text."""

from __future__ import annotations

import re
import tomllib
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCES = ("greenhouse", "lever", "ashby", "workday")


class ConfigError(Exception):
    """A problem the user can fix by editing a file; the message says which."""


@dataclass
class Search:
    titles: list[str] = field(default_factory=list)
    exclude_titles: list[str] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    max_age_days: int = 60


@dataclass
class Settings:
    search: Search
    min_score: int = 70
    model: str = "claude-opus-5-5"
    daily_limit: int = 25
    preferences: str = ""


@dataclass
class Profile:
    first_name: str
    last_name: str
    email: str
    phone: str = ""
    location: str = ""
    linkedin: str = ""
    github: str = ""
    website: str = ""
    current_company: str = ""
    current_title: str = ""
    resume_pdf: Path | None = None
    resume_text_file: Path | None = None
    answers: dict[str, str] = field(default_factory=dict)

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def city(self) -> str:
        """ "Columbus, OH" -> "Columbus", for forms that ask for the city on its own."""
        return self.location.split(",")[0].strip()


def _read_toml(path: Path, example: str) -> dict:
    if not path.exists():
        raise ConfigError(f"{path.name} not found. Run `python -m jobhunt init`, then edit {path.name} "
                          f"(it starts as a copy of {example}).")
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path.name} has a syntax error: {e}") from e


def load_settings(root: Path = ROOT) -> Settings:
    raw = _read_toml(root / "config.toml", "config.example.toml")
    s = raw.get("search", {})
    search = Search(
        titles=[t.lower() for t in s.get("titles", [])],
        exclude_titles=[t.lower() for t in s.get("exclude_titles", [])],
        locations=[t.lower() for t in s.get("locations", [])],
        keywords=[t.lower() for t in s.get("keywords", [])],
        max_age_days=int(s.get("max_age_days", 60)),
    )
    if not search.titles:
        raise ConfigError('config.toml: add at least one entry to [search] titles, e.g. titles = ["data analyst"].')
    scoring = raw.get("scoring", {})
    return Settings(
        search=search,
        min_score=int(scoring.get("min_score", 70)),
        model=scoring.get("model", "claude-opus-5-5"),
        daily_limit=int(raw.get("apply", {}).get("daily_limit", 25)),
        preferences=str(scoring.get("preferences", "")).strip(),
    )


def load_profile(root: Path = ROOT) -> Profile:
    raw = _read_toml(root / "profile.toml", "profile.example.toml")
    missing = [k for k in ("first_name", "last_name", "email") if not str(raw.get(k, "")).strip()]
    if missing:
        raise ConfigError(f"profile.toml: fill in {', '.join(missing)}.")

    def text(key: str) -> str:
        return str(raw.get(key, "")).strip()

    def path(key: str) -> Path | None:
        if not text(key):
            return None
        p = Path(text(key)).expanduser()
        return p if p.is_absolute() else root / p

    return Profile(
        first_name=text("first_name"), last_name=text("last_name"), email=text("email"),
        phone=text("phone"), location=text("location"), linkedin=text("linkedin"),
        github=text("github"), website=text("website"),
        current_company=text("current_company"), current_title=text("current_title"),
        resume_pdf=path("resume_pdf"), resume_text_file=path("resume_text"),
        answers={str(k).lower(): str(v) for k, v in raw.get("answers", {}).items()},
    )


def clean_resume_text(text: str) -> str:
    """Tidy text pulled out of a PDF: split ligatures ("staﬃng" -> "staffing"), collapse layout spacing."""
    text = unicodedata.normalize("NFKC", text)
    text = "\n".join(re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines())
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def load_resume_text(profile: Profile) -> str:
    """Resume as plain text for the model: the text file if given, else extracted from the PDF."""
    if profile.resume_text_file:
        if not profile.resume_text_file.exists():
            raise ConfigError(f"profile.toml: resume_text points to {profile.resume_text_file}, which does not exist.")
        text = profile.resume_text_file.read_text(encoding="utf-8").strip()
    elif profile.resume_pdf:
        if not profile.resume_pdf.exists():
            raise ConfigError(f"profile.toml: resume_pdf points to {profile.resume_pdf}, which does not exist.")
        try:
            from pypdf import PdfReader
        except ImportError as e:
            raise ConfigError("Reading the resume PDF needs pypdf: pip install -r requirements.txt") from e
        # Layout mode keeps each line together. The default mode returns some PDFs (Google Docs
        # exports, for one) as a single word per line.
        pages = PdfReader(str(profile.resume_pdf)).pages
        text = clean_resume_text("\n".join((page.extract_text(extraction_mode="layout") or "") for page in pages))
    else:
        raise ConfigError("profile.toml: set resume_pdf (and optionally resume_text).")
    if len(text) < 200:
        raise ConfigError("The resume text came out nearly empty (a scanned PDF has no text layer). "
                          "Save your resume as a text or Markdown file and set resume_text in profile.toml.")
    return text


def parse_company_line(line: str) -> tuple[str, str] | None:
    """`greenhouse:stripe  # note` -> ("greenhouse", "stripe"); blank or comment -> None."""
    line = line.split("#", 1)[0].strip()
    if not line:
        return None
    source, _, token = line.partition(":")
    if source not in SOURCES or not token.strip():
        raise ValueError(line)
    if source == "workday":
        from .sources import workday_token
        return source, workday_token(token)
    return source, token.strip()


def load_companies(root: Path = ROOT) -> list[tuple[str, str]]:
    path = root / "companies.txt"
    if not path.exists():
        raise ConfigError("companies.txt not found.")
    out: list[tuple[str, str]] = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            pair = parse_company_line(line)
        except ValueError:
            raise ConfigError(f"companies.txt line {n}: expected `greenhouse:name`, `lever:name`, `ashby:name` "
                              f"or `workday:host/site`, got {line.strip()!r}.") from None
        if pair and pair not in out:
            out.append(pair)
    return out
