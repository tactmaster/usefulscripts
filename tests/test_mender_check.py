"""
Tests for mender/mender-check.sh.

Each test builds a fake device filesystem under tmp_path and checks it with
``--root``, so no Mender install, root access or network is needed. The
script targets busybox devices, so every test runs under each POSIX shell
available (dash, busybox sh), not just bash.
"""

import base64
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
# Same shape as tenantadm's tokens: 43 random base64url characters.
TENANT_TOKEN = "Qx7mK2pR9vT4wZ8bN3cF6hJ1sL5dG0aY-_eUiOyPq3k"
GOOD_CONF = {
    "ServerURL": "https://hosted.mender.io",
    "TenantToken": TENANT_TOKEN,
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


def _jwt(claims):
    def b64(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{b64({'alg': 'RS256', 'typ': 'JWT'})}.{b64(claims)}.c2lnbmF0dXJl"


def _token_lines(shell, tmp_path, token):
    r = run(shell, make_root(tmp_path, conf={**GOOD_CONF, "TenantToken": token}))
    return r, [ln for ln in r.stdout.splitlines() if "TenantToken" in ln]


def test_current_tenant_token_format_passes(shell, tmp_path):
    assert len(TENANT_TOKEN) == 43
    r, tl = _token_lines(shell, tmp_path, TENANT_TOKEN)
    assert r.returncode == 0 and lines(r, "WARN") == [], r.stdout
    assert tl == ["  [ OK ] TenantToken set"]
    assert TENANT_TOKEN not in r.stdout, "the token is a secret and must not be printed"


def test_older_jwt_tenant_token_passes(shell, tmp_path):
    token = _jwt({"mender.tenant": "5f1c0a", "iss": "Mender", "sub": "5f1c0a"})
    r, tl = _token_lines(shell, tmp_path, token)
    assert lines(r, "WARN") == [] and lines(r, "FAIL") == [], r.stdout
    assert tl == ["  [ OK ] TenantToken set (older JWT format)"]


@pytest.mark.skipif(not shutil.which("base64"), reason="needs base64 to decode")
def test_user_jwt_instead_of_tenant_token_fails(shell, tmp_path):
    token = _jwt({"sub": "u1", "mender.user": True, "mender.tenant": "5f1c0a"})
    r, tl = _token_lines(shell, tmp_path, token)
    assert any("user's access token" in ln for ln in lines(r, "FAIL")), r.stdout


@pytest.mark.parametrize("token,expect", [
    ("<YOUR-TENANT-TOKEN>", "looks like a placeholder"),
    (TENANT_TOKEN[:-1], "is 42 characters and matches neither token format"),
    (TENANT_TOKEN[:20] + " " + TENANT_TOKEN[20:], "contains spaces or line breaks"),
    ("Bearer " + TENANT_TOKEN, "starts with \"Bearer\""),
])
def test_bad_tenant_tokens_warn(shell, tmp_path, token, expect):
    r, tl = _token_lines(shell, tmp_path, token)
    assert any(expect in ln for ln in lines(r, "WARN")), r.stdout
    assert TENANT_TOKEN[5:30] not in r.stdout


def test_artifact_info_not_needed_by_4x_client(shell, tmp_path):
    root = make_root(tmp_path)
    r = run(shell, root)
    assert "/etc/mender/artifact_info" not in r.stdout
    _write(root, "/etc/mender/artifact_info", "artifact_name=release-1\n")
    r = run(shell, root)
    assert lines(r, "WARN") == [], r.stdout
    assert "left over from 3.x" in r.stdout


def test_artifact_info_required_by_3x_client(shell, tmp_path):
    root = make_root(tmp_path)
    (root / "usr/bin/mender-update").rename(root / "usr/bin/mender")
    (root / "usr/bin/mender-auth").unlink()
    r = run(shell, root)
    assert any("artifact_info is missing" in ln for ln in lines(r, "WARN"))


def test_bootstrap_artifact_seeds_missing_store(shell, tmp_path):
    root = make_root(tmp_path)
    (root / "data/mender/mender-store").unlink()
    r = run(shell, root)
    assert any("no mender-store and no bootstrap.mender" in ln
               for ln in lines(r, "WARN"))
    _write(root, "/data/mender/bootstrap.mender", b"artifact")
    r = run(shell, root)
    assert lines(r, "WARN") == [], r.stdout
    assert "bootstrap.mender will initialise it" in r.stdout


# ---------------------------------------------------------------- inventory

def test_symlinked_inventory_scripts_resolve_inside_the_image(shell, tmp_path):
    """Debian packages link /usr/share/mender/inventory/* into /etc/mender."""
    root = make_root(tmp_path)
    inv = root / "usr/share/mender/inventory"
    _write(root, "/etc/mender/inventory/mender-inventory-network", "#!/bin/sh\n", 0o755)
    (inv / "mender-inventory-network").symlink_to(
        "/etc/mender/inventory/mender-inventory-network")
    r = run(shell, root)
    assert r.returncode == 0, r.stdout
    assert "2 inventory script(s)" in r.stdout


def test_broken_inventory_symlink_fails(shell, tmp_path):
    root = make_root(tmp_path)
    (root / "usr/share/mender/inventory/mender-inventory-gone").symlink_to(
        "/etc/mender/inventory/mender-inventory-gone")
    r = run(shell, root)
    assert any("mender-inventory-gone -> /etc/mender/inventory/mender-inventory-gone, "
               "which does not exist (broken symlink)" in ln for ln in lines(r, "FAIL"))


def test_inventory_script_without_prefix_warns(shell, tmp_path):
    root = make_root(tmp_path)
    _write(root, "/usr/share/mender/inventory/hostinfo", "#!/bin/sh\n", 0o755)
    r = run(shell, root)
    assert any("hostinfo: skipped by the client" in ln for ln in lines(r, "WARN"))


def live(shell, tmp_path, *args, inventory=None, dbus_token="tok"):
    """Run in live mode as a pretend root, with MENDER_* pointing at tmp.

    Pretending relies on stub commands earlier in PATH; busybox sh runs its
    built-in applets (id) instead, so these tests only run under other shells.
    """
    if shell[0] == "busybox":
        pytest.skip("busybox sh ignores PATH stubs for its own applets")
    d = tmp_path / "dev"
    _write(d, "/etc/mender/mender.conf", json.dumps(
        {"ServerURL": "https://hosted.mender.io", "TenantToken": TENANT_TOKEN}))
    _write(d, "/var/lib/mender/mender-store", _build_store([(b"a", b"b")]), 0o600)
    _write(d, "/var/lib/mender/device_type", "device_type=qemu\n")
    _write(d, "/usr/share/mender/identity/mender-device-identity",
           "#!/bin/sh\necho mac=00:11:22:33:44:55\n", 0o755)
    for name, body in (inventory or {}).items():
        _write(d, f"/usr/share/mender/inventory/{name}", body, 0o755)
    stubs = tmp_path / "stubs"
    _write(stubs, "/id", '#!/bin/sh\n[ "$1" = -u ] && { echo 0; exit 0; }\n'
           'exec /usr/bin/id "$@"\n', 0o755)
    for b in ("mender-update", "mender-auth"):
        _write(stubs, f"/{b}", "#!/bin/sh\necho release-1\n", 0o755)
    _write(stubs, "/dbus-send",
           f'#!/bin/sh\necho "method return"\necho \'   string "{dbus_token}"\'\n'
           'echo \'   string "https://hosted.mender.io"\'\n', 0o755)
    env = {k: v for k, v in os.environ.items() if not k.startswith("MENDER_")}
    env.update(PATH=f"{stubs}:{env['PATH']}",
               MENDER_CONF_DIR=str(d / "etc/mender"),
               MENDER_DATASTORE_DIR=str(d / "var/lib/mender"),
               MENDER_DATA_DIR=str(d / "usr/share/mender"))
    return subprocess.run([*shell, str(SCRIPT), "--no-network", *args],
                          capture_output=True, text=True, env=env)


def test_inventory_scripts_run_and_report(shell, tmp_path):
    r = live(shell, tmp_path, inventory={
        "mender-inventory-a": "#!/bin/sh\necho serial_number=SN1\necho ipv4=10.0.0.2\necho ipv4=10.0.0.3\n",
        "mender-inventory-b": "#!/bin/sh\necho os=Linux\n"})
    assert "2 of 2 inventory script(s) ran cleanly, reporting 3 attribute(s)" in r.stdout, r.stdout


def test_inventory_script_with_blank_line_fails(shell, tmp_path):
    r = live(shell, tmp_path, inventory={
        "mender-inventory-a": "#!/bin/sh\necho a=1\necho\necho b=2\n"})
    assert any('mender-inventory-a printed a line without "=" (line 2: "")' in ln
               for ln in lines(r, "FAIL")), r.stdout


def test_failing_inventory_script_fails(shell, tmp_path):
    r = live(shell, tmp_path, inventory={"mender-inventory-a": "#!/bin/sh\nexit 3\n"})
    assert any("mender-inventory-a exited with status 3" in ln for ln in lines(r, "FAIL"))


def test_expected_inventory_item(shell, tmp_path):
    inv = {"mender-inventory-a": "#!/bin/sh\necho serial_number=SN1\necho os=Linux\n"}
    r = live(shell, tmp_path, "--inventory", "serial_number", "--inventory", "mac",
             inventory=inv)
    assert "[ OK ] inventory: serial_number=SN1" in r.stdout, r.stdout
    assert "mac=00:11:22:33:44:55 (an identity attribute, not inventory)" in r.stdout
    r = live(shell, tmp_path, "--inventory", "cpu_model", inventory=inv)
    assert any('no inventory script reports "cpu_model"' in ln for ln in lines(r, "FAIL"))
    assert "the scripts report: serial_number os" in r.stdout


def test_expected_inventory_item_not_checked_in_image(shell, tmp_path):
    r = run(shell, make_root(tmp_path), "--inventory", "serial_number")
    assert r.returncode == 0
    assert '"serial_number" not checked: the inventory scripts were not run' in r.stdout


def test_authentication_reported_from_mender_authd(shell, tmp_path):
    r = live(shell, tmp_path)
    assert "the device is authenticated with https://hosted.mender.io" in r.stdout, r.stdout
    r = live(shell, tmp_path, dbus_token="")
    assert any("the device has no auth token" in ln for ln in lines(r, "WARN"))


# ---------------------------------------------------------------- add-ons

def with_connect(tmp_path, conf=None, shells="/bin/sh\n/bin/bash\n"):
    root = make_root(tmp_path)
    _write(root, "/usr/bin/mender-connect", ELF64, 0o755)
    _write(root, "/bin/sh", "", 0o755)
    _write(root, "/bin/bash", "", 0o755)
    _write(root, "/etc/passwd", "root:x:0:0:root:/root:/bin/sh\n")
    _write(root, "/etc/shells", shells)
    _write(root, "/usr/share/dbus-1/system.d/io.mender.AuthenticationManager.conf", "<busconfig/>")
    if conf is not None:
        _write(root, "/etc/mender/mender-connect.conf",
               conf if isinstance(conf, str) else json.dumps(conf))
    return root


def test_add_ons_not_installed_is_fine(shell, tmp_path):
    r = run(shell, make_root(tmp_path))
    for addon in ("mender-connect", "mender-configure", "mender-monitor"):
        assert f"[info] {addon} not installed" in r.stdout


def test_connect_healthy(shell, tmp_path):
    r = run(shell, with_connect(tmp_path, {"User": "root", "ShellCommand": "/bin/bash"}))
    assert r.returncode == 0, r.stdout
    assert "mender-connect: shell /bin/bash as user root" in r.stdout
    assert "D-Bus policy for mender-auth present" in r.stdout


def test_connect_without_user_fails(shell, tmp_path):
    r = run(shell, with_connect(tmp_path, {"ShellCommand": "/bin/sh"}))
    assert any('no "User" set' in ln for ln in lines(r, "FAIL"))


def test_connect_unknown_user_fails(shell, tmp_path):
    r = run(shell, with_connect(tmp_path, {"User": "mender"}))
    assert any('User "mender" doesn\'t exist' in ln for ln in lines(r, "FAIL"))


def test_connect_shell_not_in_etc_shells_fails(shell, tmp_path):
    r = run(shell, with_connect(tmp_path, {"User": "root", "ShellCommand": "/bin/bash"},
                                shells="/bin/sh\n"))
    assert any("/bin/bash is not listed in /etc/shells" in ln for ln in lines(r, "FAIL"))


def test_connect_keys_are_case_insensitive_but_types_strict(shell, tmp_path):
    r = run(shell, with_connect(tmp_path, {"user": "root",
                                           "ReconnectIntervalSeconds": "30"}))
    assert any('"ReconnectIntervalSeconds" is a string but mender-connect expects '
               'a number, so it refuses to start' in ln for ln in lines(r, "FAIL"))
    assert not any('"user"' in ln for ln in lines(r, "WARN")), r.stdout
    assert "as user root" in r.stdout


def test_connect_deprecated_server_settings_warn(shell, tmp_path):
    r = run(shell, with_connect(tmp_path, {"User": "root", "ServerURL": "https://x"}))
    assert any('mender-connect ignores "ServerURL"' in ln for ln in lines(r, "WARN"))


def test_missing_dbus_policy_fails_with_add_ons(shell, tmp_path):
    root = with_connect(tmp_path, {"User": "root"})
    (root / "usr/share/dbus-1/system.d/io.mender.AuthenticationManager.conf").unlink()
    r = run(shell, root)
    assert any("io.mender.AuthenticationManager.conf is missing" in ln
               for ln in lines(r, "FAIL"))


def with_configure(tmp_path):
    root = make_root(tmp_path)
    _write(root, "/usr/share/mender/modules/v3/mender-configure", "#!/bin/sh\n", 0o755)
    _write(root, "/usr/share/mender/inventory/mender-inventory-mender-configure",
           "#!/bin/sh\n", 0o755)
    _write(root, "/usr/lib/mender-configure/apply-device-config.d/timezone",
           "#!/bin/sh\n", 0o755)
    for t in ("dbus-send", "curl", "sha256sum"):
        _write(root, f"/usr/bin/{t}", ELF64, 0o755)
    _write(root, "/usr/share/dbus-1/system.d/io.mender.AuthenticationManager.conf", "<busconfig/>")
    return root


def test_configure_healthy(shell, tmp_path):
    root = with_configure(tmp_path)
    _write(root, "/var/lib/mender-configure/device-config.json", '{"timezone": "UTC"}')
    r = run(shell, root)
    assert lines(r, "FAIL") == [], r.stdout
    assert "mender-configure apply scripts: timezone" in r.stdout
    assert "current configuration has 1 setting(s)" in r.stdout


def test_configure_non_executable_apply_script_fails(shell, tmp_path):
    root = with_configure(tmp_path)
    (root / "usr/lib/mender-configure/apply-device-config.d/timezone").chmod(0o644)
    r = run(shell, root)
    assert any("timezone: not executable, so every configuration deployment fails" in ln
               for ln in lines(r, "FAIL")), r.stdout


def test_configure_broken_config_and_missing_tool_fail(shell, tmp_path):
    root = with_configure(tmp_path)
    (root / "usr/bin/curl").unlink()
    _write(root, "/var/lib/mender-configure/device-config.json", '{"timezone": }')
    r = run(shell, root)
    fails = lines(r, "FAIL")
    assert any("needs curl" in ln for ln in fails)
    assert any("device-config.json is not valid JSON" in ln for ln in fails)


def with_monitor(tmp_path):
    root = make_root(tmp_path)
    share = root / "usr/share/mender-monitor"
    _write(root, "/usr/share/mender-monitor/mender-monitord", "#!/bin/bash\n", 0o755)
    _write(root, "/usr/share/mender-monitor/mender-monitorctl", "#!/bin/bash\n", 0o755)
    _write(root, "/usr/share/mender-monitor/.version", "1.4.0\n")
    for b in ("mender-monitord", "mender-monitorctl"):
        (root / "usr/bin" / b).symlink_to(f"/usr/share/mender-monitor/{b}")
    for t in ("bash", "dbus-send"):
        _write(root, f"/usr/bin/{t}", ELF64, 0o755)
    md = "/etc/mender-monitor/monitor.d"
    for sub in ("service", "log", "dbus"):
        _write(root, f"{md}/{sub}.sh", "#!/bin/bash\n", 0o755)
    _write(root, f"{md}/available/service_sshd.sh", "SERVICE_NAME=sshd\n")
    (root / md.lstrip("/") / "enabled").mkdir()
    (root / md.lstrip("/") / "enabled/service_sshd.sh").symlink_to(
        f"{md}/available/service_sshd.sh")
    _write(root, "/usr/share/dbus-1/system.d/io.mender.AuthenticationManager.conf", "<busconfig/>")
    return root, share


def test_monitor_healthy(shell, tmp_path):
    root, _ = with_monitor(tmp_path)
    r = run(shell, root)
    assert lines(r, "FAIL") == [], r.stdout
    assert "mender-monitor installed (version 1.4.0)" in r.stdout
    assert "1 monitor(s) enabled: service_sshd" in r.stdout


def test_monitor_broken_symlinks_fail(shell, tmp_path):
    root, share = with_monitor(tmp_path)
    (share / "mender-monitorctl").unlink()
    md = root / "etc/mender-monitor/monitor.d"
    (md / "enabled/log_syslog.sh").symlink_to(
        "/etc/mender-monitor/monitor.d/available/log_syslog.sh")
    r = run(shell, root)
    fails = lines(r, "FAIL")
    assert any("/usr/bin/mender-monitorctl -> /usr/share/mender-monitor/mender-monitorctl, "
               "which does not exist" in ln for ln in fails), r.stdout
    assert any("enabled/log_syslog.sh -> " in ln and "broken symlink" in ln for ln in fails)


def test_monitor_without_subsystem_handler_fails(shell, tmp_path):
    root, _ = with_monitor(tmp_path)
    md = root / "etc/mender-monitor/monitor.d"
    _write(root, "/etc/mender-monitor/monitor.d/available/disk_root.sh", "X=1\n")
    (md / "enabled/disk_root.sh").symlink_to("/etc/mender-monitor/monitor.d/available/disk_root.sh")
    r = run(shell, root)
    assert any('no /etc/mender-monitor/monitor.d/disk.sh to handle the "disk" subsystem' in ln
               for ln in lines(r, "FAIL")), r.stdout
