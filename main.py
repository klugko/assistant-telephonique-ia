import os
import json
import hmac
import time
import asyncio
import hashlib
import logging
import inspect
import re
from typing import Dict, List, Optional, Any

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

from twilio.twiml.voice_response import VoiceResponse, Gather
from twilio.request_validator import RequestValidator

from openai import OpenAI
import websockets

try:
    import redis.asyncio as aioredis  # type: ignore
except Exception:
    aioredis = None

# -----------------------------------------------------------------------------
# Config & Logging
# -----------------------------------------------------------------------------
load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("voice-app")

OPENAI_API_KEY         = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL_FALLBACK  = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
REALTIME_MODEL         = os.getenv("REALTIME_MODEL", "gpt-4o-realtime-preview-2024-10-01")
REALTIME_VOICE_DEFAULT = os.getenv("REALTIME_VOICE", "alloy")
REALTIME_VOICE_MALE    = os.getenv("REALTIME_VOICE_MALE", REALTIME_VOICE_DEFAULT)  # ex: "alloy"
REALTIME_VOICE_FEMALE  = os.getenv("REALTIME_VOICE_FEMALE", REALTIME_VOICE_DEFAULT)  # ex: "verse" si dispo
SYSTEM_PROMPT          = os.getenv("SYSTEM_PROMPT", "Tu es un assistant téléphonique utile, concis et professionnel.")
SPEECH_LANGUAGE        = os.getenv("SPEECH_LANGUAGE", "fr-FR")
TWILIO_VOICE_DEFAULT   = os.getenv("TWILIO_VOICE", "alice")  # "man" | "woman" | "alice"

VERIFY_TWILIO_SIGNATURE = os.getenv("VERIFY_TWILIO_SIGNATURE", "false").lower() == "true"
TWILIO_AUTH_TOKEN       = os.getenv("TWILIO_AUTH_TOKEN", "")

USE_REALTIME     = os.getenv("USE_REALTIME", "true").lower() == "true"
PUBLIC_BASE_URL  = os.getenv("PUBLIC_BASE_URL", "")
HMAC_SECRET      = (os.getenv("HMAC_SECRET") or TWILIO_AUTH_TOKEN or "change-me").encode("utf-8")
REDIS_URL: Optional[str] = os.getenv("REDIS_URL")

WELCOME_FILE_PATH = "assets/welcome.mp3"  # local file; served under /assets/welcome.mp3

if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY manquant")

client = OpenAI(api_key=OPENAI_API_KEY)

# -----------------------------------------------------------------------------
# Mémoire (Redis -> fallback mémoire locale)
# -----------------------------------------------------------------------------
class MemoryStore:
    def __init__(self) -> None:
        self._use_redis = False
        self._redis = None
        self._local_msgs: Dict[str, List[Dict[str, str]]] = {}
        self._local_kv: Dict[str, Dict[str, Any]] = {}

    async def init(self) -> None:
        if REDIS_URL and aioredis is not None:
            self._redis = aioredis.from_url(REDIS_URL, encoding="utf-8", decode_responses=True)
            try:
                await self._redis.ping()
                self._use_redis = True
                log.info("Redis connecté")
            except Exception:
                self._use_redis = False
                log.warning("Redis indisponible, fallback mémoire locale")

    # Messages
    async def get(self, call_sid: str) -> List[Dict[str, str]]:
        if self._use_redis:
            raw = await self._redis.get(f"call:{call_sid}:msgs")
            return json.loads(raw) if raw else []
        return self._local_msgs.get(call_sid, [])

    async def set(self, call_sid: str, messages: List[Dict[str, str]]) -> None:
        if self._use_redis:
            await self._redis.setex(f"call:{call_sid}:msgs", 3600, json.dumps(messages))
        else:
            self._local_msgs[call_sid] = messages

    async def append(self, call_sid: str, role: str, content: str, keep_last: int = 12) -> None:
        msgs = await self.get(call_sid)
        msgs.append({"role": role, "content": content})
        if len(msgs) > keep_last:
            msgs = msgs[-keep_last:]
        await self.set(call_sid, msgs)

    # KV (attributs par appel)
    async def get_attr(self, call_sid: str, key: str, default: Any = None) -> Any:
        if self._use_redis:
            raw = await self._redis.hget(f"call:{call_sid}:attr", key)
            return json.loads(raw) if raw is not None else default
        return self._local_kv.get(call_sid, {}).get(key, default)

    async def set_attr(self, call_sid: str, key: str, value: Any) -> None:
        if self._use_redis:
            await self._redis.hset(f"call:{call_sid}:attr", key, json.dumps(value))
            await self._redis.expire(f"call:{call_sid}:attr", 3600)
        else:
            d = self._local_kv.setdefault(call_sid, {})
            d[key] = value

    async def clear(self, call_sid: str) -> None:
        if self._use_redis:
            await self._redis.delete(f"call:{call_sid}:msgs")
            await self._redis.delete(f"call:{call_sid}:attr")
        else:
            self._local_msgs.pop(call_sid, None)
            self._local_kv.pop(call_sid, None)

