from django.core.management.base import BaseCommand, CommandError

from api.book_service import BookConverter


class Command(BaseCommand):
    help = "Download suggested public domain books into the local storage folder."

    def add_arguments(self, parser):
        parser.add_argument(
            "--book",
            action="append",
            dest="book_ids",
            help="Specific book id(s) to download (may be provided multiple times).",
        )
        parser.add_argument(
            "--all",
            action="store_true",
            help="Download every suggested book (default if no --book given).",
        )

    def handle(self, *args, **options):
        converter = BookConverter()
        suggestions = {suggestion.book_id: suggestion for suggestion in converter.book_sources.suggestions()}

        book_ids = options.get("book_ids")
        download_all = options.get("all")

        if not book_ids and not download_all:
            download_all = True

        targets = []
        if download_all:
            targets = list(suggestions.keys())
        else:
            for book_id in book_ids or []:
                if book_id not in suggestions:
                    raise CommandError(
                        f"Book '{book_id}' is not in the suggestions list. "
                        f"Available book ids: {', '.join(sorted(suggestions.keys()))}"
                    )
                targets.append(book_id)

        if not targets:
            self.stdout.write("No books selected for download.")
            return

        for book_id in targets:
            suggestion = suggestions[book_id]
            path = converter.book_sources.ensure_downloaded(book_id)
            if path:
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Downloaded '{suggestion.title}' to {path}"
                    )
                )
            else:
                self.stdout.write(
                    self.style.WARNING(
                        f"Could not download '{suggestion.title}'. "
                        "It may already exist locally or lacks a download URL."
                    )
                )
