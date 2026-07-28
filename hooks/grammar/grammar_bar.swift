import Cocoa
import Carbon.HIToolbox

let CORRECTION_FILE = "/tmp/grammar_last_correction.txt"
let MIN_BAR_H: CGFloat = 32
let MAX_BAR_H: CGFloat = 64

class ClickableWindow: NSWindow {
    var shownAt = Date.distantPast

    override var canBecomeKey: Bool { return true }
    override var canBecomeMain: Bool { return true }

    override func mouseDown(with event: NSEvent) {
        self.orderOut(nil)
    }

    // Hover-to-dismiss: the bar gets out of the way without eating a click.
    // Grace period so it isn't killed by the mouse already sitting at the
    // bottom edge when the bar appears.
    override func mouseEntered(with event: NSEvent) {
        if Date().timeIntervalSince(shownAt) > 1.0 {
            self.orderOut(nil)
        }
    }

    override func orderFrontRegardless() {
        shownAt = Date()
        super.orderFrontRegardless()
    }
}

class GrammarBarApp: NSObject, NSApplicationDelegate {
    var window: ClickableWindow!
    var textField: NSTextField!
    var hideTimer: Timer?
    var visTimer: Timer?          // runs only while bar is visible (screen-follow)
    var watcher: DispatchSourceFileSystemObject?
    var watcherFD: Int32 = -1
    var lastText = ""
    let autoHideSeconds: TimeInterval = 300  // 5 minutes

    func applicationDidFinishLaunching(_ notification: Notification) {
        let mouseLocation = NSEvent.mouseLocation
        let screen = NSScreen.screens.first(where: { NSMouseInRect(mouseLocation, $0.frame, false) }) ?? NSScreen.main!

        let screenFrame = screen.frame
        let barW = screenFrame.width * 0.95
        let barX = screenFrame.origin.x + (screenFrame.width - barW) / 2
        let barY = screenFrame.origin.y + 5

        window = ClickableWindow(
            contentRect: NSRect(x: barX, y: barY, width: barW, height: MIN_BAR_H),
            styleMask: [.borderless],
            backing: .buffered,
            defer: false
        )
        window.level = .floating
        window.isOpaque = false
        window.backgroundColor = NSColor(red: 1.0, green: 0.95, blue: 0.70, alpha: 0.95)
        window.hasShadow = true
        window.collectionBehavior = [.canJoinAllSpaces, .stationary, .fullScreenAuxiliary]
        window.acceptsMouseMovedEvents = true

        textField = NSTextField(frame: NSRect(x: 12, y: 0, width: barW - 24, height: MIN_BAR_H))
        textField.isEditable = false
        textField.isBordered = false
        textField.drawsBackground = false
        textField.textColor = NSColor(red: 0.20, green: 0.15, blue: 0.05, alpha: 1.0)
        textField.font = NSFont.monospacedSystemFont(ofSize: 13, weight: .regular)
        textField.stringValue = ""
        textField.lineBreakMode = .byWordWrapping
        textField.maximumNumberOfLines = 3
        textField.cell?.wraps = true
        textField.cell?.isScrollable = false

        window.contentView?.addSubview(textField)
        window.contentView?.addTrackingArea(NSTrackingArea(
            rect: .zero,
            options: [.mouseEnteredAndExited, .activeAlways, .inVisibleRect],
            owner: window, userInfo: nil
        ))
        window.orderFrontRegardless()

        startWatcher()
        registerHotKey()
        poll()
    }

    // ⌥⌘G — toggle the bar: recall the last correction (e.g. after an
    // accidental dismiss click), or hide it if currently shown.
    func registerHotKey() {
        var eventType = EventTypeSpec(
            eventClass: OSType(kEventClassKeyboard),
            eventKind: UInt32(kEventHotKeyPressed)
        )
        InstallEventHandler(GetEventDispatcherTarget(), { _, _, userData in
            let app = Unmanaged<GrammarBarApp>.fromOpaque(userData!).takeUnretainedValue()
            DispatchQueue.main.async { app.toggleBar() }
            return noErr
        }, 1, &eventType, Unmanaged.passUnretained(self).toOpaque(), nil)

        var hkRef: EventHotKeyRef?
        let hotKeyID = EventHotKeyID(signature: OSType(0x474D_4252), id: 1)  // 'GMBR'
        RegisterEventHotKey(UInt32(kVK_ANSI_G), UInt32(cmdKey | optionKey),
                            hotKeyID, GetEventDispatcherTarget(), 0, &hkRef)
    }

