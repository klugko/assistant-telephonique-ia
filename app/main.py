import os
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from . import config
from .memory import memory
from .routes import router
from .ws import media_stream

app = FastAPI(title="Voice AI Assistant — Realtime + Fallback")

# Fichier Static 
app.mount("/assets", StaticFiles(directory="assets"), name="assets")

# REST routes
app.include_router(router)

# WebSocket route
app.add_api_websocket_route("/media-stream", media_stream)

@app.on_event("startup")
async def _startup():
    """Initialisation de l’application (connexion Redis, vérif assets)."""
    await memory.init()
    if not os.path.exists(config.WELCOME_FILE_PATH):
        import logging
        logging.getLogger("voice-app").warning(
            "Le fichier de bienvenue %s est manquant. Placez-le avant la mise en prod.",
            config.WELCOME_FILE_PATH,
        )
