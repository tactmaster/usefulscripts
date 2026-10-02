# usefulscripts

Small, standalone scripts for everyday fleet and admin jobs. Each script
has as few dependencies as possible and explains what it does as it
runs.

| Script | What it does | Needs |
|---|---|---|
| [mender/set_device_name_from_attribute.sh](mender/set_device_name_from_attribute.sh) | Sets each Mender device's name from an attribute of your choice, such as its serial number or MAC address. Prints every API call as a curl command you can copy. | bash, curl, jq |
| [mender/lmdb-dump.py](mender/lmdb-dump.py) | Prints the contents of a single-file LMDB database, such as Mender's `mender-store`. Reads stores written by 32-bit devices, which the standard `mdb_dump` rejects as `MDB_INVALID` on a 64-bit machine. | python3 (standard library only) |

See [mender/README.md](mender/README.md) for usage.

## Tests

The tests run the scripts against a fake `curl`, so they need no
network or credentials:

```bash
pip install -r requirements-dev.txt
pytest tests/
```

They also run on every push and pull request (see
[.github/workflows/test.yml](.github/workflows/test.yml)).

## Related

The same device-name tool, plus a Python SDK for the Mender API, is in
[tactmaster/pymenderai](https://github.com/tactmaster/pymenderai).

## License

[Apache License 2.0](LICENSE)
