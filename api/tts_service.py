# api/tts_service.py
import asyncio
import logging
import os
import re
import wave
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from threading import Lock
from typing import Any, Dict, Optional, Tuple

from gtts import gTTS

from storage import r2_client
from .ssml import ssml_to_plain_text
from .text_enricher import PAUSE_MARKER, enrich_text_for_tts

try:
    from pydub import AudioSegment
except ImportError:
    AudioSegment = None

try:
    from piper import PiperVoice, SynthesisConfig

    PIPER_AVAILABLE = True
except ImportError:
    PiperVoice = Any  # type: ignore
    SynthesisConfig = Any  # type: ignore
    PIPER_AVAILABLE = False

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent


@dataclass
class PiperVoiceModel:
    """Holds metadata and lazy-loaded Piper voice objects."""

    model_path: Path
    config_path: Path
    speaker_id: Optional[int] = None
    _voice: Optional[PiperVoice] = field(default=None, init=False, repr=False)
    _load_lock: Lock = field(default_factory=Lock, init=False, repr=False)

    def synthesize(self, text: str) -> bytes:
        """
        Perform blocking synthesis with the configured Piper voice.

        Args:
            text: Text to convert.

        Returns:
            WAV bytes.
        """
        voice = self._get_or_load_voice()
        buffer = BytesIO()
        synthesis_config = (
            SynthesisConfig(speaker_id=self.speaker_id)
            if self.speaker_id is not None
            else None
        )

        with wave.open(buffer, "wb") as wav_file:
            voice.synthesize_wav(text, wav_file, syn_config=synthesis_config)

        return buffer.getvalue()

    def _get_or_load_voice(self) -> PiperVoice:
        if self._voice is None:
            with self._load_lock:
                if self._voice is None:
                    logger.info("Loading Piper voice from %s", self.model_path)
                    self._voice = PiperVoice.load(
                        str(self.model_path),
                        str(self.config_path),
                    )
        return self._voice


