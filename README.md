# JARVIS AI

JARVIS AI is a Windows voice assistant built with Python. It combines local
rule-based commands with Gemini and Groq providers, and stores conversation
memory locally in SQLite.

## Setup

1. Create and activate a Python virtual environment.
2. Install dependencies:

   ```bash
   python -m pip install -r requirements.txt
   ```

3. Create a `.env` file with any available provider keys:

   ```dotenv
   GEMINI_API_KEY=your-gemini-key
   GROQ_API_KEY=your-groq-key
   ```

4. Run the assistant on Windows:

   ```bash
   python main.py
   ```

The application creates its configuration and SQLite memory database on first
run. These local runtime files are intentionally ignored by Git.

## Tests

```bash
pytest
```
