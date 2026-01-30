import os
import asyncio
import base64
import json
import logging
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import urlencode

import jwt
import requests
from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from .db import USERS_COLLECTION
from . import auth0_mgmt
from api.tts_service import tts_service
from storage import r2_client
from voice_common_names import VOICE_COMMON_NAMES

logger = logging.getLogger(__name__)

ALLOWED_IMAGE_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


def _json_error(message, status=400, **extra):
    payload = {"error": message}
    payload.update(extra)
    return JsonResponse(payload, status=status)


def _generate_token(user_doc):
    payload = {
        "sub": str(user_doc["_id"]),
        "email": user_doc["email"],
        "exp": datetime.utcnow() + timedelta(hours=12),
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm="HS256")


def _require_auth(request):
    auth_header = request.headers.get("Authorization") or request.META.get("HTTP_AUTHORIZATION")
    if not auth_header or not auth_header.lower().startswith("bearer "):
        return None, _json_error("Authorization header missing.", status=401)
    token = auth_header.split(" ", 1)[1]
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"])
        return payload, None
    except jwt.ExpiredSignatureError:
        return None, _json_error("Token expired.", status=401)
    except jwt.InvalidTokenError:
        return None, _json_error("Invalid token.", status=401)


def _resolve_image_upload(data: dict) -> tuple[str, str]:
    """
    Determine the file extension and content type for profile uploads.
    """
    extension = (
        data.get("extension")
        or data.get("file_extension")
        or data.get("ext")
        or ""
    )
    extension = extension.strip().lower()
    if extension and not extension.startswith("."):
        extension = f".{extension}"

    content_type = (data.get("content_type") or "").strip().lower()

    if extension:
        if extension not in ALLOWED_IMAGE_TYPES:
            raise ValueError("Unsupported image extension.")
        inferred = ALLOWED_IMAGE_TYPES[extension]
        if content_type and content_type != inferred:
            raise ValueError("Content type does not match extension.")
        return extension, inferred

    if content_type:
        for ext, ct in ALLOWED_IMAGE_TYPES.items():
            if ct == content_type:
                return ext, ct
        raise ValueError("Unsupported content type.")

    return ".jpg", ALLOWED_IMAGE_TYPES[".jpg"]


def _profile_photo_url(key: Optional[str], *, ensure_exists: bool = True, expires: int = 900) -> Optional[str]:
    if not key:
        return None

    public_base = os.getenv("R2_PUBLIC_DOMAIN_URL") or os.getenv("R2_PROFILE_PUBLIC_BASE_URL")
    if public_base:
        return f"{public_base.rstrip('/')}/{key.lstrip('/')}"

    if not r2_client.is_enabled():
        return None
    if ensure_exists and not r2_client.object_exists(key):
        return None
    # provide a longer-lived signed URL when no public base is configured
    return r2_client.generate_presigned_url(
        key,
        method="get_object",
        expires_in=max(expires, 86400),
    )


def _oauth_settings() -> Tuple[str, str, str, Optional[str], Optional[str]]:
    domain = os.getenv("OAUTH_DOMAIN")
    client_id = os.getenv("OAUTH_CLIENT_ID")
    client_secret = os.getenv("OAUTH_CLIENT_SECRET")
    audience = os.getenv("OAUTH_AUDIENCE")
    default_redirect = os.getenv("OAUTH_REDIRECT_URI")
    if not all([domain, client_id, client_secret]):
        raise RuntimeError("OAuth configuration is incomplete. Set OAUTH_DOMAIN, OAUTH_CLIENT_ID, and OAUTH_CLIENT_SECRET.")
    return domain, client_id, client_secret, audience, default_redirect


