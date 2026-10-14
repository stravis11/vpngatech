"""Georgia Tech GlobalProtect VPN login TUI."""

from __future__ import annotations

import asyncio
import subprocess
import time

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Footer, Input, Label, RichLog, Select, Static

from vpngatech import vpn
from vpngatech.cli import fmt_uptime, gateway_label


class PromptScreen(ModalScreen[str | None]):
    """An openconnect prompt we can't answer on our own."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, prompt: vpn.Prompt) -> None:
        super().__init__()
        self.prompt = prompt

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("openconnect is asking", id="dialog-title")
            if self.prompt.context:
                yield Static("\n".join(self.prompt.context), id="dialog-context", markup=False)
            yield Label(self.prompt.label, markup=False)
            yield Input(id="answer", password=self.prompt.kind == "password")
            with Horizontal(id="dialog-buttons"):
                if self.prompt.kind == "challenge":
                    yield Button("Duo Push", id="push")
                    yield Button("Call", id="phone")
                yield Button("Submit", id="submit", variant="primary")
                yield Button("Cancel", id="cancel")

    @on(Input.Submitted)
    def submit_input(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        match event.button.id:
            case "submit":
                self.dismiss(self.query_one("#answer", Input).value)
            case "cancel":
                self.dismiss(None)
            case answer:
                self.dismiss(answer)

    def action_cancel(self) -> None:
        self.dismiss(None)


class VpnApp(App):
    TITLE = "vpngatech"

    CSS = """
    Screen {
        layout: vertical;
    }
    #status {
        height: 3;
        padding: 0 2;
        content-align: left middle;
        border: round $foreground 30%;
        border-title-color: $foreground;
        color: $text-muted;
    }
    #status.connected { color: $success; border: round $success; }
    #status.busy { color: $warning; border: round $warning; }
    #status.error { color: $error; border: round $error; }
    #main {
        height: 1fr;
    }
    #form {
        width: 46;
        padding: 0 1;
        border: round $foreground 30%;
        border-title-color: $foreground;
        scrollbar-size-vertical: 1;
    }
    #form Label {
        margin-top: 0;
        color: $text-muted;
    }
    #form Input, #form Select {
        width: 100%;
    }
    #passcode-row {
        height: auto;
    }
    #passcode-row.hidden {
        display: none;
    }
    #remember {
        margin-top: 1;
        border: none;
        background: transparent;
    }
    #buttons {
        height: auto;
        margin-top: 1;
    }
    #buttons Button {
        width: 1fr;
        margin-right: 1;
    }
    #log {
        width: 1fr;
        border: round $foreground 30%;
        border-title-color: $foreground;
        padding: 0 1;
        scrollbar-size-vertical: 1;
        overflow-x: hidden;
    }
    PromptScreen {
        align: center middle;
    }
    #dialog {
        width: 72;
        height: auto;
        max-height: 90%;
        padding: 1 2;
        border: round $warning;
        background: $surface;
    }
    #dialog-title {
        text-style: bold;
        color: $warning;
    }
    #dialog-context {
        margin: 1 0;
        color: $text-muted;
    }
    #dialog-buttons {
        height: auto;
        margin-top: 1;
    }
    #dialog-buttons Button {
        margin-right: 1;
    }
    """

    BINDINGS = [
        Binding("ctrl+o", "connect", "Connect", priority=True),
        Binding("ctrl+d", "disconnect", "Disconnect", priority=True),
        Binding("escape", "cancel", "Cancel login"),
        Binding("ctrl+l", "clear_log", "Clear log"),
        Binding("ctrl+q", "quit", "Quit", priority=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.config = vpn.load_config()
        self.busy: str | None = None
        self.error: str | None = None
        self.state = vpn.status()
        self.log_offset = 0

    def compose(self) -> ComposeResult:
        yield Static(id="status")
        with Horizontal(id="main"):
            with VerticalScroll(id="form"):
                yield Label("GT username")
                yield Input(self.config.username, placeholder="gburdell3", id="username")
                yield Label("Password")
                yield Input(password=True, id="password")
                yield Label("Gateway")
                gateways = dict(vpn.GATEWAYS)
                gateways.setdefault(self.config.gateway, self.config.gateway)
                yield Select(
                    [(f"{name} · {host}" if name != host else host, host)
                     for host, name in gateways.items()],
                    value=self.config.gateway,
                    allow_blank=False,
                    id="gateway",
                )
                yield Label("Duo")
                yield Select(
                    [(label, key) for key, label in vpn.DUO_METHODS.items()],
                    value=self.config.duo if self.config.duo in vpn.DUO_METHODS else "push",
                    allow_blank=False,
                    id="duo",
                )
                with Vertical(id="passcode-row"):
                    yield Label("Duo passcode")
                    yield Input(placeholder="123456", id="passcode", restrict=r"[0-9]*")
                yield Checkbox(
                    "Remember password in keyring",
                    id="remember",
                    disabled=not vpn.keyring_available(),
                )
                with Horizontal(id="buttons"):
                    yield Button("Connect", id="connect", variant="success")
                    yield Button("Disconnect", id="disconnect", variant="error")
            yield RichLog(id="log", wrap=True, markup=False, highlight=False)
        yield Footer()

    def on_mount(self) -> None:
        self.theme = "ansi-dark"
        self.query_one("#status").border_title = "Georgia Tech VPN"
        self.query_one("#form").border_title = "Login"
        self.query_one("#log").border_title = "Log"
        vpn.prepare_log()
        self.show_log_tail()
        self.set_interval(0.25, self.tail_log)
        self.set_interval(1, self.refresh_status)
        self.sync_duo()
        self.refresh_status()
        focus = "#password" if self.config.username else "#username"
        self.query_one(focus).focus()
        self.load_saved_password()

    # --- status / log ---------------------------------------------------------

    def refresh_status(self) -> None:
        if not self.busy:
            self.state = vpn.status()
        widget = self.query_one("#status", Static)
        widget.set_class(bool(self.busy), "busy")
        widget.set_class(self.state.connected and not self.busy, "connected")
        widget.set_class(bool(self.error) and not self.busy and not self.state.connected, "error")
        if self.busy:
            spinner = "◐◓◑◒"[int(time.time() * 4) % 4]
            widget.update(f"{spinner}  {self.busy}   (esc to cancel)")
        elif self.state.connected:
            parts = [
                "●  Connected",
                gateway_label(self.state.gateway),
                self.state.address or "no address yet",
                fmt_uptime(self.state.since),
                f"pid {self.state.pid}",
            ]
            widget.update("   ".join(p for p in parts if p))
        elif self.error:
            widget.update(f"✗  {self.error}")
        else:
            widget.update("○  Disconnected")
        self.query_one("#connect", Button).disabled = bool(self.busy) or self.state.connected
        self.query_one("#disconnect", Button).disabled = bool(self.busy) or not self.state.connected

    def show_log_tail(self) -> None:
        data = vpn.LOG_FILE.read_bytes()
        self.log_offset = len(data)
        rich_log = self.query_one("#log", RichLog)
        for line in data.decode(errors="replace").splitlines()[-60:]:
            rich_log.write(line)

    def tail_log(self) -> None:
        try:
            with vpn.LOG_FILE.open("rb") as f:
                size = f.seek(0, 2)
                if size < self.log_offset:
                    self.log_offset = 0
                if size == self.log_offset:
                    return
                f.seek(self.log_offset)
                chunk = f.read()
        except FileNotFoundError:
            return
        # Only consume whole lines; the rest arrives next tick.
        end = chunk.rfind(b"\n") + 1
        self.log_offset += end
        rich_log = self.query_one("#log", RichLog)
        for line in chunk[:end].decode(errors="replace").splitlines():
            rich_log.write(line)

    def set_busy(self, message: str | None) -> None:
        self.busy = message
        self.refresh_status()

    # --- form -----------------------------------------------------------------

    @work(exclusive=True, group="keyring")
    async def load_saved_password(self) -> None:
        password = await vpn.keyring_get(self.config.username)
        if password and not self.query_one("#password", Input).value:
            self.query_one("#password", Input).value = password
            self.query_one("#remember", Checkbox).value = True

    @on(Select.Changed, "#duo")
    def sync_duo(self) -> None:
        passcode = self.query_one("#duo", Select).value == "passcode"
        self.query_one("#passcode-row").set_class(not passcode, "hidden")

    @on(Input.Submitted)
    def submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "username" and not self.query_one("#password", Input).value:
            self.query_one("#password").focus()
        else:
            self.action_connect()

    @on(Button.Pressed, "#connect")
    def pressed_connect(self) -> None:
        self.action_connect()

    @on(Button.Pressed, "#disconnect")
    def pressed_disconnect(self) -> None:
        self.action_disconnect()

    # --- actions --------------------------------------------------------------

    def action_connect(self) -> None:
        if self.busy or vpn.status().connected:
            return
        username = self.query_one("#username", Input).value.strip()
        password = self.query_one("#password", Input).value
        duo = str(self.query_one("#duo", Select).value)
        passcode = self.query_one("#passcode", Input).value.strip()
        if not username:
            self.notify("Enter your GT username", severity="warning")
            self.query_one("#username").focus()
            return
        if not password:
            self.notify("Enter your password", severity="warning")
            self.query_one("#password").focus()
            return
        if duo == "passcode" and not passcode:
            self.notify("Enter a Duo passcode", severity="warning")
            self.query_one("#passcode").focus()
            return
        self.config.username = username
        self.config.gateway = str(self.query_one("#gateway", Select).value)
        self.config.duo = duo
        vpn.save_config(self.config)
        self.connect_flow(username, password, passcode if duo == "passcode" else duo)

    def action_disconnect(self) -> None:
        if not self.busy and vpn.status().connected:
            self.disconnect_flow()

    def action_cancel(self) -> None:
        if self.busy:
            self.workers.cancel_group(self, "vpn")

    def action_clear_log(self) -> None:
        self.query_one("#log", RichLog).clear()

    # --- flows ----------------------------------------------------------------

    async def ensure_sudo(self) -> bool:
        """Get a sudo timestamp up front, so Duo doesn't time out behind a password prompt."""
        if await vpn.sudo_cached():
            return True
        with self.suspend():
            print("\nvpngatech needs root to create and remove the VPN tunnel interface.\n")
            ok = subprocess.run(["sudo", "-v"]).returncode == 0
        return ok

    @work(exclusive=True, group="vpn")
    async def connect_flow(self, username: str, password: str, duo_answer: str) -> None:
        self.error = None
        gateway = self.config.gateway
        try:
            if not await self.ensure_sudo():
                raise vpn.VpnError("sudo authentication failed")
            self.set_busy(f"Signing in to {gateway}…")
            vpn.log(f"Connecting to {gateway} as {username}")
            login = await vpn.authenticate(
                username, password, gateway, duo_answer, self.ask, self.set_busy
            )
            vpn.log("Login accepted; starting tunnel")
            await self.remember_password(username, password)
            self.query_one("#passcode", Input).value = ""
            self.set_busy("Bringing up the tunnel…")
            self.state = await vpn.start_tunnel(login, gateway)
            self.notify(f"Connected to {gateway_label(gateway)}", title="vpngatech")
        except vpn.VpnError as exc:
            self.error = str(exc)
            vpn.log(f"Error: {exc}")
            self.notify(str(exc), severity="error", title="vpngatech")
        except asyncio.CancelledError:
            self.error = "Login cancelled"
            vpn.log("Login cancelled")
            raise
        finally:
            self.set_busy(None)

    async def ask(self, prompt: vpn.Prompt) -> str | None:
        self.set_busy("Waiting for your answer…")
        return await self.push_screen_wait(PromptScreen(prompt))

    async def remember_password(self, username: str, password: str) -> None:
        if not vpn.keyring_available():
            return
        if self.query_one("#remember", Checkbox).value:
            if not await vpn.keyring_set(username, password):
                self.notify("Couldn't save the password to the keyring", severity="warning")
        else:
            await vpn.keyring_clear(username)

    @work(exclusive=True, group="vpn")
    async def disconnect_flow(self) -> None:
        self.error = None
        try:
            if not await self.ensure_sudo():
                raise vpn.VpnError("sudo authentication failed")
            self.set_busy("Disconnecting…")
            await vpn.stop_tunnel()
            self.notify("Disconnected", title="vpngatech")
        except vpn.VpnError as exc:
            self.error = str(exc)
            vpn.log(f"Error: {exc}")
            self.notify(str(exc), severity="error", title="vpngatech")
        finally:
            self.set_busy(None)
