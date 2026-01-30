# api/book_service.py
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .book_sources import BookSourceManager
from .ssml import build_book_ssml
from .tts_service import tts_service
from storage import r2_client

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
BOOKS_DIR = BASE_DIR / "public_domain_books"
OUTPUT_DIR = BASE_DIR / "generated_audio" / "books"
DEFAULT_AUDIO_FORMAT = "mp3"


def _paragraphs_from_text(text: str) -> Iterable[str]:
    blocks = re.split(r"\n\s*\n", text)
    for block in blocks:
        clean = " ".join(line.strip() for line in block.strip().splitlines())
        if clean:
            yield clean


CHAPTER_PATTERN = re.compile(
    r"^\s*(chapter|book|part)\s+([0-9ivxlcdm]+|\b[a-z]+\b)",
    re.IGNORECASE,
)
CHAPTER_ONE_PATTERN = re.compile(
    r"^\s*(chapter|book|part)\s+(1|i|one)\b",
    re.IGNORECASE,
)
MIN_BODY_CHARS = 90
MIN_BODY_WORDS = 15


def _looks_like_body(paragraph: str) -> bool:
    stripped = paragraph.strip()
    if not stripped:
        return False
    if len(stripped) >= MIN_BODY_CHARS:
        return True
    word_count = len(stripped.split())
    if word_count >= MIN_BODY_WORDS and "." in stripped:
        return True
    return False


def _next_body_paragraph(start_idx: int, paragraphs: Sequence[str]) -> Optional[int]:
    for offset in range(start_idx + 1, len(paragraphs)):
        candidate = paragraphs[offset].strip()
        if not candidate:
            continue
        if _looks_like_body(candidate):
            return offset
        # if we encounter another chapter heading immediately, treat prior as table of contents
        if CHAPTER_PATTERN.match(candidate):
            return None
    return None


def _trim_front_matter(paragraphs: Sequence[str]) -> List[str]:
    """
    Remove prefaces/table of contents until a chapter heading that is followed by a body paragraph.
    Prefer Chapter One headings, but fall back to any chapter heading that passes the body heuristic.
    """
    valid_chapter_one_hits = []
    valid_chapter_hits = []

    for idx, paragraph in enumerate(paragraphs):
        normalized = paragraph.strip()
        if not normalized:
            continue

        if CHAPTER_ONE_PATTERN.match(normalized):
            if _next_body_paragraph(idx, paragraphs) is not None:
                valid_chapter_one_hits.append(idx)
                if len(valid_chapter_one_hits) >= 2:
                    return list(paragraphs[valid_chapter_one_hits[1]:])
            continue

        if CHAPTER_PATTERN.match(normalized):
            if _next_body_paragraph(idx, paragraphs) is not None:
                valid_chapter_hits.append(idx)
                if len(valid_chapter_hits) >= 2:
                    return list(paragraphs[valid_chapter_hits[1]:])

    if valid_chapter_one_hits:
        return list(paragraphs[valid_chapter_one_hits[-1]:])
    if valid_chapter_hits:
        return list(paragraphs[valid_chapter_hits[-1]:])
    return list(paragraphs)


def _chunk_paragraphs(
    paragraphs: Sequence[str],
    chunk_chars: int,
) -> List[str]:
    chunks: List[str] = []
    current: List[str] = []
    current_len = 0

    for paragraph in paragraphs:
        paragraph_len = len(paragraph)
        if current and (current_len + paragraph_len + 1) > chunk_chars:
            chunks.append("\n\n".join(current))
            current = []
            current_len = 0
        current.append(paragraph)
        current_len += paragraph_len + 1

    if current:
        chunks.append("\n\n".join(current))

    return chunks


@dataclass
class ConversionResult:
    book_id: str
    used_voice: Optional[str]
    audio_files: List[Path]
    manifest_path: Path


