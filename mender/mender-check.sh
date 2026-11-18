#!/bin/sh
#
# Check that a device's Mender client install is healthy: the data store is
# where the client looks for it, the symlinks resolve, the configuration
# parses and makes sense, and (on a live device) the bootloader, services and
# server agree with it.
#
# POSIX sh, so it runs on busybox devices. Uses jq or python3 for a strict
# JSON check if either is present, but does not need them.
#
# Usage:
#   ./mender-check.sh                 # on the device, as root
#   ./mender-check.sh --root /mnt     # a mounted image or extracted rootfs
#   ./mender-check.sh --no-network    # skip the server reachability check
#
# Exit status: 0 if nothing failed (warnings allowed), 1 if any check
# failed, 2 for a usage error.

set -u

usage() {
    cat <<'EOF'
usage: mender-check.sh [--root DIR] [--no-network]

Checks a Mender client install and prints OK / WARN / FAIL per check.

  --root DIR     check the filesystem mounted at DIR (an image or extracted
                 rootfs) instead of the running system. Checks that need the
                 live system -- mounts, bootloader environment, services,
                 identity script output, server -- are skipped.
  --no-network   don't try to reach the Mender server
  -h, --help     show this help

Paths follow the client's own defaults and environment overrides:
MENDER_CONF_DIR (/etc/mender), MENDER_DATASTORE_DIR (/var/lib/mender) and
MENDER_DATA_DIR (/usr/share/mender).

Exit status: 0 if nothing failed, 1 if any check failed, 2 on usage error.
EOF
}

ROOT=
NETWORK=1
while [ $# -gt 0 ]; do
    case $1 in
        --root)
            [ $# -ge 2 ] || { echo "--root needs a directory" >&2; exit 2; }
            ROOT=${2%/}; shift ;;
        --root=*) ROOT=${1#--root=}; ROOT=${ROOT%/} ;;
        --no-network) NETWORK=0 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown argument: $1 (see --help)" >&2; exit 2 ;;
    esac
    shift
done
if [ -n "$ROOT" ] && [ ! -d "$ROOT" ]; then
    echo "--root $ROOT: not a directory" >&2
    exit 2
fi
LIVE=1
[ -n "$ROOT" ] && LIVE=0

CONF_DIR=${MENDER_CONF_DIR:-/etc/mender}
DATA_DIR=${MENDER_DATA_DIR:-/usr/share/mender}
STORE_DIR=${MENDER_DATASTORE_DIR:-/var/lib/mender}
CONF_MAIN=$CONF_DIR/mender.conf
CONF_FALLBACK=$STORE_DIR/mender.conf

TAB=$(printf '\t')
N_OK=0 N_WARN=0 N_FAIL=0

section() { printf '\n== %s\n' "$*"; }
ok()   { N_OK=$((N_OK + 1));     printf '  [ OK ] %s\n' "$*"; }
warn() { N_WARN=$((N_WARN + 1)); printf '  [WARN] %s\n' "$*"; }
fail() { N_FAIL=$((N_FAIL + 1)); printf '  [FAIL] %s\n' "$*"; }
info() { printf '  [info] %s\n' "$*"; }
hint() { printf '         %s\n' "$*"; }

have() { command -v "$1" >/dev/null 2>&1; }

