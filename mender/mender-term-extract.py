#!/usr/bin/env python3
"""Turn Mender remote terminal recordings into plain text.

In the Mender UI, a remote terminal session in the audit log can be downloaded
as an HTML page that replays it in a browser (terminalsession.html). This pulls
the recorded output back out of that page and writes it as text, one file per
session. Each line is prefixed with its offset from the start of the recording
and, if you give the session's start time with --start, the time of day.

Usage:
    mender-term-extract.py OUTDIR FILE.html... [--start NAME=TIME]...

Writes OUTDIR/NAME.txt for each FILE.html, where NAME is the file name without
.html, and prints the path. TIME is ISO 8601, e.g. 2026-11-10T16:32:05.120;
mender-connect's "starting shell" journal line is a good source for it.

Reads both formats the Mender UI has produced: current exports store each
output chunk as a JSON string with one character per byte, older ones as a
serialized Node Buffer ({"type": "Buffer", "data": [...]}).

Rendering is a minimal terminal: colours and other escape codes are dropped;
carriage returns, backspace, tabs and the cursor and line-editing codes a shell
prompt uses are applied, so edited command lines come out as they ended up.
Full-screen programs (top, vi) don't render usefully. Offsets add up the pauses
the recording stored, so they are approximate.

The output is whatever was on the terminal, including any secrets typed or
printed during the session: treat it like the session itself.

Uses only the Python standard library.
"""
import argparse
import base64
import bisect
import codecs
import datetime as dt
import json
import os
import re
import sys

TRANSFER = re.compile(r"const transfer = (['\"`])(.*?)\1;", re.S)
TITLE = re.compile(r"<h2>(.*?)</h2>", re.S)

# One token per match: a CSI sequence, an OSC string (window title etc.),
# another escape sequence, a run of printable text, or a single character.
TOKEN = re.compile(r"""
    \x1b\[ (?P<params>[0-?]*) [ -/]* (?P<final>[@-~])
  | \x1b\] [^\x07\x1b]* (?:\x07|\x1b\\)?
  | \x1b [()*+] [0-9A-Za-z]
  | \x1b [@-Z\\-_]
  | (?P<text>[^\x00-\x1f\x7f]+)
  | .
""", re.X | re.S)


class ExportError(Exception):
    pass


def load_items(html):
    m = TRANSFER.search(html)
    if not m:
        raise ExportError("no recording in it (no `const transfer`); "
                          "is it a Mender terminal playback export?")
    raw = m.group(2)
    try:
        items = json.loads(raw)
    except json.JSONDecodeError:
        try:
            items = json.loads(raw.encode().decode("unicode_escape"))
        except ValueError as e:
            raise ExportError(f"the recording is not valid JSON: {e}")
    if not isinstance(items, list):
        raise ExportError("the recording is not a list of items")
    return items


def chunk_bytes(content):
    """The raw terminal bytes of one recorded output chunk."""
    value = json.loads(base64.b64decode(content).decode("latin-1"))
    if isinstance(value, str):
        # current exports: btoa(JSON.stringify(String.fromCharCode(...bytes)))
        return value.encode("latin-1", errors="replace")
    if isinstance(value, dict):
        # older exports: btoa(JSON.stringify(buffer)) of a Node Buffer
        return bytes(b & 0xFF for b in value.get("data") or [])
    raise ValueError(f"unexpected {type(value).__name__}")


def timeline(items):
    """Replay the items like the export's player does: an item with a delay
    is a pause (any content it has is not shown), otherwise its content is
    output. Returns ([(offset_ms, bytes)], total_pause_ms)."""
    t, chunks = 0, []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        if item.get("delay"):
            t += int(item["delay"])
        elif item.get("content"):
            try:
                chunks.append((t, chunk_bytes(item["content"])))
            except ValueError as e:
                raise ExportError(f"item {i}: can't decode its output: {e}")
    return chunks, t


def csi_arg(params, default):
    first = params.split(";")[0]
    return int(first) if first.isdigit() else default


