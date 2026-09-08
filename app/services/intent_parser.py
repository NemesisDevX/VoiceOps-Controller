"""Deterministic, stateful voice-command parsing with conversational pronoun resolution.

The parser is intentionally rule-based (no LLM) so that operator commands issued during an
incident are fast, auditable, and fully reproducible. `ConversationContext` retains the
subjects of the most recent INSPECT query so that a natural follow-up like "kill it" or
"isolate the top one" resolves against the process that was just surfaced.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.core.security import requires_confirmation
from app.schemas.command import InspectionTarget, IntentType, ParsedCommand
from app.schemas.telemetry import ProcessInfo

_PRONOUN_PATTERN = re.compile(
    r"\b(it|that process|that one|the top one|the top process|that pid|this process)\b",
    re.IGNORECASE,
)
_PID_PATTERN = re.compile(r"\bpid\s+(\d+)\b", re.IGNORECASE)
_BARE_NUMBER_PATTERN = re.compile(r"\b(\d+)\b")
_IP_PATTERN = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")
_PROCESS_NAME_PATTERN = re.compile(
    r"\b(?:process|processes|program)\s+(?:named|called)?\s*([a-zA-Z0-9_.\-]+)",
    re.IGNORECASE,
)

_KILL_VERBS = ("kill", "terminate", "stop", "end")
_ISOLATE_VERBS = ("isolate", "quarantine", "block", "disconnect", "cut off")
_INSPECT_VERBS = ("show", "list", "what", "which", "display", "get", "check")

# --- Multilingual lexicon matrix -------------------------------------------------------------
#
# `_LEXICON` maps each `IntentType` to the verbs that express it, grouped by ISO 639-1 language
# code. The English entries intentionally start from the original `_KILL_VERBS`/`_ISOLATE_VERBS`/
# `_INSPECT_VERBS` tuples above (extended with a few synonyms) so existing English matching
# behavior is unchanged; Arabic, Spanish, French, and Chinese verbs are new. Matching against
# this matrix is done via substring containment (see `_match_multilingual`) rather than `\b`
# word-boundary regexes, because Chinese has no whitespace word boundaries and Arabic script is
# not reliably segmented by ASCII-oriented boundary heuristics either.
_LEXICON: dict[IntentType, dict[str, list[str]]] = {
    IntentType.PROCESS_KILL: {
        "en": [*_KILL_VERBS, "nuke", "drop"],
        "ar": ["اقفل", "اقتل", "وقف"],
        "es": ["terminar", "detener"],
        "fr": ["arrêter", "tuer"],
        "zh": ["终止", "停止"],
    },
    IntentType.NETWORK_ISOLATE: {
        "en": [*_ISOLATE_VERBS, "firewall"],
        "ar": ["اعزل", "بلوك", "حظر"],
        "es": ["aislar", "bloquear"],
        "fr": ["isoler", "bloquer"],
        "zh": ["隔离", "封锁"],
    },
    IntentType.INSPECT: {
        "en": [*_INSPECT_VERBS, "inspect", "top", "monitor"],
        "ar": ["افحص", "وريني", "استعلم"],
        "es": ["inspeccionar", "mostrar"],
        "fr": ["inspecter", "afficher"],
        "zh": ["检查", "查看"],
    },
    IntentType.ROLLBACK: {
        "en": ["rollback", "revert", "undo"],
        "ar": ["ارجع", "الغ التعديل"],
        "es": ["revertir"],
        "fr": ["annuler"],
        "zh": ["回滚"],
    },
}

# Priority order in which intents are checked against the lexicon matrix. Mirrors the original
# kill-before-isolate-before-inspect ordering of the English-only `if` chain, with ROLLBACK
# inserted ahead of INSPECT (its verbs don't collide with inspection verbs in any language).
_INTENT_MATCH_ORDER: tuple[IntentType, ...] = (
    IntentType.PROCESS_KILL,
    IntentType.NETWORK_ISOLATE,
    IntentType.ROLLBACK,
    IntentType.INSPECT,
)
_LANG_ORDER: tuple[str, ...] = ("en", "ar", "es", "fr", "zh")

# Fuzzy resolution (requirement 3) is restricted to the Latin-script language family (English,
# Spanish, French) so that acoustic slips like "kilit"/"stopp"/"terminador" still resolve to the
# right intent. Arabic and Chinese are matched exclusively by exact/substring containment above;
# comparing their glyphs against Latin candidates with `difflib` would be meaningless and could
# produce nonsensical cross-script matches.
_LATIN_LANGS: tuple[str, ...] = ("en", "es", "fr")
_FUZZY_CUTOFF = 0.65  # Empirically the lowest threshold that still resolves "kilit" -> "kill"
# without matching unrelated short words (see tests for verification of both directions).
_LATIN_TOKEN_PATTERN = re.compile(r"[a-zà-öø-ÿ]+")

# Per-intent (not globally flattened) Latin verb buckets used for fuzzy matching. Keeping the
# buckets separated by intent -- rather than one flat list -- and walking them in
# `_INTENT_MATCH_ORDER` means a slip like "kilit" is compared against the PROCESS_KILL bucket
# first and resolves to "kill" there, instead of tying with an equally-close but wrong-intent
# word (e.g. "list") that happens to live in a different bucket.
_FUZZY_BUCKETS: dict[IntentType, list[str]] = {
    intent: [verb for lang in _LATIN_LANGS for verb in verbs.get(lang, [])] for intent, verbs in _LEXICON.items()
}


def _verb_language(intent: IntentType, verb: str) -> str:
    """Return the language code under which `verb` is registered for `intent`."""
    for lang in _LANG_ORDER:
        if verb in _LEXICON[intent].get(lang, []):
            return lang
    return "en"


# Fused verb+pronoun single tokens ("kill-it" said as one word) that have no separate substring
# match against the plain verb lexicon above because the pronoun suffix changes the verb's
# surface form (e.g. French "arrête" vs. infinitive "arrêter"). Each entry is a complete
# implicit "kill it" command in that language; matching one short-circuits straight to
# PROCESS_KILL with a forced pronoun/context-resolution signal, exactly like the bare English
# "it" pronoun path.
_FUSED_KILL_PRONOUNS: dict[str, list[str]] = {
    "ar": ["اقفله"],
    "es": ["terminarlo"],
    "fr": ["arrête-le"],
    "zh": ["把它关掉"],
}

# Standalone (non-fused) pronoun phrases in other languages, used the same way as the English
# `_PRONOUN_PATTERN` above: when one of these appears alongside a verb matched elsewhere in the
# lexicon (e.g. Arabic "اقفل هذه العملية" = "kill this process"), it signals that the target
# should be resolved from conversational context rather than parsed structurally.
_MULTILINGUAL_PRONOUNS: dict[str, list[str]] = {
    "ar": ["هذه العملية"],
    "es": ["eso", "ese proceso"],
    "fr": ["celui-là", "ce processus"],
    "zh": ["它", "这个进程"],
}


@dataclass
class ConversationContext:
    """Stateful memory of the most recent INSPECT result set, used for pronoun resolution."""

    last_intent: IntentType | None = None
    last_subjects: list[ProcessInfo] = field(default_factory=list)
    last_inspection_target: InspectionTarget | None = None
    updated_at: datetime | None = None

    def remember(self, intent: IntentType, subjects: list[ProcessInfo], target: InspectionTarget | None = None) -> None:
        """Record the results of an executed INSPECT query for later pronoun resolution."""
        self.last_intent = intent
        self.last_subjects = subjects
        self.last_inspection_target = target
        self.updated_at = datetime.now(timezone.utc)

    def top_subject(self) -> ProcessInfo | None:
        """Return the primary (highest-ranked) subject from the last INSPECT query, if any."""
        return self.last_subjects[0] if self.last_subjects else None

    def clear(self) -> None:
        self.last_intent = None
        self.last_subjects = []
        self.last_inspection_target = None
        self.updated_at = None


def _detect_inspection_target(text: str) -> InspectionTarget:
    if "memory" in text or "ram" in text:
        return InspectionTarget.MEMORY
    if "cpu" in text or "processor" in text:
        return InspectionTarget.CPU
    if "disk" in text or "storage" in text:
        return InspectionTarget.DISK
    if "network" in text or "socket" in text or "connection" in text:
        return InspectionTarget.NETWORK
    return InspectionTarget.GENERAL


class IntentParser:
    """Parses raw transcripts into `ParsedCommand`s, maintaining per-session conversational state."""

    def __init__(self) -> None:
        self.context = ConversationContext()

    def parse(self, text: str) -> ParsedCommand:
        """Parse a single transcript into a `ParsedCommand`, resolving pronouns against context."""
        normalized = unicodedata.normalize("NFKC", text.strip().lower())
        if not normalized:
            return ParsedCommand(raw_text=text, intent=IntentType.UNKNOWN, confidence=0.0)

        # Fused verb+pronoun forms (e.g. Arabic "اقفله", Spanish "terminarlo") are a complete
        # "kill it" command with no separately-matchable verb token; resolve them first.
        fused_language = self._match_fused_kill_pronoun(normalized)
        if fused_language is not None:
            return self._parse_mutation(
                text, normalized, IntentType.PROCESS_KILL, language=fused_language, force_pronoun=True
            )

        # Original English-only fast path, left untouched so existing behavior/tests are stable.
        if any(verb in normalized for verb in _KILL_VERBS):
            return self._parse_mutation(text, normalized, IntentType.PROCESS_KILL, language="en")

        if any(verb in normalized for verb in _ISOLATE_VERBS):
            return self._parse_mutation(text, normalized, IntentType.NETWORK_ISOLATE, language="en")

        if any(verb in normalized for verb in _INSPECT_VERBS):
            return self._parse_inspection(text, normalized, language="en")

        # Multilingual extension: exact/substring lexicon matches across all five languages,
        # falling back to Latin-script fuzzy matching for acoustic slips (see requirement 3/6).
        intent, language = self._match_multilingual(normalized)
        if intent is IntentType.INSPECT:
            return self._parse_inspection(text, normalized, language=language or "en")
        if intent in (IntentType.PROCESS_KILL, IntentType.NETWORK_ISOLATE, IntentType.ROLLBACK):
            return self._parse_mutation(text, normalized, intent, language=language or "en")

        return ParsedCommand(raw_text=text, intent=IntentType.UNKNOWN, confidence=0.0)

    def _match_fused_kill_pronoun(self, normalized: str) -> str | None:
        """Return the language code if `normalized` contains a fused verb+pronoun kill phrase."""
        for lang, phrases in _FUSED_KILL_PRONOUNS.items():
            if any(phrase in normalized for phrase in phrases):
                return lang
        return None

    def _has_multilingual_pronoun(self, normalized: str) -> bool:
        """True if a non-English standalone pronoun phrase (see `_MULTILINGUAL_PRONOUNS`) is present."""
        return any(phrase in normalized for phrases in _MULTILINGUAL_PRONOUNS.values() for phrase in phrases)

    def _match_multilingual(self, normalized: str) -> tuple[IntentType | None, str | None]:
        """Resolve `normalized` to an `(intent, language)` pair via the multilingual lexicon.

        First pass: exact/substring containment against every language's verb list, walked in
        `_INTENT_MATCH_ORDER` / `_LANG_ORDER` priority. Substring containment (rather than a
        `\\b`-bounded regex) is deliberate: Chinese text has no whitespace between words, so a
        lexicon phrase like "检查" must be found by scanning for it anywhere in the transcript.

        Second pass: fuzzy matching (stdlib `difflib.get_close_matches`) of Latin-script tokens
        against each intent's Latin-only verb bucket, to catch acoustic slips such as "kilit" or
        "stopp" without ever comparing Latin script against Arabic/Chinese script.
        """
        for intent in _INTENT_MATCH_ORDER:
            # Collect every matching verb across languages for this intent rather than
            # returning on the first hit: some short English verbs are literal substrings of
            # a longer verb in another language (e.g. "revert" inside Spanish "revertir"), so
            # the *longest* (most specific) matching verb wins the language attribution.
            candidates = [
                (len(verb), lang, verb)
                for lang in _LANG_ORDER
                for verb in _LEXICON[intent].get(lang, [])
                if verb in normalized
            ]
            if candidates:
                candidates.sort(key=lambda c: c[0], reverse=True)
                return intent, candidates[0][1]

        # Tokens shorter than 4 characters are excluded from fuzzy matching: short function
        # words ("how", "are", "it") are close enough (by edit distance) to short verbs like
        # "show" to produce false positives, whereas real acoustic slips on our multi-syllable
        # verbs ("kilit", "stopp", "terminador") are always at least 4 characters long.
        latin_tokens = [token for token in _LATIN_TOKEN_PATTERN.findall(normalized) if len(token) >= 4]
        if latin_tokens:
            for intent in _INTENT_MATCH_ORDER:
                bucket = _FUZZY_BUCKETS.get(intent, [])
                if not bucket:
                    continue
                for token in latin_tokens:
                    matches = difflib.get_close_matches(token, bucket, n=1, cutoff=_FUZZY_CUTOFF)
                    if matches:
                        return intent, _verb_language(intent, matches[0])

        return None, None

    def _parse_inspection(self, raw_text: str, normalized: str, language: str = "en") -> ParsedCommand:
        target = _detect_inspection_target(normalized)
        return ParsedCommand(
            raw_text=raw_text,
            intent=IntentType.INSPECT,
            inspection_target=target,
            requires_confirmation=False,
            confidence=0.9,
            language=language,
        )

    def _parse_mutation(
        self,
        raw_text: str,
        normalized: str,
        intent: IntentType,
        language: str = "en",
        force_pronoun: bool = False,
    ) -> ParsedCommand:
        pid_match = _PID_PATTERN.search(normalized)
        ip_match = _IP_PATTERN.search(normalized) if intent is IntentType.NETWORK_ISOLATE else None
        name_match = _PROCESS_NAME_PATTERN.search(normalized)

        target_pid: int | None = int(pid_match.group(1)) if pid_match else None
        target_ip: str | None = ip_match.group(1) if ip_match else None
        target_name: str | None = name_match.group(1) if name_match else None

        if target_pid is None and target_name is None and not target_ip:
            bare_number = _BARE_NUMBER_PATTERN.search(normalized)
            if bare_number:
                target_pid = int(bare_number.group(1))

        # `force_pronoun` is set for fused verb+pronoun tokens (see `_match_fused_kill_pronoun`),
        # which carry an implicit "resolve from context" signal the same way a bare "it" does.
        pronoun_signal = (
            force_pronoun
            or bool(_PRONOUN_PATTERN.search(normalized))
            or self._has_multilingual_pronoun(normalized)
        )

        resolved_from_context = False
        if target_pid is None and target_name is None and not target_ip and pronoun_signal:
            subject = self.context.top_subject()
            if subject is not None:
                target_pid = subject.pid
                target_name = subject.name
                resolved_from_context = True

        return ParsedCommand(
            raw_text=raw_text,
            intent=intent,
            target_pid=target_pid,
            target_process_name=target_name,
            target_ip=target_ip,
            resolved_from_context=resolved_from_context,
            requires_confirmation=requires_confirmation(intent),
            confidence=0.85 if (target_pid or target_name or target_ip) else 0.4,
            language=language,
        )

    def remember_inspection(self, target: InspectionTarget, subjects: list[ProcessInfo]) -> None:
        """Update conversational memory after an INSPECT command has been executed."""
        self.context.remember(IntentType.INSPECT, subjects, target)
