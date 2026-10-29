import AppKit
import Foundation

final class Launcher: NSObject, NSApplicationDelegate, NSWindowDelegate {
    let service = Process()
    let output = Pipe()
    let state = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Application Support/MainAIger Lab", isDirectory: true)
    let url = URL(string: "http://127.0.0.1:8794/")!
    var window: NSWindow!
    var status: NSTextField!
    var ready = false
    var buffer = ""

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 520, height: 220),
                          styleMask: [.titled, .closable, .miniaturizable], backing: .buffered, defer: false)
        window.title = "MainAIger Lab"
        window.delegate = self
        window.center()
        let view = window.contentView!
        let title = NSTextField(labelWithString: "MainAIger Lab — developer preview")
        title.font = .boldSystemFont(ofSize: 20)
        title.frame = NSRect(x: 24, y: 168, width: 470, height: 28)
        view.addSubview(title)
        status = NSTextField(wrappingLabelWithString: "Starting the local Lab service…")
        status.frame = NSRect(x: 24, y: 105, width: 470, height: 50)
        view.addSubview(status)
        let open = NSButton(title: "Open Lab", target: self, action: #selector(openLab))
        open.frame = NSRect(x: 24, y: 45, width: 120, height: 32)
        view.addSubview(open)
        let key = NSButton(title: "Open owner key", target: self, action: #selector(openKey))
        key.frame = NSRect(x: 155, y: 45, width: 150, height: 32)
        view.addSubview(key)
        let quit = NSButton(title: "Quit Lab", target: self, action: #selector(quitLab))
        quit.frame = NSRect(x: 316, y: 45, width: 120, height: 32)
        view.addSubview(quit)
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        startService()
    }

    func startService() {
        guard let resource = Bundle.main.resourceURL else { status.stringValue = "Missing app resources."; return }
        service.executableURL = resource.appendingPathComponent("mainaiger-lab-service/mainaiger-lab-service")
        service.arguments = ["--port", "8794", "--state-dir", state.path]
        var environment = ProcessInfo.processInfo.environment
        environment["PATH"] = "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        service.environment = environment
        service.standardOutput = output
        service.standardError = output
        output.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            if data.isEmpty { handle.readabilityHandler = nil; return }
            let chunk = String(decoding: data, as: UTF8.self)
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.buffer += chunk
                if self.buffer.count > 8192 { self.buffer = String(self.buffer.suffix(8192)) }
                if !self.ready && self.buffer.contains("Lab panel: http://127.0.0.1:8794\n") {
                    self.ready = true
                    self.status.stringValue = "Lab is running on this Mac. Open the owner key to unlock the local page; keep it private."
                    NSWorkspace.shared.open(self.url)
                }
            }
        }
        service.terminationHandler = { [weak self] process in
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.ready = false
                self.status.stringValue = "Lab stopped (status \(process.terminationStatus)). Port 8794 may already be in use; check the developer instructions."
            }
        }
        do { try service.run() }
        catch { status.stringValue = "Could not start the bundled local service: \(error.localizedDescription)" }
    }

    @objc func openLab() {
        if ready && service.isRunning { NSWorkspace.shared.open(url) }
    }
    @objc func openKey() {
        let key = state.appendingPathComponent("owner.key")
        if ready && FileManager.default.fileExists(atPath: key.path) {
            let editor = URL(fileURLWithPath: "/System/Applications/TextEdit.app")
            NSWorkspace.shared.open([key], withApplicationAt: editor,
                                    configuration: NSWorkspace.OpenConfiguration(), completionHandler: nil)
        }
    }
    @objc func quitLab() { NSApp.terminate(nil) }
    func windowShouldClose(_ sender: NSWindow) -> Bool { NSApp.terminate(nil); return true }
    func applicationWillTerminate(_ notification: Notification) {
        output.fileHandleForReading.readabilityHandler = nil
        if service.isRunning { service.terminate() }
    }
}

let app = NSApplication.shared
let delegate = Launcher()
app.delegate = delegate
app.run()
