"""
Dynamic Q&A generator for application form questions.

When the bot encounters a textarea/input with a question like
"Why are you a good fit?", it pipes the question + candidate profile
to Ollama and returns a tailored response.
"""

import logging
from typing import Optional

from ..config import PROFILE
from .ollama_client import OllamaClient, OllamaError

logger = logging.getLogger("barq.auto_applier.qa")

QA_SYSTEM = (
    "You are a professional job application assistant. "
    "Answer application questions concisely and compellingly "
    "using the candidate's real experience. Be truthful — never invent experience. "
    "Output ONLY the answer text, no preamble, no explanation."
)

QA_PROMPT = """\
CANDIDATE PROFILE:
Name: {name}
Education: {education}
Years of Experience: {yoe}
Technical Stack: {skills}
Experience Summary: {summary}

EXPERIENCE HIGHLIGHTS:
{experiences}

TARGETING: {seeking}

APPLICATION QUESTION:
{question}

JOB DESCRIPTION CONTEXT:
{job_context}

Write a professional, compelling answer (2-4 sentences). Be specific — reference
technologies from the candidate's stack and concrete achievements. Do NOT use
generic phrases like "I am passionate about" or "I am a team player".
"""


class QAGenerator:
    """Generates tailored answers to application form questions."""

    def __init__(self, ollama: Optional[OllamaClient] = None):
        self.ollama = ollama or OllamaClient()

    async def answer_question(
        self,
        question: str,
        job_context: str = "",
        max_length: int = 500,
    ) -> str:
        """Generate a tailored answer for an application form question.

        Args:
            question: The question text (e.g., "Why are you a good fit?")
            job_context: Optional job description text for context
            max_length: Max response characters

        Returns:
            Generated answer text.
        """
        # Format experiences
        exp_text = ""
        for exp in PROFILE.experiences:
            exp_text += f"• {exp['role']} @ {exp['company']} ({exp['period']}):\n"
            for h in exp["highlights"][:2]:
                exp_text += f"  - {h}\n"

        # Inject failure patterns from EvoMap (learn from past failures)
        failure_context = ""
        try:
            import importlib
            evo_mod = importlib.import_module("jobs.auto_applier.failure.evo_logger")
            evo = evo_mod.EvoLogger()
            failure_context = evo.get_llm_failure_context(max_chars=800)
        except ImportError:
            pass

        prompt = QA_PROMPT.format(
            name=PROFILE.full_name,
            education=PROFILE.education,
            yoe=PROFILE.years_of_experience,
            skills=", ".join(PROFILE.skills[:12]),
            summary=PROFILE.summary,
            experiences=exp_text[:2000],
            seeking=PROFILE.seeking,
            question=question.strip(),
            job_context=job_context.strip()[:1500],
        )

        # Append failure context if available
        if failure_context:
            prompt += f"\n\n{failure_context}"

        try:
            answer = await self.ollama.generate(
                prompt=prompt,
                system=QA_SYSTEM,
                temperature=0.3,
                max_tokens=max_length,
            )
            logger.info("Generated answer for: %s... (%d chars)", question[:50], len(answer))
            return answer
        except OllamaError as exc:
            logger.warning("Q&A generation failed: %s", exc)
            return ""

    async def answer_cover_letter(
        self,
        company: str,
        role: str,
        job_description: str,
    ) -> str:
        """Generate a targeted cover letter."""
        prompt = f"""Write a one-page cover letter for {PROFILE.full_name} applying for {role} at {company}.

RULES:
- Open with one specific reason this role fits (reference something concrete in the JD)
- Paragraph 1: most relevant experience (2-3 sentences, reference specific achievements)
- Paragraph 2: why this company specifically
- Close: clear ask for interview
- Tone: direct and confident
- Do NOT use: "I am excited to apply", "I am a team player", "I am passionate about"

CANDIDATE:
{PROFILE.summary}
Stack: {", ".join(PROFILE.skills[:10])}

JOB DESCRIPTION:
{job_description[:3000]}
"""
        return await self.ollama.generate(
            prompt=prompt,
            temperature=0.3,
            max_tokens=1024,
        )
