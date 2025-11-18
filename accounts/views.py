import os
import asyncio
import base64
import json
from datetime import datetime, timedelta

import jwt
from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from .db import USERS_COLLECTION
from voice_common_names import VOICE_COMMON_NAMES
from api.tts_service import tts_service


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
    voice = (data.get("voice") or "").strip() or os.getenv("PIPER_DEFAULT_VOICE", "en_US-hfc_male-medium")

    if not all([first_name, last_name, email, password]):
        return _json_error("All fields are required: first_name, last_name, email, password.")

    if USERS_COLLECTION.find_one({"email": email}):
        return _json_error("An account with this email already exists.", status=409)

    hashed_password = make_password(password)
    now = datetime.utcnow()
    doc = {
        "first_name": first_name,
        "last_name": last_name,
        "email": email,
        "password": hashed_password,
        "created_at": now,
        "updated_at": now,
        "voice": voice,
    }

    result = USERS_COLLECTION.insert_one(doc)
    doc["_id"] = result.inserted_id
    token = _generate_token(doc)

    return JsonResponse(
        {
            "message": "Signup successful",
            "token": token,
        },
        status=201,
    )


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

    token = _generate_token(user)
    return JsonResponse(
        {
            "message": "Signin successful",
            "token": token,
            "user": {
                "first_name": user.get("first_name"),
                "last_name": user.get("last_name"),
                "email": user.get("email"),
                "voice": user.get("voice") or os.getenv("PIPER_DEFAULT_VOICE", "en_US-hfc_male-medium"),
            },
        }
    )


VOICE_SAMPLE_TEXT = (
    "Hi, I am your Hear Helper Assistant. I am all excited to know about your favorite books."
)


def available_models(request):
    if request.method != "GET":
        return _json_error("Method not allowed", status=405)

    payload, error_response = _require_auth(request)
    if error_response:
        return error_response

    voices = tts_service.available_voices()
    models = []
    for voice_id in voices:
        sample = _generate_voice_sample(voice_id)
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

    return JsonResponse({"message": "Voice updated.", "voice": voice})


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
