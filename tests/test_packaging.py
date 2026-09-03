"""Tests guarding the declared dependencies against machine-local paths.

``pyproject.toml`` once wired the ``odoo`` extra to ``../oerpc`` through
``[tool.uv.sources]``. That resolves only on a machine that happens to have
the checkout as a sibling directory: a fresh clone fails with
``Distribution not found``, and ``pip`` — which ignores ``[tool.uv.sources]``
altogether — looks for an ``oerpc`` that is not on PyPI.

Nothing in the test suite noticed, because the suite imports ``wgman``, never
the packaging metadata. These tests read ``pyproject.toml`` itself.
"""

import pathlib
import sys

import pytest

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - python 3.10 fallback
    tomllib = pytest.importorskip("tomli")


PYPROJECT = pathlib.Path(__file__).resolve().parent.parent / "pyproject.toml"


@pytest.fixture(scope="module")
def pyproject():
    with PYPROJECT.open("rb") as f:
        return tomllib.load(f)


def _all_requirements(pyproject):
    """Every requirement string, from both base and optional dependencies."""
    reqs = list(pyproject["project"].get("dependencies", []))
    for extra in pyproject["project"].get("optional-dependencies", {}).values():
        reqs.extend(extra)
    return reqs


class TestNoLocalPaths:
    def test_no_uv_sources_section(self, pyproject):
        """``[tool.uv.sources]`` is uv-only and was used to point at ``../oerpc``.

        ``pip`` ignores the section entirely, so a dependency that relies on it
        is invisible to half the installers in use.
        """
        assert "sources" not in pyproject.get("tool", {}).get("uv", {})

    @pytest.mark.parametrize("marker", ["../", "file://", " @ ./", " @ /"])
    def test_no_filesystem_reference(self, pyproject, marker):
        for req in _all_requirements(pyproject):
            assert marker not in req, f"{req!r} refers to a local filesystem path"


class TestOerpcIsReachable:
    def test_declared_as_direct_reference(self, pyproject):
        """The ``odoo`` extra must name a fetchable oerpc.

        ``oerpc`` is not on PyPI, so a bare ``"oerpc"`` requirement resolves
        nowhere; it needs a PEP 508 direct reference to the public repository.
        """
        odoo = pyproject["project"]["optional-dependencies"]["odoo"]
        oerpc = [r for r in odoo if r == "oerpc" or r.startswith("oerpc ")]
        assert oerpc, "the odoo extra no longer declares oerpc"
        assert oerpc[0].startswith("oerpc @ git+https://"), (
            f"{oerpc[0]!r} must be a git+https direct reference: oerpc is not "
            "published on PyPI, so any other form is unresolvable"
        )

    def test_pinned_to_a_tag(self, pyproject):
        """Pin to a tag: a moving branch makes installs unreproducible."""
        odoo = pyproject["project"]["optional-dependencies"]["odoo"]
        direct = [r for r in odoo if r.startswith("oerpc @ git+")]
        assert direct, "the odoo extra declares no oerpc direct reference"
        url = direct[0].partition(" @ ")[2]
        ref = url.rpartition("@")[2]
        assert ref, f"{direct[0]!r} has no @<ref> pin"
        assert ref not in ("master", "main"), (
            f"{direct[0]!r} tracks a moving branch; pin a tag instead"
        )
