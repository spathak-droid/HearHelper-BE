"""
Utilities for shaping long-form narration into SSML and translating SSML back
to plain text when the synthesis engine cannot interpret markup directly.

Book narration benefits from explicit paragraph pauses, sentence level pacing,
and slightly different prosody for dialogue, questions, or shouted lines.
"""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from typing import List, Optional

from .text_enricher import PAUSE_MARKER

PARAGRAPH_SPLIT_PATTERN = re.compile(r"(?:\r?\n){2,}")
SENTENCE_BOUNDARY_PATTERN = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'“‘])")
CLAUSE_BREAK_PATTERN = re.compile(r",\s+")
SEMICOLON_BREAK_PATTERN = re.compile(r";\s*")
BREAK_STRENGTH_MS = {
    "x-weak": 120,
    "weak": 220,
    "medium": 380,
    "strong": 650,
    "x-strong": 900,
}
CLAUSE_BREAK_TOKEN = "__SSML_BREAK_SHORT__"
SEMICOLON_BREAK_TOKEN = "__SSML_BREAK_MEDIUM__"
SHORT_PAUSE_MARKER = " .. "


def build_book_ssml(text: str) -> str:
    """
    Convert raw book text into SSML tailored for long-form narration.

    - Paragraphs become <p> nodes with longer pauses between them.
    - Sentences are enclosed in <s> elements.
    - Clauses inside long sentences receive light <break> cues.
    - Dialogue/questions/exclamations are wrapped with <prosody> tweaks.
    """
    paragraphs = _split_paragraphs(text)
    if not paragraphs:
        return "<speak/>"

    ssml_parts: List[str] = ['<speak version="1.0" xml:lang="en-US">']

    for index, paragraph in enumerate(paragraphs):
        sentences = _split_sentences(paragraph)
        if not sentences:
            continue

        ssml_parts.append("<p>")
        for sent_index, sentence in enumerate(sentences):
            sentence_markup = _sentence_to_ssml(sentence)
            if not sentence_markup:
                continue

            ssml_parts.append("<s>")
            ssml_parts.append(sentence_markup)
            ssml_parts.append("</s>")

            if sent_index < len(sentences) - 1:
                ssml_parts.append('<break time="420ms" />')
        ssml_parts.append("</p>")

        if index < len(paragraphs) - 1:
            ssml_parts.append('<break time="1100ms" />')

    ssml_parts.append("</speak>")
    return "".join(ssml_parts)


def ssml_to_plain_text(ssml: str, pause_marker: str = PAUSE_MARKER) -> str:
    """
    Reduce SSML to plain text for engines that do not understand SSML.

    Break elements become pause markers so that downstream TTS still leaves
    breathing room even when SSML is stripped.
    """
    if not ssml:
        return ""

    try:
        root = ET.fromstring(ssml)
    except ET.ParseError:
        text_only = re.sub(r"<[^>]+>", " ", ssml)
        return _collapse_whitespace(html.unescape(text_only))

    tokens: List[str] = []

    def _walk(node: ET.Element) -> None:
        if node.text:
            tokens.append(node.text)

        for child in node:
            tag = child.tag.split("}")[-1].lower()
            if tag == "break":
                tokens.append(_marker_for_break(child, pause_marker))
            else:
                _walk(child)

            if child.tail:
                tokens.append(child.tail)

    _walk(root)
    combined = "".join(tokens)
    return _collapse_whitespace(html.unescape(combined))


def _split_paragraphs(text: str) -> List[str]:
    normalized = text.replace("\r\n", "\n").strip()
    if not normalized:
        return []
    paragraphs = [
        paragraph.strip()
        for paragraph in PARAGRAPH_SPLIT_PATTERN.split(normalized)
        if paragraph.strip()
    ]
    if paragraphs:
        return paragraphs
    return [normalized]


def _split_sentences(paragraph: str) -> List[str]:
    paragraph = paragraph.strip()
    if not paragraph:
        return []
    sentences = [
        sentence.strip()
        for sentence in SENTENCE_BOUNDARY_PATTERN.split(paragraph)
        if sentence.strip()
    ]
    if sentences:
        return sentences
    return [paragraph]


def _sentence_to_ssml(sentence: str) -> str:
    if not sentence:
        return ""

    prepared = _inject_clause_breaks(sentence)
    prepared = _inject_semicolon_breaks(prepared)
    escaped = html.escape(prepared, quote=False)
    escaped = escaped.replace(CLAUSE_BREAK_TOKEN, '<break time="260ms" />')
    escaped = escaped.replace(SEMICOLON_BREAK_TOKEN, '<break time="420ms" />')

    prosody = _prosody_attributes(sentence)
    if prosody:
        return f"<prosody {prosody}>{escaped}</prosody>"
    return escaped


def _inject_clause_breaks(sentence: str) -> str:
    if "," not in sentence:
        return sentence
    return CLAUSE_BREAK_PATTERN.sub(lambda match: f"{match.group(0)}{CLAUSE_BREAK_TOKEN}", sentence)


def _inject_semicolon_breaks(sentence: str) -> str:
    if ";" not in sentence:
        return sentence
    return SEMICOLON_BREAK_PATTERN.sub(lambda match: f"; {SEMICOLON_BREAK_TOKEN}", sentence)


def _prosody_attributes(sentence: str) -> Optional[str]:
    trimmed = sentence.strip()
    if not trimmed:
        return None

    lowered = trimmed.lower()
    is_dialogue = trimmed.startswith(('"', "“", "'"))
    if not is_dialogue and '"' in trimmed:
        first_quote = trimmed.find('"')
        if first_quote != -1 and trimmed.find('"', first_quote + 1) != -1:
            is_dialogue = True
    if is_dialogue:
        return 'rate="medium" pitch="+1st" volume="+1dB"'

    if trimmed.endswith("?"):
        return 'rate="medium" pitch="+1st"'
    if trimmed.endswith("!"):
        return 'rate="fast" volume="+2dB"'
    if len(trimmed) >= 220:
        return 'rate="slow"'
    if "—" in trimmed or " - " in trimmed:
        return 'rate="medium"'
    if "said" in lowered or "whispered" in lowered or "shouted" in lowered:
        return 'rate="medium" pitch="+0.5st"'
    return None


def _marker_for_break(node: ET.Element, default_marker: str) -> str:
    time_attr = node.attrib.get("time")
    strength = node.attrib.get("strength")
    duration_ms = None

    if time_attr:
        duration_ms = _parse_duration_to_ms(time_attr)
    elif strength:
        duration_ms = BREAK_STRENGTH_MS.get(strength.lower())

    if duration_ms is None:
        return default_marker

    if duration_ms >= 2500:
        return f"{default_marker.strip()} {default_marker.strip()} {default_marker.strip()} "
    if duration_ms >= 1300:
        return f"{default_marker.strip()} {default_marker.strip()} "
    if duration_ms >= 800:
        return default_marker
    if duration_ms >= 400:
        return SHORT_PAUSE_MARKER
    return " "


def _parse_duration_to_ms(value: str) -> Optional[int]:
    value = value.strip().lower()
    if not value:
        return None
    numeric = value.rstrip("abcdefghijklmnopqrstuvwxyz")
    try:
        amount = float(numeric)
    except ValueError:
        return None

    if value.endswith("ms"):
        return int(amount)
    if value.endswith("s"):
        return int(amount * 1000)
    return int(amount)


def _collapse_whitespace(text: str) -> str:
    return re.sub(r"\s{2,}", " ", text).strip()


__all__ = ["build_book_ssml", "ssml_to_plain_text"]
