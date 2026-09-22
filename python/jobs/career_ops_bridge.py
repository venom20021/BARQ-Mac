"""
Career-Ops Bridge — professional PDF generation via career-ops tools.

Supports two backends:
1. **Playwright** (HTML → PDF via generate-pdf.mjs) — uses ATS-friendly template
2. **LaTeX** (.tex → PDF via build-cv-latex.mjs + pdflatex) — full-page typesetting
"""

import asyncio
import ctypes
import json
import logging
import os
import re
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any


from utils import safe_filename


def _short_path(path: str) -> str:
    """Convert a Windows path to its short (8.3) form to avoid spaces breaking tools like pdflatex."""
    try:
        buf = ctypes.create_unicode_buffer(260)
        ctypes.windll.kernel32.GetShortPathNameW(path, buf, 260)
        short = buf.value
        return short if short else path
    except Exception:
        return path


def _mkdtemp_no_spaces(prefix: str = "brg-") -> str:
    """Create a temp directory whose path has no spaces (short form on Windows)."""
    raw = tempfile.mkdtemp(prefix=prefix)
    short = _short_path(raw)
    # If the short path still has spaces (unlikely), retry with a different location
    if " " in short:
        fallback = Path(os.environ.get("TEMP", "/tmp")) / prefix + uuid.uuid4().hex[:8]
        fallback.mkdir(parents=True, exist_ok=True)
        return str(fallback)
    return short

logger = logging.getLogger("barq.career_ops_bridge")

# ── Paths ────────────────────────────────────────────────────────────────────
CAREER_OPS_DIR = Path(os.environ.get(
    "CAREER_OPS_TOOL_PATH",
    os.path.expanduser("~/career-ops"),
))

FONTS_DIR = CAREER_OPS_DIR / "fonts"
PDF_SCRIPT = CAREER_OPS_DIR / "generate-pdf.mjs"
LATEX_BUILD_SCRIPT = CAREER_OPS_DIR / "build-cv-latex.mjs"
LATEX_TEMPLATE_PATH = CAREER_OPS_DIR / "templates" / "cv-template.tex"

# ── HTML template paths ──────────────────────────────────────────────────────
ATS_TEMPLATE_PATH = Path(__file__).parent.parent / "templates" / "ats-resume-template.html"
CO_TEMPLATE_PATH = CAREER_OPS_DIR / "templates" / "resume-template.html"

# ═══════════════════════════════════════════════════════════════════════════════
# JD-based relevance filtering (smart project/experience selection)
# ═══════════════════════════════════════════════════════════════════════════════


def _tokenize(text: str) -> set[str]:
    """Tokenize text into lowercase keywords, removing common stop words."""
    STOP_WORDS = {
        "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
        "of", "with", "by", "from", "as", "is", "was", "are", "were", "be",
        "been", "being", "have", "has", "had", "do", "does", "did", "will",
        "would", "could", "should", "may", "might", "shall", "can", "about",
        "into", "through", "during", "before", "after", "above", "below",
        "between", "out", "off", "over", "under", "again", "further", "then",
        "once", "here", "there", "when", "where", "why", "how", "all", "each",
        "every", "both", "few", "more", "most", "other", "some", "such", "no",
        "nor", "not", "only", "own", "same", "so", "than", "too", "very",
        "just", "because", "also", "its", "their", "our", "your", "his", "her",
    }
    # Extract meaningful words and common tech terms (e.g. "c#", ".net", "react.js")
    tokens = set()
    # Match words, hyphenated terms, and tech terms with dots/slashes
    for match in re.finditer(r"[a-zA-Z0-9][a-zA-Z0-9.#+\-/]*[a-zA-Z0-9+#]|[a-zA-Z0-9]{2,}", text):
        token = match.group().lower().strip(".-#")
        if token and token not in STOP_WORDS and len(token) > 1:
            tokens.add(token)
    return tokens


