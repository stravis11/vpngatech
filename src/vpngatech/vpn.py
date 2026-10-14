"""openconnect plumbing: gateway login, tunnel daemon, status, and keyring."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import re
import shlex
import shutil
import socket
import struct
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

# GT's portal (vpn.gatech.edu) authenticates but its config lists no gateways,
# so we log in to a gateway directly, the way the Windows/Mac client ends up.
GATEWAYS = {
    "dc-ext-gw.vpn.gatech.edu": "DC",
    "ni-ext-gw.vpn.gatech.edu": "NI",
}
DEFAULT_GATEWAY = "dc-ext-gw.vpn.gatech.edu"

# Duo answers accepted at the GlobalProtect "Challenge:" prompt.
DUO_METHODS = {"push": "Duo Push", "phone": "Phone call", "passcode": "Passcode"}

INTERFACE = "gatech0"
PID_FILE = Path("/run/vpngatech.pid")
HIP_WRAPPER = Path("/usr/lib/openconnect/hipreport.sh")
OPENCONNECT = os.environ.get("VPNGATECH_OPENCONNECT", "openconnect")

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "vpngatech"
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "vpngatech"
CONFIG_FILE = CONFIG_DIR / "config.json"
SESSION_FILE = STATE_DIR / "session.json"
LOG_FILE = STATE_DIR / "openconnect.log"
LOG_MAX_BYTES = 1_000_000

# Seconds of silence after a partial line before we treat it as a prompt.
PROMPT_QUIET = 0.5


class VpnError(Exception):
    pass


@dataclass
class Config:
    username: str = ""
    gateway: str = DEFAULT_GATEWAY
    duo: str = "push"


def load_config() -> Config:
    try:
        data = json.loads(CONFIG_FILE.read_text())
    except (OSError, ValueError):
        return Config()
    fields = Config.__dataclass_fields__
    return Config(**{k: v for k, v in data.items() if k in fields and isinstance(v, str)})


def save_config(config: Config) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(asdict(config), indent=2) + "\n")


# --- log -------------------------------------------------------------------


def prepare_log() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        if LOG_FILE.stat().st_size > LOG_MAX_BYTES:
            LOG_FILE.replace(LOG_FILE.with_suffix(".log.1"))
    except FileNotFoundError:
        pass
    LOG_FILE.touch()


def log(message: str) -> None:
    """Append a line in the same "[HH:MM:SS]" style openconnect --timestamp uses."""
    stamp = datetime.now().strftime("%H:%M:%S")
    with LOG_FILE.open("a") as f:
        for line in message.splitlines() or [""]:
            f.write(f"[{stamp}] {line}\n")


# --- status ----------------------------------------------------------------


@dataclass
class Status:
    pid: int | None
    address: str | None
    gateway: str | None
    since: float | None

    @property
    def connected(self) -> bool:
        return self.pid is not None


def _openconnect_pid() -> int | None:
    try:
        pid = int(PID_FILE.read_text().strip())
        comm = Path(f"/proc/{pid}/comm").read_text().strip()
    except (OSError, ValueError):
        return None
    return pid if comm == "openconnect" else None


def _iface_ipv4(name: str) -> str | None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            res = fcntl.ioctl(s.fileno(), 0x8915, struct.pack("256s", name[:15].encode()))
        except OSError:
            return None
    return socket.inet_ntoa(res[20:24])


def status() -> Status:
    pid = _openconnect_pid()
    if pid is None:
        return Status(None, None, None, None)
    gateway = since = None
    try:
        session = json.loads(SESSION_FILE.read_text())
        if session.get("pid") == pid:
            gateway, since = session.get("gateway"), session.get("since")
    except (OSError, ValueError):
        pass
    return Status(pid, _iface_ipv4(INTERFACE), gateway, since)


# --- gateway login ---------------------------------------------------------


@dataclass
class Prompt:
    label: str
    context: list[str]
    kind: str  # "challenge", "password", "username", or "other"


Ask = Callable[[Prompt], Awaitable[str | None]]


@dataclass
class Login:
    cookie: str
    connect_url: str
    fingerprint: str
    resolve: str


def _prompt_kind(label: str) -> str:
    low = label.lower()
    if "challenge" in low or "passcode" in low:
        return "challenge"
    if "password" in low:
        return "password"
    if "username" in low:
        return "username"
    return "other"


async def authenticate(
    username: str,
    password: str,
    gateway: str,
    duo_answer: str,
    ask: Ask,
    on_waiting: Callable[[str], None],
) -> Login:
    """Log in with openconnect --authenticate (no root needed) and return the cookie.

    openconnect writes progress and prompts to stderr and reads answers from stdin.
    Prompts have no trailing newline, so a partial line followed by silence is a prompt.
    """
    if shutil.which(OPENCONNECT) is None:
        raise VpnError("openconnect is not installed (omarchy pkg add openconnect)")
    cmd = [
        OPENCONNECT,
        "--protocol=gp",
        "--usergroup=gateway",
        "--authenticate",
        f"--user={username}",
        "--passwd-on-stdin",
        gateway,
    ]
    log(f"$ {shlex.join(cmd)}")
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    assert proc.stdin and proc.stdout and proc.stderr

    async def send(answer: str) -> None:
        proc.stdin.write(answer.encode() + b"\n")
        await proc.stdin.drain()

    stdout_task = asyncio.create_task(proc.stdout.read())
    try:
        await send(password)
        on_waiting("Signing in…")
        context: list[str] = []
        challenges = 0
        buf = ""
        while True:
            try:
                chunk = await asyncio.wait_for(proc.stderr.read(4096), PROMPT_QUIET)
            except TimeoutError:
                label = buf.strip()
                if not label:
                    continue
                buf = ""
                log(label)
                kind = _prompt_kind(label)
                if kind == "challenge":
                    challenges += 1
                    if challenges == 1 and duo_answer:
                        shown = duo_answer if duo_answer in ("push", "phone") else "passcode"
                        log(f"(answering Duo challenge with {shown})")
                        await send(duo_answer)
                        on_waiting(
                            "Approve the Duo push on your phone…"
                            if duo_answer == "push"
                            else "Answer the Duo phone call…"
                            if duo_answer == "phone"
                            else "Checking Duo passcode…"
                        )
                        context = []
                        continue
                elif kind == "username":
                    await send(username)
                    continue
                elif kind == "password":
                    # --passwd-on-stdin covers the first one; another means it was rejected.
                    raise VpnError(_last_error(context) or "Login failed: wrong username or password")
                answer = await ask(Prompt(label, context[-8:], kind))
                if answer is None:
                    raise VpnError("Login cancelled")
                await send(answer)
                on_waiting("Signing in…")
                context = []
                continue
            if not chunk:
                break
            buf += chunk.decode(errors="replace")
            *lines, buf = buf.split("\n")
            for line in lines:
                line = line.rstrip("\r")
                if line.strip():
                    log(line)
                    context.append(line)
        if buf.strip():
            log(buf.strip())
            context.append(buf.strip())
        rc = await proc.wait()
        out = (await stdout_task).decode(errors="replace")
    except BaseException:
        stdout_task.cancel()
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        raise

    if rc != 0:
        raise VpnError(_last_error(context) or f"openconnect exited with status {rc}")
    return _parse_login(out)


def _last_error(lines: list[str]) -> str | None:
    for line in reversed(lines):
        if re.search(r"fail|error|denied|invalid|incorrect|timed out|rejected", line, re.I):
            return line.strip()
    return None


def _parse_login(out: str) -> Login:
    values: dict[str, str] = {}
    for line in out.splitlines():
        key, sep, rest = line.partition("=")
        if sep and key.isupper():
            parts = shlex.split(rest)
            values[key] = parts[0] if parts else ""
    if not values.get("COOKIE"):
        raise VpnError("openconnect did not return a session cookie")
    url = values.get("CONNECT_URL") or values.get("HOST", "")
    return Login(values["COOKIE"], url, values.get("FINGERPRINT", ""), values.get("RESOLVE", ""))


# --- tunnel ------------------------------------------------------------------

# How to become root: sudo where a terminal can take the password (the TUI),
# pkexec and the shell's polkit dialog where none can (the bar widget).
SUDO = ("sudo", "-n")
PKEXEC = ("pkexec",)


def _as_root(elevate: tuple[str, ...], *cmd: str) -> list[str]:
    if elevate == PKEXEC:
        # pkexec wants an absolute program path.
        cmd = (shutil.which(cmd[0]) or cmd[0], *cmd[1:])
    return [*elevate, *cmd]


def _elevation_error(elevate: tuple[str, ...], rc: int) -> VpnError | None:
    # pkexec: 126 = dialog dismissed, 127 = not authorized.
    if elevate == PKEXEC and rc in (126, 127):
        return VpnError("Authorization cancelled")
    return None


async def sudo_cached() -> bool:
    proc = await asyncio.create_subprocess_exec(
        "sudo", "-n", "true",
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    return await proc.wait() == 0


def tunnel_command(login: Login, user: str, elevate: tuple[str, ...] = SUDO) -> list[str]:
    cmd = _as_root(
        elevate,
        # Own session, so the daemon outlives sudo's pty and never sees our SIGHUP.
        "setsid", "-w",
        shutil.which(OPENCONNECT) or OPENCONNECT,
        "--protocol=gp",
        "--cookie-on-stdin",
        f"--interface={INTERFACE}",
        "--background",
        f"--pid-file={PID_FILE}",
        "--timestamp",
    )
    if login.fingerprint:
        cmd.append(f"--servercert={login.fingerprint}")
    if login.resolve:
        cmd.append(f"--resolve={login.resolve}")
    if HIP_WRAPPER.exists():
        cmd += [f"--csd-wrapper={HIP_WRAPPER}", f"--csd-user={user}"]
    cmd.append(login.connect_url)
    return cmd


async def start_tunnel(login: Login, gateway: str, elevate: tuple[str, ...] = SUDO) -> Status:
    """Hand the cookie to a root openconnect that daemonizes once the tunnel is up."""
    cmd = tunnel_command(login, os.environ.get("USER") or str(os.getuid()), elevate)
    log(f"$ {shlex.join(cmd)}")
    with LOG_FILE.open("ab") as logf:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdin=asyncio.subprocess.PIPE, stdout=logf, stderr=logf
        )
    assert proc.stdin
    proc.stdin.write(login.cookie.encode() + b"\n")
    await proc.stdin.drain()
    proc.stdin.close()
    try:
        rc = await asyncio.wait_for(proc.wait(), 90)
    except TimeoutError:
        proc.kill()
        raise VpnError("Timed out waiting for the tunnel to come up") from None
    if rc != 0:
        raise _elevation_error(elevate, rc) or VpnError(
            f"openconnect failed to bring up the tunnel (status {rc}); see log"
        )
    for _ in range(20):
        current = status()
        if current.connected:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            SESSION_FILE.write_text(
                json.dumps({"pid": current.pid, "gateway": gateway, "since": time.time()})
            )
            return status()
        await asyncio.sleep(0.25)
    raise VpnError("openconnect exited but no tunnel is running; see log")


async def stop_tunnel(elevate: tuple[str, ...] = SUDO) -> None:
    """SIGINT makes openconnect log the session off before exiting."""
    pid = _openconnect_pid()
    if pid is None:
        return
    log(f"Disconnecting (pid {pid})")
    proc = await asyncio.create_subprocess_exec(
        *_as_root(elevate, "kill", "-INT", str(pid)),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        raise _elevation_error(elevate, proc.returncode) or VpnError(
            err.decode().strip() or "Failed to signal openconnect"
        )
    for _ in range(40):
        if _openconnect_pid() is None:
            SESSION_FILE.unlink(missing_ok=True)
            log("Disconnected")
            return
        await asyncio.sleep(0.25)
    raise VpnError("openconnect is still running after SIGINT")


# --- keyring (libsecret via secret-tool) -----------------------------------


def keyring_available() -> bool:
    return shutil.which("secret-tool") is not None


async def _secret_tool(*args: str, stdin: str | None = None) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "secret-tool", *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate(stdin.encode() if stdin is not None else b"")
    return proc.returncode or 0, out.decode()


async def keyring_get(username: str) -> str | None:
    if not (keyring_available() and username):
        return None
    rc, out = await _secret_tool("lookup", "service", "vpngatech", "username", username)
    return out if rc == 0 and out else None


async def keyring_set(username: str, password: str) -> bool:
    if not keyring_available():
        return False
    rc, _ = await _secret_tool(
        "store", f"--label=vpngatech ({username})",
        "service", "vpngatech", "username", username,
        stdin=password,
    )
    return rc == 0


async def keyring_clear(username: str) -> None:
    if keyring_available() and username:
        await _secret_tool("clear", "service", "vpngatech", "username", username)