# canon PATH: resolve symlinks one component at a time, treating $ROOT as /,
# so absolute links inside a mounted image resolve inside the image.
canon() {
    local rest out comp target hops
    rest=$1 out= hops=0
    while [ -n "$rest" ]; do
        case $rest in
            */*) comp=${rest%%/*}; rest=${rest#*/} ;;
            *) comp=$rest; rest= ;;
        esac
        case $comp in
            ''|.) continue ;;
            ..) out=${out%/*}; continue ;;
        esac
        if [ -L "$ROOT$out/$comp" ]; then
            hops=$((hops + 1))
            [ $hops -le 40 ] || return 1
            target=$(readlink "$ROOT$out/$comp")
            case $target in /*) out= ;; esac
            rest=$target${rest:+/$rest}
        else
            out=$out/$comp
        fi
    done
    printf '%s\n' "${out:-/}"
}

# rp PATH: where PATH really is on this filesystem ($ROOT + resolved path)
rp() {
    local c
    c=$(canon "$1") || c=$1
    printf '%s%s\n' "$ROOT" "$c"
}

# hexbytes FILE OFFSET COUNT: lowercase hex, no spaces
hexbytes() {
    local h
    h=$(od -An -v -tx1 -j "$2" -N "$3" "$1" 2>/dev/null | tr -d ' \t\n')
    if [ -z "$h" ] && have hexdump; then
        h=$(hexdump -v -s "$2" -n "$3" -e '1/1 "%02x"' "$1" 2>/dev/null)
    fi
    if [ -z "$h" ] && have xxd; then
        h=$(xxd -p -s "$2" -l "$3" "$1" 2>/dev/null | tr -d '\n')
    fi
    printf '%s\n' "$h"
}

# Flatten JSON to "path<TAB>type<TAB>value" lines (type: s n b z o a), e.g.
#   ServerURL  s  https://hosted.mender.io
#   Servers.0.ServerURL  s  https://...
# On a syntax error prints "!<TAB>message" and exits 3. A small recursive
# descent parser so this works without jq or python3.
JSON_AWK='
function ws() { while (p <= n && index(" \t\r\n", substr(s, p, 1))) p++ }
function err(m) { if (bad == "") bad = m " (byte " p ")"; p = n + 1 }
function emit(path, t, v) {
    if (path == "") return
    gsub(/\t/, "\\t", v); gsub(/\n/, "\\n", v)
    print path "\t" t "\t" v
}
function str(   c, out) {
    p++; out = ""
    while (p <= n) {
        c = substr(s, p, 1)
        if (c == "\"") { p++; return out }
        if (c == "\n") break
        if (c == "\\") {
            p++; c = substr(s, p, 1)
            if (c == "n") c = "\n"; else if (c == "t") c = "\t"
            else if (c == "r") c = "\r"; else if (c == "b" || c == "f") c = ""
            else if (c == "u") { out = out "\\u" substr(s, p + 1, 4); p += 5; continue }
            else if (c != "\"" && c != "\\" && c != "/") { err("bad escape \\" c); return out }
        }
        out = out c; p++
    }
    err("unterminated string"); return out
}
function val(path,   c, k, i) {
    ws(); c = substr(s, p, 1)
    if (c == "{") {
        emit(path, "o", ""); p++; ws()
        if (substr(s, p, 1) == "}") { p++; return }
        while (bad == "") {
            ws()
            if (substr(s, p, 1) != "\"") { err("expected a quoted key"); return }
            k = str(); ws()
            if (substr(s, p, 1) != ":") { err("expected : after \"" k "\""); return }
            p++
            val(path == "" ? k : path "." k); ws()
            c = substr(s, p, 1)
            if (c == ",") { p++; continue }
            if (c == "}") { p++; return }
            err("expected , or } after \"" k "\""); return
        }
    } else if (c == "[") {
        emit(path, "a", ""); p++; ws()
        if (substr(s, p, 1) == "]") { p++; return }
        for (i = 0; bad == ""; i++) {
            val(path "." i); ws()
            c = substr(s, p, 1)
            if (c == ",") { p++; continue }
            if (c == "]") { p++; return }
            err("expected , or ] in array"); return
        }
    } else if (c == "\"") {
        emit(path, "s", str())
    } else if (match(substr(s, p), /^(true|false)/)) {
        emit(path, "b", substr(s, p, RLENGTH)); p += RLENGTH
    } else if (match(substr(s, p), /^null/)) {
        emit(path, "z", ""); p += 4
    } else if (match(substr(s, p), /^-?[0-9][0-9.eE+-]*/)) {
        emit(path, "n", substr(s, p, RLENGTH)); p += RLENGTH
    } else if (p > n) {
        err("unexpected end of file")
    } else {
        err("unexpected \"" c "\"")
    }
}
{ s = s $0 "\n" }
END {
    n = length(s); p = 1; bad = ""
    ws()
    if (substr(s, p, 1) != "{") err("top level must be a JSON object")
    else val("")
    ws()
    if (bad == "" && p <= n) err("extra data after the closing }")
    if (bad != "") { print "!\t" bad; exit 3 }
}'

flatten() { awk "$JSON_AWK" "$1"; }

# lookup FLAT KEY -> "type<TAB>value", empty if absent
lookup() {
    printf '%s\n' "$1" | awk -F "$TAB" -v k="$2" \
        '$1 == k { sub(/^[^\t]*\t/, ""); print; exit }'
}

# cget KEY: effective value, main config over fallback. Sets V, T, SRC.
cget() {
    local hit
    V= T= SRC=
    hit=$(lookup "$FLAT_MAIN" "$1")
    if [ -n "$hit" ]; then SRC=$CONF_MAIN
    else hit=$(lookup "$FLAT_FALLBACK" "$1"); SRC=$CONF_FALLBACK; fi
    [ -n "$hit" ] || { SRC=; return 1; }
    T=${hit%%"$TAB"*} V=${hit#*"$TAB"}
    return 0
}

typename() {
    case $1 in s) echo string ;; n) echo number ;; b) echo boolean ;;
        z) echo null ;; o) echo object ;; a) echo array ;; esac
}

# Keys the 4.x client reads (src/client_shared/config_parser), plus
# RootfsPartA/B (read by the rootfs-image update module) and keys only the
# 3.x Go client read. Format: Key:type
KNOWN_KEYS='ArtifactVerifyKey:s ArtifactVerifyKeys:a DaemonLogLevel:s
DeviceTier:s DeviceTypeFile:s HttpsClient:o InventoryPollIntervalSeconds:n
ModuleTimeoutSeconds:n RetryDownloadCount:n RetryPollCount:n
RetryPollIntervalSeconds:n RootfsPartA:s RootfsPartB:s Security:o
ServerCertificate:s Servers:a ServerURL:s SkipVerify:b
StateScriptRetryIntervalSeconds:n StateScriptRetryTimeoutSeconds:n
StateScriptTimeoutSeconds:n TenantToken:s UpdateLogPath:s
UpdatePollIntervalSeconds:n
BootUtilitiesGetNextActivePart:s BootUtilitiesSetActivePart:s
ClientProtocol:s Connectivity:o DBus:o'

