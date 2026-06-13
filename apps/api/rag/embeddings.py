"""Azure OpenAI embedding helpers with Redis caching for queries."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from openai import AsyncAzureOpenAI

from core.config import Settings, get_settings

if TYPE_CHECKING:
    from redis.asyncio import Redis


def _azure_embedding_client(settings: Settings) -> AsyncAzureOpenAI:
    ep = settings.AZURE_OPENAI_ENDPOINT.strip().rstrip("/")
    key = settings.AZURE_OPENAI_API_KEY.strip()
    if not ep or not key:
        raise RuntimeError(
            "Azure OpenAI not configured (AZURE_OPENAI_ENDPOINT / AZURE_OPENAI_API_KEY)."
        )
    return AsyncAzureOpenAI(
        azure_endpoint=ep,
        api_key=key,
        api_version=settings.AZURE_OPENAI_API_VERSION,
    )


class EmbeddingService:
    """Batch document embeddings + cached query embeddings."""

    def __init__(
        self,
        settings: Settings | None = None,
        redis: Redis | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._redis = redis
        self._model = self._settings.AZURE_OPENAI_EMBEDDING_DEPLOYMENT

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        client = _azure_embedding_client(self._settings)
        batch_size = self._settings.RAG_EMBEDDING_BATCH_SIZE
        out: list[list[float]] = []
        try:
            for i in range(0, len(texts), batch_size):
                batch = texts[i : i + batch_size]
                resp = await client.embeddings.create(input=batch, model=self._model)
                out.extend([item.embedding for item in resp.data])
            return out
        finally:
            await client.close()

    async def embed_query(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        key = f"egpt:emb:{digest}"
        if self._redis is not None:
            cached = await self._redis.get(key)
            if cached:
                return json.loads(cached)
        vec = (await self.embed_texts([text]))[0]
        if self._redis is not None:
            await self._redis.setex(key, 3600, json.dumps(vec))
        return vec
