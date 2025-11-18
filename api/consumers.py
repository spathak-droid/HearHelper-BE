# api/consumers.py
import asyncio
import json
import logging
import base64
import jwt
from django.conf import settings
from channels.generic.websocket import AsyncWebsocketConsumer
from .tts_service import tts_service
from .response_service import response_service
from .book_service import BookConverter
from accounts.db import USERS_COLLECTION

logger = logging.getLogger(__name__)

STOPWORDS = {
    "a",
    "an",
    "and",
    "the",
    "of",
    "in",
    "on",
    "for",
    "with",
    "by",
    "at",
    "to",
    "from",
    "into",
    "about",
    "as",
    "is",
    "it",
    "this",
    "that",
    "these",
    "those",
    "be",
}


class ChatConsumer(AsyncWebsocketConsumer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.book_converter = BookConverter()
        self.pending_book_options = None
        self.book_progress = {}
        self.book_access_granted = False
        self.book_access_book_id = None
        self.preferred_voice = None

    async def connect(self):
        try:
            self.room_name = 'room'
            self.room_group_name = f'chat_{self.room_name}'

            await self.channel_layer.group_add(
                self.room_group_name,
                self.channel_name
            )

            await self.accept()
            await self._authenticate_user()
            logger.info(f"WebSocket connected: {self.channel_name}")
            
        except Exception as e:
            logger.error(f"Error in WebSocket connect: {str(e)}")
            await self.close()

    async def _authenticate_user(self):
        token_value = None
        query_string = self.scope.get("query_string", b"").decode()
        if query_string:
            for part in query_string.split("&"):
                if part.startswith("token="):
                    token_value = part.split("=", 1)[1]
                    break
        if not token_value:
            headers = self.scope.get("headers")
            if headers:
                for header_name, header_value in headers:
                    if header_name.decode().lower() == "authorization":
                        value = header_value.decode()
                        if value.lower().startswith("bearer "):
                            token_value = value.split(" ", 1)[1]
                        break
        if not token_value:
            return
        try:
            payload = jwt.decode(token_value, settings.SECRET_KEY, algorithms=["HS256"])
            user = USERS_COLLECTION.find_one({"email": payload.get("email")})
            if user:
                self.preferred_voice = user.get("voice")
        except Exception:
            self.preferred_voice = None

    async def receive(self, text_data):
        message_id = ''
        try:
            text_data_json = json.loads(text_data)
            message_type = text_data_json.get('type', '')
            message_id = text_data_json.get('message_id', '')

            if message_type == 'tts':
                text = text_data_json.get('text') or text_data_json.get('message', '')
                requested_voice = text_data_json.get('voice') or self.preferred_voice
                if not text:
                    raise ValueError("No text provided for TTS")

                reply = response_service.generate_reply(text)

                if await self._handle_book_flow(
                    user_text=text,
                    reply=reply,
                    voice_id=requested_voice,
                    message_id=message_id,
                ):
                    return

                reply_text = reply.response_text

                audio_data, error, used_voice, audio_format = await tts_service.text_to_speech(
                    reply_text,
                    voice_id=requested_voice,
                )

                if error:
                    raise Exception(f"TTS Error: {error}")

                audio_b64 = base64.b64encode(audio_data).decode('utf-8')

                await self.send(text_data=json.dumps({
                    'type': 'tts_result',
                    'message_id': message_id,
                    'request_text': text,
                    'audio': audio_b64,
                    'text': reply_text,
                    'format': audio_format,
                    'voice': used_voice,
                    'match': {
                        'prompt': reply.matched_prompt,
                        'confidence': reply.confidence
                    },
                    'context': reply.context
                }))

        except json.JSONDecodeError as e:
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message_id': message_id,
                'error': 'Invalid JSON format',
                'details': str(e)
            }))
        except Exception as e:
            logger.error(f"Error processing message: {str(e)}", exc_info=True)
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message_id': message_id,
                'error': 'Processing error',
                'details': str(e)
            }))

    async def disconnect(self, close_code):
        try:
            await self.channel_layer.group_discard(
                self.room_group_name,
                self.channel_name
            )
            logger.info(f"WebSocket disconnected: {self.channel_name}")
        except Exception as e:
            logger.error(f"Error in WebSocket disconnect: {str(e)}")

    async def _handle_book_flow(self, user_text, reply, voice_id, message_id):
        text_lower = (user_text or '').strip().lower()

        if self._is_continue_request(text_lower) and await self._continue_book(voice_id, message_id):
            return True

        if reply.intent == 'book_prompt' and reply.context:
            self.pending_book_options = reply.context.get('book_ids') or []
            self.book_progress.pop(self.channel_name, None)
            return False

        if self.pending_book_options:
            selection = self._select_book_from_text(user_text, self.pending_book_options)
            if selection:
                if self._has_reached_book_limit(selection, allow_same=True):
                    await self._send_access_denied(user_text, message_id)
                    self.pending_book_options = None
                    return True
                await self._start_book(selection, voice_id, message_id)
                self.pending_book_options = None
                return True
            if text_lower:
                await self._send_book_not_found_response(user_text, voice_id, message_id)
                return True

        direct_selection = self._select_book_from_text(
            user_text,
            list(self.book_converter.available_books()),
        )
        if direct_selection:
            if self._has_reached_book_limit(direct_selection, allow_same=True):
                await self._send_access_denied(user_text, message_id)
                return True
            await self._start_book(direct_selection, voice_id, message_id)
            return True
        if any(keyword in text_lower for keyword in ('book', 'read', 'story', 'audiobook', 'novel')):
            await self._send_book_not_found_response(user_text, voice_id, message_id)
            return True

        return False

    @staticmethod
    def _is_continue_request(normalized_text):
        if not normalized_text:
            return False
        followup_words = {'next', 'continue', 'keep going', 'more', 'next chunk', 'go on'}
        return any(word in normalized_text for word in followup_words)

    def _select_book_from_text(self, user_text, options):
        normalized = (user_text or '').strip().lower()
        if not normalized:
            return None

        confirmation_words = {'yes', 'yeah', 'yep', 'sure', 'ok', 'okay', 'please', 'start', 'play it'}
        if len(options) == 1 and any(word in normalized for word in confirmation_words):
            return options[0]

        normalized_underscored = normalized.replace(' ', '_')
        for book_id in options:
            if normalized == book_id or normalized_underscored == book_id:
                return book_id

        suggestion_map = {s.book_id: s for s in self.book_converter.book_sources.suggestions()}

        for book_id in options:
            suggestion = suggestion_map.get(book_id)
            keywords = (suggestion.keywords if suggestion and suggestion.keywords else [])
            for keyword in keywords:
                if keyword and keyword.lower() in normalized:
                    return book_id

        for book_id in options:
            suggestion = suggestion_map.get(book_id)
            title = suggestion.title if suggestion else book_id.replace('_', ' ')
            title_lower = title.lower()
            if title_lower in normalized:
                return book_id
            significant_words = [
                word for word in title_lower.split() if len(word) >= 4 and word not in STOPWORDS
            ]
            if any(word in normalized for word in significant_words):
                return book_id

        return None

    async def _start_book(self, book_id, voice_id, message_id):
        await self._send_book_chunk(book_id, voice_id, message_id, chunk_index=0)
        if not self.book_access_granted:
            self.book_access_granted = True
            self.book_access_book_id = book_id

    async def _continue_book(self, voice_id, message_id):
        progress = self.book_progress.get(self.channel_name)
        if not progress:
            return False

        next_chunk_index = progress['current_chunk'] + 1
        if next_chunk_index >= progress['total_chunks']:
            await self._send_error_message(
                message_id,
                "You're already at the end of the book. Say another title to start over.",
            )
            return True

        book_id = progress['book_id']
        if self._has_reached_book_limit(book_id, allow_same=True):
            await self._send_access_denied("", message_id)
            return True
        voice_for_chunk = voice_id or progress.get('voice')
        await self._send_book_chunk(book_id, voice_for_chunk, message_id, chunk_index=next_chunk_index)
        return True

    async def _send_book_chunk(self, book_id, voice_id, message_id, chunk_index):
        try:
            audio_data, audio_format, used_voice, total_chunks, chunk_text = await self.book_converter.get_chunk_audio(
                book_id,
                chunk_index,
                voice_id=voice_id,
                preferred_format="mp3",
            )
        except (FileNotFoundError, ValueError, IndexError) as exc:
            await self._send_error_message(message_id, str(exc))
            return

        audio_b64 = base64.b64encode(audio_data).decode('utf-8')
        human_title = book_id.replace('_', ' ').title()
        chunk_number = chunk_index + 1

        voice_for_progress = voice_id or used_voice
        self.book_progress[self.channel_name] = {
            'book_id': book_id,
            'current_chunk': chunk_index,
            'total_chunks': total_chunks,
            'voice': voice_for_progress,
        }

        await self.send(text_data=json.dumps({
            'type': 'book_playback',
            'message_id': message_id,
            'request_text': f"{human_title} (chunk {chunk_number})",
            'audio': audio_b64,
            'text': chunk_text,
            'format': audio_format,
            'voice': used_voice,
            'book': {
                'id': book_id,
                'title': human_title,
                'chunk': chunk_number,
                'total_chunks': total_chunks,
            }
        }))

        next_index = chunk_index + 1
        if next_index < total_chunks:
            asyncio.create_task(
                self.book_converter.prefetch_chunk(
                    book_id,
                    next_index,
                    voice_id=voice_for_progress,
                    preferred_format=audio_format,
                )
            )

    async def _send_error_message(self, message_id, error):
        await self.send(text_data=json.dumps({
            'type': 'error',
            'message_id': message_id,
            'error': 'Processing error',
            'details': error
        }))

    async def _send_book_not_found_response(self, query, voice_id, message_id):
        query_text = (query or '').strip()
        message = (
            f"I couldn't find a public domain book matching '{query_text}'. "
            "Try naming a specific title from the suggestions or another well-known classic."
        )
        audio_data, error, used_voice, audio_format = await tts_service.text_to_speech(
            message,
            voice_id=voice_id,
        )

        if error:
            await self._send_error_message(message_id, error)
            return

        audio_b64 = base64.b64encode(audio_data).decode('utf-8')
        await self.send(text_data=json.dumps({
            'type': 'tts_result',
            'message_id': message_id,
            'request_text': query_text,
            'audio': audio_b64,
            'text': message,
            'format': audio_format,
            'voice': used_voice,
            'match': {
                'prompt': 'book_not_found',
                'confidence': 1.0,
            }
        }))

    def _has_reached_book_limit(self, requested_book_id, allow_same=False):
        if not self.book_access_granted:
            return False
        if allow_same and requested_book_id == self.book_access_book_id:
            return False
        return requested_book_id != self.book_access_book_id

    async def _send_access_denied(self, request_text, message_id):
        message = "Please sign in to read more. You've reached the preview limit."
        await self.send(text_data=json.dumps({
            'type': 'error',
            'message_id': message_id,
            'code': 111,
            'error': 'Access restricted',
            'details': message,
            'request_text': request_text,
        }))