check_conf_keys() {  # check_conf_keys FILE FLAT
    local key t want entry lower match
    for key in $(printf '%s\n' "$2" | awk -F "$TAB" '$1 !~ /\./ { print $1 }'); do
        t=$(lookup "$2" "$key"); t=${t%%"$TAB"*}
        want=
        for entry in $KNOWN_KEYS; do
            [ "${entry%:*}" = "$key" ] && { want=${entry#*:}; break; }
        done
        if [ -z "$want" ]; then
            lower=$(printf '%s' "$key" | tr 'A-Z' 'a-z')
            match=
            for entry in $KNOWN_KEYS; do
                if [ "$(printf '%s' "${entry%:*}" | tr 'A-Z' 'a-z')" = "$lower" ]; then
                    match=${entry%:*}
                fi
            done
            if [ -n "$match" ] && [ "$CLIENT_MAJOR" = 3 ]; then
                :  # the 3.x Go client matched keys case-insensitively
            elif [ -n "$match" ]; then
                warn "$1: \"$key\" is ignored -- keys are case-sensitive; did you mean \"$match\"?"
            else
                warn "$1: \"$key\" is not a setting the Mender client reads (typo?)"
            fi
        elif [ "$t" != "$want" ]; then
            warn "$1: \"$key\" is a $(typename "$t"), but the client expects a $(typename "$want") and silently ignores it"
            [ "$want" = n ] && [ "$t" = s ] && hint "write it without quotes, e.g. \"$key\": 1800"
        fi
    done
}

# load_conf PATH: report problems; on success the flattened file is in LOADED
FLAT_MAIN= FLAT_FALLBACK= CONF_UNREADABLE=0
load_conf() {
    local path=$1 f flat bad
    f=$(rp "$path")
    [ -e "$f" ] || return 1
    if [ ! -r "$f" ]; then
        warn "$path: exists but can't be read (run as root)"
        CONF_UNREADABLE=1
        return 1
    fi
    flat=$(flatten "$f")
    bad=$(printf '%s\n' "$flat" | sed -n "s/^!$TAB//p")
    if [ -n "$bad" ]; then
        fail "$path: not valid JSON: $bad"
        hint "the client refuses to start with a broken config file"
        return 1
    fi
    if have jq; then
        jq empty "$f" >/dev/null 2>&1 \
            || { fail "$path: jq rejects it as invalid JSON"; return 1; }
    elif have python3; then
        python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$f" \
            >/dev/null 2>&1 \
            || { fail "$path: python3 rejects it as invalid JSON"; return 1; }
    fi
    LOADED=$flat
    return 0
}

# find_bin NAME -> path (inside ROOT), or nothing
find_bin() {
    local d
    if [ $LIVE = 1 ]; then command -v "$1" 2>/dev/null; return; fi
    for d in /usr/bin /usr/sbin /bin /sbin /usr/local/bin; do
        [ -f "$(rp "$d/$1")" ] && { echo "$d/$1"; return; }
    done
}

# elf_bits FILE -> 32 / 64 / nothing
elf_bits() {
    case $(hexbytes "$1" 0 5) in
        7f454c4601) echo 32 ;;
        7f454c4602) echo 64 ;;
    esac
}

# Running root block device, resolved, or nothing
running_root() {
    local dev arg
    dev=$(awk '$2 == "/" { d = $1 } END { print d }' /proc/mounts 2>/dev/null)
    case $dev in
        /dev/root|'') dev= ;;
        /dev/*) ;;
        *) dev= ;;
    esac
    if [ -z "$dev" ]; then
        arg=$(tr ' ' '\n' < /proc/cmdline 2>/dev/null | sed -n 's/^root=//p' | tail -n 1)
        case $arg in
            PARTUUID=*) dev=/dev/disk/by-partuuid/${arg#PARTUUID=} ;;
            UUID=*) dev=/dev/disk/by-uuid/${arg#UUID=} ;;
            PARTLABEL=*) dev=/dev/disk/by-partlabel/${arg#PARTLABEL=} ;;
            LABEL=*) dev=/dev/disk/by-label/${arg#LABEL=} ;;
            ubi*) dev=/dev/$(printf '%s' "$arg" | tr ':' '_') ;;
            *) dev=$arg ;;
        esac
    fi
    [ -n "$dev" ] || return 1
    readlink -f "$dev" 2>/dev/null || printf '%s\n' "$dev"
}

# Same normalisation the rootfs-image module applies to RootfsPartA/B
resolve_part() {
    local p
    p=$(printf '%s' "$1" | sed -e 's,^ubi,/dev/ubi,')
    readlink -f "$p" 2>/dev/null || printf '%s\n' "$p"
}

# mount_of PATH -> "device mountpoint"
mount_of() { df -P "$1" 2>/dev/null | awk 'NR == 2 { print $1, $NF }'; }

# exec_dir_check DIR WHAT [SKIP]: warn about non-executable files; COUNT and NAMES
# describe the usable (executable) ones
exec_dir_check() {
    local f
    COUNT=0 NAMES=
    for f in "$(rp "$1")"/*; do
        [ -f "$f" ] || continue
        [ "${f##*/}" = "${3:-}" ] && continue
        if [ -x "$f" ]; then
            COUNT=$((COUNT + 1))
            NAMES=${NAMES:+$NAMES, }${f##*/}
        else
            warn "$1/${f##*/}: not executable, so the client can't run this $2"
        fi
    done
}

