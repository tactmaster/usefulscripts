"""
Tests for mender/mender-check.sh.

Each test builds a fake device filesystem under tmp_path and checks it with
``--root``, so no Mender install, root access or network is needed. The
script targets busybox devices, so every test runs under each POSIX shell
available (dash, busybox sh), not just bash.
"""

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from test_lmdb_dump import _build_store

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "mender" / "mender-check.sh"

ELF64 = b"\x7fELF\x02" + b"\0" * 60
ELF32 = b"\x7fELF\x01" + b"\0" * 60
GOOD_CONF = {
    "ServerURL": "https://hosted.mender.io",
    "TenantToken": "aaa.bbb.ccc",
    "RootfsPartA": "/dev/mmcblk0p2",
    "RootfsPartB": "/dev/mmcblk0p3",
    "UpdatePollIntervalSeconds": 1800,
}

SHELLS = [s for s in (["dash"], ["busybox", "sh"])
          if shutil.which(s[0])] or [["sh"]]


@pytest.fixture(params=SHELLS, ids=lambda s: " ".join(s))
def shell(request):
    return request.param


def _write(root, path, data, mode=0o644):
    p = root / path.lstrip("/")
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        p.write_bytes(data)
    else:
        p.write_text(data)
    p.chmod(mode)
    return p


def make_root(tmp_path, conf=GOOD_CONF, store_wide=True, client=ELF64):
    """A healthy Yocto-style layout: /var/lib/mender -> /data/mender."""
    r = tmp_path / "root"
    _write(r, "/usr/bin/mender-update", client, 0o755)
    _write(r, "/usr/bin/mender-auth", client, 0o755)
    if conf is not None:
        _write(r, "/etc/mender/mender.conf",
               conf if isinstance(conf, str) else json.dumps(conf, indent=2))
    _write(r, "/etc/mender/artifact_info", "artifact_name=release-1\n")
    _write(r, "/etc/fstab", "/dev/mmcblk0p4 /data auto defaults 0 0\n")
    (r / "var/lib").mkdir(parents=True)
    (r / "var/lib/mender").symlink_to("/data/mender")
    _write(r, "/data/mender/mender-store",
           _build_store([(b"artifact-name", b"release-1")], wide=store_wide))
    _write(r, "/data/mender/device_type", "device_type=raspberrypi4\n")
    _write(r, "/data/mender/mender-agent.pem",
           "-----BEGIN RSA PRIVATE KEY-----\nx\n"
           "-----END RSA PRIVATE KEY-----\n", 0o600)
    _write(r, "/usr/share/mender/identity/mender-device-identity",
           "#!/bin/sh\necho mac=00:11:22:33:44:55\n", 0o755)
    _write(r, "/usr/share/mender/inventory/mender-inventory-os",
           "#!/bin/sh\n", 0o755)
    _write(r, "/usr/share/mender/modules/v3/rootfs-image", "#!/bin/sh\n", 0o755)
    wants = r / "etc/systemd/system/multi-user.target.wants"
    wants.mkdir(parents=True)
    for svc in ("mender-authd", "mender-updated"):
        (wants / f"{svc}.service").symlink_to(f"/lib/systemd/system/{svc}.service")
    return r


def run(shell, root, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith("MENDER_")}
    return subprocess.run([*shell, str(SCRIPT), "--root", str(root), *args],
                          capture_output=True, text=True, env=env)


def lines(r, tag):
    return [ln for ln in r.stdout.splitlines() if f"[{tag}]" in ln]


def test_healthy_device_passes(shell, tmp_path):
    r = run(shell, make_root(tmp_path))
    assert r.returncode == 0, r.stdout + r.stderr
    assert lines(r, "FAIL") == [] and lines(r, "WARN") == [], r.stdout
    assert "/var/lib/mender -> /data/mender" in r.stdout
    assert "written by a 64-bit client" in r.stdout
    assert "device_type=raspberrypi4" in r.stdout
    assert r.stderr == ""


