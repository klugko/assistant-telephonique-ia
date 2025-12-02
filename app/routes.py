from fastapi import APIRouter, Request, Depends
from fastapi.responses import PlainTextResponse
from twilio.twiml.voice_response import VoiceResponse, Gather

from . import config
from .helpers import validate_twilio_http, twiml, ws_public_url, http_base_url
from .memory import memory
from .llm import should_hangup, llm_reply_fallback
from .voice import parse_gender_command, twilio_voice_for_gender

router = APIRouter()

@router.get("/healthz")
async def healthz():
    """Indique l’état de l’application et la présence du fichier audio."""
    return {
        "status": "ok",
        "realtime": config.USE_REALTIME,
        "assets": True, 
    }

@router.post("/incoming-call", response_class=PlainTextResponse)
async def incoming_call(request: Request, _=Depends(validate_twilio_http)):
    """Webhook Twilio: débute l’appel, joue le welcome.mp3 et connecte le stream."""
    form = await request.form()
    call_sid = form.get("CallSid", "unknown")

    base_url = http_base_url(request)
    welcome_url = f"{base_url}/assets/welcome.mp3"

    vr = VoiceResponse()
    vr.play(welcome_url)

    if config.USE_REALTIME:
        token = _sign(call_sid)  
        wss_url = ws_public_url(request)
        with vr.connect() as conn:
            stream = conn.stream(url=wss_url)
            stream.parameter(name="tok", value=token)
        return twiml(str(vr))

    vr.redirect("/twilio/voice")
    return twiml(str(vr))

def _sign(call_sid: str) -> str:
    """Signe un jeton HMAC pour authentifier la websocket côté serveur."""
    from .helpers import sign_ws_token
    return sign_ws_token(call_sid)

@router.post("/twilio/voice", response_class=PlainTextResponse)
async def twilio_voice(request: Request, _=Depends(validate_twilio_http)):
    """Mode fallback: prompt initial, lecture welcome, Gather sur la voix courante."""
    form = await request.form()
    call_sid = form.get("CallSid", "unknown")
    if not await memory.get(call_sid):
        await memory.set(call_sid, [])

    base_url = http_base_url(request)
    welcome_url = f"{base_url}/assets/welcome.mp3"

    vr = VoiceResponse()
    vr.play(welcome_url)

    greeting = ("Bonjour, ici l'assistant. Posez votre question après le bip. "
                "Dites « change ta voix en voix de femme » ou « voix d'homme » pour changer de voix. "
                "Dites « au revoir » pour terminer.")

    gender = await memory.get_attr(call_sid, "tts_gender", None)
    voice_cur = twilio_voice_for_gender(gender)

    gather = Gather(input="speech", action="/twilio/respond", method="POST",
                    language=config.SPEECH_LANGUAGE, speech_timeout="auto")
    gather.say(greeting, voice=voice_cur, language=config.SPEECH_LANGUAGE)
    vr.append(gather)
    vr.redirect("/twilio/voice")
    return twiml(str(vr))

@router.post("/twilio/respond", response_class=PlainTextResponse)
async def twilio_respond(request: Request, _=Depends(validate_twilio_http)):
    """Mode fallback: traite la reconnaissance, bascule de voix ou réponse LLM."""
    form = await request.form()
    call_sid = form.get("CallSid", "unknown")
    user_text = (form.get("SpeechResult") or "").strip()
    vr = VoiceResponse()

    gender = await memory.get_attr(call_sid, "tts_gender", None)
    voice_cur = twilio_voice_for_gender(gender)

    if not user_text:
        gather = Gather(input="speech", action="/twilio/respond", method="POST",
                        language=config.SPEECH_LANGUAGE, speech_timeout="auto")
        gather.say("Je n’ai pas bien entendu. Pouvez-vous répéter ?",
                   voice=voice_cur, language=config.SPEECH_LANGUAGE)
        vr.append(gather)
        vr.redirect("/twilio/voice")
        return twiml(str(vr))

    if should_hangup(user_text):
        vr.say("Très bien. Merci pour votre appel. Au revoir.", voice=voice_cur, language=config.SPEECH_LANGUAGE)
        vr.hangup()
        await memory.clear(call_sid)
        return twiml(str(vr))

    cmd_gender = parse_gender_command(user_text)
    if cmd_gender:
        await memory.set_attr(call_sid, "tts_gender", cmd_gender)
        voice_cur = twilio_voice_for_gender(cmd_gender)
        confirm = "D'accord, je passe à une voix féminine." if cmd_gender == "female" else "D'accord, je passe à une voix masculine."
        gather = Gather(input="speech", action="/twilio/respond", method="POST",
                        language=config.SPEECH_LANGUAGE, speech_timeout="auto")
        gather.say(confirm, voice=voice_cur, language=config.SPEECH_LANGUAGE)
        gather.say("Vous pouvez continuer.", voice=voice_cur, language=config.SPEECH_LANGUAGE)
        vr.append(gather)
        vr.redirect("/twilio/voice")
        return twiml(str(vr))

    try:
        answer = await llm_reply_fallback(call_sid, user_text)
    except Exception:
        answer = "Désolé, une erreur est survenue. Pouvez-vous reformuler brièvement ?"

    gather = Gather(input="speech", action="/twilio/respond", method="POST",
                    language=config.SPEECH_LANGUAGE, speech_timeout="auto")
    gather.say(answer, voice=voice_cur, language=config.SPEECH_LANGUAGE)
    gather.say("Vous pouvez répondre quand vous êtes prêt.", voice=voice_cur, language=config.SPEECH_LANGUAGE)
    vr.append(gather)
    vr.redirect("/twilio/voice")
    return twiml(str(vr))

@router.post("/twilio/events")
async def twilio_events(request: Request, _=Depends(validate_twilio_http)):
    """Webhook d’événements d’appel Twilio: purge la session en fin d’appel."""
    form = await request.form()
    call_sid = form.get("CallSid")
    call_status = form.get("CallStatus")
    if call_sid and call_status in {"completed", "canceled", "failed", "busy", "no-answer"}:
        await memory.clear(call_sid)
    return {"ok": True}
