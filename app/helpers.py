import hmac
import time
import hashlib
from fastapi import Request, HTTPException
from fastapi.responses import PlainTextResponse
from twilio.request_validator import RequestValidator
from . import config

def twiml(xml: str) -> PlainTextResponse:
    """Retourne une réponse TwiML (XML) avec le bon Content-Type."""
    return PlainTextResponse(xml, media_type="application/xml")

async def validate_twilio_http(request: Request):
    """Vérifie la signature Twilio sur les webhooks HTTP."""
    if not config.VERIFY_TWILIO_SIGNATURE:
        return
    if not config.TWILIO_AUTH_TOKEN:
        raise HTTPException(status_code=500, detail="TWILIO_AUTH_TOKEN requis")
    validator = RequestValidator(config.TWILIO_AUTH_TOKEN)
    signature = request.headers.get("X-Twilio-Signature", "")
    url = str(request.url)
    form = await request.form()
    if not validator.validate(url, dict(form), signature):
        raise HTTPException(status_code=403, detail="Signature Twilio invalide")

def sign_ws_token(call_sid: str) -> str:
    """Signe un token court HMAC pour authentifier la websocket Twilio→serveur."""
    ts = int(time.time())
    payload = f"{call_sid}.{ts}"
    sig = hmac.new(config.HMAC_SECRET, payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"

def verify_ws_token(token: str, max_age: int = 300) -> str:
    """Vérifie et retourne le call_sid contenu dans le token HMAC signé."""
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("Token mal formé")
    call_sid, ts_str, sig = parts
    data = f"{call_sid}.{ts_str}"
    expected = hmac.new(config.HMAC_SECRET, data.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        raise ValueError("HMAC invalide")
    ts = int(ts_str)
    if time.time() - ts > max_age:
        raise ValueError("Token expiré")
    return call_sid

def ws_public_url(request: Request) -> str:
    """Construit l’URL wss publique du endpoint websocket /media-stream."""
    if config.PUBLIC_BASE_URL:
        return config.PUBLIC_BASE_URL.replace("https://", "wss://").replace("http://", "wss://") + "/media-stream"
    return f"wss://{request.url.hostname}/media-stream"

def http_base_url(request: Request) -> str:
    """Construit l’URL http(s) publique de base (fallback si PUBLIC_BASE_URL absent)."""
    if config.PUBLIC_BASE_URL:
        return config.PUBLIC_BASE_URL
    scheme = "https" if request.url.scheme in ("http", "https") else "https"
    return f"{scheme}://{request.url.hostname}"
