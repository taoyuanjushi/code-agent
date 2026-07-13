from coding_agent.ranking import rank_files
from coding_agent.search import SearchMatch
from coding_agent.types import WorkspaceFile


def _hit(path: str, line: int = 1) -> SearchMatch:
    return SearchMatch(
        path=path,
        line=line,
        column=1,
        preview="match",
    )


def test_rank_files_scores_exact_basename_and_path_tokens() -> None:
    files = [
        WorkspaceFile(path="src/payment_service.py", size=100),
        WorkspaceFile(path="src/refund_service.py", size=100),
    ]

    ranked = rank_files(
        files,
        task="Fix src/refund_service.py refund calculation",
    )

    assert [file.path for file in ranked] == [
        "src/refund_service.py",
        "src/payment_service.py",
    ]
    assert ranked[0].score == 200
    assert ranked[0].reasons == (
        "basename mentioned in task (+100)",
        "task tokens in path: py, refund, service, src (+100)",
    )
    assert ranked[1].score == 75


def test_rank_files_search_hit_score_is_capped() -> None:
    files = [
        WorkspaceFile(path="README.md", size=100),
        WorkspaceFile(path="src/refund.py", size=100),
    ]
    hits = [_hit("src/refund.py", line) for line in range(1, 6)]

    ranked = rank_files(files, task="", search_hits=hits)

    assert [(file.path, file.score) for file in ranked] == [
        ("src/refund.py", 60),
        ("README.md", 20),
    ]
    assert ranked[0].reasons == ("5 search hits (capped +60)",)
    assert ranked[1].reasons == ("project entry point (+20)",)


def test_rank_files_promotes_test_for_search_hit_source() -> None:
    files = [
        WorkspaceFile(path="src/refund_service.py", size=100),
        WorkspaceFile(path="tests/test_refund_service.py", size=100),
        WorkspaceFile(path="tests/test_payment_service.py", size=100),
    ]

    ranked = rank_files(
        files,
        task="",
        search_hits=[_hit("src/refund_service.py")],
    )

    assert [(file.path, file.score) for file in ranked] == [
        ("tests/test_refund_service.py", 30),
        ("src/refund_service.py", 15),
        ("tests/test_payment_service.py", 0),
    ]
    assert ranked[0].reasons == ("test for search-hit source (+30)",)


def test_rank_files_recognizes_spec_style_test_names() -> None:
    files = [
        WorkspaceFile(path="src/cart-service.ts", size=100),
        WorkspaceFile(path="tests/cart-service.spec.ts", size=100),
    ]

    ranked = rank_files(
        files,
        task="",
        search_hits=[_hit("src/cart-service.ts")],
    )

    assert ranked[0].path == "tests/cart-service.spec.ts"
    assert ranked[0].score == 30


def test_rank_files_applies_deterministic_large_file_penalties() -> None:
    files = [
        WorkspaceFile(path="small.txt", size=64 * 1024 - 1),
        WorkspaceFile(path="medium.txt", size=64 * 1024),
        WorkspaceFile(path="large.txt", size=256 * 1024),
        WorkspaceFile(path="huge.txt", size=1024 * 1024),
    ]

    ranked = rank_files(files, task="")
    by_path = {file.path: file for file in ranked}

    assert by_path["small.txt"].score == 0
    assert by_path["medium.txt"].score == -10
    assert by_path["large.txt"].score == -20
    assert by_path["huge.txt"].score == -30
    assert by_path["huge.txt"].reasons == (
        "large file: 1048576 bytes (-30)",
    )


def test_rank_files_tokenizes_case_insensitively_across_separators() -> None:
    ranked = rank_files(
        [WorkspaceFile(path="src/refund-service.py", size=100)],
        task="Investigate REFUND/service",
    )

    assert ranked[0].score == 50
    assert ranked[0].reasons == (
        "task tokens in path: refund, service (+50)",
    )


def test_rank_files_uses_path_as_stable_tie_breaker() -> None:
    files = [
        WorkspaceFile(path="src/zeta.py", size=100),
        WorkspaceFile(path="src/alpha.py", size=100),
        WorkspaceFile(path="src/middle.py", size=100),
    ]

    first = rank_files(files, task="")
    second = rank_files(list(reversed(files)), task="")

    expected = ["src/alpha.py", "src/middle.py", "src/zeta.py"]
    assert [file.path for file in first] == expected
    assert [file.path for file in second] == expected


def test_rank_files_normalizes_windows_style_paths() -> None:
    ranked = rank_files(
        [WorkspaceFile(path=r"src\refund_service.py", size=100)],
        task="refund_service.py",
        search_hits=[_hit("src/refund_service.py")],
    )

    assert ranked[0].path == "src/refund_service.py"
    assert ranked[0].score == 190

def test_rank_files_detects_filename_before_sentence_punctuation() -> None:
    ranked = rank_files(
        [WorkspaceFile(path="README.md", size=100)],
        task="Please inspect README.md.",
    )

    assert ranked[0].score == 170
    assert "basename mentioned in task (+100)" in ranked[0].reasons