def _compute_relevance_score(text: str, job_description: str) -> float:
    """Score how relevant a piece of text is to the job description (0.0–1.0).

    Uses weighted keyword overlap: technical/domain terms get higher weight.
    """
    if not job_description or not text:
        return 0.5  # Neutral score when no JD to compare against

    jd_tokens = _tokenize(job_description)
    text_tokens = _tokenize(text)

    if not jd_tokens or not text_tokens:
        return 0.3

    # High-value tech/domain terms (weighted more heavily)
    HIGH_VALUE_PREFIXES = {
        "aws", "azure", "gcp", "cloud", "docker", "kubernetes", "k8s",
        "react", "angular", "vue", "node", "python", "java", "golang", "rust",
        "typescript", "javascript", "sql", "nosql", "mongodb", "postgres",
        "redis", "kafka", "rabbitmq", "graphql", "rest", "grpc", "microservice",
        "ci/cd", "pipeline", "devops", "mlops", "machine", "learning", "deep",
        "tensorflow", "pytorch", "llm", "ai", "genai", "langchain", "fastapi",
        "django", "flask", "next", "tailwind", "framer", "socket", "websocket",
        "distributed", "scalable", "high", "availability", "performance",
        "real", "time", "data", "pipeline", "etl", "analytics", "big",
        "testing", "deployment", "serverless", "lambda", "ec2", "s3", "dynamodb",
    }

    overlap = jd_tokens & text_tokens
    if not overlap:
        return 0.1

    # Compute weighted score
    total_weight = 0.0
    matched_weight = 0.0

    for token in jd_tokens:
        weight = 3.0 if token in HIGH_VALUE_PREFIXES else 1.0
        total_weight += weight
        if token in text_tokens:
            matched_weight += weight

    # Also compute simple Jaccard similarity
    jaccard = len(overlap) / len(jd_tokens | text_tokens) if (jd_tokens | text_tokens) else 0

    # Combined: 60% weighted overlap, 40% Jaccard
    weighted_score = matched_weight / total_weight if total_weight > 0 else 0
    combined = 0.6 * weighted_score + 0.4 * jaccard

    return min(1.0, max(0.0, combined))


def _filter_experience(
    experience: list[dict],
    job_description: str = "",
    max_entries: int = 3,
    max_bullets: int = 4,
) -> list[dict]:
    """Filter and score experience entries by JD relevance for 1-page resumes.

    When a job_description is provided, scores entries by keyword overlap
    and keeps only the most relevant ones (limited to max_entries).
    When no JD is given, returns ALL entries unchanged (backward compat).
    """
    if not experience:
        return []

    if not job_description:
        # No JD: return ALL entries unchanged (backward compatible)
        return [dict(e) for e in experience]

    # Score each experience entry by relevance
    scored = []
    for exp in experience:
        text = " ".join(filter(None, [
            exp.get("role", ""),
            exp.get("company", ""),
            *exp.get("bullets", []),
        ]))
        score = _compute_relevance_score(text, job_description)
        scored.append((score, exp))

    # Sort by relevance (descending)
    scored.sort(key=lambda x: x[0], reverse=True)

    # Take top entries, limit bullets
    result = []
    for score, entry in scored[:max_entries]:
        e = dict(entry)
        e["bullets"] = e.get("bullets", [])[:max_bullets]
        e["_relevance_score"] = round(score, 3)
        result.append(e)

    # Always keep at least 1 entry
    if not result and experience:
        e = dict(experience[0])
        e["bullets"] = e.get("bullets", [])[:max_bullets]
        result.append(e)

    return result


def _filter_projects(
    projects: list[dict],
    job_description: str = "",
    max_entries: int = 3,
    max_bullets: int = 3,
) -> list[dict]:
    """Filter and score project entries by JD relevance for 1-page resumes.

    When a job_description is provided, scores entries by keyword overlap
    and keeps only the most relevant ones (limited to max_entries).
    When no JD is given, returns ALL entries unchanged (backward compat).
    """
    if not projects:
        return []

    if not job_description:
        # No JD: return ALL entries unchanged (backward compatible)
        return [dict(p) for p in projects]

    # Score each project by relevance
    scored = []
    for proj in projects:
        text = " ".join(filter(None, [
            proj.get("name", ""),
            proj.get("description", ""),
        ]))
        score = _compute_relevance_score(text, job_description)
        scored.append((score, proj))

    # Sort by relevance (descending)
    scored.sort(key=lambda x: x[0], reverse=True)

    result = []
    for score, proj in scored[:max_entries]:
        p = dict(proj)
        p["_relevance_score"] = round(score, 3)
        result.append(p)

    return result


# ═══════════════════════════════════════════════════════════════════════════════
# Shared helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _h(text: str) -> str:
    """Escape text for safe inclusion in HTML."""
    text = str(text or "")
    text = text.replace("&", "&amp;")
    text = text.replace("<", "&lt;")
    text = text.replace(">", "&gt;")
    text = text.replace('"', "&quot;")
    text = text.replace("'", "&#39;")
    return text


