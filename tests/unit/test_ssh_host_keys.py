"""SSHClient verifies the robot's host key against known_hosts.

The rejection paths run against a real paramiko server on localhost, so the
host-key check is paramiko's own rather than a mock's. The server refuses
every login: a run that reaches authentication proves the key was accepted.
"""

import socket
import threading
from pathlib import Path
from unittest.mock import patch

import paramiko
import pytest

from opentrons_server.transport import HostKeyVerificationError, SSHClient, SessionState

HOST = "127.0.0.1"

# The test server's transport threads end with EOFError when a refused client
# hangs up; that is the expected end of every connection here, not a failure.
pytestmark = pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")


@pytest.fixture(scope="module")
def keys():
    return {
        "server": paramiko.RSAKey.generate(1024),
        "other": paramiko.RSAKey.generate(1024),
        "client": paramiko.RSAKey.generate(1024),
    }


class _RefuseLogin(paramiko.ServerInterface):
    def get_allowed_auths(self, username):
        return "publickey"

    def check_auth_publickey(self, username, key):
        return paramiko.AUTH_FAILED


@pytest.fixture
def server(keys):
    """SSH server presenting keys["server"]; counts accepted connections."""
    listener = socket.socket()
    listener.bind((HOST, 0))
    listener.listen(8)
    listener.settimeout(0.2)
    state = {"connections": 0, "stop": False}
    transports = []

    def serve():
        while not state["stop"]:
            try:
                conn, _ = listener.accept()
            except socket.timeout:
                continue
            state["connections"] += 1
            transport = paramiko.Transport(conn)
            transport.add_server_key(keys["server"])
            transport.start_server(server=_RefuseLogin())
            transports.append(transport)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    port = listener.getsockname()[1]
    original_connect = paramiko.SSHClient.connect

    def connect_to_test_port(self, **kwargs):
        kwargs.update(port=port, allow_agent=False, look_for_keys=False)
        return original_connect(self, **kwargs)

    with patch.object(paramiko.SSHClient, "connect", connect_to_test_port):
        yield {"port": port, "state": state, "name": f"[{HOST}]:{port}"}
    state["stop"] = True
    thread.join(timeout=2)
    for transport in transports:
        transport.close()
    listener.close()


@pytest.fixture
def ssh_home(tmp_path, keys, monkeypatch):
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    keys["client"].write_private_key_file(str(home / ".ssh" / "ot2_ssh_key"))
    monkeypatch.setenv("OT2_SSH_HOME", str(home))
    monkeypatch.delenv("OT2_SSH_KNOWN_HOSTS", raising=False)
    monkeypatch.setenv("OT2_SSH_CONFIG", str(home / ".ssh" / "config"))
    monkeypatch.setattr("opentrons_server.transport.ssh_client.time.sleep", lambda s: None)
    return home


def _write_known_hosts(path: Path, entries) -> str:
    hosts = paramiko.HostKeys()
    for name, key in entries:
        hosts.add(name, key.get_name(), key)
    hosts.save(str(path))
    return path.read_text()


def _client(home: Path, max_retries=2) -> SSHClient:
    return SSHClient(
        hostname=HOST, username="root",
        key_file_path=str(home / ".ssh" / "ot2_ssh_key"),
        max_retries=max_retries, connection_timeout=5,
    )


def test_known_key_is_accepted_and_reaches_login(server, ssh_home, keys):
    _write_known_hosts(ssh_home / ".ssh" / "known_hosts", [(server["name"], keys["server"])])
    client = _client(ssh_home)

    assert client.connect() is False
    # Past the host-key check: the login refusal is an ordinary, retried failure.
    assert client.host_key_failure is False
    assert "Authentication" in client.last_error
    assert server["state"]["connections"] == 2


def test_hashed_known_hosts_entry_is_accepted(server, ssh_home, keys):
    hashed = paramiko.HostKeys.hash_host(server["name"])
    _write_known_hosts(ssh_home / ".ssh" / "known_hosts", [(hashed, keys["server"])])
    client = _client(ssh_home)

    assert client.connect() is False
    assert client.host_key_failure is False


def test_unknown_host_is_refused_once_and_names_the_fingerprint(server, ssh_home, keys):
    known_hosts = ssh_home / ".ssh" / "known_hosts"
    before = _write_known_hosts(known_hosts, [("192.0.2.1", keys["other"])])
    client = _client(ssh_home, max_retries=3)

    assert client.connect() is False
    assert client.host_key_failure is True
    assert server["state"]["connections"] == 1
    assert keys["server"].fingerprint in client.last_error
    assert str(known_hosts) in client.last_error
    assert client.is_connected is False and client.ssh_client is None
    # Never trust-on-first-use: the file is left exactly as it was.
    assert known_hosts.read_text() == before