memory = MemoryStore()

# -----------------------------------------------------------------------------
# Sécurité Twilio & utilitaires
# -----------------------------------------------------------------------------
async def validate_twilio_http(request: Request):
    if not VERIFY_TWILIO_SIGNATURE:
        return
    if not TWILIO_AUTH_TOKEN:
        raise HTTPException(status_code=500, detail="TWILIO_AUTH_TOKEN requis")
    validator = RequestValidator(TWILIO_AUTH_TOKEN)
    signature = request.headers.get("X-Twilio-Signature", "")
    url = str(request.url)
    form = await request.form()
    if not validator.validate(url, dict(form), signature):
        raise HTTPException(status_code=403, detail="Signature Twilio invalide")

def sign_ws_token(call_sid: str) -> str:
    ts = int(time.time())
    payload = f"{call_sid}.{ts}"
    sig = hmac.new(HMAC_SECRET, payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"

def verify_ws_token(token: str, max_age: int = 300) -> str:
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("Token mal formé")
    call_sid, ts_str, sig = parts
    data = f"{call_sid}.{ts_str}"
    expected = hmac.new(HMAC_SECRET, data.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        raise ValueError("HMAC invalide")
    ts = int(ts_str)
    if time.time() - ts > max_age:
        raise ValueError("Token expiré")
    return call_sid

def ws_public_url(request: Request) -> str:
    if PUBLIC_BASE_URL:
        return PUBLIC_BASE_URL.replace("https://", "wss://").replace("http://", "wss://") + "/media-stream"
    return f"wss://{request.url.hostname}/media-stream"

def http_base_url(request: Request) -> str:
    if PUBLIC_BASE_URL:
        return PUBLIC_BASE_URL
    # fallback best-effort
    scheme = "https" if request.url.scheme in ("http", "https") else "https"
    return f"{scheme}://{request.url.hostname}"

def twiml(xml: str) -> PlainTextResponse:
    return PlainTextResponse(xml, media_type="application/xml")

# -----------------------------------------------------------------------------
# LLM fallback (tour-par-tour)
# -----------------------------------------------------------------------------
GOODBYE = ("au revoir", "c'est tout", "merci c'est tout", "bye", "j'ai fini", "je n'ai plus de questions")

def should_hangup(text: str) -> bool:
    t = text.lower()
    return any(p in t for p in GOODBYE)

def build_messages(history: List[Dict[str, str]]) -> List[Dict[str, str]]:
    # Ajoute des consignes de prosodie pour plus de naturel côté fallback TwiML
    enhanced_system = (
        SYSTEM_PROMPT
        + " Parle de façon naturelle, fluide et chaleureuse. Rythme modéré, phrases courtes, micro-pauses pertinentes."
    )
    return [{"role": "system", "content": enhanced_system}] + history

async def llm_reply_fallback(call_sid: str, user_text: str) -> str:
    await memory.append(call_sid, "user", user_text)
    messages = build_messages(await memory.get(call_sid))
    completion = client.chat.completions.create(
        model=OPENAI_MODEL_FALLBACK,
        messages=messages,
        temperature=0.6,
        max_tokens=250,
    )
    answer = (completion.choices[0].message.content or "").strip()
    await memory.append(call_sid, "assistant", answer)
    return answer

# -----------------------------------------------------------------------------
# WebSocket compat pour OpenAI Realtime
# -----------------------------------------------------------------------------
def oai_ws_connect(url: str, headers: Dict[str, str]):
    params = inspect.signature(websockets.connect).parameters
    common = dict(ping_interval=20, ping_timeout=20, max_size=None)
    if "extra_headers" in params:
        return websockets.connect(url, extra_headers=headers, **common)
    if "additional_headers" in params:
        return websockets.connect(url, additional_headers=headers, **common)
    if "headers" in params:
        return websockets.connect(url, headers=headers, **common)
    return websockets.connect(url, **common)

# -----------------------------------------------------------------------------
# Détection commandes voix (fallback TwiML)
# -----------------------------------------------------------------------------
_CMD_FEMALE = re.compile(r"(voix\s+(de\s+)?femme|voix\s+féminine|voix\s+feminine|female voice)", re.I)
_CMD_MALE   = re.compile(r"(voix\s+(d['e]\s+)?homme|voix\s+masculine|male voice)", re.I)

def parse_gender_command(text: str) -> Optional[str]:
    if _CMD_FEMALE.search(text):
        return "female"
    if _CMD_MALE.search(text):
        return "male"
    return None

def twilio_voice_for_gender(gender: Optional[str]) -> str:
    if gender == "male":
        return "man"
    if gender == "female":
        return "woman"
    return TWILIO_VOICE_DEFAULT

def realtime_voice_for_gender(gender: Optional[str]) -> str:
    if gender == "male":
        return REALTIME_VOICE_MALE
    if gender == "female":
        return REALTIME_VOICE_FEMALE
    return REALTIME_VOICE_DEFAULT

# -----------------------------------------------------------------------------
# FastAPI
# -----------------------------------------------------------------------------
app = FastAPI(title="Voice AI Assistant — Realtime + Fallback")
app.mount("/assets", StaticFiles(directory="assets"), name="assets")  # sert welcome.mp3

@app.on_event("startup")
async def _startup():
    await memory.init()
    if not os.path.exists(WELCOME_FILE_PATH):
        log.warning("Le fichier de bienvenue %s est manquant. Placez-le avant la mise en prod.", WELCOME_FILE_PATH)

@app.get("/healthz")
async def healthz():
    return {
        "status": "ok",
        "realtime": USE_REALTIME,
        "websockets": getattr(websockets, "__version__", "unknown"),
        "assets": os.path.exists(WELCOME_FILE_PATH),
    }

# 1) Entrée d'appel
@app.post("/incoming-call", response_class=PlainTextResponse)
async def incoming_call(request: Request, _=Depends(validate_twilio_http)):
    form = await request.form()
    call_sid = form.get("CallSid", "unknown")

    base_url = http_base_url(request)
    welcome_url = f"{base_url}/assets/welcome.mp3"

    vr = VoiceResponse()
    # 1. Jouer le message de bienvenue MP3
    vr.play(welcome_url)

    if USE_REALTIME:
        # 2. Connexion au Realtime OpenAI via Twilio <Connect><Stream>
        token = sign_ws_token(call_sid)
        wss_url = ws_public_url(request)
        with vr.connect() as conn:
            stream = conn.stream(url=wss_url)
            stream.parameter(name="tok", value=token)
        return twiml(str(vr))

    # Fallback tour-par-tour
    vr.redirect("/twilio/voice")
    return twiml(str(vr))

# 2) WebSocket Twilio <-> OpenAI Realtime
@app.websocket("/media-stream")
async def media_stream(ws: WebSocket):
    await ws.accept()
    stream_sid: Optional[str] = None
    authed = False
    call_sid: Optional[str] = None
    current_gender: Optional[str] = None  # "male" | "female" | None

    oai_url = f"wss://api.openai.com/v1/realtime?model={REALTIME_MODEL}"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "OpenAI-Beta": "realtime=v1"}

    try:
        conn = oai_ws_connect(oai_url, headers)
        async with conn as oai_ws:
            # Configuration session initiale + tool-calling
            enhanced_instructions = (
                SYSTEM_PROMPT
                + " Parle de façon naturelle, fluide et chaleureuse, avec un débit modéré et des micro-pauses."
                + " Si l'utilisateur demande de changer de voix (homme/femme), utilise l'outil set_voice sans poser de questions."
            )
            await oai_ws.send(json.dumps({
                "type": "session.update",
                "session": {
                    "voice": REALTIME_VOICE_DEFAULT,
                    "modalities": ["text", "audio"],
                    "instructions": enhanced_instructions,
                    "turn_detection": {"type": "server_vad"},
                    "input_audio_format": "g711_ulaw",
                    "output_audio_format": "g711_ulaw",
                    "temperature": 0.65,
                    "tools": [
                        {
                            "type": "function",
                            "name": "set_voice",
                            "description": "Change la voix de sortie à masculine (male) ou féminine (female).",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "gender": {
                                        "type": "string",
                                        "enum": ["male", "female"]
                                    }
                                },
                                "required": ["gender"]
                            }
                        }
                    ],
                    "tool_choice": "auto",
                },
            }))

            async def handle_function_call(evt: Dict[str, Any]):
                nonlocal current_gender
                name = evt.get("name")
                if name != "set_voice":
                    return
                args = evt.get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        args = {}
                gender = (args or {}).get("gender")
                if gender not in ("male", "female"):
                    return
                current_gender = gender
                # Màj de la voix Realtime
                new_voice = realtime_voice_for_gender(gender)
                await oai_ws.send(json.dumps({
                    "type": "session.update",
                    "session": {"voice": new_voice}
                }))
                # Persistance côté serveur
                if call_sid:
                    await memory.set_attr(call_sid, "realtime_gender", gender)
                # Confirmation vocale courte
                confirm = "Très bien, je passe à une voix féminine." if gender == "female" else "Très bien, je passe à une voix masculine."
                await oai_ws.send(json.dumps({
                    "type": "response.create",
                    "response": {
                        "modalities": ["audio"],
                        "instructions": confirm
                    }
                }))

            async def pump_twilio_to_openai():
                nonlocal stream_sid, authed, call_sid
                try:
                    while True:
                        msg = await ws.receive_text()
                        data = json.loads(msg)
                        etype = data.get("event")

                        if etype == "start":
                            stream_sid = data["start"]["streamSid"]
                            custom = data["start"].get("customParameters") or data["start"].get("custom_parameters") or {}
                            tok = custom.get("tok")
                            try:
                                if not tok:
                                    raise ValueError("Jeton manquant")
                                token_call_sid = verify_ws_token(tok)
                                start_call_sid = data["start"].get("callSid")
                                if start_call_sid and token_call_sid != start_call_sid:
                                    raise ValueError("Jeton/CallSid incohérents")
                                authed = True
                                call_sid = token_call_sid
                                log.info(f"WS auth OK callSid={token_call_sid}")
                                # Appliquer voix sauvegardée si existante
                                saved = await memory.get_attr(call_sid, "realtime_gender")
                                if saved in ("male", "female"):
                                    await oai_ws.send(json.dumps({
                                        "type": "session.update",
                                        "session": {"voice": realtime_voice_for_gender(saved)}
                                    }))
                            except Exception as e:
                                log.warning(f"WS auth failed: {e}")
                                await ws.close(code=1008, reason=f"auth failed: {e}")
                                try:
                                    await oai_ws.close()
                                except Exception:
                                    pass
                                return

                        elif etype == "media":
                            if not authed:
                                continue
                            payload = data["media"]["payload"]
                            await oai_ws.send(json.dumps({
                                "type": "input_audio_buffer.append",
                                "audio": payload
                            }))

                        elif etype == "stop":
                            if authed:
                                await oai_ws.send(json.dumps({"type": "input_audio_buffer.commit"}))

                except WebSocketDisconnect:
                    try:
                        await oai_ws.close()
                    except Exception:
                        pass
                except Exception as e:
                    log.exception(f"pump_twilio_to_openai error: {e}")
                    try:
                        await oai_ws.close()
                    except Exception:
                        pass

            async def pump_openai_to_twilio():
                try:
                    async for raw in oai_ws:
                        evt = json.loads(raw)
                        et = evt.get("type")
                        # Audio sortant → Twilio
                        if et == "response.audio.delta" and evt.get("delta") and stream_sid:
                            out = {"event": "media", "streamSid": stream_sid, "media": {"payload": evt["delta"]}}
                            await ws.send_json(out)
                        # Tool-calling (set_voice)
                        elif et in ("response.function_call", "function_call", "tool_call"):
                            await handle_function_call(evt)
                        # Optionnel : close propre quand terminé
                        elif et in ("response.completed", "response.error"):
                            # Rien de spécial ici; Twilio VAD gère les tours
                            pass
                except Exception as e:
                    log.exception(f"pump_openai_to_twilio error: {e}")
                    try:
                        await ws.close()
                    except Exception:
                        pass

            await asyncio.gather(pump_twilio_to_openai(), pump_openai_to_twilio())

    except Exception as e:
        log.exception(f"media_stream outer error: {e}")
        try:
            await ws.close()
        except Exception:
            pass