def _extract_section_text(md: str, section_name: str) -> str:
    """Extract a markdown section's body text."""
    pat = re.compile(
        rf"(?im)^##{{1,3}}\s*{re.escape(section_name)}\s*\n([\s\S]*?)(?=\n##{{1,3}}\s|\Z)"
    )
    m = pat.search(md)
    return m.group(1).strip() if m else ""


def _parse_md_blocks(md_section: str) -> list[str]:
    """Split a section into job/project blocks separated by blank lines."""
    blocks = re.split(r"\n\n+(?=\S)", md_section.strip())
    return [b.strip() for b in blocks if b.strip()]


def _parse_job_block(block: str) -> dict[str, Any]:
    """Parse a single job entry from markdown into structured dict."""
    lines = [ln for ln in block.split("\n")]
    result = {"role": "", "company": "", "date_range": "", "bullets": [], "location": ""}

    for ln in lines:
        s = ln.strip().strip("*").strip()
        if s and not s.startswith("-") and not s.startswith("* ") and not s.startswith("["):
            for sep in [" -- ", " — ", " – ", " - ", " at ", " @ ", ", "]:
                if sep in s:
                    parts = s.split(sep, 1)
                    result["role"] = parts[0].strip()
                    result["company"] = parts[1].strip()
                    break
            if not result["role"]:
                result["role"] = s
            break

    date_pat = re.compile(
        r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+\d{4}"
        r"\s*(?:[-–to]+|–|—|to)\s*"
        r"(?:\w+\s+\d{4}|Present|Current|Now)",
        re.IGNORECASE,
    )
    for ln in lines[:4]:
        dm = date_pat.search(ln.strip())
        if dm:
            result["date_range"] = dm.group(0)
            break

    for ln in lines:
        s = ln.strip()
        if (s.startswith("* ") or s.startswith("- ")) and "**" not in s:
            result["bullets"].append(s[2:].strip())
        elif s.startswith("* **"):
            clean = re.sub(r"^\*\s*\*\*|\*\*\s*", "", s).strip()
            if clean:
                result["bullets"].append(clean)
    return result


def _parse_project_block(block: str) -> dict[str, Any]:
    """Parse a single project entry from markdown."""
    lines = [ln.strip() for ln in block.split("\n") if ln.strip()]
    result = {"name": "", "description": "", "technologies": []}

    for ln in lines:
        s = re.sub(r"^[-*+]\s+", "", ln).strip()
        bold_m = re.search(r"\*\*(.+?)\*\*", s)
        if bold_m and not result["name"]:
            result["name"] = bold_m.group(1)
            rest = re.sub(r"\*\*.+?\*\*\s*", "", s, count=1).strip()
            rest = re.sub(r"\[↗\]\([^)]+\)\s*", "", rest).strip()
            rest = re.sub(r"^--?\s*", "", rest).strip()
            if rest:
                result["description"] = rest
        elif not result["name"]:
            parts = s.split(" -- ", 1)
            result["name"] = parts[0].strip()
            if len(parts) > 1:
                result["description"] = parts[1].strip()
        else:
            if result["description"]:
                result["description"] += " " + s
            else:
                result["description"] = s
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# HTML builders
# ═══════════════════════════════════════════════════════════════════════════════

def _build_experience_html(content_md: str, parsed_experience: list[dict]) -> str:
    exp_md = _extract_section_text(content_md, "Work Experience")
    if not exp_md:
        exp_md = _extract_section_text(content_md, "Experience")
    if not exp_md:
        return _build_experience_from_parsed(parsed_experience)

    html_parts = []
    for block in _parse_md_blocks(exp_md):
        job = _parse_job_block(block)
        if not job["role"]:
            continue
        h = '<div class="job avoid-break">\n  <div class="job-header">\n'
        h += f'    <span class="job-company">{_h(job["company"] or job["role"])}</span>\n'
        if job["date_range"]:
            h += f'    <span class="job-period">{_h(job["date_range"])}</span>\n'
        h += '  </div>\n'
        if job["company"]:
            h += f'  <div class="job-role">{_h(job["role"])}</div>\n'
        if job["bullets"]:
            h += '  <ul>\n'
            for b in job["bullets"][:5]:
                h += f'    <li>{_h(b[:350])}</li>\n'
            h += '  </ul>\n'
        h += '</div>\n'
        html_parts.append(h)
    return "\n".join(html_parts)


