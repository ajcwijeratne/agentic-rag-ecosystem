"""
Sentence chunking for spoken replies
====================================
Turns a stream of LLM tokens into speakable fragments, so the assistant can
start talking while the rest of the answer is still being generated.

This is the single biggest lever on perceived latency. Measured on this stack,
a spoken turn waited 12.7s for the complete answer before making any sound —
86% of the time from wake word to first audio. Speaking sentence one as soon as
it exists collapses that wait to roughly the time-to-first-sentence.

Two rules shape the design:

  * The first fragment is emitted aggressively — at the first clause boundary,
    or once enough characters have accumulated — because it alone determines
    when the silence ends. Later fragments wait for real sentence ends, which
    sound better and by then cost nothing, since speech is already playing and
    generation is running ahead of the voice.
  * A boundary is only a boundary if it is not an abbreviation, a decimal, or
    an ellipsis. Splitting "3.5 per cent" into two utterances makes the speech
    stutter in a way listeners notice immediately.
"""

from __future__ import annotations

import re

# Terminators that end a spoken sentence.
_SENTENCE_END = re.compile(r"[.!?]['\"’”)]?(\s|$)")

# A full stop that is not a sentence end: an abbreviation, an initial, a decimal,
# or one of a run of dots.
_NOT_AN_END = re.compile(
    r"(?:"
    r"\b(?:mr|mrs|ms|dr|prof|st|sr|jr|vs|etc|eg|ie|approx|dept|est|fig|no|al)\.$"
    r"|\b[A-Z]\.$"                 # initials: "A." in "A. Wijeratne"
    r"|\d\.$"                      # decimals: the "3." of 3.5
    r"|\.\.$"                      # part of an ellipsis
    r")",
    re.IGNORECASE,
)

# Softer boundaries, used only to get the first fragment out quickly.
_CLAUSE_END = re.compile(r"[,;:—](\s|$)")


class SentenceChunker:
    """
    Feed tokens in, take speakable fragments out.

        chunker = SentenceChunker()
        for token in stream:
            for fragment in chunker.push(token):
                speak(fragment)
        for fragment in chunker.flush():
            speak(fragment)

    `first_min_chars` and `first_max_chars` bound the opening fragment: short
    enough to start speaking quickly, long enough not to sound clipped.
    """

    def __init__(
        self,
        first_min_chars: int = 30,
        first_max_chars: int = 140,
        min_chars: int = 40,
        max_chars: int = 320,
    ):
        self.first_min_chars = first_min_chars
        self.first_max_chars = first_max_chars
        self.min_chars = min_chars
        self.max_chars = max_chars
        self._buf = ""
        self.emitted = 0

    # -- helpers -----------------------------------------------------------

    def _is_real_end(self, text: str) -> bool:
        stripped = text.rstrip()
        return not _NOT_AN_END.search(stripped)

    def _find_boundary(self, text: str, pattern: re.Pattern) -> int:
        """Index just past the last usable boundary in `text`, or -1."""
        best = -1
        for m in pattern.finditer(text):
            end = m.end()
            if self._is_real_end(text[:end]):
                best = end
        return best

    # -- streaming ---------------------------------------------------------

    def push(self, token: str) -> list:
        """Add a token; return any fragments now ready to speak."""
        if not token:
            return []
        self._buf += token
        out: list = []

        while True:
            fragment = self._take()
            if fragment is None:
                break
            out.append(fragment)
        return out

    def _take(self) -> str | None:
        buf = self._buf
        if not buf.strip():
            return None

        first = self.emitted == 0
        min_chars = self.first_min_chars if first else self.min_chars
        max_chars = self.first_max_chars if first else self.max_chars

        if len(buf.strip()) < min_chars:
            return None

        cut = self._find_boundary(buf, _SENTENCE_END)

        # For the opening fragment only, a clause boundary will do — getting
        # sound out quickly matters more than a perfect sentence.
        if cut == -1 and first:
            cut = self._find_boundary(buf, _CLAUSE_END)

        # Runaway sentence: cut at the last space rather than let the listener
        # wait indefinitely for a full stop that may never arrive.
        if cut == -1 and len(buf) >= max_chars:
            space = buf.rfind(" ", 0, max_chars)
            cut = space if space > min_chars else max_chars

        if cut == -1:
            return None

        fragment = buf[:cut].strip()
        self._buf = buf[cut:]
        if not fragment:
            return None
        self.emitted += 1
        return fragment

    def flush(self) -> list:
        """Emit whatever is left at the end of the stream."""
        rest = self._buf.strip()
        self._buf = ""
        if not rest:
            return []
        self.emitted += 1
        return [rest]


def split_for_speech(text: str, **kwargs) -> list:
    """Chunk a complete string the same way the streaming path would."""
    chunker = SentenceChunker(**kwargs)
    out = chunker.push(text)
    out.extend(chunker.flush())
    return out


def cap_sentences(text: str, max_sentences: int) -> str:
    """
    Hard-limit a spoken answer to N sentences.

    The persona asks the model for brevity; this enforces it. A prompt is a
    request, and a spoken answer that ignores it costs the listener far more
    than it costs the model — 500 characters is roughly half a minute of
    talking at them.
    """
    if max_sentences <= 0 or not text:
        return text
    sentences, start = [], 0
    for m in _SENTENCE_END.finditer(text):
        end = m.end()
        if not _NOT_AN_END.search(text[:end].rstrip()):
            sentences.append(text[start:end].strip())
            start = end
            if len(sentences) >= max_sentences:
                break
    if not sentences:
        return text.strip()
    if start < len(text) and len(sentences) < max_sentences:
        tail = text[start:].strip()
        if tail:
            sentences.append(tail)
    return " ".join(s for s in sentences if s).strip()
