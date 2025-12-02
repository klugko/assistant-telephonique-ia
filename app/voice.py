import re
from typing import Optional
from . import config

# Regex de détection de commandes vocales
_CMD_FEMALE = re.compile(r"(voix\s+(de\s+)?femme|voix\s+féminine|voix\s+feminine|female voice)", re.I)
_CMD_MALE   = re.compile(r"(voix\s+(d['e]\s+)?homme|voix\s+masculine|male voice)", re.I)

def parse_gender_command(text: str) -> Optional[str]:
    """Détecte une commande de changement de voix ('male'|'female') dans le texte."""
    if _CMD_FEMALE.search(text):
        return "female"
    if _CMD_MALE.search(text):
        return "male"
    return None

def twilio_voice_for_gender(gender: Optional[str]) -> str:
    """Mappe le genre détecté vers une voix Twilio ('man'|'woman'|voix par défaut)."""
    if gender == "male":
        return "man"
    if gender == "female":
        return "woman"
    return config.TWILIO_VOICE_DEFAULT

def realtime_voice_for_gender(gender: Optional[str]) -> str:
    """Mappe le genre vers l’ID de voix Realtime (male/female/default)."""
    if gender == "male":
        return config.REALTIME_VOICE_MALE
    if gender == "female":
        return config.REALTIME_VOICE_FEMALE
    return config.REALTIME_VOICE_DEFAULT
