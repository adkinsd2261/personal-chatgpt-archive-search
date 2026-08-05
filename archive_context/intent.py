from __future__ import annotations

import re
import unicodedata

from tools.archive_lib import searchable_terms

from .models import QueryIntent, QueryVariant


EARLIEST_RE = re.compile(
    r"\b(first time|earliest|when did i (?:first )?(?:start|begin)|first found|first began)\b",
    re.IGNORECASE,
)
LATEST_RE = re.compile(
    r"\b(latest|most recent|recently|last time|current(?:ly)?|right now|where (?:are|am) (?:we|i) now)\b",
    re.IGNORECASE,
)
LONGITUDINAL_RE = re.compile(
    r"\b(over time|timeline|longitudinal|how (?:have|has|did).*(?:change|evolve)|"
    r"changed|evolved|early.*recent|then.*now|across (?:the )?years?)\b",
    re.IGNORECASE,
)
DECISION_RE = re.compile(
    r"\b(decision|decide|decided|plan|next step|what should|settled on|direction|roadmap)\b",
    re.IGNORECASE,
)
CORRECTION_RE = re.compile(
    r"\b(correct(?:ed|ion)?|final position|what did i (?:finally )?mean|"
    r"what i actually meant|rejected|wrong|instead|not .* but|settled on)\b",
    re.IGNORECASE,
)
EXACT_RE = re.compile(r"\b(exact (?:phrase|quote|line|words)|verbatim|what did i say)\b", re.IGNORECASE)
REJECTION_RE = re.compile(
    r"^\s*(?:nah|nope|no\b|wrong\b|not it\b)|\b(?:that(?:'|’)s not|doesn(?:'|’)t work|you missed)\b",
    re.IGNORECASE,
)
REFINEMENT_RE = re.compile(
    r"\b(?:actually|i mean|more like|instead|correction|corrected|but what i|"
    r"not .* but|the point is|old direction was right,? but)\b",
    re.IGNORECASE,
)
ADOPTION_RE = re.compile(
    r"\b(?:exactly|that(?:'|’)s it|this is it|perfect|you nailed it|this works|yes[, ]+that)\b",
    re.IGNORECASE,
)


def normalize_for_match(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def _quoted_phrases(query: str) -> tuple[str, ...]:
    phrases = re.findall(r"[\"“](.{2,300}?)[\"”]", query)
    return tuple(dict.fromkeys(phrase.strip() for phrase in phrases if phrase.strip()))[:2]


def _identifiers(query: str) -> tuple[str, ...]:
    generic = {
        "All", "Can", "Could", "Did", "Do", "Find", "First", "How", "Latest",
        "Most", "My", "Tell", "The", "What", "When", "Where", "Which", "Who", "Why",
    }
    tokens = re.findall(r"\b[A-Z][A-Za-z0-9_-]{2,}\b|\b[A-Za-z]+\d+[A-Za-z0-9_.-]*\b", query)
    return tuple(dict.fromkeys(token for token in tokens if token not in generic))[:3]


def interpret_query(query: str) -> QueryIntent:
    normalized = normalize_for_match(query)
    quoted = _quoted_phrases(query)
    identifiers = _identifiers(query)
    flags: list[str] = []
    if quoted or EXACT_RE.search(query):
        flags.append("exact_phrase")
    if EARLIEST_RE.search(query):
        flags.append("earliest")
    if LATEST_RE.search(query):
        flags.append("current_state")
    if LONGITUDINAL_RE.search(query):
        flags.append("change_over_time")
    if DECISION_RE.search(query):
        flags.append("decision")
    if CORRECTION_RE.search(query):
        flags.append("final_position")
    if identifiers:
        flags.append("named_identifier")

    if "final_position" in flags:
        mode = "correction"
    elif "earliest" in flags:
        mode = "earliest"
    elif "change_over_time" in flags:
        mode = "longitudinal"
    elif "current_state" in flags:
        mode = "latest"
    elif "exact_phrase" in flags:
        mode = "exact"
    elif "decision" in flags:
        mode = "decision"
    else:
        mode = "recall"
    return QueryIntent(mode, tuple(flags), normalized, quoted, identifiers)


def generate_variants(query: str, intent: QueryIntent, cap: int = 8) -> list[QueryVariant]:
    terms = searchable_terms(query)
    variants: list[QueryVariant] = [QueryVariant("semantic_original", query.strip(), 1.0)]
    for phrase in intent.quoted_phrases:
        variants.append(QueryVariant("quoted_phrase", phrase, 1.0))
    for identifier in intent.identifiers:
        variants.append(QueryVariant("identifier", identifier, 0.9))
    if terms:
        variants.append(QueryVariant("lexical_and", " ".join(terms[:12]), 0.85))
        variants.append(QueryVariant("lexical_or", " ".join(terms[:16]), 0.55))
    anchors = list(intent.identifiers) or terms[:3]
    temporal_anchor = " ".join(anchors[:2])
    if temporal_anchor:
        if intent.primary_mode == "longitudinal":
            variants.append(QueryVariant("temporal_earliest", temporal_anchor, 0.35))
            variants.append(QueryVariant("temporal_latest", temporal_anchor, 0.35))
        elif intent.primary_mode == "earliest":
            variants.append(QueryVariant("temporal_earliest", temporal_anchor, 0.35))
        elif intent.primary_mode == "latest" or "current_state" in intent.flags:
            variants.append(QueryVariant("temporal_latest", temporal_anchor, 0.35))
    if intent.primary_mode == "correction" and anchors:
        variants.append(QueryVariant("correction_terms", " ".join(anchors + ["actually", "instead", "correction"]), 0.6))
    elif intent.primary_mode == "longitudinal" and anchors:
        variants.append(QueryVariant("change_terms", " ".join(anchors + ["changed", "evolved"]), 0.6))

    output: list[QueryVariant] = []
    seen: set[tuple[str, str]] = set()
    for variant in variants:
        normalized = normalize_for_match(variant.text)
        key = (variant.kind, normalized)
        if normalized and key not in seen:
            seen.add(key)
            output.append(variant)
        if len(output) >= cap:
            break
    return output


def classify_user_signal(text: str) -> str | None:
    sample = text[:4000]
    if REJECTION_RE.search(sample):
        return "possible_rejection"
    if REFINEMENT_RE.search(sample):
        return "possible_correction_or_refinement"
    if ADOPTION_RE.search(sample):
        return "possible_adoption_or_confirmation"
    return None
