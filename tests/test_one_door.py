"""Structural guards: one door to dial, no private imports, no secrets in the tree."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "phone_desk"


def test_the_call_create_request_exists_only_in_vapi_py():
    hits = [p.name for p in PKG.rglob("*.py") if re.search(r'"POST",\s*"/call"', p.read_text())]
    assert hits == ["vapi.py"]


def test_place_call_goes_through_the_gate_first():
    src = (PKG / "vapi.py").read_text()
    place = src[src.index("def place_call"):src.index("def place_test_call")]
    test = src[src.index("def place_test_call"):]
    assert place.index("gate.admit(") < place.index("self._create_call(")
    assert test.index("gate.admit_test_line(") < test.index("self._create_call(")


def test_the_package_is_standalone():
    for p in PKG.rglob("*.py"):
        assert not re.search(r"^\s*(from|import)\s+agents\b", p.read_text(), re.M), p


def test_no_real_looking_keys_in_the_repo():
    pat = re.compile(r"(sk-[A-Za-z0-9]{20,}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")
    for p in ROOT.rglob("*"):
        if p.is_file() and ".git" not in p.parts and p.suffix in (".py", ".md", ".toml", ".example", ""):
            assert not pat.search(p.read_text(errors="ignore")), p