# -----------------------------------------------------------------------------
# Fallback HTTP (tour-par-tour)
# -----------------------------------------------------------------------------
@app.post("/twilio/voice", response_class=PlainTextResponse)
async def twilio_voice(request: Request, _=Depends(validate_twilio_http)):
    form = await request.form()
    call_sid = form.get("CallSid", "unknown")
    if not await memory.get(call_sid):
        await memory.set(call_sid, [])

    base_url = http_base_url(request)
    welcome_url = f"{base_url}/assets/welcome.mp3"

    vr = VoiceResponse()
    # Lire le welcome MP3
    vr.play(welcome_url)

    # Prompt initial
    greeting = ("Bonjour, ici l'assistant. Posez votre question après le bip. "
                "Dites « change ta voix en voix de femme » ou « voix d'homme » pour changer de voix. "
                "Dites « au revoir » pour terminer.")

    # Voix Twilio courante
    gender = await memory.get_attr(call_sid, "tts_gender", None)
    voice_cur = twilio_voice_for_gender(gender)

    gather = Gather(
        input="speech",
        action="/twilio/respond",
        method="POST",
        language=SPEECH_LANGUAGE,
        speech_timeout="auto"
    )
    gather.say(greeting, voice=voice_cur, language=SPEECH_LANGUAGE)
    vr.append(gather)
    vr.redirect("/twilio/voice")
    return twiml(str(vr))

