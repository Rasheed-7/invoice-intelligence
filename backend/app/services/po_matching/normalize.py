"""Deterministic description normalization and similarity.

Everything here is plain string math (difflib) — the comparison path never
calls an LLM.
"""

from difflib import SequenceMatcher

# Packaging/unit words that extraction emits inconsistently ("10 pcs Screw"
# vs "Screw" vs "Screw unit"). They describe how an item is packed, not what
# the item is, so removing them stops packaging noise from lowering the
# similarity between otherwise identical lines.
_NOISE_TOKENS = frozenset({"pcs", "unit", "x"})

# Punctuation is rendered inconsistently between POs and invoices
# ("red-fox" / "red fox" / "redfox", "v2.0" / "v2-0"). Replace it with a
# space so hyphen/period joins split into separate tokens instead of fusing
# words together.
_PUNCTUATION = str.maketrans({ch: " " for ch in ".,;:-_"})


def normalize_description(s: str | None) -> str:
    """Lowercase, strip, collapse whitespace, drop punctuation and noise tokens."""
    if s is None:
        return ""
    text = s.lower().strip().translate(_PUNCTUATION)
    tokens = [token for token in text.split() if token not in _NOISE_TOKENS]
    return " ".join(tokens)


def description_similarity(a: str | None, b: str | None) -> float:
    """Similarity ratio in [0, 1] between normalized descriptions.

    ``None`` behaves like an empty string: two missing descriptions are
    identical (1.0), a missing vs. present description is unrelated (0.0).
    """
    if a is None and b is None:
        return 1.0
    if a is None or b is None:
        return 0.0
    return SequenceMatcher(None, normalize_description(a), normalize_description(b)).ratio()


def descriptions_match(a: str | None, b: str | None, threshold: float = 0.8) -> bool:
    """True when both descriptions are (near-)identical after normalization.

    Both ``None`` counts as a match (nothing to disagree about); exactly one
    ``None`` does not. Comparison is difflib ratio on normalized text —
    deterministic, no LLM.
    """
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return description_similarity(a, b) >= threshold
