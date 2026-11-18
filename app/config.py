"""Settings read from environment variables (prefix PVO_) or a local .env file."""
from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # The AI team's SQLite file (structx.sqlite). Read-only from this API. Empty = the /structx routes answer 503.
    # Keep it outside synced folders such as OneDrive: a sync client can corrupt a live SQLite file.
    # The AI team's database setup (init_db.py, its README) is in the structx_db/ folder; the extraction package is structx/.
    structx_db_path: str = ''

    cors_origins: list[str] = []
    # Where uploaded source documents are saved (<dir>/<asset code>/<file>). Empty = uploads answer 503. Keep it outside synced
    # folders such as OneDrive, and make sure the platform's malware scanner covers it.
    upload_dir: str = ''
    max_upload_bytes: int = 50 * 1024 * 1024  # per file
    max_json_bytes: int = 2 * 1024 * 1024

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
