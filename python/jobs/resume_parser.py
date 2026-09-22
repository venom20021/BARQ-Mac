"""
Resume Parser — reads a markdown resume file and extracts structured data.

Parses sections like name, email, phone, LinkedIn, GitHub, skills,
work experience (with bullet points), education, and projects.
"""

import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

# Default path for the resume file
DEFAULT_RESUME_PATH = os.path.join(
    os.environ.get("CAREER_OPS_PATH", os.path.join(os.path.expanduser("~"), "career-ops")),
    "cv.md"
)

# Cached result to avoid re-parsing
_parsed_cache: dict[str, Any] | None = None
_cache_path: str = ""
_cache_mtime: float = 0.0


def parse_resume(file_path: str | None = None) -> dict[str, Any]:
    """
    Read and parse a markdown resume file into structured JSON.

    Args:
        file_path: Path to the markdown resume file.
                   Defaults to CAREER_OPS_PATH/cv.md

    Returns:
        Dict with keys: full_name, email, phone, linkedin_url, github_url,
                        skills, experience, education, projects, raw_md
    """
    global _parsed_cache, _cache_path, _cache_mtime

    path = file_path or DEFAULT_RESUME_PATH

    # Return cached result if file hasn't changed
    if _parsed_cache and _cache_path == path and os.path.getmtime(path) == _cache_mtime:
        return _parsed_cache

    if not os.path.exists(path):
        return _empty_resume(path)

    raw_md = Path(path).read_text(encoding="utf-8")

    result = {
        "full_name": _extract_name(raw_md),
        "email": _extract_email(raw_md),
        "phone": _extract_phone(raw_md),
        "linkedin_url": _extract_url(raw_md, "linkedin"),
        "github_url": _extract_url(raw_md, "github"),
        "portfolio_url": _extract_url(raw_md, "portfolio") or _extract_url(raw_md, "website"),
        "headline": _extract_headline(raw_md),
        "summary": _extract_section(raw_md, "professional summary|summary|profile|about"),
        "skills": _extract_skills(raw_md),
        "experience": _extract_experience(raw_md),
        "education": _extract_education(raw_md),
        "projects": _extract_projects(raw_md),
        "certifications": _extract_certifications(raw_md),
        "raw_md": raw_md,
        "parsed_at": datetime.utcnow().isoformat(),
    }

    # Cache the result
    _parsed_cache = result
    _cache_path = path
    _cache_mtime = os.path.getmtime(path)

    return result


def _empty_resume(path: str) -> dict[str, Any]:
    """Return an empty resume structure when file is missing."""
    return {
        "full_name": "",
        "email": "",
        "phone": "",
        "linkedin_url": "",
        "github_url": "",
        "portfolio_url": "",
        "headline": "",
        "summary": "",
        "skills": [],
        "experience": [],
        "education": [],
        "projects": [],
        "certifications": [],
        "raw_md": "",
        "parsed_at": "",
        "_error": f"Resume file not found at: {path}",
    }


def _extract_name(md: str) -> str:
    """Extract the full name from the resume."""
    lines = md.strip().split("\n")
    # Usually the first non-empty, non-heading line is the name
    for line_text in lines[:5]:
        stripped = line_text.strip().strip("#").strip("*").strip()
        if stripped and len(stripped) < 60 and not stripped.startswith(("-", ">", "|")):
            # Check if it looks like a name (not an email, URL, etc.)
            if not re.match(r"^[\w.+-]+@[\w-]+\.[\w.]+$", stripped) and \
               not stripped.startswith("http") and \
               not stripped.startswith("@"):
                return stripped
    return ""


def _extract_email(md: str) -> str:
    """Extract email address from the resume."""
    match = re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", md)
    return match.group(0) if match else ""


def _extract_phone(md: str) -> str:
    """Extract phone number from the resume."""
    # Match various phone formats
    patterns = [
        r"(\+\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}",
        r"(\+\d{1,3}[-.\s]?)?\d{3}[-.\s]?\d{3}[-.\s]?\d{4}",
    ]
    for pattern in patterns:
        match = re.search(pattern, md)
        if match:
            return match.group(0)
    return ""


