import QtQuick
import QtQuick.Controls
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

// Georgia Tech GlobalProtect VPN in the bar. All VPN work happens in the
// vpngatech CLI (~/Work/vpngatech): `status --json` for polling, `up --json`
// for the login conversation (JSON events out, password and prompt answers
// in), and `down --json`. Root comes from pkexec, i.e. the shell's polkit
// dialog.
Panel {
  id: root
  moduleName: "st87.vpngatech"
  ipcTarget: "st87.vpngatech"
  manageIpc: false

  readonly property string cli: Quickshell.env("HOME") + "/.local/bin/vpngatech"
  readonly property string glyph: String.fromCodePoint(0xF0582)

  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color urgent: bar ? bar.urgent : Color.urgent
  readonly property color dim: Qt.darker(foreground, 1.55)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

  // Last `status --json`.
  property var status: ({})
  readonly property bool connected: status.connected === true
  readonly property var gateways: status.gateways || []
  readonly property var duoMethods: status.duoMethods || []

  // "idle" | "busy" | "prompt" — the login/logout conversation.
  property string phase: "idle"
  property string message: ""
  property string error: ""
  property var prompt: null
  readonly property bool busy: phase !== "idle"

  // Form state, seeded from the CLI's saved config on first status.
  property bool formSeeded: false
  property string username: ""
  property string gateway: ""
  property string duo: "push"
  property bool remember: true
  property bool hasPassword: false

  property real now: Date.now() / 1000

  readonly property color barIconColor: error !== "" && !connected && !busy
    ? urgent : (connected ? barForeground : Qt.darker(barForeground, 1.55))

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  function gatewayName(host) {
    for (var i = 0; i < gateways.length; i++)
      if (gateways[i].host === host) return gateways[i].name + " · " + host
    return host || "unknown gateway"
  }

  function uptime(since) {
    if (!since) return ""
    var secs = Math.max(0, Math.floor(now - since))
    var h = Math.floor(secs / 3600), m = Math.floor(secs / 60) % 60, s = secs % 60
    return h + ":" + (m < 10 ? "0" : "") + m + ":" + (s < 10 ? "0" : "") + s
  }

  function heroMeta() {
    if (busy) return phase === "prompt" ? "Needs your answer" : "Working"
    if (connected) return [status.address || "no address yet", uptime(status.since)].filter(Boolean).join(" · ")
    return "Disconnected"
  }

  function tooltip() {
    if (busy) return "Georgia Tech VPN: " + (message || "working…")
    if (connected) return "Georgia Tech VPN: connected to " + gatewayName(status.gateway)
    return "Georgia Tech VPN: disconnected"
  }

  function refresh(withKeyring) {
    if (statusProc.running) return
    statusProc.command = withKeyring ? [cli, "status", "--json", "--keyring"] : [cli, "status", "--json"]
    statusProc.running = true
  }

  function applyStatus(data) {
    status = data
    if (data.hasPassword !== undefined) hasPassword = data.hasPassword
    if (!formSeeded && data.config) {
      username = data.config.username || ""
      gateway = data.config.gateway || ""
      duo = data.config.duo || "push"
      formSeeded = true
    }
  }

  function notify(body) {
    if (!opened) Quickshell.execDetached(["notify-send", "-a", "vpngatech", "Georgia Tech VPN", body])
  }

  function connect() {
    if (busy || connected) return
    error = ""
    username = usernameField.text.trim()
    if (username === "") { error = "Enter your GT username"; usernameField.forceActiveFocus(); return }
    if (passwordField.text === "" && !hasPassword) { error = "Enter your password"; passwordField.forceActiveFocus(); return }
    if (duo === "passcode" && passcodeField.text.trim() === "") { error = "Enter a Duo passcode"; passcodeField.forceActiveFocus(); return }

    var cmd = [cli, "up", "--json", "--username", username, "--gateway", gateway, "--duo", duo]
    if (duo === "passcode") cmd.push("--passcode", passcodeField.text.trim())
    cmd.push(remember ? "--remember" : "--forget")
    upProc.secret = passwordField.text
    upProc.command = cmd
    phase = "busy"
    message = "Signing in…"
    upProc.running = true
  }

  function disconnect() {
    if (busy || !connected) return
    error = ""
    phase = "busy"
    message = "Disconnecting…"
    downProc.running = true
  }

  function toggleVpn() { connected ? disconnect() : connect() }

  function cancel() {
    if (upProc.running) upProc.running = false
  }

  function answer(text) {
    if (phase !== "prompt") return
    upProc.write(text + "\n")
    prompt = null
    phase = "busy"
    message = "Signing in…"
  }

  function handleEvent(line) {
    var ev
    try { ev = JSON.parse(line) } catch (e) { return }
    if (ev.event === "busy") {
      phase = "busy"
      message = ev.message
    } else if (ev.event === "prompt") {
      prompt = ev
      phase = "prompt"
      Qt.callLater(function() { promptField.text = ""; promptField.forceActiveFocus() })
    } else if (ev.event === "connected") {
      applyStatus(ev.status)
      phase = "idle"
      passwordField.text = ""
      passcodeField.text = ""
      notify("Connected to " + gatewayName(ev.status.gateway))
    } else if (ev.event === "error") {
      error = ev.message
      phase = "idle"
      notify(ev.message)
    } else if (ev.event === "disconnected") {
      phase = "idle"
    }
  }

  function focusForm() {
    if (connected || busy) return
    if (usernameField.text === "") usernameField.forceActiveFocus()
    else if (!hasPassword) passwordField.forceActiveFocus()
  }

  onOpenedChanged: if (opened) {
    error = connected ? "" : error
    refresh(true)
    Qt.callLater(focusForm)
  }

  Component.onCompleted: refresh(true)

  IpcHandler {
    target: root.ipcTarget
    function open(): void { root.open() }
    function close(): void { root.close() }
    function show(): void { root.open() }
    function hide(): void { root.close() }
    function toggle(): void { root.toggle() }
    function up(): string { root.connect(); return root.error || "ok" }
    function down(): string { root.disconnect(); return "ok" }
    function status(): string { return JSON.stringify(root.status) }
  }

  Process {
    id: statusProc
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        try { root.applyStatus(JSON.parse(text)) } catch (e) {}
      }
    }
  }

  // The password goes over stdin, never argv; an empty line means "use the keyring".
  Process {
    id: upProc
    property string secret: ""
    stdinEnabled: true
    stdout: SplitParser { onRead: function(line) { root.handleEvent(line) } }
    onStarted: {
      write(secret + "\n")
      secret = ""
    }
    onExited: function(exitCode) {
      if (root.phase !== "idle") {
        root.phase = "idle"
        if (root.error === "" && !root.connected) root.error = exitCode === 0 ? "" : "Login cancelled"
      }
      root.prompt = null
      root.refresh(true)
    }
  }

  Process {
    id: downProc
    command: [root.cli, "down", "--json"]
    stdout: SplitParser { onRead: function(line) { root.handleEvent(line) } }
    onExited: {
      root.phase = "idle"
      root.refresh(false)
    }
  }

  Timer {
    interval: Math.max(2, Number(root.setting("refreshIntervalSec", 5))) * 1000
    running: true
    repeat: true
    onTriggered: if (!root.busy) root.refresh(false)
  }

  Timer {
    interval: 1000
    running: root.opened && root.connected
    repeat: true
    onTriggered: root.now = Date.now() / 1000
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.glyph
    foreground: root.barIconColor
    tooltipText: root.tooltip()
    opacity: root.busy ? 0.45 + 0.55 * pulse.value : 1.0
    onPressed: function(buttonCode) {
      if (buttonCode === Qt.RightButton) root.toggleVpn()
      else if (buttonCode === Qt.MiddleButton) Quickshell.execDetached(["omarchy-launch-or-focus-tui", root.cli])
      else root.toggle()
    }
  }

  // Breathing bar icon while a login or logout is in flight.
  QtObject {
    id: pulse
    property real value: 1.0
  }
  SequentialAnimation {
    running: root.busy
    loops: Animation.Infinite
    onStopped: pulse.value = 1.0
    NumberAnimation { target: pulse; property: "value"; from: 1.0; to: 0.0; duration: 700; easing.type: Easing.InOutSine }
    NumberAnimation { target: pulse; property: "value"; from: 0.0; to: 1.0; duration: 700; easing.type: Easing.InOutSine }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(340))
    contentHeight: panel.fittedContentHeight(column.implicitHeight, Style.space(600))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      // Text fields and open dropdowns own their keys.
      blocked: usernameField.activeFocus || passwordField.activeFocus || passcodeField.activeFocus
        || promptField.activeFocus || gatewayDropdown.popupOpen || duoDropdown.popupOpen
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onActivateRequested: root.toggleVpn()

      Flickable {
        id: flick
        anchors.fill: parent
        contentWidth: width
        contentHeight: column.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        interactive: contentHeight > height
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

        Column {
          id: column
          width: flick.width
          spacing: Style.space(12)

          PanelHero {
            id: hero
            width: parent.width
            title: "Georgia Tech VPN"
            meta: root.heroMeta()
            foreground: root.foreground
            fontFamily: root.fontFamily
            iconOpacity: root.connected ? 1.0 : 0.5
            iconComponent: Component {
              Text {
                text: root.glyph
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.display
              }
            }
            trailingControl: Component {
              ToggleSwitch {
                id: powerSwitch
                checked: root.connected
                busy: root.busy
                foreground: root.foreground
                onToggled: root.toggleVpn()

                PanelToolTip {
                  visible: powerSwitch.containsMouse
                  text: root.connected ? "Disconnect" : "Connect"
                  fontFamily: root.fontFamily
                }
              }
            }
          }

          Text {
            visible: (root.error !== "" && !root.busy) || (root.phase === "busy" && root.message !== "")
            width: parent.width
            textFormat: Text.PlainText
            text: root.busy ? root.message : root.error
            color: root.busy ? root.foreground : root.urgent
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
            wrapMode: Text.WordWrap
          }

          // --- connected ----------------------------------------------------

          Column {
            visible: root.connected && !root.busy
            width: parent.width
            spacing: Style.space(6)

            PanelSeparator { foreground: root.foreground }

            Repeater {
              model: [
                ["Gateway", root.gatewayName(root.status.gateway)],
                ["Address", root.status.address || "—"],
                ["Uptime", root.uptime(root.status.since) || "—"],
                ["Interface", "gatech0 · pid " + (root.status.pid || "?")]
              ]
              Item {
                required property var modelData
                width: column.width
                implicitHeight: valueText.implicitHeight

                Text {
                  text: modelData[0]
                  color: root.dim
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.body
                }
                Text {
                  id: valueText
                  anchors.right: parent.right
                  width: parent.width * 0.72
                  horizontalAlignment: Text.AlignRight
                  elide: Text.ElideMiddle
                  textFormat: Text.PlainText
                  text: modelData[1]
                  color: root.foreground
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.body
                }
              }
            }

            Item { width: 1; height: Style.space(4) }

            Button {
              width: parent.width
              text: "Disconnect"
              bordered: true
              foreground: root.foreground
              fontFamily: root.fontFamily
              onClicked: root.disconnect()
            }
          }

          // --- openconnect asks something we can't answer ourselves ---------

          Column {
            visible: root.phase === "prompt" && root.prompt !== null
            width: parent.width
            spacing: Style.space(8)

            PanelSeparator { foreground: root.foreground }

            PanelSectionHeader {
              text: "OPENCONNECT IS ASKING"
              foreground: root.foreground
              fontFamily: root.fontFamily
            }

            Text {
              visible: text !== ""
              width: parent.width
              textFormat: Text.PlainText
              text: root.prompt && root.prompt.context ? root.prompt.context.join("\n") : ""
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
              wrapMode: Text.WrapAnywhere
            }

            Text {
              width: parent.width
              textFormat: Text.PlainText
              text: root.prompt ? root.prompt.label : ""
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.body
              wrapMode: Text.WordWrap
            }

            TextField {
              id: promptField
              width: parent.width
              password: root.prompt !== null && root.prompt.kind === "password"
              foreground: root.foreground
              onAccepted: root.answer(text)
              Keys.onEscapePressed: root.cancel()
            }

            Row {
              spacing: Style.space(6)

              Button {
                visible: root.prompt !== null && root.prompt.kind === "challenge"
                text: "Duo Push"
                bordered: true
                foreground: root.foreground
                fontFamily: root.fontFamily
                onClicked: root.answer("push")
              }
              Button {
                visible: root.prompt !== null && root.prompt.kind === "challenge"
                text: "Call"
                bordered: true
                foreground: root.foreground
                fontFamily: root.fontFamily
                onClicked: root.answer("phone")
              }
              Button {
                text: "Submit"
                bordered: true
                foreground: root.foreground
                fontFamily: root.fontFamily
                onClicked: root.answer(promptField.text)
              }
              Button {
                text: "Cancel"
                foreground: root.foreground
                fontFamily: root.fontFamily
                onClicked: root.cancel()
              }
            }
          }

          // --- waiting (Duo push, polkit, tunnel) ---------------------------

          Button {
            visible: root.phase === "busy" && upProc.running
            width: parent.width
            text: "Cancel"
            bordered: true
            foreground: root.foreground
            fontFamily: root.fontFamily
            onClicked: root.cancel()
          }

          // --- login form ---------------------------------------------------

          Column {
            visible: !root.connected && root.phase === "idle"
            width: parent.width
            spacing: Style.space(8)

            PanelSeparator { foreground: root.foreground }

            TextField {
              id: usernameField
              width: parent.width
              placeholderText: "GT username (gburdell3)"
              text: root.username
              foreground: root.foreground
              onAccepted: passwordField.forceActiveFocus()
              Keys.onEscapePressed: root.close()
            }

            TextField {
              id: passwordField
              width: parent.width
              password: true
              placeholderText: root.hasPassword ? "Password (saved in keyring)" : "Password"
              foreground: root.foreground
              onAccepted: root.connect()
              Keys.onEscapePressed: root.close()
            }

            Dropdown {
              id: gatewayDropdown
              width: parent.width
              label: "Gateway"
              fontFamily: root.fontFamily
              options: root.gateways.map(function(g) { return { value: g.host, label: g.name + " · " + g.host } })
              value: root.gateway
              onChanged: function(v) { root.gateway = v }
            }

            Dropdown {
              id: duoDropdown
              width: parent.width
              label: "Duo"
              fontFamily: root.fontFamily
              options: root.duoMethods
              value: root.duo
              onChanged: function(v) {
                root.duo = v
                if (v === "passcode") Qt.callLater(passcodeField.forceActiveFocus)
              }
            }

            TextField {
              id: passcodeField
              visible: root.duo === "passcode"
              width: parent.width
              placeholderText: "Duo passcode"
              validator: RegularExpressionValidator { regularExpression: /[0-9]{0,10}/ }
              foreground: root.foreground
              onAccepted: root.connect()
              Keys.onEscapePressed: root.close()
            }

            Toggle {
              width: parent.width
              label: "Remember password"
              description: "Store it in the GNOME keyring"
              checked: root.remember
              foreground: root.foreground
              fontFamily: root.fontFamily
              onClicked: root.remember = !root.remember
            }

            Button {
              width: parent.width
              text: "Connect"
              bordered: true
              foreground: root.foreground
              fontFamily: root.fontFamily
              onClicked: root.connect()
            }
          }

          Text {
            width: parent.width
            textFormat: Text.PlainText
            text: "Right-click icon: connect / disconnect · Middle-click: full TUI"
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            horizontalAlignment: Text.AlignHCenter
            wrapMode: Text.WordWrap
          }
        }
      }
    }
  }
}
