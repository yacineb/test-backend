"""The provider mocks must stay exactly as the statement specifies.

Signatures, sleep ranges and the failure rate are load-bearing: every latency
number in docs/orchestration/ is derived from them. This exists so that a
well-meaning cleanup of steps.py fails loudly instead of silently invalidating
the measurements.

Both `ruff format` and its import sorting would break the match, which is why
app/pipeline/steps.py is excluded from each in pyproject.toml.
"""

import inspect
import pathlib
import re

from app.pipeline import steps

ROOT = pathlib.Path(__file__).resolve().parents[2]

EXPECTED_SIGNATURES = {
    "ocr": "() -> str",
    "metadata": "(text: str) -> dict",
    "chunking": "(text: str) -> list[str]",
    "external_call": (
        "(doc_id: str, ocr_text: str, meta: dict, chunks: list[str]) -> str"
    ),
}

EXPECTED_SLEEPS = {
    "ocr": "random.uniform(1, 15)",
    "metadata": "random.uniform(1, 10)",
    "chunking": "random.uniform(1, 12)",
    "external_call": "random.uniform(1, 5)",
}


def test_source_matches_the_statement_byte_for_byte():
    """The strongest form of the guarantee: diff the shipped module against
    the code block in README.md, which is the exercise statement itself."""
    statement = (ROOT / "README.md").read_text()
    expected = re.search(r"```python\n(.*?)```", statement, re.S).group(1)

    shipped = pathlib.Path(steps.__file__).read_text()
    shipped = shipped[shipped.index("import random") :]  # drop our docstring

    assert shipped.strip() == expected.strip()


def test_signatures_are_unchanged():
    for name, expected in EXPECTED_SIGNATURES.items():
        assert str(inspect.signature(getattr(steps, name))) == expected


def test_sleep_ranges_are_unchanged():
    for name, expected in EXPECTED_SLEEPS.items():
        assert f"time.sleep({expected})" in inspect.getsource(getattr(steps, name))


def test_failure_rate_is_one_in_three():
    for name in EXPECTED_SIGNATURES:
        assert "if random.random() < 1/3:" in inspect.getsource(getattr(steps, name))
