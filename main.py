import os
import json
import hmac
import time
import asyncio
import hashlib
import logging
import inspect
from typing import Dict, List, Optional

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from dotenv import load_dotenv

from twilio.twiml.voice_response import VoiceResponse, Gather
from twilio.request_validator import RequestValidator

from openai import OpenAI
import websockets

try:
    import redis.asyncio as aioredis  # type: ignore
except Exception:
    aioredis = None

# -------------------------
# Config
# -------------------------
load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("voice-app")

OPENAI_API_KEY        = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL_FALLBACK = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
REALTIME_MODEL        = os.getenv("REALTIME_MODEL", "gpt-4o-realtime-preview-2024-10-01")
REALTIME_VOICE        = os.getenv("REALTIME_VOICE", "alloy")
SYSTEM_PROMPT         = os.getenv("SYSTEM_PROMPT", "Tu es un assistant téléphonique utile, concis et professionnel.")
SPEECH_LANGUAGE       = os.getenv("SPEECH_LANGUAGE", "fr-FR")
TWILIO_VOICE          = os.getenv("TWILIO_VOICE", "alice")

VERIFY_TWILIO_SIGNATURE = os.getenv("VERIFY_TWILIO_SIGNATURE", "false").lower() == "true"
TWILIO_AUTH_TOKEN       = os.getenv("TWILIO_AUTH_TOKEN", "")

USE_REALTIME    = os.getenv("USE_REALTIME", "true").lower() == "true"
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "")
HMAC_SECRET     = (os.getenv("HMAC_SECRET") or TWILIO_AUTH_TOKEN or "change-me").encode("utf-8")
REDIS_URL: Optional[str] = os.getenv("REDIS_URL")

if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY manquant")

client = OpenAI(api_key=OPENAI_API_KEY)

# -------------------------
# Mémoire fallback
# -------------------------
class MemoryStore:
    def __init__(self):
        self._use_redis = False
        self._redis = None
        self._local: Dict[str, List[Dict[str, str]]] = {}

    async def init(self) -> None:
        if REDIS_URL and aioredis is not None:
            self._redis = aioredis.from_url(REDIS_URL, encoding="utf-8", decode_responses=True)
            try:
                await self._redis.ping()
                self._use_redis = True
            except Exception:
                self._use_redis = False

    async def get(self, call_sid: str) -> List[Dict[str, str]]:
        if self._use_redis:
            raw = await self._redis.get(f"call:{call_sid}:msgs")
            return json.loads(raw) if raw else []
        return self._local.get(call_sid, [])

    async def set(self, call_sid: str, messages: List[Dict[str, str]]) -> None:
        if self._use_redis:
            await self._redis.setex(f"call:{call_sid}:msgs", 3600, json.dumps(messages))
        else:
            self._local[call_sid] = messages

    async def append(self, call_sid: str, role: str, content: str, keep_last: int = 12) -> None:
        msgs = await self.get(call_sid)
        msgs.append({"role": role, "content": content})
        if len(msgs) > keep_last:
            msgs = msgs[-keep_last:]
        await self.set(call_sid, msgs)

    async def clear(self, call_sid: str) -> None:
        if self._use_redis:
            await self._redis.delete(f"call:{call_sid}:msgs")
        else:
            self._local.pop(call_sid, None)

memory = MemoryStore()

# -------------------------
# Helpers Twilio & sécurité
# -------------------------
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
    call_sid, ts_str, sig = token.split(".")
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

def twiml(xml: str) -> PlainTextResponse:
    return PlainTextResponse(xml, media_type="application/xml")

# -------------------------
# Fallback LLM
# -------------------------
GOODBYE = ("au revoir", "c'est tout", "merci c'est tout", "bye", "j'ai fini", "je n'ai plus de questions")

def should_hangup(text: str) -> bool:
    t = text.lower()
    return any(p in t for p in GOODBYE)

def build_messages(history: List[Dict[str, str]]) -> List[Dict[str, str]]:
    return [{"role": "system", "content": SYSTEM_PROMPT}] + history

async def llm_reply_fallback(call_sid: str, user_text: str) -> str:
    await memory.append(call_sid, "user", user_text)
    messages = build_messages(await memory.get(call_sid))
    completion = client.chat.completions.create(
        model=OPENAI_MODEL_FALLBACK,
        messages=messages,
        temperature=0.6,
        max_tokens=250,
    )
    answer = completion.choices[0].message.content.strip()
    await memory.append(call_sid, "assistant", answer)
    return answer

# -------------------------
# Compat headers websockets 
# -------------------------
def oai_ws_connect(url: str, headers: Dict[str, str]):
    """Retourne un connecteur WS compatible (v12/v15)."""
    params = inspect.signature(websockets.connect).parameters
    common = dict(ping_interval=20, ping_timeout=20)
    if "extra_headers" in params:
        return websockets.connect(url, extra_headers=headers, **common)         
    if "additional_headers" in params:
        return websockets.connect(url, additional_headers=headers, **common)     # au cas où
    if "headers" in params:
        return websockets.connect(url, headers=headers, **common)                # très ancien
    return websockets.connect(url, **common)