    func toggleBar() {
        if window.isVisible {
            hideTimer?.invalidate()
            window.orderOut(nil)
        } else if !lastText.isEmpty {
            textField.stringValue = lastText
            window.orderFrontRegardless()
            resetHideTimer()
            ensureVisTimer()
        }
    }

    // Event-driven: kqueue watch on the correction file — zero wakeups while idle.
    func startWatcher() {
        watcher?.cancel()
        watcher = nil
        if watcherFD >= 0 { close(watcherFD); watcherFD = -1 }

        watcherFD = open(CORRECTION_FILE, O_EVTONLY)
        guard watcherFD >= 0 else {
            // File not there yet — retry until the hook creates it.
            DispatchQueue.main.asyncAfter(deadline: .now() + 2) { [weak self] in
                self?.startWatcher()
            }
            return
        }
        let src = DispatchSource.makeFileSystemObjectSource(
            fileDescriptor: watcherFD,
            eventMask: [.write, .extend, .delete, .rename],
            queue: .main
        )
        src.setEventHandler { [weak self] in
            guard let self = self else { return }
            let ev = src.data
            self.poll()
            if ev.contains(.delete) || ev.contains(.rename) {
                self.startWatcher()  // inode replaced — re-arm on the new file
            }
        }
        src.resume()
        watcher = src
    }

    // While visible, follow the mouse across screens; stops itself once hidden.
    func ensureVisTimer() {
        guard visTimer == nil else { return }
        visTimer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in
            guard let self = self else { return }
            if !self.window.isVisible {
                self.visTimer?.invalidate()
                self.visTimer = nil
                return
            }
            self.poll()
        }
    }

    func resetHideTimer() {
        hideTimer?.invalidate()
        hideTimer = Timer.scheduledTimer(withTimeInterval: autoHideSeconds, repeats: false) { [weak self] _ in
            DispatchQueue.main.async {
                self?.window.orderOut(nil)
            }
        }
    }

    func poll() {
        guard let data = try? String(contentsOfFile: CORRECTION_FILE, encoding: .utf8) else { return }
        let text = data.trimmingCharacters(in: .whitespacesAndNewlines)

        DispatchQueue.main.async {
            // Always track the screen where the mouse is
            let mouseLocation = NSEvent.mouseLocation
            let screen = NSScreen.screens.first(where: { NSMouseInRect(mouseLocation, $0.frame, false) }) ?? NSScreen.main!
            let screenFrame = screen.frame
            let barW = screenFrame.width * 0.95
            let barX = screenFrame.origin.x + (screenFrame.width - barW) / 2
            let textWidth = barW - 24

            let textChanged = text != self.lastText
            let screenChanged = self.window.frame.width != barW || self.window.frame.origin.x != barX

            if textChanged {
                self.lastText = text
                self.textField.stringValue = text
                if !text.isEmpty {
                    self.window.orderFrontRegardless()
                    self.resetHideTimer()
                    self.ensureVisTimer()
                } else {
                    self.hideTimer?.invalidate()
                    self.window.orderOut(nil)
                }
            }

            if textChanged || screenChanged {
                let displayText = text.isEmpty ? self.lastText : text
                let attrStr = NSAttributedString(string: displayText, attributes: [
                    .font: NSFont.monospacedSystemFont(ofSize: 13, weight: .regular)
                ])
                let boundingRect = attrStr.boundingRect(
                    with: NSSize(width: textWidth, height: CGFloat.greatestFiniteMagnitude),
                    options: [.usesLineFragmentOrigin, .usesFontLeading]
                )
                let neededH = min(max(ceil(boundingRect.height) + 12, MIN_BAR_H), MAX_BAR_H)

                self.textField.frame = NSRect(x: 12, y: 0, width: textWidth, height: neededH)
                self.window.setFrame(
                    NSRect(x: barX, y: screenFrame.origin.y + 5, width: barW, height: neededH),
                    display: true
                )
            }
        }
    }
}

let app = NSApplication.shared
let delegate = GrammarBarApp()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
