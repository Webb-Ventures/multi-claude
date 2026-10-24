import AppKit
import Foundation

struct Profile: Codable {
    let schemaVersion: Int
    let id: String
    let name: String
    let sourceApp: String
    let dataDirectory: String
    let codeConfigDirectory: String
    let color: String
    let codeProxyEnabled: Bool
}

enum ProfileError: LocalizedError {
    case invalid(String)
    var errorDescription: String? {
        switch self {
        case .invalid(let message): return message
        }
    }
}

func iconColor(_ value: String) throws -> NSColor {
    let hex = value.hasPrefix("#") ? String(value.dropFirst()) : value
    guard hex.count == 6, let number = UInt32(hex, radix: 16) else {
        throw ProfileError.invalid("Expected a six-digit icon colour.")
    }
    return NSColor(srgbRed: CGFloat((number >> 16) & 255) / 255,
                   green: CGFloat((number >> 8) & 255) / 255,
                   blue: CGFloat(number & 255) / 255, alpha: 1)
}

func renderIcons(source: String, directory: String, hex: String, initials: String, flat: Bool = false) throws {
    guard let original = NSImage(contentsOfFile: source) else {
        throw ProfileError.invalid("The source Claude icon could not be read.")
    }
    let color = try iconColor(hex)
    let destination = URL(fileURLWithPath: directory, isDirectory: true)
    try FileManager.default.createDirectory(at: destination, withIntermediateDirectories: true)
    let image = NSImage(size: NSSize(width: 1024, height: 1024))
    image.lockFocus()
    NSColor.clear.setFill()
    NSRect(x: 0, y: 0, width: 1024, height: 1024).fill()
    if flat {
        original.draw(in: NSRect(x: 0, y: 0, width: 1024, height: 1024),
                      from: .zero, operation: .sourceOver, fraction: 1)
    } else {
        color.setFill()
        NSBezierPath(roundedRect: NSRect(x: 28, y: 28, width: 968, height: 968),
                     xRadius: 212, yRadius: 212).fill()
        original.draw(in: NSRect(x: 105, y: 105, width: 814, height: 814),
                      from: .zero, operation: .sourceOver, fraction: 1)
        let badgeRect = NSRect(x: 630, y: 48, width: 310, height: 250)
        let badge = NSBezierPath(roundedRect: badgeRect, xRadius: 80, yRadius: 80)
        color.setFill()
        badge.fill()
        NSColor.white.withAlphaComponent(0.95).setStroke()
        badge.lineWidth = 12
        badge.stroke()
        let label = String(initials.prefix(2)).uppercased() as NSString
        let attributes: [NSAttributedString.Key: Any] = [
            .font: NSFont.systemFont(ofSize: 136, weight: .bold),
            .foregroundColor: NSColor.white,
        ]
        let size = label.size(withAttributes: attributes)
        label.draw(at: NSPoint(x: badgeRect.midX - size.width / 2,
                              y: badgeRect.midY - size.height / 2 + 5),
                   withAttributes: attributes)
    }
    image.unlockFocus()
    for points in [16, 32, 128, 256, 512] {
        for scale in [1, 2] {
            let pixels = points * scale
            guard let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: pixels,
                                                 pixelsHigh: pixels, bitsPerSample: 8,
                                                 samplesPerPixel: 4, hasAlpha: true,
                                                 isPlanar: false, colorSpaceName: .deviceRGB,
                                                 bytesPerRow: 0, bitsPerPixel: 0),
                  let context = NSGraphicsContext(bitmapImageRep: bitmap) else {
                throw ProfileError.invalid("Could not allocate the icon image.")
            }
            NSGraphicsContext.saveGraphicsState()
            NSGraphicsContext.current = context
            context.imageInterpolation = .high
            image.draw(in: NSRect(x: 0, y: 0, width: pixels, height: pixels),
                       from: .zero, operation: .copy, fraction: 1)
            context.flushGraphics()
            NSGraphicsContext.restoreGraphicsState()
            guard let png = bitmap.representation(using: .png, properties: [:]) else {
                throw ProfileError.invalid("Could not encode the icon image.")
            }
            let suffix = scale == 2 ? "@2x" : ""
            try png.write(to: destination.appendingPathComponent("icon_\(points)x\(points)\(suffix).png"),
                          options: .atomic)
        }
    }
}