class BookConverter:
    """
    Lazily converts public domain text files into chunked audio files using the TTS service.
    A per-book manifest.json file stores chunk metadata, generation logs, and cached files.
    """

    def __init__(
        self,
        books_dir: Optional[Path] = None,
        output_root: Optional[Path] = None,
        chunk_chars: int = 1500,
    ) -> None:
        self.books_dir = Path(books_dir) if books_dir else BOOKS_DIR
        self.output_root = Path(output_root) if output_root else OUTPUT_DIR
        self.chunk_chars = chunk_chars
        self.book_sources = BookSourceManager(self.books_dir)
        self.remote_enabled = r2_client.is_enabled()

        self.books_dir.mkdir(parents=True, exist_ok=True)
        self.output_root.mkdir(parents=True, exist_ok=True)

    def available_books(self) -> Tuple[str, ...]:
        existing = {
            book_path.stem
            for book_path in self.books_dir.glob("*.txt")
            if book_path.is_file()
        }
        remote_books = set()
        if self.remote_enabled:
            for key in r2_client.list_objects("public_domain_books/"):
                if key.endswith(".txt"):
                    remote_books.add(Path(key).stem)
        suggested = {suggestion.book_id for suggestion in self.book_sources.suggestions()}
        combined = sorted(existing | suggested | remote_books)
        return tuple(combined)

    def load_book_text(self, book_id: str) -> str:
        remote_key = f"public_domain_books/{book_id}.txt"
        if self.remote_enabled:
            data = r2_client.download_bytes(remote_key)
            if data:
                return data.decode("utf-8")

        path = self.ensure_book_file(book_id)
        if not path:
            raise FileNotFoundError(
                f"Book '{book_id}' was not found locally and no download source is configured."
            )
        text = path.read_text(encoding="utf-8")

        if self.remote_enabled:
            r2_client.upload_bytes(text.encode("utf-8"), remote_key)
            path.unlink(missing_ok=True)

        return text

    def build_chunks(self, text: str) -> List[str]:
        paragraphs = list(_paragraphs_from_text(text))
        paragraphs = _trim_front_matter(paragraphs)
        return _chunk_paragraphs(paragraphs, self.chunk_chars)

    async def convert_book(
        self,
        book_id: str,
        voice_id: Optional[str] = None,
        preferred_format: str = DEFAULT_AUDIO_FORMAT,
    ) -> ConversionResult:
        manifest = self._ensure_manifest(book_id, preferred_format, voice_id=voice_id)
        chunk_count = manifest.get("chunk_count", len(manifest["chunks"]))
        for chunk_index in range(chunk_count):
            manifest = await self.ensure_chunk_generated(
                book_id,
                chunk_index,
                voice_id=voice_id,
                preferred_format=preferred_format,
                manifest=manifest,
            )

        audio_files = []
        if not self.remote_enabled:
            for chunk in manifest["chunks"]:
                file_name = chunk.get("file")
                if file_name:
                    audio_files.append(self._chunk_file_path(book_id, file_name, voice_id))

        return ConversionResult(
            book_id=book_id,
            used_voice=manifest.get("voice"),
            audio_files=audio_files,
            manifest_path=(
                self._manifest_path(book_id, voice_id)
                if not self.remote_enabled
                else Path(self._manifest_remote_key(book_id, voice_id))
            ),
        )

    async def get_chunk_audio(
        self,
        book_id: str,
        chunk_index: int,
        voice_id: Optional[str] = None,
        preferred_format: str = DEFAULT_AUDIO_FORMAT,
    ) -> Tuple[bytes, str, Optional[str], int, str]:
        """
        Returns audio bytes, audio format, used voice, total chunk count, and chunk text.
        Generates the requested chunk on demand and leaves future chunks pending.
        """
        manifest = await self.ensure_chunk_generated(
            book_id,
            chunk_index,
            voice_id=voice_id,
            preferred_format=preferred_format,
        )

        chunk_entry = manifest["chunks"][chunk_index]
        filename = chunk_entry["file"]
        if self.remote_enabled:
            audio_bytes = r2_client.download_bytes(self._chunk_remote_key(book_id, voice_id, filename))
            if not audio_bytes:
                raise FileNotFoundError(f"Chunk {filename} missing from remote storage")
            audio_format = manifest.get("format") or Path(filename).suffix.lstrip(".") or DEFAULT_AUDIO_FORMAT
        else:
            file_path = self._chunk_file_path(book_id, filename, voice_id)
            audio_bytes = file_path.read_bytes()
            audio_format = manifest.get("format") or file_path.suffix.lstrip(".") or DEFAULT_AUDIO_FORMAT
            file_path.unlink(missing_ok=True)
        chunk_text = chunk_entry.get("text", "")
        total_chunks = manifest.get("chunk_count") or len(manifest["chunks"])
        return audio_bytes, audio_format, manifest.get("voice"), total_chunks, chunk_text

    async def prefetch_chunk(
        self,
        book_id: str,
        chunk_index: int,
        voice_id: Optional[str] = None,
        preferred_format: str = DEFAULT_AUDIO_FORMAT,
    ) -> None:
        try:
            manifest = await self.ensure_chunk_generated(
                book_id,
                chunk_index,
                voice_id=voice_id,
                preferred_format=preferred_format,
            )
            if self.remote_enabled:
                chunk_entry = manifest["chunks"][chunk_index]
                if chunk_entry.get("file"):
                    path = self._chunk_file_path(book_id, chunk_entry["file"], voice_id)
                    path.unlink(missing_ok=True)
        except Exception as exc:  # pragma: no cover - best-effort cache warming
            logger.debug("Prefetch for %s chunk %s skipped: %s", book_id, chunk_index, exc)

    async def ensure_chunk_generated(
        self,
        book_id: str,
        chunk_index: int,
        voice_id: Optional[str],
        preferred_format: str,
        manifest: Optional[Dict[str, object]] = None,
    ) -> Dict[str, object]:
        manifest = self._ensure_manifest(book_id, preferred_format, voice_id=voice_id, manifest=manifest)

        chunks = manifest["chunks"]
        if chunk_index >= len(chunks):
            raise IndexError(f"Chunk {chunk_index} not available for book '{book_id}'")

        chunk_entry = chunks[chunk_index]
        file_name = chunk_entry.get("file")
        if file_name and not self.remote_enabled:
            file_path = self._chunk_file_path(book_id, file_name, voice_id)
            if file_path.exists():
                return manifest
        if file_name and self.remote_enabled:
            if r2_client.object_exists(self._chunk_remote_key(book_id, voice_id, file_name)):
                return manifest

        text = chunk_entry.get("text", "")
        ssml_text = chunk_entry.get("ssml")
        if not ssml_text and text:
            ssml_text = build_book_ssml(text)
            chunk_entry["ssml"] = ssml_text
        audio_data, error, used_voice, audio_format = await tts_service.text_to_speech(
            text,
            voice_id=voice_id,
            preferred_format=preferred_format or manifest.get("format") or DEFAULT_AUDIO_FORMAT,
            ssml_text=ssml_text,
        )

        if error:
            raise RuntimeError(f"TTS error on chunk {chunk_index + 1}: {error}")

        filename = chunk_entry.get("file") or f"{book_id}_part_{chunk_index + 1:03d}.{audio_format}"
        if self.remote_enabled:
            r2_client.upload_bytes(audio_data, self._chunk_remote_key(book_id, voice_id, filename))
        else:
            file_path = self._chunk_file_path(book_id, filename, voice_id)
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_bytes(audio_data)

        chunk_entry["file"] = filename
        chunk_entry["generated"] = True
        chunk_entry["generated_at"] = datetime.utcnow().isoformat() + "Z"

        manifest["voice"] = used_voice or voice_id
        manifest["format"] = audio_format
        self._append_log(
            manifest,
            f"Generated chunk {chunk_index + 1} ({filename}).",
        )
        self._save_manifest(manifest)
        return manifest

    def _ensure_manifest(
        self,
        book_id: str,
        preferred_format: str,
        voice_id: Optional[str] = None,
        manifest: Optional[Dict[str, object]] = None,
    ) -> Dict[str, object]:
        if manifest is not None:
            return manifest

        manifest = self._load_manifest(book_id, voice_id)
        if manifest is not None:
            if preferred_format and not manifest.get("format"):
                manifest["format"] = preferred_format
                self._save_manifest(manifest)
            if not manifest.get("voice_key"):
                manifest["voice_key"] = self._voice_namespace(voice_id)
                self._save_manifest(manifest)
            return manifest

        text = self.load_book_text(book_id)
        chunk_texts = self.build_chunks(text)

        if not chunk_texts:
            raise ValueError(f"Book '{book_id}' is empty or could not be chunked.")

        chunk_entries = [
            {
                "index": idx,
                "chunk_number": idx + 1,
                "text": chunk_text,
                "ssml": None,
                "file": None,
                "generated": False,
                "generated_at": None,
            }
            for idx, chunk_text in enumerate(chunk_texts)
        ]

        manifest = {
            "book_id": book_id,
            "voice_key": self._voice_namespace(voice_id),
            "voice": None,
            "format": preferred_format or DEFAULT_AUDIO_FORMAT,
            "chunk_count": len(chunk_entries),
            "created_at": datetime.utcnow().isoformat() + "Z",
            "chunks": chunk_entries,
            "logs": [],
        }
        self._append_log(
            manifest,
            f"Created manifest with {len(chunk_entries)} chunks.",
        )
        self._save_manifest(manifest)
        return manifest

    def _load_manifest(self, book_id: str, voice_id: Optional[str]) -> Optional[Dict[str, object]]:
        if self.remote_enabled:
            remote_key = self._manifest_remote_key(book_id, voice_id)
            data = r2_client.download_bytes(remote_key)
            if data:
                try:
                    manifest = json.loads(data.decode("utf-8"))
                    _ensure_chunk_schema(manifest)
                    return manifest
                except json.JSONDecodeError:
                    pass
            legacy_path = self._legacy_manifest_path(book_id)
            if legacy_path.exists():
                try:
                    manifest = json.loads(legacy_path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    legacy_path.unlink(missing_ok=True)
                    return None
                self._migrate_legacy_manifest(book_id, manifest, voice_id)
                _ensure_chunk_schema(manifest)
                return manifest
            return None

        manifest_path = self._manifest_path(book_id, voice_id)
        if not manifest_path.exists():
            legacy_path = self._legacy_manifest_path(book_id)
            if voice_id in (None, "", "default") and legacy_path.exists():
                try:
                    manifest = json.loads(legacy_path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    logger.warning("Legacy manifest for %s is corrupted; deleting.", book_id)
                    legacy_path.unlink(missing_ok=True)
                    return None
                self._migrate_legacy_manifest(book_id, manifest, voice_id)
                return manifest
            return None
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.warning("Manifest for book %s is corrupted; starting over.", book_id)
            manifest_path.unlink(missing_ok=True)
            return None
        _ensure_chunk_schema(manifest)
        return manifest

    def _save_manifest(self, manifest: Dict[str, object]) -> None:
        voice_ns = manifest.get("voice_key") or self._voice_namespace(manifest.get("voice"))
        manifest["voice_key"] = voice_ns
        serialized = json.dumps(manifest, indent=2)
        if self.remote_enabled:
            r2_client.upload_bytes(serialized.encode("utf-8"), self._manifest_remote_key(manifest["book_id"], voice_ns))
        else:
            manifest_path = self._manifest_path(manifest["book_id"], voice_ns)
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(serialized, encoding="utf-8")

    def _append_log(self, manifest: Dict[str, object], message: str) -> None:
        manifest.setdefault("logs", []).append(
            {
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "message": message,
            }
        )

    def _manifest_path(self, book_id: str, voice_id: Optional[str]) -> Path:
        return self._book_output_dir(book_id, voice_id).joinpath("manifest.json")

    def _chunk_file_path(self, book_id: str, filename: str, voice_id: Optional[str]) -> Path:
        return self._book_output_dir(book_id, voice_id) / filename

    def _book_output_dir(self, book_id: str, voice_id: Optional[str]) -> Path:
        voice_ns = self._voice_namespace(voice_id)
        return self.output_root / book_id / voice_ns

    def _legacy_manifest_path(self, book_id: str) -> Path:
        return (self.output_root / book_id).joinpath("manifest.json")

    def _manifest_remote_key(self, book_id: str, voice_id: Optional[str]) -> str:
        return f"generated_audio/{book_id}/{self._voice_namespace(voice_id)}/manifest.json"

    def _chunk_remote_key(self, book_id: str, voice_id: Optional[str], filename: str) -> str:
        return f"generated_audio/{book_id}/{self._voice_namespace(voice_id)}/{filename}"

    def _migrate_legacy_manifest(self, book_id: str, manifest: Dict[str, object], voice_id: Optional[str]) -> None:
        voice_ns = self._voice_namespace(voice_id)

        if self.remote_enabled:
            for chunk in manifest.get("chunks", []):
                file_name = chunk.get("file")
                if not file_name:
                    continue
                legacy_file = (self.output_root / book_id) / file_name
                if legacy_file.exists():
                    r2_client.upload_file(legacy_file, self._chunk_remote_key(book_id, voice_id, file_name))
                    legacy_file.unlink(missing_ok=True)
            manifest["voice_key"] = voice_ns
            self._save_manifest(manifest)
            legacy_manifest = self._legacy_manifest_path(book_id)
            if legacy_manifest.exists():
                legacy_manifest.unlink(missing_ok=True)
            return

        legacy_dir = self.output_root / book_id
        new_dir = self._book_output_dir(book_id, voice_id)
        new_dir.mkdir(parents=True, exist_ok=True)

        for chunk in manifest.get("chunks", []):
            file_name = chunk.get("file")
            if not file_name:
                continue
            legacy_file = legacy_dir / file_name
            new_file = new_dir / file_name
            if legacy_file.exists():
                legacy_file.replace(new_file)

        manifest["voice_key"] = voice_ns
        self._save_manifest(manifest)
        legacy_manifest = self._legacy_manifest_path(book_id)
        legacy_manifest.unlink(missing_ok=True)

    @staticmethod
    def _voice_namespace(voice_id: Optional[str]) -> str:
        return voice_id or "default"

    def ensure_book_file(self, book_id: str) -> Optional[Path]:
        path = self.books_dir / f"{book_id}.txt"
        if path.exists():
            return path
        return self.book_sources.ensure_downloaded(book_id)


def _ensure_chunk_schema(manifest: Dict[str, object]) -> None:
    chunks = manifest.get("chunks")
    if not isinstance(chunks, list):
        return
    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue
        chunk.setdefault("ssml", None)