@app.post("/twilio/respond", response_class=PlainTextResponse)
async def twilio_respond(request: Request, _=Depends(validate_twilio_http)):
    form = await request.form()
    call_sid = form.get("CallSid", "unknown")
    user_text = (form.get("SpeechResult") or "").strip()
    vr = VoiceResponse()

    # Voix Twilio courante
    gender = await memory.get_attr(call_sid, "tts_gender", None)
    voice_cur = twilio_voice_for_gender(gender)

    if not user_text:
        reprompt = "Je n’ai pas bien entendu. Pouvez-vous répéter ?"
        gather = Gather(input="speech", action="/twilio/respond", method="POST",
                        language=SPEECH_LANGUAGE, speech_timeout="auto")
        gather.say(reprompt, voice=voice_cur, language=SPEECH_LANGUAGE)
        vr.append(gather)
        vr.redirect("/twilio/voice")
        return twiml(str(vr))

    # Fin d'appel
    if should_hangup(user_text):
        vr.say("Très bien. Merci pour votre appel. Au revoir.", voice=voice_cur, language=SPEECH_LANGUAGE)
        vr.hangup()
        await memory.clear(call_sid)
        return twiml(str(vr))

    # Commande de changement de voix ?
    cmd_gender = parse_gender_command(user_text)
    if cmd_gender:
        await memory.set_attr(call_sid, "tts_gender", cmd_gender)
        # Confirmation
        voice_cur = twilio_voice_for_gender(cmd_gender)
        confirm = "D'accord, je passe à une voix féminine." if cmd_gender == "female" else "D'accord, je passe à une voix masculine."
        gather = Gather(input="speech", action="/twilio/respond", method="POST",
                        language=SPEECH_LANGUAGE, speech_timeout="auto")
        gather.say(confirm, voice=voice_cur, language=SPEECH_LANGUAGE)
        gather.say("Vous pouvez continuer.", voice=voice_cur, language=SPEECH_LANGUAGE)
        vr.append(gather)
        vr.redirect("/twilio/voice")
        return twiml(str(vr))

    # Réponse LLM fallback
    try:
        answer = await llm_reply_fallback(call_sid, user_text)
    except Exception:
        answer = "Désolé, une erreur est survenue. Pouvez-vous reformuler brièvement ?"

    gather = Gather(input="speech", action="/twilio/respond", method="POST",
                    language=SPEECH_LANGUAGE, speech_timeout="auto")
    gather.say(answer, voice=voice_cur, language=SPEECH_LANGUAGE)
    gather.say("Vous pouvez répondre quand vous êtes prêt.", voice=voice_cur, language=SPEECH_LANGUAGE)
    vr.append(gather)
    vr.redirect("/twilio/voice")
    return twiml(str(vr))

@app.post("/twilio/events")
async def twilio_events(request: Request, _=Depends(validate_twilio_http)):
    form = await request.form()
    call_sid = form.get("CallSid")
    call_status = form.get("CallStatus")
    if call_sid and call_status in {"completed", "canceled", "failed", "busy", "no-answer"}:
        await memory.clear(call_sid)
    return {"ok": True}