def _extract_url(md: str, service: str) -> str:
    """Extract a specific service URL (linkedin, github, portfolio, website)."""
    patterns = {
        "linkedin": r"https?://(www\.)?linkedin\.com/\S+",
        "github": r"https?://(www\.)?github\.com/\S+",
        "portfolio": r"https?://\S+portfolio\S+",
        "website": r"https?://\S+",
    }
    pattern = patterns.get(service)
    if not pattern:
        return ""

    # For generic website, find the first URL that isn't linkedin or github
    if service in ("portfolio", "website"):
        urls = re.findall(r"https?://[^\s)]+", md)
        for url in urls:
            if "linkedin" not in url and "github" not in url:
                return url
        return ""

    match = re.search(pattern, md, re.IGNORECASE)
    return match.group(0) if match else ""


def _extract_headline(md: str) -> str:
    """Extract the headline/tagline (often right after the name)."""
    raw_lines = [ln.strip() for ln in md.strip().split("\n") if ln.strip()]
    for i, line in enumerate(raw_lines):
        name = _extract_name(md)
        if name and name in line:
            # Next non-empty line after name might be the headline
            for next_line in raw_lines[i + 1:i + 4]:
                if next_line != name and \
                   not re.match(r"[\w.+-]+@[\w-]+\.[\w.]+", next_line) and \
                   not next_line.startswith("http") and \
                   not next_line.startswith("#") and \
                   len(next_line) < 100:
                    return next_line
    return ""


def _extract_section(md: str, section_names: str) -> str:
    """Extract content from a named section (case-insensitive).

    Stops at the next top-level (##) heading, allowing sub-headings
    (###) within the section (e.g., individual project or job entries).
    """
    pattern = rf"(?i)(?:^|\n)##\s*({section_names})\s*\n(.*?)(?=\n##\s|\Z)"
    match = re.search(pattern, md, re.DOTALL)
    if match:
        content = match.group(2).strip()
        return content.strip()
    return ""


def _extract_skills(md: str) -> list[str]:
    """Extract skills list from the resume."""
    # Try to find a Skills section
    section = _extract_section(md, "skills|technologies|tech stack|technical skills")
    if section:
        # Skills are often comma-separated or bullet points
        skills = re.split(r"[,;•|\n]", section)
        return [s.strip() for s in skills if s.strip() and len(s.strip()) > 1]

    # Fallback: look for inline skill mentions
    common_skills = [
        "python", "javascript", "typescript", "react", "node", "java", "c++", "go",
        "rust", "sql", "aws", "docker", "kubernetes", "git", "linux", "fastapi",
        "django", "flask", "machine learning", "ai", "data science", "cloud",
    ]
    found = []
    for skill in common_skills:
        if re.search(rf"\b{re.escape(skill)}\b", md, re.IGNORECASE):
            found.append(skill.title())
    return found


def _extract_experience(md: str) -> list[dict[str, Any]]:
    """Extract work experience entries."""
    section = _extract_section(md, "experience|work experience|employment|work history")
    if not section:
        return []

    entries = []
    # Split by job entries (each starts with a company/role name on its own line)
    blocks = re.split(r"\n\n+", section)

    for block in blocks:
        block = block.strip()
        if not block:
            continue

        lines = block.split("\n")
        # First line is usually the role + company (may have ### heading prefix)
        title_line = lines[0].strip().lstrip("-*+#").strip()

        # Try to parse: "Role at Company" or "Role — Company" or "Role, Company"
        role = title_line
        company = ""
        for sep in [" at ", " @ ", " — ", " – ", " - ", ", "]:
            if sep in title_line:
                parts = title_line.split(sep, 1)
                role = parts[0].strip()
                company = parts[1].strip()
                break

        # Extract date range
        date_line = ""
        date_pattern = r"(\w+\s+\d{4})\s*(?:[-–to]+|–|—)\s*(\w+\s+\d{4}|present|current|now)"
        for line in lines[1:4]:
            match = re.search(date_pattern, line, re.IGNORECASE)
            if match:
                date_line = line.strip()
                break

        # Extract bullet points
        bullets = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("-") or stripped.startswith("*") or stripped.startswith("+"):
                bullets.append(stripped.lstrip("-*+ ").strip())

        entries.append({
            "role": role,
            "company": company,
            "date_range": date_line,
            "bullets": bullets,
            "raw": block[:200],
        })

    return entries


