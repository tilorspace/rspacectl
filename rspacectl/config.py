"""Configuration loading for rspacectl.

Credential resolution order (first match wins):
  1. Explicit CLI flags (--url, --api-key)
  2. Environment variables (RSPACE_URL, RSPACE_API_KEY)
  3. OS keychain  (requires: pip install rspacectl[keychain])
  4. Profile dotenv file (~/.rspacectl  or  ~/.rspacectl.<profile>)

Named profiles
--------------
The default profile reads/writes ~/.rspacectl (backward compatible).
Any other profile name uses ~/.rspacectl.<name>, e.g. ~/.rspacectl.staging.

Select a profile with the global --profile flag:

    rspace --profile staging list samples

Keychain storage
----------------
Store credentials in the OS keychain (macOS Keychain, Windows Credential
Manager, Linux Secret Service) instead of a plain-text file:

    rspace configure --keychain
    rspace configure --profile staging --keychain

The keychain service name is "rspacectl" for the default profile,
"rspacectl.<name>" for named profiles.
"""

import os
from pathlib import Path
from typing import Optional, Tuple

from dotenv import load_dotenv

_CONFIG_DIR = Path.home()
DEFAULT_PROFILE = "default"
URL_KEY = "RSPACE_URL"
APIKEY_KEY = "RSPACE_API_KEY"
_KEYCHAIN_SERVICE_BASE = "rspacectl"

# Keep this as a convenience alias used by configure.py
CONFIG_FILE = _CONFIG_DIR / ".rspacectl"


class ConfigError(Exception):
    """Raised when required configuration is missing."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _config_file(profile: str) -> Path:
    if profile == DEFAULT_PROFILE:
        return _CONFIG_DIR / ".rspacectl"
    return _CONFIG_DIR / f".rspacectl.{profile}"


def _keychain_service(profile: str) -> str:
    if profile == DEFAULT_PROFILE:
        return _KEYCHAIN_SERVICE_BASE
    return f"{_KEYCHAIN_SERVICE_BASE}.{profile}"


def _load_from_keychain(profile: str) -> Tuple[Optional[str], Optional[str]]:
    """Return (url, api_key) from the OS keychain, or (None, None) if unavailable."""
    try:
        import keyring  # type: ignore[import]
    except ImportError:
        return None, None
    service = _keychain_service(profile)
    url = keyring.get_password(service, "url")
    api_key = keyring.get_password(service, "api_key")
    return url or None, api_key or None


def _save_to_keychain(url: str, api_key: str, profile: str) -> None:
    """Store credentials in the OS keychain. Raises ConfigError if keyring not installed."""
    try:
        import keyring  # type: ignore[import]
    except ImportError:
        raise ConfigError(
            "The 'keyring' package is required for keychain storage.\n"
            "Install it with:  pip install rspacectl[keychain]"
        )
    service = _keychain_service(profile)
    keyring.set_password(service, "url", url.rstrip("/"))
    keyring.set_password(service, "api_key", api_key)


def _delete_from_keychain(profile: str) -> None:
    """Remove a profile's credentials from the OS keychain (best-effort)."""
    try:
        import keyring  # type: ignore[import]
        from keyring.errors import PasswordDeleteError  # type: ignore[import]
    except ImportError:
        return
    service = _keychain_service(profile)
    for username in ("url", "api_key"):
        try:
            keyring.delete_password(service, username)
        except PasswordDeleteError:
            pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_config(
    url_override: Optional[str] = None,
    api_key_override: Optional[str] = None,
    profile: str = DEFAULT_PROFILE,
) -> Tuple[str, str]:
    """Load RSpace URL and API key for the given profile.

    Resolution order:
      1. Explicit overrides (CLI flags)
      2. Environment variables
      3. OS keychain
      4. Profile dotenv file

    For named profiles the dotenv file overrides env vars (the profile is an
    explicit choice, not an ambient environment setting).

    Returns:
        (url, api_key) tuple

    Raises:
        ConfigError: if URL or API key cannot be resolved
    """
    config_file = _config_file(profile)

    # Step 4 — load dotenv file into env (non-default profiles override env vars
    # because the user explicitly selected that profile)
    if config_file.exists():
        override = profile != DEFAULT_PROFILE
        load_dotenv(config_file, override=override)

    # Steps 1 & 2 — CLI flags and env vars
    url = url_override or os.environ.get(URL_KEY)
    api_key = api_key_override or os.environ.get(APIKEY_KEY)

    # Step 3 — keychain fallback
    if not url or not api_key:
        kc_url, kc_key = _load_from_keychain(profile)
        url = url or kc_url
        api_key = api_key or kc_key

    missing = []
    if not url:
        missing.append(URL_KEY)
    if not api_key:
        missing.append(APIKEY_KEY)

    if missing:
        profile_hint = f" (profile: {profile})" if profile != DEFAULT_PROFILE else ""
        raise ConfigError(
            f"Missing configuration{profile_hint}: {', '.join(missing)}\n"
            f"Run 'rspace configure' to set up credentials, or set environment variables."
        )

    if url.startswith("http://"):
        from .output import err_console
        err_console.print(
            "[yellow]Warning:[/yellow] RSPACE_URL uses HTTP — your API key will be sent "
            "unencrypted. Use HTTPS unless this is a local development server."
        )

    return url.rstrip("/"), api_key


def save_config(
    url: str,
    api_key: str,
    profile: str = DEFAULT_PROFILE,
    use_keychain: bool = False,
) -> None:
    """Persist credentials for the given profile.

    Args:
        url:           RSpace server URL.
        api_key:       RSpace API key.
        profile:       Profile name (default: "default").
        use_keychain:  Store in OS keychain instead of a dotenv file.
    """
    if use_keychain:
        _save_to_keychain(url, api_key, profile)
        return

    # File-based storage — created with mode 600 atomically.
    import os as _os
    config_file = _config_file(profile)
    content = f"{URL_KEY}={url.rstrip('/')}\n{APIKEY_KEY}={api_key}\n"
    fd = _os.open(config_file, _os.O_WRONLY | _os.O_CREAT | _os.O_TRUNC, 0o600)
    try:
        _os.write(fd, content.encode())
    finally:
        _os.close(fd)


def list_profiles() -> list:
    """Return names of all profiles that have a dotenv file or keychain entry.

    Always includes "default" if any config exists.
    """
    profiles = []

    # Scan for dotenv files
    for path in sorted(_CONFIG_DIR.glob(".rspacectl*")):
        if path.name == ".rspacectl":
            profiles.append(DEFAULT_PROFILE)
        elif path.name.startswith(".rspacectl.") and not path.suffix == ".bak":
            profiles.append(path.name[len(".rspacectl."):])

    # Check keychain for any of the found profiles (and default)
    for candidate in set(profiles) | {DEFAULT_PROFILE}:
        kc_url, kc_key = _load_from_keychain(candidate)
        if (kc_url or kc_key) and candidate not in profiles:
            profiles.append(candidate)

    return sorted(set(profiles)) or [DEFAULT_PROFILE]
