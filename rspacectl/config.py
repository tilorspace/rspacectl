"""Configuration loading for rspacectl.

Priority order:
1. Explicit CLI flags (--url, --api-key)
2. Environment variables (RSPACE_URL, RSPACE_API_KEY)
3. Dotenv file at ~/.rspacectl
"""

import os
from pathlib import Path
from typing import Optional, Tuple

from dotenv import load_dotenv

CONFIG_FILE = Path.home() / ".rspacectl"
URL_KEY = "RSPACE_URL"
APIKEY_KEY = "RSPACE_API_KEY"


class ConfigError(Exception):
    """Raised when required configuration is missing."""


def load_config(
    url_override: Optional[str] = None,
    api_key_override: Optional[str] = None,
) -> Tuple[str, str]:
    """Load RSpace URL and API key.

    Checks, in order:
    1. Explicit overrides (from CLI flags)
    2. Environment variables
    3. ~/.rspacectl dotenv file

    Returns:
        (url, api_key) tuple

    Raises:
        ConfigError: if URL or API key cannot be found
    """
    # Load dotenv file if it exists (won't override already-set env vars)
    if CONFIG_FILE.exists():
        load_dotenv(CONFIG_FILE, override=False)

    url = url_override or os.environ.get(URL_KEY)
    api_key = api_key_override or os.environ.get(APIKEY_KEY)

    missing = []
    if not url:
        missing.append(URL_KEY)
    if not api_key:
        missing.append(APIKEY_KEY)

    if missing:
        raise ConfigError(
            f"Missing configuration: {', '.join(missing)}\n"
            f"Run 'rspace configure' to set up credentials, or set environment variables."
        )

    return url.rstrip("/"), api_key


def save_config(url: str, api_key: str) -> None:
    """Write credentials to ~/.rspacectl."""
    CONFIG_FILE.write_text(
        f"{URL_KEY}={url.rstrip('/')}\n"
        f"{APIKEY_KEY}={api_key}\n"
    )
    CONFIG_FILE.chmod(0o600)  # restrict permissions — contains secrets
