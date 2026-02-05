import AppKit
import SwiftUI
import ArgumentParser

private let toastNotificationName = Notification.Name("smarts.companion.toast")
private let toastLockFileName = "smarts-companion-toast.lock"
private let maxToastCount = 4
private let toastSpacing: CGFloat = 10
private let toastSize = NSSize(width: 360, height: 80)

@main
struct CompanionCLI: ParsableCommand {
    static let configuration = CommandConfiguration(
        commandName: "companion",
        abstract: "Smarts companion helper app",
        subcommands: [Toast.self]
    )
}

struct Toast: ParsableCommand {
    static let configuration = CommandConfiguration(
        abstract: "Show a floating toast notification"
    )

    @Option(name: .long, help: "Toast message text")
    var message: String

    @Option(name: .long, help: "Duration in seconds")
    var duration: Double = 2.0

    func run() throws {
        ToastApp.run(message: message, duration: max(0.1, duration))
    }
}

private enum ToastApp {
    static func run(message: String, duration: Double) {
        if ToastManager.tryRunAsManager(initialMessage: message, duration: duration) {
            let app = NSApplication.shared
            app.setActivationPolicy(.prohibited)
            app.delegate = AppDelegate.shared
            app.run()
        } else {
            ToastManager.postToast(message: message, duration: duration)
        }
    }
}

private final class AppDelegate: NSObject, NSApplicationDelegate {
    static let shared = AppDelegate()

    func applicationWillTerminate(_ notification: Notification) {
        ToastManager.releaseLock()
    }
}

private final class ToastWindowController: NSObject, NSWindowDelegate {
    private let message: String
    private let duration: Double
    private var window: NSPanel?
    var onClose: (() -> Void)?

    init(message: String, duration: Double) {
        self.message = message
        self.duration = duration
        super.init()
    }

    func show(at frame: NSRect) {
        let view = ToastView(message: message)
        let hosting = NSHostingView(rootView: view)

        let window = ToastPanel(contentRect: frame)
        window.isOpaque = false
        window.backgroundColor = .clear
        window.hasShadow = true
        window.level = .floating
        window.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .transient]
        window.ignoresMouseEvents = true
        window.isFloatingPanel = true
        window.hidesOnDeactivate = false
        window.becomesKeyOnlyIfNeeded = true
        window.alphaValue = 0
        window.contentView = hosting
        window.delegate = self

        setInitialFrameForSlide(window: window, finalFrame: frame)

        self.window = window
        window.orderFront(nil)
        slideIn(window: window, finalFrame: frame)

        DispatchQueue.main.asyncAfter(deadline: .now() + duration) {
            self.slideOutAndClose(window: window, finalFrame: frame)
        }
    }

    func updateFrame(_ frame: NSRect) {
        window?.setFrame(frame, display: true)
    }

    func forceClose() {
        window?.orderOut(nil)
        window = nil
    }

    private func setInitialFrameForSlide(window: NSWindow, finalFrame: NSRect) {
        var start = finalFrame
        start.origin.y -= 18
        window.setFrame(start, display: false)
        window.alphaValue = 0
    }

    private func slideIn(window: NSWindow, finalFrame: NSRect) {
        NSAnimationContext.runAnimationGroup { context in
            context.duration = 0.18
            context.timingFunction = CAMediaTimingFunction(name: .easeOut)
            window.animator().alphaValue = 1
            window.animator().setFrame(finalFrame, display: true)
        }
    }

    private func slideOutAndClose(window: NSWindow, finalFrame: NSRect) {
        var end = finalFrame
        end.origin.y -= 12
        NSAnimationContext.runAnimationGroup({ context in
            context.duration = 0.18
            context.timingFunction = CAMediaTimingFunction(name: .easeIn)
            window.animator().alphaValue = 0
            window.animator().setFrame(end, display: true)
        }, completionHandler: {
            window.orderOut(nil)
            self.onClose?()
        })
    }
}

private final class ToastPanel: NSPanel {
    init(contentRect: NSRect) {
        super.init(
            contentRect: contentRect,
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
    }

    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }
}

private final class ToastManager {
    private static var shared: ToastManager?
    private var toasts: [ToastWindowController] = []
    private var idleTimer: Timer?

    static func tryRunAsManager(initialMessage: String, duration: Double) -> Bool {
        if !acquireLockIfPossible() {
            if !isLockOwnerAlive() {
                releaseLock()
                if !acquireLockIfPossible() {
                    return false
                }
            } else {
                return false
            }
        }

        let manager = ToastManager()
        shared = manager
        manager.start()
        manager.showToast(message: initialMessage, duration: duration)
        return true
    }

    static func postToast(message: String, duration: Double) {
        let center = DistributedNotificationCenter.default()
        center.post(
            name: toastNotificationName,
            object: nil,
            userInfo: ["message": message, "duration": duration]
        )
    }

    static func releaseLock() {
        let url = lockFileURL()
        try? FileManager.default.removeItem(at: url)
    }

    private func start() {
        let center = DistributedNotificationCenter.default()
        center.addObserver(
            self,
            selector: #selector(handleToastNotification(_:)),
            name: toastNotificationName,
            object: nil
        )
    }

    @objc private func handleToastNotification(_ notification: Notification) {
        let userInfo = notification.userInfo
        let message = userInfo?["message"] as? String ?? ""
        let duration = userInfo?["duration"] as? Double ?? 2.0
        showToast(message: message, duration: max(0.1, duration))
    }