def test_changed_key_is_refused_once(server, ssh_home, keys):
    known_hosts = ssh_home / ".ssh" / "known_hosts"
    before = _write_known_hosts(known_hosts, [(server["name"], keys["other"])])
    client = _client(ssh_home, max_retries=3)

    assert client.connect() is False
    assert client.host_key_failure is True
    assert "changed" in client.last_error
    assert keys["server"].fingerprint in client.last_error
    assert keys["other"].fingerprint in client.last_error
    assert server["state"]["connections"] == 1
    assert known_hosts.read_text() == before


def test_missing_known_hosts_refuses_before_connecting(server, ssh_home):
    client = _client(ssh_home, max_retries=3)

    assert client.connect() is False
    assert client.host_key_failure is True
    assert "no known_hosts file" in client.last_error
    assert server["state"]["connections"] == 0


def test_known_hosts_path_resolution(ssh_home, tmp_path, monkeypatch):
    config = ssh_home / ".ssh" / "config"
    config.write_text(
        "Host robot\n    HostName 192.0.2.7\n    UserKnownHostsFile \"/x y/hosts\"\n"
    )
    # UserKnownHostsFile is not read: the profile's own known_hosts is used.
    aliased = SSHClient(host_alias="robot")
    assert aliased._known_hosts_path() == ssh_home / ".ssh" / "known_hosts"
    plain = SSHClient(hostname="192.0.2.7")
    assert plain._known_hosts_path() == ssh_home / ".ssh" / "known_hosts"

    override = tmp_path / "pinned_hosts"
    monkeypatch.setenv("OT2_SSH_KNOWN_HOSTS", str(override))
    assert aliased._known_hosts_path() == override
    assert plain._known_hosts_path() == override


def test_host_key_alias_is_refused_not_ignored(server, ssh_home, keys):
    # The address has a key on record, but the alias names a different identity:
    # ignoring HostKeyAlias would accept whatever is recorded for the address.
    _write_known_hosts(ssh_home / ".ssh" / "known_hosts", [
        (server["name"], keys["server"]), ("pinned-robot", keys["other"]),
    ])
    (ssh_home / ".ssh" / "config").write_text(
        f"Host robot\n    HostName {HOST}\n    HostKeyAlias pinned-robot\n"
    )
    client = SSHClient(host_alias="robot", max_retries=3, connection_timeout=5)

    assert client.connect() is False
    assert client.host_key_failure is True
    assert "HostKeyAlias" in client.last_error
    assert server["state"]["connections"] == 0


def test_failed_reconnect_keeps_the_session_mode(ssh_home):
    client = _client(ssh_home, max_retries=3)
    client.session_state = SessionState.PYTHON
    outcomes = iter([False, True])
    client.connect = lambda: next(outcomes)
    started = []
    client.start_python_session = lambda: started.append(1)
    client._execute_python_command = lambda code, timeout: "2"

    assert client.execute_python_command("1 + 1") == "2"
    assert started == [1]


def test_command_reconnect_stops_on_a_host_key_failure(ssh_home):
    client = _client(ssh_home, max_retries=3)
    client.session_state = SessionState.PYTHON
    calls = []

    def refuse():
        calls.append(1)
        client.host_key_failure = True
        client.last_error = "SSH host key check failed: changed"
        return False

    client.connect = refuse
    with pytest.raises(HostKeyVerificationError, match="changed"):
        client.execute_python_command("1 + 1")
    assert len(calls) == 1

    client.session_state = SessionState.SHELL
    with pytest.raises(HostKeyVerificationError):
        client.execute_shell_command("true")
    assert len(calls) == 2


def test_ot2control_surfaces_a_host_key_refusal():
    from opentrons_server.control.ot2_control import OT2Control

    with patch("opentrons_server.control.ot2_control.SSHClient") as ssh_cls:
        client = ssh_cls.return_value
        client.connect.return_value = False
        client.hostname = "192.0.2.7"
        client.host_key_failure = True
        client.last_error = "SSH host key check failed: no known_hosts file at X"

        with pytest.raises(HostKeyVerificationError, match="no known_hosts file at X"):
            OT2Control(host_alias="192.0.2.7")
