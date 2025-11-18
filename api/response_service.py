# api/response_service.py
import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from .book_service import BookConverter


def _tokenize(text: str) -> List[str]:
    """Lowercase, strip punctuation, and split text into tokens."""
    return re.findall(r"\b[\w']+\b", text.lower())


def _vectorize(tokens: Sequence[str]) -> Counter:
    """Create a frequency counter for the provided tokens."""
    return Counter(tokens)


def _cosine_similarity(vec_a: Counter, vec_b: Counter) -> float:
    """Compute cosine similarity between two sparse frequency vectors."""
    if not vec_a or not vec_b:
        return 0.0

    intersection = set(vec_a.keys()) & set(vec_b.keys())
    dot_product = sum(vec_a[token] * vec_b[token] for token in intersection)
    if dot_product == 0:
        return 0.0

    magnitude_a = math.sqrt(sum(value * value for value in vec_a.values()))
    magnitude_b = math.sqrt(sum(value * value for value in vec_b.values()))
    if magnitude_a == 0 or magnitude_b == 0:
        return 0.0

    return dot_product / (magnitude_a * magnitude_b)


@dataclass
class ResponseTemplate:
    prompts: List[str]
    response: str
    requires_book_list: bool = False
    book_prompt_type: str = "general"  # "general" or "suggestions"


@dataclass
class ResponseResult:
    response_text: str
    matched_prompt: Optional[str]
    confidence: float
    intent: Optional[str] = None
    context: Optional[Dict[str, object]] = None


class SimpleResponder:
    """
    Lightweight semantic-ish responder that performs cosine similarity against
    a handful of sample prompts to pick an appropriate canned response.
    """

    def __init__(self) -> None:
        self.book_converter = BookConverter()
        self.templates: List[ResponseTemplate] = [
            ResponseTemplate(
                prompts=["hi", "hello", "hey there", "good morning", "good afternoon"],
                response="Hello! How can I help you?",
            ),
            ResponseTemplate(
                prompts=[
                    "can you help",
                    "help me",
                    "can you assist",
                    "i need assistance",
                    "i need some help",
                ],
                response="Sure, what can I get started with?",
            ),
            ResponseTemplate(
                prompts=[
                    "who are you",
                    "what are you",
                    "what is this",
                    "tell me about yourself",
                ],
                response="I'm HearHelper, your voice assistant for quick conversations.",
            ),
            ResponseTemplate(
                prompts=[
                    "thank you",
                    "thanks a lot",
                    "appreciate it",
                    "thanks for the help",
                ],
                response="Happy to help! Is there anything else you need?",
            ),
            ResponseTemplate(
                prompts=[
                    "goodbye",
                    "bye",
                    "see you later",
                    "talk to you soon",
                ],
                response="Take care! I'm here whenever you want to chat again.",
            ),
            ResponseTemplate(
                prompts=[
                    "i want to read a book",
                    "read a book",
                    "audiobook",
                    "can you read me a book",
                    "lets read a book",
                    "play an audiobook",
                ],
                response="book_prompt",
                requires_book_list=True,
                book_prompt_type="general",
            ),
            ResponseTemplate(
                prompts=[
                    "suggest me a book",
                    "recommend a book",
                    "any book suggestions",
                    "which books do you have",
                    "what audiobooks are available",
                ],
                response="book_prompt",
                requires_book_list=True,
                book_prompt_type="suggestions",
            ),
        ]

        self.fallback_response = (
            "I'm here to help with anything you need. What can I do for you?"
        )
        self._compiled_templates = self._compile_templates()

    def _compile_templates(
        self,
    ) -> List[Tuple[ResponseTemplate, List[Tuple[str, Counter]]]]:
        compiled = []
        for template in self.templates:
            prompt_vectors = [
                (prompt, _vectorize(_tokenize(prompt))) for prompt in template.prompts
            ]
            compiled.append((template, prompt_vectors))
        return compiled

    def generate_reply(self, user_text: str) -> ResponseResult:
        tokens = _tokenize(user_text)
        vector = _vectorize(tokens)
        if not vector:
            return ResponseResult(
                response_text=self.fallback_response,
                matched_prompt=None,
                confidence=0.0,
            )

        best_template: Optional[ResponseTemplate] = None
        best_prompt: Optional[str] = None
        best_score = 0.0

        for template, prompt_vectors in self._compiled_templates:
            for prompt, prompt_vector in prompt_vectors:
                score = _cosine_similarity(vector, prompt_vector)
                if score > best_score:
                    best_score = score
                    best_prompt = prompt
                    best_template = template

        if best_template and best_score >= 0.2:
            response_text = best_template.response
            intent = None
            context: Optional[Dict[str, object]] = None

            if best_template.requires_book_list:
                intent = "book_prompt"
                response_text, context = self._book_list_response(best_template.book_prompt_type)

            return ResponseResult(
                response_text=response_text,
                matched_prompt=best_prompt,
                confidence=best_score,
                intent=intent,
                context=context,
            )

        return ResponseResult(
            response_text=self.fallback_response,
            matched_prompt=None,
            confidence=0.0,
        )

    def _book_list_response(self, prompt_type: str) -> Tuple[str, Dict[str, object]]:
        suggestions = self.book_converter.book_sources.suggestions()
        books = list(self.book_converter.available_books())
        if not books:
            return (
                "I'd love to read with you, but I don't have any public domain books "
                "loaded yet. Drop a .txt file into the public_domain_books folder and try again.",
                {"book_ids": []},
            )

        title_lookup = {
            suggestion.book_id: suggestion.title for suggestion in suggestions
        }
        book_ids = list(dict.fromkeys(books))
        display_titles = [
            title_lookup.get(book_id, self._humanize_title(book_id)) for book_id in book_ids
        ]

        if len(display_titles) == 1:
            book_list_text = display_titles[0]
        elif len(display_titles) == 2:
            book_list_text = " or ".join(display_titles)
        else:
            book_list_text = ", ".join(display_titles[:-1]) + f", or {display_titles[-1]}"

        if prompt_type == "suggestions":
            response_text = (
                "Here are a few public domain books I can stream instantly: "
                f"{book_list_text}. Just say the title you want."
            )
        else:
            response_text = (
                "Sure, which book would you like to hear? I can offer "
                f"{book_list_text}. "
                "If you have another public domain title in mind, tell me the name and I'll try to fetch it."
            )

        context = {
            "book_ids": book_ids,
            "book_titles": {book_id: display_titles[idx] for idx, book_id in enumerate(book_ids)},
            "suggestions": [
                {
                    "id": suggestion.book_id,
                    "title": suggestion.title,
                    "source": suggestion.source,
                    "description": suggestion.description,
                }
                for suggestion in suggestions
            ],
        }
        return response_text, context

    @staticmethod
    def _humanize_title(book_id: str) -> str:
        return book_id.replace("_", " ").title()


response_service = SimpleResponder()