    private func showToast(message: String, duration: Double) {
        idleTimer?.invalidate()

        if toasts.count >= maxToastCount {
            let oldest = toasts.removeFirst()
            oldest.forceClose()
        }

        let controller = ToastWindowController(message: message, duration: duration)
        controller.onClose = { [weak self, weak controller] in
            if let controller = controller {
                self?.removeToast(controller)
            }
        }
        toasts.append(controller)
        layoutToasts()
        if let frame = frameForToast(at: toasts.count - 1) {
            controller.show(at: frame)
        }
    }

    private func removeToast(_ controller: ToastWindowController) {
        toasts.removeAll { $0 === controller }
        layoutToasts()
        if toasts.isEmpty {
            scheduleIdleExit()
        }
    }

    private func layoutToasts() {
        for (index, toast) in toasts.enumerated() {
            if let frame = frameForToast(at: index) {
                toast.updateFrame(frame)
            }
        }
    }

    private func frameForToast(at index: Int) -> NSRect? {
        guard let screen = NSScreen.main else { return nil }
        let frame = screen.visibleFrame
        let margin: CGFloat = 24
        let x = frame.maxX - toastSize.width - margin
        let y = frame.minY + margin + CGFloat(index) * (toastSize.height + toastSpacing)
        return NSRect(x: x, y: y, width: toastSize.width, height: toastSize.height)
    }

    private func scheduleIdleExit() {
        idleTimer?.invalidate()
        idleTimer = Timer.scheduledTimer(withTimeInterval: 2.0, repeats: false) { _ in
            ToastManager.releaseLock()
            NSApp.terminate(nil)
        }
    }

    private static func acquireLockIfPossible() -> Bool {
        let url = lockFileURL()
        let fd = open(url.path, O_CREAT | O_EXCL | O_WRONLY, S_IRUSR | S_IWUSR)
        if fd == -1 {
            return false
        }
        let pid = "\(getpid())"
        _ = pid.withCString { ptr in
            write(fd, ptr, strlen(ptr))
        }
        close(fd)
        return true
    }

    private static func isLockOwnerAlive() -> Bool {
        let url = lockFileURL()
        guard let data = try? Data(contentsOf: url),
              let pidString = String(data: data, encoding: .utf8),
              let pid = Int32(pidString.trimmingCharacters(in: .whitespacesAndNewlines))
        else { return false }

        return kill(pid, 0) == 0
    }

    private static func lockFileURL() -> URL {
        FileManager.default.temporaryDirectory.appendingPathComponent(toastLockFileName)
    }
}

private struct ToastView: View {
    let message: String
    private let iconImage = CompanionIconResolver.load()
    private let iconSize: CGFloat = 20

    var body: some View {
        ZStack {
            VisualEffectView(material: .hudWindow, blendingMode: .behindWindow)
                .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))

            HStack(spacing: 12) {
                Group {
                    if let image = iconImage {
                        Image(nsImage: image)
                            .resizable()
                            .interpolation(.high)
                            .frame(width: iconSize, height: iconSize)
                            .clipShape(RoundedRectangle(cornerRadius: 5, style: .continuous))
                    } else {
                        Circle()
                            .fill(Color.accentColor)
                            .frame(width: 9, height: 9)
                    }
                }

                VStack(alignment: .leading, spacing: 2) {
                    Text("Smarts")
                        .font(.system(size: 13, weight: .semibold))
                        .foregroundColor(.primary)
                    Text(message)
                        .font(.system(size: 12))
                        .foregroundColor(.primary)
                        .lineLimit(2)
                }
                Spacer(minLength: 0)
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 12)
        }
        .frame(width: toastSize.width, height: toastSize.height)
    }
}

private enum CompanionIconResolver {
    static func load() -> NSImage? {
        let executableURL = URL(fileURLWithPath: CommandLine.arguments[0]).resolvingSymlinksInPath()
        let executableDir = executableURL.deletingLastPathComponent()

        let candidates: [URL] = [
            // Expected installed layout: <repo>/bin/companion + <repo>/companion/assets/...
            executableDir
                .deletingLastPathComponent()
                .appendingPathComponent("companion")
                .appendingPathComponent("assets")
                .appendingPathComponent("companion-icon-1024.png"),
            // Running from Swift build folder inside companion/
            executableDir
                .deletingLastPathComponent()
                .deletingLastPathComponent()
                .deletingLastPathComponent()
                .appendingPathComponent("assets")
                .appendingPathComponent("companion-icon-1024.png"),
            // Relative to current working directory
            URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
                .appendingPathComponent("companion")
                .appendingPathComponent("assets")
                .appendingPathComponent("companion-icon-1024.png")
        ]

        for candidate in candidates where FileManager.default.fileExists(atPath: candidate.path) {
            return NSImage(contentsOf: candidate)
        }
        return nil
    }
}

private struct VisualEffectView: NSViewRepresentable {
    let material: NSVisualEffectView.Material
    let blendingMode: NSVisualEffectView.BlendingMode

    func makeNSView(context: Context) -> NSVisualEffectView {
        let view = NSVisualEffectView()
        view.material = material
        view.blendingMode = blendingMode
        view.state = .active
        return view
    }

    func updateNSView(_ nsView: NSVisualEffectView, context: Context) {
        nsView.material = material
        nsView.blendingMode = blendingMode
    }
}
