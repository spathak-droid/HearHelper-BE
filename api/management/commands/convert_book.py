import asyncio
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from api.book_service import BookConverter


class Command(BaseCommand):
    help = "Convert a public domain book from text to chunked audio files."

    def add_arguments(self, parser):
        parser.add_argument(
            "--book",
            required=True,
            help="Book identifier (filename without .txt) located in public_domain_books/",
        )
        parser.add_argument(
            "--voice",
            default=None,
            help="Optional Piper voice id to use.",
        )
        parser.add_argument(
            "--preferred-format",
            default="mp3",
            help="Desired output format (defaults to mp3).",
        )
        parser.add_argument(
            "--chunk-size",
            type=int,
            default=1500,
            help="Maximum characters per chunk.",
        )
        parser.add_argument(
            "--output-dir",
            default=None,
            help="Optional directory to store generated audio (defaults to generated_audio/books/<book>).",
        )

    def handle(self, *args, **options):
        book_id = options["book"]
        voice_id = options["voice"]
        preferred_format = options["preferred_format"]
        chunk_size = options["chunk_size"]

        output_dir_option = options["output_dir"]

        converter = BookConverter(
            chunk_chars=chunk_size,
            output_root=Path(output_dir_option) if output_dir_option else None,
        )

        if book_id not in converter.available_books():
            raise CommandError(
                f"Book '{book_id}' not found. Available books: {', '.join(converter.available_books()) or 'none'}"
            )

        result = asyncio.run(
            converter.convert_book(
                book_id=book_id,
                voice_id=voice_id,
                preferred_format=preferred_format,
            )
        )

        self.stdout.write(self.style.SUCCESS(f"Generated {len(result.audio_files)} files for '{book_id}'"))
        self.stdout.write(f"Manifest: {result.manifest_path}")
