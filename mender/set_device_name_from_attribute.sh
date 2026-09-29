#!/usr/bin/env bash
#
# Set each device's name (server side) from one of its attributes.
#
# Needs only bash, curl and jq. The device name shown in the Mender UI
# is the "name" attribute in the "tags" scope; this copies an attribute
# you choose (e.g. serial_number) into it with
#   PATCH /api/management/v1/inventory/devices/{id}/tags
# which only adds or updates the tags it's given.
#
# Every call is printed as a curl command you can copy, using
# $MENDER_API_TOKEN in place of the token.
#
# Usage:
#   ./set_device_name_from_attribute.sh serial_number --dry-run
#   ./set_device_name_from_attribute.sh serial_number
#   ./set_device_name_from_attribute.sh serial_number --scope identity \
#       --device-id <id> --device-id <id> --overwrite
#
# Environment (or a .env file next to this script or in the current
# directory; variables already set in the shell win):
#   MENDER_API_TOKEN   personal access token (required)
#   MENDER_SERVER_URL  default https://hosted.mender.io
#                      (EU tenants: https://eu.hosted.mender.io)

set -euo pipefail

PER_PAGE=500
DEFAULT_SCOPE_ORDER='["inventory","identity","system"]'

usage() {
    cat <<'EOF'
usage: set_device_name_from_attribute.sh ATTRIBUTE [--scope SCOPE]
         [--device-id ID]... [--overwrite] [--dry-run] [--no-curl]

  ATTRIBUTE        name of the attribute to copy into the device name,
                   e.g. serial_number or mac (a name, not a value)
  --scope SCOPE    only read it from inventory|identity|system|tags
                   (default: first found in inventory, identity, system)
  --device-id ID   only process this device (repeatable)
  --overwrite      replace names that are already set
  --dry-run        read inventory but only print the rename calls
  --no-curl        don't print the API calls as curl commands
EOF
}

