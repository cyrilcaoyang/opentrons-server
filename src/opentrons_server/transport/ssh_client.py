"""SSH transport layer for OT-2/Flex hosts.

Paramiko-based SSH client with explicit session state management. Maintains
either a Python REPL session or a shell session, never both simultaneously.
"""

import paramiko
import paramiko.config
import time
import os
import logging
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple
import threading
import socket
from contextlib import contextmanager
from enum import Enum

logger = logging.getLogger(__name__)

_DEFAULT_WINDOWS_SDL2_HOME = Path("C:/Users/sdl2")


class HostKeyVerificationError(paramiko.SSHException):
    """The robot's SSH host key could not be verified against known_hosts.

    Raised for an unknown host, a changed key, no known_hosts file at all, or
    an SSH config that sets ``HostKeyAlias`` (unsupported, so refused rather
    than ignored). Never retried: another attempt meets the same key, and
    connecting anyway would log in to and drive whatever answered.
    """


class _RejectUnknownHostKey(paramiko.MissingHostKeyPolicy):
    """Refuse a host absent from known_hosts, naming the key it presented."""

    def __init__(self, known_hosts: List[Path]):
        self.known_hosts = known_hosts

    def missing_host_key(self, client, hostname, key):
        files = ", ".join(str(path) for path in self.known_hosts)
        raise HostKeyVerificationError(
            f"SSH host key for {hostname!r} ({key.get_name()} {key.fingerprint}) "
            f"is not in {files}. Verify that fingerprint on the robot, then add "
            f"it by connecting once with ssh from the account that owns that file"
        )


class SessionState(Enum):
    SHELL = "shell"
    PYTHON = "python"
    UNKNOWN = "unknown"


