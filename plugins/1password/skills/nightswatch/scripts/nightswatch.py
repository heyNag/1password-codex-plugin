#!/usr/bin/python3
"""Keep one desktop-approved shell open in the caller's foreground terminal.

This supervisor never authenticates, runs credential commands, records shell
input/output, or changes power settings. The host owns the terminal handle.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import signal
import stat
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Optional


POLL_SECONDS = 0.25
OWNER_CHECK_SECONDS = 2.0
END_WAIT_SECONDS = 6.0
MAX_STATE_BYTES = 65536


class WatchError(Exception):
    """A deliberately value-free error suitable for a JSON receipt."""


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    started: str

    def as_dict(self) -> dict:
        return {"pid": self.pid, "started": self.started}


@dataclass(frozen=True)
class ProcessInfo:
    identity: ProcessIdentity
    ppid: int
    command: str


def parse_process(line: str) -> Optional[ProcessInfo]:
    """Parse LC_ALL=C ps pid,ppid,lstart,comm, without reading arguments."""
    parts = line.strip().split(None, 7)
    if len(parts) != 8:
        return None
    try:
        pid, ppid = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if pid <= 1 or ppid < 0:
        return None
    return ProcessInfo(ProcessIdentity(pid, " ".join(parts[2:7])), ppid, parts[7])


def process_info(pid: int) -> Optional[ProcessInfo]:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        return None
    try:
        result = subprocess.run(
            ["/bin/ps", "-ww", "-p", str(pid), "-o", "pid=,ppid=,lstart=,comm="],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, timeout=2, env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
        )
        return parse_process(result.stdout) if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def process_identity(pid: int) -> Optional[ProcessIdentity]:
    info = process_info(pid)
    return info.identity if info else None


def identity_from(value: object) -> Optional[ProcessIdentity]:
    if not isinstance(value, dict):
        return None
    pid, started = value.get("pid"), value.get("started")
    if (not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1
            or not isinstance(started, str) or not started or len(started) > 128
            or not started.isprintable()):
        return None
    return ProcessIdentity(pid, started)


def identity_alive(identity: Optional[ProcessIdentity],
                   process: Callable[[int], Optional[ProcessIdentity]] = process_identity) -> bool:
    return identity is not None and process(identity.pid) == identity


def find_owner(start_pid: int, provider: Callable[[int], Optional[ProcessInfo]] = process_info) -> ProcessIdentity:
    """Prefer the outer desktop app; an embedded CodexCLI is not its owner."""
    seen = set()
    desktop = None
    cli = None
    pid = start_pid
    while pid > 1 and pid not in seen and len(seen) < 100:
        seen.add(pid)
        info = provider(pid)
        if info is None:
            break
        match = re.search(r"/([^/]+)\.app/Contents/MacOS/[^/]+$", info.command)
        if match and match.group(1).lower() in ("codex", "chatgpt", "opencodex"):
            desktop = info.identity
        if os.path.basename(info.command).lower() in ("codex", "codex-cli", "opencodex"):
            cli = info.identity
        pid = info.ppid
    if desktop or cli:
        return desktop or cli  # type: ignore[return-value]
    raise WatchError("owner-not-found: supply the verified owning app or CLI PID with --owner-pid")


def clean_environment(environment: Dict[str, str]) -> Dict[str, str]:
    """Desktop integration only, restricted to this child shell's environment."""
    excluded = {"OP_SERVICE_ACCOUNT_TOKEN", "OP_CONNECT_HOST", "OP_CONNECT_TOKEN"}
    cleaned = {key: value for key, value in environment.items()
               if key not in excluded and not key.startswith("OP_SESSION")}
    cleaned["OP_BIOMETRIC_UNLOCK_ENABLED"] = "true"
    return cleaned


def valid_uuid(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError):
        raise WatchError("invalid-watch-id") from None