def test_does_not_modify_the_filesystem(shell, tmp_path):
    root = make_root(tmp_path)

    def snapshot():
        out = {}
        for p in sorted(root.rglob("*")):
            if p.is_file() and not p.is_symlink():
                out[str(p)] = (hashlib.sha256(p.read_bytes()).hexdigest(),
                               p.stat().st_mode)
        return out

    before = snapshot()
    run(shell, root)
    assert snapshot() == before


def test_missing_store_dir_fails(shell, tmp_path):
    root = make_root(tmp_path)
    (root / "var/lib/mender").unlink()
    r = run(shell, root)
    assert r.returncode == 1
    assert any("/var/lib/mender does not exist" in ln for ln in lines(r, "FAIL"))


def test_data_partition_not_in_image_is_not_a_failure(shell, tmp_path):
    root = make_root(tmp_path)
    shutil.rmtree(root / "data")
    r = run(shell, root)
    assert r.returncode == 0, r.stdout
    assert "not in this filesystem" in r.stdout


def test_fstab_without_data_fails(shell, tmp_path):
    root = make_root(tmp_path)
    (root / "etc/fstab").write_text("/dev/mmcblk0p4 /media auto defaults 0 0\n")
    r = run(shell, root)
    assert any("no entry for /data" in ln for ln in lines(r, "FAIL"))


def test_data_mount_unit_instead_of_fstab_passes(shell, tmp_path):
    root = make_root(tmp_path)
    (root / "etc/fstab").write_text("")
    _write(root, "/lib/systemd/system/data.mount", "[Mount]\nWhere=/data\n")
    r = run(shell, root)
    assert r.returncode == 0, r.stdout
    assert "data.mount mounts /data" in r.stdout


def test_32bit_store_with_64bit_client_fails(shell, tmp_path):
    r = run(shell, make_root(tmp_path, store_wide=False))
    assert r.returncode == 1
    assert any("written by a 32-bit client but this client is 64-bit" in ln
               for ln in lines(r, "FAIL"))


def test_32bit_store_with_32bit_client_passes(shell, tmp_path):
    r = run(shell, make_root(tmp_path, store_wide=False, client=ELF32))
    assert r.returncode == 0, r.stdout


def test_garbage_store_fails(shell, tmp_path):
    root = make_root(tmp_path)
    (root / "data/mender/mender-store").write_bytes(b"junk" * 100)
    r = run(shell, root)
    assert any("not an LMDB database" in ln for ln in lines(r, "FAIL"))


def test_stray_store_copy_warns(shell, tmp_path):
    root = make_root(tmp_path)
    shutil.copy(root / "data/mender/mender-store", root / "data/mender-store")
    r = run(shell, root)
    assert any("found a mender-store at /data/mender-store" in ln
               for ln in lines(r, "WARN"))


def test_device_type_with_extra_line_fails(shell, tmp_path):
    root = make_root(tmp_path)
    (root / "data/mender/device_type").write_text("device_type=a\nfoo\n")
    r = run(shell, root)
    assert any("more than one line" in ln for ln in lines(r, "FAIL"))


def test_missing_device_type_fails(shell, tmp_path):
    root = make_root(tmp_path)
    (root / "data/mender/device_type").unlink()
    r = run(shell, root)
    assert any("device_type is missing" in ln for ln in lines(r, "FAIL"))


def test_world_readable_key_warns(shell, tmp_path):
    root = make_root(tmp_path)
    (root / "data/mender/mender-agent.pem").chmod(0o644)
    r = run(shell, root)
    assert any("mode 644" in ln for ln in lines(r, "WARN"))


def test_invalid_json_fails_with_position(shell, tmp_path):
    root = make_root(tmp_path, conf='{"ServerURL": "https://a"\n "X": 1}\n')
    r = run(shell, root)
    assert r.returncode == 1
    assert any('not valid JSON: expected , or } after "ServerURL"' in ln
               for ln in lines(r, "FAIL")), r.stdout


def test_wrong_case_key_warns_with_suggestion(shell, tmp_path):
    conf = dict(GOOD_CONF)
    conf["ServerUrl"] = conf.pop("ServerURL")
    r = run(shell, make_root(tmp_path, conf=conf))
    assert any('"ServerUrl" is ignored' in ln and 'did you mean "ServerURL"' in ln
               for ln in lines(r, "WARN")), r.stdout


