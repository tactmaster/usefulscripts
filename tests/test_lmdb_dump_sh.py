"""
Tests for mender/lmdb-dump.sh, the bash port of lmdb-dump.py.

Reuses ``_build_store`` from test_lmdb_dump so both readers are exercised
against the same hand-built LMDB images, and adds a check that the two
readers produce identical output.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from test_lmdb_dump import _build_store

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "mender" / "lmdb-dump.sh"
PY_SCRIPT = REPO_ROOT / "mender" / "lmdb-dump.py"


def _run(path, *args):
    return subprocess.run(
        ["bash", str(SCRIPT), str(path), *args],
        capture_output=True, text=True,
    )


@pytest.fixture
def store64(tmp_path):
    p = tmp_path / "mender-store"
    p.write_bytes(_build_store(
        [(b"artifact-name", b"release-1.2.3"),
         (b"device-type", b"raspberrypi4")],
        wide=True))
    return p


@pytest.fixture
def store32(tmp_path):
    p = tmp_path / "mender-store-32"
    p.write_bytes(_build_store(
        [(b"artifact-name", b"release-0.9.0")],
        wide=False))
    return p


def test_reads_64bit_store(store64):
    r = _run(store64)
    assert r.returncode == 0, r.stderr
    assert "64-bit writer" in r.stdout
    assert "artifact-name" in r.stdout
    assert "release-1.2.3" in r.stdout


def test_reads_32bit_store(store32):
    r = _run(store32)
    assert r.returncode == 0, r.stderr
    assert "32-bit writer" in r.stdout
    assert "release-0.9.0" in r.stdout


def test_matches_python_reader(store64):
    """Both readers print the same thing for the same store."""
    py = subprocess.run([sys.executable, str(PY_SCRIPT), str(store64)],
                        capture_output=True, text=True)
    sh = _run(store64)
    assert py.returncode == 0 and sh.returncode == 0
    assert py.stdout == sh.stdout


def test_json_values_are_pretty_printed(tmp_path):
    p = tmp_path / "store"
    payload = json.dumps({"Name": "update-after-reboot", "Id": "abc"}).encode()
    p.write_bytes(_build_store([(b"state", payload)], wide=True))
    r = _run(p)
    assert r.returncode == 0, r.stderr
    # Pretty-printed, not the compact form it was stored as.
    assert '"Name": "update-after-reboot"' in r.stdout
    assert payload.decode() not in r.stdout


def test_stats_only_skips_entries(store64):
    r = _run(store64, "--stats-only")
    assert r.returncode == 0, r.stderr
    assert "page size" in r.stdout
    assert "ENTRIES" not in r.stdout
    assert "artifact-name" not in r.stdout


def test_help_exits_zero():
    r = subprocess.run(["bash", str(SCRIPT), "--help"],
                       capture_output=True, text=True)
    assert r.returncode == 0
    assert "MDB_NOSUBDIR" in r.stdout


def test_missing_file_is_a_clean_error(tmp_path):
    r = _run(tmp_path / "does-not-exist")
    assert r.returncode != 0
    assert "no such file" in r.stderr


def test_non_lmdb_file_is_a_clean_error(tmp_path):
    p = tmp_path / "junk.bin"
    p.write_bytes(b"not an lmdb file" * 100)
    r = _run(p)
    assert r.returncode != 0
    assert "not an LMDB file" in r.stderr


def test_empty_file_is_a_clean_error(tmp_path):
    p = tmp_path / "empty"
    p.write_bytes(b"")
    r = _run(p)
    assert r.returncode != 0
    assert "empty" in r.stderr


def test_unknown_option_is_a_clean_error(store64):
    r = _run(store64, "--bogus")
    assert r.returncode != 0
    assert "unknown option" in r.stderr


def test_does_not_modify_the_store(store64):
    before = store64.read_bytes()
    _run(store64)
    assert store64.read_bytes() == before, "reader must never write to the store"