def _oauth_upsert_user(profile: dict) -> tuple[dict, bool]:
    email = (profile.get("email") or "").strip().lower()
    if not email:
        raise ValueError("OAuth profile did not include an email address.")

    user = USERS_COLLECTION.find_one({"email": email})
    first_name = (profile.get("given_name") or profile.get("name") or "").split(" ")[0] or "Guest"
    last_name = profile.get("family_name") or ""
    voice = profile.get("app_metadata", {}).get("voice") or os.getenv("PIPER_DEFAULT_VOICE", "en_US-hfc_male-medium")

    if user:
        update = {
            "first_name": first_name or user.get("first_name"),
            "last_name": last_name or user.get("last_name"),
            "updated_at": datetime.utcnow(),
        }
        USERS_COLLECTION.update_one({"_id": user["_id"]}, {"$set": update})
        user.update(update)
        return user, False

    now = datetime.utcnow()
    doc = {
        "first_name": first_name,
        "last_name": last_name,
        "email": email,
        "voice": voice,
        "created_at": now,
        "updated_at": now,
        "oauth_provider": "google",
    }
    result = USERS_COLLECTION.insert_one(doc)
    doc["_id"] = result.inserted_id
    return doc, True


@csrf_exempt
def signup(request):
    if request.method != "POST":
        return _json_error("Method not allowed", status=405)

    try:
        data = json.loads(request.body.decode("utf-8"))
    except json.JSONDecodeError:
        return _json_error("Invalid JSON payload")

    first_name = (data.get("first_name") or "").strip()
    last_name = (data.get("last_name") or "").strip()
    email = (data.get("email") or "").strip().lower()
    password = data.get("password")
    voice = (data.get("voice") or "").strip() or os.getenv("PIPER_DEFAULT_VOICE", "en_US-amy-medium")

    if not all([first_name, last_name, email, password]):
        return _json_error("All fields are required: first_name, last_name, email, password.")

    # Check if user exists in MongoDB
    existing_user = USERS_COLLECTION.find_one({"email": email})
    if existing_user:
        logger.warning(f"User {email} already exists in MongoDB")
        # If user exists in MongoDB but not in Auth0, we'll still allow the process to continue
        # This handles the case where Auth0 user creation previously failed
        if auth0_mgmt.is_configured() and not auth0_mgmt.user_exists(email):
            logger.info(f"User {email} exists in MongoDB but not in Auth0, will attempt to create Auth0 user")
        else:
            return _json_error("An account with this email already exists.", status=409, errorCode="account_exists")

    hashed_password = make_password(password)
    auth0_id = None
    
    # If user exists in MongoDB but not in Auth0, try to get the auth0_id from the existing user
    if existing_user and 'auth0_id' in existing_user:
        auth0_id = existing_user['auth0_id']
    if auth0_mgmt.is_configured():
        try:
            # First, try to get the user by email
            auth0_user = auth0_mgmt.get_user_by_email(email)
            if auth0_user:
                auth0_id = auth0_user.get('user_id')
                logger.info(f"Found existing Auth0 user: {auth0_id}")
            else:
                # User doesn't exist, try to create
                logger.info(f"Attempting to create Auth0 user for email: {email}")
                created = auth0_mgmt.create_user(
                    email=email,
                    password=password,
                    first_name=first_name,
                    last_name=last_name,
                )
                auth0_id = created.get("user_id")
                logger.info(f"Successfully created Auth0 user: {auth0_id}")
                
        except Exception as exc:
            # If we get a 409, try to get the user again
            if hasattr(exc, 'response') and hasattr(exc.response, 'status_code') and exc.response.status_code == 409:
                logger.warning(f"Auth0 reported conflict for {email}, attempting to fetch user...")
                auth0_user = auth0_mgmt.get_user_by_email(email)
                if auth0_user:
                    auth0_id = auth0_user.get('user_id')
                    logger.info(f"Retrieved existing Auth0 user after conflict: {auth0_id}")
                else:
                    logger.error("Auth0 reported conflict but user not found. This might indicate a race condition.")
            else:
                logger.error(f"Auth0 operation failed for {email}", exc_info=True)
                logger.warning("Proceeding with local user creation despite Auth0 failure")
    else:
        logger.warning("Auth0 is not configured. User will be created locally only.")

    now = datetime.utcnow()
    update_doc = {
        "first_name": first_name,
        "last_name": last_name,
        "password": hashed_password,
        "updated_at": now,
        "voice": voice,
    }
    
    if existing_user:
        # Update existing user
        USERS_COLLECTION.update_one(
            {"email": email},
            {"$set": {**update_doc, "auth0_id": auth0_id}}
        )
        doc = {**existing_user, **update_doc, "auth0_id": auth0_id}
    else:
        # Create new user
        doc = {
            **update_doc,
            "email": email,
            "created_at": now,
            "email_verified": False,
            "auth0_id": auth0_id,
        }
        USERS_COLLECTION.insert_one(doc)

    result = USERS_COLLECTION.insert_one(doc)
    doc["_id"] = result.inserted_id
    token = _generate_token(doc)

    response = {
        "message": "Signup successful",
        "token": token,
    }
    response["user"] = {
        "email": doc["email"],
        "profile_photo_url": _profile_photo_url(doc.get("profile_photo_key")),
    }

    return JsonResponse(response, status=201)


