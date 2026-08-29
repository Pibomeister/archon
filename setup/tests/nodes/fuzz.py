"""Seeded corruption generator for AI-authored JSON envelopes (deslop-review.json,
critique.json, revision.json). Takes one valid seed object and produces
deterministic, named byte-level mutations that a bash gate must either accept
(if still schema-valid) or reject with a typed line — never with an untyped
crash. See test_gate_fuzz.py for how these are run against the real gate
bodies.

variants(seed_obj, n, seed=0) is the only entry point. It is deterministic:
same seed_obj/n/seed always produces the same list, in the same order, which
is what makes a red variant reproducible as a named regression test.
"""
import copy
import json
import math
import random
from typing import Any, Optional


def _dumps(obj: Any) -> bytes:
    # allow_nan=True (the default) is deliberate: it is how the NaN/Infinity
    # mutations below get a literal (non-quoted) token into the JSON text.
    return json.dumps(obj, ensure_ascii=False, allow_nan=True).encode("utf-8")


def _containers_with_key(obj: Any, predicate) -> list:
    """Depth-first walk collecting every (container, key) pair — dict item or
    list index — whose value satisfies predicate(value). Used to find, e.g.,
    every 'confidence' field or every string field anywhere in the doc."""
    found = []

    def rec(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if predicate(k, v):
                    found.append((o, k))
                rec(v)
        elif isinstance(o, list):
            for i, v in enumerate(o):
                if predicate(i, v):
                    found.append((o, i))
                rec(v)

    rec(obj)
    return found


def _pick(rng: random.Random, items: list):
    return items[rng.randrange(len(items))] if items else None


# ---------------------------------------------------------------------------
# Individual mutations. Each takes (obj, rng) and returns (name, bytes) or
# None if this mutation kind does not apply to this particular seed shape
# (e.g. no "confidence" field to corrupt). variants() below skips a None and
# tries the next kind, so every call to variants() still yields exactly n.
# ---------------------------------------------------------------------------

def _mut_confidence(obj, rng, label, value):
    hits = _containers_with_key(obj, lambda k, v: k == "confidence")
    if not hits:
        return None
    doc = copy.deepcopy(obj)
    hits2 = _containers_with_key(doc, lambda k, v: k == "confidence")
    container, key = _pick(rng, hits2)
    container[key] = value
    return f"confidence_{label}", _dumps(doc)


def mut_confidence_string(obj, rng):
    return _mut_confidence(obj, rng, "string", "high")


def mut_confidence_bool(obj, rng):
    return _mut_confidence(obj, rng, "bool", True)


def mut_confidence_null(obj, rng):
    return _mut_confidence(obj, rng, "null", None)


def mut_confidence_negative(obj, rng):
    return _mut_confidence(obj, rng, "negative", -50)


def mut_confidence_over_max(obj, rng):
    # The real enum is {50, 75, 100}; "over 1" in the fuzz brief maps here to
    # "far outside the enum's domain" since this codebase's confidences are
    # not 0..1 floats.
    return _mut_confidence(obj, rng, "over_max", 1000000)


def mut_extra_key(obj, rng):
    if not isinstance(obj, dict):
        return None
    doc = copy.deepcopy(obj)
    doc[f"zz_unexpected_{rng.randrange(10**6)}"] = "unexpected"
    return "extra_key", _dumps(doc)


def mut_unicode_emoji_string(obj, rng):
    hits = _containers_with_key(obj, lambda k, v: isinstance(v, str))
    if not hits:
        return None
    doc = copy.deepcopy(obj)
    hits2 = _containers_with_key(doc, lambda k, v: isinstance(v, str))
    container, key = _pick(rng, hits2)
    container[key] = "cafe \U0001F600 test \U0001F4A9 \u200b"
    return "unicode_emoji_string", _dumps(doc)


def mut_empty_array(obj, rng):
    hits = _containers_with_key(obj, lambda k, v: isinstance(v, list))
    if not hits:
        return None
    doc = copy.deepcopy(obj)
    hits2 = _containers_with_key(doc, lambda k, v: isinstance(v, list))
    container, key = _pick(rng, hits2)
    container[key] = []
    return "empty_array", _dumps(doc)


def mut_missing_required_key(obj, rng):
    if not isinstance(obj, dict) or not obj:
        return None
    doc = copy.deepcopy(obj)
    key = _pick(rng, sorted(doc.keys()))
    del doc[key]
    return f"missing_key_{key}", _dumps(doc)


def mut_wrong_type_list_for_dict(obj, rng):
    hits = _containers_with_key(obj, lambda k, v: isinstance(v, dict))
    if not hits:
        return None
    doc = copy.deepcopy(obj)
    hits2 = _containers_with_key(doc, lambda k, v: isinstance(v, dict))
    container, key = _pick(rng, hits2)
    container[key] = ["not", "a", "dict"]
    return "wrong_type_list_for_dict", _dumps(doc)


def mut_wrong_type_dict_for_list(obj, rng):
    hits = _containers_with_key(obj, lambda k, v: isinstance(v, list))
    if not hits:
        return None
    doc = copy.deepcopy(obj)
    hits2 = _containers_with_key(doc, lambda k, v: isinstance(v, list))
    container, key = _pick(rng, hits2)
    container[key] = {"not": "a list"}
    return "wrong_type_dict_for_list", _dumps(doc)


def mut_truncated_file(obj, rng):
    raw = _dumps(obj)
    if len(raw) < 2:
        return None
    k = rng.randrange(1, len(raw))
    return f"truncated_at_{k}", raw[:k]


def mut_utf8_bom(obj, rng):
    return "utf8_bom_prefix", b"\xef\xbb\xbf" + _dumps(obj)


def mut_trailing_garbage(obj, rng):
    return "trailing_garbage", _dumps(obj) + b"\ngarbage not json {{{ trailing bytes \xff"


def mut_duplicate_key(obj, rng):
    if not isinstance(obj, dict) or not obj:
        return None
    key = _pick(rng, sorted(obj.keys()))
    val = obj[key]
    # A bogus value of a different shape than the real one, so a parser that
    # (wrongly) took the FIRST occurrence instead of the last would diverge
    # visibly from a parser that took the last (Python's json.load always
    # takes the last — this mutation proves the gate tolerates that).
    bogus = 999999 if not isinstance(val, int) else "sneaky-duplicate"
    dup_prefix = json.dumps({key: bogus}, ensure_ascii=False)[1:-1]
    rest = json.dumps(obj, ensure_ascii=False)[1:]
    text = "{" + dup_prefix + ", " + rest
    return f"duplicate_key_{key}", text.encode("utf-8")


def mut_nested_50_deep(obj, rng):
    if not isinstance(obj, dict) or not obj:
        return None
    doc = copy.deepcopy(obj)
    key = _pick(rng, sorted(doc.keys()))
    nested: Any = "bottom"
    for _ in range(50):
        nested = [nested]
    doc[key] = nested
    return f"nested_50_deep_{key}", _dumps(doc)


def mut_giant_string_1mb(obj, rng):
    hits = _containers_with_key(obj, lambda k, v: isinstance(v, str))
    if not hits:
        return None
    doc = copy.deepcopy(obj)
    hits2 = _containers_with_key(doc, lambda k, v: isinstance(v, str))
    container, key = _pick(rng, hits2)
    container[key] = "A" * 1_000_000
    return "giant_string_1mb", _dumps(doc)


def mut_integer_overflow(obj, rng):
    hits = _containers_with_key(obj, lambda k, v: isinstance(v, int) and not isinstance(v, bool))
    doc = copy.deepcopy(obj)
    if hits:
        hits2 = _containers_with_key(doc, lambda k, v: isinstance(v, int) and not isinstance(v, bool))
        container, key = _pick(rng, hits2)
        container[key] = 10**300
    elif isinstance(doc, dict):
        doc["zz_overflow"] = 10**300
    else:
        return None
    return "integer_overflow", _dumps(doc)


def mut_nan_literal(obj, rng):
    hits = _containers_with_key(obj, lambda k, v: isinstance(v, (int, float)) and not isinstance(v, bool))
    doc = copy.deepcopy(obj)
    if hits:
        hits2 = _containers_with_key(doc, lambda k, v: isinstance(v, (int, float)) and not isinstance(v, bool))
        container, key = _pick(rng, hits2)
        container[key] = math.nan
    elif isinstance(doc, dict):
        doc["zz_nan"] = math.nan
    else:
        return None
    return "nan_literal", _dumps(doc)


def mut_infinity_literal(obj, rng):
    hits = _containers_with_key(obj, lambda k, v: isinstance(v, (int, float)) and not isinstance(v, bool))
    doc = copy.deepcopy(obj)
    sign = rng.choice([1, -1])
    val = math.inf * sign
    if hits:
        hits2 = _containers_with_key(doc, lambda k, v: isinstance(v, (int, float)) and not isinstance(v, bool))
        container, key = _pick(rng, hits2)
        container[key] = val
    elif isinstance(doc, dict):
        doc["zz_infinity"] = val
    else:
        return None
    return "infinity_literal", _dumps(doc)


def mut_plain_text_non_json(obj, rng):
    return "plain_text_non_json", b"this is not json at all\njust a plain text failure case\n"


MUTATIONS = [
    mut_confidence_string,
    mut_confidence_bool,
    mut_confidence_null,
    mut_confidence_negative,
    mut_confidence_over_max,
    mut_extra_key,
    mut_unicode_emoji_string,
    mut_empty_array,
    mut_missing_required_key,
    mut_wrong_type_list_for_dict,
    mut_wrong_type_dict_for_list,
    mut_truncated_file,
    mut_utf8_bom,
    mut_trailing_garbage,
    mut_duplicate_key,
    mut_nested_50_deep,
    mut_giant_string_1mb,
    mut_integer_overflow,
    mut_nan_literal,
    mut_infinity_literal,
    mut_plain_text_non_json,
]


def variants(seed_obj: Any, n: int, seed: int = 0) -> list:
    """Return exactly n deterministic (name, content_bytes) variants of
    seed_obj. Cycles through MUTATIONS in order; a mutation kind that does
    not apply to this seed's shape (e.g. no list field to empty) is skipped
    in favor of the next kind, so every call still yields n variants as long
    as at least one kind (plain_text_non_json etc. always apply) succeeds.
    Deterministic in (seed_obj, n, seed): same inputs, same output, in the
    same order — required so a red variant can be pinned as a named test."""
    out = []
    i = 0
    guard = 0
    max_guard = n * len(MUTATIONS) * 4 + 100
    while len(out) < n:
        guard += 1
        if guard > max_guard:
            raise RuntimeError(
                f"variants(): could not produce {n} variants for this seed shape "
                f"after {guard} attempts (got {len(out)})"
            )
        kind_idx = i % len(MUTATIONS)
        mutation = MUTATIONS[kind_idx]
        rng = random.Random(f"{seed}:{i}")
        result = mutation(seed_obj, rng)
        i += 1
        if result is None:
            continue
        name, content = result
        out.append({"name": f"{name}#{len(out)}", "content": content})
    return out
