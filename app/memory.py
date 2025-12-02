import json
from typing import Dict, List, Optional, Any
try:
    import redis.asyncio as aioredis  # type: ignore
except Exception:
    aioredis = None

from . import config

class MemoryStore:
    """Stockage conversationnel (Redis si dispo, sinon mémoire locale)."""

    def __init__(self) -> None:
        self._use_redis = False
        self._redis = None
        self._local_msgs: Dict[str, List[Dict[str, str]]] = {}
        self._local_kv: Dict[str, Dict[str, Any]] = {}

    async def init(self) -> None:
        """Initialise la connexion Redis si REDIS_URL est défini."""
        if config.REDIS_URL and aioredis is not None:
            self._redis = aioredis.from_url(config.REDIS_URL, encoding="utf-8", decode_responses=True)
            try:
                await self._redis.ping()
                self._use_redis = True
            except Exception:
                self._use_redis = False

    async def get(self, call_sid: str) -> List[Dict[str, str]]:
        """Retourne l’historique de messages pour un appel."""
        if self._use_redis:
            raw = await self._redis.get(f"call:{call_sid}:msgs")
            return json.loads(raw) if raw else []
        return self._local_msgs.get(call_sid, [])

    async def set(self, call_sid: str, messages: List[Dict[str, str]]) -> None:
        """Écrit l’historique de messages pour un appel."""
        if self._use_redis:
            await self._redis.setex(f"call:{call_sid}:msgs", 3600, json.dumps(messages))
        else:
            self._local_msgs[call_sid] = messages

    async def append(self, call_sid: str, role: str, content: str, keep_last: int = 12) -> None:
        """Ajoute un message et tronque l’historique à keep_last éléments."""
        msgs = await self.get(call_sid)
        msgs.append({"role": role, "content": content})
        if len(msgs) > keep_last:
            msgs = msgs[-keep_last:]
        await self.set(call_sid, msgs)

    async def get_attr(self, call_sid: str, key: str, default: Any = None) -> Any:
        """Lit un attribut de session (clé/valeur) pour l’appel."""
        if self._use_redis:
            raw = await self._redis.hget(f"call:{call_sid}:attr", key)
            return json.loads(raw) if raw is not None else default
        return self._local_kv.get(call_sid, {}).get(key, default)

    async def set_attr(self, call_sid: str, key: str, value: Any) -> None:
        """Écrit un attribut de session (clé/valeur) pour l’appel."""
        if self._use_redis:
            await self._redis.hset(f"call:{call_sid}:attr", key, json.dumps(value))
            await self._redis.expire(f"call:{call_sid}:attr", 3600)
        else:
            d = self._local_kv.setdefault(call_sid, {})
            d[key] = value

    async def clear(self, call_sid: str) -> None:
        """Efface l’historique et les attributs liés à l’appel."""
        if self._use_redis:
            await self._redis.delete(f"call:{call_sid}:msgs")
            await self._redis.delete(f"call:{call_sid}:attr")
        else:
            self._local_msgs.pop(call_sid, None)
            self._local_kv.pop(call_sid, None)

memory = MemoryStore()
