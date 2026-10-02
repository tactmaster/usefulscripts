# Mender scripts

`lmdb-dump.py` works on a file you already have and needs no server access --
skip to [lmdb-dump.py](#lmdb-dumppy) for that one.

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
