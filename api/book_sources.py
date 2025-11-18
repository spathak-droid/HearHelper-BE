# api/book_sources.py
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

try:
    import requests
except ImportError as exc:  # pragma: no cover
    raise ImportError("The 'requests' package is required for remote book downloads. Install it with `pip install requests`.") from exc

logger = logging.getLogger(__name__)


@dataclass
class BookSuggestion:
    book_id: str
    title: str
    source: str  # "local" or a URL
    description: str = ""
    download_url: Optional[str] = None
    keywords: Optional[List[str]] = None


class BookSourceManager:
    """
    Handles suggested public domain books and fetching them from remote sources when necessary.
    """

    def __init__(self, storage_dir: Path) -> None:
        self.storage_dir = storage_dir
        self._suggestions: List[BookSuggestion] = [
            BookSuggestion(
                book_id="sherlock_holmes_a_scandal_in_bohemia",
                title="Sherlock Holmes: A Scandal in Bohemia",
                source="local",
                description="Short story by Arthur Conan Doyle (1891).",
                keywords=["detective", "mystery", "holmes", "victorian"],
            ),
            BookSuggestion(
                book_id="pride_and_prejudice",
                title="Pride and Prejudice",
                source="https://www.gutenberg.org/",
                description="Classic novel by Jane Austen (1813).",
                download_url="https://www.gutenberg.org/cache/epub/1342/pg1342.txt",
                keywords=["romance", "austen", "love", "regency"],
            ),
            BookSuggestion(
                book_id="frankenstein_or_the_modern_prometheus",
                title="Frankenstein; or, The Modern Prometheus",
                source="https://www.gutenberg.org/",
                description="Mary Shelley’s 1818 gothic novel.",
                download_url="https://www.gutenberg.org/cache/epub/84/pg84.txt",
                keywords=["horror", "science fiction", "monster", "gothic"],
            ),
            BookSuggestion(
                book_id="dracula",
                title="Dracula",
                source="https://www.gutenberg.org/",
                description="Bram Stoker's classic vampire novel (1897).",
                download_url="https://www.gutenberg.org/cache/epub/345/pg345.txt",
                keywords=["horror", "vampire", "gothic"],
            ),
            BookSuggestion(
                book_id="little_women",
                title="Little Women",
                source="https://www.gutenberg.org/",
                description="Louisa May Alcott's coming-of-age novel (1868).",
                download_url="https://www.gutenberg.org/cache/epub/514/pg514.txt",
                keywords=["family", "coming of age", "classic"],
            ),
            BookSuggestion(
                book_id="moby_dick",
                title="Moby-Dick; or, The Whale",
                source="https://www.gutenberg.org/",
                description="Herman Melville's epic seafaring tale (1851).",
                download_url="https://www.gutenberg.org/cache/epub/2701/pg2701.txt",
                keywords=["adventure", "sea", "classic"],
            ),
            BookSuggestion(
                book_id="alice_in_wonderland",
                title="Alice's Adventures in Wonderland",
                source="https://www.gutenberg.org/",
                description="Lewis Carroll's whimsical fantasy (1865).",
                download_url="https://www.gutenberg.org/cache/epub/11/pg11.txt",
                keywords=["fantasy", "children", "wonderland"],
            ),
            BookSuggestion(
                book_id="the_picture_of_dorian_gray",
                title="The Picture of Dorian Gray",
                source="https://www.gutenberg.org/",
                description="Oscar Wilde's tale of vanity and morality (1890).",
                download_url="https://www.gutenberg.org/cache/epub/174/pg174.txt",
                keywords=["gothic", "philosophical", "classic"],
            ),
            BookSuggestion(
                book_id="the_time_machine",
                title="The Time Machine",
                source="https://www.gutenberg.org/",
                description="H. G. Wells' foundational science fiction novella (1895).",
                download_url="https://www.gutenberg.org/cache/epub/35/pg35.txt",
                keywords=["science fiction", "time travel", "classic"],
            ),
            BookSuggestion(
                book_id="a_tale_of_two_cities",
                title="A Tale of Two Cities",
                source="https://www.gutenberg.org/",
                description="Charles Dickens' historical drama set in London and Paris (1859).",
                download_url="https://www.gutenberg.org/cache/epub/98/pg98.txt",
                keywords=["historical", "drama", "classic"],
            ),
            BookSuggestion(
                book_id="the_adventures_of_sherlock_holmes",
                title="The Adventures of Sherlock Holmes",
                source="https://www.gutenberg.org/",
                description="Twelve detective stories by Arthur Conan Doyle (1892).",
                download_url="https://www.gutenberg.org/cache/epub/1661/pg1661.txt",
                keywords=["detective", "mystery", "holmes"],
            ),
        ]

    def suggestions(self) -> List[BookSuggestion]:
        return list(self._suggestions)

    def find_suggestion(self, book_id: str) -> Optional[BookSuggestion]:
        book_id = book_id.strip().lower()
        for suggestion in self._suggestions:
            if suggestion.book_id == book_id:
                return suggestion
        return None

    def ensure_downloaded(self, book_id: str) -> Optional[Path]:
        """
        Ensure the specified book exists in storage. Returns path if available.
        """
        book_path = self.storage_dir / f"{book_id}.txt"
        if book_path.exists():
            return book_path

        suggestion = self.find_suggestion(book_id)
        if not suggestion or not suggestion.download_url:
            return None

        try:
            logger.info("Downloading public domain book %s from %s", book_id, suggestion.download_url)
            response = requests.get(suggestion.download_url, timeout=30)
            response.raise_for_status()
            book_path.write_text(response.text, encoding="utf-8")
            return book_path
        except Exception as exc:
            logger.error("Failed to download %s: %s", book_id, exc)
            if book_path.exists():
                book_path.unlink(missing_ok=True)
            return None
