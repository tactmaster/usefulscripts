"""
Tests for mender/mender-term-extract.py.

The fixtures are built the way the Mender UI builds its terminal playback
export (TerminalPlayer.tsx, generateHtml): current exports store each output
chunk as btoa(JSON.stringify(String.fromCharCode(...bytes))), older ones as
btoa(JSON.stringify(buffer)) of a Node Buffer.
"""

import base64
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "mender" / "mender-term-extract.py"
STAMP = re.compile(r"^(\S+ )?\+ *[0-9.]+  ")

PAGE = """<!DOCTYPE html>
<html>
  <body>
    <h2>Terminal playback</h2>
    <div id="terminal"></div>
    <script>
      const transfer = '{transfer}';
      const content = JSON.parse(transfer);
    </script>
  </body>
</html>
"""


def current_item(data: bytes):
    # String.fromCharCode(...bytes): one character per byte. JSON.stringify
    # leaves characters >= 0x80 as they are; btoa encodes each as one byte.
    text = json.dumps(data.decode("latin-1"), ensure_ascii=False)
    return {"content": base64.b64encode(text.encode("latin-1")).decode()}


def older_item(data: bytes):
    buf = json.dumps({"type": "Buffer", "data": list(data)})
    return {"content": base64.b64encode(buf.encode()).decode()}


def export(tmp_path, chunks, name="terminalsession", item=current_item):
    """chunks: bytes (output) or int (a recorded pause in ms)."""
    items = [{"delay": c} if isinstance(c, int) else item(c) for c in chunks]
    p = tmp_path / f"{name}.html"
    p.write_text(PAGE.replace("{transfer}", json.dumps(items, separators=(",", ":"))))
    return p


def run(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)],
                          capture_output=True, text=True)


def extract(tmp_path, chunks, *args, **kw):
    """Extract one export and return its lines without the header and stamp."""
    src = export(tmp_path, chunks, **kw)
    out = tmp_path / "out"
    r = run(out, src, *args)
    assert r.returncode == 0, r.stderr
    text = (out / (src.stem + ".txt")).read_text()
    return [STAMP.sub("", ln) for ln in text.splitlines() if not ln.startswith("#")]


def test_plain_session(tmp_path):
    assert extract(tmp_path, [b"$ ls\r\n", b"a.txt\r\nb.txt\r\n$ "]) == [
        "$ ls", "a.txt", "b.txt", "$"]


def test_offsets_come_from_recorded_pauses(tmp_path):
    src = export(tmp_path, [b"$ date\r\n", 1500, b"Mon\r\n", 250, b"$ "])
    r = run(tmp_path / "out", src)
    assert r.returncode == 0, r.stderr
    text = (tmp_path / "out" / "terminalsession.txt").read_text().splitlines()
    assert text[0] == "# source: terminalsession.html"
    assert text[1] == "# title: Terminal playback"
    assert text[2] == "# items: 5, recorded pauses: 1.750s"
    assert text[3:] == ["+  0.000  $ date", "+  1.500  Mon", "+  1.750  $"]
    assert r.stdout.strip() == str(tmp_path / "out" / "terminalsession.txt")


def test_start_time_adds_time_of_day(tmp_path):
    src = export(tmp_path, [b"a\r\n", 2500, b"b\r\n"], name="AMGW1_1632")
    r = run(tmp_path / "out", src, "--start", "AMGW1_1632=2026-11-10T16:32:05.120")
    assert r.returncode == 0, r.stderr
    text = (tmp_path / "out" / "AMGW1_1632.txt").read_text().splitlines()
    assert text[2].endswith(", start: 2026-11-10T16:32:05.120000")
    assert text[3:] == ["16:32:05.120 +  0.000  a", "16:32:07.620 +  2.500  b"]


def test_utf8_split_across_chunks(tmp_path):
    dot = "●".encode()  # 3 bytes, as systemctl status prints it
    lines = extract(tmp_path, [b"x " + dot[:1], dot[1:2], dot[2:] + b" active\r\n"])
    assert lines == ["x ● active"]


def test_invalid_utf8_does_not_crash(tmp_path):
    assert extract(tmp_path, [b"bad \xff byte\r\n"]) == ["bad � byte"]


def test_older_buffer_format(tmp_path):
    lines = extract(tmp_path, [b"caf\xc3", 100, b"\xa9 ok\r\n"], item=older_item)
    assert lines == ["café ok"]