# -------------------------
# FastAPI
# -------------------------
app = FastAPI(title="Voice AI Assistant — Realtime + Fallback")

@app.on_event("startup")
async def _startup():
    await memory.init()

@app.get("/healthz")
async def healthz():
    return {"status": "ok", "realtime": USE_REALTIME, "websockets": getattr(websockets, "__version__", "unknown")}

# 1) Entrée d'appel
@app.post("/incoming-call", response_class=PlainTextResponse)
async def incoming_call(request: Request, _=Depends(validate_twilio_http)):
    form = await request.form()
    call_sid = form.get("CallSid", "unknown")

    if USE_REALTIME:
        vr = VoiceResponse()
        vr.say("Bonjour, je suis votre assistant virtuel. Je vous écoute.", voice=TWILIO_VOICE, language=SPEECH_LANGUAGE)

        token = sign_ws_token(call_sid)
        wss_url = ws_public_url(request)

        with vr.connect() as conn:
            stream = conn.stream(url=wss_url)
            stream.parameter(name="tok", value=token)  
        return twiml(str(vr))

    vr = VoiceResponse()
    vr.redirect("/twilio/voice")
    return twiml(str(vr))

# 2) WebSocket Twilio <-> OpenAI Realtime
@app.websocket("/media-stream")
async def media_stream(ws: WebSocket):
    await ws.accept()
    stream_sid: Optional[str] = None
    authed = False

    oai_url = f"wss://api.openai.com/v1/realtime?model={REALTIME_MODEL}"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "OpenAI-Beta": "realtime=v1"}

    try:
        conn = oai_ws_connect(oai_url, headers)
        async with conn as oai_ws:
            # Configuration session
            await oai_ws.send(json.dumps({
                "type": "session.update",
                "session": {
                    "voice": REALTIME_VOICE,
                    "modalities": ["text", "audio"],
                    "instructions": SYSTEM_PROMPT,
                    "turn_detection": {"type": "server_vad"},
                    "input_audio_format": "g711_ulaw",
                    "output_audio_format": "g711_ulaw",
                    "temperature": 0.6,
                },
            }))

            async def pump_twilio_to_openai():
                nonlocal stream_sid, authed
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
                                log.info(f"WS auth OK callSid={token_call_sid}")
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
                            await oai_ws.send(json.dumps({"type": "input_audio_buffer.append", "audio": payload}))

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
                        if evt.get("type") == "response.audio.delta" and evt.get("delta") and stream_sid:
                            out = {"event": "media", "streamSid": stream_sid, "media": {"payload": evt["delta"]}}
                            await ws.send_json(out)
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

# -------------------------
# Fallback HTTP (tour-par-tour)
# -------------------------
@app.post("/twilio/voice", response_class=PlainTextResponse)
async def twilio_voice(request: Request, _=Depends(validate_twilio_http)):
    form = await request.form()
    call_sid = form.get("CallSid", "unknown")
    if not await memory.get(call_sid):
        await memory.set(call_sid, [])

    greeting = ("Bonjour, ici l'assistant automatique. "
                "Posez votre question après le bip. Dites 'au revoir' pour terminer.")
    vr = VoiceResponse()
    gather = Gather(input="speech", action="/twilio/respond", method="POST",
                    language=SPEECH_LANGUAGE, speech_timeout="auto")
    gather.say(greeting, voice=TWILIO_VOICE, language=SPEECH_LANGUAGE)
    vr.append(gather)
    vr.redirect("/twilio/voice")
    return twiml(str(vr))

@app.post("/twilio/respond", response_class=PlainTextResponse)
async def twilio_respond(request: Request, _=Depends(validate_twilio_http)):
    form = await request.form()
    call_sid = form.get("CallSid", "unknown")
    user_text = (form.get("SpeechResult") or "").strip()
    vr = VoiceResponse()

    if not user_text:
        reprompt = "Je n’ai pas bien entendu. Pouvez-vous répéter ?"
        gather = Gather(input="speech", action="/twilio/respond", method="POST",
                        language=SPEECH_LANGUAGE, speech_timeout="auto")
        gather.say(reprompt, voice=TWILIO_VOICE, language=SPEECH_LANGUAGE)
        vr.append(gather)
        vr.redirect("/twilio/voice")
        return twiml(str(vr))

    if should_hangup(user_text):
        vr.say("Très bien. Merci pour votre appel. Au revoir.", voice=TWILIO_VOICE, language=SPEECH_LANGUAGE)
        vr.hangup()
        await memory.clear(call_sid)
        return twiml(str(vr))

    try:
        answer = await llm_reply_fallback(call_sid, user_text)
    except Exception:
        answer = "Désolé, une erreur est survenue. Pouvez-vous reformuler brièvement ?"

    gather = Gather(input="speech", action="/twilio/respond", method="POST",
                    language=SPEECH_LANGUAGE, speech_timeout="auto")
    gather.say(answer, voice=TWILIO_VOICE, language=SPEECH_LANGUAGE)
    gather.say("Vous pouvez répondre quand vous êtes prêt.", voice=TWILIO_VOICE, language=SPEECH_LANGUAGE)
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