printf 'Mender client check'
if [ $LIVE = 1 ]; then
    printf ' (this device: %s)\n' "$(uname -n 2>/dev/null)"
else
    printf ' (filesystem at %s)\n' "$ROOT"
fi
IS_ROOT=1
if [ $LIVE = 1 ] && [ "$(id -u 2>/dev/null)" != 0 ]; then
    IS_ROOT=0
    printf 'Not running as root: some files can'"'"'t be read and those checks are skipped.\n'
fi

# ---------------------------------------------------------------- client
section "Client"
CLIENT_BIN= CLIENT_MAJOR=
if b=$(find_bin mender-update) && [ -n "$b" ]; then
    CLIENT_BIN=$b CLIENT_MAJOR=4
    if a=$(find_bin mender-auth) && [ -n "$a" ]; then
        ok "Mender client 4.x or later (mender-update and mender-auth in ${b%/*})"
    else
        fail "mender-update is installed but mender-auth is not"
        hint "without mender-auth the device can't authenticate to the server"
    fi
elif b=$(find_bin mender) && [ -n "$b" ]; then
    CLIENT_BIN=$b CLIENT_MAJOR=3
    ok "Mender client 3.x or older ($b)"
else
    fail "no Mender client found (neither mender-update nor mender)"
fi
if [ $LIVE = 1 ] && [ -n "$CLIENT_BIN" ]; then
    v=$("$CLIENT_BIN" --version 2>/dev/null | head -n 1)
    [ -n "$v" ] && info "version: $v"
fi
CLIENT_BITS=
[ -n "$CLIENT_BIN" ] && CLIENT_BITS=$(elf_bits "$ROOT$CLIENT_BIN")

case $CLIENT_MAJOR in
    4) SERVICES="mender-authd mender-updated" ;;
    3) SERVICES="mender-client" ;;
    *) SERVICES= ;;
esac
for svc in $SERVICES; do
    if [ $LIVE = 1 ]; then
        have systemctl || { info "no systemctl; service state not checked"; break; }
        en=$(systemctl is-enabled "$svc" 2>/dev/null)
        ac=$(systemctl is-active "$svc" 2>/dev/null)
        if [ "$ac" = active ]; then
            ok "$svc.service is running (${en:-unknown})"
        else
            warn "$svc.service is ${ac:-unknown} (enabled: ${en:-unknown})"
            hint "fine for standalone updates only; to check: journalctl -u $svc"
        fi
    else
        found=
        for w in "$ROOT"/etc/systemd/system/*.wants/"$svc".service \
                 "$ROOT"/lib/systemd/system/*.wants/"$svc".service \
                 "$ROOT"/usr/lib/systemd/system/*.wants/"$svc".service; do
            [ -e "$w" ] || [ -L "$w" ] && { found=1; break; }
        done
        if [ -n "$found" ]; then
            ok "$svc.service is enabled"
        elif [ -d "$ROOT/etc/systemd" ]; then
            warn "$svc.service is not enabled, so it won't start at boot"
        fi
    fi
done

# ---------------------------------------------------------------- config
section "Configuration"
nkeys() { printf '%s\n' "$1" | awk -F "$TAB" 'NF && $1 !~ /\./' | wc -l | tr -d ' '; }
if load_conf "$CONF_MAIN"; then
    FLAT_MAIN=$LOADED
    ok "$CONF_MAIN: valid JSON, $(nkeys "$FLAT_MAIN") settings"
    check_conf_keys "$CONF_MAIN" "$FLAT_MAIN"
fi
if load_conf "$CONF_FALLBACK"; then
    FLAT_FALLBACK=$LOADED
    ok "$CONF_FALLBACK: valid JSON, $(nkeys "$FLAT_FALLBACK") settings (overridden by $CONF_MAIN)"
    check_conf_keys "$CONF_FALLBACK" "$FLAT_FALLBACK"
fi
if [ ! -e "$(rp "$CONF_MAIN")" ] && [ ! -e "$(rp "$CONF_FALLBACK")" ]; then
    fail "no configuration: neither $CONF_MAIN nor $CONF_FALLBACK exists"
fi

# Server
has_servers=0 has_url=0
for flat in "$FLAT_MAIN" "$FLAT_FALLBACK"; do
    [ -n "$(lookup "$flat" Servers)" ] && has_servers=1
    [ -n "$(lookup "$flat" ServerURL)" ] && has_url=1
done
SERVER_URL= TOKEN=
if [ $has_servers = 1 ] && [ $has_url = 1 ]; then
    fail "both \"Servers\" and \"ServerURL\" are set; the client refuses to start"
    hint "keep only one (they may be in different files)"
fi
if cget ServerURL && [ "$T" = s ]; then
    SERVER_URL=$V; url_src=$SRC
    cget TenantToken && [ "$T" = s ] && TOKEN=$V
elif cget Servers.0.ServerURL; then
    SERVER_URL=$V; url_src=$SRC
    cget Servers.0.TenantToken && TOKEN=$V
    cget TenantToken && [ -z "$TOKEN" ] && TOKEN=$V
fi
if [ -z "$SERVER_URL" ] && [ $CONF_UNREADABLE = 1 ]; then
    info "server settings not checked (configuration not readable)"
elif [ -z "$SERVER_URL" ]; then
    warn "no server configured (ServerURL); only standalone updates will work"
else
    case $SERVER_URL in
        *' '*|*"$TAB"*) fail "ServerURL \"$SERVER_URL\" contains whitespace" ;;
        https://*) ok "ServerURL $SERVER_URL (from $url_src)" ;;
        http://*) warn "ServerURL $SERVER_URL uses plain http" ;;
        *) fail "ServerURL \"$SERVER_URL\" must start with https://" ;;
    esac
    case $SERVER_URL in */) warn "ServerURL ends with /; remove the trailing slash" ;; esac
    host=${SERVER_URL#*://}; host=${host%%/*}; host=${host%%:*}
    case $host in
        hosted.mender.io|*.hosted.mender.io)
            if [ -z "$TOKEN" ]; then
                fail "$host is hosted Mender, which needs a TenantToken"
                hint "copy it from the Mender UI: Settings > Organization and billing"
            fi ;;
        docker.mender.io) info "$host is the demo server address" ;;
    esac
    if [ -n "$TOKEN" ]; then
        case $TOKEN in
            *.*.*) ok "TenantToken set" ;;
            *) warn "TenantToken \"$TOKEN\" doesn't look like a token (expected three dot-separated parts)" ;;
        esac
    fi
