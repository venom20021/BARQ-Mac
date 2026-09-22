"""
Candidate profile, credentials, and operational settings for the Auto Applier.

All sensitive values are loaded from the parent .env with sensible fallbacks.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_DOTENV = _PROJECT_ROOT / ".env"
load_dotenv(_DOTENV)


# ─── Hardcoded Profile (from your requirements) ─────────────────────────────

@dataclass
class CandidateProfile:
    """Your professional profile — hardcoded as specified."""
    full_name: str = "Sai Prabhat"
    email: str = "prabhatsai047@gmail.com"
    phone: str = "+919278011092"
    linkedin_url: str = "https://www.linkedin.com/in/saiprabhat/"
    github_url: str = "https://github.com/venom20021"
    education: str = "Bachelor of Computer Science, University of Windsor (Aug 2024)"
    years_of_experience: int = 3

    # Professional experience (hardcoded from your spec)
    current_ctc: int = 0          # Annual salary in local currency
    desired_salary: int = 0       # Desired annual salary
    notice_period_days: int = 30  # Notice period in days
    us_citizenship: str = "No"    # Visa status
    require_visa: str = "Yes"     # Sponsorship needed
    gender: str = "Prefer not to say"
    disability_status: str = "I don't have a disability"
    veteran_status: str = "I am not a protected veteran"

    # Professional summary for LLM prompts
    summary: str = (
        "Product-driven Software Engineer with experience building scalable "
        "backend architectures, distributed microservices, and high-availability "
        "systems. Skilled in full-stack development, cloud infrastructure, and "
        "performance optimization."
    )

    # Detailed experience for form Q&A and resume tailoring
    experiences: list[dict] = field(default_factory=lambda: [
        {
            "company": "Tech Verse Solutions",
            "role": "Software Engineer Intern",
            "period": "Jun 2024 – Dec 2024",
            "highlights": [
                "Architected distributed microservices for 50,000+ monthly users",
                "Engineered RESTful APIs with Python and Node.js, reducing latency by 25%",
                "Built batch-processing data pipelines on AWS (EC2, S3, DynamoDB), cutting costs by 18%",
            ],
        },
        {
            "company": "Coinmint",
            "role": "Fullstack Developer",
            "period": "Jun 2022 – Jul 2023",
            "highlights": [
                "Engineered backend APIs and optimized server-side data retrieval",
                "Integrated third-party services with secure end-to-end data flows",
                "Accelerated development by 20% using React and scalable backend integration",
            ],
        },
        {
            "company": "SpotLine",
            "role": "Fullstack Developer",
            "period": "Dec 2021 – Sep 2022",
            "highlights": [
                "Led development of 5+ large-scale web applications, increasing engagement by 20%",
                "Redesigned 10+ websites improving performance by 25% and mobile responsiveness by 30%",
                "Optimized Core Web Vitals, reducing page load time by 30%",
            ],
        },
    ])

    # Technical stack (for LLM to tailor answers)
    skills: list[str] = field(default_factory=lambda: [
        ".NET (CLR/BCL)", "C#", "Python", "Node.js", "TypeScript", "JavaScript",
        "React.js", "Angular", "HTML5", "CSS3",
        "AWS (EC2, S3, DynamoDB, Lambda)", "Docker", "Kubernetes", "CI/CD",
        "PostgreSQL", "MongoDB", "SQL", "REST APIs", "Microservices",
        "System Design", "OOP", "Agile/Scrum",
    ])

    seeking: str = (
        "Full-Stack Developer / Software Engineer roles. "
        "Priority locations: Italy, Luxembourg, Middle East (UAE, Saudi Arabia, Qatar), "
        "UK, US, Canada, then India. Open to remote, hybrid, or relocation."
    )
    not_suitable: str = "Pure frontend roles, non-technical management, junior/intern roles."
    relocation_note: str = (
        "Open to global opportunities. Priority: Italy, Luxembourg, Middle East, "
        "UK, US, Canada, then India. Experienced working remotely across time zones; "
        "willing to relocate for the right opportunity."
    )


# ─── Applier Settings ─────────────────────────────────────────────────────

@dataclass
class ApplierConfig:
    """Operational settings for the auto-apply engine."""

    # Browser
    headless: bool = os.getenv("BROWSER_HEADLESS", "false").lower() == "true"
    browser_type: str = "chromium"             # chromium | firefox | webkit
    slow_mo: int = 50                          # ms delay between actions (human-like)
    viewport_width: int = 1366
    viewport_height: int = 768

    # Session persistence
    storage_state_path: str = str(
        Path.home() / ".barq" / "linkedin_storage_state.json"
    )

    # LinkedIn credentials (from .env)
    linkedin_email: str = os.getenv("LINKEDIN_EMAIL", "")
    linkedin_password: str = os.getenv("LINKEDIN_PASSWORD", "")

    # Telegram (from .env)
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")
    telegram_webhook_url: str = os.getenv("TELEGRAM_WEBHOOK_URL", "")
    telegram_polling: bool = os.getenv("TELEGRAM_POLLING", "true").lower() == "true"

    # Ollama
    ollama_host: str = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")
    ollama_model: str = os.getenv("OLLAMA_MODEL", "llama3.2:3b")
    ollama_timeout: int = 60

    # TinyFish (for job discovery — from autopilot-jobhunt .env)
    tinyfish_api_key: str = os.getenv("TINYFISH_API_KEY", "")

    # Resume
    resume_pdf_path: str = os.getenv(
        "RESUME_PDF_PATH",
        str(_PROJECT_ROOT / "python" / "jobs" / "auto_applier" / "resume" / "Sai_Prabhat_Resume.pdf"),
    )

    # Ops
    match_threshold: int = 60                  # Minimum match score to consider
    max_applications_per_run: int = 10         # Max to process before cooldown
    pause_before_submit: bool = False          # If True, pause at final submit
    screenshot_on_error: bool = True
    interactive_mode: bool = False             # If True, pause on CAPTCHA for human

    # EvoMap failure logging
    evolution_log_path: str = str(
        _PROJECT_ROOT / "memory" / "evolution" / "error_log.json"
    )

    # Paths
    project_root: str = str(_PROJECT_ROOT)
    data_dir: str = str(_PROJECT_ROOT / "data" / "ingest" / "ai_chats")


# ─── Singleton instances ──────────────────────────────────────────────────

PROFILE = CandidateProfile()
CONFIG = ApplierConfig()
