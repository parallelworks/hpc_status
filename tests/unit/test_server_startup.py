"""Starting up: claiming a port, and saying so when we cannot.

Running the dashboard on an ACTIVATE workspace failed with

    OSError: [Errno 98] Address already in use

four frames deep in socketserver, printed *after* a full fleet scrape and
two started worker threads — because port 8080 on those nodes belongs to
Grafana. These tests cover the two halves of that fix: bind before doing
any work, and explain the conflict in terms of what is actually holding
the port.
"""

import socket
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from src.server.main import _create_server, _identify_listener

REPO = Path(__file__).resolve().parents[2]
RUN_SH = REPO / "scripts" / "run.sh"


@pytest.fixture
def occupied_port():
    """A port held by an HTTP server that announces itself as Grafana."""

    class Handler(BaseHTTPRequestHandler):
        server_version = "nginx/1.2"

        def do_GET(self):
            body = b"<html><head><title>Grafana</title></head><body>hi</body></html>"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()


class TestIdentifyListener:
    def test_names_the_service_holding_the_port(self, occupied_port):
        """lsof and ss show nothing for another user's process; HTTP does."""
        assert "Grafana" in _identify_listener(occupied_port)

    def test_falls_back_to_the_server_header(self, occupied_port):
        """A page with no title still identifies itself in its headers."""
        description = _identify_listener(occupied_port)
        assert description.startswith("an HTTP server")

    def test_survives_a_port_that_does_not_speak_http(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        try:
            assert "not an HTTP server" in _identify_listener(listener.getsockname()[1])
        finally:
            listener.close()

    def test_survives_a_closed_port(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        # No exception, no hang — just a vague answer.
        assert _identify_listener(port)


class TestCreateServer:
    def test_busy_port_exits_instead_of_raising_oserror(self, occupied_port, capsys):
        with pytest.raises(SystemExit) as exc:
            _create_server("127.0.0.1", occupied_port, None)
        assert exc.value.code == 1

        output = capsys.readouterr().out
        assert f"Port {occupied_port} is already in use" in output
        assert "Grafana" in output, "the message must say what is in the way"
        assert "pw endpoints run" in output, "and how to avoid choosing a port"

    def test_port_zero_gets_a_free_port(self):
        server = _create_server("127.0.0.1", 0, None)
        try:
            assert server.server_address[1] > 0
        finally:
            server.server_close()

    def test_unexpected_errors_still_raise(self):
        """Only address conflicts get the friendly treatment."""
        with pytest.raises(OSError):
            _create_server("203.0.113.1", 9, None)  # not a local address


class TestPortArgument:
    def test_zero_is_a_real_choice_not_an_absent_one(self):
        import sys

        import src.server.main as main_module

        argv = sys.argv
        try:
            sys.argv = ["prog", "--port", "0"]
            args = main_module.parse_args()
        finally:
            sys.argv = argv
        assert args.port == 0, "--port 0 must survive as 0, not read as unset"

    def test_no_port_flag_means_none(self):
        import sys

        import src.server.main as main_module

        argv = sys.argv
        try:
            sys.argv = ["prog"]
            args = main_module.parse_args()
        finally:
            sys.argv = argv
        assert args.port is None, "an absent --port must let the config file win"


class TestRunScriptPortHandling:
    """scripts/run.sh used to kill whatever held the port.

    On a laptop that is a convenient restart. On a shared workspace it is
    someone else's Grafana, so the match is now against our own command
    line rather than the port.
    """

    def run_in_shell(self, snippet: str, port: str = "8080") -> str:
        # The assignment needs its own line: `VAR=x source file` scopes the
        # assignment to the builtin, so it is gone by the next command.
        script = f'PORT={port}\nsource "{RUN_SH}"\n{snippet}\n'
        result = subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            cwd=REPO,
            timeout=30,
        )
        return result.stdout + result.stderr

    def test_sourcing_does_not_start_a_dashboard(self):
        """The helpers must be reachable without launching the server."""
        assert "Starting dashboard" not in self.run_in_shell("echo sourced")
        assert "sourced" in self.run_in_shell("echo sourced")

    def test_port_is_free_detects_a_listener(self, occupied_port):
        out = self.run_in_shell(
            f"port_is_free {occupied_port} && echo FREE || echo BUSY"
        )
        assert "BUSY" in out

    def test_port_is_free_on_an_unused_port(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        out = self.run_in_shell(f"port_is_free {port} && echo FREE || echo BUSY")
        assert "FREE" in out

    def test_default_port_moves_aside_for_another_service(self, occupied_port):
        """An unset PORT is a suggestion: step around whatever is there."""
        out = self.run_in_shell(
            "PORT_EXPLICIT=0; select_port; echo CHOSE=$PORT", port=str(occupied_port)
        )
        assert f"CHOSE={occupied_port}" not in out
        assert "is in use by another service" in out

    def test_explicit_port_is_never_moved(self, occupied_port):
        """`pw endpoints run` tunnels to the port it assigned — stay on it."""
        out = self.run_in_shell(
            "PORT_EXPLICIT=1; select_port; echo CHOSE=$PORT", port=str(occupied_port)
        )
        assert f"CHOSE={occupied_port}" in out

    def test_cleanup_ignores_processes_that_are_not_ours(self, occupied_port):
        """The old version killed the port holder whatever it was."""
        # A long-lived process that merely mentions the port, the way an
        # unrelated service might.
        victim = subprocess.Popen(
            ["sleep", "45"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            self.run_in_shell("cleanup_existing", port=str(occupied_port))
            assert victim.poll() is None, "cleanup killed an unrelated process"
        finally:
            victim.terminate()
            victim.wait(timeout=10)

    def test_cleanup_pattern_matches_only_our_own_dashboard(self):
        """The pgrep pattern is the whole safety property; pin its shape."""
        source = RUN_SH.read_text()
        assert "src\\.server\\.main .*--port" in source, (
            "cleanup must match our own command line, not the port alone"
        )
        assert "lsof -ti" not in source and "netstat -tulpn" not in source, (
            "killing by port alone is what took down other services"
        )
