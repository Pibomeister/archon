#!/usr/bin/env python3
"""Self-check for the determinism harness itself.

`test_node_stress.py` reports `identical=100` for every covered node. That
number is only worth anything if the harness can actually SEE a node that
changes between runs, so this file stresses synthetic bodies whose behaviour is
known and asserts the harness reaches the right verdict on each.

The four cases in `BlanketNormalizationNegativeControl` are the ones that
originally passed WRONGLY: the harness mapped every sha to `<SHA>`, every
timestamp to `<TS>`, every scratch path to `<TMP>` and every binary file to its
byte length, so a body emitting fresh random values on every run compared
identical. That control reinstates the old normalization and asserts the four
bodies still slip through it — if it ever starts failing, the new
normalization's tests above it have stopped proving anything.
"""
import os
import unittest
from unittest import mock

from nodes import runner
from nodes.runner import stress

N = 6


def nofixture(_tmp):
    return None


# --- synthetic bodies -----------------------------------------------------
# Each prints one typed line so the exit is typed, and writes at most one file.
DETERMINISTIC = 'echo "SELFCHECK=PASS"; printf "stable\\n" > "$ARTIFACTS_DIR/f.txt"'
RAND_TYPED_LINE = 'echo "SELFCHECK=PASS n=$RANDOM"'
RAND_FILE = 'echo "SELFCHECK=PASS"; echo "$RANDOM" > "$ARTIFACTS_DIR/f.txt"'
# The four that used to slip through:
RAND_TMPPATH = (
    'echo "SELFCHECK=PASS"; echo "/tmp/run-$RANDOM/x" > "$ARTIFACTS_DIR/f.txt"'
)
RAND_SHA = (
    'echo "SELFCHECK=PASS"; '
    'python3 -c "import secrets;print(secrets.token_hex(20))" > "$ARTIFACTS_DIR/f.txt"'
)
# A real clock read. Microsecond precision, so two runs in the same second
# still differ — `date` on macOS has no sub-second format and would make the
# probe depend on where the second boundary fell.
TIMESTAMP = (
    'echo "SELFCHECK=PASS"; '
    'python3 -c "import datetime;print(datetime.datetime.now().isoformat())"'
    ' > "$ARTIFACTS_DIR/f.txt"'
)
# 16 random bytes, FIXED length, guaranteed non-UTF-8 (every byte has its high
# bit set, so it can never be a valid leading byte sequence).
RAND_BINARY = (
    'echo "SELFCHECK=PASS"; '
    'python3 -c "import os,sys;sys.stdout.buffer.write('
    'bytes(0x80|(b%0x80) for b in os.urandom(16)))" > "$ARTIFACTS_DIR/f.bin"'
)

UNTYPED_ZERO = 'echo "just some prose, no discriminator"'
UNTYPED_NONZERO = 'echo "something went wrong"; exit 3'
TYPED_FAIL = 'echo "SELFCHECK=FAIL reason=x"; exit 1'

ONE_SHA_TWICE = (
    'S=$(python3 -c "import secrets;print(secrets.token_hex(20))"); '
    'echo "SELFCHECK=PASS"; echo "$S" > "$ARTIFACTS_DIR/a.txt"; '
    'echo "$S" > "$ARTIFACTS_DIR/b.txt"'
)
TWO_DIFFERENT_SHAS = (
    'echo "SELFCHECK=PASS"; '
    'python3 -c "import secrets;print(secrets.token_hex(20))" > "$ARTIFACTS_DIR/a.txt"; '
    'python3 -c "import secrets;print(secrets.token_hex(20))" > "$ARTIFACTS_DIR/b.txt"'
)


