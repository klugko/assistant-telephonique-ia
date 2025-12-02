import json
import inspect
import asyncio
from typing import Dict, Any, Optional
from fastapi import WebSocket, WebSocketDisconnect
import websockets

from . import config
from .helpers import verify_ws_token
from .memory import memory
from .voice import realtime_voice_for_gender

def oai_ws_connect(url: str, headers: Dict[str, str]):
    """Retourne un connecteur WebSocket compatible selon la version de websockets."""
    params = inspect.signature(websockets.connect).parameters
    common = dict(ping_interval=20, ping_timeout=20, max_size=None)
    if "extra_headers" in params:
        return websockets.connect(url, extra_headers=headers, **common)
    if "additional_headers" in params:
        return websockets.connect(url, additional_headers=headers, **common)
    if "headers" in params:
        return websockets.connect(url, headers=headers, **common)
    return websockets.connect(url, **common)

async def media_stream(ws: WebSocket):
    """Ponte WebSocket Twilio <-> OpenAI Realtime (audio full-duplex)."""
    await ws.accept()
    stream_sid: Optional[str] = None
    authed = False
    call_sid: Optional[str] = None

    oai_url = f"wss://api.openai.com/v1/realtime?model={config.REALTIME_MODEL}"
    headers = {"Authorization": f"Bearer {config.OPENAI_API_KEY}", "OpenAI-Beta": "realtime=v1"}

    try:
        conn = oai_ws_connect(oai_url, headers)
        async with conn as oai_ws:
            # Session initiale 
            enhanced_instructions = (
                config.SYSTEM_PROMPT
                + " Parle de façon naturelle, fluide et chaleureuse, avec un débit modéré et des micro-pauses."
                + " Si l'utilisateur demande de changer de voix (homme/femme), utilise l'outil set_voice sans poser de questions."
            )
            await oai_ws.send(json.dumps({
                "type": "session.update",
                "session": {
                    "voice": config.REALTIME_VOICE_DEFAULT,
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
                                "properties": {"gender": {"type": "string", "enum": ["male", "female"]}},
                                "required": ["gender"]
                            }
                        }
                    ],
                    "tool_choice": "auto",
                },
            }))

            async def handle_function_call(evt: Dict[str, Any]):
                """Gère un appel d’outil 'set_voice' envoyé par le modèle Realtime."""
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
                new_voice = realtime_voice_for_gender(gender)
                await oai_ws.send(json.dumps({"type": "session.update", "session": {"voice": new_voice}}))
                if call_sid:
                    await memory.set_attr(call_sid, "realtime_gender", gender)
                confirm = "Très bien, je passe à une voix féminine." if gender == "female" else "Très bien, je passe à une voix masculine."
                await oai_ws.send(json.dumps({
                    "type": "response.create",
                    "response": {"modalities": ["audio"], "instructions": confirm}
                }))

            async def pump_twilio_to_openai():
                """Relais: flux entrant Twilio → buffer audio OpenAI Realtime."""
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
                                # Appliquer voix sauvegardée si existante
                                saved = await memory.get_attr(call_sid, "realtime_gender")
                                if saved in ("male", "female"):
                                    await oai_ws.send(json.dumps({
                                        "type": "session.update",
                                        "session": {"voice": realtime_voice_for_gender(saved)}
                                    }))
                            except Exception as e:
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
                except Exception:
                    try:
                        await oai_ws.close()
                    except Exception:
                        pass

            async def pump_openai_to_twilio():
                """Relais: audio delta OpenAI → messages media Twilio."""
                try:
                    async for raw in oai_ws:
                        evt = json.loads(raw)
                        et = evt.get("type")
                        if et == "response.audio.delta" and evt.get("delta") and stream_sid:
                            out = {"event": "media", "streamSid": stream_sid, "media": {"payload": evt["delta"]}}
                            await ws.send_json(out)
                        elif et in ("response.function_call", "function_call", "tool_call"):
                            await handle_function_call(evt)
                except Exception:
                    try:
                        await ws.close()
                    except Exception:
                        pass

            await asyncio.gather(pump_twilio_to_openai(), pump_openai_to_twilio())

    except Exception:
        try:
            await ws.close()
        except Exception:
            pass