@csrf_exempt
def signin(request):
    if request.method != "POST":
        return _json_error("Method not allowed", status=405)

    try:
        data = json.loads(request.body.decode("utf-8"))
    except json.JSONDecodeError:
        return _json_error("Invalid JSON payload")

    email = (data.get("email") or "").strip().lower()
    password = data.get("password")

    if not email or not password:
        return _json_error("Email and password are required.")

    user = USERS_COLLECTION.find_one({"email": email})
    if not user or not check_password(password, user.get("password", "")):
        return _json_error("Invalid credentials.", status=401)

    auth0_id = user.get("auth0_id")
    if auth0_mgmt.is_configured():
        if not auth0_id:
            auth0_record = None
            try:
                auth0_record = auth0_mgmt.find_user_by_email(email)
            except Exception as exc:  # pragma: no cover - best effort lookup
                logger.warning("Auth0 lookup by email failed for %s: %s", email, exc)
            if auth0_record is None:
                try:
                    auth0_record = auth0_mgmt.create_user(
                        email=email,
                        password=password,
                        first_name=user.get("first_name") or "",
                        last_name=user.get("last_name") or "",
                    )
                except Exception as exc:  # pragma: no cover - legacy fallback
                    logger.warning("Auth0 provisioning during signin failed for %s: %s", email, exc)
            if auth0_record:
                auth0_id = auth0_record.get("user_id")
                USERS_COLLECTION.update_one(
                    {"_id": user["_id"]},
                    {"$set": {"auth0_id": auth0_id, "email_verified": False}},
                )
                user["auth0_id"] = auth0_id
                user["email_verified"] = False
        if auth0_id:
            try:
                auth0_profile = auth0_mgmt.get_user(auth0_id)
            except Exception as exc:  # pragma: no cover - network errors
                logger.warning("Auth0 lookup failed for %s: %s", email, exc)
                auth0_profile = None
            if auth0_profile is not None:
                verified = bool(auth0_profile.get("email_verified"))
                USERS_COLLECTION.update_one(
                    {"_id": user["_id"]},
                    {"$set": {"email_verified": verified}},
                )
                user["email_verified"] = verified
            else:
                verified = bool(user.get("email_verified"))
            if not verified:
                auth0_mgmt.trigger_verification_email(auth0_id)
                return _json_error(
                    "Please verify your email address. We've sent a verification email through Auth0.",
                    status=403,
                )
        else:
            return _json_error(
                "Unable to verify email right now. Please try again in a few minutes.",
                status=503,
                errorCode="email"
            )

    token = _generate_token(user)
    user_voice = user.get("voice") or os.getenv("PIPER_DEFAULT_VOICE", "en_US-hfc_male-medium")
    user_payload = {
        "first_name": user.get("first_name"),
        "last_name": user.get("last_name"),
        "email": user.get("email"),
        "voice": user_voice,
        "voice_common_name": VOICE_COMMON_NAMES.get(user_voice, user_voice),
        "role": user.get("role"),
    }
    user_payload["profile_photo_url"] = _profile_photo_url(user.get("profile_photo_key"))

    return JsonResponse(
        {
            "message": "Signin successful",
            "token": token,
            "user": user_payload,
        }
    )


