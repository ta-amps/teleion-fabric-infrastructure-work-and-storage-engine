"""Deterministic problem fingerprints: a small pure library, not a learning policy.

Shared with deploy/cloudflare/execution_observations.js through
tests/fixtures/execution_learning_vectors.json. Learners choose how to index,
rank or ignore these values. A fingerprint never authorizes reuse or admission.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from tessera.receipts import canonical_json

PROFILE = "execution-learning-v1"
MINHASH_SIZE = 32
BAND_COUNT = 4
BAND_ROWS = 2
MAX_WORDS = 512
MAX_SHAPE_KEYS = 32
MAX_BRIEF_TEXT = 4096
MAX_TYPE_BYTES = 1024
BRIEF_TEXT_FIELDS = (
    "title",
    "goal",
    "problem",
    "summary",
    "description",
    "question",
    "instructions",
    "topic",
)
_STOPWORD_TEXT = (
    "a an and any are as at be been but by can do does for from has have if in into is it its "
    "may must not of on or our should so such than that the their them then there these they "
    "this those to us was we were what when which while who will with would you your"
)
STOPWORDS = frozenset(_STOPWORD_TEXT.split())
_SPACE = re.compile(r"[ \t\n\v\f\r]+")
_MASK = 0xFFFFFFFF


def valid_text(text: str) -> bool:
    """False for strings with lone surrogates, which neither runtime can hash identically."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def normalized(text: str) -> str:
    """ASCII case fold, collapse ASCII whitespace; other characters are left unchanged."""
    folded = "".join(chr(ord(c) + 32) if "A" <= c <= "Z" else c for c in text)
    return _SPACE.sub(" ", folded).strip(" ")


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def tokens(text: str) -> list[str]:
    """Distinct ASCII-folded words in order; ASCII punctuation separates, non-ASCII is kept."""
    found: list[str] = []
    seen: set[str] = set()
    current: list[str] = []

    def flush() -> None:
        word = "".join(current)
        current.clear()
        if (len(word) > 1 or not word.isascii()) and word not in STOPWORDS and word not in seen:
            seen.add(word)
            found.append(word)

    for char in text:
        code = ord(char)
        if 65 <= code <= 90:
            current.append(chr(code + 32))
        elif 97 <= code <= 122 or 48 <= code <= 57 or code >= 128:
            current.append(char)
        elif current:
            flush()
    if current:
        flush()
    return found


def kind(value: Any) -> str:
    if value is None:
        return "null"
    if type(value) is bool:
        return "bool"
    if type(value) is int or (type(value) is float and value.is_integer()):
        return "int"
    if type(value) is float:
        return "num"
    if isinstance(value, str):
        return "str"
    return "list" if isinstance(value, list) else "object"


def type_signature(value: Any, depth: int = 0) -> str:
    """Bounded structural JSON type such as {"a":int,"b":[str]}; containers stop at depth 3."""
    shape = kind(value)
    if shape not in ("list", "object"):
        return shape
    if depth >= 3:
        return "any"
    if isinstance(value, list):
        return "[" + "|".join(sorted({type_signature(v, depth + 1) for v in value[:16]})) + "]"
    keys = sorted(k for k in value if valid_text(k))
    parts = [canonical_json(k) + ":" + type_signature(value[k], depth + 1) for k in keys[:32]]
    return "{" + ",".join(parts + (["..."] if len(keys) > 32 else [])) + "}"


def bounded_type(value: Any) -> str:
    signature = type_signature(value)
    if len(signature.encode("utf-8")) <= MAX_TYPE_BYTES:
        return signature
    return "sha256:" + hashlib.sha256(signature.encode("utf-8")).hexdigest()


def brief_view(brief: dict[str, Any]) -> dict[str, Any]:
    """The bounded part of a verb brief that fingerprints and observations may use."""
    text: dict[str, str] = {}
    budget = MAX_BRIEF_TEXT
    for name in BRIEF_TEXT_FIELDS:
        value = brief.get(name)
        if isinstance(value, str) and valid_text(value) and budget > 0:
            text[name] = value[:budget]
            budget -= len(text[name])
    contract = brief.get("contract")
    template = (
        contract
        if isinstance(contract, str)
        and valid_text(contract)
        and 0 < len(contract.encode("utf-8")) <= 128
        else None
    )
    keys = sorted(k for k in brief if valid_text(k))[:MAX_SHAPE_KEYS]
    return {
        "template": template,
        "text": text,
        "shape": {key: kind(brief[key]) for key in keys},
        "type": bounded_type(brief),
    }