def _extract_education(md: str) -> list[dict[str, Any]]:
    """Extract education entries."""
    section = _extract_section(md, "education|academic|qualifications")
    if not section:
        return []

    entries = []
    blocks = re.split(r"\n\n+", section)

    for block in blocks:
        block = block.strip()
        if not block:
            continue

        lines = [ln.strip().lstrip("-*+").strip() for ln in block.split("\n") if ln.strip()]
        title = lines[0] if lines else ""

        entries.append({
            "title": title,
            "details": lines[1:] if len(lines) > 1 else [],
        })

    return entries


def _extract_projects(md: str) -> list[dict[str, Any]]:
    """Extract project entries."""
    section = _extract_section(md, "projects|side projects|personal projects")
    if not section:
        return []

    entries = []
    blocks = re.split(r"\n\n+", section)

    for block in blocks:
        block = block.strip()
        if not block:
            continue

        # Each block starts with ### project-name; strip the ### and any markers
        lines = [ln.strip().lstrip("-*+#").strip() for ln in block.split("\n") if ln.strip()]
        title = lines[0] if lines else ""
        description = " ".join(lines[1:]) if len(lines) > 1 else ""

        entries.append({
            "name": title,
            "description": description,
        })

    return entries


def _extract_certifications(md: str) -> list[dict[str, Any]]:
    """Extract certification entries from the resume.

    Handles formats like:
      - **Cert Name** — Issuer (Date)
        Credential: link-or-id

      - **Cert Name** — Issuer (Date)
        Credential ID: ABC123
    """
    section = _extract_section(md, "certifications|certification|certs|credentials")
    if not section:
        return []

    entries = []
    # Each entry is a bullet point line (starting with - or *)
    # Possibly followed by an indented credential line
    lines = section.split("\n")

    i = 0
    while i < len(lines):
        line = lines[i].strip()
        i += 1

        # Look for bullet-pointed cert entries
        if not (line.startswith("-") or line.startswith("*")):
            continue

        # Remove bullet marker and strip
        content = line.lstrip("-* ").strip()
        if not content:
            continue

        # Remove bold markers (**)
        clean = content.replace("**", "")

        # Parse: "Cert Name — Issuer (Date)" or "Cert Name — Issuer (Date)"
        cert_name = clean
        issuer = ""
        date = ""
        credential = ""

        # Split on em-dash or hyphen-with-spaces
        for sep in [" — ", " – ", " - ", " -- "]:
            if sep in clean:
                parts = clean.split(sep, 1)
                cert_name = parts[0].strip()
                rest = parts[1].strip()
                # Extract date from parentheses: (Jun 2026)
                date_match = re.search(r"\(([^)]+)\)", rest)
                if date_match:
                    date = date_match.group(1).strip()
                    issuer = rest[:date_match.start()].strip()
                else:
                    issuer = rest
                break

        # Check next line for credential link/ID (indented, no bullet)
        if i < len(lines):
            next_line = lines[i].strip()
            if next_line and not next_line.startswith("-") and not next_line.startswith("*"):
                # Could be "Credential: link" or "Credential ID: ABC123" or just a URL
                if "credential" in next_line.lower() or "verify" in next_line.lower():
                    cred_match = re.search(r"https?://[^\s)]+", next_line)
                    if cred_match:
                        credential = cred_match.group(0)
                    else:
                        # Extract after the colon as raw text
                        col_match = re.search(r":\s*(.+)$", next_line)
                        if col_match:
                            credential = col_match.group(1).strip()
                    i += 1

        entries.append({
            "name": cert_name,
            "issuer": issuer,
            "date": date,
            "credential": credential,
        })

    return entries


def clear_parse_cache():
    """Clear the cached parsed resume."""
    global _parsed_cache, _cache_path, _cache_mtime
    _parsed_cache = None
    _cache_path = ""
