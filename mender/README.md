# Mender scripts

`mender-check.sh`, `lmdb-dump.py` and `lmdb-dump.sh` run on a device or on files
you already have and need no API token -- skip to
[mender-check.sh](#mender-checksh) and [lmdb-dump.py](#lmdb-dumppy) for those.

The remaining scripts talk to the [Mender](https://mender.io) management API.
Set your server and a personal access token (Mender UI: Settings, then
Personal access tokens):

```bash
export MENDER_SERVER_URL=https://hosted.mender.io   # EU tenants: https://eu.hosted.mender.io
export MENDER_API_TOKEN=<personal access token>
```

Instead of exporting them, you can put the same lines in a `.env` file in
this folder or in the directory you run from. The file is read line by
line, never executed, and it's git-ignored so the token isn't committed.
Variables already set in your shell take precedence.

A token only works on its own region's server. If you get
`401 Unauthorized`, check that `MENDER_SERVER_URL` matches your tenant.

## set_device_name_from_attribute.sh

Sets each device's name (the `name` tag, shown as the device name in the
Mender UI) from an attribute you choose, such as the `serial_number`
identity attribute or `mac`. The argument is the attribute's *name*, not
a value. If no device has it, the script lists the attribute names your
devices do have.

```bash
# Preview first: reads inventory and prints the rename calls without sending them
./mender/set_device_name_from_attribute.sh serial_number --dry-run

# Apply to every device that doesn't have a name yet
./mender/set_device_name_from_attribute.sh serial_number
```

| Option | Effect |
|---|---|
| `--scope inventory\|identity\|system\|tags` | Only read the attribute from this scope. By default the first match in inventory, identity, system is used. |
| `--device-id <id>` | Only process this device. Repeat for several. |
| `--overwrite` | Replace names that are already set. Without it, named devices are skipped. |
| `--dry-run` | Read inventory but only print the rename calls. |
| `--no-curl` | Don't print curl commands. |

Every API call is printed as a curl command you can paste into your own
scripts. The commands use `$MENDER_API_TOKEN` in place of your token.
Example dry run:

```text
curl -X GET \
  'https://hosted.mender.io/api/management/v1/inventory/devices?page=1&per_page=500' \
  -H "Authorization: Bearer $MENDER_API_TOKEN"
# 5f1c...: name -> 'SN-000123'
# [dry-run] not sent:
curl -X PATCH \
  'https://hosted.mender.io/api/management/v1/inventory/devices/5f1c.../tags' \
  -H "Authorization: Bearer $MENDER_API_TOKEN" \
  -H 'Content-Type: application/json' \
  --data-raw '[{"name":"name","value":"SN-000123"}]'

would rename 1, already named 0, kept existing name 0, missing 'serial_number' 0
```

How it works: it pages through
`GET /api/management/v1/inventory/devices`, picks the attribute out of
each device's `attributes` list, then sends
`PATCH /api/management/v1/inventory/devices/{id}/tags` with
`[{"name": "name", "value": "<value>"}]`. That call only adds or updates
the tags it's given, so the device's other tags are kept. The token
needs permission to manage device inventory.

## mender-check.sh

Checks that a device's Mender client install is healthy and prints one
OK / WARN / FAIL line per check, with a hint on how to fix each problem. It
exits 1 if anything failed, so it also works in scripts and CI.

```bash
scp mender/mender-check.sh device:/tmp/
ssh device sudo sh /tmp/mender-check.sh            # on the device, as root

./mender/mender-check.sh --root /mnt/rootfs        # a mounted image or rootfs
./mender/mender-check.sh --no-network              # skip the server check
```

It is plain POSIX sh, so it runs on busybox devices; `jq` and `python3` are
used for a stricter JSON check if present but not needed. Run it as root on a
device: `mender.conf`, the store and the device key are only readable by root,
and checks that need them are skipped (and say so) otherwise.

What it checks:

| Area | Checks |
|---|---|
| Client | `mender-update` + `mender-auth` (4.x and later) or `mender` (3.x) installed; services enabled and running |
| Configuration | `/etc/mender/mender.conf` and the fallback `/var/lib/mender/mender.conf` are valid JSON; unknown keys and wrong-case keys (`ServerUrl`); values of the wrong type, which the client silently ignores (`"UpdatePollIntervalSeconds": "1800"`); `Servers` and `ServerURL` both set (the client refuses to start); ServerURL scheme; TenantToken present for hosted Mender; files named by `ServerCertificate`, `ArtifactVerifyKey(s)` and `HttpsClient` exist; `RootfsPartA`/`B` set, different, existing, and one of them is the running root |
| Data store | `/var/lib/mender` exists and its symlink resolves (normally to `/data/mender`); it is on a separate, writable partition, not the root filesystem that A/B updates replace; `/data` is mounted (fstab or `data.mount`); `mender-store` is an LMDB file written with the same word size as the client (a 32-bit store can't be opened by a 64-bit client); stray copies of `mender-store` in places the client never reads; `device_type` is exactly one `device_type=...` line; device key is a private key with mode 600 |
| Device files | identity script exists, is executable and prints `key=value` lines; inventory scripts, update modules and state scripts are executable; state scripts have a `version` file; with no `mender-store` yet, a `bootstrap.mender` is there to set the artifact name (otherwise it becomes `unknown`); `artifact_info` only for the 3.x client, which is the only one that reads it |
| Bootloader | `fw_printenv` (with `/etc/fw_env.config`) or GRUB tools present; `mender_boot_part` matches the partition the device is running from; `upgrade_available` (an uncommitted update); the bootloader can save its environment |
| Server | the clock is sane (TLS fails otherwise); ServerURL is reachable |

With `--root`, checks that need the running system (mounts, bootloader,
service state, running the identity script, the server) are skipped. If
`/var/lib/mender` points at a data partition that isn't part of the image, the
store checks are skipped too.

Paths follow the client's own defaults and its `MENDER_CONF_DIR`,
`MENDER_DATASTORE_DIR` and `MENDER_DATA_DIR` overrides. To look inside the store
itself, use [lmdb-dump.sh](#lmdb-dumpsh).

## lmdb-dump.py

Prints what is inside a single-file LMDB database. Mender's `mender-store` is
one, so this is a way to see what a device has recorded about itself: the
artifact name it believes it is running, its device type, and where it is in an
update. It reads a file you already have, so it needs no server access and no
API token.

```bash
./mender/lmdb-dump.py mender-store                # stats, then every key/value
./mender/lmdb-dump.py mender-store --stats-only   # just the header stats
```

On a device the store is at `/var/lib/mender/mender-store`. Copy it off before
reading it -- the script never writes, but a store that a running client is
updating can be caught mid-write and show a torn page.

```bash
scp device:/var/lib/mender/mender-store .
./mender/lmdb-dump.py mender-store
```

Values that parse as JSON are pretty-printed, so the state entry is readable
rather than one long line. It uses only the Python standard library, so it runs
anywhere python3 does -- no `lmdb` module, no LMDB command-line tools.

Nothing in it is Mender-specific; it works on any single-file (`MDB_NOSUBDIR`)
LMDB database.

### lmdb-dump.sh

The same reader in bash, for machines that have no python3 at all -- the usual
case being the device itself, so you can read the store in place over ssh
instead of copying it off first (still prefer a copy if the client is running;
see above):

```bash
./mender/lmdb-dump.sh mender-store                # same output as the .py
./mender/lmdb-dump.sh mender-store --stats-only
```

It needs bash (ash alone is not enough, but the other tools can be busybox
applets) plus one of `od`, `xxd`, or `hexdump` -- whichever it finds -- and
`dd`. JSON values are pretty-printed when `jq` or `python3` is available and
printed as-is otherwise. The two scripts produce
identical output and the test suite checks that they stay that way. The bash
version is noticeably slower on large stores (hundreds of entries); for a
typical `mender-store` with a dozen entries the difference doesn't matter.

### Stores from 32-bit devices

`mdb_dump` and `mdb_stat` are built for the word size of the machine running
them, so a store copied off a 32-bit device is rejected on a 64-bit workstation:

```
mdb_env_open failed, error -30793 MDB_INVALID: File is not an LMDB file
```

The file is usually fine -- on a 32-bit writer `pgno_t` and `size_t` are 4 bytes
rather than 8, so the meta page sits at a different offset. This script detects
the layout and reads both.

### Limits

- dupsort / LEAF2 (dupfixed) databases are not supported; the script exits with
  an error rather than printing something that looks plausible but is wrong.
- Sub-databases are listed as `<sub-database>` but not descended into.
