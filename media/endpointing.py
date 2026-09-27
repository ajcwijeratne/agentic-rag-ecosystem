"""
Adaptive endpointing
====================
Decides how long a pause has to last before an utterance counts as finished.

A fixed timer is wrong in both directions. Set it short and it cuts people off
mid-thought, the moment they pause to find a word; set it long and every
finished sentence is followed by dead air before the assistant reacts. The
Command Centre used a flat 600ms, which on a measured 3.6s time-to-first-audio
is a sixth of the whole wait.

The signal is already there for free. VOSK is producing a running partial
transcript, and how an utterance trails off says a great deal about whether the
speaker has finished:

    "what is the diagnostic sprint"      finished, stop waiting
    "what is the diagnostic sprint and"  clearly mid-sentence, wait longer
    "show me the"                        a dangling determiner, wait longer
    "um"                                 filler, wait much longer

So the silence requirement is scaled per utterance rather than fixed. No model
and no extra latency: it is a lookup against the tail of text already being
produced for the live caption.

This is a heuristic, deliberately. A neural endpointer would be better and needs
a model, a download and a GPU budget; this costs nothing and removes most of the
penalty in both directions.
"""

from __future__ import annotations

import os
import re

# Fraction of the configured silence window to wait, by how finished the
# utterance sounds. 1.0 is the unmodified VAD_SILENCE_MS.
ENDPOINT_ADAPTIVE: bool = os.getenv("ENDPOINT_ADAPTIVE", "true").lower() in ("1", "true", "yes")
ENDPOINT_COMPLETE_SCALE:   float = float(os.getenv("ENDPOINT_COMPLETE_SCALE", "0.55"))
ENDPOINT_NEUTRAL_SCALE:    float = float(os.getenv("ENDPOINT_NEUTRAL_SCALE", "1.0"))
ENDPOINT_INCOMPLETE_SCALE: float = float(os.getenv("ENDPOINT_INCOMPLETE_SCALE", "1.8"))

# Words that almost never end an English sentence. Stopping on one of these
# means cutting the speaker off mid-clause.
_DANGLING = {
    # conjunctions and connectives
    "and", "or", "but", "so", "because", "although", "though", "while", "whereas",
    "if", "unless", "until", "since", "than", "then", "plus", "versus",
    # determiners and prepositions
    "the", "a", "an", "my", "our", "your", "their", "its", "this", "that", "these",
    "those", "some", "any", "each", "every", "of", "to", "for", "with", "about",
    "from", "into", "onto", "over", "under", "between", "against", "per",
    # auxiliaries and pronouns left hanging
    "is", "are", "was", "were", "be", "been", "am", "do", "does", "did", "have",
    "has", "had", "can", "could", "will", "would", "should", "shall", "may",
    "might", "must", "i", "we", "they", "he", "she", "it", "you",
}

# Hesitation. A pause after one of these is thinking, not finishing.
_FILLER = {"um", "uh", "er", "erm", "ah", "hmm", "like", "sort", "kind", "well"}

# Openers that signal a question is still being formed.
_QUESTION_OPENERS = ("what", "who", "when", "where", "why", "how", "which",
                     "can", "could", "would", "should", "is", "are", "do", "does",
                     "tell", "show", "give", "find", "read", "summarise", "summarize")


def _words(text: str) -> list:
    return re.findall(r"[a-z0-9']+", (text or "").lower())


def completeness(text: str) -> str:
    """
    Classify how finished an utterance sounds: complete, neutral or incomplete.

    Judged on the tail, because that is what a pause follows.
    """
    words = _words(text)
    if not words:
        return "neutral"

    last = words[-1]
    if last in _FILLER or last in _DANGLING:
        return "incomplete"

    # Too short to be a question on its own — "show me" is going somewhere.
    if len(words) <= 2 and words[0] in _QUESTION_OPENERS:
        return "incomplete"

    # Explicit terminal punctuation from the recogniser, when it offers any.
    if (text or "").rstrip().endswith(("?", ".", "!")):
        return "complete"

    # A question that opened with an interrogative and has a reasonable body
    # reads as finished.
    if words[0] in _QUESTION_OPENERS and len(words) >= 4:
        return "complete"

    return "neutral"


def silence_scale(text: str) -> float:
    """How much of the configured silence window this utterance should wait."""
    if not ENDPOINT_ADAPTIVE:
        return 1.0
    return {
        "complete":   ENDPOINT_COMPLETE_SCALE,
        "neutral":    ENDPOINT_NEUTRAL_SCALE,
        "incomplete": ENDPOINT_INCOMPLETE_SCALE,
    }[completeness(text)]


def silence_ms_for(text: str, base_ms: int) -> int:
    """The silence window to use for this utterance, in milliseconds."""
    return max(120, int(round(base_ms * silence_scale(text))))
