"""ADR files: unique, consecutive numbers, matching headings, citations that resolve (#58)."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ADR_DIR = ROOT / "adrs"
FILE_NAME = re.compile(r"ADR(\d{3})-[a-z0-9]+(?:-[a-z0-9]+)*\.md")
LINK = re.compile(r"adrs/(ADR\d{3}-[a-z0-9-]+\.md)")
CITATION = re.compile(r"\bADR(\d{3})\b")
STALE_SKILLS = ("orchestrate", "plan-story")
STALE_REFERENCE = re.compile(r"plan-story|skills/orchestrate/|board\.(?:sh|md)")


def adr_files() -> list[Path]:
    return sorted(ADR_DIR.glob("*.md"))


def citing_files() -> list[Path]:
    """Docs and code that may cite ADRs. ``plans/`` is a historical record and is skipped,
    and so is ``.claude/worktrees/`` (other branches' checkouts)."""
    files = [ROOT / "README.md", ROOT / "CLAUDE.md", *adr_files()]
    files += sorted((ROOT / "src").rglob("*.py"))
    for sub in ("agents", "skills", "sdlc"):
        files += sorted((ROOT / ".claude" / sub).rglob("*.md"))
    return [path for path in files if path.is_file()]


def number(path: Path) -> str:
    match = FILE_NAME.fullmatch(path.name)
    assert match, f"{path.name} is not named ADR<nnn>-<kebab-title>.md"
    return match.group(1)


def test_adr_file_names_follow_the_convention() -> None:
    assert adr_files()
    for path in adr_files():
        number(path)


def test_adr_numbers_are_unique_and_consecutive() -> None:
    numbers = sorted(int(number(path)) for path in adr_files())
    assert numbers == list(range(1, len(numbers) + 1))


@pytest.mark.parametrize("path", adr_files(), ids=lambda path: path.name)
def test_adr_heading_matches_file_name(path: Path) -> None:
    first = path.read_text(encoding="utf-8").splitlines()[0]
    assert first.startswith(f"# ADR{number(path)}: ")


def test_adr_links_point_to_existing_files() -> None:
    missing = [
        f"{path.relative_to(ROOT)}: {name}"
        for path in citing_files()
        for name in LINK.findall(path.read_text(encoding="utf-8"))
        if not (ADR_DIR / name).is_file()
    ]
    assert missing == []


def test_adr_citations_name_existing_adrs() -> None:
    existing = {number(path) for path in adr_files()}
    unknown = [
        f"{path.relative_to(ROOT)}: ADR{cited}"
        for path in citing_files()
        for cited in CITATION.findall(path.read_text(encoding="utf-8"))
        if cited not in existing
    ]
    assert unknown == []


def test_stale_pipeline_skills_are_gone() -> None:
    for name in STALE_SKILLS:
        assert not (ROOT / ".claude" / "skills" / name).exists()
    stale = [
        str(path.relative_to(ROOT))
        for path in citing_files()
        if STALE_REFERENCE.search(path.read_text(encoding="utf-8"))
    ]
    assert stale == []
