"""Unified-diff parser tests: fixtures, round-trips, and a live repository."""

from __future__ import annotations

from lazysnake.git.diff import LineKind, parse_diff
from lazysnake.git.runner import Git
from lazysnake.git.status import parse_status

FIXTURE = """\
diff --git a/mod.py b/mod.py
index 1111111..2222222 100644
--- a/mod.py
+++ b/mod.py
@@ -1,4 +1,4 @@
 keep
-old line
+new line
 also keep
@@ -10,3 +10,5 @@
 ten
 eleven
+twelve
+thirteen
diff --git a/added.txt b/added.txt
new file mode 100644
index 0000000..3333333
--- /dev/null
+++ b/added.txt
@@ -0,0 +1,2 @@
+first
+second
diff --git a/gone.txt b/gone.txt
deleted file mode 100644
index 4444444..0000000
--- a/gone.txt
+++ /dev/null
@@ -1,1 +0,0 @@
-goodbye
diff --git a/moved.txt b/moved2.txt
similarity index 98%
rename from moved.txt
rename to moved2.txt
diff --git a/logo.png b/logo.png
index 5555555..6666666 100644
Binary files a/logo.png and b/logo.png differ
"""


def test_parse_modified_file() -> None:
    files = parse_diff(FIXTURE)
    mod = files[0]
    assert mod.old_path == "mod.py"
    assert mod.new_path == "mod.py"
    assert mod.path == "mod.py"
    assert len(mod.hunks) == 2
    assert mod.additions == 3
    assert mod.deletions == 1

    first = mod.hunks[0]
    assert (first.old_start, first.old_count) == (1, 4)
    assert (first.new_start, first.new_count) == (1, 4)
    kinds = [pl.kind for pl in first.lines]
    assert kinds.count(LineKind.ADDITION) == 1
    assert kinds.count(LineKind.DELETION) == 1

    added = first.lines[2]
    assert added.kind is LineKind.ADDITION
    assert added.new_no == 2
    assert added.old_no is None

    second = mod.hunks[1]
    assert (second.old_start, second.old_count) == (10, 3)
    assert (second.new_start, second.new_count) == (10, 5)


def test_parse_new_and_deleted_files() -> None:
    files = parse_diff(FIXTURE)
    added = files[1]
    assert added.is_new
    assert not added.is_deleted
    assert added.path == "added.txt"
    assert added.hunks[0].new_count == 2
    assert added.hunks[0].old_count == 0

    gone = files[2]
    assert gone.is_deleted
    assert gone.path == "gone.txt"
    assert gone.hunks[0].old_count == 1


def test_parse_rename_without_hunks() -> None:
    files = parse_diff(FIXTURE)
    moved = files[3]
    assert moved.rename_from == "moved.txt"
    assert moved.rename_to == "moved2.txt"
    assert moved.path == "moved2.txt"
    assert moved.hunks == []


def test_parse_binary() -> None:
    files = parse_diff(FIXTURE)
    assert files[4].is_binary
    assert files[4].hunks == []


def test_patch_roundtrip() -> None:
    """Rebuilt patches must parse back to the same structure."""
    files = parse_diff(FIXTURE)
    rebuilt = "\n".join(f.to_patch() for f in files if not f.is_binary and f.hunks)
    reparsed = parse_diff(rebuilt)
    assert len(reparsed) == 3  # mod.py, added.txt, gone.txt
    assert [h.raw_header for h in reparsed[0].hunks] == [h.raw_header for h in files[0].hunks]
    assert reparsed[0].additions == files[0].additions
    assert reparsed[2].deletions == files[2].deletions


def test_partial_hunk_patch() -> None:
    files = parse_diff(FIXTURE)
    mod = files[0]
    patch = mod.to_patch([mod.hunks[1]])  # only the second hunk
    reparsed = parse_diff(patch)
    assert len(reparsed[0].hunks) == 1
    assert reparsed[0].hunks[0].additions == 2


def test_parse_cap_keeps_only_complete_hunks_and_marks_truncation() -> None:
    raw = """\
diff --git a/big.txt b/big.txt
--- a/big.txt
+++ b/big.txt
@@ -1,0 +1,4 @@
+oversized-1
+oversized-2
+oversized-3
+oversized-4
@@ -20,0 +25,1 @@
+visible
"""

    files = parse_diff(raw, max_rows=3)

    assert len(files) == 1
    assert files[0].truncated
    assert [line.content for line in files[0].hunks[0].lines] == ["visible"]
    retained_rows = sum(1 + len(hunk.lines) for hunk in files[0].hunks)
    assert retained_rows <= 3
    patch = files[0].to_patch()
    assert "visible" in patch
    assert "oversized" not in patch


def test_parse_truncated_output_discards_incomplete_hunk() -> None:
    raw = """\
diff --git a/large.txt b/large.txt
--- a/large.txt
+++ b/large.txt
@@ -1,1 +1,3 @@
-old
+new-1
+new-2
"""

    files = parse_diff(raw, truncated=True)

    assert len(files) == 1
    assert files[0].truncated
    assert files[0].hunks == []


async def test_live_diff_and_apply_roundtrip(repo) -> None:
    """A rebuilt patch must be accepted by ``git apply --cached``."""
    git = Git(repo.root)
    repo.write("README.md", "initial\nmore context\nanother line\n")

    raw = await git.run("diff", "--no-color")
    files = parse_diff(raw)
    readme = next(f for f in files if f.path == "README.md")
    assert readme.additions == 2

    # Stage via our reconstructed patch; the index must then match `git add`.
    await git.run("apply", "--cached", input=readme.to_patch())
    staged = parse_status(await git.run("status", "--porcelain=v2", "--branch", "-z"))
    by_path = {f.path: f for f in staged.files}
    assert by_path["README.md"].display == "M "
    assert not by_path["README.md"].unstaged
