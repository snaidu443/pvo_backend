"""File storage for uploaded documents: a local folder (development) or a private S3 bucket.

Both implement the same methods, so routes never know which one is in use.

Keys
- The database stores a relative key: ``assets/<asset_id>/<uuid32>.<ext>``. The uuid is random and the
  extension comes from the server's allow-list, so a crafted file name can never become part of a path.
- Older uploads used a flat ``<uuid32>`` key; those still open and delete.
- S3 object key = ``<PVO_S3_PREFIX>/<key>``. The prefix is fixed per environment.

S3
- Uses the AWS default credential chain (IAM role in AWS, `aws sso login` locally). No keys in config.
- Every object is written with SSE-KMS (the configured key) and a SHA-256 checksum, over HTTPS.
- boto3 is imported only when PVO_STORAGE_BACKEND=s3 (it must be approved and installed first).
"""
import os
import re
import uuid
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, BinaryIO, Protocol
from urllib.parse import urlencode

from .config import get_settings

_EXT = r'(?:pdf|doc|docx|xls|xlsx|xlsm|ppt|pptx|csv)'
_ASSET = r'[A-Za-z0-9][A-Za-z0-9_-]{0,39}'
_KEY = re.compile(rf'^(?:[0-9a-f]{{32}}|assets/{_ASSET}/[0-9a-f]{{32}}\.{_EXT})$')
_ASSET_ID = re.compile(rf'^{_ASSET}$')
_EXT_ONLY = re.compile(rf'^{_EXT}$')
_CHUNK = 1024 * 1024


class FileTooLarge(Exception):
    pass


class StorageError(Exception):
    """The file could not be stored or read. The message is safe to log; it never holds file contents."""


@dataclass(frozen=True)
class StoredObject:
    key: str
    size: int
    version_id: str | None = None


class FileStorage(Protocol):
    backend: str

    def save(self, stream: BinaryIO, *, asset_id: str, ext: str, content_type: str, max_bytes: int) -> StoredObject: ...
    def open(self, key: str) -> BinaryIO: ...
    def delete(self, key: str) -> None: ...


def new_key(asset_id: str, ext: str) -> str:
    if not _ASSET_ID.match(asset_id) or not _EXT_ONLY.match(ext):
        raise ValueError('Invalid asset id or file extension.')
    return f'assets/{asset_id}/{uuid.uuid4().hex}.{ext}'


def check_key(key: str) -> str:
    if not isinstance(key, str) or not _KEY.match(key):
        raise ValueError('Invalid storage key.')
    return key


# Longest relative key: assets/<40-char id>/<32 hex>.<4-char ext> = 85 characters (+ separator).
_LONGEST_KEY = 86
_WINDOWS_MAX_PATH = 259


class LocalFileStorage:
    backend = 'local'

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        if os.name == 'nt' and len(str(self.root)) + _LONGEST_KEY > _WINDOWS_MAX_PATH:
            # Fail at start-up instead of on the first upload: Windows cannot open longer paths by default.
            raise RuntimeError(f'PVO_UPLOAD_DIR is too long for Windows ({len(str(self.root))} characters; keep it under '
                               f'{_WINDOWS_MAX_PATH - _LONGEST_KEY}). Use a short folder such as C:\\PVOData\\uploads.')
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        path = (self.root / check_key(key)).resolve()
        if self.root not in path.parents:  # belt and braces: the key pattern already forbids "..", "/" and "\\"
            raise ValueError('Invalid storage key.')
        return path

    def save(self, stream: BinaryIO, *, asset_id: str, ext: str, content_type: str, max_bytes: int) -> StoredObject:
        """Copy the upload in chunks, stopping as soon as it is larger than max_bytes."""
        key = new_key(asset_id, ext)
        path, size = self._path(key), 0
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open('xb') as out:
                while chunk := stream.read(_CHUNK):
                    size += len(chunk)
                    if size > max_bytes:
                        raise FileTooLarge()
                    out.write(chunk)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return StoredObject(key, size)

    def open(self, key: str) -> BinaryIO:
        try:
            return self._path(key).open('rb')
        except FileNotFoundError as exc:
            raise StorageError('Stored file is missing.') from exc

    def delete(self, key: str) -> None:
        path = self._path(key)
        path.unlink(missing_ok=True)
        folder = path.parent
        if folder != self.root:
            try:
                folder.rmdir()  # removes the per-asset folder only once it is empty
            except OSError:
                pass


class S3FileStorage:
    backend = 's3'

    def __init__(self, bucket: str, prefix: str, kms_key_id: str, client: Any):
        self.bucket, self.prefix, self.kms_key_id, self.client = bucket, prefix.strip('/'), kms_key_id, client

    def _object_key(self, key: str) -> str:
        return f'{self.prefix}/{check_key(key)}'

    def save(self, stream: BinaryIO, *, asset_id: str, ext: str, content_type: str, max_bytes: int) -> StoredObject:
        stream.seek(0, 2)
        size = stream.tell()
        stream.seek(0)
        if size > max_bytes:
            raise FileTooLarge()
        key = new_key(asset_id, ext)
        object_key = self._object_key(key)
        extra = {
            'ContentType': content_type,
            'ServerSideEncryption': 'aws:kms',
            'SSEKMSKeyId': self.kms_key_id,
            'ChecksumAlgorithm': 'SHA256',  # S3 rejects the upload if the bytes change in transit
            'Tagging': urlencode({'pvo-asset': asset_id}),
        }
        try:
            self.client.upload_fileobj(stream, self.bucket, object_key, ExtraArgs=extra)  # multipart above 8 MB
            head = self.client.head_object(Bucket=self.bucket, Key=object_key)
        except Exception as exc:  # botocore errors; their text can include request ids, never credentials
            self._delete_quietly(object_key)
            raise StorageError(f'S3 upload failed ({type(exc).__name__}).') from exc
        if int(head.get('ContentLength', -1)) != size:
            self._delete_quietly(object_key)
            raise StorageError('S3 upload size mismatch.')
        return StoredObject(key, size, head.get('VersionId'))

    def open(self, key: str) -> BinaryIO:
        try:
            return self.client.get_object(Bucket=self.bucket, Key=self._object_key(key))['Body']
        except Exception as exc:
            raise StorageError(f'S3 read failed ({type(exc).__name__}).') from exc

    def delete(self, key: str) -> None:
        try:
            self.client.delete_object(Bucket=self.bucket, Key=self._object_key(key))
        except Exception as exc:
            raise StorageError(f'S3 delete failed ({type(exc).__name__}).') from exc

    def _delete_quietly(self, object_key: str) -> None:
        try:
            self.client.delete_object(Bucket=self.bucket, Key=object_key)
        except Exception:  # noqa: BLE001 - best effort; the reconciliation job reports leftovers
            pass


def _s3_client(region: str) -> Any:
    try:
        import boto3
        from botocore.config import Config
    except ImportError as exc:  # fail closed: never fall back to local storage silently
        raise RuntimeError('PVO_STORAGE_BACKEND=s3 needs the approved boto3 package installed.') from exc
    config = Config(signature_version='s3v4', retries={'mode': 'standard', 'max_attempts': 5})
    return boto3.client('s3', region_name=region or None, config=config)  # HTTPS with certificate checks (default)


@lru_cache
def get_storage() -> FileStorage:
    s = get_settings()
    if s.storage_backend == 's3':
        return S3FileStorage(s.s3_bucket, s.s3_prefix, s.s3_kms_key_id, _s3_client(s.s3_region))
    return LocalFileStorage(s.upload_dir)
