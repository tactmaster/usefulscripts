"""
Tests for mender/set_device_name_from_attribute.sh.

The script runs against a fake ``curl`` that serves canned responses and
records every call, so no network is used. It's copied into a temp
directory first so it never picks up a real mender/.env.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "mender" / "set_device_name_from_attribute.sh"
BASE = "https://hosted.mender.io"
INVENTORY = f"{BASE}/api/management/v1/inventory/devices"

FAKE_CURL = """#!{python}
import json, sys
args = sys.argv[1:]
method, body, url = "GET", None, None
i = 0
while i < len(args):
    a = args[i]
    if a == "-X":
        method = args[i + 1]; i += 2; continue
    if a == "--data-raw":
        body = args[i + 1]; i += 2; continue
    if a in ("-H", "-w"):
        i += 2; continue
    if not a.startswith("-"):
        url = a
    i += 1
with open({log!r}, "a") as f:
    f.write(json.dumps({{"method": method, "url": url, "body": body}}) + "\\n")
routes = json.load(open({routes!r}))
status, payload = routes.get(method + " " + url, [404, {{"error": "no route"}}])
sys.stdout.write((json.dumps(payload) if payload is not None else "") + "\\n" + str(status))
"""

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("jq") is None,
    reason="bash and jq are required",
)


def _device(device_id: str, attrs: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"id": device_id, "attributes": attrs}


def _serial(value: str) -> Dict[str, Any]:
    return {"name": "serial_number", "value": value, "scope": "identity"}


def _name(value: str) -> Dict[str, Any]:
    return {"name": "name", "value": value, "scope": "tags"}


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.dir = tmp_path
        self.script = tmp_path / SCRIPT.name
        shutil.copy(SCRIPT, self.script)
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        self.log = tmp_path / "calls.log"
        self.routes_file = tmp_path / "routes.json"
        self.routes: Dict[str, Tuple[int, Any]] = {}
        curl = bin_dir / "curl"
        curl.write_text(
            FAKE_CURL.format(
                python=sys.executable, log=str(self.log), routes=str(self.routes_file)
            )
        )
        curl.chmod(0o755)
        self.env = {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "MENDER_API_TOKEN": "secret-token",
            "MENDER_SERVER_URL": BASE,
        }

    def route(self, method: str, url: str, payload: Any, status: int = 200) -> None:
        self.routes[f"{method} {url}"] = (status, payload)

    def run(
        self, *args: str, env: Optional[Dict[str, str]] = None
    ) -> "subprocess.CompletedProcess[str]":
        self.routes_file.write_text(json.dumps(self.routes))
        return subprocess.run(
            ["bash", str(self.script), *args],
            cwd=self.dir,
            env=env if env is not None else self.env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def calls(self) -> List[Dict[str, Any]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]


@pytest.fixture
def h(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


LIST_URL = f"{INVENTORY}?page=1&per_page=500"
FLEET = [
    _device("new", [_serial("SN1")]),
    _device("same", [_serial("SN2"), _name("SN2")]),
    _device("named", [_serial("SN3"), _name("kitchen")]),
    _device("bare", []),
]


def test_renames_and_reports(h: Harness) -> None:
    h.route("GET", LIST_URL, FLEET)
    h.route("PATCH", f"{INVENTORY}/new/tags", None)

    result = h.run("serial_number")

    assert result.returncode == 0, result.stderr
    patches = [c for c in h.calls() if c["method"] == "PATCH"]
    assert len(patches) == 1
    assert patches[0]["url"] == f"{INVENTORY}/new/tags"
    assert json.loads(patches[0]["body"]) == [{"name": "name", "value": "SN1"}]
    assert (
        "renamed 1, already named 1, kept existing name 1, "
        "missing 'serial_number' 1" in result.stdout
    )


def test_dry_run_prints_curl_and_sends_no_patch(h: Harness) -> None:
    h.route("GET", LIST_URL, FLEET)

    result = h.run("serial_number", "--dry-run")

    assert result.returncode == 0, result.stderr
    assert [c["method"] for c in h.calls()] == ["GET"]
    out = result.stdout
    assert "curl -X GET" in out
    assert "curl -X PATCH" in out
    assert f"'{INVENTORY}/new/tags'" in out
    assert """--data-raw '[{"name":"name","value":"SN1"}]'""" in out
    assert '-H "Authorization: Bearer $MENDER_API_TOKEN"' in out
    assert "secret-token" not in out + result.stderr
    assert "would rename 1" in out
    # The printed GET comes before the per-device lines.
    assert out.index("curl -X GET") < out.index("# new: name -> 'SN1'")


def test_overwrite_and_device_id(h: Harness) -> None:
    h.route("GET", f"{INVENTORY}/named", FLEET[2])
    h.route("PATCH", f"{INVENTORY}/named/tags", None)

    result = h.run("serial_number", "--device-id", "named", "--overwrite")

    assert result.returncode == 0, result.stderr
    assert [c["method"] for c in h.calls()] == ["GET", "PATCH"]
    assert "renamed 1" in result.stdout


def test_scope_selection(h: Harness) -> None:
    device = _device(
        "d1",
        [
            _serial("ID-SN"),
            {"name": "serial_number", "value": "INV-SN", "scope": "inventory"},
        ],
    )
    h.route("GET", LIST_URL, [device])

    default = h.run("serial_number", "--dry-run", "--no-curl")
    identity = h.run("serial_number", "--dry-run", "--scope", "identity")

    assert "name -> 'INV-SN'" in default.stdout
    assert "name -> 'ID-SN'" in identity.stdout


def test_hint_when_argument_is_a_value(h: Harness) -> None:
    mac = {"name": "mac", "value": "10:e7:7a:e3:cd:e9", "scope": "identity"}
    h.route("GET", LIST_URL, [_device("d1", [mac])])

    result = h.run("10:e7:7a:e3:cd:e9", "--dry-run")

    assert result.returncode == 0, result.stderr
    assert "an attribute name, not a value" in result.stdout
    assert "mac (identity)" in result.stdout


def test_paginates_full_pages(h: Harness) -> None:
    h.route("GET", LIST_URL, [_device(f"d{i}", []) for i in range(500)])
    h.route("GET", f"{INVENTORY}?page=2&per_page=500", [_device("last", [])])

    result = h.run("serial_number", "--no-curl")

    assert result.returncode == 0, result.stderr
    assert "missing 'serial_number' 501" in result.stdout


def test_401_explains_region(h: Harness) -> None:
    h.route("GET", LIST_URL, {"error": "unauthorized"}, status=401)

    result = h.run("serial_number")

    assert result.returncode == 1
    assert "rejected the token" in result.stderr
    assert "eu.hosted.mender.io" in result.stderr


def test_missing_token(h: Harness) -> None:
    env = dict(h.env)
    del env["MENDER_API_TOKEN"]

    result = h.run("serial_number", env=env)

    assert result.returncode == 2
    assert "MENDER_API_TOKEN is not set" in result.stderr
    assert h.calls() == []


def test_env_file_is_read_and_shell_wins(h: Harness) -> None:
    (h.dir / ".env").write_text(
        "# test\n"
        "export MENDER_API_TOKEN='from-file'\n"
        'MENDER_SERVER_URL="https://eu.hosted.mender.io"\n'
        "echo this line must not run\n"
    )
    eu_list = (
        "https://eu.hosted.mender.io/api/management/v1/inventory/devices"
        "?page=1&per_page=500"
    )
    h.route("GET", eu_list, [])
    env = dict(h.env)
    del env["MENDER_API_TOKEN"]
    del env["MENDER_SERVER_URL"]

    from_file = h.run("serial_number", env=env)
    env["MENDER_SERVER_URL"] = BASE
    h.route("GET", LIST_URL, [])
    shell_wins = h.run("serial_number", env=env)

    assert from_file.returncode == 0, from_file.stderr
    assert "this line must not run" not in from_file.stdout
    assert h.calls()[0]["url"] == eu_list
    assert shell_wins.returncode == 0, shell_wins.stderr
    assert h.calls()[1]["url"] == LIST_URL


def test_usage_errors(h: Harness) -> None:
    assert h.run().returncode == 2
    assert h.run("serial_number", "--scope", "bogus").returncode == 2
    assert h.run("a", "b").returncode == 2
    assert h.run("--help").returncode == 0
