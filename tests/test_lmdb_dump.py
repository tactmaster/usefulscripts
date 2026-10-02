"""
Tests for mender/lmdb-dump.py.

The fixtures are real LMDB files, but they are built here by hand rather than
with the ``lmdb`` module, so the tests need no compiled dependency and no
network. Building them by hand is also the only way to produce the 32-bit
layout on a 64-bit machine, which is the case the script exists for.
"""

import json
import struct
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "mender" / "lmdb-dump.py"

MAGIC = 0xBEEFC0DE
PAGE = 4096
P_LEAF = 0x02


def _build_store(entries, wide=True, page_size=PAGE):
    """Create a minimal single-file LMDB image containing ``entries``.

    Only what this reader needs is filled in: two meta pages and one leaf page
    holding the entries. ``wide`` selects the 64-bit (True) or 32-bit (False)
    layout, which is the difference the script has to detect.
    """
    hdr = 16 if wide else 12
    pgno = "<Q" if wide else "<I"
    psz = 8 if wide else 4
    db_size = 48 if wide else 28

    def db_record(root, entries_count, leaf_pages):
        # pad(=page_size), flags, depth, then five counters
        out = struct.pack("<IHH", page_size, 0, 1)
        fmt = "<QQQQQ" if wide else "<IIIII"
        out += struct.pack(fmt, 0, leaf_pages, 0, entries_count, root)
        return out

    def meta_page(txnid):
        # page header: pgno, pad, flags, lower, upper  (reader only uses flags
        # and the bounds via _page, so keep the shape right)
        p = struct.pack(pgno, 0) + struct.pack("<HHHH", 0, 0, 0, 0)
        p = p[:hdr].ljust(hdr, b"\x00")
        body = struct.pack("<II", MAGIC, 1)
        body += struct.pack(pgno, 0)          # address
        body += struct.pack(pgno, 1 << 20)    # mapsize
        body += db_record(0xFFFFFFFFFFFFFFFF if wide else 0xFFFFFFFF, 0, 0)
        body += db_record(2, len(entries), 1)
        body += struct.pack("<QQ" if wide else "<II", 3, txnid)
        return (p + body).ljust(page_size, b"\x00")

    # Leaf page: node offsets grow from the front, node data from the back.
    nodes, blob = [], b""
    for key, val in entries:
        node = struct.pack("<HHHH", len(val) & 0xFFFF, len(val) >> 16, 0, len(key))
        node += key + val
        blob = node + blob
        nodes.append(page_size - len(blob))

    leaf = struct.pack(pgno, 2)
    leaf += struct.pack("<HHH", P_LEAF, hdr + 2 * len(nodes), min(nodes))
    leaf = leaf[:hdr].ljust(hdr, b"\x00")
    leaf += b"".join(struct.pack("<H", off) for off in nodes)
    leaf = leaf.ljust(page_size - len(blob), b"\x00") + blob

    return meta_page(1) + meta_page(0) + leaf


def _run(path, *args):
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(path), *args],
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
    """The whole reason the script exists: mdb_dump rejects these."""
    r = _run(store32)
    assert r.returncode == 0, r.stderr
    assert "32-bit writer" in r.stdout
    assert "release-0.9.0" in r.stdout


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
    r = subprocess.run([sys.executable, str(SCRIPT), "--help"],
                       capture_output=True, text=True)
    assert r.returncode == 0
    assert "MDB_NOSUBDIR" in r.stdout


def test_missing_file_is_a_clean_error(tmp_path):
    r = _run(tmp_path / "does-not-exist")
    assert r.returncode != 0
    assert "no such file" in r.stderr
    assert "Traceback" not in r.stderr


def test_non_lmdb_file_is_a_clean_error(tmp_path):
    p = tmp_path / "junk.bin"
    p.write_bytes(b"not an lmdb file" * 100)
    r = _run(p)
    assert r.returncode != 0
    assert "not an LMDB file" in r.stderr
    assert "Traceback" not in r.stderr


def test_empty_file_is_a_clean_error(tmp_path):
    p = tmp_path / "empty"
    p.write_bytes(b"")
    r = _run(p)
    assert r.returncode != 0
    assert "empty" in r.stderr
    assert "Traceback" not in r.stderr


def test_does_not_modify_the_store(store64):
    before = store64.read_bytes()
    _run(store64)
    assert store64.read_bytes() == before, "reader must never write to the store"