fi

# Referenced files
check_ref() {  # check_ref KEY PATH PATTERN WHAT
    local real
    case $2 in pkcs11:*|engine:*) return ;; esac
    real=$(canon "$2") || real=$2
    if [ ! -e "$ROOT$real" ]; then
        fail "$1 points at $2, which does not exist"
    elif [ ! -r "$ROOT$real" ]; then
        info "$1: $2 present (not readable without root)"
    elif ! grep -q "$3" "$ROOT$real"; then
        fail "$1: $2 is not a PEM $4"
    else
        ok "$1: $2"
    fi
}
cget ServerCertificate && [ "$T" = s ] && check_ref ServerCertificate "$V" 'BEGIN CERTIFICATE' certificate
cget ArtifactVerifyKey && [ "$T" = s ] && check_ref ArtifactVerifyKey "$V" 'PUBLIC KEY' "public key"
i=0
while cget "ArtifactVerifyKeys.$i"; do
    [ "$T" = s ] && check_ref "ArtifactVerifyKeys[$i]" "$V" 'PUBLIC KEY' "public key"
    i=$((i + 1))
done
cget HttpsClient.Certificate && [ -n "$V" ] && check_ref HttpsClient.Certificate "$V" 'BEGIN CERTIFICATE' certificate
cget HttpsClient.Key && [ -n "$V" ] && check_ref HttpsClient.Key "$V" 'PRIVATE KEY' "private key"

# A/B partitions
ROOTFS_MODULE=$DATA_DIR/modules/v3/rootfs-image
PART_A= PART_B=
cget RootfsPartA && PART_A=$V
cget RootfsPartB && PART_B=$V
if [ $CONF_UNREADABLE = 1 ] && { [ -z "$PART_A" ] || [ -z "$PART_B" ]; }; then
    info "RootfsPartA/B not checked (configuration not readable)"
elif [ -z "$PART_A" ] && [ -z "$PART_B" ]; then
    if [ -e "$(rp "$ROOTFS_MODULE")" ] || [ "$CLIENT_MAJOR" = 3 ]; then
        warn "RootfsPartA/B are not set, so rootfs-image (full system) deployments will fail"
        hint "fine if this device only receives application updates"
    else
        info "no RootfsPartA/B: no A/B rootfs updates (application updates only)"
    fi
elif [ -z "$PART_A" ] || [ -z "$PART_B" ]; then
    fail "only one of RootfsPartA and RootfsPartB is set; rootfs-image updates need both"
elif [ "$PART_A" = "$PART_B" ]; then
    fail "RootfsPartA and RootfsPartB are the same ($PART_A)"
else
    ok "RootfsPartA=$PART_A RootfsPartB=$PART_B"
    for p in "$PART_A" "$PART_B"; do
        printf '%s' "$p" | grep -Eq '[0-9]+$' \
            || fail "$p doesn't end in a partition number, which the rootfs-image module needs"
    done
fi

ACTIVE_NUM=
if [ $LIVE = 1 ] && [ -n "$PART_A" ] && [ -n "$PART_B" ]; then
    RA=$(resolve_part "$PART_A") RB=$(resolve_part "$PART_B")
    for p in "$RA" "$RB"; do
        [ -b "$p" ] || [ -c "$p" ] || fail "$p does not exist on this device"
    done
    if RUN_ROOT=$(running_root); then
        if [ "$RUN_ROOT" = "$RA" ]; then
            ok "running from RootfsPartA ($RUN_ROOT)"; ACTIVE_NUM=$(printf '%s' "$RA" | grep -Eo '[0-9]+$')
        elif [ "$RUN_ROOT" = "$RB" ]; then
            ok "running from RootfsPartB ($RUN_ROOT)"; ACTIVE_NUM=$(printf '%s' "$RB" | grep -Eo '[0-9]+$')
        else
            fail "the running root ($RUN_ROOT) is neither RootfsPartA ($RA) nor RootfsPartB ($RB)"
        fi
    else
        info "couldn't work out which partition is the running root"
    fi
