"""Command line entry point; also the JSON protocol the bar widget speaks.

`up --json` reads the password (empty line = use the keyring) and then any
prompt answers from stdin, one line each, and writes one JSON event per line:

    {"event": "busy", "message": "..."}
    {"event": "prompt", "label": "...", "context": [...], "kind": "challenge"}
    {"event": "connected", "status": {...}}
    {"event": "error", "message": "..."}

Closing stdin or sending SIGTERM cancels the login.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import subprocess
import sys
import time

from vpngatech import vpn


def fmt_uptime(since: float | None) -> str:
    if since is None:
        return ""
    secs = int(time.time() - since)
    return f"{secs // 3600}:{secs // 60 % 60:02d}:{secs % 60:02d}"


def gateway_label(host: str | None) -> str:
    if not host:
        return "unknown gateway"
    name = vpn.GATEWAYS.get(host)
    return f"{name} ({host})" if name else host


def status_json(state: vpn.Status, has_password: bool | None = None) -> dict:
    config = vpn.load_config()
    data = {
        "connected": state.connected,
        "pid": state.pid,
        "address": state.address,
        "gateway": state.gateway,
        "since": state.since,
        "config": {"username": config.username, "gateway": config.gateway, "duo": config.duo},
        "gateways": [{"host": h, "name": n} for h, n in vpn.GATEWAYS.items()],
        "duoMethods": [{"value": k, "label": v} for k, v in vpn.DUO_METHODS.items()],
        "log": str(vpn.LOG_FILE),
    }
    if has_password is not None:
        data["hasPassword"] = has_password
    return data


def emit(event: str, **fields) -> None:
    print(json.dumps({"event": event, **fields}), flush=True)


def cmd_status(args: argparse.Namespace) -> int:
    state = vpn.status()
    if args.json:
        has_password = None
        if args.keyring:
            username = vpn.load_config().username
            has_password = asyncio.run(vpn.keyring_get(username)) is not None
        print(json.dumps(status_json(state, has_password)))
        return 0
    if not state.connected:
        print("disconnected")
        return 1
    print(
        f"connected  {gateway_label(state.gateway)}  {state.address or '-'}"
        f"  up {fmt_uptime(state.since) or '?'}  pid {state.pid}"
    )
    return 0


def cmd_down(args: argparse.Namespace) -> int:
    if not vpn.status().connected:
        if args.json:
            emit("disconnected")
        else:
            print("not connected")
        return 0
    if args.json:
        elevate = vpn.PKEXEC
    else:
        if subprocess.run(["sudo", "-v"]).returncode != 0:
            return 1
        elevate = vpn.SUDO
    vpn.prepare_log()
    try:
        asyncio.run(vpn.stop_tunnel(elevate))
    except vpn.VpnError as exc:
        if args.json:
            emit("error", message=str(exc))
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        emit("disconnected")
    else:
        print("disconnected")
    return 0


async def _up(args: argparse.Namespace) -> int:
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    assert task
    loop.add_signal_handler(signal.SIGTERM, task.cancel)
    # An asyncio pipe rather than a reader thread, so cancelling never waits on stdin.
    stdin = asyncio.StreamReader()
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(stdin), sys.stdin)

    async def _readline() -> str | None:
        line = await stdin.readline()
        return line.decode().rstrip("\n") if line else None

    config = vpn.load_config()
    config.username = args.username or config.username
    config.gateway = args.gateway or config.gateway
    if args.duo in vpn.DUO_METHODS:
        config.duo = args.duo
    duo_answer = args.passcode if config.duo == "passcode" else config.duo
    if not config.username:
        emit("error", message="Enter your GT username")
        return 1
    if config.duo == "passcode" and not duo_answer:
        emit("error", message="Enter a Duo passcode")
        return 1
    if vpn.status().connected:
        emit("connected", status=status_json(vpn.status()))
        return 0

    password = await _readline()
    typed = bool(password)
    if not password:
        password = await vpn.keyring_get(config.username)
    if not password:
        emit("error", message="Enter your password")
        return 1
    vpn.save_config(config)

    async def ask(prompt: vpn.Prompt) -> str | None:
        emit("prompt", label=prompt.label, context=prompt.context, kind=prompt.kind)
        return await _readline()

    def busy(message: str) -> None:
        emit("busy", message=message)

    try:
        vpn.log(f"Connecting to {config.gateway} as {config.username} (bar)")
        login = await vpn.authenticate(
            config.username, password, config.gateway, duo_answer, ask, busy
        )
        vpn.log("Login accepted; starting tunnel")
        if typed and args.remember:
            await vpn.keyring_set(config.username, password)
        elif args.forget:
            await vpn.keyring_clear(config.username)
        busy("Waiting for authorization…")
        state = await vpn.start_tunnel(login, config.gateway, vpn.PKEXEC)
    except vpn.VpnError as exc:
        vpn.log(f"Error: {exc}")
        emit("error", message=str(exc))
        return 1
    except asyncio.CancelledError:
        vpn.log("Login cancelled")
        emit("error", message="Login cancelled")
        return 1
    emit("connected", status=status_json(state))
    return 0


def cmd_up(args: argparse.Namespace) -> int:
    vpn.prepare_log()
    return asyncio.run(_up(args))


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="vpngatech", description="Georgia Tech GlobalProtect VPN (vpn.gatech.edu)"
    )
    sub = parser.add_subparsers(dest="command")
    p = sub.add_parser("status", help="print connection status (exit 1 when disconnected)")
    p.add_argument("--json", action="store_true", help="machine-readable status")
    p.add_argument("--keyring", action="store_true", help="with --json, report a saved password")
    p = sub.add_parser("down", help="disconnect the VPN")
    p.add_argument("--json", action="store_true", help="JSON events; authorize with pkexec")
    p = sub.add_parser("up", help="connect without the TUI (JSON protocol, for the bar)")
    p.add_argument("--json", action="store_true", required=True)
    p.add_argument("--username")
    p.add_argument("--gateway")
    p.add_argument("--duo", choices=list(vpn.DUO_METHODS))
    p.add_argument("--passcode", default="")
    p.add_argument("--remember", action="store_true", help="save a typed password to the keyring")
    p.add_argument("--forget", action="store_true", help="remove the saved password")
    args = parser.parse_args()

    match args.command:
        case "status":
            sys.exit(cmd_status(args))
        case "down":
            sys.exit(cmd_down(args))
        case "up":
            sys.exit(cmd_up(args))
    from vpngatech.app import VpnApp

    VpnApp().run()
