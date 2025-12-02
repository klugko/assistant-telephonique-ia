# Voice AI Assistant — FastAPI · Twilio · OpenAI Realtime

Assistant téléphonique IA temps réel. Un appel arrive sur votre numéro Twilio, l’application lit un message de bienvenue (`assets/welcome.mp3`), route l’audio vers OpenAI Realtime pour des réponses naturelles, et supporte les **commandes vocales** pour **changer de voix (homme/femme)**. Un **fallback** HTTP (tour-par-tour) est prévu si le Realtime est désactivé.

---

## Caractéristiques

* **Message de bienvenue**: lecture d’un MP3 au démarrage d’appel.
* **Voix naturelle** (Realtime OpenAI): prosodie réglée, latence faible.
* **Bascule de voix par commande vocale**: “change ta voix en voix de femme / d’homme”.
* **Fallback TwiML** (sans WebSocket): ASR Twilio + Chat Completions.
* **Mémoire par appel**: Redis si dispo, sinon mémoire locale.
* **Sécurité**: HMAC pour auth WebSocket; vérification de signature Twilio (optionnelle/dev, **obligatoire en prod**).

---

## Architecture

```
app/
  __init__.py
  main.py          # Bootstrap FastAPI (routes + websocket + statiques)
  config.py        # Variables d’environnement + client OpenAI
  helpers.py       # Twilio signature, HMAC, builders d’URL, twiml()
  memory.py        # MemoryStore (Redis / local)
  voice.py         # Parsing commandes voix, mapping voix Twilio/Realtime
  llm.py           # Fallback LLM (Chat Completions) + fin d’appel
  ws.py            # Pont WebSocket Twilio <-> OpenAI Realtime
  routes.py        # Webhooks Twilio: /incoming-call, /twilio/voice, /twilio/respond, /twilio/events
assets/
  welcome.mp3      # Message de bienvenue
```

---

## Prérequis

* Python 3.11+
* Compte **Twilio** (Programmable Voice + un numéro)
* Clé **OpenAI** avec accès au **Realtime**
* (Optionnel) **Redis** managé (prod) ou local (dev)

---

## Installation (local)

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt  
```

Créez un fichier `.env` à la racine :

```env
OPENAI_API_KEY=sk-...
USE_REALTIME=true
REALTIME_MODEL=gpt-4o-realtime-preview-2024-10-01
REALTIME_VOICE=alloy
REALTIME_VOICE_MALE=alloy
REALTIME_VOICE_FEMALE=verse
OPENAI_MODEL=gpt-4o-mini
SYSTEM_PROMPT=Tu es un assistant téléphonique utile, concis et professionnel.
SPEECH_LANGUAGE=fr-FR
TWILIO_VOICE=alice
VERIFY_TWILIO_SIGNATURE=false
TWILIO_AUTH_TOKEN=xxxxx
PUBLIC_BASE_URL=http://localhost:8000
REDIS_URL=redis://127.0.0.1:6379/0
HMAC_SECRET=generate-a-long-random-secret
```

> Générer un secret fort :
> `python -c "import secrets; print(secrets.token_hex(32))"`

Placez votre fichier audio : `assets/welcome.mp3`.

Lancez l’API :

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

---

## Configuration Twilio

1. **Achetez/Utilisez** un numéro dans Programmable Voice.
2. **A Call Comes In** (Webhook Voice) → `POST` vers:

   * `https://<PUBLIC_BASE_URL>/incoming-call`
3. **Status Callback** (recommandé) → `POST` vers:

   * `https://<PUBLIC_BASE_URL>/twilio/events`
4. Assurez-vous que `PUBLIC_BASE_URL` est accessible en HTTPS depuis Twilio.
5. Si `USE_REALTIME=true`, Twilio recevra une TwiML `<Connect><Stream>` vers `wss://<PUBLIC_BASE_URL>/media-stream`.

> En **dev**, utilisez un tunnel (ngrok, Cloudflared) pour exposer `localhost:8000` en HTTPS et définissez `PUBLIC_BASE_URL` sur l’URL tunnel.