class StateStore:
    """Private, descriptor-relative files; no followed symlinks or hardlinks."""

    def __init__(self, directory: str):
        self.path = os.path.abspath(os.path.expanduser(directory))
        self.fd = -1
        try:
            os.makedirs(self.path, mode=0o700, exist_ok=True)
            self.fd = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            info = os.fstat(self.fd)
            if info.st_uid != os.geteuid() or not stat.S_ISDIR(info.st_mode):
                raise WatchError("unsafe-state-directory")
            os.fchmod(self.fd, 0o700)
        except (OSError, WatchError):
            self.close()
            raise WatchError("unsafe-state-directory") from None

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    @staticmethod
    def _name(name: str) -> str:
        if not re.fullmatch(r"[a-f0-9.-]+(?:\.stop)?\.(?:json|lock)", name):
            raise WatchError("invalid-state-name")
        return name

    @staticmethod
    def _check_file(fd: int) -> None:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_nlink != 1 or info.st_mode & 0o077):
            raise WatchError("unsafe-state-file")

    def read(self, name: str) -> Optional[dict]:
        try:
            fd = os.open(self._name(name), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.fd)
        except FileNotFoundError:
            return None
        except OSError:
            raise WatchError("unsafe-state-file") from None
        try:
            self._check_file(fd)
            with os.fdopen(fd, "r", encoding="utf-8") as stream:
                fd = -1
                data = stream.read(MAX_STATE_BYTES + 1)
            if len(data) > MAX_STATE_BYTES:
                raise WatchError("invalid-state-file")
            result = json.loads(data)
            if not isinstance(result, dict):
                raise WatchError("invalid-state-file")
            return result
        except (ValueError, UnicodeError):
            raise WatchError("invalid-state-file") from None
        finally:
            if fd >= 0:
                os.close(fd)

    def write(self, name: str, value: dict) -> None:
        name = self._name(name)
        # Refuse an unsafe existing destination even though replace would not follow it.
        self.read(name)
        temporary = str(uuid.uuid4()) + ".json"
        fd = -1
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=self.fd)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                fd = -1
                json.dump(value, stream, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
        except OSError:
            raise WatchError("state-write-failed") from None
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(temporary, dir_fd=self.fd)
            except FileNotFoundError:
                pass

    def lock(self, watch_id: str) -> Optional[int]:
        fd = -1
        try:
            fd = os.open(self._name(watch_id + ".lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                         0o600, dir_fd=self.fd)
            self._check_file(fd)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            if fd >= 0:
                os.close(fd)
            return None
        except (OSError, WatchError):
            if fd >= 0:
                os.close(fd)
            raise WatchError("unsafe-watch-lock") from None

    def remove(self, name: str) -> None:
        self.read(name)
        try:
            os.unlink(self._name(name), dir_fd=self.fd)
        except FileNotFoundError:
            pass


def load_state(store: StateStore, watch_id: str) -> Optional[dict]:
    state = store.read(watch_id + ".json")
    if state is None:
        return None
    if (state.get("version") != 1 or state.get("watch_id") != watch_id
            or state.get("phase") not in ("starting", "running", "closed")
            or identity_from(state.get("owner")) is None
            or identity_from(state.get("monitor")) is None
            or (state.get("shell") is not None and identity_from(state["shell"]) is None)):
        raise WatchError("invalid-watch-state")
    if valid_uuid(state.get("instance_id")) != state.get("instance_id"):
        raise WatchError("invalid-watch-state")
    if state.get("reason") not in (None, "shell-exited", "owner-exited", "ended",
                                   "monitor-signalled", "startup-or-monitor-failed"):
        raise WatchError("invalid-watch-state")
    cleanup = state.get("cleanup")
    if (not isinstance(cleanup, dict) or not isinstance(cleanup.get("shell_closed"), bool)
            or set(cleanup) - {"shell_closed", "method"}
            or cleanup.get("method") not in (None, "already-exited", "hangup", "terminate",
                                              "kill", "unverified", "not-started")):
        raise WatchError("invalid-watch-state")
    return state


def inspect_state(state: dict,
                  process: Callable[[int], Optional[ProcessIdentity]] = process_identity) -> dict:
    identities = {key: identity_from(state.get(key)) for key in ("owner", "monitor", "shell")}
    alive = {key: identity_alive(value, process) for key, value in identities.items()}
    phase = state.get("phase")
    healthy = all(alive.values())
    closed = phase == "closed" and state.get("cleanup", {}).get("shell_closed") is True and not alive["shell"]
    status = "closed" if closed else "running" if phase == "running" and healthy else (
        "starting" if phase == "starting" and alive["owner"] and alive["monitor"] else "stale")
    return {
        "watch_id": state["watch_id"], "instance_id": state["instance_id"], "status": status,
        "shell_ready": status == "running", "reason": state.get("reason"),
        "cleanup": state.get("cleanup", {"shell_closed": False}),
        **{key + "_alive": value for key, value in alive.items()},
        **{key + "_pid": value.pid if value else None for key, value in identities.items()},
    }


def emit(value: dict) -> None:
    print(json.dumps(value, sort_keys=True), flush=True)


def stop_shell(child: subprocess.Popen) -> dict:
    """Signal only this monitor's unreaped child, never a PID from a state file."""
    if child.poll() is not None:
        return {"shell_closed": True, "method": "already-exited"}
    for sig, name, timeout in ((signal.SIGHUP, "hangup", 0.75),
                               (signal.SIGTERM, "terminate", 0.75),
                               (signal.SIGKILL, "kill", 1.0)):
        try:
            child.send_signal(sig)
            child.wait(timeout=timeout)
            return {"shell_closed": True, "method": name}
        except subprocess.TimeoutExpired:
            continue
        except (OSError, ChildProcessError):
            if child.poll() is not None:
                return {"shell_closed": True, "method": "already-exited"}
    return {"shell_closed": child.poll() is not None, "method": "unverified"}


def stop_name(state: dict) -> str:
    return state["watch_id"] + "." + state["instance_id"] + ".stop.json"


def start(args: argparse.Namespace, store: StateStore) -> int:
    watch_id = valid_uuid(args.watch_id) if args.watch_id else str(uuid.uuid4())
    lock = store.lock(watch_id)
    if lock is None:
        # A concurrent start may hold the lock just before its first state write.
        deadline = time.monotonic() + 1.0
        state = load_state(store, watch_id)
        while state is None and time.monotonic() < deadline:
            time.sleep(0.05)
            state = load_state(store, watch_id)
        receipt = inspect_state(state) if state else {"watch_id": watch_id, "status": "starting", "shell_ready": False}
        emit(dict(receipt, existing=True))
        return 0 if receipt["status"] in ("running", "starting") else 1
    child = None
    state = None
    old_handlers = {}
    stopping = []
    result = 0
    try:
        previous = load_state(store, watch_id)
        if previous and previous["phase"] != "closed" and identity_alive(identity_from(previous.get("monitor"))):
            raise WatchError("watch-lock-inconsistent: existing monitor must close first")
        if previous and identity_alive(identity_from(previous.get("shell"))):
            raise WatchError("previous-shell-still-running: close it through its original terminal before restarting")
        if not os.isatty(sys.stdin.fileno()):
            raise WatchError("terminal-required: start through the host terminal tool with tty=true")
        if not os.path.isabs(args.shell) or not os.path.isfile(args.shell) or not os.access(args.shell, os.X_OK):
            raise WatchError("shell-must-be-an-absolute-executable")
        owner = process_identity(args.owner_pid) if args.owner_pid is not None else find_owner(os.getppid())
        if owner is None:
            raise WatchError("owner-not-running")
        monitor = process_identity(os.getpid())
        if monitor is None:
            raise WatchError("monitor-identity-unavailable")
        state = {"version": 1, "watch_id": watch_id, "instance_id": str(uuid.uuid4()),
                 "owner": owner.as_dict(), "monitor": monitor.as_dict(), "shell": None,
                 "phase": "starting", "reason": None, "cleanup": {"shell_closed": False}}
        store.write(watch_id + ".json", state)
        # A caught (rather than ignored) SIGINT becomes default in the exec'd child.
        # The parent stays present when a terminal user cancels a shell command.
        old_handlers[signal.SIGINT] = signal.signal(signal.SIGINT, lambda _sig, _frame: None)
        for sig in (signal.SIGTERM, signal.SIGHUP):
            old_handlers[sig] = signal.signal(sig, lambda number, _frame: stopping.append(number))
        try:
            child = subprocess.Popen([args.shell, "-f"], env=clean_environment(dict(os.environ)), close_fds=True)
        except OSError:
            raise WatchError("shell-start-failed") from None
        shell_identity = process_identity(child.pid)
        if child.poll() is not None or shell_identity is None:
            raise WatchError("shell-exited-before-ready")
        state["shell"] = shell_identity.as_dict()
        state["phase"] = "running"
        store.write(watch_id + ".json", state)
        emit(inspect_state(state))
        next_owner_check = time.monotonic()
        while True:
            if stopping:
                state["reason"] = "monitor-signalled"
                break
            if child.poll() is not None:
                state["reason"] = "shell-exited"
                break
            if time.monotonic() >= next_owner_check:
                if not identity_alive(owner):
                    state["reason"] = "owner-exited"
                    break
                next_owner_check = time.monotonic() + OWNER_CHECK_SECONDS
            marker = store.read(stop_name(state))
            if marker and marker.get("instance_id") == state["instance_id"]:
                state["reason"] = "ended"
                break
            time.sleep(POLL_SECONDS)
    except WatchError:
        if state is not None:
            state["reason"] = "startup-or-monitor-failed"
        result = 1
        raise
    finally:
        if state is not None:
            state["cleanup"] = stop_shell(child) if child is not None else {"shell_closed": True, "method": "not-started"}
            state["phase"] = "closed"
            try:
                store.write(watch_id + ".json", state)
                store.remove(stop_name(state))
                emit(inspect_state(state))
            except (WatchError, OSError):
                emit({"watch_id": watch_id, "status": "closed" if state["cleanup"]["shell_closed"] else "stale", "shell_ready": False,
                      "cleanup": state["cleanup"], "error": "final-state-write-failed"})
            if not state["cleanup"]["shell_closed"]:
                result = 1
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)
        os.close(lock)
    return result


def status(args: argparse.Namespace, store: StateStore) -> int:
    watch_id = valid_uuid(args.watch_id)
    state = load_state(store, watch_id)
    receipt = inspect_state(state) if state else {"watch_id": watch_id, "status": "missing", "shell_ready": False}
    emit(receipt)
    return 1 if receipt["status"] in ("missing", "stale") else 0


def end(args: argparse.Namespace, store: StateStore) -> int:
    watch_id = valid_uuid(args.watch_id)
    state = load_state(store, watch_id)
    if state is None:
        emit({"watch_id": watch_id, "status": "closed", "shell_ready": False,
              "already_closed": True, "cleanup": {"shell_closed": True, "method": "not-found"}})
        return 0
    receipt = inspect_state(state)
    if state["phase"] == "closed":
        emit(dict(receipt, already_closed=True))
        return 0 if receipt["status"] == "closed" else 1
    if not receipt["monitor_alive"]:
        emit(dict(receipt, cleanup={"shell_closed": not receipt["shell_alive"], "method": "monitor-missing"}))
        return 1 if receipt["shell_alive"] else 0
    store.write(stop_name(state), {"instance_id": state["instance_id"]})
    deadline = time.monotonic() + END_WAIT_SECONDS
    while time.monotonic() < deadline:
        current = load_state(store, watch_id)
        if current is None or current["instance_id"] != state["instance_id"]:
            emit({"watch_id": watch_id, "instance_id": state["instance_id"], "status": "stale",
                  "shell_ready": False, "reason": "watch-replaced-during-end"})
            return 1
        receipt = inspect_state(current)
        if current["phase"] == "closed":
            emit(receipt)
            return 0 if receipt["status"] == "closed" else 1
        if not receipt["monitor_alive"]:
            break
        time.sleep(POLL_SECONDS)
    emit(dict(receipt, end_pending=True))
    return 1


def main(argv) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    default_state = str(Path.home() / "Library" / "Application Support" / "Nightswatch")
    for name in ("start", "status", "end"):
        command = commands.add_parser(name)
        command.add_argument("--watch-id", required=name != "start")
        command.add_argument("--state-dir", default=default_state)
        if name == "start":
            command.add_argument("--owner-pid", type=int)
            command.add_argument("--shell", default="/bin/zsh")
    args = parser.parse_args(argv)
    try:
        if sys.platform != "darwin":
            raise WatchError("macos-required")
        if os.geteuid() == 0:
            raise WatchError("must-run-as-the-desktop-user-not-root")
        with StateStore(args.state_dir) as store:
            return {"start": start, "status": status, "end": end}[args.command](args, store)
    except (WatchError, OSError):
        # Arbitrary OS paths, environment contents and child diagnostics never enter errors.
        error = sys.exc_info()[1]
        emit({"status": "error", "error": str(error) if isinstance(error, WatchError) else "local-operation-failed"})
        return 1
    except Exception:
        emit({"status": "error", "error": "unexpected-helper-failure"})
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
