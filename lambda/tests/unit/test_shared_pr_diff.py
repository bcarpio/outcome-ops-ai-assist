"""Unit tests for shared.pr_diff — client-side unified-diff generator.

The retrieval-heavy PR-check handlers depend on this producing diffs
in a shape compatible with GitHub's server-side diff output so their
section-extractors work uniformly across providers.
"""

import pytest

from shared import pr_diff


class TestComputeFileDiff:

    def test_added_file_marks_dev_null_as_base(self):
        result = pr_diff.compute_file_diff(
            base_content=None,
            head_content=b"line one\nline two\n",
            filename="src/new.py",
        )
        assert "diff --git a/src/new.py b/src/new.py" in result
        assert "--- /dev/null" in result
        assert "+++ b/src/new.py" in result
        assert "+line one" in result
        assert "+line two" in result

    def test_deleted_file_marks_dev_null_as_head(self):
        result = pr_diff.compute_file_diff(
            base_content=b"gone content\n",
            head_content=None,
            filename="src/removed.py",
        )
        assert "diff --git a/src/removed.py b/src/removed.py" in result
        assert "--- a/src/removed.py" in result
        assert "+++ /dev/null" in result
        assert "-gone content" in result

    def test_modified_file_shows_both_sides(self):
        result = pr_diff.compute_file_diff(
            base_content=b"line one\nline two\nline three\n",
            head_content=b"line one\nline TWO\nline three\n",
            filename="src/mod.py",
        )
        assert "diff --git a/src/mod.py b/src/mod.py" in result
        assert "--- a/src/mod.py" in result
        assert "+++ b/src/mod.py" in result
        assert "-line two" in result
        assert "+line TWO" in result

    def test_unchanged_content_returns_empty(self):
        # File was in changed_files but content matches -- return
        # empty so caller can skip cleanly.
        result = pr_diff.compute_file_diff(
            base_content=b"identical\n",
            head_content=b"identical\n",
            filename="src/same.py",
        )
        assert result == ""

    def test_both_none_returns_empty_shaped_header(self):
        # Defensive: if the caller couldn't fetch either side, we still
        # produce a header so downstream section-extractors don't crash.
        result = pr_diff.compute_file_diff(
            base_content=None,
            head_content=None,
            filename="src/mystery.py",
        )
        assert "diff --git a/src/mystery.py b/src/mystery.py" in result
        assert "no content on either side" in result

    def test_binary_content_produces_stub(self):
        # A NUL byte in the first 1KB triggers the binary stub.
        result = pr_diff.compute_file_diff(
            base_content=b"\x00\x01\x02lots more binary bytes",
            head_content=b"\x00\x01\x02\x03different binary",
            filename="assets/logo.png",
        )
        assert "diff --git a/assets/logo.png b/assets/logo.png" in result
        assert "(binary file changed)" in result

    def test_binary_head_only_still_stubs(self):
        result = pr_diff.compute_file_diff(
            base_content=b"text before",
            head_content=b"\x00binary added",
            filename="x",
        )
        assert "(binary file changed)" in result

    def test_utf8_replace_tolerates_bad_bytes(self):
        # A single invalid UTF-8 byte should not crash the diff.
        result = pr_diff.compute_file_diff(
            base_content=b"good bytes\n",
            head_content=b"good\xffbytes\n",  # invalid byte, no NUL
            filename="src/utf8.py",
        )
        assert result != ""
        # We produced a diff without raising.
        assert "diff --git" in result

    def test_context_lines_parameter_respected(self):
        # Make a change surrounded by lots of unchanged lines to
        # confirm context_lines controls output size.
        base = b"".join(f"line {i}\n".encode() for i in range(20))
        head = b"".join(
            (f"line {i}\n" if i != 10 else "line CHANGED\n").encode()
            for i in range(20)
        )
        result_3 = pr_diff.compute_file_diff(base, head, "f", context_lines=3)
        result_1 = pr_diff.compute_file_diff(base, head, "f", context_lines=1)
        # The wider-context output should be at least a couple lines
        # longer than the narrower one.
        assert result_3.count("\n") > result_1.count("\n")

    def test_filename_appears_in_header_for_all_shapes(self):
        # Added
        r1 = pr_diff.compute_file_diff(None, b"x", "path/a.py")
        assert "a/path/a.py" in r1 and "b/path/a.py" in r1
        # Deleted
        r2 = pr_diff.compute_file_diff(b"x", None, "path/b.py")
        assert "a/path/b.py" in r2 and "b/path/b.py" in r2
        # Modified
        r3 = pr_diff.compute_file_diff(b"one\n", b"two\n", "path/c.py")
        assert "a/path/c.py" in r3 and "b/path/c.py" in r3

    def test_unicode_content_round_trips(self):
        result = pr_diff.compute_file_diff(
            base_content="héllo\nwörld\n".encode("utf-8"),
            head_content="héllo\nWÖRLD\n".encode("utf-8"),
            filename="src/i18n.py",
        )
        assert "-wörld" in result
        assert "+WÖRLD" in result

    def test_result_ends_with_newline_when_body_present(self):
        result = pr_diff.compute_file_diff(
            base_content=b"a\n",
            head_content=b"b\n",
            filename="f",
        )
        assert result.endswith("\n")
