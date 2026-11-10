"""Provider routing for the assistant's online AI services."""

import logging
import os

from dotenv import load_dotenv

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
LOGGER = logging.getLogger(__name__)


class AIRouter:
    """Use Gemini first and fall back to Groq when necessary."""

    GEMINI_MODEL = "gemini-2.0-flash"
    GROQ_MODEL = "llama-3.3-70b-versatile"
    FALLBACK_RESPONSE = "Sorry, both AI providers are unavailable."

    def __init__(self):
        self.gemini = None
        self.groq = None
        self.init_gemini()
        self.init_groq()

    def init_gemini(self):
        if not GEMINI_API_KEY:
            LOGGER.info("Gemini is unavailable because GEMINI_API_KEY is not set.")
            return

        try:
            import google.generativeai as genai

            genai.configure(api_key=GEMINI_API_KEY)
            self.gemini = genai.GenerativeModel(self.GEMINI_MODEL)
            LOGGER.info("Gemini initialized successfully.")
        except Exception:
            LOGGER.exception("Gemini initialization failed.")

    def init_groq(self):
        if not GROQ_API_KEY:
            LOGGER.info("Groq is unavailable because GROQ_API_KEY is not set.")
            return

        try:
            from groq import Groq

            self.groq = Groq(api_key=GROQ_API_KEY)
            LOGGER.info("Groq initialized successfully.")
        except Exception:
            LOGGER.exception("Groq initialization failed.")

    def ask_gemini(self, prompt):
        if self.gemini is None:
            raise RuntimeError("Gemini is unavailable.")
        return self.gemini.generate_content(prompt).text

    def ask_groq(self, prompt):
        if self.groq is None:
            raise RuntimeError("Groq is unavailable.")

        completion = self.groq.chat.completions.create(
            model=self.GROQ_MODEL,
            messages=[{"role": "user", "content": prompt}],
        )
        return completion.choices[0].message.content

    def ask(self, prompt):
        try:
            return self.ask_gemini(prompt)
        except Exception:
            LOGGER.exception("Gemini request failed; trying Groq.")
            try:
                return self.ask_groq(prompt)
            except Exception:
                LOGGER.exception("Groq request failed.")
                return self.FALLBACK_RESPONSE