func launchProfile() throws {
    guard let manifestURL = Bundle.main.url(forResource: "profile", withExtension: "json") else {
        throw ProfileError.invalid("This launcher has no profile manifest.")
    }
    let profile = try JSONDecoder().decode(Profile.self, from: Data(contentsOf: manifestURL))
    guard profile.schemaVersion == 1,
          UUID(uuidString: profile.id) != nil,
          NSString(string: profile.sourceApp).isAbsolutePath,
          NSString(string: profile.dataDirectory).isAbsolutePath,
          NSString(string: profile.codeConfigDirectory).isAbsolutePath else {
        throw ProfileError.invalid("The profile manifest is invalid.")
    }
    let source = URL(fileURLWithPath: profile.sourceApp, isDirectory: true)
    guard let bundle = Bundle(url: source),
          bundle.bundleIdentifier == "com.anthropic.claudefordesktop",
          let executable = bundle.executableURL,
          FileManager.default.isExecutableFile(atPath: executable.path) else {
        throw ProfileError.invalid("Claude is missing at \(profile.sourceApp). Restore the original installation or rebuild this launcher with --source.")
    }
    for path in [profile.dataDirectory, profile.codeConfigDirectory] {
        try FileManager.default.createDirectory(atPath: path, withIntermediateDirectories: true,
                                                attributes: [.posixPermissions: 0o700])
    }
    var environment = ProcessInfo.processInfo.environment
    let excluded = ["CLAUDE_USER_DATA_DIR", "CLAUDE_CODE_OAUTH_TOKEN", "ELECTRON_RUN_AS_NODE",
                    "NODE_TLS_REJECT_UNAUTHORIZED", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                    "http_proxy", "https_proxy", "all_proxy", "DISABLE_AUTOUPDATER"]
    for key in Array(environment.keys) where key.hasPrefix("ANTHROPIC_") || excluded.contains(key) {
        environment.removeValue(forKey: key)
    }
    environment["CLAUDE_CONFIG_DIR"] = profile.codeConfigDirectory
    let child = Process()
    child.executableURL = executable
    // Packaged Claude strips CLAUDE_USER_DATA_DIR; the Chromium argument is required.
    child.arguments = ["--user-data-dir=\(profile.dataDirectory)"]
    child.environment = environment
    child.standardInput = FileHandle.nullDevice
    child.standardOutput = FileHandle.nullDevice
    child.standardError = FileHandle.nullDevice
    try child.run()
}

final class LauncherDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        do {
            try launchProfile()
        } catch {
            let alert = NSAlert()
            alert.messageText = "Could not open this Claude profile"
            alert.informativeText = error.localizedDescription
            alert.alertStyle = .critical
            alert.runModal()
        }
        NSApplication.shared.terminate(nil)
    }
}

let arguments = CommandLine.arguments
if arguments.count == 6 && ["--make-icons", "--make-flat-icons"].contains(arguments[1]) {
    do {
        NSApplication.shared.setActivationPolicy(.prohibited)
        try renderIcons(source: arguments[2], directory: arguments[3], hex: arguments[4], initials: arguments[5],
                        flat: arguments[1] == "--make-flat-icons")
    } catch {
        fputs("claude-profile-icon: \(error.localizedDescription)\n", stderr)
        exit(1)
    }
} else {
    let application = NSApplication.shared
    application.setActivationPolicy(.accessory)
    let delegate = LauncherDelegate()
    application.delegate = delegate
    application.run()
}
