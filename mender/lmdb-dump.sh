#!/usr/bin/env bash
# Print the contents of a single-file LMDB database.
#
# Bash port of lmdb-dump.py for machines that have no python3 at all --
# e.g. run it directly on an embedded device over ssh. Same output format,
# same limits. See --help or mender/README.md.
set -u
LC_ALL=C

usage() {
    cat <<'EOF'
Print the contents of a single-file LMDB database.

Mender's `mender-store` is one, so this is a way to see what a device has
recorded about itself: the artifact name it believes it is running, its device
type, and where it is in an update. Nothing in here is Mender-specific -- it
reads any single-file (MDB_NOSUBDIR) LMDB database.

Usage:
    lmdb-dump.sh <store>                # stats, then every key and value
    lmdb-dump.sh <store> --stats-only   # just the header stats

Values that parse as JSON are pretty-printed when jq or python3 is available;
anything else is printed as-is.

Needs only bash plus one of od/xxd/hexdump and dd, so it runs on systems
without python3. The helper tools can be busybox applets, but the shell must
be bash. It opens the file read-only and never writes to it.

It also handles stores written by a 32-bit device, which the standard mdb_dump
rejects on a 64-bit machine with "MDB_INVALID: File is not an LMDB file". That
is not corruption: on a 32-bit writer pgno_t and size_t are 4 bytes, so the
meta page sits at a different offset. This reader detects the layout from
where the 0xBEEFC0DE magic lands and parses either.

Limits: dupsort/LEAF2 (dupfixed) databases are not supported and exit with an
error rather than printing something misleading. Sub-databases are listed but
not descended into.
EOF
}

