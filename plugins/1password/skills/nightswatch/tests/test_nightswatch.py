#!/usr/bin/python3
from __future__ import annotations

"""Test shell ownership with fictional environment values and temporary files."""

import importlib.util
import json
import os
from pathlib import Path
import pty
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "nightswatch.py"
SPEC = importlib.util.spec_from_file_location("nightswatch_under_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
nw = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = nw
SPEC.loader.exec_module(nw)

AUTH_NAMES = (
    "OP_SERVICE_ACCOUNT_TOKEN", "OP_CONNECT_HOST", "OP_CONNECT_TOKEN",
    "OP_SESSION", "OP_SESSION_personal", "OP_SESSION_work",
)


def sample_state():
    return {
        "version": 1, "watch_id": str(uuid.uuid4()), "instance_id": str(uuid.uuid4()),
        "owner": {"pid": 201, "started": "owner-start"},
        "monitor": {"pid": 202, "started": "monitor-start"},
        "shell": {"pid": 203, "started": "shell-start"},
        "phase": "running", "reason": None, "cleanup": {"shell_closed": False},
    }


class EnvironmentAndIdentityTests(unittest.TestCase):
    def test_desktop_environment_removes_alternate_auth_without_mutating_parent(self):
        original = {name: "fictional-test-value" for name in AUTH_NAMES}
        original.update({
            "OP_BIOMETRIC_UNLOCK_ENABLED": "false", "PATH": "/usr/bin:/bin",
            "PROJECT_SETTING": "preserved", "NOT_OP_SESSION": "preserved",
        })
        before = dict(original)
        cleaned = nw.clean_environment(original)
        self.assertTrue(all(name not in cleaned for name in AUTH_NAMES))
        self.assertEqual(cleaned["OP_BIOMETRIC_UNLOCK_ENABLED"], "true")
        self.assertEqual(cleaned["PROJECT_SETTING"], "preserved")
        self.assertEqual(cleaned["NOT_OP_SESSION"], "preserved")
        self.assertEqual(cleaned["PATH"], "/usr/bin:/bin")
        self.assertEqual(original, before)

    def test_identity_requires_same_pid_and_start_time(self):
        identity = nw.ProcessIdentity(321, "first-start")
        self.assertTrue(nw.identity_alive(identity, lambda pid: identity))
        self.assertFalse(nw.identity_alive(identity, lambda pid: None))
        self.assertFalse(nw.identity_alive(
            identity, lambda pid: nw.ProcessIdentity(pid, "replacement-start")))
        self.assertFalse(nw.identity_alive(
            identity, lambda pid: nw.ProcessIdentity(pid + 1, identity.started)))

    def test_process_parser_preserves_command_with_spaces(self):
        info = nw.parse_process("321 7 Mon Oct 26 12:34:56 2026 /Applications/Example App/helper")
        self.assertIsNotNone(info)
        self.assertEqual(info.identity.pid, 321)
        self.assertEqual(info.ppid, 7)
        self.assertEqual(info.identity.started, "Mon Oct 26 12:34:56 2026")
        self.assertEqual(info.command, "/Applications/Example App/helper")

    def test_process_parser_rejects_missing_or_invalid_identity(self):
        for line in ("", "321", "not-a-pid 7 Mon Oct 26 12:34:56 2026 zsh"):
            with self.subTest(line=line):
                self.assertIsNone(nw.parse_process(line))


class StateAndOwnershipTests(unittest.TestCase):
    def test_readiness_requires_all_three_original_processes(self):
        state = sample_state()
        identities = {value["pid"]: nw.ProcessIdentity(**value)
                      for key, value in state.items() if key in ("owner", "monitor", "shell")}
        self.assertTrue(nw.inspect_state(state, identities.get)["shell_ready"])
        for key in ("owner", "monitor", "shell"):
            for reused in (False, True):
                with self.subTest(key=key, reused=reused):
                    current = dict(identities)
                    pid = state[key]["pid"]
                    if reused:
                        current[pid] = nw.ProcessIdentity(pid, "reused-pid-start")
                    else:
                        del current[pid]
                    receipt = nw.inspect_state(state, current.get)
                    self.assertEqual(receipt["status"], "stale")
                    self.assertFalse(receipt["shell_ready"])
                    self.assertFalse(receipt[key + "_alive"])

    def test_closed_receipt_preserves_unverified_cleanup(self):
        state = sample_state()
        state["phase"] = "closed"
        state["cleanup"] = {"shell_closed": False, "method": "unverified"}
        receipt = nw.inspect_state(state, lambda pid: None)
        self.assertEqual(receipt["status"], "stale")
        self.assertFalse(receipt["shell_ready"])
        self.assertEqual(receipt["cleanup"], state["cleanup"])

    def test_closed_receipt_is_stale_when_recorded_shell_is_still_alive(self):
        state = sample_state()
        state["phase"] = "closed"
        state["cleanup"] = {"shell_closed": True, "method": "already-exited"}
        shell = nw.ProcessIdentity(**state["shell"])
        receipt = nw.inspect_state(state, {shell.pid: shell}.get)
        self.assertEqual(receipt["status"], "stale")
        self.assertTrue(receipt["shell_alive"])
        self.assertFalse(receipt["shell_ready"])

    def test_owner_selection_prefers_outer_desktop_over_embedded_cli(self):
        entries = {
            21: nw.ProcessInfo(nw.ProcessIdentity(21, "shell"), 22, "/bin/zsh"),
            22: nw.ProcessInfo(nw.ProcessIdentity(22, "cli"), 23, "/tmp/codex"),
            23: nw.ProcessInfo(nw.ProcessIdentity(23, "app"), 1,
                               "/Applications/Codex.app/Contents/MacOS/Codex"),
        }
        self.assertEqual(nw.find_owner(21, entries.get), entries[23].identity)
        del entries[23]
        self.assertEqual(nw.find_owner(21, entries.get), entries[22].identity)

    def test_owner_selection_does_not_claim_unrelated_terminal(self):
        entry = nw.ProcessInfo(nw.ProcessIdentity(21, "shell"), 1, "/bin/zsh")
        with self.assertRaises(nw.WatchError):
            nw.find_owner(21, {21: entry}.get)

    def test_failed_child_cleanup_is_reported_without_claiming_success(self):
        class Child:
            def __init__(self):
                self.signals = []

            def poll(self):
                return None

            def send_signal(self, value):
                self.signals.append(value)

            def wait(self, timeout):
                raise subprocess.TimeoutExpired("fictional-child", timeout)

        child = Child()
        receipt = nw.stop_shell(child)
        self.assertFalse(receipt["shell_closed"])
        self.assertEqual(receipt["method"], "unverified")
        self.assertEqual(child.signals, [signal.SIGHUP, signal.SIGTERM, signal.SIGKILL])

    def test_reaped_child_is_not_signalled_again(self):
        class Child:
            def poll(self):
                return 0

            def send_signal(self, value):
                raise AssertionError("An already reaped child must not be signalled")

        self.assertTrue(nw.stop_shell(Child())["shell_closed"])

    def test_state_store_rejects_fifo_and_hardlinks_without_consuming_them(self):
        with tempfile.TemporaryDirectory(prefix="nightswatch-storage-") as root:
            name = str(uuid.uuid4()) + ".json"
            with nw.StateStore(root) as store:
                path = Path(root) / name
                os.mkfifo(path, 0o600)
                with self.assertRaises(nw.WatchError):
                    store.read(name)
                path.unlink()
                target = Path(root) / (str(uuid.uuid4()) + ".json")
                target.write_text("{}")
                target.chmod(0o600)
                os.link(target, path)
                with self.assertRaises(nw.WatchError):
                    store.read(name)
                self.assertEqual(target.read_text(), "{}")


@unittest.skipUnless(sys.platform == "darwin", "CLI process identities use macOS ps")
class ShellLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="nightswatch-tests-")
        self.root = Path(self.temp.name)
        self.state_dir = self.root / "state"
        self.watch_id = str(uuid.uuid4())
        self.monitors = []
        self.shell_identities = []
        self.env = {
            "HOME": str(self.root), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": "C", "LC_ALL": "C", "TERM": "dumb",
            "OP_BIOMETRIC_UNLOCK_ENABLED": "false", "PROJECT_SETTING": "preserved",
        }
        self.env.update({name: "fictional-test-value" for name in AUTH_NAMES})
        self.owner = subprocess.Popen(
            [sys.executable, "-I", "-B", "-c", "import time; time.sleep(90)"],
            env=self.env, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.shell = self.root / "fake-shell"
        self.shell.write_text(
            "#!" + sys.executable + " -B\n"
            "import json, os, signal, sys, time\n"
            "from pathlib import Path\n"
            "base = Path(__file__).parent\n"
            "names = " + repr(AUTH_NAMES) + "\n"
            "report = {'pid': os.getpid(), 'args': sys.argv[1:],\n"
            "          'auth_absent': all(name not in os.environ for name in names),\n"
            "          'desktop_enabled': os.environ.get('OP_BIOMETRIC_UNLOCK_ENABLED') == 'true',\n"
            "          'project_kept': os.environ.get('PROJECT_SETTING') == 'preserved'}\n"
            "with (base / 'fake-shell.calls').open('a') as stream:\n"
            "    stream.write(json.dumps(report) + '\\n')\n"
            "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))\n"
            "time.sleep(90)\n",
            encoding="utf-8",
        )
        self.shell.chmod(0o700)

    def tearDown(self):
        try:
            self.cli("end", timeout=8)
        except (OSError, subprocess.TimeoutExpired):
            pass
        for process, master, slave in self.monitors:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=4)
            os.close(master)
            os.close(slave)
        for identity in self.shell_identities:
            if nw.identity_alive(identity, nw.process_identity):
                try:
                    os.kill(identity.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        if self.owner.poll() is None:
            self.owner.terminate()
        self.owner.wait(timeout=4)
        self.temp.cleanup()

    def cli(self, verb, *extra, timeout=8):
        return subprocess.run(
            [sys.executable, "-I", "-B", str(SCRIPT), verb,
             "--watch-id", self.watch_id, "--state-dir", str(self.state_dir), *extra],
            env=self.env, capture_output=True, text=True, timeout=timeout,
        )

    def status(self):
        result = self.cli("status")
        try:
            return result.returncode, json.loads(result.stdout)
        except ValueError:
            self.fail("Status did not return JSON: " + result.stderr)

    def wait_for(self, predicate, message, timeout=6):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        self.fail(message)

    def start(self, wait_ready=True):
        master, slave = pty.openpty()
        process = subprocess.Popen(
            [sys.executable, "-I", "-B", str(SCRIPT), "start",
             "--watch-id", self.watch_id, "--owner-pid", str(self.owner.pid),
             "--state-dir", str(self.state_dir), "--shell", str(self.shell)],
            env=self.env, stdin=slave, stdout=slave, stderr=slave,
        )
        # macOS can discard unread PTY output when the last slave closes.
        # Keep the parent's descriptor until the receipt has been inspected.
        os.set_blocking(master, False)
        self.monitors.append((process, master, slave))
        if wait_ready:
            self.wait_for(lambda: self.status()[1].get("shell_ready") is True,
                          "Fake shell never became ready")
            _, receipt = self.status()
            identity = nw.process_identity(receipt["shell_pid"])
            self.assertIsNotNone(identity)
            self.shell_identities.append(identity)
        return process, master

    def drain(self, master):
        chunks = []
        while True:
            try:
                chunk = os.read(master, 65536)
            except (BlockingIOError, OSError):
                break
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks).decode("utf-8", "replace")

    def shell_calls(self):
        return [json.loads(line) for line in (self.root / "fake-shell.calls").read_text().splitlines()]

    def test_start_scrubs_environment_and_end_is_idempotent(self):
        monitor, master = self.start()
        self.wait_for(lambda: (self.root / "fake-shell.calls").exists(), "Fake shell did not report")
        report = self.shell_calls()[0]
        self.assertTrue(report["auth_absent"])
        self.assertTrue(report["desktop_enabled"])
        self.assertTrue(report["project_kept"])
        self.assertEqual(report["args"], ["-f"])
        self.assertEqual(self.state_dir.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.state_dir / (self.watch_id + ".json")).stat().st_mode & 0o777, 0o600)
        ended = self.cli("end")
        self.assertEqual(ended.returncode, 0, ended.stderr)
        monitor.wait(timeout=6)
        self.assertEqual(self.cli("end").returncode, 0)
        _, receipt = self.status()
        self.assertEqual(receipt["status"], "closed")
        self.assertTrue(receipt["cleanup"]["shell_closed"])
        self.assertFalse(receipt["shell_ready"])
        self.assertIsNone(self.owner.poll(), "End must not terminate the borrowed owner")
        self.assertNotIn("fictional-test-value", self.drain(master) + ended.stdout + ended.stderr)

    def test_duplicate_start_reuses_existing_watch(self):
        first, _ = self.start()
        _, before = self.status()
        second, master = self.start(wait_ready=False)
        self.assertEqual(second.wait(timeout=5), 0)
        output = self.drain(master)
        receipts = [json.loads(line) for line in output.splitlines() if line.strip().startswith("{")]
        self.assertTrue(any(receipt.get("existing") is True for receipt in receipts), repr(output))
        _, after = self.status()
        self.assertEqual(after["instance_id"], before["instance_id"])
        self.assertEqual(after["monitor_pid"], before["monitor_pid"])
        self.assertEqual(after["shell_pid"], before["shell_pid"])
        self.assertEqual(len(self.shell_calls()), 1)
        self.assertIsNone(first.poll())

    def test_owner_exit_closes_only_its_shell(self):
        monitor, _ = self.start()
        self.owner.terminate()
        self.owner.wait(timeout=4)
        self.wait_for(lambda: self.status()[1].get("status") == "closed", "Owner exit did not close watch")
        monitor.wait(timeout=5)
        _, receipt = self.status()
        self.assertTrue(receipt["cleanup"]["shell_closed"])
        self.assertFalse(receipt["shell_ready"])

    def test_shell_exit_closes_watch_without_ending_owner(self):
        monitor, _ = self.start()
        identity = self.shell_identities[-1]
        self.assertTrue(nw.identity_alive(identity, nw.process_identity))
        os.kill(identity.pid, signal.SIGTERM)
        self.wait_for(lambda: self.status()[1].get("status") == "closed", "Shell exit did not close watch")
        monitor.wait(timeout=5)
        self.assertIsNone(self.owner.poll())

    def test_missing_end_is_safe_and_idempotent(self):
        self.assertEqual(self.cli("end").returncode, 0)
        self.assertEqual(self.cli("end").returncode, 0)
        code, receipt = self.status()
        self.assertEqual(code, 1)
        self.assertEqual(receipt["status"], "missing")
        self.assertIsNone(self.owner.poll())

    def test_malformed_state_is_not_overwritten_or_used_to_spawn(self):
        self.state_dir.mkdir(mode=0o700)
        state_file = self.state_dir / (self.watch_id + ".json")
        original = "{malformed-state"
        state_file.write_text(original)
        state_file.chmod(0o600)
        status = self.cli("status")
        self.assertNotEqual(status.returncode, 0)
        self.assertEqual(state_file.read_text(), original)
        monitor, _ = self.start(wait_ready=False)
        self.assertNotEqual(monitor.wait(timeout=5), 0)
        self.assertEqual(state_file.read_text(), original)
        self.assertFalse((self.root / "fake-shell.calls").exists())

    def test_symlinked_state_is_rejected_without_following_or_replacing_it(self):
        self.state_dir.mkdir(mode=0o700)
        destination = self.root / "unrelated.json"
        destination.write_text("{}")
        destination.chmod(0o600)
        state_file = self.state_dir / (self.watch_id + ".json")
        state_file.symlink_to(destination)
        self.assertNotEqual(self.cli("status").returncode, 0)
        monitor, _ = self.start(wait_ready=False)
        self.assertNotEqual(monitor.wait(timeout=5), 0)
        self.assertTrue(state_file.is_symlink())
        self.assertEqual(destination.read_text(), "{}")
        self.assertFalse((self.root / "fake-shell.calls").exists())

    def test_stale_pid_state_never_terminates_the_unrelated_live_process(self):
        self.state_dir.mkdir(mode=0o700)
        state = sample_state()
        state["watch_id"] = self.watch_id
        identity = {"pid": self.owner.pid, "started": "an-older-process-with-this-pid"}
        for key in ("owner", "monitor", "shell"):
            state[key] = dict(identity)
        state_file = self.state_dir / (self.watch_id + ".json")
        state_file.write_text(json.dumps(state))
        state_file.chmod(0o600)
        code, receipt = self.status()
        self.assertEqual(code, 1)
        self.assertEqual(receipt["status"], "stale")
        self.assertFalse(receipt["shell_ready"])
        self.cli("end")
        self.assertIsNone(self.owner.poll(), "Stale state must not authorize signalling a reused PID")
        self.assertEqual(json.loads(state_file.read_text()), state)

    def test_old_generation_stop_marker_does_not_end_new_watch(self):
        self.state_dir.mkdir(mode=0o700)
        old_instance = str(uuid.uuid4())
        marker = self.state_dir / (self.watch_id + "." + old_instance + ".stop.json")
        marker.write_text(json.dumps({"instance_id": old_instance}))
        marker.chmod(0o600)
        monitor, _ = self.start()
        _, receipt = self.status()
        self.assertNotEqual(receipt["instance_id"], old_instance)
        self.assertTrue(receipt["shell_ready"])
        self.assertIsNone(monitor.poll())

    def test_end_reports_incomplete_cleanup_without_erasing_evidence(self):
        self.state_dir.mkdir(mode=0o700)
        state = sample_state()
        state["watch_id"] = self.watch_id
        identity = nw.process_identity(self.owner.pid)
        self.assertIsNotNone(identity)
        state["owner"] = state["monitor"] = identity.as_dict()
        state["shell"] = None
        state["phase"] = "closed"
        state["cleanup"] = {"shell_closed": False, "method": "unverified"}
        state_file = self.state_dir / (self.watch_id + ".json")
        state_file.write_text(json.dumps(state))
        state_file.chmod(0o600)
        result = self.cli("end")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)["status"], "stale")
        self.assertEqual(json.loads(state_file.read_text()), state)
        self.assertIsNone(self.owner.poll())


if __name__ == "__main__":
    unittest.main()
