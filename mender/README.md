# Mender scripts

These scripts talk to the [Mender](https://mender.io) management API.
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