def test_item_with_delay_and_content_is_a_pause(tmp_path):
    """The older player shows an item's content only if it has no delay."""
    src = tmp_path / "terminalsession.html"
    items = [older_item(b"shown\r\n"),
             {"delay": 40, "content": base64.b64encode(b"not shown").decode()}]
    src.write_text(PAGE.replace("{transfer}", json.dumps(items)))
    r = run(tmp_path / "out", src)
    assert r.returncode == 0, r.stderr
    text = (tmp_path / "out" / "terminalsession.txt").read_text()
    assert "shown" in text and "not shown" not in text


def test_colours_titles_and_bracketed_paste_are_dropped(tmp_path):
    out = (b"\x1b[?2004h\x1b]0;root@dev: ~\x07\x1b[01;32mroot@dev\x1b[00m:"
           b"\x1b[01;34m~\x1b[00m# ls\r\n\x1b[?2004l\r")
    assert extract(tmp_path, [out]) == ["root@dev:~# ls"]


def test_escape_sequence_split_across_chunks(tmp_path):
    assert extract(tmp_path, [b"a\x1b[3", b"1mred\x1b[0m\r\n"]) == ["ared"]


def test_carriage_return_overwrites(tmp_path):
    assert extract(tmp_path, [b"10%\r", b"50%\r", b"100%\r\n"]) == ["100%"]


def test_tabs_expand(tmp_path):
    assert extract(tmp_path, [b"a\tbc\td\r\n"]) == ["a       bc      d"]


def test_backspace_and_erase(tmp_path):
    # bash rubbing out a typo: "lss", backspace, erase to end of line
    assert extract(tmp_path, [b"$ lss", b"\b\x1b[K", b" -l\r\n"]) == ["$ ls -l"]


def test_insert_and_delete_characters(tmp_path):
    # cursor left twice, insert one blank, type x; then delete a character
    assert extract(tmp_path, [b"$ ls\x1b[2D\x1b[1@x", b"\r\n"]) == ["$ xls"]
    assert extract(tmp_path, [b"$ cat\x1b[3D\x1b[1P\r\n"]) == ["$ at"]


def test_history_browsing_leaves_the_last_command(tmp_path):
    """Up-arrow redraws with \\r + cursor-right to the end of the prompt; the
    line that ends up on screen is what was run, not the history browsed."""
    prompt = b"user@host:/$ "  # 13 columns
    back = b"\r" + b"\x1b[C" * 13
    session = [prompt, b"systemctl status mender-updated",
               back + b"journalctl -u mender-authd -f\x1b[K",
               back + b"\x1b[K", b"\x07", b"lss", b"\b\x1b[K", b" /data\r\n"]
    assert extract(tmp_path, session) == ["user@host:/$ ls /data"]


def test_several_files_and_one_bad_one(tmp_path):
    good = export(tmp_path, [b"ok\r\n"], name="good")
    bad = tmp_path / "bad.html"
    bad.write_text("<html>not an export</html>")
    r = run(tmp_path / "out", bad, good, tmp_path / "missing.html")
    assert r.returncode == 1
    assert (tmp_path / "out" / "good.txt").exists()
    assert "bad.html: no recording in it" in r.stderr
    assert "missing.html: No such file or directory" in r.stderr
    assert "Traceback" not in r.stderr


def test_same_name_twice_is_not_overwritten(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    one = export(tmp_path / "a", [b"one\r\n"])
    two = export(tmp_path / "b", [b"two\r\n"])
    r = run(tmp_path / "out", one, two)
    assert r.returncode == 1
    assert "would overwrite" in r.stderr
    assert "one" in (tmp_path / "out" / "terminalsession.txt").read_text()


def test_bad_start_is_a_usage_error(tmp_path):
    src = export(tmp_path, [b"x\r\n"])
    r = run(tmp_path / "out", src, "--start", "terminalsession=yesterday")
    assert r.returncode == 2
    assert "not an ISO 8601 time" in r.stderr
    assert "Traceback" not in r.stderr


def test_start_for_unknown_session_warns(tmp_path):
    src = export(tmp_path, [b"x\r\n"])
    r = run(tmp_path / "out", src, "--start", "other=2026-11-10T16:32:05")
    assert r.returncode == 0
    assert "--start other: no input file named other.html" in r.stderr


def test_help(tmp_path):
    r = run("--help")
    assert r.returncode == 0
    assert "OUTDIR" in r.stdout and "--start" in r.stdout
