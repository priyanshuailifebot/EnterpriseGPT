"""S3-compatible object storage (MinIO) for document binaries."""

from __future__ import annotations

import asyncio
import mimetypes
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from uuid import UUID

import boto3
from botocore.client import BaseClient
from botocore.exceptions import ClientError

from core.config import Settings, get_settings

_executor = ThreadPoolExecutor(max_workers=4)


def _bucket_for_workspace(workspace_id: UUID) -> str:
    return f"documents-{workspace_id}"


def _sync_client(settings: Settings) -> BaseClient:
    return boto3.client(
        "s3",
        endpoint_url=(
            f"{'https' if settings.MINIO_USE_SSL else 'http'}://{settings.MINIO_ENDPOINT}"
        ),
        aws_access_key_id=settings.MINIO_USER,
        aws_secret_access_key=settings.MINIO_PASSWORD,
        region_name="us-east-1",
    )


async def _run_sync(fn, *args: Any, **kwargs: Any) -> Any:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_executor, lambda: fn(*args, **kwargs))


class StorageService:
    """Async-facing MinIO/S3 helper (boto3 calls run in a thread pool)."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    async def ensure_bucket(self, workspace_id: UUID) -> str:
        bucket = _bucket_for_workspace(workspace_id)

        def _ensure() -> None:
            cli = _sync_client(self._settings)
            try:
                cli.head_bucket(Bucket=bucket)
            except ClientError as e:
                code = e.response.get("Error", {}).get("Code", "")
                if code in ("404", "NoSuchBucket", "403"):
                    cli.create_bucket(Bucket=bucket)
                else:
                    raise

        await _run_sync(_ensure)
        return bucket

    async def upload_document(
        self,
        file_bytes: bytes,
        filename: str,
        workspace_id: UUID,
        user_id: UUID,
    ) -> dict[str, str]:
        bucket = await self.ensure_bucket(workspace_id)
        safe = filename.replace("/", "_")
        key = f"{user_id}/{uuid.uuid4().hex}_{safe}"
        content_type, _ = mimetypes.guess_type(filename)
        if not content_type:
            content_type = "application/octet-stream"

        def _put() -> None:
            _sync_client(self._settings).put_object(
                Bucket=bucket,
                Key=key,
                Body=file_bytes,
                ContentType=content_type,
            )

        await _run_sync(_put)

        base = (
            f"{'https' if self._settings.MINIO_USE_SSL else 'http'}"
            f"://{self._settings.MINIO_ENDPOINT}"
        )
        url = f"{base}/{bucket}/{key}"
        return {"bucket": bucket, "key": key, "url": url}

    async def download_document(self, bucket: str, key: str) -> bytes:
        def _get() -> bytes:
            resp = _sync_client(self._settings).get_object(Bucket=bucket, Key=key)
            return resp["Body"].read()

        return await _run_sync(_get)

    async def delete_document(self, bucket: str, key: str) -> None:
        def _delete() -> None:
            _sync_client(self._settings).delete_object(Bucket=bucket, Key=key)

        await _run_sync(_delete)

    @staticmethod
    def bucket_name_for_workspace(workspace_id: UUID) -> str:
        return _bucket_for_workspace(workspace_id)