fi

for k in UpdatePollIntervalSeconds InventoryPollIntervalSeconds RetryPollIntervalSeconds; do
    cget "$k" && [ "$T" = n ] && info "$k: $V"
done


AB=0
[ -n "$PART_A" ] && [ -n "$PART_B" ] && AB=1

# ---------------------------------------------------------------- data store
section "Data store ($STORE_DIR)"
STORE_REAL=$(canon "$STORE_DIR") || {
    fail "$STORE_DIR: symlink loop"
    STORE_REAL=
}
STORE_OK=0
if [ -z "$STORE_REAL" ]; then
    :
elif [ -L "$ROOT$STORE_DIR" ]; then
    tgt=$(readlink "$ROOT$STORE_DIR")
    if [ -d "$ROOT$STORE_REAL" ]; then
        ok "$STORE_DIR -> $tgt"
        STORE_OK=1
    elif [ $LIVE = 0 ]; then
        info "$STORE_DIR -> $tgt, which is not in this filesystem"
        hint "normal for an image: the data partition is separate, so the"
        hint "store, device_type and key can't be checked here"
    else
        fail "$STORE_DIR -> $tgt, but $STORE_REAL does not exist (broken symlink)"
        hint "is the data partition mounted? otherwise: mkdir -p $STORE_REAL"
    fi
    case $STORE_REAL in
        /data/*) ;;
        *) [ $AB = 1 ] && warn "$STORE_DIR resolves to $STORE_REAL; the standard layout is /data/mender on the data partition" ;;
    esac
elif [ -d "$ROOT$STORE_DIR" ]; then
    if [ "$STORE_REAL" != "$STORE_DIR" ]; then
        ok "$STORE_DIR is a directory (really $STORE_REAL)"
    else
        ok "$STORE_DIR is a directory"
    fi
    STORE_OK=1
    if [ $LIVE = 0 ] && [ $AB = 1 ] && \
            ! awk -v d="$STORE_DIR" '$2 == d { f = 1 } END { exit !f }' "$ROOT/etc/fstab" 2>/dev/null; then
        warn "$STORE_DIR is a plain directory on the root filesystem, not a symlink to /data/mender"
        hint "with A/B updates its contents are replaced by every rootfs update"
    fi
elif [ -e "$ROOT$STORE_DIR" ]; then
    fail "$STORE_DIR exists but is not a directory"
else
    fail "$STORE_DIR does not exist"
    hint "the standard layout is a symlink: ln -s /data/mender $STORE_DIR"
fi

if [ $LIVE = 1 ] && [ $STORE_OK = 1 ]; then
    set -- $(mount_of "$STORE_REAL")
    store_dev=${1:-?} store_mnt=${2:-?}
    case $STORE_REAL in
        /data/*)
            set -- $(mount_of /data)
            if [ "${2:-}" != /data ]; then
                fail "/data is not mounted, so the store is on the root filesystem"
                hint "check the /data line in /etc/fstab"
            fi ;;
    esac
    if [ "$store_mnt" = / ] && [ $AB = 1 ]; then
        fail "the store is on the root filesystem ($store_dev)"
        hint "it must be on a partition that A/B updates don't replace, usually /data"
    elif [ "$store_mnt" = / ]; then
        info "the store is on the root filesystem ($store_dev); fine without A/B rootfs updates"
    else
        ok "on $store_dev, mounted at $store_mnt"
    fi
    opts=$(awk -v m="$store_mnt" '$2 == m { o = $4 } END { print o }' /proc/mounts)
    case ,$opts, in
        *,ro,*) fail "$store_mnt is mounted read-only; the client can't save its state" ;;
    esac
elif [ $LIVE = 0 ] && { [ $STORE_OK = 1 ] || [ -L "$ROOT$STORE_DIR" ]; }; then
    case $STORE_REAL in
        /data/*)
            unit=
            for u in /etc/systemd/system/data.mount /lib/systemd/system/data.mount \
                    /usr/lib/systemd/system/data.mount; do
                [ -f "$ROOT$u" ] && { unit=$u; break; }
            done
            if [ -n "$unit" ]; then
                ok "$unit mounts /data"
            elif [ -f "$ROOT/etc/fstab" ]; then
                if awk '$1 !~ /^#/ && $2 == "/data" { f = 1 } END { exit !f }' "$ROOT/etc/fstab"; then
                    ok "/etc/fstab mounts /data"
                else
                    fail "/etc/fstab has no entry for /data, so the store would end up on the root filesystem"
                fi
            fi ;;
    esac
fi

if [ $STORE_OK = 1 ]; then
    STORE_FILE=$STORE_REAL/mender-store
    if [ -f "$ROOT$STORE_FILE" ]; then
        if [ ! -r "$ROOT$STORE_FILE" ]; then
            info "mender-store present (not readable without root)"
        else
            size=$(wc -c < "$ROOT$STORE_FILE" | tr -d ' ')
            magic=$(hexbytes "$ROOT$STORE_FILE" 12 8)
            bits=
            case $magic in
                dec0efbe*) bits=32 ;;
                ????????dec0efbe) bits=64 ;;
            esac
            if [ -z "$bits" ]; then
                fail "mender-store ($size bytes) is not an LMDB database"
                hint "move it aside; the client creates a fresh one, but loses the artifact name it recorded"
            else
                ok "mender-store present ($size bytes, written by a $bits-bit client)"
                if [ -n "$CLIENT_BITS" ] && [ "$CLIENT_BITS" != "$bits" ]; then
                    fail "the store was written by a $bits-bit client but this client is $CLIENT_BITS-bit"
                    hint "it can't open the store (MDB_INVALID); was it copied from another device?"
                fi
            fi
        fi
    elif [ -e "$ROOT$STORE_FILE" ]; then
        fail "$STORE_FILE exists but is not a regular file"
        hint "the client uses a single-file LMDB store, not a directory"
    else
        warn "no mender-store in $STORE_REAL"
        hint "normal before the client's first start; otherwise it has lost its state"
    fi

    # Copies in places the client never reads are a common mistake.
    for cand in /data/mender-store /data/mender/mender/mender-store \
            "$STORE_DIR/mender/mender-store" "$CONF_DIR/mender-store" \
            /var/lib/mender-store /mender-store; do
        [ -f "$(rp "$cand")" ] || continue
        real=$(canon "$cand") || continue
        [ "$real" = "$STORE_FILE" ] && continue
        warn "found a mender-store at $cand, but the client only reads $STORE_DIR/mender-store"
    done

    # device_type
    if cget DeviceTypeFile; then DT_FILE=$V; else DT_FILE=$STORE_DIR/device_type; fi
    dt_real=$(canon "$DT_FILE") || dt_real=$DT_FILE
    if [ ! -f "$ROOT$dt_real" ]; then
        fail "$DT_FILE is missing"
        hint "the client can't tell the server its device type; create it with:"
        hint "echo device_type=<your-type> > $DT_FILE"
    else
        dt_line=$(head -n 1 "$ROOT$dt_real")
        dt_extra=$(sed -n '2,$p' "$ROOT$dt_real" | tr -d ' \t\r\n')
        case $dt_line in
            device_type=?*)
                if [ -n "$dt_extra" ]; then
                    fail "$DT_FILE has more than one line; the client rejects it (\"Trailing device_type data\")"
                else
                    ok "$dt_line"
                    case $dt_line in *"$(printf '\r')"|*' ') warn "$DT_FILE ends in whitespace or a CR, which becomes part of the device type" ;; esac
                fi ;;
            *) fail "$DT_FILE should be one line \"device_type=<type>\", but starts with \"$dt_line\"" ;;
        esac
    fi

    # Device key
    KEY_FILE=$STORE_DIR/mender-agent.pem
    cget Security.AuthPrivateKey && KEY_FILE=$V
    case $KEY_FILE in
        pkcs11:*|engine:*) info "device key is in an HSM ($KEY_FILE)" ;;
        *)
            key_real=$(canon "$KEY_FILE") || key_real=$KEY_FILE
            if [ ! -e "$ROOT$key_real" ]; then
                warn "no device key at $KEY_FILE"
                hint "normal before the first start; mender-auth generates it"
            elif [ ! -r "$ROOT$key_real" ]; then
                info "device key present (not readable without root)"
            elif grep -q 'PRIVATE KEY' "$ROOT$key_real"; then
                mode=$(stat -c %a "$ROOT$key_real" 2>/dev/null)
                case $mode in
                    *[1-7]?|*[1-7]) warn "device key $KEY_FILE has mode $mode; it should only be readable by root (600)" ;;
                    *) ok "device key present${mode:+ (mode $mode)}" ;;
                esac
            else
                fail "$KEY_FILE is not a PEM private key"
            fi ;;
    esac
fi

# ---------------------------------------------------------------- device files
section "Identity, inventory and update modules"
ID_SCRIPT=$DATA_DIR/identity/mender-device-identity
if [ ! -f "$(rp "$ID_SCRIPT")" ]; then
    fail "$ID_SCRIPT is missing; the device can't identify itself to the server"
elif [ ! -x "$(rp "$ID_SCRIPT")" ]; then
    fail "$ID_SCRIPT is not executable"
elif [ $LIVE = 1 ]; then
    id_out=$("$ID_SCRIPT" 2>/dev/null)
    id_rc=$?
    id_kv=$(printf '%s\n' "$id_out" | grep -c '^[^=][^=]*=..*')
    if [ $id_rc -ne 0 ]; then
        fail "$ID_SCRIPT exited with status $id_rc"
    elif [ "$id_kv" -eq 0 ]; then
        fail "$ID_SCRIPT printed no key=value lines"
    else
        ok "identity: $(printf '%s\n' "$id_out" | grep '=' | tr '\n' ' ' | sed 's/ $//')"
    fi
else
    ok "$ID_SCRIPT present"
fi

exec_dir_check "$DATA_DIR/inventory" "inventory script"
if [ "$COUNT" -eq 0 ]; then
    warn "no usable inventory scripts in $DATA_DIR/inventory; the server will show no inventory"
else
    ok "$COUNT inventory script(s)"
fi

exec_dir_check "$DATA_DIR/modules/v3" "update module"
if [ "$COUNT" -gt 0 ]; then
    ok "update modules: $NAMES"
else
    warn "no update modules in $DATA_DIR/modules/v3"
fi

exec_dir_check "$CONF_DIR/scripts" "state script" version
if [ "$COUNT" -gt 0 ]; then
    if [ "$(cat "$(rp "$CONF_DIR/scripts/version")" 2>/dev/null)" = 3 ]; then
        ok "$COUNT rootfs state script(s) in $CONF_DIR/scripts"
    else
        fail "$CONF_DIR/scripts has state scripts but no version file containing 3"
        hint "the client won't run them; fix with: echo 3 > $CONF_DIR/scripts/version"
    fi
fi

if [ -f "$(rp "$CONF_DIR/artifact_info")" ]; then
    an=$(sed -n 's/^artifact_name=//p' "$(rp "$CONF_DIR/artifact_info")" | head -n 1)
    if [ -n "$an" ]; then ok "artifact_info: $an"
    else warn "$CONF_DIR/artifact_info has no artifact_name= line"; fi
elif [ $AB = 1 ]; then
    warn "$CONF_DIR/artifact_info is missing; images built by Yocto or mender-convert always have it"
else
    info "no $CONF_DIR/artifact_info (normal for a package install)"
fi
if [ $LIVE = 1 ] && [ $IS_ROOT = 1 ] && [ -n "$CLIENT_BIN" ]; then
    cur=$("$CLIENT_BIN" show-artifact 2>/dev/null | tail -n 1)
    [ -n "$cur" ] && info "client reports the installed artifact as: $cur"
fi

# ---------------------------------------------------------------- boot
if [ $LIVE = 1 ] && [ -n "$PART_A" ]; then
    section "Bootloader environment"
    if have grub-mender-grubenv-print; then
        PRINTENV=grub-mender-grubenv-print
    elif have fw_printenv; then
        PRINTENV=fw_printenv
        [ -f /etc/fw_env.config ] || fail "/etc/fw_env.config is missing; fw_printenv can't find the U-Boot environment"
    else
        PRINTENV=
        fail "neither fw_printenv nor grub-mender-grubenv-print is installed"
    fi
    if [ -n "$PRINTENV" ]; then
        bp=$($PRINTENV mender_boot_part 2>/dev/null); bp=${bp#mender_boot_part=}
        ua=$($PRINTENV upgrade_available 2>/dev/null); ua=${ua#upgrade_available=}
        if [ -z "$bp" ]; then
            fail "can't read mender_boot_part with $PRINTENV"; [ $IS_ROOT = 0 ] && hint "run as root"
        elif [ -n "$ACTIVE_NUM" ] && [ "$bp" != "$ACTIVE_NUM" ]; then
            fail "bootloader says mender_boot_part=$bp but the device is running from partition $ACTIVE_NUM"
            hint "the next update will write to the partition you're running from"
        else
            ok "mender_boot_part=$bp"
        fi
        case $ua in
            0) ok "upgrade_available=0 (no update waiting for commit)" ;;
            1) warn "upgrade_available=1: an update is installed and not yet committed" ;;
            '') ;;
            *) warn "upgrade_available=$ua (expected 0 or 1)" ;;
        esac
        canary=$($PRINTENV mender_check_saveenv_canary 2>/dev/null)
        if [ "$canary" = "mender_check_saveenv_canary=1" ]; then
            if [ "$($PRINTENV mender_saveenv_canary 2>/dev/null)" = "mender_saveenv_canary=1" ]; then
                ok "bootloader can save its environment"
            else
                fail "bootloader couldn't save its environment (mender_saveenv_canary not set)"
                hint "check the bootloader integration; rollback won't work"
            fi
        fi
    fi
fi

# ---------------------------------------------------------------- server
if [ $LIVE = 1 ] && [ $NETWORK = 1 ] && [ -n "$SERVER_URL" ]; then
    section "Server"
    year=$(date +%Y)
    if [ "$year" -lt 2024 ]; then
        fail "the clock says $(date); TLS certificate checks will fail"
        hint "set the time or enable NTP"
    else
        ok "clock looks sane ($(date '+%Y-%m-%d %H:%M'))"
    fi
    if have curl; then
        code=$(curl -sS -o /dev/null --max-time 15 -w '%{http_code}' "$SERVER_URL" 2>&1)
        rc=$?
        if [ $rc = 60 ] && cget ServerCertificate; then
            code=$(curl -sS -o /dev/null --max-time 15 --cacert "$V" -w '%{http_code}' "$SERVER_URL" 2>&1)
            rc=$?
        fi
        if [ $rc = 0 ]; then
            ok "$SERVER_URL is reachable (HTTP $code)"
        else
            fail "can't reach $SERVER_URL: $(printf '%s' "$code" | sed 's/000$//' | head -n 1)"
        fi
    elif have wget; then
        if wget -q -T 15 -O /dev/null "$SERVER_URL" 2>/dev/null; then
            ok "$SERVER_URL is reachable"
        else
            warn "wget couldn't fetch $SERVER_URL (DNS, network or TLS problem)"
        fi
    else
        info "no curl or wget; server reachability not checked"
    fi
fi

printf '\n%s ok, %s warning(s), %s failure(s)\n' "$N_OK" "$N_WARN" "$N_FAIL"
[ "$N_FAIL" -eq 0 ]
