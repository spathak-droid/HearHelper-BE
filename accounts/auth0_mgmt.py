import json
import logging
import os
import time
from typing import Dict, Optional, Any

import requests

logger = logging.getLogger(__name__)

_token_cache: Dict[str, object] = {"value": None, "expires_at": 0.0}


def _domain() -> Optional[str]:
    return os.getenv("OAUTH_DOMAIN")


def _client_id() -> Optional[str]:
    return os.getenv("AUTH0_MGMT_CLIENT_ID")


def _client_secret() -> Optional[str]:
    return os.getenv("AUTH0_MGMT_CLIENT_SECRET")


def _connection_name() -> str:
    return os.getenv("AUTH0_DB_CONNECTION") or "Username-Password-Authentication"


def is_configured() -> bool:
    return all([_domain(), _client_id(), _client_secret()])


def _audience(domain: str) -> str:
    return f"https://{domain}/api/v2/"


def _management_token() -> str:
    now = time.time()
    cached = _token_cache.get("value")
    expires_at = float(_token_cache.get("expires_at") or 0)
    if cached and now < expires_at - 30:
        return cached  # type: ignore[return-value]

    domain = _domain()
    client_id = _client_id()
    client_secret = _client_secret()
    if not all([domain, client_id, client_secret]):
        raise RuntimeError("Auth0 management credentials are not configured.")

    token_url = f"https://{domain}/oauth/token"
    payload = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
        "audience": _audience(domain),
    }
    try:
        response = requests.post(token_url, json=payload, timeout=10)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(f"Auth0 token request failed: {exc}") from exc

    data = response.json()
    token = data.get("access_token")
    expires_in = data.get("expires_in") or 3600
    if not token:
        raise RuntimeError("Auth0 token response missing access_token.")

    _token_cache["value"] = token
    _token_cache["expires_at"] = now + float(expires_in)
    return token


def _headers() -> dict:
    token = _management_token()
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def create_user(email: str, password: str, first_name: str, last_name: str) -> Dict[str, Any]:
    """Create a new user in Auth0.
    
    Args:
        email: User's email address
        password: User's password (must meet Auth0 complexity requirements)
        first_name: User's first name
        last_name: User's last name
        
    Returns:
        Dict containing the created user's information
        
    Raises:
        RuntimeError: If Auth0 is not configured or user creation fails
        requests.RequestException: For HTTP request errors
    """
    if not is_configured():
        raise RuntimeError("Auth0 management integration is not configured. Check your environment variables.")

    domain = _domain()
    if not domain:
        raise RuntimeError("OAUTH_DOMAIN environment variable is not set.")
        
    url = f"https://{domain}/api/v2/users"
    
    # Prepare user data with validation
    if not all([email, password, first_name, last_name]):
        raise ValueError("All user fields are required.")
        
    if len(password) < 8:
        raise ValueError("Password must be at least 8 characters long.")
    
    payload = {
        "connection": _connection_name(),
        "email": email.lower().strip(),
        "password": password,
        "given_name": first_name.strip(),
        "family_name": last_name.strip(),
        "name": f"{first_name.strip()} {last_name.strip()}".strip(),
        "email_verified": False,
        "verify_email": True,
    }
    try:
        logger.info(f"Creating Auth0 user with payload: {json.dumps(payload, indent=2)}")
        response = requests.post(url, json=payload, headers=_headers(), timeout=10)
        
        # Log the full response for debugging
        logger.info(f"Auth0 API response status: {response.status_code}")
        logger.info(f"Auth0 API response body: {response.text}")
        
        response.raise_for_status()
    except requests.RequestException as exc:
        error_msg = f"Auth0 user creation failed: {exc}"
        if hasattr(exc, 'response') and exc.response is not None:
            try:
                error_data = exc.response.json()
                error_msg = f"{error_msg} - {json.dumps(error_data, indent=2)}"
            except:
                error_msg = f"{error_msg} - {exc.response.text}"
        logger.error(error_msg)
        raise RuntimeError(error_msg) from exc

    return response.json()


def get_user(user_id: str) -> Optional[dict]:
    if not is_configured():
        return None
    domain = _domain()
    url = f"https://{domain}/api/v2/users/{user_id}"
    try:
        response = requests.get(url, headers=_headers(), timeout=10)
        if response.status_code == 404:
            return None
        response.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(f"Auth0 user lookup failed: {exc}") from exc
    return response.json()


def trigger_verification_email(user_id: str) -> None:
    if not is_configured():
        return
    domain = _domain()
    url = f"https://{domain}/api/v2/jobs/verification-email"
    payload = {"user_id": user_id}
    try:
        response = requests.post(url, json=payload, headers=_headers(), timeout=10)
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("Auth0 verification email trigger failed: %s", exc)


def get_user_by_email(email: str) -> Optional[dict]:
    """Get a user from Auth0 by email.
    
    Args:
        email: The email address to look up
        
    Returns:
        Optional[dict]: User data if found, None otherwise
    """
    if not is_configured():
        return None

    domain = _domain()
    if not domain:
        return None
        
    url = f"https://{domain}/api/v2/users-by-email"
    params = {"email": email.lower().strip()}
    
    try:
        response = requests.get(url, params=params, headers=_headers(), timeout=10)
        if response.status_code == 200:
            users = response.json()
            # Find a user with matching email (case-insensitive)
            for user in users:
                if user.get('email', '').lower() == email.lower():
                    return user
        return None
    except requests.RequestException as exc:
        logger.error(f"Error getting user from Auth0: {exc}")
        return None


def user_exists(email: str) -> bool:
    """Check if a user exists in Auth0 by email.
    
    Args:
        email: The email address to check
        
    Returns:
        bool: True if user exists, False otherwise or if an error occurs
    """
    return get_user_by_email(email) is not None


def find_user_by_email(email: str) -> Optional[dict]:
    """Find a user in Auth0 by email.
    
    Args:
        email: The email address to search for
        
    Returns:
        Optional[dict]: User data if found, None otherwise
    """
    if not is_configured():
        return None
    domain = _domain()
    url = f"https://{domain}/api/v2/users-by-email"
    params = {"email": email}
    try:
        response = requests.get(url, headers=_headers(), params=params, timeout=10)
        if response.status_code == 404:
            return None
        response.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(f"Auth0 user search failed: {exc}") from exc
    records = response.json()
    if not isinstance(records, list) or not records:
        return None
    return records[0]
