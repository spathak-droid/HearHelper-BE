import logging
import os
from pathlib import Path
from typing import Optional

import boto3
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger(__name__)

_client = None
_bucket = None


def _initialize_client():
    global _client, _bucket
    endpoint = os.getenv("R2_ENDPOINT_URL")
    access_key = os.getenv("R2_ACCESS_KEY_ID")
    secret_key = os.getenv("R2_SECRET_ACCESS_KEY")
    bucket = os.getenv("R2_BUCKET")
    region = os.getenv("R2_REGION", "auto")

    if not all([endpoint, access_key, secret_key, bucket]):
        return None, None

    session = boto3.session.Session()
    client = session.client(
        "s3",
        endpoint_url=endpoint,
        region_name=region,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
    )
    _client = client
    _bucket = bucket
    return client, bucket


def _get_client():
    global _client, _bucket
    if _client is not None and _bucket is not None:
        return _client, _bucket
    return _initialize_client()


def is_enabled() -> bool:
    client, bucket = _get_client()
    return client is not None and bucket is not None


def upload_file(local_path: Path, remote_key: str) -> bool:
    client, bucket = _get_client()
    if client is None:
        return False
    try:
        client.upload_file(str(local_path), bucket, remote_key)
        return True
    except (BotoCoreError, ClientError) as exc:
        logger.warning("Failed to upload %s to R2: %s", remote_key, exc)
        return False


def download_file(remote_key: str, local_path: Path) -> bool:
    client, bucket = _get_client()
    if client is None:
        return False
    try:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        client.download_file(bucket, remote_key, str(local_path))
        return True
    except (BotoCoreError, ClientError) as exc:
        logger.info("R2 download miss for %s: %s", remote_key, exc)
        return False


def object_exists(remote_key: str) -> bool:
    client, bucket = _get_client()
    if client is None:
        return False
    try:
        client.head_object(Bucket=bucket, Key=remote_key)
        return True
    except (BotoCoreError, ClientError):
        return False


def download_bytes(remote_key: str) -> Optional[bytes]:
    client, bucket = _get_client()
    if client is None:
        return None
    try:
        response = client.get_object(Bucket=bucket, Key=remote_key)
        return response["Body"].read()
    except (BotoCoreError, ClientError) as exc:
        logger.info("R2 download bytes miss for %s: %s", remote_key, exc)
        return None


def upload_bytes(data: bytes, remote_key: str) -> bool:
    client, bucket = _get_client()
    if client is None:
        return False
    try:
        client.put_object(Bucket=bucket, Key=remote_key, Body=data)
        return True
    except (BotoCoreError, ClientError) as exc:
        logger.warning("Failed to upload bytes to %s: %s", remote_key, exc)
        return False


def list_objects(prefix: str) -> list[str]:
    client, bucket = _get_client()
    if client is None:
        return []
    objects = []
    paginator = client.get_paginator("list_objects_v2")
    try:
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                objects.append(obj["Key"])
    except (BotoCoreError, ClientError) as exc:
        logger.info("R2 list_objects error for %s: %s", prefix, exc)
    return objects