def test_unknown_key_warns(shell, tmp_path):
    r = run(shell, make_root(tmp_path, conf={**GOOD_CONF, "PollFoo": 1}))
    assert any('"PollFoo" is not a setting' in ln for ln in lines(r, "WARN"))


def test_quoted_number_warns(shell, tmp_path):
    conf = {**GOOD_CONF, "UpdatePollIntervalSeconds": "1800"}
    r = run(shell, make_root(tmp_path, conf=conf))
    assert any("is a string, but the client expects a number" in ln
               for ln in lines(r, "WARN"))


def test_servers_and_serverurl_in_different_files_fails(shell, tmp_path):
    conf = {k: v for k, v in GOOD_CONF.items() if k != "ServerURL"}
    conf["Servers"] = [{"ServerURL": "https://hosted.mender.io"}]
    root = make_root(tmp_path, conf=conf)
    _write(root, "/data/mender/mender.conf",
           json.dumps({"ServerURL": "https://other.example.com"}))
    r = run(shell, root)
    assert any('both "Servers" and "ServerURL"' in ln for ln in lines(r, "FAIL"))


def test_hosted_without_tenant_token_fails(shell, tmp_path):
    conf = {k: v for k, v in GOOD_CONF.items() if k != "TenantToken"}
    r = run(shell, make_root(tmp_path, conf=conf))
    assert any("needs a TenantToken" in ln for ln in lines(r, "FAIL"))


def test_server_url_without_https_fails(shell, tmp_path):
    r = run(shell, make_root(tmp_path,
                             conf={**GOOD_CONF, "ServerURL": "hosted.mender.io"}))
    assert any("must start with https://" in ln for ln in lines(r, "FAIL"))


def test_missing_server_certificate_fails(shell, tmp_path):
    conf = {**GOOD_CONF, "ServerCertificate": "/etc/mender/server.crt"}
    r = run(shell, make_root(tmp_path, conf=conf))
    assert any("server.crt, which does not exist" in ln for ln in lines(r, "FAIL"))


def test_same_rootfs_partitions_fail(shell, tmp_path):
    conf = {**GOOD_CONF, "RootfsPartB": GOOD_CONF["RootfsPartA"]}
    r = run(shell, make_root(tmp_path, conf=conf))
    assert any("are the same" in ln for ln in lines(r, "FAIL"))


def test_missing_config_fails(shell, tmp_path):
    r = run(shell, make_root(tmp_path, conf=None))
    assert any("no configuration" in ln for ln in lines(r, "FAIL"))


def test_non_executable_inventory_script_warns(shell, tmp_path):
    root = make_root(tmp_path)
    (root / "usr/share/mender/inventory/mender-inventory-os").chmod(0o644)
    r = run(shell, root)
    assert any("mender-inventory-os: not executable" in ln
               for ln in lines(r, "WARN"))


def test_state_scripts_need_version_file(shell, tmp_path):
    root = make_root(tmp_path)
    _write(root, "/etc/mender/scripts/Download_Enter_00", "#!/bin/sh\n", 0o755)
    r = run(shell, root)
    assert any("no version file" in ln for ln in lines(r, "FAIL"))
    _write(root, "/etc/mender/scripts/version", "3\n")
    r = run(shell, root)
    assert lines(r, "FAIL") == [], r.stdout


def test_help_and_usage_errors(shell, tmp_path):
    r = subprocess.run([*shell, str(SCRIPT), "--help"],
                       capture_output=True, text=True)
    assert r.returncode == 0 and "--root DIR" in r.stdout
    r = subprocess.run([*shell, str(SCRIPT), "--bogus"],
                       capture_output=True, text=True)
    assert r.returncode == 2 and "unknown argument" in r.stderr
    r = subprocess.run([*shell, str(SCRIPT), "--root", str(tmp_path / "nope")],
                       capture_output=True, text=True)
    assert r.returncode == 2 and "not a directory" in r.stderr
