import os
import logging
from typing import Optional
from dotenv import load_dotenv
from openai import OpenAI

# Chargement env + logging
load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("voice-app")

# Configuration principale
OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL_FALLBACK: str = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
REALTIME_MODEL: str = os.getenv("REALTIME_MODEL", "gpt-4o-realtime-preview-2024-10-01")

REALTIME_VOICE_DEFAULT: str = os.getenv("REALTIME_VOICE", "alloy")
REALTIME_VOICE_MALE: str = os.getenv("REALTIME_VOICE_MALE", REALTIME_VOICE_DEFAULT)
REALTIME_VOICE_FEMALE: str = os.getenv("REALTIME_VOICE_FEMALE", REALTIME_VOICE_DEFAULT)

SYSTEM_PROMPT: str = os.getenv("SYSTEM_PROMPT", "Tu es un assistant téléphonique utile, concis et professionnel.")
SPEECH_LANGUAGE: str = os.getenv("SPEECH_LANGUAGE", "fr-FR")
TWILIO_VOICE_DEFAULT: str = os.getenv("TWILIO_VOICE", "alice")  # "man" | "woman" | "alice"

VERIFY_TWILIO_SIGNATURE: bool = os.getenv("VERIFY_TWILIO_SIGNATURE", "false").lower() == "true"
TWILIO_AUTH_TOKEN: str = os.getenv("TWILIO_AUTH_TOKEN", "")

USE_REALTIME: bool = os.getenv("USE_REALTIME", "true").lower() == "true"
PUBLIC_BASE_URL: str = os.getenv("PUBLIC_BASE_URL", "")
HMAC_SECRET: bytes = (os.getenv("HMAC_SECRET") or TWILIO_AUTH_TOKEN or "change-me").encode("utf-8")
REDIS_URL: Optional[str] = os.getenv("REDIS_URL")

WELCOME_FILE_PATH: str = "assets/welcome.mp3"

if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY manquant")

client = OpenAI(api_key=OPENAI_API_KEY)
