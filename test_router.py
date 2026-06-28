from ai_router import AIRouter

router = AIRouter()

print()

print("Gemini object :", router.gemini)

print("Groq object :", router.groq)

print()

print(router.ask("Hello!"))