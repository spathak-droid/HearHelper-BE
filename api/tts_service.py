# api/tts_service.py
import asyncio
import logging
import os
import wave
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from threading import Lock
from typing import Any, Dict, Optional, Tuple

from gtts import gTTS

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

        if self.piper_enabled:
            self._discover_models()
            if not self.models:
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
    ) -> Tuple[Optional[bytes], Optional[str], Optional[str], str]:
        """
        Convert text to speech asynchronously.

        Args:
            text: Text to convert.
            voice_id: Optional id of the Piper model to use.
            preferred_format: Target audio format (e.g., "mp3").

        Returns:
            Tuple of (audio_data, error_message, used_voice_id, audio_format)
        """
        preferred_format = (
            preferred_format.lower() if preferred_format else preferred_format
        )

        if not text or not isinstance(text, str):
            return None, "Invalid text input", None, self._determine_format(preferred_format)

        text = text.strip()
        if not text:
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
                    audio_data = await loop.run_in_executor(
                        None,
                        model.synthesize,
                        text,
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
            text,
            preferred_format=preferred_format,
        )

    def available_voices(self) -> Tuple[str, ...]:
        """Return the list of discovered voice ids."""
        if not self.piper_enabled:
            return tuple()
        return tuple(sorted(self.models.keys()))

    async def _synthesize_with_gtts_async(
        self,
        loop: asyncio.AbstractEventLoop,
        text: str,
        preferred_format: Optional[str],
    ) -> Tuple[Optional[bytes], Optional[str], Optional[str], str]:
        try:
            audio_data = await loop.run_in_executor(
                None,
                self._synthesize_with_gtts,
                text,
            )
            return await self._ensure_format(
                audio_data,
                source_format=self._gtts_format,
                voice_id=None,
                preferred_format=preferred_format,
            )
        except Exception as exc:
            logger.error("Error in gTTS fallback: %s", exc, exc_info=True)
            return None, str(exc), None, self._gtts_format

    def _synthesize_with_gtts(self, text: str) -> bytes:
        buffer = BytesIO()
        tts = gTTS(text=text, lang=self.gtts_lang)
        tts.write_to_fp(buffer)
        return buffer.getvalue()

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

        if not self.models:
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
        if not self.piper_enabled or not self.models:
            return None, None

        model = None
        target_id = requested_voice_id

        if target_id:
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


# Create a singleton instance
tts_service = TTSService()
