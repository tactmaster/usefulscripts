"""Print the contents of a single-file LMDB database.

Mender's `mender-store` is one, so this is a way to see what a device has
recorded about itself: the artifact name it believes it is running, its device
type, and where it is in an update. Nothing in here is Mender-specific -- it
reads any single-file (MDB_NOSUBDIR) LMDB database.

Usage:
    lmdb-dump.py <store>                # stats, then every key and value
    lmdb-dump.py <store> --stats-only   # just the header stats

Values that parse as JSON are pretty-printed; anything else is shown as UTF-8
with undecodable bytes replaced.

Uses only the Python standard library, so it runs anywhere python3 does without
installing the lmdb module or the LMDB command-line tools. It opens the file
read-only and never writes to it.

It also handles stores written by a 32-bit device, which the standard mdb_dump
rejects on a 64-bit machine with "MDB_INVALID: File is not an LMDB file". That
is not corruption: on a 32-bit writer pgno_t and size_t are 4 bytes, so the meta
page sits at a different offset. This reader detects the layout from where the
0xBEEFC0DE magic lands and parses either.

Limits: dupsort/LEAF2 (dupfixed) databases are not supported and exit with an
error rather than printing something misleading. Sub-databases are listed but
not descended into.
"""
import json
import struct
import sys

MAGIC = 0xBEEFC0DE

P_BRANCH, P_LEAF, P_OVERFLOW, P_META, P_LEAF2 = 0x01, 0x02, 0x04, 0x08, 0x20
F_BIGDATA, F_SUBDATA, F_DUPDATA = 0x01, 0x02, 0x04


class Store:
    def __init__(self, path):
        try:
            with open(path, "rb") as fh:
                self.buf = fh.read()
        except FileNotFoundError:
            raise SystemExit(f"{path}: no such file")
        except IsADirectoryError:
            raise SystemExit(
                f"{path}: is a directory. This reads single-file (MDB_NOSUBDIR) "
                "stores; for a directory-style database point at its data.mdb")
        except PermissionError:
            raise SystemExit(f"{path}: permission denied")
        if not self.buf:
            raise SystemExit(f"{path}: file is empty")
        # 32-bit writer -> magic at 12; 64-bit -> at 16.
        for hdr, wide in ((12, False), (16, True)):
            if len(self.buf) > hdr + 4 and \
                    struct.unpack_from("<I", self.buf, hdr)[0] == MAGIC:
                self.hdr, self.wide = hdr, wide
                break
        else:
            raise SystemExit(f"{path}: no LMDB magic at offset 12 or 16 "
                             "-- not an LMDB file")
        self.pgno = "<Q" if wide else "<I"
        self.psz = 8 if wide else 4
        self.db_size = 48 if wide else 28
        self.page_size = self.meta(0)["page_size"]

    def _db(self, off):
        pad, flags, depth = struct.unpack_from("<IHH", self.buf, off)
        fmt = "<QQQQQ" if self.wide else "<IIIII"
        branch, leaf, overflow, entries, root = struct.unpack_from(
            fmt, self.buf, off + 8)
        return dict(pad=pad, flags=flags, depth=depth, branch_pages=branch,
                    leaf_pages=leaf, overflow_pages=overflow,
                    entries=entries, root=root)

    def meta(self, page):
        o = page * (getattr(self, "page_size", 4096)) + self.hdr
        magic, version = struct.unpack_from("<II", self.buf, o)
        if magic != MAGIC:
            raise SystemExit(f"meta page {page}: bad magic {magic:#x}")
        mapsize = struct.unpack_from(self.pgno, self.buf, o + 8 + self.psz)[0]
        dbs_at = o + 8 + 2 * self.psz
        dbs = [self._db(dbs_at), self._db(dbs_at + self.db_size)]
        tail = dbs_at + 2 * self.db_size
        last_pg, txnid = struct.unpack_from(
            "<QQ" if self.wide else "<II", self.buf, tail)
        return dict(version=version, mapsize=mapsize, page_size=dbs[0]["pad"],
                    free=dbs[0], main=dbs[1], last_pg=last_pg, txnid=txnid)

    def current(self):
        """LMDB alternates meta pages; the live one has the higher txnid."""
        m0, m1 = self.meta(0), self.meta(1)
        return (m0, 0) if m0["txnid"] >= m1["txnid"] else (m1, 1)

    def _page(self, num):
        o = num * self.page_size
        flags, lower, upper = struct.unpack_from("<HHH", self.buf, o + self.psz + 2)
        return o, flags, lower, upper

    def walk(self, root, out):
        if root == (2 ** (64 if self.wide else 32)) - 1:
            return  # P_INVALID: empty database
        o, flags, lower, upper = self._page(root)
        if flags & P_LEAF2:
            raise SystemExit("LEAF2/dupfixed pages are not supported")
        count = (lower - self.hdr) // 2
        for i in range(count):
            off = struct.unpack_from("<H", self.buf, o + self.hdr + i * 2)[0]
            lo, hi, nflags, ksize = struct.unpack_from("<HHHH", self.buf, o + off)
            key = self.buf[o + off + 8: o + off + 8 + ksize]
            if flags & P_BRANCH:
                child = lo | (hi << 16)
                if self.wide:
                    child |= nflags << 32
                self.walk(child, out)
            elif nflags & F_SUBDATA:
                out.append((key, b"<sub-database>"))
            else:
                size = lo | (hi << 16)
                if nflags & F_BIGDATA:
                    opg = struct.unpack_from(self.pgno, self.buf,
                                             o + off + 8 + ksize)[0]
                    base = opg * self.page_size + self.hdr
                else:
                    base = o + off + 8 + ksize
                out.append((key, self.buf[base:base + size]))

    def entries(self):
        meta, _ = self.current()
        out = []
        self.walk(meta["main"]["root"], out)
        return out


def main():
    argv = sys.argv[1:]
    if "-h" in argv or "--help" in argv:
        print(__doc__.strip())
        return
    args = [a for a in argv if not a.startswith("-")]
    if not args:
        raise SystemExit(__doc__.strip())
    st = Store(args[0])
    meta, which = st.current()
    print(f"word size     : {64 if st.wide else 32}-bit writer")
    print(f"format version: {meta['version']}")
    print(f"page size     : {meta['page_size']}")
    print(f"map size      : {meta['mapsize']}")
    print(f"live meta page: {which} (txnid {meta['txnid']})")
    print(f"last page     : {meta['last_pg']}  "
          f"(file holds {len(st.buf) // st.page_size} pages)")
    m = meta["main"]
    print(f"main db       : {m['entries']} entries, depth {m['depth']}, "
          f"root page {m['root']}, "
          f"{m['leaf_pages']} leaf / {m['branch_pages']} branch / "
          f"{m['overflow_pages']} overflow")
    if "--stats-only" in sys.argv[1:]:
        return
    print()
    print("########## ENTRIES ##########")
    for key, val in st.entries():
        print("=" * 80)
        print("KEY:", key.decode("utf-8", "replace"))
        print("-" * 80)
        try:
            print(json.dumps(json.loads(val), indent=2))
        except Exception:
            print(val.decode("utf-8", "replace"))
    print("=" * 80)


if __name__ == "__main__":
    main()