def _build_experience_from_parsed(parsed: list[dict]) -> str:
    html_parts = []
    for exp in parsed:
        role = exp.get("role", "")
        company = exp.get("company", "")
        date_range = exp.get("date_range", "")
        bullets = exp.get("bullets", [])
        h = '<div class="job avoid-break">\n  <div class="job-header">\n'
        h += f'    <span class="job-company">{_h(company or role)}</span>\n'
        if date_range:
            h += f'    <span class="job-period">{_h(date_range)}</span>\n'
        h += '  </div>\n'
        if company:
            h += f'  <div class="job-role">{_h(role)}</div>\n'
        if bullets:
            h += '  <ul>\n'
            for b in bullets[:5]:
                h += f'    <li>{_h(b[:350])}</li>\n'
            h += '  </ul>\n'
        h += '</div>\n'
        html_parts.append(h)
    return "\n".join(html_parts)


def _build_projects_html(content_md: str, parsed_projects: list[dict]) -> str:
    proj_md = _extract_section_text(content_md, "Projects")
    if not proj_md:
        return _build_projects_from_parsed(parsed_projects)

    html_parts = []
    for block in _parse_md_blocks(proj_md):
        proj = _parse_project_block(block)
        if not proj["name"]:
            continue
        h = '<div class="project avoid-break">\n'
        h += f'  <div class="project-title">{_h(proj["name"])}</div>\n'
        if proj["description"]:
            h += f'  <div class="project-desc">{_h(proj["description"][:350])}</div>\n'
        if proj["technologies"]:
            h += f'  <div class="project-tech">{_h(", ".join(proj["technologies"]))}</div>\n'
        h += '</div>\n'
        html_parts.append(h)
    return "\n".join(html_parts)


def _build_projects_from_parsed(parsed: list[dict]) -> str:
    html_parts = []
    for proj in parsed:
        name = proj.get("name", "")
        desc = proj.get("description", "")
        if not name:
            continue
        h = '<div class="project avoid-break">\n'
        h += f'  <div class="project-title">{_h(name)}</div>\n'
        if desc:
            h += f'  <div class="project-desc">{_h(desc[:350])}</div>\n'
        h += '</div>\n'
        html_parts.append(h)
    return "\n".join(html_parts)


def _build_education_html(content_md: str, parsed_education: list[dict]) -> str:
    edu_md = _extract_section_text(content_md, "Education")
    if edu_md:
        lines = [
            ln.strip().lstrip("-*").strip()
            for ln in edu_md.split("\n") if ln.strip()
        ]
        parts = []
        for line in lines:
            if line and "**" not in line and not line.startswith("["):
                parts.append(
                    f'<div class="edu-item avoid-break"><div class="edu-header">'
                    f'<span class="edu-title">{_h(line)}</span></div></div>'
                )
        if parts:
            return "\n".join(parts)

    parts = []
    for edu in parsed_education:
        title = edu.get("title", "")
        details = edu.get("details", [])
        if title:
            h = '<div class="edu-item avoid-break">\n  <div class="edu-header">\n'
            h += f'    <span class="edu-title">{_h(title)}</span>\n'
            if details:
                h += f'    <span class="edu-org">{_h(" | ".join(details[:2]))}</span>\n'
            h += '  </div>\n</div>\n'
            parts.append(h)
    return "\n".join(parts)


# ═══════════════════════════════════════════════════════════════════════════════
# Main bridge class
# ═══════════════════════════════════════════════════════════════════════════════

