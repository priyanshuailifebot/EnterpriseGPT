"""Unit tests for MinIO/S3 StorageService (boto3 mocked)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from botocore.exceptions import ClientError

from core.config import Settings
from core.storage import StorageService


@pytest.mark.asyncio
async def test_storage_ensure_bucket_creates_on_404() -> None:
    workspace_id = uuid4()
    mock_cli = MagicMock()
    mock_cli.head_bucket.side_effect = ClientError({"Error": {"Code": "404"}}, "HeadBucket")

    settings = Settings(
        MINIO_ENDPOINT="localhost:9000",
        MINIO_USER="test",
        MINIO_PASSWORD="secret",
        MINIO_USE_SSL=False,
    )

    with patch("core.storage.boto3.client", return_value=mock_cli):
        svc = StorageService(settings)
        bucket = await svc.ensure_bucket(workspace_id)

    assert bucket == f"documents-{workspace_id}"
    mock_cli.create_bucket.assert_called_once_with(Bucket=bucket)


@pytest.mark.asyncio
async def test_upload_document_put_object() -> None:
    workspace_id = uuid4()
    user_id = uuid4()
    mock_cli = MagicMock()
    mock_cli.head_bucket.return_value = {}

    settings = Settings(
        MINIO_ENDPOINT="localhost:9000",
        MINIO_USER="test",
        MINIO_PASSWORD="secret",
        MINIO_USE_SSL=False,
    )

    with patch("core.storage.boto3.client", return_value=mock_cli):
        svc = StorageService(settings)
        out = await svc.upload_document(b"hello", "a.pdf", workspace_id, user_id)

    assert "key" in out and "bucket" in out
    mock_cli.put_object.assert_called_once()
    call_kw = mock_cli.put_object.call_args.kwargs
    assert call_kw["Bucket"] == f"documents-{workspace_id}"
    assert call_kw["Body"] == b"hello"
    assert call_kw["ContentType"] == "application/pdf"
