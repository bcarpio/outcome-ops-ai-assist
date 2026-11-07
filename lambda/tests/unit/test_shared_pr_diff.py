"""Unit tests for shared.pr_diff — client-side unified-diff generator.

The retrieval-heavy PR-check handlers depend on this producing diffs
in a shape compatible with GitHub's server-side diff output so their
section-extractors work uniformly across providers.
"""

from unittest.mock import patch

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
        # Load-bearing: downstream is_new_file() detection in
        # license_compliance.py greps for this marker.
        assert "new file mode 100644" in result
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
        assert "deleted file mode 100644" in result
        assert "--- a/src/removed.py" in result
        assert "+++ /dev/null" in result
        assert "-gone content" in result

    def test_modified_file_omits_mode_lines(self):
        # Regular modification (both base + head present) does NOT emit
        # new/deleted file mode lines -- matches how GitHub's server-side
        # diff formats these.
        result = pr_diff.compute_file_diff(
            base_content=b"a\n",
            head_content=b"b\n",
            filename="src/mod.py",
        )
        assert "new file mode" not in result
        assert "deleted file mode" not in result

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


class TestAddedLineNumbers:
    """``added_line_numbers`` -- 1-based RIGHT-side line numbers of added lines.

    Anchors inline findings (ADR-031): only real changed lines, never guessed.
    """

    def test_multi_hunk_returns_right_side_numbers(self):
        # Two hunks; RIGHT-side counter restarts from each @@ +c,d header.
        diff = "\n".join([
            "diff --git a/f b/f",
            "--- a/f",
            "+++ b/f",
            "@@ -1,3 +1,4 @@",
            " context1",     # new_line 1 -> 2
            "+added line 2",  # added at 2 -> 3
            " context3",      # -> 4
            " context4",      # -> 5
            "@@ -10,2 +11,3 @@",
            " context11",     # new_line 11 -> 12
            "+added line 12",  # added at 12 -> 13
            "+added line 13",  # added at 13 -> 14
        ])
        assert pr_diff.added_line_numbers(diff) == [2, 12, 13]

    def test_removed_lines_do_not_advance_right_side(self):
        diff = "\n".join([
            "@@ -1,4 +1,3 @@",
            " ctx1",       # 1 -> 2
            "-removed",     # no advance on RIGHT
            "+added at 2",  # added at 2 -> 3
            " ctx3",        # -> 4
        ])
        assert pr_diff.added_line_numbers(diff) == [2]

    def test_file_header_and_in_hunk_triple_markers_skipped(self):
        # The ``+++`` / ``---`` continue branch: a would-be added line whose
        # content itself starts with ``++``/``--`` renders with a 3-char
        # marker and must NOT be counted or advance the counter.
        diff = "\n".join([
            "@@ -1,2 +1,3 @@",
            " ctx",            # 1 -> 2
            "--- not a real removed line",
            "+++ not a real added line",
            "+real add",       # added at 2
        ])
        assert pr_diff.added_line_numbers(diff) == [2]

    def test_blank_line_in_body_does_not_crash(self):
        diff = "\n".join([
            "@@ -1,1 +1,2 @@",
            " ctx",        # 1 -> 2
            "",             # blank / unparseable -> ignored, no advance
            "+added",       # added at 2
        ])
        assert pr_diff.added_line_numbers(diff) == [2]

    def test_malformed_hunk_header_is_skipped_no_crash(self):
        # A header with no parseable ``+c`` turns the hunk off, so lines
        # under it are not counted. A later well-formed header resumes.
        diff = "\n".join([
            "@@ this is not a valid header @@",
            "+should not count (no active hunk)",
            "@@ -1 +3 @@",
            "+counts at 3",
        ])
        assert pr_diff.added_line_numbers(diff) == [3]

    def test_no_added_lines_returns_empty(self):
        diff = "\n".join([
            "@@ -1,2 +1,1 @@",
            " ctx",
            "-removed",
        ])
        assert pr_diff.added_line_numbers(diff) == []

    def test_empty_string_returns_empty(self):
        assert pr_diff.added_line_numbers("") == []


class TestAddedLinesByFile:
    """``added_lines_by_file`` -- {path: ALL added RIGHT-side lines} (full set).

    Full set (not just the first line) is what lets a check anchor multiple
    findings in one file on distinct lines. ``fetch_file_at_sha`` is mocked so
    ``compute_file_diff`` runs for real.
    """

    def _patch_fetch(self, side_effect):
        return patch("shared.source_fetch.fetch_file_at_sha", side_effect=side_effect)

    def test_collects_all_added_lines_in_order(self):
        def fake(provider, path, sha, **kwargs):
            if sha == "base":
                return b"a\nb\nc\n"
            return b"a\nNEW1\nb\nc\nNEW2\n"  # adds at RIGHT lines 2 and 5
        with self._patch_fetch(fake):
            out = pr_diff.added_lines_by_file(
                "github", "owner/repo", "base", "head", ["f.py"],
                github_token="ghp")
        assert out == {"f.py": [2, 5]}

    def test_no_added_lines_omitted(self):
        def fake(provider, path, sha, **kwargs):
            return b"identical\n"
        with self._patch_fetch(fake):
            out = pr_diff.added_lines_by_file(
                "github", "owner/repo", "base", "head", ["same.py"],
                github_token="ghp")
        assert out == {}

    def test_first_added_lines_delegates_to_full_set(self):
        # first_added_lines is a thin convenience: first element of each list.
        def fake(provider, path, sha, **kwargs):
            if sha == "base":
                return b"a\nb\n"
            return b"a\nNEW1\nb\nNEW2\n"  # adds at 2 and 4
        with self._patch_fetch(fake):
            full = pr_diff.added_lines_by_file(
                "github", "owner/repo", "base", "head", ["f.py"], github_token="ghp")
            first = pr_diff.first_added_lines(
                "github", "owner/repo", "base", "head", ["f.py"], github_token="ghp")
        assert full == {"f.py": [2, 4]}
        assert first == {"f.py": 2}


