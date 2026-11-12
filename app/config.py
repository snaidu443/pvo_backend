"""Settings read from environment variables (prefix PVO_) or a local .env file."""
import re
from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

_BUCKET = re.compile(r'^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$')
_PREFIX = re.compile(r'^[a-z0-9][a-z0-9/_-]{0,99}$')


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', env_prefix='PVO_', extra='ignore')

    environment: str = 'local'  # local | dev | test | prod
    api_prefix: str = '/api'
    database_url: str = 'sqlite:///./pvo_local.db'

    okta_issuer: str = ''
    okta_audience: str = ''
    okta_client_id: str = ''
    okta_required_scope: str = 'pvo.api'
    okta_jwks_url: str = ''

    # LOCAL ONLY: user name + password sign-in until the Okta app exists (see app/devauth.py).
    # Refused outside local. The test password is chosen in your own .env, never written in code.
    dev_login: bool = False
    dev_password: SecretStr = SecretStr('')

    cors_origins: list[str] = []
    upload_dir: str = './uploads'  # local storage only; keep it outside synced folders such as OneDrive
    max_upload_bytes: int = 50 * 1024 * 1024  # per file; same limit as the React app
    max_json_bytes: int = 2 * 1024 * 1024
    # Data Submission (POST /assets/submissions): one request carries the set-up fields and every file.
    max_files_per_submission: int = 20
    max_files_per_type: int = 5
    max_submission_bytes: int = 200 * 1024 * 1024

    # Where uploaded files are kept: 'local' (upload_dir) or 's3' (private, KMS-encrypted bucket).
    # S3 credentials come from the AWS default chain (IAM role, or `aws sso login` locally); never from this file.
    storage_backend: Literal['local', 's3'] = 'local'
    s3_bucket: str = ''
    s3_prefix: str = 'pvo/local'  # e.g. pvo/dev; fixed per environment (object key = prefix + '/' + stored key)
    s3_kms_key_id: str = ''  # key id, ARN or alias of the customer-managed KMS key (an identifier, not a secret)
    s3_region: str = ''

    @property
    def jwks_url(self) -> str:
        return self.okta_jwks_url or f'{self.okta_issuer.rstrip("/")}/v1/keys'

    @property
    def docs_enabled(self) -> bool:
        return self.environment == 'local'

    def check(self) -> None:
        """Refuse to start with an unsafe configuration outside local development (fail closed)."""
        if self.dev_login and self.environment != 'local':
            raise RuntimeError('PVO_DEV_LOGIN is for local development only.')
        if self.dev_login and not self.dev_password.get_secret_value():
            raise RuntimeError('PVO_DEV_LOGIN=true needs PVO_DEV_PASSWORD (your local test password) in .env.')
        if self.storage_backend == 's3':
            if not _BUCKET.match(self.s3_bucket):
                raise RuntimeError('PVO_S3_BUCKET must be a valid S3 bucket name.')
            if not self.s3_kms_key_id:
                raise RuntimeError('PVO_S3_KMS_KEY_ID is required: uploads are always encrypted with the KMS key.')
            if not _PREFIX.match(self.s3_prefix) or '..' in self.s3_prefix:
                raise RuntimeError('PVO_S3_PREFIX may only contain a-z, 0-9, "-", "_" and "/" (e.g. pvo/dev).')
        if self.environment == 'prod' and self.storage_backend != 's3':
            raise RuntimeError('Production stores documents in S3 (PVO_STORAGE_BACKEND=s3).')
        if self.environment == 'local':
            return
        if not self.okta_issuer.startswith('https://') or not self.okta_audience:
            raise RuntimeError('PVO_OKTA_ISSUER (https) and PVO_OKTA_AUDIENCE must be set.')
        if self.environment == 'prod' and self.database_url.startswith('sqlite'):
            raise RuntimeError('SQLite is for local development only.')
        if any(o == '*' or not o.startswith('https://') for o in self.cors_origins):
            raise RuntimeError('PVO_CORS_ORIGINS must list exact https:// origins.')


@lru_cache
def get_settings() -> Settings:
    return Settings()
