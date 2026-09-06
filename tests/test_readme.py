"""The README's examples are run, not read.

An example that cannot even be constructed is worse than no example: the reader
copies it, gets an exception out of a line the documentation presented as
working, and has no way to tell a mistake of ours from one of theirs.

That is not hypothetical. While the SDK inferred "a domain of your own" from a
single default host, the README showed

    FourPay(bearer_token=token, base_url="https://pay.partner.example")

as the way a partner integrates. It constructed — and then sent every call with
no organization at all, because nothing in front of that host pins the header.
Once the SDK started asking for the claim out loud, the same snippet stopped
constructing.

So every ``FourPay(...)`` and ``FourPay.for_login(...)`` in the README is built
here; a snippet that raises fails this test.
"""

import ast
from pathlib import Path

import pytest

from fourpay import FourPay

README = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")

# Values for the names the snippets read from the environment or from an
# earlier line of the same example.
BINDINGS = {
    "os.environ['FOURPAY_API_KEY']": "key-1",
    "os.environ['FOURPAY_ORGANIZATION_ID']": "org-1",
    "session['token']": "token-1",
    "token": "token-1",
    "org_id": "org-1",
}


def _source_of_call(text: str, start: int) -> str:
    """The call starting at ``start``, up to its balanced closing parenthesis.

    Balanced parentheses rather than a lazy pattern: the arguments span several
    lines and carry nested calls, and a ``[^)]*`` would stop at the first inner
    parenthesis and quietly test half a snippet.
    """
    i = text.index("(", start)
    depth = 0

    for j in range(i, len(text)):
        if text[j] == "(":
            depth += 1
        elif text[j] == ")":
            depth -= 1
            if depth == 0:
                return text[start : j + 1]

    raise AssertionError(f"unbalanced call in README at offset {start}")


def _calls():
    """Every FourPay construction the README shows, as parsed AST calls."""
    found = []

    for marker in ("FourPay(", "FourPay.for_login("):
        start = 0
        while (start := README.find(marker, start)) != -1:
            # `FourPay(` also matches inside `FourPay.for_login(` — skip it there,
            # the longer marker collects that one with its right constructor
            if marker == "FourPay(" and README[start:].startswith("FourPay.for_login"):
                start += 1
                continue

            source = _source_of_call(README, start)
            found.append((source, ast.parse(source, mode="eval").body))
            start += 1

    return found


def _value(node: ast.AST):
    """A snippet argument as a Python value, with its free names bound."""
    try:
        return ast.literal_eval(node)
    except ValueError:
        pass

    key = ast.unparse(node)
    assert key in BINDINGS, f"README example uses an unbound name: {key}"
    return BINDINGS[key]


CALLS = _calls()


def test_the_readme_still_shows_how_the_client_is_built():
    # The README is the install-and-go page: if it stops showing the client
    # being built, the check below has nothing to check and must say so
    assert len(CALLS) >= 3, f"README shows only {len(CALLS)} client constructions"


@pytest.mark.parametrize("source,call", CALLS, ids=lambda v: None)
def test_every_fourpay_in_the_readme_actually_constructs(source, call):
    kwargs = {kw.arg: _value(kw.value) for kw in call.keywords}
    build = FourPay.for_login if source.startswith("FourPay.for_login") else FourPay

    try:
        build(**kwargs)
    except ValueError as exc:  # pragma: no cover - the failure message is the point
        pytest.fail(f"README snippet does not construct:\n{source}\n\n{exc}")