MAGIC=$(( 16#BEEFC0DE ))
P_BRANCH=1 P_LEAF2=32
F_BIGDATA=1 F_SUBDATA=2

die() { printf '%s\n' "$*" >&2; exit 1; }

# Whole file as one lowercase hex string; ${HEX:off*2:2} is byte `off`.
# Each tool is probed on one byte first, so a busybox od built without -t
# falls through to the next tool instead of producing garbage.
load_hex() {
    if [ "$(printf A | od -An -v -tx1 2>/dev/null | tr -d ' \t\n')" = "41" ]; then
        HEX=$(od -An -v -tx1 -- "$FILE" | tr -d ' \t\n')
    elif [ "$(printf A | xxd -p 2>/dev/null)" = "41" ]; then
        HEX=$(xxd -p "$FILE" | tr -d '\n')
    elif [ "$(printf A | hexdump -v -e '1/1 "%02x"' 2>/dev/null)" = "41" ]; then
        HEX=$(hexdump -v -e '1/1 "%02x"' "$FILE")
    else
        die "need a working od, xxd, or hexdump to read the file"
    fi
    (( ${#HEX} == SIZE * 2 )) || die "$FILE: hex dump came up short"
}

le() {  # le OFFSET NBYTES: little-endian unsigned int -> global R
    local i h=
    for (( i = $2 - 1; i >= 0; i-- )); do h+=${HEX:($1+i)*2:2}; done
    (( ${#h} == $2 * 2 )) || die "$FILE: truncated read at byte offset $1"
    R=$(( 16#$h ))
}

raw() {  # raw OFFSET NBYTES: the bytes themselves, to stdout
    dd if="$FILE" bs=1 skip="$1" count="$2" 2>/dev/null
}

json_pretty() {
    if command -v jq >/dev/null 2>&1; then jq .
    elif command -v python3 >/dev/null 2>&1; then python3 -m json.tool
    else return 1; fi
}

read_meta() {  # read_meta PAGE -> globals M_*
    local page=$1 o dbs_at main tail tsz
    o=$(( page * ${PAGE_SIZE:-4096} + HDR ))
    le "$o" 4
    (( R == MAGIC )) || die "meta page $page: bad magic $(printf '%#x' "$R")"
    le $((o + 4)) 4;              M_version=$R
    le $((o + 8 + PSZ)) "$PSZ";   M_mapsize=$R
    dbs_at=$(( o + 8 + 2 * PSZ ))
    # free db: only its pad field matters -- LMDB keeps the page size there
    le "$dbs_at" 4;               M_pagesize=$R
    main=$(( dbs_at + DB_SIZE ))
    le $((main + 6)) 2;           M_depth=$R
    le $((main + 8)) "$PSZ";          M_branch=$R
    le $((main + 8 + PSZ)) "$PSZ";    M_leaf=$R
    le $((main + 8 + 2 * PSZ)) "$PSZ"; M_overflow=$R
    le $((main + 8 + 3 * PSZ)) "$PSZ"; M_entries=$R
    le $((main + 8 + 4 * PSZ)) "$PSZ"; M_root=$R
    tail=$(( dbs_at + 2 * DB_SIZE ))
    tsz=$(( WIDE ? 8 : 4 ))
    le "$tail" "$tsz";            M_lastpg=$R
    le $((tail + tsz)) "$tsz";    M_txnid=$R
}

RULE_EQ=$(printf '=%.0s' {1..80})
RULE_DASH=$(printf -- '-%.0s' {1..80})

print_entry() {  # print_entry PAGE_OFF NODE_OFF LO HI NFLAGS KSIZE
    local o=$1 off=$2 lo=$3 hi=$4 nflags=$5 ksize=$6 size base pretty
    printf '%s\n' "$RULE_EQ"
    printf 'KEY: %s\n' "$(raw $((o + off + 8)) "$ksize")"
    printf '%s\n' "$RULE_DASH"
    if (( nflags & F_SUBDATA )); then
        printf '<sub-database>\n'
        return 0
    fi
    size=$(( lo | hi << 16 ))
    if (( nflags & F_BIGDATA )); then
        le $(( o + off + 8 + ksize )) "$PSZ"   # overflow page number
        base=$(( R * PAGE_SIZE + HDR ))
    else
        base=$(( o + off + 8 + ksize ))
    fi
    if pretty=$(raw "$base" "$size" | json_pretty 2>/dev/null) \
            && [ -n "$pretty" ]; then
        printf '%s\n' "$pretty"
    else
        raw "$base" "$size"
        printf '\n'
    fi
    return 0
}

walk() {  # walk PAGE_NUMBER: recurse the B-tree, printing leaf entries
    local root=$1 o pflags lower count i off lo hi nflags ksize child
    (( root == INVALID )) && return 0   # P_INVALID: empty database
    o=$(( root * PAGE_SIZE ))
    le $(( o + PSZ + 2 )) 2; pflags=$R
    le $(( o + PSZ + 4 )) 2; lower=$R
    if (( pflags & P_LEAF2 )); then
        die "LEAF2/dupfixed pages are not supported"
    fi
    count=$(( (lower - HDR) / 2 ))
    for (( i = 0; i < count; i++ )); do
        le $(( o + HDR + i * 2 )) 2; off=$R
        le $(( o + off )) 2;     lo=$R
        le $(( o + off + 2 )) 2; hi=$R
        le $(( o + off + 4 )) 2; nflags=$R
        le $(( o + off + 6 )) 2; ksize=$R
        if (( pflags & P_BRANCH )); then
            child=$(( lo | hi << 16 ))
            if (( WIDE )); then child=$(( child | nflags << 32 )); fi
            walk "$child"
        else
            print_entry "$o" "$off" "$lo" "$hi" "$nflags" "$ksize"
        fi
    done
    return 0
}

FILE= STATS_ONLY=0
for arg in "$@"; do
    case $arg in
        -h|--help) usage; exit 0 ;;
        --stats-only) STATS_ONLY=1 ;;
        -*) die "unknown option: $arg (see --help)" ;;
        *) [ -z "$FILE" ] && FILE=$arg ;;
    esac
done
if [ -z "$FILE" ]; then usage >&2; exit 1; fi

[ -e "$FILE" ] || die "$FILE: no such file"
[ -d "$FILE" ] && die "$FILE: is a directory. This reads single-file \
(MDB_NOSUBDIR) stores; for a directory-style database point at its data.mdb"
[ -r "$FILE" ] || die "$FILE: permission denied"
[ -s "$FILE" ] || die "$FILE: file is empty"

SIZE=$(wc -c < "$FILE")
load_hex

# 32-bit writer -> magic at 12; 64-bit -> at 16. The offset doubles as the
# page header size (pgno + pad + flags + lower + upper).
HDR= WIDE=
if (( SIZE > 16 )); then
    le 12 4; (( R == MAGIC )) && { HDR=12; WIDE=0; }
fi
if [ -z "$HDR" ] && (( SIZE > 20 )); then
    le 16 4; (( R == MAGIC )) && { HDR=16; WIDE=1; }
fi
[ -n "$HDR" ] || die "$FILE: no LMDB magic at offset 12 or 16 -- not an LMDB file"

if (( WIDE )); then
    PSZ=8 DB_SIZE=48 INVALID=-1   # 0xFFFFFFFFFFFFFFFF wraps to -1 in bash
else
    PSZ=4 DB_SIZE=28 INVALID=4294967295
fi

PAGE_SIZE=
read_meta 0
PAGE_SIZE=$M_pagesize
(( PAGE_SIZE > 0 )) || die "$FILE: meta page says page size 0"
TXNID0=$M_txnid
read_meta 1
# LMDB alternates meta pages; the live one has the higher txnid.
if (( TXNID0 >= M_txnid )); then
    read_meta 0; WHICH=0
else
    WHICH=1
fi

printf 'word size     : %s-bit writer\n' "$(( WIDE ? 64 : 32 ))"
printf 'format version: %s\n' "$M_version"
printf 'page size     : %s\n' "$M_pagesize"
printf 'map size      : %s\n' "$M_mapsize"
printf 'live meta page: %s (txnid %s)\n' "$WHICH" "$M_txnid"
printf 'last page     : %s  (file holds %s pages)\n' \
    "$M_lastpg" "$(( SIZE / PAGE_SIZE ))"
printf 'main db       : %s entries, depth %s, root page %s, %s leaf / %s branch / %s overflow\n' \
    "$M_entries" "$M_depth" "$M_root" "$M_leaf" "$M_branch" "$M_overflow"
(( STATS_ONLY )) && exit 0

printf '\n########## ENTRIES ##########\n'
walk "$M_root"
printf '%s\n' "$RULE_EQ"