class TTSService:
    """
    Text-to-speech service backed by Piper voice models, with automatic
    fallback to gTTS when Piper or its models are unavailable.

    Voice models are discovered from a configurable directory (defaults to
    BASE_DIR / "piper_models"). Individual voices can be selected by id,
    which is inferred from the filename (e.g. "en_US-amy-medium.onnx" -> "en_US-amy-medium").
    """

    def __init__(
        self,
        models_dir: Optional[Path] = None,
        default_voice: Optional[str] = None,
    ):
        if models_dir is not None:
            self.models_dir = Path(models_dir)
        else:
            env_dir = os.getenv("PIPER_MODELS_DIR")
            self.models_dir = Path(env_dir) if env_dir else BASE_DIR / "piper_models"

        self.models_dir.mkdir(parents=True, exist_ok=True)

        env_default_voice = os.getenv("PIPER_DEFAULT_VOICE")
        self.default_voice_id = default_voice or env_default_voice
        self.models: Dict[str, PiperVoiceModel] = {}

        self.piper_enabled = PIPER_AVAILABLE
        self.gtts_lang = os.getenv("GTTS_LANG", "en")
        self._gtts_format = "mp3"
        self.remote_models: set[str] = set()
        self.allow_ssml_passthrough = os.getenv("TTS_ALLOW_SSML", "").lower() in {"1", "true", "yes", "on"}
        self.sentence_pause_markers = max(1, int(os.getenv("TTS_SENTENCE_PAUSE_MARKERS", "3")))
        self.sentence_pause_ms = max(100, int(os.getenv("TTS_SENTENCE_PAUSE_MS", "500")))
        self.comma_pause_ms = max(50, int(os.getenv("TTS_COMMA_PAUSE_MS", "250")))
        self.semicolon_pause_ms = max(50, int(os.getenv("TTS_SEMICOLON_PAUSE_MS", "250")))
        self.punctuation_pauses_enabled = os.getenv("TTS_PUNCTUATION_PAUSES", "true").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        self._inline_break_pattern = re.compile(r"\[(?:break|pause|silence)\]", re.IGNORECASE)
        self._abbrev_tokens = {
            "mr",
            "mrs",
            "ms",
            "dr",
            "prof",
            "sr",
            "jr",
            "st",
            "mt",
            "vs",
            "etc",
            "e.g",
            "i.e",
        }

        if self.piper_enabled:
            self._discover_models()
            if not self.models and not self.remote_models:
                logger.warning(
                    "Piper is installed but no models were found in %s. Falling back to gTTS.",
                    self.models_dir,
                )
                self.piper_enabled = False
        else:
            logger.warning(
                "piper-tts is not installed in the current environment. Falling back to gTTS."
            )

    async def text_to_speech(
        self,
        text: str,
        voice_id: Optional[str] = None,
        preferred_format: Optional[str] = None,
        ssml_text: Optional[str] = None,
    ) -> Tuple[Optional[bytes], Optional[str], Optional[str], str]:
        """
        Convert text to speech asynchronously.

        Args:
            text: Text to convert.
            voice_id: Optional id of the Piper model to use.
            preferred_format: Target audio format (e.g., "mp3").
            ssml_text: Optional SSML markup to drive pauses/prosody.

        Returns:
            Tuple of (audio_data, error_message, used_voice_id, audio_format)
        """
        preferred_format = (
            preferred_format.lower() if preferred_format else preferred_format
        )

        prepared_text = self._prepare_text_payload(text, ssml_text)
        if not prepared_text:
            return None, "Invalid text input", None, self._determine_format(preferred_format)

        loop = asyncio.get_running_loop()

        if self.piper_enabled:
            selected_voice_id, model = self._select_model(voice_id)

            if model is None:
                logger.warning(
                    "Piper TTS requested but no model found. Falling back to gTTS."
                )
            else:
                try:
                    if self.punctuation_pauses_enabled and isinstance(text, str):
                        audio_data = await loop.run_in_executor(
                            None,
                            self._synthesize_with_piper_pauses,
                            model,
                            text,
                        )
                    else:
                        audio_data = await loop.run_in_executor(
                            None,
                            model.synthesize,
                            prepared_text,
                        )
                    return await self._ensure_format(
                        audio_data,
                        source_format="wav",
                        voice_id=selected_voice_id,
                        preferred_format=preferred_format,
                    )
                except Exception as exc:
                    logger.error(
                        "Error in text-to-speech conversion with Piper voice %s: %s",
                        selected_voice_id,
                        exc,
                        exc_info=True,
                    )
                    return None, str(exc), selected_voice_id, "wav"

        if voice_id:
            logger.warning(
                "Voice '%s' requested but Piper is not available. Using gTTS fallback.",
                voice_id,
            )

        return await self._synthesize_with_gtts_async(
            loop,
            prepared_text,
            preferred_format=preferred_format,
            metadata_voice=voice_id or self.default_voice_id,
        )

    def _synthesize_with_piper_pauses(self, model: PiperVoiceModel, text: str) -> bytes:
        segments = self._split_text_with_pauses(text)
        if not segments:
            return model.synthesize(text)

        audio_chunks: list[bytes] = []
        pauses_ms: list[int] = []
        for segment_text, pause_ms in segments:
            prepared = self._prepare_text_payload(segment_text, None, apply_sentence_pauses=False)
            if not prepared:
                continue
            audio_chunks.append(model.synthesize(prepared))
            pauses_ms.append(pause_ms)

        if not audio_chunks:
            return model.synthesize(text)

        if AudioSegment is not None:
            combined = AudioSegment.empty()
            for audio_data, pause_ms in zip(audio_chunks, pauses_ms, strict=False):
                chunk = AudioSegment.from_file(BytesIO(audio_data), format="wav")
                combined += chunk
                if pause_ms:
                    combined += AudioSegment.silent(duration=pause_ms)
            output = BytesIO()
            combined.export(output, format="wav")
            return output.getvalue()

        return self._concat_wav_with_silence(audio_chunks, pauses_ms)

    def available_voices(self) -> Tuple[str, ...]:
        """Return the list of discovered voice ids."""
        if not self.piper_enabled and not self.remote_models:
            return tuple()
        voice_ids = set(self.remote_models)
        voice_ids.update(self.models.keys())
        return tuple(sorted(voice_ids))

    async def _synthesize_with_gtts_async(
        self,
        loop: asyncio.AbstractEventLoop,
        text: str,
        preferred_format: Optional[str],
        metadata_voice: Optional[str],
    ) -> Tuple[Optional[bytes], Optional[str], Optional[str], str]:
        try:
            if self.punctuation_pauses_enabled:
                audio_data = await loop.run_in_executor(
                    None,
                    self._synthesize_with_gtts_pauses,
                    text,
                    preferred_format or self._gtts_format,
                )
                return audio_data, None, metadata_voice, self._determine_format(preferred_format)

            audio_data = await loop.run_in_executor(
                None,
                self._synthesize_with_gtts,
                text,
            )
            return await self._ensure_format(
                audio_data,
                source_format=self._gtts_format,
                voice_id=metadata_voice,
                preferred_format=preferred_format,
            )
        except Exception as exc:
            logger.error("Error in gTTS fallback: %s", exc, exc_info=True)
            return None, str(exc), metadata_voice, self._gtts_format

    def _synthesize_with_gtts(self, text: str) -> bytes:
        buffer = BytesIO()
        tts = gTTS(text=text, lang=self.gtts_lang)
        tts.write_to_fp(buffer)
        return buffer.getvalue()

    def _synthesize_with_gtts_pauses(self, text: str, target_format: str) -> bytes:
        if AudioSegment is None:
            logger.warning("Programmatic pauses require pydub/ffmpeg for gTTS. Falling back.")
            return self._synthesize_with_gtts(text)

        segments = self._split_text_with_pauses(text)
        if not segments:
            return self._synthesize_with_gtts(text)

        combined = AudioSegment.empty()
        for segment_text, pause_ms in segments:
            prepared = self._prepare_text_payload(segment_text, None, apply_sentence_pauses=False)
            if not prepared:
                continue
            chunk_bytes = self._synthesize_with_gtts(prepared)
            chunk = AudioSegment.from_file(BytesIO(chunk_bytes), format=self._gtts_format)
            combined += chunk
            if pause_ms:
                combined += AudioSegment.silent(duration=pause_ms)

        output = BytesIO()
        combined.export(output, format=target_format)
        return output.getvalue()

    async def _ensure_format(
        self,
        audio_data: bytes,
        source_format: str,
        voice_id: Optional[str],
        preferred_format: Optional[str],
    ) -> Tuple[bytes, Optional[str], Optional[str], str]:
        target_format = self._determine_format(preferred_format, source_format)
        if target_format == source_format:
            return audio_data, None, voice_id, source_format

        try:
            converted_data = await self._convert_audio_format_async(
                audio_data,
                source_format=source_format,
                target_format=target_format,
            )
            return converted_data, None, voice_id, target_format
        except Exception as exc:
            logger.error(
                "Failed to convert audio from %s to %s: %s",
                source_format,
                target_format,
                exc,
            )
            raise

    async def _convert_audio_format_async(
        self,
        audio_data: bytes,
        source_format: str,
        target_format: str,
    ) -> bytes:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            self._convert_audio_format,
            audio_data,
            source_format,
            target_format,
        )

    def _convert_audio_format(
        self,
        audio_data: bytes,
        source_format: str,
        target_format: str,
    ) -> bytes:
        if AudioSegment is None:
            raise RuntimeError(
                "Audio conversion requires the 'pydub' package and ffmpeg installation."
            )

        buffer = BytesIO(audio_data)
        segment = AudioSegment.from_file(buffer, format=source_format)
        output_buffer = BytesIO()
        segment.export(output_buffer, format=target_format)
        return output_buffer.getvalue()

    def _determine_format(
        self,
        preferred_format: Optional[str],
        default_format: Optional[str] = None,
    ) -> str:
        if preferred_format:
            return preferred_format
        if default_format:
            return default_format
        return self._gtts_format

    def _prepare_text_payload(
        self,
        raw_text: object,
        ssml_text: Optional[str],
        apply_sentence_pauses: bool = True,
    ) -> Optional[str]:
        """
        Decide which text should be fed into the synthesis engine.

        When SSML is provided we either pass it through (if explicitly allowed)
        or downgrade to plain text with pause markers so Piper/gTTS can work.
        """
        base_text = raw_text.strip() if isinstance(raw_text, str) else ""
        ssml_candidate = ssml_text.strip() if isinstance(ssml_text, str) else None

        if not ssml_candidate and base_text:
            ssml_candidate = self._maybe_wrap_inline_ssml(base_text) if apply_sentence_pauses else None

        if ssml_candidate:
            if self.allow_ssml_passthrough:
                return ssml_candidate
            downgrade = ssml_to_plain_text(ssml_candidate)
            if downgrade:
                return self._normalize_text_breaks(downgrade)
            # Fall back to whatever plain text we have

        if not base_text:
            return None

        normalized = self._normalize_text_breaks(base_text, apply_sentence_pauses=apply_sentence_pauses)
        enriched = enrich_text_for_tts(normalized)
        return enriched or normalized

    def _maybe_wrap_inline_ssml(self, text: str) -> Optional[str]:
        if "<speak" in text or "<break" in text or "<p>" in text or "<s>" in text:
            if "<speak" in text:
                return text
            return f"<speak>{text}</speak>"
        return None

    def _normalize_text_breaks(self, text: str, apply_sentence_pauses: bool = True) -> str:
        cleaned = text.replace("\r\n", "\n").replace("\r", "\n")
        cleaned = self._inline_break_pattern.sub(PAUSE_MARKER, cleaned)
        cleaned = re.sub(r"\n{2,}", f"{PAUSE_MARKER}", cleaned)
        if apply_sentence_pauses:
            cleaned = cleaned
        cleaned = cleaned.replace("\n", " ")
        collapsed = " ".join(cleaned.split())
        return collapsed

    def _split_text_with_pauses(self, text: str) -> list[tuple[str, int]]:
        if not text:
            return []
        matches = list(re.finditer(r"[.!?,;]", text))
        if not matches:
            return [(text.strip(), 0)] if text.strip() else []

        segments: list[tuple[str, int]] = []
        buffer: list[str] = []
        last_idx = 0

        for match in matches:
            end = match.end()
            buffer.append(text[last_idx:end])
            pause_ms = self._pause_for_punctuation(text, match)
            if pause_ms:
                segment = "".join(buffer).strip()
                if segment:
                    segments.append((segment, pause_ms))
                buffer = []
            last_idx = end

        buffer.append(text[last_idx:])
        tail = "".join(buffer).strip()
        if tail:
            segments.append((tail, 0))
        return segments

    def _pause_for_punctuation(self, text: str, match: re.Match) -> int:
        punct = match.group(0)
        if punct == ",":
            return self.comma_pause_ms
        if punct == ";":
            return self.semicolon_pause_ms
        if punct in {"!", "?"}:
            return self.sentence_pause_ms
        if punct == ".":
            if self._is_abbreviation_period(text, match):
                return 0
            return self.sentence_pause_ms
        return 0

    def _is_abbreviation_period(self, text: str, match: re.Match) -> bool:
        start = match.start()
        if start > 0 and text[start - 1].isdigit():
            if match.end() < len(text) and text[match.end()].isdigit():
                return True
        prefix = text[:start]
        token_match = re.search(r"([A-Za-z]{1,5})$", prefix)
        if token_match:
            token = token_match.group(1).lower()
            if token in self._abbrev_tokens or len(token) == 1:
                return True
        return False

    def _concat_wav_with_silence(self, chunks: list[bytes], pauses_ms: list[int]) -> bytes:
        if not chunks:
            return b""

        params = None
        wave_chunks: list[tuple[bytes, int, int, int]] = []
        for chunk in chunks:
            with wave.open(BytesIO(chunk), "rb") as reader:
                chunk_params = reader.getparams()
                if params is None:
                    params = chunk_params
                elif (
                    params.nchannels != chunk_params.nchannels
                    or params.sampwidth != chunk_params.sampwidth
                    or params.framerate != chunk_params.framerate
                ):
                    raise RuntimeError("WAV chunks use different audio params; cannot concat without pydub.")
                wave_chunks.append(
                    (reader.readframes(reader.getnframes()), params.nchannels, params.sampwidth, params.framerate)
                )

        if params is None:
            return b""

        output = BytesIO()
        with wave.open(output, "wb") as writer:
            writer.setnchannels(params.nchannels)
            writer.setsampwidth(params.sampwidth)
            writer.setframerate(params.framerate)
            for (frames, channels, sampwidth, framerate), pause_ms in zip(wave_chunks, pauses_ms, strict=False):
                writer.writeframes(frames)
                if pause_ms:
                    silence_frames = int(framerate * (pause_ms / 1000))
                    writer.writeframes(b"\x00" * silence_frames * channels * sampwidth)

        return output.getvalue()

    def _inject_period_pause(self, text: str, match: re.Match, pause: str) -> str:
        prefix = text[: match.start()]
        token_match = re.search(r"([A-Za-z]{1,5})$", prefix)
        if token_match:
            token = token_match.group(1).lower()
            if token in self._abbrev_tokens or len(token) == 1:
                return match.group(0)
        spacer = " " if pause and not pause.endswith(" ") else ""
        return f".{spacer}{pause}{match.group(1)}"

    def _discover_models(self) -> None:
        discovered: Dict[str, PiperVoiceModel] = {}

        for onnx_file in sorted(self.models_dir.rglob("*.onnx")):
            model_id = onnx_file.stem
            config_path = onnx_file.with_suffix(onnx_file.suffix + ".json")

            if not config_path.exists():
                logger.warning(
                    "Skipping Piper model %s because config file %s was not found",
                    model_id,
                    config_path,
                )
                continue

            speaker_id = self._speaker_id_for_model(model_id)
            discovered[model_id] = PiperVoiceModel(
                model_path=onnx_file,
                config_path=config_path,
                speaker_id=speaker_id,
            )

        self.models = discovered
        self._refresh_remote_models()

        if not self.models:
            if self.remote_models:
                logger.info(
                    "No local Piper models found, but %d remote voices detected in R2.",
                    len(self.remote_models),
                )
            else:
                logger.warning(
                    "No Piper models discovered in %s. Add .onnx/.onnx.json pairs to enable speech.",
                    self.models_dir,
                )
            self.default_voice_id = None
            return

        if self.default_voice_id not in self.models:
            if self.default_voice_id:
                logger.warning(
                    "Configured default Piper voice '%s' not found. Falling back to first available voice.",
                    self.default_voice_id,
                )
            self.default_voice_id = next(iter(self.models))

        logger.info(
            "Piper voices ready (%s). Default voice: %s",
            ", ".join(sorted(self.models.keys())),
            self.default_voice_id,
        )

    def _select_model(
        self,
        requested_voice_id: Optional[str],
    ) -> Tuple[Optional[str], Optional[PiperVoiceModel]]:
        if not self.piper_enabled:
            return None, None

        # Lazy-fetch remote assets if we have voices listed remotely but none on disk yet.
        if not self.models and self.remote_models:
            candidate = requested_voice_id or self.default_voice_id or next(iter(self.remote_models))
            if self._download_voice_assets(candidate):
                self._discover_models()

        if not self.models:
            return None, None

        model = None
        target_id = requested_voice_id

        if target_id:
            model = self.models.get(target_id)
            if model is None and self._download_voice_assets(target_id):
                self._discover_models()
                model = self.models.get(target_id)
            if model is None:
                logger.warning(
                    "Requested Piper voice '%s' not found. Using default voice.",
                    target_id,
                )

        if model is None and self.default_voice_id:
            target_id = self.default_voice_id
            model = self.models.get(target_id)

        if model is None:
            target_id, model = next(iter(self.models.items()))

        return target_id, model

    def _speaker_id_for_model(self, model_id: str) -> Optional[int]:
        """
        Retrieve an optional speaker id for a multi-speaker model.

        Checks PIPER_SPEAKER_<MODEL_ID> (hyphens replaced with underscores) and
        falls back to PIPER_DEFAULT_SPEAKER_ID if present.
        """
        env_key = f"PIPER_SPEAKER_{model_id.upper().replace('-', '_')}"
        value = os.getenv(env_key)

        if value is None:
            value = os.getenv("PIPER_DEFAULT_SPEAKER_ID")

        if value is None:
            return None

        try:
            return int(value)
        except ValueError:
            logger.warning(
                "Invalid speaker id '%s' for Piper model %s. Ignoring speaker override.",
                value,
                model_id,
            )
            return None

    def _refresh_remote_models(self) -> None:
        if not r2_client.is_enabled():
            return
        keys = r2_client.list_objects("piper_models/")
        remote_ids = set()
        for key in keys:
            if key.endswith(".onnx"):
                remote_ids.add(Path(key).stem)
        self.remote_models = remote_ids

    def _download_voice_assets(self, voice_id: str) -> bool:
        if not r2_client.is_enabled():
            return False
        model_path = self.models_dir / f"{voice_id}.onnx"
        config_path = Path(f"{model_path}.json")
        downloaded = False
        if not model_path.exists():
            downloaded |= r2_client.download_file(f"piper_models/{model_path.name}", model_path)
        if not config_path.exists():
            downloaded |= r2_client.download_file(f"piper_models/{config_path.name}", config_path)
        return model_path.exists() and config_path.exists()


# Create a singleton instance
tts_service = TTSService()
