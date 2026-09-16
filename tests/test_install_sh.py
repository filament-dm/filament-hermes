"""Guards for ``install.sh`` — specifically for bash 3.2 (macOS ``/bin/bash``).

The documented install is ``curl ... | bash``, so on macOS the installer runs
under bash 3.2.57. A here-doc nested inside a ``<(...)``/``$(...)`` breaks
there and nowhere else: 3.2 rescans the substitution's raw text at expansion
time to find the closing paren and does not skip ``#`` comments, so an
apostrophe in a comment reads as an opening quote. ``bash -n`` never expands,
so it passes; the failure is non-fatal, so the install continues on the
hardcoded fallback dependency list. Nothing about it is visible on bash 5.
"""

import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = ROOT / "install.sh"
DEP_READ_SH = ROOT / "tests" / "install-dep-read.sh"

_HEREDOC_OPEN = re.compile(r"<<-?\s*[\"']?[A-Za-z_][A-Za-z0-9_]*")
_SUBST_OPEN = re.compile(r"[$<>]\(")


def _declared_dependencies() -> list[str]:
    """``[project].dependencies`` from pyproject.toml, in declaration order.

    Regex rather than tomllib, matching ``test_directory_plugin.py`` — tomllib
    is 3.11+ and the project declares ``requires-python = ">=3.9"``.
    """
    text = (ROOT / "pyproject.toml").read_text()
    block = re.search(r"^dependencies\s*=\s*\[(.*?)^\]", text, re.M | re.S)
    assert block, "no [project].dependencies in pyproject.toml"
    return re.findall(r"[\"\']([^\"\']+)[\"\']", block.group(1))


def _substitution_depth(prefix: str, depth: int) -> int:
    """Running count of open ``$(``/``<(``/``>(`` substitutions, clamped at 0.

    Clamped because unmatched ``)`` also ends a ``case`` pattern and a function
    definition; both are common and neither opens anything.
    """
    for token in re.findall(r"[$<>]\(|\(|\)", prefix):
        if _SUBST_OPEN.fullmatch(token):
            depth += 1
        elif token == ")":
            depth = max(0, depth - 1)
    return depth


def _heredocs_opened_inside_substitutions(script: str) -> list[tuple[int, str]]:
    offenders = []
    depth = 0
    terminator = None
    for lineno, line in enumerate(script.splitlines(), start=1):
        if terminator is not None:
            if line.strip() == terminator:
                terminator = None
            continue
        opener = _HEREDOC_OPEN.search(line)
        if opener is None:
            depth = _substitution_depth(line, depth)
            continue
        if _substitution_depth(line[: opener.start()], depth) > 0:
            offenders.append((lineno, line.strip()))
        terminator = opener.group(0).split("<<")[1].strip("- \"'")
        depth = _substitution_depth(line, depth)
    return offenders


def test_no_heredoc_opens_inside_a_substitution():
    # The bash 3.2 landmine. Redirect the here-doc to a temp file and read that
    # back instead; see the dep-read block in install.sh.
    assert _heredocs_opened_inside_substitutions(INSTALL_SH.read_text()) == []


def test_the_scanner_would_catch_the_shape_it_guards_against():
    # Otherwise a scanner that silently matches nothing still passes forever.
    broken = "\n".join(
        [
            "while IFS= read -r x; do :; done < <(python3 - <<'PYEOF'",
            "print(1)",
            "PYEOF",
            ")",
        ]
    )
    assert _heredocs_opened_inside_substitutions(broken) == [
        (1, "while IFS= read -r x; do :; done < <(python3 - <<'PYEOF'")
    ]


def test_install_sh_parses():
    # Necessary but famously not sufficient — -n passes on the broken shape.
    bash = shutil.which("bash")
    assert bash is not None
    subprocess.run([bash, "-n", str(INSTALL_SH)], check=True)


def test_dep_read_block_yields_every_declared_dependency():
    # The regression this is all about was silent: the block read nothing and
    # the installer used its fallback list. Pin the block's real output to
    # pyproject.toml, which is what it claims to be the single source of truth
    # for. On macOS run the same harness under /bin/bash to cover 3.2.
    bash = shutil.which("bash")
    assert bash is not None
    out = subprocess.run(
        [bash, str(DEP_READ_SH)],
        check=True,
        capture_output=True,
        text=True,
        env={"PATH": str(Path(sys.executable).parent) + ":/usr/bin:/bin"},
    )
    assert out.stdout.splitlines() == _declared_dependencies()