@csrf_exempt
def oauth_login_url(request):
    if request.method not in ("GET", "POST"):
        return _json_error("Method not allowed", status=405)

    body: dict = {}
    if request.method == "POST":
        try:
            body = json.loads(request.body.decode("utf-8"))
        except json.JSONDecodeError:
            return _json_error("Invalid JSON payload")
    domain, client_id, _, audience, default_redirect = _oauth_settings()
    redirect_uri = (
        body.get("redirect_uri")
        or request.GET.get("redirect_uri")
        or default_redirect
    )
    if not redirect_uri:
        return _json_error("redirect_uri is required.")

    state = body.get("state") or request.GET.get("state") or secrets.token_urlsafe(16)
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": body.get("scope") or request.GET.get("scope") or "openid profile email",
        "state": state,
        "connection": "google-oauth2",
        "prompt": body.get("prompt") or request.GET.get("prompt") or "login",
    }
    audience_param = body.get("audience") or request.GET.get("audience") or audience
    if audience_param:
        params["audience"] = audience_param

    authorize_url = f"https://{domain}/authorize?{urlencode(params)}"
    return JsonResponse({"authorize_url": authorize_url, "state": state})


@csrf_exempt
def oauth_callback(request):
    if request.method != "POST":
        return _json_error("Method not allowed", status=405)

    try:
        data = json.loads(request.body.decode("utf-8"))
    except json.JSONDecodeError:
        return _json_error("Invalid JSON payload")

    code = data.get("code")
    redirect_uri = data.get("redirect_uri")
    if not code:
        return _json_error("Authorization code is required.")

    domain, client_id, client_secret, _, default_redirect = _oauth_settings()
    redirect_uri = redirect_uri or default_redirect
    if not redirect_uri:
        return _json_error("redirect_uri is required.")

    token_payload = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "redirect_uri": redirect_uri,
    }

    token_url = f"https://{domain}/oauth/token"
    try:
        token_response = requests.post(token_url, data=token_payload, timeout=10)
    except requests.RequestException as exc:
        return _json_error("OAuth token request failed.", status=502, details=str(exc))
    if token_response.status_code >= 400:
        try:
            error_detail = token_response.json()
        except ValueError:
            error_detail = token_response.text
        return _json_error("Failed to exchange authorization code.", status=502, details=error_detail)

    token_data = token_response.json()
    access_token = token_data.get("access_token")
    if not access_token:
        return _json_error("OAuth provider did not return an access token.", status=502)

    try:
        userinfo_resp = requests.get(
            f"https://{domain}/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
    except requests.RequestException as exc:
        return _json_error("OAuth userinfo request failed.", status=502, details=str(exc))
    if userinfo_resp.status_code >= 400:
        return _json_error("Failed to fetch user profile from OAuth provider.", status=502)

    profile = userinfo_resp.json()
    try:
        user_doc, created = _oauth_upsert_user(profile)
    except ValueError as exc:
        return _json_error(str(exc), status=400)

    token = _generate_token(user_doc)
    user_voice = user_doc.get("voice") or os.getenv("PIPER_DEFAULT_VOICE", "en_US-hfc_male-medium")
    user_payload = {
        "first_name": user_doc.get("first_name"),
        "last_name": user_doc.get("last_name"),
        "email": user_doc.get("email"),
        "voice": user_voice,
        "voice_common_name": VOICE_COMMON_NAMES.get(user_voice, user_voice),
        "role": user_doc.get("role"),
    }
    user_payload["profile_photo_url"] = _profile_photo_url(user_doc.get("profile_photo_key"))

    return JsonResponse(
        {
            "message": "Signin successful" if not created else "Signup successful",
            "token": token,
            "user": user_payload,
            "oauth": {
                "access_token": access_token,
                "expires_in": token_data.get("expires_in"),
                "id_token": token_data.get("id_token"),
                "scope": token_data.get("scope"),
                "token_type": token_data.get("token_type"),
            },
        }
    )


VOICE_SAMPLE_TEXT = (
    "Hi, I am your Hear Helper Assistant. I am all excited to know about your favorite books."
)
SAMPLE_CACHE_DIR = settings.BASE_DIR / "voice_samples"


def available_models(request):
    if request.method != "GET":
        return _json_error("Method not allowed", status=405)

    payload, error_response = _require_auth(request)
    if error_response:
        return error_response

    voices = tts_service.available_voices()
    models = []
    for voice_id in voices:
        sample = _load_or_generate_voice_sample(voice_id)
        entry = {"id": voice_id, "common_name": VOICE_COMMON_NAMES.get(voice_id, voice_id)}
        if sample:
            entry["sample"] = sample
        models.append(entry)

    return JsonResponse(
        {
            "models": models,
            "count": len(models),
        }
    )


@csrf_exempt
def profile_photo_upload_url(request):
    if request.method != "POST":
        return _json_error("Method not allowed", status=405)

    payload, error_response = _require_auth(request)
    if error_response:
        return error_response

    try:
        data = json.loads(request.body.decode("utf-8"))
    except json.JSONDecodeError:
        return _json_error("Invalid JSON payload")

    try:
        extension, content_type = _resolve_image_upload(data)
    except ValueError as exc:
        return _json_error(str(exc))

    if not r2_client.is_enabled():
        return _json_error("Cloud storage is not configured.", status=503)

    key_prefix = os.getenv("R2_PROFILE_DIR", "users")
    user_id = payload.get("sub") or payload["email"]
    remote_key = f"{key_prefix.rstrip('/')}/{user_id}{extension}"

    upload_url = r2_client.generate_presigned_url(
        remote_key,
        method="put_object",
        expires_in=900,
        content_type=content_type,
    )

    if upload_url is None:
        return _json_error("Failed to generate upload URL.", status=502)

    USERS_COLLECTION.update_one(
        {"email": payload["email"]},
        {"$set": {"profile_photo_key": remote_key, "updated_at": datetime.utcnow()}},
    )

    profile_url = _profile_photo_url(remote_key, ensure_exists=False)

    return JsonResponse(
        {
            "upload_url": upload_url,
            "download_url": profile_url,
            "profile_photo_url": profile_url,
            "object_key": remote_key,
            "content_type": content_type,
            "expires_in": 900,
        }
    )




@csrf_exempt
def update_voice(request):
    if request.method != "POST":
        return _json_error("Method not allowed", status=405)

    payload, error_response = _require_auth(request)
    if error_response:
        return error_response

    try:
        data = json.loads(request.body.decode("utf-8"))
    except json.JSONDecodeError:
        return _json_error("Invalid JSON payload")

    voice = (data.get("voice") or "").strip()
    if not voice:
        return _json_error("Voice is required.")

    if voice not in tts_service.available_voices():
        return _json_error("Unknown voice id.", status=400)

    result = USERS_COLLECTION.update_one(
        {"email": payload["email"]},
        {"$set": {"voice": voice, "updated_at": datetime.utcnow()}},
    )
    if result.matched_count == 0:
        return _json_error("User not found.", status=404)

    return JsonResponse({
        "message": "Voice updated.",
        "voice": voice,
        "voice_common_name": VOICE_COMMON_NAMES.get(voice, voice),
    })


def _load_or_generate_voice_sample(voice_id: str):
    remote_key = f"voice_samples/{voice_id}.json"
    if r2_client.is_enabled():
        data = r2_client.download_bytes(remote_key)
        if data:
            try:
                return json.loads(data.decode("utf-8"))
            except json.JSONDecodeError:
                pass

    cache_path = SAMPLE_CACHE_DIR / f"{voice_id}.json"
    if cache_path.exists() and not r2_client.is_enabled():
        try:
            return json.loads(cache_path.read_text())
        except json.JSONDecodeError:
            cache_path.unlink(missing_ok=True)

    sample = _generate_voice_sample(voice_id)
    if not sample:
        return None

    if r2_client.is_enabled():
        r2_client.upload_bytes(json.dumps(sample).encode("utf-8"), remote_key)
    else:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(sample), encoding="utf-8")

    return sample


def _generate_voice_sample(voice_id: str):
    try:
        audio_data, error, used_voice, audio_format = asyncio.run(
            tts_service.text_to_speech(
                VOICE_SAMPLE_TEXT,
                voice_id=voice_id,
                preferred_format="mp3",
            )
        )
    except Exception:
        return None

    if error or not audio_data:
        return None

    audio_b64 = base64.b64encode(audio_data).decode("utf-8")
    return {
        "format": audio_format,
        "text": VOICE_SAMPLE_TEXT,
        "data": audio_b64,
    }
