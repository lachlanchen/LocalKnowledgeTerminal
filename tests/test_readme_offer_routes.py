from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
README_PATHS = [ROOT / "README.md", *sorted((ROOT / "i18n").glob("README.*.md"))]
SAMPLE_ROUTE = (
    "https://lazying.art/mcp-boundary-review/sample-report/"
    "?utm_source=github&utm_medium=readme&utm_campaign=mcp_boundary_review"
    "&utm_content=lkt_readme_sample"
)
FIT_ROUTE = (
    "https://lazying.art/mcp-boundary-review/fit-check/"
    "?utm_source=github&utm_medium=readme&utm_campaign=mcp_boundary_review"
    "&utm_content=lkt_readme_fit"
)


def test_all_readme_editions_have_one_exact_mcp_review_route() -> None:
    assert len(README_PATHS) == 11
    for path in README_PATHS:
        text = path.read_text(encoding="utf-8")
        assert text.count(SAMPLE_ROUTE) == 1, path
        assert text.count(FIT_ROUTE) == 1, path
