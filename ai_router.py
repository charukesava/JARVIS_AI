import os
import traceback

from dotenv import load_dotenv

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")


class AIRouter:

    def __init__(self):

        self.provider = "gemini"

        self.gemini = None
        self.groq = None

        self.init_gemini()
        self.init_groq()

    #####################################################
    # Initialize Gemini
    #####################################################

    def init_gemini(self):

    print("\nInitializing Gemini...")

    if not GEMINI_API_KEY:
        print("❌ GEMINI_API_KEY not found")
        return

    try:

        import google.generativeai as genai

        genai.configure(api_key=GEMINI_API_KEY)

        self.gemini = genai.GenerativeModel(
            "gemini-2.0-flash"
        )

        print("✅ Gemini initialized successfully")

    except Exception:

        print("❌ Gemini initialization failed")

        traceback.print_exc()
    # Initialize Groq
    #####################################################

    def init_groq(self):

    print("\nInitializing Groq...")

    if not GROQ_API_KEY:
        print("❌ GROQ_API_KEY not found")
        return

    try:

        from groq import Groq

        self.groq = Groq(
            api_key=GROQ_API_KEY
        )

        print("✅ Groq initialized successfully")

    except Exception:

        print("❌ Groq initialization failed")

        traceback.print_exc()
    # Gemini
    #####################################################

    def ask_gemini(self, prompt):

        if self.gemini is None:
            raise Exception("Gemini unavailable")

        response = self.gemini.generate_content(prompt)

        return response.text

    #####################################################
    # Groq
    #####################################################

    def ask_groq(self, prompt):

        if self.groq is None:
            raise Exception("Groq unavailable")

        completion = self.groq.chat.completions.create(

            model="llama-3.3-70b-versatile",

            messages=[
                {
                    "role": "user",
                    "content": prompt
                }
            ]

        )

        return completion.choices[0].message.content

    #####################################################
    # Main Router
    #####################################################

    def ask(self, prompt):

        try:

            return self.ask_gemini(prompt)

        except Exception as gemini_error:

            print("Gemini failed:", gemini_error)

            try:

                return self.ask_groq(prompt)

            except Exception as groq_error:

                print("Groq failed:", groq_error)

                return "Sorry, both AI providers are unavailable."