class TestFirstAddedLines:
    """``first_added_lines`` -- {path: first added RIGHT-side line} across providers.

    ``shared.source_fetch.fetch_file_at_sha`` is mocked so ``compute_file_diff``
    runs for real against synthetic base/head bytes.
    """

    def _patch_fetch(self, side_effect):
        return patch("shared.source_fetch.fetch_file_at_sha", side_effect=side_effect)

    def test_returns_first_added_line_per_file(self):
        def fake(provider, path, sha, **kwargs):
            if sha == "base":
                return b"line1\nline2\n"
            return b"line1\nline2\nline3\n"  # line3 added at RIGHT line 3
        with self._patch_fetch(fake):
            out = pr_diff.first_added_lines(
                "github", "owner/repo", "base", "head", ["f.py"],
                github_token="ghp")
        assert out == {"f.py": 3}

    def test_file_with_no_added_lines_is_omitted(self):
        def fake(provider, path, sha, **kwargs):
            return b"identical\n"  # base == head -> empty diff -> no added lines
        with self._patch_fetch(fake):
            out = pr_diff.first_added_lines(
                "github", "owner/repo", "base", "head", ["same.py"],
                github_token="ghp")
        assert out == {}

    def test_base_fetch_error_treated_as_added_file(self):
        from shared.source_fetch import SourceFetchError

        def fake(provider, path, sha, **kwargs):
            if sha == "base":
                raise SourceFetchError("absent at base")
            return b"new line 1\nnew line 2\n"  # added file -> first add at 1
        with self._patch_fetch(fake):
            out = pr_diff.first_added_lines(
                "github", "owner/repo", "base", "head", ["new.py"],
                github_token="ghp")
        assert out == {"new.py": 1}

    def test_head_fetch_error_treated_as_deletion_omitted(self):
        from shared.source_fetch import SourceFetchError

        def fake(provider, path, sha, **kwargs):
            if sha == "head":
                raise SourceFetchError("deleted in head")
            return b"old content\n"  # deletion -> no added lines -> omitted
        with self._patch_fetch(fake):
            out = pr_diff.first_added_lines(
                "github", "owner/repo", "base", "head", ["gone.py"],
                github_token="ghp")
        assert out == {}

    def test_non_sourcefetch_exception_skips_file_not_raised(self):
        # A generic (non-SourceFetchError) failure is logged and the file
        # skipped -- never propagated out of first_added_lines.
        def fake(provider, path, sha, **kwargs):
            raise ValueError("unexpected parse failure")
        with self._patch_fetch(fake):
            out = pr_diff.first_added_lines(
                "github", "owner/repo", "base", "head", ["f.py"],
                github_token="ghp")
        assert out == {}

    def test_empty_changed_files_returns_empty(self):
        with patch("shared.source_fetch.fetch_file_at_sha") as mock_fetch:
            out = pr_diff.first_added_lines(
                "github", "owner/repo", "base", "head", [], github_token="ghp")
        assert out == {}
        mock_fetch.assert_not_called()

    def test_unknown_provider_returns_empty(self):
        # No fetch should even be attempted.
        with patch("shared.source_fetch.fetch_file_at_sha") as mock_fetch:
            out = pr_diff.first_added_lines(
                "bitbucket", "owner/repo", "base", "head", ["f.py"])
        assert out == {}
        mock_fetch.assert_not_called()

    def test_github_fetch_kwargs(self):
        captured = []

        def fake(provider, path, sha, **kwargs):
            captured.append((provider, kwargs))
            return b"same\n"
        with self._patch_fetch(fake):
            pr_diff.first_added_lines(
                "github", "owner/repo", "base", "head", ["f.py"],
                github_token="ghp")
        assert captured, "fetch should have been called"
        for provider, kwargs in captured:
            assert provider == "github"
            assert kwargs == {"repository": "owner/repo", "github_token": "ghp"}

    def test_gitlab_fetch_kwargs(self):
        captured = []

        def fake(provider, path, sha, **kwargs):
            captured.append((provider, kwargs))
            return b"same\n"
        with self._patch_fetch(fake):
            pr_diff.first_added_lines(
                "gitlab", "group/sub/project", "base", "head", ["f.py"],
                gitlab_access_token="glpat", gitlab_base_url="https://gl.example")
        for provider, kwargs in captured:
            assert provider == "gitlab"
            assert kwargs == {
                "gitlab_project_path": "group/sub/project",
                "gitlab_access_token": "glpat",
                "gitlab_base_url": "https://gl.example",
            }

    def test_azure_devops_fetch_kwargs(self):
        captured = []

        def fake(provider, path, sha, **kwargs):
            captured.append((provider, kwargs))
            return b"same\n"
        with self._patch_fetch(fake):
            pr_diff.first_added_lines(
                "azure_devops", "repo", "base", "head", ["f.py"],
                ado_org="org", ado_project="proj", ado_repo="repo",
                ado_access_token="adotok")
        for provider, kwargs in captured:
            assert provider == "azure_devops"
            assert kwargs == {
                "ado_org": "org", "ado_project": "proj",
                "ado_repo": "repo", "ado_access_token": "adotok",
            }