class SSHClient:
    """SSH Client for OT-2 with explicit session state management.

    This client maintains either a Python REPL session or a shell session,
    never both simultaneously. Users must explicitly choose which type of
    session they want to use.
    """

    def __init__(self, hostname=None, username=None, key_file_path=None,
                 host_alias=None, password=None, max_retries=3,
                 command_timeout=30, connection_timeout=10):
        self.hostname = hostname
        self.username = username
        self.key_file_path = key_file_path
        self.host_alias = host_alias
        self.password = password
        self.max_retries = max_retries
        self.command_timeout = command_timeout
        self.connection_timeout = connection_timeout

        self.ssh_client = None
        self.session = None
        self.is_connected = False
        self.session_state = SessionState.UNKNOWN
        self._lock = threading.Lock()
        self._ssh_config: Dict[str, Any] = {}
        # Why the last connect() failed, and whether that failure was a host
        # key one (terminal: not retried by connect() or the command loops).
        self.last_error: Optional[str] = None
        self.host_key_failure = False

        self._config_host()

    def _config_host(self):
        if (self.hostname is None) and (self.host_alias is None):
            raise ValueError("Both hostname and host_alias are None, invalid")
        if self.host_alias:
            ssh_config = self._load_ssh_config()
            self._ssh_config = ssh_config
            self.hostname = ssh_config["hostname"]
            self.username = ssh_config["user"]
            identity_file = ssh_config["identityfile"]
            if isinstance(identity_file, list):
                identity_file = identity_file[0]
            self.key_file_path = self._expand_ssh_path(identity_file)

    def _load_ssh_config(self):
        ssh_config_file = self._ssh_config_path()
        if not ssh_config_file.exists():
            logger.info(
                "SSH config %s not found; using host_alias=%r as hostname",
                ssh_config_file,
                self.host_alias,
            )
            return self._default_ot2_ssh_config()
        config = paramiko.config.SSHConfig()
        with open(ssh_config_file) as f:
            config.parse(f)
        resolved = config.lookup(self.host_alias)
        if "hostname" not in resolved:
            return self._default_ot2_ssh_config()
        if "identityfile" not in resolved:
            resolved["identityfile"] = str(self._ssh_home() / ".ssh" / "ot2_ssh_key")
        if "user" not in resolved:
            resolved["user"] = "root"
        return resolved

    def _default_ot2_ssh_config(self) -> Dict[str, str]:
        return {
            "hostname": self.host_alias,
            "user": "root",
            "identityfile": str(self._ssh_home() / ".ssh" / "ot2_ssh_key"),
        }

    def _ssh_home(self) -> Path:
        """Return the home directory used for OT-2 SSH config/key lookup.

        The gateway may run as a Windows service account whose home is
        ``systemprofile``. In the AC lab deployment the OT-2 SSH config and key
        live under the interactive ``sdl2`` profile, so prefer that profile when
        no explicit override is provided.
        """

        configured_home = os.getenv("OT2_SSH_HOME")
        if configured_home:
            return Path(configured_home).expanduser()

        home = Path.home()
        if (
            os.name == "nt"
            and "systemprofile" in str(home).lower()
            and (_DEFAULT_WINDOWS_SDL2_HOME / ".ssh").exists()
        ):
            return _DEFAULT_WINDOWS_SDL2_HOME
        return home

    def _ssh_config_path(self) -> Path:
        configured_config = os.getenv("OT2_SSH_CONFIG")
        if configured_config:
            return Path(configured_config).expanduser()
        return self._ssh_home() / ".ssh" / "config"

    def _expand_ssh_path(self, value: str) -> str:
        if value.startswith("~/") or value.startswith("~\\"):
            return str(self._ssh_home() / value[2:])
        return os.path.expanduser(value)

    def _known_hosts_path(self) -> Path:
        """The known_hosts file the robot's host key is verified against.

        ``OT2_SSH_KNOWN_HOSTS``, else ``known_hosts`` beside the SSH config and
        key this client already uses (see :meth:`_ssh_home`). The SSH config's
        ``UserKnownHostsFile`` is not read; the key is looked up under the
        hostname actually connected to.
        """
        configured = os.getenv("OT2_SSH_KNOWN_HOSTS")
        if configured:
            return Path(self._expand_ssh_path(configured))
        return self._ssh_home() / ".ssh" / "known_hosts"

    def _load_known_hosts(self, client: paramiko.SSHClient) -> List[Path]:
        """Load known_hosts read-only; raise if it cannot verify this host."""
        if "hostkeyalias" in self._ssh_config:
            # Honouring it means trusting another name's key for this address;
            # ignoring it could trust a different robot recorded here. Refuse.
            raise HostKeyVerificationError(
                f"SSH config for {self.host_alias!r} sets HostKeyAlias, which the "
                "gateway does not support; remove it and record the robot's key "
                f"under {self.hostname!r}"
            )
        path = self._known_hosts_path()
        if not path.is_file():
            raise HostKeyVerificationError(
                f"no known_hosts file at {path}; cannot verify the SSH host key "
                f"of {self.hostname!r}"
            )
        # load_system_host_keys: these entries are never written back.
        client.load_system_host_keys(str(path))
        return [path]

    def _drop_connection(self) -> None:
        """Close a failed or stale connection. Keeps ``session_state`` so a
        later command can still reconnect into the mode it expects."""
        if self.session:
            self.session.close()
            self.session = None
        if self.ssh_client:
            self.ssh_client.close()
            self.ssh_client = None
        self.is_connected = False

    def connect(self) -> bool:
        """Establish SSH connection and start in shell mode.

        The robot's host key must already be in known_hosts. An unknown or
        changed key fails at once without retrying; ``last_error`` and
        ``host_key_failure`` say why.
        """
        with self._lock:
            self.last_error = None
            self.host_key_failure = False
            for attempt in range(self.max_retries):
                try:
                    self._drop_connection()

                    self.ssh_client = paramiko.SSHClient()
                    known_hosts = self._load_known_hosts(self.ssh_client)
                    self.ssh_client.set_missing_host_key_policy(
                        _RejectUnknownHostKey(known_hosts))

                    private_key = paramiko.RSAKey.from_private_key_file(
                        self.key_file_path, password=self.password
                    )

                    self.ssh_client.connect(
                        hostname=self.hostname,
                        username=self.username,
                        pkey=private_key,
                        timeout=self.connection_timeout,
                        banner_timeout=self.connection_timeout
                    )

                    self.session = self.ssh_client.invoke_shell()
                    self.session.settimeout(self.command_timeout)

                    time.sleep(1)
                    self._wait_for_shell_prompt()

                    self.session_state = SessionState.SHELL
                    self.is_connected = True
                    logger.info(f"SSH connection established to {self.hostname} in SHELL mode")
                    return True

                except paramiko.BadHostKeyException as e:
                    self.last_error = (
                        f"SSH host key check failed: the key for {e.hostname!r} changed: "
                        f"it presented {e.key.get_name()} {e.key.fingerprint}, "
                        f"known_hosts has {e.expected_key.get_name()} "
                        f"{e.expected_key.fingerprint}"
                    )
                    self.host_key_failure = True
                    logger.error(f"{self.last_error} (not retried)")
                    self._drop_connection()
                    return False

                except HostKeyVerificationError as e:
                    self.last_error = f"SSH host key check failed: {e}"
                    self.host_key_failure = True
                    logger.error(f"{self.last_error} (not retried)")
                    self._drop_connection()
                    return False

                except Exception as e:
                    self.last_error = f"{type(e).__name__}: {e}"
                    logger.warning(f"Connection attempt {attempt + 1} failed: {e}")
                    self._drop_connection()
                    if attempt < self.max_retries - 1:
                        time.sleep(2 ** attempt)
                    else:
                        logger.error(f"Failed to connect after {self.max_retries} attempts")
                        return False

            return False

    def _reconnect_or_raise(self) -> bool:
        """Reconnect inside a command loop; a host key failure ends the loop."""
        if self.connect():
            return True
        if self.host_key_failure:
            raise HostKeyVerificationError(self.last_error)
        return False

    def _wait_for_shell_prompt(self):
        """Wait for shell prompt (#) to appear."""
        self._clear_buffer()
        self.session.send("\n")
        time.sleep(0.5)

        output = ""
        start_time = time.time()
        while time.time() - start_time < 5:
            if self.session.recv_ready():
                chunk = self.session.recv(1024).decode('utf-8')
                output += chunk
                if "#" in chunk:
                    logger.info("Shell prompt detected")
                    return
            time.sleep(0.1)

        logger.warning("Shell prompt not detected within timeout")

    def _wait_for_python_prompt(self):
        """Wait for Python prompt (>>>) to appear."""
        output = ""
        start_time = time.time()
        while time.time() - start_time < 10:
            if self.session.recv_ready():
                chunk = self.session.recv(1024).decode('utf-8')
                output += chunk
                if ">>>" in chunk:
                    logger.info("Python prompt detected")
                    return
            time.sleep(0.1)

        raise Exception("Python prompt not detected within timeout")

    def _clear_buffer(self):
        """Clear any pending output from the buffer."""
        try:
            while self.session and self.session.recv_ready():
                self.session.recv(1024)
        except Exception:
            pass

    def start_python_session(self):
        """Switch to Python REPL session.

        This will start a Python interpreter and all subsequent commands
        will be executed as Python code until switch_to_shell() is called.
        """
        if self.session_state == SessionState.PYTHON:
            logger.info("Already in Python session")
            return True

        if not self.is_connected:
            raise Exception("Not connected to robot")

        try:
            logger.info("Starting Python session")
            self._clear_buffer()
            self.session.send("python3\n")

            self._wait_for_python_prompt()
            self.session_state = SessionState.PYTHON
            logger.info("Successfully started Python session")
            return True

        except Exception as e:
            logger.error(f"Failed to start Python session: {e}")
            raise Exception(f"Failed to start Python session: {e}")

    def switch_to_shell(self):
        """Switch to shell session.

        If currently in Python session, this will exit Python and return
        to the shell prompt. All subsequent commands will be shell commands.
        """
        if self.session_state == SessionState.SHELL:
            logger.info("Already in shell session")
            return True

        if not self.is_connected:
            raise Exception("Not connected to robot")

        try:
            logger.info("Switching to shell session")
            self.session.send("exit()\n")

            self._wait_for_shell_prompt()
            self.session_state = SessionState.SHELL
            logger.info("Successfully switched to shell session")
            return True

        except Exception as e:
            logger.error(f"Failed to switch to shell: {e}")
            raise Exception(f"Failed to switch to shell: {e}")

    def is_alive(self) -> bool:
        """Public liveness check, for callers that need to *report* the state.

        Same answer as :meth:`_is_connection_alive`, exposed because ``/status``
        must be able to say whether the session is actually up rather than
        whether an object exists. Inspects the paramiko transport only — no
        I/O — so it is safe on the side-effect-free status path.
        """
        return self._is_connection_alive()

    def _is_connection_alive(self) -> bool:
        """Check if SSH connection is still alive."""
        try:
            if not self.ssh_client or not self.session:
                return False

            transport = self.ssh_client.get_transport()
            return transport and transport.is_active()

        except Exception:
            return False

    def execute_python_command(self, code: str, timeout: Optional[int] = None) -> str:
        """Execute Python command in Python REPL session.

        The session must be in Python mode. If not, raises an exception.
        Use start_python_session() first to enter Python mode.
        """
        if self.session_state != SessionState.PYTHON:
            raise Exception(f"Cannot execute Python command: session is in {self.session_state.value} mode. Call start_python_session() first.")

        if timeout is None:
            timeout = self.command_timeout

        for attempt in range(self.max_retries):
            try:
                if not self.is_connected or not self._is_connection_alive():
                    logger.info("Connection lost, attempting to reconnect...")
                    if not self._reconnect_or_raise():
                        continue
                    self.start_python_session()

                return self._execute_python_command(code, timeout)

            except HostKeyVerificationError:
                raise
            except (socket.timeout, paramiko.SSHException, OSError) as e:
                logger.warning(f"Python command execution failed (attempt {attempt + 1}): {e}")
                self.is_connected = False

                if attempt < self.max_retries - 1:
                    time.sleep(1)
                    continue
                else:
                    raise Exception(f"Python command failed after {self.max_retries} attempts: {e}")

        raise Exception("Failed to execute Python command after all retry attempts"
                        f" (last connect error: {self.last_error})")

    def execute_shell_command(self, command: str, timeout: Optional[int] = None) -> str:
        """Execute shell command in shell session.

        The session must be in shell mode. If not, raises an exception.
        Use switch_to_shell() first to enter shell mode.
        """
        if self.session_state != SessionState.SHELL:
            raise Exception(f"Cannot execute shell command: session is in {self.session_state.value} mode. Call switch_to_shell() first.")

        if timeout is None:
            timeout = self.command_timeout

        for attempt in range(self.max_retries):
            try:
                if not self.is_connected or not self._is_connection_alive():
                    logger.info("Connection lost, attempting to reconnect...")
                    if not self._reconnect_or_raise():
                        continue

                return self._execute_shell_command(command, timeout)

            except HostKeyVerificationError:
                raise
            except (socket.timeout, paramiko.SSHException, OSError) as e:
                logger.warning(f"Shell command execution failed (attempt {attempt + 1}): {e}")
                self.is_connected = False

                if attempt < self.max_retries - 1:
                    time.sleep(1)
                    continue
                else:
                    raise Exception(f"Shell command failed after {self.max_retries} attempts: {e}")

        raise Exception("Failed to execute shell command after all retry attempts"
                        f" (last connect error: {self.last_error})")

    def _execute_python_command(self, code: str, timeout: int) -> str:
        """Execute a Python command and wait for >>> prompt (handling multi-line code)."""
        with self._lock:
            if not self.session:
                raise Exception("No active session")

            self._clear_buffer()

            self.session.send(code + "\n")

            is_multiline = any(keyword in code for keyword in ['def ', 'class ', 'if ', 'for ', 'while ', 'with ', 'try:'])

            output = ""
            start_time = time.time()
            last_chunk_time = start_time
            continuation_mode = False

            while True:
                current_time = time.time()
                if current_time - start_time > timeout:
                    raise socket.timeout(f"Python command timeout after {timeout} seconds")

                try:
                    if self.session.recv_ready():
                        chunk = self.session.recv(1024).decode('utf-8')
                        output += chunk
                        last_chunk_time = current_time

                        if "... " in chunk:
                            continuation_mode = True
                            if is_multiline:
                                self.session.send("\n")

                        elif ">>> " in chunk:
                            if continuation_mode:
                                time.sleep(0.1)
                            break
                    else:
                        if continuation_mode and is_multiline and (current_time - last_chunk_time > 1.0):
                            self.session.send("\n")
                            continuation_mode = False
                        time.sleep(0.1)

                except socket.timeout:
                    raise socket.timeout(f"Python command timeout after {timeout} seconds")
                except Exception as e:
                    raise Exception(f"Error reading Python response: {e}")

            if "Traceback (most recent call last):" in output:
                raise Exception(f"Python error in command execution:\n{output}")

            return output

    def _execute_shell_command(self, command: str, timeout: int) -> str:
        """Execute a shell command and wait for # prompt."""
        with self._lock:
            if not self.session:
                raise Exception("No active session")

            self._clear_buffer()

            self.session.send(command + "\n")

            output = ""
            start_time = time.time()

            while True:
                if time.time() - start_time > timeout:
                    raise socket.timeout(f"Shell command timeout after {timeout} seconds")

                try:
                    if self.session.recv_ready():
                        chunk = self.session.recv(1024).decode('utf-8')
                        output += chunk

                        if "# " in chunk:
                            break
                    else:
                        time.sleep(0.1)

                except socket.timeout:
                    raise socket.timeout(f"Shell command timeout after {timeout} seconds")
                except Exception as e:
                    raise Exception(f"Error reading shell response: {e}")

            return output

    def close(self):
        """Close SSH connection."""
        with self._lock:
            try:
                if self.session:
                    if self.session_state == SessionState.PYTHON:
                        self.session.send("exit()\n")
                        time.sleep(0.5)
                    self.session.close()
                    self.session = None

                if self.ssh_client:
                    self.ssh_client.close()
                    self.ssh_client = None

                self.is_connected = False
                self.session_state = SessionState.UNKNOWN
                logger.info("SSH connection closed")

            except Exception as e:
                logger.warning(f"Error closing SSH connection: {e}")

    def ping(self) -> bool:
        """Test connection with a simple ping."""
        try:
            if self.session_state != SessionState.PYTHON:
                self.start_python_session()

            response = self.execute_python_command("print('ping')", timeout=5)
            return "ping" in response
        except Exception:
            return False

    def execute_command_batch(self, commands: List[Tuple[str, str]],
                              command_delay: float = 0.2,
                              show_progress: bool = True,
                              stop_on_error: bool = True,
                              timeout: Optional[int] = None) -> List[Dict[str, Any]]:
        """Execute a batch of commands with descriptions and formatted output.

        Args:
            commands: List of (description, command) tuples
            command_delay: Delay between commands in seconds
            show_progress: Whether to show progress output
            stop_on_error: Whether to stop execution on first error
            timeout: Timeout for each command (uses default if None)

        Returns:
            List of results: [{'description': str, 'command': str, 'success': bool, 'output': str, 'error': str}]
        """
        results = []
        total_commands = len(commands)

        for i, (description, command) in enumerate(commands, 1):
            if show_progress:
                print(f"\n[{i:2d}/{total_commands}] {description}")
                print(f"         -> {command}")

            result = {
                'description': description,
                'command': command,
                'success': False,
                'output': '',
                'error': ''
            }

            try:
                if self.session_state == SessionState.PYTHON:
                    response = self.execute_python_command(command, timeout)
                else:
                    response = self.execute_shell_command(command, timeout)

                result['success'] = True
                result['output'] = response

                if show_progress and response and response.strip():
                    lines = response.strip().split('\n')
                    for line in lines:
                        if (line.strip() and
                                not line.startswith('>>> ') and
                                not line.startswith('# ') and
                                command not in line):
                            print(f"         OK {line}")

                if command_delay > 0:
                    time.sleep(command_delay)

            except Exception as e:
                result['error'] = str(e)

                if show_progress:
                    print(f"         ERR Error: {e}")

                if stop_on_error:
                    results.append(result)
                    if show_progress:
                        print(f"\nStopping execution due to error in command {i}")
                    break

            results.append(result)

        return results

    def execute_python_batch(self, commands: List[Tuple[str, str]], **kwargs) -> List[Dict[str, Any]]:
        """Execute a batch of Python commands. Ensures Python mode first."""
        if self.session_state != SessionState.PYTHON:
            if kwargs.get('show_progress', True):
                print("Switching to Python mode...")
            self.start_python_session()
            if kwargs.get('show_progress', True):
                print(f"Now in {self.session_state.value} mode")

        return self.execute_command_batch(commands, **kwargs)

    def execute_shell_batch(self, commands: List[Tuple[str, str]], **kwargs) -> List[Dict[str, Any]]:
        """Execute a batch of shell commands. Ensures shell mode first."""
        if self.session_state != SessionState.SHELL:
            if kwargs.get('show_progress', True):
                print("Switching to shell mode...")
            self.switch_to_shell()
            if kwargs.get('show_progress', True):
                print(f"Now in {self.session_state.value} mode")

        return self.execute_command_batch(commands, **kwargs)

    def send_code_block(self, code: str, description: str = "Code block",
                        timeout: Optional[int] = None) -> Dict[str, Any]:
        """Send a multi-line code block (function, class, etc.) in Python mode."""
        if self.session_state != SessionState.PYTHON:
            raise Exception("Must be in Python mode to send code blocks")

        print(f"Sending {description}...")

        try:
            response = self.execute_python_command(code, timeout)

            if "Traceback" in response or "Error" in response:
                print(f"FAIL {description}: {response}")
                return {'success': False, 'error': response, 'output': response}
            else:
                print(f"OK {description} loaded successfully")
                return {'success': True, 'error': '', 'output': response}

        except Exception as e:
            print(f"FAIL {description} with exception: {e}")
            return {'success': False, 'error': str(e), 'output': ''}

    def get_connection_status(self) -> Dict[str, Any]:
        """Get detailed connection status."""
        return {
            "connected": self.is_connected,
            "hostname": self.hostname,
            "username": self.username,
            "session_state": self.session_state.value,
            "transport_active": self._is_connection_alive(),
            "can_ping": self.ping() if self.is_connected else False
        }

    @contextmanager
    def session_context(self):
        """Context manager for SSH sessions."""
        try:
            if not self.connect():
                raise Exception("Failed to establish SSH connection")
            yield self
        finally:
            self.close()