def _view_text(view: dict[str, Any]) -> str:
    return "\n".join(view["text"][name] for name in BRIEF_TEXT_FIELDS if name in view["text"])


def phrasing_hash(view: dict[str, Any]) -> str | None:
    """Identity of a verb's normalized wording fields, independent of its other inputs."""
    fields = {
        name: normalized(view["text"][name]) for name in BRIEF_TEXT_FIELDS if name in view["text"]
    }
    fields = {name: text for name, text in fields.items() if text}
    return _digest({"profile": PROFILE, "phrasing": fields}) if fields else None


def features(category: str, view: dict[str, Any], report: dict[str, Any]) -> list[str]:
    """Category, declared input/output type tokens and problem words, sorted.

    Brief shape is deliberately excluded: verbs from one template share it, and it would
    make unrelated problems look alike. Template identity is reported separately.
    """
    found = {"c:" + category}
    types = (report.get("learning") or {}).get("types") or {}
    for role, prefix in (("input", "ti:"), ("output", "to:")):
        if isinstance(types.get(role), str):
            found.update(prefix + word for word in tokens(types[role]))
    problem = (report.get("summaries") or {}).get("problem") or ""
    words: set[str] = set()
    for word in tokens(_view_text(view)) + tokens(problem):
        if len(words) == MAX_WORDS:
            break
        words.add(word)
    found.update("w:" + word for word in words)
    return sorted(found)


def _fmix32(h: int) -> int:
    h ^= h >> 16
    h = (h * 0x85EBCA6B) & _MASK
    h ^= h >> 13
    h = (h * 0xC2B2AE35) & _MASK
    return h ^ (h >> 16)


def minhash(feature_list: list[str]) -> str | None:
    """32 minimum 32-bit hashes from one domain-separated SHA-256 per feature."""
    if not feature_list:
        return None
    mins = [1 << 32] * MINHASH_SIZE
    for feature in feature_list:
        digest = hashlib.sha256(
            b"tessera-execution-learning-v1\0" + feature.encode("utf-8")
        ).digest()
        words = [int.from_bytes(digest[i : i + 4], "big") for i in range(0, 32, 4)]
        for i in range(MINHASH_SIZE):
            value = _fmix32(words[i & 7] ^ (((i >> 3) * 0x9E3779B9) & _MASK))
            if value < mins[i]:
                mins[i] = value
    return "".join(f"{value:08x}" for value in mins)


def bands(signature: str | None) -> list[str]:
    """Four LSH band keys of two hashes each, for learners that index candidates."""
    if not signature:
        return []
    width = 8 * BAND_ROWS
    return [f"m{b}:{signature[b * width : (b + 1) * width]}" for b in range(BAND_COUNT)]


def similarity(a: str | None, b: str | None) -> float | None:
    """Estimated Jaccard similarity of two MinHash signatures; a ranking signal only."""
    if not a or not b or len(a) != len(b):
        return None
    same = sum(a[i : i + 8] == b[i : i + 8] for i in range(0, len(a), 8))
    return same / MINHASH_SIZE


def exact_fingerprint(category: str, input_type: str, input_digest: str | None) -> str | None:
    """SHA-256 of category, brief type signature and the captured input digest."""
    if not input_digest:
        return None
    return _digest(
        {
            "profile": PROFILE,
            "category": category,
            "input_type": input_type,
            "input_digest": input_digest,
        }
    )


def problem_fingerprints(
    category: str, view: dict[str, Any], report: dict[str, Any], input_digest: str | None
) -> dict[str, Any]:
    """Exact and fuzzy fingerprints of one verb instance; report must be normalized."""
    found = features(category, view, report)
    signature = minhash(found)
    problem = (report.get("summaries") or {}).get("problem") or ""
    return {
        "profile": PROFILE,
        "category": category,
        "template": view["template"],
        "phrasing": phrasing_hash(view),
        "input_type": view["type"],
        "exact": exact_fingerprint(category, view["type"], input_digest),
        "feature_count": len(found),
        "text_sources": [
            name
            for name, text in (("brief", _view_text(view)), ("report", problem))
            if tokens(text)
        ],
        "minhash": signature,
        "bands": bands(signature) if len(found) >= 2 else [],
    }