# Load KEY=VALUE lines without executing the file; shell values win.
load_env_file() {
    local file=$1 line key value
    [ -f "$file" ] || return 0
    while IFS= read -r line || [ -n "$line" ]; do
        line="${line#"${line%%[![:space:]]*}"}"
        [[ -z "$line" || "$line" == \#* || "$line" != *=* ]] && continue
        line="${line#export }"
        key="${line%%=*}"
        key="${key%"${key##*[![:space:]]}"}"
        value="${line#*=}"
        value="${value#"${value%%[![:space:]]*}"}"
        value="${value%"${value##*[![:space:]]}"}"
        [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
        if [[ ${#value} -ge 2 && ( "$value" == \"*\" || "$value" == \'*\' ) ]]; then
            value="${value:1:${#value}-2}"
        fi
        if [ -z "${!key+set}" ]; then
            export "$key=$value"
        fi
    done <"$file"
}

# Quote a string for pasting into a shell.
sq() {
    printf "'%s'" "${1//\'/\'\\\'\'}"
}

print_curl() {
    local method=$1 url=$2 body=${3-}
    [ "$SHOW_CURL" = 1 ] || return 0
    printf 'curl -X %s \\\n  %s \\\n  -H "Authorization: Bearer $MENDER_API_TOKEN"' \
        "$method" "$(sq "$url")"
    if [ -n "$body" ]; then
        printf " \\\\\n  -H 'Content-Type: application/json' \\\\\n  --data-raw %s" \
            "$(sq "$body")"
    fi
    printf '\n'
}

# api METHOD URL [BODY] -> response body on stdout; exits on HTTP errors.
api() {
    local method=$1 url=$2 body=${3-} response status
    print_curl "$method" "$url" "$body" >&3
    local args=(-sS -X "$method" -H "Authorization: Bearer $MENDER_API_TOKEN"
        -w '\n%{http_code}')
    [ -n "$body" ] && args+=(-H 'Content-Type: application/json' --data-raw "$body")
    response=$(curl "${args[@]}" "$url") || {
        echo "error: request to $url failed" >&2
        exit 1
    }
    status=${response##*$'\n'}
    response=${response%$'\n'*}
    if [ "$status" -ge 400 ]; then
        echo >&2
        echo "error: $method $url returned HTTP $status" >&2
        case "$status" in
        401)
            cat >&2 <<EOF
The server at $SERVER_URL rejected the token. Check that
MENDER_API_TOKEN is a valid personal access token, and that
MENDER_SERVER_URL is your tenant's server: a token only works
on its own region, e.g. EU tenants need
  export MENDER_SERVER_URL=https://eu.hosted.mender.io
EOF
            ;;
        403)
            echo "The token is valid but not allowed to do this. It needs" >&2
            echo "permission to read and manage device inventory." >&2
            ;;
        esac
        exit 1
    fi
    printf '%s' "$response"
}

# One TSV line per device: id, attribute value ("" if absent), current name.
extract='
.[] | . as $d
| [$d.attributes[]? | select(.name == $attr)] as $matches
| (first($order[] as $s | $matches[] | select((.scope // "inventory") == $s)
    | .value) // null) as $v
| [$d.attributes[]? | select(.name == "name" and .scope == "tags") | .value][0]
    as $n
| [$d.id,
   (if $v == null then ""
    elif ($v | type) == "array" then ($v | map(tostring) | join(","))
    else ($v | tostring) end),
   ($n // "" | tostring)]
| @tsv'

attribute=""
scope=""
device_ids=()
overwrite=0
dry_run=0
SHOW_CURL=1
while [ $# -gt 0 ]; do
    case "$1" in
    --scope)
        scope=${2:?--scope needs a value}
        shift 2
        ;;
    --device-id)
        device_ids+=("${2:?--device-id needs a value}")
        shift 2
        ;;
    --overwrite) overwrite=1; shift ;;
    --dry-run) dry_run=1; shift ;;
    --no-curl) SHOW_CURL=0; shift ;;
    -h | --help) usage; exit 0 ;;
    -*) echo "error: unknown option $1" >&2; usage >&2; exit 2 ;;
    *)
        if [ -n "$attribute" ]; then
            echo "error: only one attribute can be given" >&2
            exit 2
        fi
        attribute=$1
        shift
        ;;
    esac
done
if [ -z "$attribute" ]; then
    usage >&2
    exit 2
fi
case "$scope" in
"") order=$DEFAULT_SCOPE_ORDER ;;
inventory | identity | system | tags) order="[\"$scope\"]" ;;
*) echo "error: --scope must be inventory, identity, system or tags" >&2; exit 2 ;;
esac

# curl commands go to fd 3 (stdout) even from inside $(...).
exec 3>&1

for tool in curl jq; do
    command -v "$tool" >/dev/null || { echo "error: $tool is required" >&2; exit 2; }
done

load_env_file "$(dirname "$0")/.env"
load_env_file "./.env"
if [ -z "${MENDER_API_TOKEN-}" ]; then
    echo "error: MENDER_API_TOKEN is not set" >&2
    exit 2
fi
SERVER_URL=${MENDER_SERVER_URL:-https://hosted.mender.io}
SERVER_URL=${SERVER_URL%/}
INVENTORY_URL="$SERVER_URL/api/management/v1/inventory/devices"

# Collect every device inventory as one JSON array.
devices_file=$(mktemp)
trap 'rm -f "$devices_file" "$devices_file.pages"' EXIT
if [ ${#device_ids[@]} -gt 0 ]; then
    for id in "${device_ids[@]}"; do
        api GET "$INVENTORY_URL/$(jq -rn --arg x "$id" '$x | @uri')" | jq -c '[.]'
    done | jq -s 'add' >"$devices_file"
else
    page=1
    : >"$devices_file.pages"
    while true; do
        batch=$(api GET "$INVENTORY_URL?page=$page&per_page=$PER_PAGE")
        printf '%s\n' "$batch" >>"$devices_file.pages"
        count=$(jq 'length' <<<"$batch")
        [ "$count" -lt "$PER_PAGE" ] && break
        page=$((page + 1))
    done
    jq -s 'add // []' "$devices_file.pages" >"$devices_file"
    rm -f "$devices_file.pages"
fi

renamed=0 unchanged=0 has_name=0 missing=0
while IFS=$'\t' read -r id value existing; do
    if [ -z "$value" ]; then
        echo "# $id: no '$attribute' attribute, skipped"
        missing=$((missing + 1))
    elif [ "$existing" = "$value" ]; then
        echo "# $id: already named '$value'"
        unchanged=$((unchanged + 1))
    elif [ -n "$existing" ] && [ "$overwrite" = 0 ]; then
        echo "# $id: has name '$existing', skipped (use --overwrite to replace)"
        has_name=$((has_name + 1))
    else
        body=$(jq -cn --arg v "$value" '[{name: "name", value: $v}]')
        url="$INVENTORY_URL/$(jq -rn --arg x "$id" '$x | @uri')/tags"
        echo "# $id: name -> '$value'"
        if [ "$dry_run" = 1 ]; then
            echo "# [dry-run] not sent:"
            SHOW_CURL=1 print_curl PATCH "$url" "$body" >&3
        else
            api PATCH "$url" "$body" >/dev/null
        fi
        renamed=$((renamed + 1))
    fi
done < <(jq -r --arg attr "$attribute" --argjson order "$order" "$extract" \
    "$devices_file")

total=$((renamed + unchanged + has_name + missing))
if [ "$missing" -gt 0 ] && [ "$missing" = "$total" ]; then
    echo
    echo "# No device has an attribute named '$attribute'. The argument is"
    echo "# an attribute name, not a value."
    echo "# Attribute names on your devices (scope):"
    echo "#   $(jq -r '[.[].attributes[]? | "\(.name) (\(.scope // "inventory"))"]
        | unique | join(", ")' "$devices_file")"
fi

verb=renamed
[ "$dry_run" = 1 ] && verb="would rename"
echo
echo "$verb $renamed, already named $unchanged, kept existing name $has_name," \
    "missing '$attribute' $missing"