def render(chunks):
    """[(offset_ms, bytes)] -> [(offset_ms, line)]. Decodes UTF-8 across chunk
    boundaries and applies the escape codes a line-oriented shell uses. Each
    line gets the time of its first character."""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    starts, times, parts, size = [], [], [], 0
    for t, data in chunks:
        text = decoder.decode(data)
        starts.append(size)
        times.append(t)
        parts.append(text)
        size += len(text)
    parts.append(decoder.decode(b"", final=True))
    text = "".join(parts)

    def time_at(pos):
        i = bisect.bisect_right(starts, pos) - 1
        return times[i] if i >= 0 else 0

    lines, cells, col, line_t = [], [], 0, None

    def put(ch, pos):
        nonlocal col, line_t
        if line_t is None:
            line_t = time_at(pos)
        if col > len(cells):
            cells.extend(" " * (col - len(cells)))
        if col < len(cells):
            cells[col] = ch
        else:
            cells.append(ch)
        col += 1

    for m in TOKEN.finditer(text):
        tok, pos = m.group(), m.start()
        if m.group("text"):
            for k, ch in enumerate(tok):
                put(ch, pos + k)
        elif m.group("final"):
            final, params = m.group("final"), m.group("params")
            if params.startswith("?"):
                continue
            n = csi_arg(params, 1)
            if final == "K":
                mode = csi_arg(params, 0)
                if mode == 0:
                    del cells[col:]
                elif mode == 1:
                    cells[:col] = " " * min(col, len(cells))
                else:
                    cells[:] = []
            elif final == "C":
                col += n
            elif final == "D":
                col = max(0, col - n)
            elif final == "G":
                col = max(0, n - 1)
            elif final == "P":
                del cells[col:col + n]
            elif final == "@":
                if col < len(cells):
                    cells[col:col] = " " * n
            elif final == "X":
                end = min(col + n, len(cells))
                cells[col:end] = " " * max(0, end - col)
        elif tok == "\n":
            lines.append((line_t if line_t is not None else time_at(pos),
                          "".join(cells).rstrip()))
            cells, col, line_t = [], 0, None
        elif tok == "\r":
            col = 0
        elif tok == "\b":
            col = max(0, col - 1)
        elif tok == "\t":
            col = (col // 8 + 1) * 8
    if cells:
        lines.append((line_t if line_t is not None else 0, "".join(cells).rstrip()))
    return lines


def parse_start(value):
    name, sep, when = value.partition("=")
    if not sep or not name:
        raise argparse.ArgumentTypeError(f"expected NAME=TIME, got {value!r}")
    if when.endswith("Z"):
        when = when[:-1] + "+00:00"
    try:
        return name, dt.datetime.fromisoformat(when)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"{when!r} is not an ISO 8601 time, e.g. 2026-11-10T16:32:05.120")


def extract(path, out, start):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            html = fh.read()
    except OSError as e:
        raise ExportError(e.strerror or str(e))
    items = load_items(html)
    chunks, paused = timeline(items)
    title = TITLE.search(html)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(f"# source: {os.path.basename(path)}\n")
        if title:
            fh.write(f"# title: {title.group(1).strip()}\n")
        fh.write(f"# items: {len(items)}, recorded pauses: {paused / 1000:.3f}s"
                 + (f", start: {start.isoformat()}" if start else "") + "\n")
        for off, line in render(chunks):
            stamp = f"+{off / 1000:7.3f}"
            if start:
                at = start + dt.timedelta(milliseconds=off)
                stamp = at.strftime("%H:%M:%S.%f")[:-3] + " " + stamp
            fh.write(f"{stamp}  {line}\n")


def main(argv=None):
    doc = __doc__.split("\n\n")
    ap = argparse.ArgumentParser(
        prog="mender-term-extract.py", description=doc[0],
        epilog="\n\n".join(doc[2:]),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("outdir", metavar="OUTDIR", help="directory for the .txt files")
    ap.add_argument("files", metavar="FILE.html", nargs="+",
                    help="terminal playback exports from the Mender UI")
    ap.add_argument("--start", metavar="NAME=TIME", action="append", default=[],
                    type=parse_start,
                    help="when session NAME (file name without .html) started, "
                         "to print the time of day; repeatable")
    args = ap.parse_intermixed_args(argv)

    starts = dict(args.start)
    stems = [os.path.splitext(os.path.basename(f))[0] for f in args.files]
    for name in starts:
        if name not in stems:
            print(f"--start {name}: no input file named {name}.html", file=sys.stderr)
    try:
        os.makedirs(args.outdir, exist_ok=True)
    except OSError as e:
        sys.exit(f"{args.outdir}: {e.strerror}")

    failed, written = 0, {}
    for path, stem in zip(args.files, stems):
        out = os.path.join(args.outdir, stem + ".txt")
        if out in written:
            print(f"{path}: skipped, it would overwrite {out} from {written[out]}",
                  file=sys.stderr)
            failed += 1
            continue
        try:
            extract(path, out, starts.get(stem))
        except ExportError as e:
            print(f"{path}: {e}", file=sys.stderr)
            failed += 1
            continue
        written[out] = path
        print(out)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