---

## Commandes vocales

* “**change ta voix en voix de femme**” → bascule vers la voix féminine
* “**change ta voix en voix d’homme**” → bascule vers la voix masculine

Côté Realtime, définissez **deux voix distinctes**:

* `REALTIME_VOICE_MALE` (ex: `alloy`)
* `REALTIME_VOICE_FEMALE` (ex: `verse`)

Sans ces deux variables, les deux commandes tomberont sur la même voix.

---

## Endpoints

* `GET /healthz` — état de l’app.
* `POST /incoming-call` — webhook d’entrée d’appel (joue `welcome.mp3`, connecte Realtime ou fallback).
* `POST /twilio/voice` — fallback: prompt initial + Gather.
* `POST /twilio/respond` — fallback: traitement ASR, bascule voix, réponse LLM.
* `POST /twilio/events` — callbacks d’état d’appel (purge mémoire).
* `WS /media-stream` — pont Twilio Media Streams ↔ OpenAI Realtime.

---

## Déploiement (Render)

1. **Create Web Service**

   * Build command: `pip install -r requirements.txt`
   * Start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
2. **Env Vars** (type Secret/Environment)

   * Renseignez toutes les variables `.env.prod` (cf. ci-dessous).
3. **Redis**

   * Ajouter un Redis managé (Render/Upstash) → `REDIS_URL=redis://:<pwd>@<host>:<port>/<db>`
4. **Twilio**

   * Pointez `A Call Comes In` sur `https://<render-app-url>/incoming-call`
   * `Status Callback` sur `https://<render-app-url>/twilio/events`

Exemple `.env.example`:

```env
OPENAI_API_KEY=sk-...
USE_REALTIME=true
REALTIME_MODEL=gpt-4o-realtime-preview-2024-10-01
REALTIME_VOICE=alloy
REALTIME_VOICE_MALE=alloy
REALTIME_VOICE_FEMALE=verse
OPENAI_MODEL=gpt-4o-mini
SYSTEM_PROMPT=Tu es un assistant téléphonique utile, concis et professionnel.
SPEECH_LANGUAGE=fr-FR
TWILIO_VOICE=alice
VERIFY_TWILIO_SIGNATURE=true
TWILIO_AUTH_TOKEN=xxxxx
PUBLIC_BASE_URL=https://<render-app-url>
REDIS_URL=redis://:<password>@<host>:<port>/<db>
HMAC_SECRET=<long-random-secret>
```

---

## Sécurité & production

* **Vérifie la signature Twilio** : `VERIFY_TWILIO_SIGNATURE=true`.
* **HMAC secret**: utilisez une valeur unique et forte, différente du token Twilio.
* **Clés/Secrets**: stockez-les dans l’UI Render (ou équivalent), jamais en clair dans le repo.
* **TLS**: Twilio requiert TLS 1.2+.
* **Quotas & coûts**: surveillez usage Realtime (logging, métriques).

---

## Tests rapides

* Santé: `curl https://<PUBLIC_BASE_URL>/healthz`
* Appel réel: appelez votre numéro Twilio, vous devez entendre `welcome.mp3` puis la voix de l’assistant.
* Commande: dites “change ta voix en voix de femme” → la voix bascule et confirme.

---

## Dépannage

* **403 Signature Twilio**: vérifiez `VERIFY_TWILIO_SIGNATURE` et `TWILIO_AUTH_TOKEN`.
* **Pas d’audio Realtime**: vérifier `PUBLIC_BASE_URL` (wss://), firewall, port 443, et variables `REALTIME_*`.
* **Fallback activé** alors que Realtime attendu: `USE_REALTIME=true`, clé OpenAI valide, pas d’erreur WS dans les logs.
* **Bascule de voix ne change rien**: définissez `REALTIME_VOICE_MALE` et `REALTIME_VOICE_FEMALE` sur **deux** voix différentes.

---


## Auteurs

Jean Aimé - jeanaime.dev@gmail.com