class CareerOpsBridge:
    """
    Bridge to external career-ops tools for professional PDF generation.

    Supports two PDF backends:
    - ``"playwright"`` — HTML → PDF via generate-pdf.mjs (Playwright/Chromium)
    - ``"latex"``     — .tex → PDF via build-cv-latex.mjs + pdflatex (MiKTeX)
    """

    def __init__(self):
        self._available = self._check_available()

    # ── Process helpers ──────────────────────────────────────────────────

    @staticmethod
    def _find_on_path_or_common(name: str, common_paths: list[str]) -> str | None:
        node_path = shutil.which(name)
        if node_path:
            return node_path
        for p in common_paths:
            if os.path.isfile(p):
                return p
        return None

    @staticmethod
    def _find_node() -> str | None:
        return CareerOpsBridge._find_on_path_or_common("node", [
            r"C:\Program Files\nodejs\node.exe",
            r"C:\Program Files (x86)\nodejs\node.exe",
            os.path.expanduser(r"~\AppData\Roaming\npm\node.exe"),
            os.path.expanduser(r"~\AppData\Local\Programs\nodejs\node.exe"),
        ])

    @staticmethod
    def _find_pdflatex() -> str | None:
        return CareerOpsBridge._find_on_path_or_common("pdflatex", [
            r"C:\Program Files\MiKTeX\miktex\bin\x64\pdflatex.exe",
            r"C:\Program Files (x86)\MiKTeX\miktex\bin\pdflatex.exe",
            os.path.expanduser(r"~\AppData\Local\Programs\MiKTeX\miktex\bin\x64\pdflatex.exe"),
            os.path.expanduser(r"~\AppData\Local\Programs\MiKTeX\miktex\bin\pdflatex.exe"),
        ])

    # ── Availability check ───────────────────────────────────────────────

    def _check_available(self) -> bool:
        checks = [
            ("tool dir", CAREER_OPS_DIR.exists()),
            ("ats template", ATS_TEMPLATE_PATH.exists()),
            ("pdf script", PDF_SCRIPT.exists()),
            ("latex script", LATEX_BUILD_SCRIPT.exists()),
            ("latex template", LATEX_TEMPLATE_PATH.exists()),
            ("fonts", FONTS_DIR.exists() and any(FONTS_DIR.iterdir())),
        ]
        if not all(ok for _, ok in checks):
            missing = [n for n, ok in checks if not ok]
            print(f"[CareerOpsBridge] Missing: {', '.join(missing)}")
            logger.info("Career-Ops not available: %s", ", ".join(missing))
            return False
        print(f"[CareerOpsBridge] Available at {CAREER_OPS_DIR}")
        logger.info("Career-Ops bridge: available at %s", CAREER_OPS_DIR)
        return True

    @property
    def is_available(self) -> bool:
        return self._available

    # ── HTML generation (for Playwright backend) ─────────────────────────

    def build_html(
        self,
        resume_data: dict[str, Any],
        content_md: str | None = None,
        job_description: str = "",
    ) -> str:
        """Fill the ATS-friendly HTML template with resume data.

        When a job_description is provided, intelligently filters projects
        and experience to keep only the most relevant entries for a
        1-page ATS-friendly resume.
        """
        template = ATS_TEMPLATE_PATH.read_text(encoding="utf-8")
        raw_md = content_md or resume_data.get("raw_md", "")

        full_name = resume_data.get("full_name", "Sai Prabhat")
        email = resume_data.get("email", "")
        phone = resume_data.get("phone", "")
        linkedin_url = resume_data.get("linkedin_url", "")
        github_url = resume_data.get("github_url", "")
        portfolio_url = resume_data.get("portfolio_url", "") or github_url

        # Location
        location = ""
        loc_m = re.search(r"(?im)^\*\*Location:\*\*\s*(.+)", raw_md)
        if loc_m:
            location = loc_m.group(1).strip()
        if not location and resume_data.get("headline"):
            location = resume_data["headline"]

        linkedin_display = linkedin_url.replace("https://", "").replace("http://", "").rstrip("/")
        if linkedin_display.startswith("www."):
            linkedin_display = linkedin_display[4:]

        portfolio_display = ""
        if portfolio_url:
            portfolio_display = portfolio_url.replace("https://", "").replace("http://", "").rstrip("/")
        if portfolio_display.startswith("www."):
            portfolio_display = portfolio_display[4:]

        # Link labels: show "LinkedIn" and "GitHub"/"Portfolio" as clickable text
        is_github = 'github.com' in portfolio_url.lower()
        portfolio_label = 'GitHub' if is_github and github_url else ('Portfolio' if portfolio_url else '')

        # Summary
        summary = resume_data.get("summary", "")
        if not summary:
            sm = _extract_section_text(raw_md, "Professional Summary")
            if sm:
                summary = sm

        # Section content — filtered by JD relevance for 1-page output
        skills = resume_data.get("skills", [])
        filtered_experience = _filter_experience(
            resume_data.get("experience", []),
            job_description,
            max_entries=3,
            max_bullets=4,
        )
        filtered_projects = _filter_projects(
            resume_data.get("projects", []),
            job_description,
            max_entries=3,
            max_bullets=3,
        )
        experience_html = _build_experience_html(raw_md, filtered_experience)
        projects_html = _build_projects_html(raw_md, filtered_projects)
        education_html = _build_education_html(raw_md, resume_data.get("education", []))

        # Skills as inline text
        skill_parts = []
        for s in skills:
            if ":" in s:
                cat, items = s.split(":", 1)
                skill_parts.append(f"{cat.strip()}: {items.strip()}")
            else:
                skill_parts.append(s.strip())
        skills_text = " | ".join(skill_parts)

        # Separators
        def _between(a: str, b: str) -> str:
            return " | " if a and b else ""

        phone_sep = _between(phone, email or linkedin_url or portfolio_url or location)
        email_sep = _between(email, linkedin_url or portfolio_url or location)
        linkedin_sep = _between(linkedin_url, portfolio_url or location)
        portfolio_sep = _between(portfolio_url, location)

        replacements = {
            "{{NAME}}": _h(full_name),
            "{{PHONE}}": _h(phone),
            "{{PHONE_SEP}}": phone_sep,
            "{{EMAIL}}": _h(email),
            "{{EMAIL_SEP}}": email_sep,
            "{{LINKEDIN_URL}}": _h(linkedin_url),
            "{{LINKEDIN_SEP}}": linkedin_sep,
            "{{PORTFOLIO_URL}}": _h(portfolio_url),
            "{{PORTFOLIO_LABEL}}": _h(portfolio_label),
            "{{PORTFOLIO_SEP}}": portfolio_sep,
            "{{LOCATION}}": _h(location),
            "{{SUMMARY_TEXT}}": _h(summary),
            "{{SKILLS_TEXT}}": _h(skills_text),
            "{{EXPERIENCE}}": experience_html,
            "{{PROJECTS}}": projects_html,
            "{{EDUCATION}}": education_html,
        }
        for ph, val in replacements.items():
            template = template.replace(ph, val)
        return template

    # ── LaTeX JSON builder ────────────────────────────────────────────────

    @staticmethod
    def _build_json_for_latex(
        resume_data: dict[str, Any],
        content_md: str | None = None,
        job_description: str = "",
    ) -> dict[str, Any]:
        """
        Build the JSON data structure expected by build-cv-latex.mjs.

        When job_description is provided, filters projects/experience to
        keep only the most relevant entries for a 1-page ATS resume.
        """
        raw_md = content_md or resume_data.get("raw_md", "")
        full_name = resume_data.get("full_name", "Sai Prabhat")
        email = resume_data.get("email", "")
        phone = resume_data.get("phone", "")
        linkedin_url = resume_data.get("linkedin_url", "")
        github_url = resume_data.get("github_url", "")

        # Location for contact line
        location = ""
        loc_m = re.search(r"(?im)^\*\*Location:\*\*\s*(.+)", raw_md)
        if loc_m:
            location = loc_m.group(1).strip()

        contact_parts = [p for p in [phone, email, location] if p]
        contact_line = " | ".join(contact_parts)

        # Github display
        github_display = github_url.replace("https://", "").replace("http://", "").rstrip("/")
        if github_display.startswith("www."):
            github_display = github_display[4:]

        linkedin_display = linkedin_url.replace("https://", "").replace("http://", "").rstrip("/")
        if linkedin_display.startswith("www."):
            linkedin_display = linkedin_display[4:]

        # Education
        education = []
        parsed_edu = resume_data.get("education", [])
        for edu in parsed_edu:
            title = edu.get("title", "")
            details = edu.get("details", [])
            education.append({
                "institution": title,
                "location": details[0] if details else "",
                "degree": details[1] if len(details) > 1 else "",
                "dates": "",
                "coursework": details[2:] if len(details) > 2 else [],
            })
        if not education:
            edu_md = _extract_section_text(raw_md, "Education")
            if edu_md:
                for line in edu_md.split("\n"):
                    s = line.strip().lstrip("-*").strip()
                    if s and "**" not in s and not s.startswith("["):
                        education.append({
                            "institution": s,
                            "location": "", "degree": "",
                            "dates": "", "coursework": [],
                        })

        # Experience — filtered by JD relevance for 1-page output
        experience = []
        parsed_exp = _filter_experience(
            resume_data.get("experience", []),
            job_description,
            max_entries=3,
            max_bullets=4,
        )
        if parsed_exp:
            for exp in parsed_exp:
                experience.append({
                    "company": exp.get("company", ""),
                    "role": exp.get("role", ""),
                    "location": "",
                    "dates": exp.get("date_range", ""),
                    "bullets": exp.get("bullets", [])[:5],
                })
        else:
            exp_md = _extract_section_text(raw_md, "Work Experience")
            if not exp_md:
                exp_md = _extract_section_text(raw_md, "Experience")
            for block in _parse_md_blocks(exp_md):
                job = _parse_job_block(block)
                if job["role"]:
                    experience.append({
                        "company": job["company"],
                        "role": job["role"],
                        "location": job["location"],
                        "dates": job["date_range"],
                        "bullets": job["bullets"][:5],
                    })

        # Projects — filtered by JD relevance for 1-page output
        projects = []
        parsed_proj = _filter_projects(
            resume_data.get("projects", []),
            job_description,
            max_entries=3,
            max_bullets=3,
        )
        for proj in parsed_proj:
            projects.append({
                "name": proj.get("name", ""),
                "context": proj.get("description", "")[:100],
                "dates": "",
                "bullets": [proj.get("description", "")[:200]],
            })

        # Skills
        skills = []
        for s in resume_data.get("skills", []):
            if ":" in s:
                cat, items = s.split(":", 1)
                skills.append({
                    "category": cat.strip(),
                    "items": items.strip(),
                })
            else:
                skills.append({"category": "", "items": s.strip()})

        return {
            "name": full_name,
            "contact_line": contact_line,
            "email": {
                "url": f"mailto:{email}" if email else "",
                "display": email,
            },
            "linkedin": {
                "url": linkedin_url,
                "display": linkedin_display,
            },
            "github": {
                "url": github_url,
                "display": github_display,
            },
            "education": education,
            "experience": experience,
            "projects": projects,
            "skills": skills,
        }

    # ═════════════════════════════════════════════════════════════════════
    # PDF generation — Playwright backend
    # ═════════════════════════════════════════════════════════════════════

    async def _generate_via_playwright(
        self,
        resume_data: dict[str, Any],
        optimized_md: str | None,
        output_path: str | None,
        page_format: str,
        job_description: str = "",
    ) -> dict[str, Any]:
        """Generate PDF using Playwright (HTML → PDF via generate-pdf.mjs)."""
        node_exe = self._find_node()
        if node_exe is None:
            return {"status": "error", "message": "Node.js not found", "pdf_path": ""}

        html_content = self.build_html(resume_data, optimized_md, job_description=job_description)

        co_out_dir = CAREER_OPS_DIR / "output"
        co_out_dir.mkdir(parents=True, exist_ok=True)

        name_slug = safe_filename(resume_data.get("full_name", "Resume").replace(" ", "_"), max_len=40)
        temp_pdf = str(co_out_dir / f".bridge-pw-{uuid.uuid4().hex[:8]}.pdf")
        final_path = output_path or str(co_out_dir / f"{name_slug}_Resume.pdf")

        html_name = f".bridge-pw-{uuid.uuid4().hex[:8]}.html"
        html_path = str(CAREER_OPS_DIR / html_name)

        try:
            Path(html_path).write_text(html_content, encoding="utf-8")
            cmd = [
                node_exe, str(PDF_SCRIPT), html_path, temp_pdf,
                f"--format={page_format}", "--allow-reorder",
            ]
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=str(CAREER_OPS_DIR),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=45)

            if proc.returncode != 0:
                err = stderr.decode().strip() if stderr else f"Exit code {proc.returncode}"
                return {"status": "error", "message": err, "pdf_path": ""}
            if not Path(temp_pdf).is_file():
                return {"status": "error", "message": "PDF not created", "pdf_path": ""}

            Path(final_path).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(temp_pdf, final_path)
            os.unlink(temp_pdf)

            file_size = Path(final_path).stat().st_size
            page_count = 0
            pc_m = re.search(r"Pages:\s*(\d+)", stdout.decode())
            if pc_m:
                page_count = int(pc_m.group(1))

            return {
                "status": "completed", "pdf_path": final_path,
                "page_count": page_count, "file_size_bytes": file_size,
                "backend": "playwright",
            }
        except asyncio.TimeoutError:
            return {"status": "error", "message": "Timed out (45s)", "pdf_path": ""}
        except Exception as e:
            return {"status": "error", "message": str(e), "pdf_path": ""}
        finally:
            try:
                Path(html_path).unlink(missing_ok=True)
            except Exception:
                pass
            try:
                Path(temp_pdf).unlink(missing_ok=True)
            except Exception:
                pass

    # ═════════════════════════════════════════════════════════════════════
    # PDF generation — LaTeX backend
    # ═════════════════════════════════════════════════════════════════════

    async def _generate_via_latex(
        self,
        resume_data: dict[str, Any],
        optimized_md: str | None,
        output_path: str | None,
        page_format: str,
        job_description: str = "",
    ) -> dict[str, Any]:
        """Generate PDF using LaTeX (build-cv-latex.mjs + pdflatex)."""
        node_exe = self._find_node()
        if node_exe is None:
            return {"status": "error", "message": "Node.js not found", "pdf_path": ""}
        pdflatex_exe = self._find_pdflatex()
        if pdflatex_exe is None:
            return {"status": "error", "message": "pdflatex not found", "pdf_path": ""}

        # Build JSON data
        latex_data = self._build_json_for_latex(resume_data, optimized_md, job_description=job_description)

        co_out_dir = CAREER_OPS_DIR / "output"
        co_out_dir.mkdir(parents=True, exist_ok=True)

        name_slug = safe_filename(resume_data.get("full_name", "Resume").replace(" ", "_"), max_len=40)
        final_path = output_path or str(co_out_dir / f"{name_slug}_Resume.pdf")

        # Write temp files in a unique temp dir (no spaces — pdflatex breaks on space in paths)
        tmp_dir = Path(_mkdtemp_no_spaces(prefix="bridge-ltx-"))
        json_path = tmp_dir / "resume.json"
        tex_path = tmp_dir / "resume.tex"
        pdf_path = tmp_dir / "resume.pdf"

        try:
            # Write JSON input
            json_path.write_text(json.dumps(latex_data, indent=2), encoding="utf-8")

            # Step 1: build-cv-latex.mjs → .tex
            proc = await asyncio.create_subprocess_exec(
                node_exe, str(LATEX_BUILD_SCRIPT),
                str(json_path), str(tex_path),
                cwd=str(CAREER_OPS_DIR),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)

            if proc.returncode != 0:
                err = stderr.decode().strip() if stderr else f"Exit {proc.returncode}"
                return {"status": "error", "message": f"build-cv-latex failed: {err}", "pdf_path": ""}

            if not tex_path.is_file():
                return {"status": "error", "message": ".tex not created", "pdf_path": ""}

            # Step 2: pdflatex → .pdf (run twice for proper references)
            # Set MiKTeX to auto-install packages without GUI prompt
            env = os.environ.copy()
            env["MIKTEX_AUTOINSTALL"] = "1"
            for _ in range(2):
                proc = await asyncio.create_subprocess_exec(
                    pdflatex_exe,
                    "-interaction=nonstopmode", "-halt-on-error",
                    "--enable-installer",
                    f"-output-directory={tmp_dir}",
                    str(tex_path),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                )
                await asyncio.wait_for(proc.communicate(), timeout=120)

            if not pdf_path.is_file():
                # Check stderr for errors
                return {"status": "error", "message": "LaTeX PDF not generated", "pdf_path": ""}

            # Copy to final destination
            Path(final_path).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(pdf_path), final_path)

            file_size = Path(final_path).stat().st_size

            return {
                "status": "completed", "pdf_path": final_path,
                "page_count": 0, "file_size_bytes": file_size,
                "backend": "latex",
            }
        except asyncio.TimeoutError:
            return {"status": "error", "message": "LaTeX timed out", "pdf_path": ""}
        except Exception as e:
            return {"status": "error", "message": str(e), "pdf_path": ""}
        finally:
            # Clean up temp dir
            try:
                shutil.rmtree(str(tmp_dir))
            except Exception:
                pass

    # ═════════════════════════════════════════════════════════════════════
    # Public generation method
    # ═════════════════════════════════════════════════════════════════════

    async def generate_pdf(
        self,
        resume_data: dict[str, Any],
        optimized_md: str | None = None,
        output_path: str | None = None,
        page_format: str = "a4",
        backend: str = "latex",
        job_description: str = "",
    ) -> dict[str, Any]:
        """
        Generate a professional PDF via career-ops tools.

        When a job_description is provided, the bridge will intelligently
        select only the most relevant projects and experience entries
        to produce a focused 1-page ATS-friendly resume.

        Args:
            resume_data:     Parsed resume dict (from resume_parser).
            optimized_md:    Job-tailored markdown (from ResumeOptimizer).
            output_path:     Where to save the PDF. Auto-generated if None.
            page_format:     "a4" or "letter".
            backend:         ``"latex"`` (default, full-page typesetting) or
                            ``"playwright"`` (HTML → PDF via Chromium).
            job_description: The job description text used to filter
                            and rank projects/experience by relevance.

        Returns:
            Dict with status, pdf_path, page_count, file_size_bytes, backend.
        """
        if not self._available:
            return {"status": "error", "message": "Career-Ops dir not found", "pdf_path": ""}

        if backend == "latex":
            result = await self._generate_via_latex(
                resume_data, optimized_md, output_path, page_format,
                job_description=job_description,
            )
            if result.get("status") == "completed":
                return result
            # Fall through to Playwright if LaTeX fails
            print(f"[CareerOpsBridge] LaTeX failed ({result.get('message')}), falling back to Playwright")

        # Default/fallback: Playwright
        return await self._generate_via_playwright(
            resume_data, optimized_md, output_path, page_format,
            job_description=job_description,
        )
