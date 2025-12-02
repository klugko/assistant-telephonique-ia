from typing import Dict, List
from . import config
from .memory import memory

# Keywords de fin d'appel
GOODBYE = ("au revoir", "c'est tout", "merci c'est tout", "bye", "j'ai fini", "je n'ai plus de questions")

def should_hangup(text: str) -> bool:
    """Retourne True si l’utilisateur annonce la fin de l’appel."""
    t = text.lower()
    return any(p in t for p in GOODBYE)

def build_messages(history: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Construit le prompt (instructions + historique) pour le fallback HTTP."""
    enhanced_system = (
        config.SYSTEM_PROMPT
        + " Parle de façon naturelle, fluide et chaleureuse. Rythme modéré, phrases courtes, micro-pauses pertinentes."
    )
    return [{"role": "system", "content": enhanced_system}] + history

async def llm_reply_fallback(call_sid: str, user_text: str) -> str:
    """Produit une réponse textuelle via Chat Completions pour le mode fallback."""
    await memory.append(call_sid, "user", user_text)
    messages = build_messages(await memory.get(call_sid))
    completion = config.client.chat.completions.create(
        model=config.OPENAI_MODEL_FALLBACK,
        messages=messages,
        temperature=0.6,
        max_tokens=250,
    )
    answer = (completion.choices[0].message.content or "").strip()
    await memory.append(call_sid, "assistant", answer)
    return answer