class HarnessDetectsChange(unittest.TestCase):
    """Each body is a known verdict; the harness must reach it."""

    def test_deterministic_body_is_identical(self):
        r = stress("selfcheck:deterministic", DETERMINISTIC, nofixture, n=N)
        self.assertEqual(r["identical"], N)
        self.assertEqual(r["untyped_exits"], 0)

    # --- caught by the STRUCTURE comparison ------------------------------
    def test_random_typed_line_is_not_identical(self):
        with self.assertRaisesRegex(AssertionError, "nondeterministic"):
            stress("selfcheck:rand-typed", RAND_TYPED_LINE, nofixture, n=N)

    def test_random_file_content_is_not_identical(self):
        with self.assertRaisesRegex(AssertionError, "nondeterministic"):
            stress("selfcheck:rand-file", RAND_FILE, nofixture, n=N)

    def test_random_binary_of_fixed_size_is_not_identical(self):
        """Was compared by LENGTH, so 16 random bytes matched 16 other random
        bytes. Now hashed."""
        with self.assertRaisesRegex(AssertionError, "nondeterministic"):
            stress("selfcheck:rand-binary", RAND_BINARY, nofixture, n=N)

    # --- caught by the VALUE-STABILITY comparison ------------------------
    # Ordinals alone cannot see these: one random value per run is `<X:1>` in
    # every run, so the structures match and only the raw values differ.
    def test_random_scratch_path_is_not_identical(self):
        with self.assertRaisesRegex(AssertionError, "differ between runs"):
            stress("selfcheck:rand-tmppath", RAND_TMPPATH, nofixture, n=N)

    def test_random_sha_is_not_identical(self):
        with self.assertRaisesRegex(AssertionError, "differ between runs"):
            stress("selfcheck:rand-sha", RAND_SHA, nofixture, n=N)

    def test_clock_read_is_not_identical(self):
        with self.assertRaisesRegex(AssertionError, "differ between runs"):
            stress("selfcheck:timestamp", TIMESTAMP, nofixture, n=N)

    # --- typed-exit contract ---------------------------------------------
    def test_zero_exit_without_a_pass_line_is_untyped(self):
        with self.assertRaisesRegex(AssertionError, "untyped_exit"):
            stress("selfcheck:untyped-zero", UNTYPED_ZERO, nofixture, n=N)

    def test_nonzero_exit_without_a_fail_line_is_untyped(self):
        with self.assertRaisesRegex(AssertionError, "untyped_exit"):
            stress("selfcheck:untyped-nonzero", UNTYPED_NONZERO, nofixture, n=N)

    def test_nonzero_exit_with_a_fail_line_is_typed(self):
        r = stress("selfcheck:typed-fail", TYPED_FAIL, nofixture, n=N)
        self.assertEqual(r["rc"], 1)
        self.assertEqual(r["untyped_exits"], 0)


class SlotStructure(unittest.TestCase):
    """The ordinals encode WHERE each value appeared, so a body that reuses one
    value is distinguishable from one that mints two."""

    def test_one_value_reused_occupies_one_slot(self):
        r = stress("selfcheck:one-sha-twice", ONE_SHA_TWICE, nofixture, n=N,
                   volatile=("SHA:1",))
        self.assertEqual(sorted(r["slots"]["SHA"]), [1])

    def test_two_distinct_values_occupy_two_slots(self):
        r = stress("selfcheck:two-shas", TWO_DIFFERENT_SHAS, nofixture, n=N,
                   volatile=("SHA:1", "SHA:2"))
        self.assertEqual(sorted(r["slots"]["SHA"]), [1, 2])

    def test_volatile_must_name_the_slot_that_moves(self):
        """Declaring the wrong slot does not silence the right one."""
        with self.assertRaisesRegex(AssertionError, r"SHA:2 took"):
            stress("selfcheck:two-shas-partial", TWO_DIFFERENT_SHAS, nofixture,
                   n=N, volatile=("SHA:1",))


class _BlanketNormalizer:
    """The harness's ORIGINAL normalization, kept only to prove it was blind."""

    def __init__(self, tmp):
        self.tmp = str(tmp)
        self.real = os.path.realpath(self.tmp)
        self.slots = {}          # no slots -> no value-stability check

    def __call__(self, text):
        text = text.replace(self.tmp, "<TMP>").replace(self.real, "<TMP>")
        text = runner._TMPPATH_RE.sub("<TMP>", text)
        text = runner._SHA_RE.sub("<SHA>", text)
        text = runner._TS_RE.sub("<TS>", text)
        return text


def _blanket_binary(raw):
    return f"<BINARY bytes={len(raw)}>"


class BlanketNormalizationNegativeControl(unittest.TestCase):
    """RED, kept permanently.

    Under the original normalization all four of these bodies — a fresh scratch
    path, a fresh 40-hex sha, a fresh clock read, and 16 fresh random bytes of
    the same length — reported `identical`. Each assertion below is the bug, not
    the desired behaviour; they exist so that the four tests in
    `HarnessDetectsChange` cannot silently stop discriminating.
    """

    def _blind(self, label, body):
        with mock.patch.object(runner, "Normalizer", _BlanketNormalizer), \
             mock.patch.object(runner, "_binary_digest", _blanket_binary):
            return stress(label, body, nofixture, n=N)

    def test_blanket_normalization_hid_a_random_scratch_path(self):
        self.assertEqual(self._blind("nc:tmppath", RAND_TMPPATH)["identical"], N)

    def test_blanket_normalization_hid_a_random_sha(self):
        self.assertEqual(self._blind("nc:sha", RAND_SHA)["identical"], N)

    def test_blanket_normalization_hid_a_clock_read(self):
        self.assertEqual(self._blind("nc:timestamp", TIMESTAMP)["identical"], N)

    def test_blanket_normalization_hid_random_bytes_of_equal_length(self):
        self.assertEqual(self._blind("nc:binary", RAND_BINARY)["identical"], N)


if __name__ == "__main__":
    unittest.main()
