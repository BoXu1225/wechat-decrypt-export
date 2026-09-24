// WeChatSendHelper: a small, dumb UI driver for the WeChat macOS app.
//
// Runs as a LaunchAgent (LSUIElement, no Dock icon) so that the macOS
// Accessibility permission is granted to this one app instead of to python,
// a terminal, or sshd. It exposes UI primitives over a Unix domain socket:
//
//   ~/Library/Application Support/wechat-decrypt-export/sendhelper.sock
//
// (directory 0700, socket 0600, peer uid checked on every connection).
// Protocol: one JSON object per line in each direction.
//   request:  {"id": 1, "op": "chat_title", ...args}
//   response: {"id": 1, "ok": true, "result": {...}}
//          |  {"id": 1, "ok": false, "error": {"code": "...", "message": "..."}}
//
// All decisions (which chat, whether the title is right, whether to send)
// are made by wechat_send.py. The only compound op is "send", which re-checks
// that the title and input box still hold exactly what the caller approved
// and then presses the send key -- in one call, so nothing can change between
// the check and the keystroke. It never reads or returns message history.

import AppKit
import ApplicationServices
import Foundation
import ScreenCaptureKit
import Vision

let helperVersion = "10"
let wechatBundleID = "com.tencent.xinWeChat"

// MARK: - Errors / JSON

struct OpError: Error {
    let code: String
    let message: String
    init(_ code: String, _ message: String) {
        self.code = code
        self.message = message
    }
}

typealias JSON = [String: Any]

func str(_ req: JSON, _ key: String) throws -> String {
    guard let v = req[key] as? String else { throw OpError("bad_request", "missing string '\(key)'") }
    return v
}

// MARK: - AX helpers

let editRoles: Set<String> = ["AXTextArea", "AXTextField"]
let textRoles: Set<String> = ["AXStaticText", "AXTextField", "AXTextArea", "AXButton", "AXCell", "AXRow"]

func axAttr(_ el: AXUIElement, _ name: String) -> AnyObject? {
    var value: AnyObject?
    let err = AXUIElementCopyAttributeValue(el, name as CFString, &value)
    return err == .success ? value : nil
}

func axString(_ el: AXUIElement, _ name: String) -> String? {
    return axAttr(el, name) as? String
}

func axBool(_ el: AXUIElement, _ name: String) -> Bool? {
    return (axAttr(el, name) as? NSNumber)?.boolValue
}

@discardableResult
func axSet(_ el: AXUIElement, _ name: String, _ value: AnyObject) -> Bool {
    return AXUIElementSetAttributeValue(el, name as CFString, value) == .success
}

func axChildren(_ el: AXUIElement) -> [AXUIElement] {
    guard let v = axAttr(el, kAXChildrenAttribute) else { return [] }
    return (v as? [AXUIElement]) ?? []
}

func axFrame(_ el: AXUIElement) -> CGRect? {
    guard let p = axAttr(el, kAXPositionAttribute), let s = axAttr(el, kAXSizeAttribute) else { return nil }
    guard CFGetTypeID(p) == AXValueGetTypeID(), CFGetTypeID(s) == AXValueGetTypeID() else { return nil }
    var point = CGPoint.zero
    var size = CGSize.zero
    // swiftlint:disable force_cast
    guard AXValueGetValue(p as! AXValue, .cgPoint, &point),
          AXValueGetValue(s as! AXValue, .cgSize, &size) else { return nil }
    // swiftlint:enable force_cast
    return CGRect(origin: point, size: size)
}

func axText(_ el: AXUIElement) -> String? {
    for name in [kAXValueAttribute, kAXTitleAttribute, kAXDescriptionAttribute] {
        if let s = axString(el, name), !s.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return s
        }
    }
    return nil
}

func axElement(_ el: AXUIElement, _ name: String) -> AXUIElement? {
    guard let v = axAttr(el, name), CFGetTypeID(v) == AXUIElementGetTypeID() else { return nil }
    return (v as! AXUIElement)  // swiftlint:disable:this force_cast
}

func axRole(_ el: AXUIElement) -> String { return axString(el, kAXRoleAttribute) ?? "" }

func axSameElement(_ a: AXUIElement?, _ b: AXUIElement?) -> Bool {
    guard let a = a, let b = b else { return false }
    return CFEqual(a, b)
}

/// Depth-first walk, bounded in nodes and depth.
func axWalk(_ root: AXUIElement, maxNodes: Int = 5000, maxDepth: Int = 40,
            _ visit: (AXUIElement, Int) -> Void) {
    var stack: [(AXUIElement, Int)] = [(root, 0)]
    var n = 0
    while let (el, depth) = stack.popLast(), n < maxNodes {
        n += 1
        visit(el, depth)
        if depth < maxDepth {
            for child in axChildren(el).reversed() { stack.append((child, depth + 1)) }
        }
    }
}

struct Elem {
    let el: AXUIElement
    let role: String
    let frame: CGRect
}

// MARK: - Events

enum Key: CGKeyCode {
    case a = 0, f = 3, v = 9, returnKey = 36, delete = 51, escape = 53
}

/// Sleeps; on the main thread it keeps the run loop turning so NSWorkspace
/// state (frontmostApplication) stays current while an op waits.
func sleepMs(_ ms: Int) {
    if Thread.isMainThread {
        RunLoop.current.run(until: Date(timeIntervalSinceNow: Double(ms) / 1000))
    } else {
        usleep(useconds_t(ms * 1000))
    }
}

// MARK: - Driver

final class Driver {
    var pid: pid_t = 0
    var app: AXUIElement?
    var searchArmed = false
    var menuArmed = false
    var sessionStart: Date?
    var ownEvents: [Date] = []
    let banner = Banner()
    var searchField: AXUIElement?

    // --- state ---

    func screenLocked() -> Bool {
        guard let d = CGSessionCopyCurrentDictionary() as? [String: Any] else { return false }
        return (d["CGSSessionScreenIsLocked"] as? NSNumber)?.boolValue ?? false
    }

    func onConsole() -> Bool {
        guard let d = CGSessionCopyCurrentDictionary() as? [String: Any] else { return false }
        return (d[kCGSessionOnConsoleKey as String] as? NSNumber)?.boolValue ?? false
    }

    func wechat() -> NSRunningApplication? {
        return NSRunningApplication.runningApplications(withBundleIdentifier: wechatBundleID)
            .first { !$0.isTerminated }
    }

    /// Asks the accessibility server (always current) and falls back to
    /// NSWorkspace, whose cached value only updates as the run loop turns.
    func frontmost() -> NSRunningApplication? {
        if AXIsProcessTrusted(), let f = axAttr(AXUIElementCreateSystemWide(), kAXFocusedApplicationAttribute) {
            var p: pid_t = 0
            // swiftlint:disable:next force_cast
            if AXUIElementGetPid(f as! AXUIElement, &p) == .success, let a = NSRunningApplication(processIdentifier: p) {
                return a
            }
        }
        return NSWorkspace.shared.frontmostApplication
    }

    func attach() throws -> AXUIElement {
        guard AXIsProcessTrusted() else {
            throw OpError("not_trusted", "WeChatSendHelper has no Accessibility permission")
        }
        guard let w = wechat() else { throw OpError("wechat_not_running", "WeChat is not running") }
        if w.processIdentifier != pid || app == nil {
            pid = w.processIdentifier
            let a = AXUIElementCreateApplication(pid)
            AXUIElementSetMessagingTimeout(a, 2.0)
            // Chromium/Qt-style apps build their accessibility tree only
            // when an assistive client asks for it through one of these.
            axSet(a, "AXManualAccessibility", kCFBooleanTrue)
            axSet(a, "AXEnhancedUserInterface", kCFBooleanTrue)
            app = a
            searchField = nil
            sleepMs(300)
        }
        return app!
    }

    func mainWindow() throws -> AXUIElement? {
        let a = try attach()
        let wins = (axAttr(a, kAXWindowsAttribute) as? [AXUIElement]) ?? []
        var best: AXUIElement?
        var bestArea: CGFloat = -1
        for w in wins {
            let sub = axString(w, kAXSubroleAttribute)
            if let sub = sub, sub != (kAXStandardWindowSubrole as String) { continue }
            let f = axFrame(w) ?? .zero
            let area = f.width * f.height
            if area > bestArea { best = w; bestArea = area }
        }
        return best
    }

    func requireWindow() throws -> AXUIElement {
        guard let w = try mainWindow() else { throw OpError("no_window", "WeChat main window not found") }
        return w
    }

    func requireReady() throws {
        if screenLocked() { throw OpError("screen_locked", "the screen is locked") }
        _ = try attach()
    }

    func requireFront() throws {
        try requireReady()
        try checkUserActivity()
        guard frontmost()?.processIdentifier == pid else {
            throw OpError("not_frontmost", "WeChat is not the frontmost app")
        }
    }

    // --- events (keyboard events go to WeChat's pid only) ---

    func key(_ k: Key, cmd: Bool = false) {
        let src = CGEventSource(stateID: .hidSystemState)
        for down in [true, false] {
            guard let ev = CGEvent(keyboardEventSource: src, virtualKey: k.rawValue, keyDown: down) else { continue }
            ev.flags = cmd ? .maskCommand : []
            noteOwnEvent()
            ev.postToPid(pid)
            sleepMs(25)
        }
    }

    func click(_ rect: CGRect, right: Bool = false) {
        let saved = CGEvent(source: nil)?.location
        let pt = CGPoint(x: rect.midX, y: rect.midY)
        let types: [CGEventType] = right ? [.rightMouseDown, .rightMouseUp] : [.leftMouseDown, .leftMouseUp]
        for type in types {
            noteOwnEvent()
            CGEvent(mouseEventSource: nil, mouseType: type, mouseCursorPosition: pt,
                    mouseButton: right ? .right : .left)?.post(tap: .cghidEventTap)
            sleepMs(40)
        }
        if let saved = saved { CGWarpMouseCursorPosition(saved) }
    }

    // --- clipboard ---

    /// Put `text` on the clipboard, run `body` (which pastes), then restore the
    /// user's clipboard unless someone else changed it in the meantime.
    func withClipboard(_ text: String, _ body: () throws -> Void) rethrows {
        let pb = NSPasteboard.general
        var saved: [[NSPasteboard.PasteboardType: Data]] = []
        for item in pb.pasteboardItems ?? [] {
            var d: [NSPasteboard.PasteboardType: Data] = [:]
            for t in item.types { if let data = item.data(forType: t) { d[t] = data } }
            saved.append(d)
        }
        pb.clearContents()
        let item = NSPasteboardItem()
        item.setString(text, forType: .string)
        item.setString("", forType: NSPasteboard.PasteboardType("org.nspasteboard.TransientType"))
        item.setString("", forType: NSPasteboard.PasteboardType("org.nspasteboard.ConcealedType"))
        pb.writeObjects([item])
        let ours = pb.changeCount
        defer {
            if pb.changeCount == ours {
                pb.clearContents()
                let items: [NSPasteboardItem] = saved.map { d in
                    let it = NSPasteboardItem()
                    for (t, data) in d { it.setData(data, forType: t) }
                    return it
                }
                if !items.isEmpty { pb.writeObjects(items) }
            }
        }
        try body()
    }

    func pasteInto(_ el: AXUIElement, _ text: String) {
        let before = axString(el, kAXValueAttribute)
        withClipboard(text) {
            key(.v, cmd: true)
            // Wait until the app has read the clipboard (value changed).
            for _ in 0..<30 {
                sleepMs(50)
                if axString(el, kAXValueAttribute) != before { break }
            }
            sleepMs(100)
        }
    }

    // --- layout ---

    func elements(_ win: AXUIElement, roles: Set<String>) -> [Elem] {
        var out: [Elem] = []
        axWalk(win) { el, _ in
            let r = axRole(el)
            if roles.contains(r), let f = axFrame(el) { out.append(Elem(el: el, role: r, frame: f)) }
        }
        return out
    }

    /// Chat input: the lowest wide editable text element in the lower half.
    func inputBox(_ win: AXUIElement, _ elems: [Elem]? = nil) -> Elem? {
        guard let wf = axFrame(win) else { return nil }
        let cands = (elems ?? elements(win, roles: editRoles)).filter {
            editRoles.contains($0.role) && $0.frame.minY >= wf.minY + wf.height * 0.5
                && $0.frame.width >= wf.width * 0.3
        }
        return cands.max { $0.frame.minY < $1.frame.minY }
    }

    func requireInput() throws -> (AXUIElement, Elem) {
        let win = try requireWindow()
        guard let box = inputBox(win) else { throw OpError("no_input", "chat input box not found") }
        return (win, box)
    }

    /// Chat title: the top-most static text in the header of the chat pane
    /// (the pane is the region right of the input box's left edge).
    func title(_ win: AXUIElement) -> String? {
        let elems = elements(win, roles: textRoles)
        guard let wf = axFrame(win), let box = inputBox(win, elems) else { return nil }
        let paneX = box.frame.minX - 10
        let header = elems.filter {
            $0.role == "AXStaticText" && $0.frame.minX >= paneX
                && $0.frame.minY >= wf.minY && $0.frame.minY < wf.minY + 90
        }.sorted { ($0.frame.minY, $0.frame.minX) < ($1.frame.minY, $1.frame.minX) }
        for e in header {
            if let t = axText(e.el) { return t.trimmingCharacters(in: .whitespacesAndNewlines) }
        }
        return nil
    }

    func focusInput(_ box: Elem) throws {
        let a = try attach()
        axSet(box.el, kAXFocusedAttribute, kCFBooleanTrue)
        sleepMs(80)
        if !axSameElement(axElement(a, kAXFocusedUIElementAttribute), box.el) {
            click(box.frame)
            sleepMs(150)
        }
        guard axSameElement(axElement(a, kAXFocusedUIElementAttribute), box.el) else {
            throw OpError("focus_failed", "could not focus the chat input box")
        }
    }

    // --- ops ---

    func status() -> JSON {
        var r: JSON = [
            "version": helperVersion,
            "helper_pid": Int(getpid()),
            "trusted": AXIsProcessTrusted(),
            "screen_capture": CGPreflightScreenCaptureAccess(),
            "screen_locked": screenLocked(),
            "on_console": onConsole(),
        ]
        if let w = wechat() {
            r["wechat_running"] = true
            r["wechat_frontmost"] = frontmost()?.processIdentifier == w.processIdentifier
            r["wechat_hidden"] = w.isHidden
            if AXIsProcessTrusted(), let win = try? mainWindow() {
                r["wechat_window"] = true
                r["wechat_minimized"] = axBool(win, kAXMinimizedAttribute) ?? false
            } else {
                r["wechat_window"] = AXIsProcessTrusted() ? false : NSNull()
            }
        } else {
            r["wechat_running"] = false
        }
        return r
    }

    func activate() throws -> JSON {
        try requireReady()
        guard let w = wechat() else { throw OpError("wechat_not_running", "WeChat is not running") }
        let prev = frontmost()
        if w.isHidden { w.unhide() }
        var win = try mainWindow()
        if win == nil, let url = w.bundleURL {
            // Main window closed: a reopen event brings it back.
            let sem = DispatchSemaphore(value: 0)
            NSWorkspace.shared.openApplication(at: url, configuration: NSWorkspace.OpenConfiguration()) { _, _ in
                sem.signal()
            }
            _ = sem.wait(timeout: .now() + 3)
            sleepMs(800)
            win = try mainWindow()
        }
        guard let window = win else { throw OpError("no_window", "WeChat main window not found (logged in?)") }
        if axBool(window, kAXMinimizedAttribute) == true {
            axSet(window, kAXMinimizedAttribute, kCFBooleanFalse)
            sleepMs(400)
        }
        axSet(app!, kAXFrontmostAttribute, kCFBooleanTrue)
        w.activate()
        AXUIElementPerformAction(window, kAXRaiseAction as CFString)
        if frontmost()?.processIdentifier != w.processIdentifier, let url = w.bundleURL {
            // macOS 14+ ignores activate() from a background agent;
            // an open request through LaunchServices is still honored.
            let cfg = NSWorkspace.OpenConfiguration()
            cfg.activates = true
            let sem = DispatchSemaphore(value: 0)
            NSWorkspace.shared.openApplication(at: url, configuration: cfg) { _, _ in sem.signal() }
            _ = sem.wait(timeout: .now() + 3)
        }
        for _ in 0..<30 {
            if frontmost()?.processIdentifier == w.processIdentifier { break }
            sleepMs(50)
        }
        guard frontmost()?.processIdentifier == w.processIdentifier else {
            throw OpError("activate_failed", "could not bring WeChat to the front")
        }
        return ["previous_pid": prev.map { Int($0.processIdentifier) } ?? NSNull()]
    }

    func restore(_ req: JSON) throws -> JSON {
        guard let p = req["pid"] as? Int, p > 0, p != Int(pid),
              let other = NSRunningApplication(processIdentifier: pid_t(p)) else {
            return ["restored": false]
        }
        other.activate()
        return ["restored": true]
    }

    func openSearch(_ req: JSON) throws -> JSON {
        let query = try str(req, "query")
        guard !query.isEmpty, query.count <= 200 else { throw OpError("bad_request", "bad query") }
        try requireFront()
        key(.f, cmd: true)
        sleepMs(300)
        guard let field = axElement(app!, kAXFocusedUIElementAttribute) else {
            throw OpError("no_search", "search box did not take focus")
        }
        guard editRoles.contains(axRole(field)) else {
            key(.escape)
            throw OpError("no_search", "focused element after Cmd+F is not a text field")
        }
        searchField = field
        key(.a, cmd: true)
        pasteInto(field, query)
        return ["value_matches": axString(field, kAXValueAttribute) == query]
    }

    /// Visible text elements in the search results area (left column under the
    /// search box), top to bottom. Texts are returned so the caller can pick an
    /// exact match; they are never logged by the helper.
    func results() throws -> [Elem] {
        let win = try requireWindow()
        guard let sf = searchField, let sfr = axFrame(sf) else {
            throw OpError("no_search", "no active search; call open_search first")
        }
        let wf = axFrame(win) ?? .zero
        let elems = elements(win, roles: ["AXStaticText", "AXCell", "AXRow", "AXButton"])
        // The results list spans at most the left part of the window.
        let maxX = max(sfr.maxX + 60, wf.minX + wf.width * 0.45)
        return elems.filter {
            $0.frame.minY > sfr.maxY && $0.frame.minX >= wf.minX && $0.frame.minX < maxX
                && $0.frame.height > 0 && $0.frame.height < 200
        }.sorted { ($0.frame.minY, $0.frame.minX) < ($1.frame.minY, $1.frame.minX) }
    }

    func searchResults() throws -> JSON {
        try requireFront()
        let rows: [JSON] = try results().compactMap { e in
            guard let t = axText(e.el) else { return nil }
            return ["text": t, "role": e.role,
                    "x": Int(e.frame.minX), "y": Int(e.frame.minY), "h": Int(e.frame.height)]
        }
        return ["results": rows]
    }

    func clickResult(_ req: JSON) throws -> JSON {
        let text = try str(req, "text")
        try requireFront()
        let match = try results().first {
            axText($0.el)?.trimmingCharacters(in: .whitespacesAndNewlines) == text
        }
        guard let m = match else { throw OpError("not_found", "no search result with that exact text") }
        click(m.frame)
        sleepMs(400)
        searchField = nil
        return ["clicked": true, "y": Int(m.frame.minY)]
    }

    func searchEnter() throws -> JSON {
        try requireFront()
        guard let sf = searchField,
              axSameElement(axElement(app!, kAXFocusedUIElementAttribute), sf) else {
            throw OpError("no_search", "search box is not focused")
        }
        key(.returnKey)
        sleepMs(400)
        searchField = nil
        return [:]
    }

    func escape() throws -> JSON {
        try requireFront()
        key(.escape)
        searchField = nil
        return [:]
    }

    func chatTitle() throws -> JSON {
        try requireReady()
        let win = try requireWindow()
        return ["title": title(win) ?? NSNull()]
    }

    func inputText() throws -> JSON {
        try requireReady()
        let (_, box) = try requireInput()
        return ["text": axString(box.el, kAXValueAttribute) ?? NSNull()]
    }

    func pasteInput(_ req: JSON) throws -> JSON {
        let text = try str(req, "text")
        guard !text.isEmpty, text.count <= 20000 else { throw OpError("bad_request", "bad text") }
        try requireFront()
        let (_, box) = try requireInput()
        try focusInput(box)
        pasteInto(box.el, text)
        return ["text": axString(box.el, kAXValueAttribute) ?? NSNull()]
    }

    func clearInput() throws -> JSON {
        try requireFront()
        let (_, box) = try requireInput()
        try focusInput(box)
        key(.a, cmd: true)
        key(.delete)
        sleepMs(100)
        return ["text": axString(box.el, kAXValueAttribute) ?? NSNull()]
    }

    /// Compound op: re-check title and input against what the caller approved,
    /// then press the send key, all in one call.
    func send(_ req: JSON) throws -> JSON {
        let expectedTitle = try str(req, "expected_title")
        let expectedInput = try str(req, "expected_input")
        let keyName = (req["key"] as? String) ?? "enter"
        guard keyName == "enter" || keyName == "cmd_enter" else {
            throw OpError("bad_request", "key must be enter or cmd_enter")
        }
        guard !expectedInput.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            throw OpError("bad_request", "empty expected_input")
        }
        try requireFront()
        let win = try requireWindow()
        guard title(win) == expectedTitle else {
            throw OpError("precondition_failed", "chat title is not the approved one")
        }
        guard let box = inputBox(win) else { throw OpError("no_input", "chat input box not found") }
        guard axString(box.el, kAXValueAttribute) == expectedInput else {
            throw OpError("precondition_failed", "input box does not hold the approved text")
        }
        try focusInput(box)
        guard frontmost()?.processIdentifier == pid else {
            throw OpError("not_frontmost", "WeChat lost focus")
        }
        key(.returnKey, cmd: keyName == "cmd_enter")
        var leftover = axString(box.el, kAXValueAttribute)
        for _ in 0..<20 {
            sleepMs(50)
            leftover = axString(box.el, kAXValueAttribute)
            if (leftover ?? "").isEmpty { break }
        }
        return ["pressed": true, "leftover": leftover ?? NSNull()]
    }

    func probe(_ req: JSON) throws -> JSON {
        try requireReady()
        let showText = (req["show_text"] as? Bool) ?? false
        let maxNodes = min((req["max_nodes"] as? Int) ?? 3000, 20000)
        let win = try requireWindow()
        var lines: [String] = []
        axWalk(win, maxNodes: maxNodes) { el, depth in
            var line = String(repeating: "  ", count: depth) + axRole(el)
            if let sub = axString(el, kAXSubroleAttribute) { line += "/\(sub)" }
            if let f = axFrame(el) {
                line += " (\(Int(f.minX)),\(Int(f.minY)) \(Int(f.width))x\(Int(f.height)))"
            }
            for name in [kAXValueAttribute, kAXTitleAttribute, kAXDescriptionAttribute] {
                if let s = axString(el, name), !s.isEmpty {
                    line += showText ? " \(name)=\(s.debugDescription)" : " \(name)=<\(s.count) chars>"
                }
            }
            if axBool(el, kAXFocusedAttribute) == true { line += " [focused]" }
            lines.append(line)
        }
        var diag: [String] = []
        let a = try attach()
        var names: CFArray?
        if AXUIElementCopyAttributeNames(a, &names) == .success, let n = names as? [String] {
            diag.append("app attrs: " + n.joined(separator: ","))
        }
        for attr in ["AXManualAccessibility", "AXEnhancedUserInterface"] {
            diag.append("app \(attr)=\(String(describing: axAttr(a, attr)))")
        }
        diag.append("app children: \(axChildren(a).count)")
        if AXUIElementCopyAttributeNames(win, &names) == .success, let n = names as? [String] {
            diag.append("window attrs: " + n.joined(separator: ","))
        }
        if let nav = axAttr(win, "AXChildrenInNavigationOrder") as? [AXUIElement] {
            diag.append("window nav children: \(nav.count)")
        }
        if let f = axElement(a, kAXFocusedUIElementAttribute) {
            diag.append("focused: \(axRole(f)) \(axFrame(f).map { "\($0)" } ?? "-")")
        }
        if let wf = axFrame(win) {
            // Hit-test a grid: some toolkits answer these even with no children.
            for (fx, fy) in [(0.15, 0.1), (0.15, 0.5), (0.6, 0.06), (0.6, 0.5), (0.6, 0.9)] {
                var hit: AXUIElement?
                let x = Float(wf.minX + wf.width * CGFloat(fx)), y = Float(wf.minY + wf.height * CGFloat(fy))
                if AXUIElementCopyElementAtPosition(a, x, y, &hit) == .success, let h = hit {
                    diag.append("hit(\(fx),\(fy)): \(axRole(h)) \(axFrame(h).map { "\($0)" } ?? "-")")
                } else {
                    diag.append("hit(\(fx),\(fy)): none")
                }
            }
        }
        return ["lines": lines, "diag": diag]
    }

    func dispatch(_ op: String, _ req: JSON) throws -> JSON {
        switch op {
        case "ping": return ["version": helperVersion]
        case "status": return status()
        case "request_trust":
            let opts = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
            return ["trusted": AXIsProcessTrustedWithOptions(opts)]
        case "activate": return try activate()
        case "restore": return try restore(req)
        case "open_search": return try openSearch(req)
        case "search_results": return try searchResults()
        case "click_result": return try clickResult(req)
        case "search_enter": return try searchEnter()
        case "escape": return try escape()
        case "chat_title": return try chatTitle()
        case "input_text": return try inputText()
        case "paste_input": return try pasteInput(req)
        case "clear_input": return try clearInput()
        case "send": return try send(req)
        case "probe": return try probe(req)
        case "v_ocr": return try vOCR(req)
        case "idle": return try vIdle(req)
        case "session_begin": return try sessionBegin(req)
        case "session_end": return try sessionEnd(req)
        case "v_open_search": return try vOpenSearch(req)
        case "v_search_enter": return try vSearchEnter(req)
        case "v_click_popup": return try vClickPopup(req)
        case "v_right_click": return try vRightClick(req)
        case "v_click": return try vClick(req)
        case "v_paste": return try vPaste(req)
        case "v_clear": return try vClear(req)
        case "v_send": return try vSend(req)
        default: throw OpError("unknown_op", "unknown op")
        }
    }
}

let opTimeouts: [String: Double] = [
    "ping": 2, "status": 5, "request_trust": 5, "activate": 8, "restore": 3,
    "open_search": 6, "search_results": 6, "click_result": 6, "search_enter": 4,
    "escape": 3, "chat_title": 6, "input_text": 4, "paste_input": 6,
    "clear_input": 4, "send": 6, "probe": 30,
    "v_ocr": 10, "v_open_search": 6, "v_search_enter": 10, "v_paste": 6, "v_clear": 5, "v_click_popup": 10, "v_right_click": 5, "v_click": 5, "idle": 2, "session_begin": 3, "session_end": 3,
    "v_send": 25,
]

// MARK: - Server

final class Box: @unchecked Sendable {
    var value: JSON = [:]
}

final class Server {
    let path: String
    let driver = Driver()
    var busy = false  // guarded by lock
    let lock = NSLock()

    init(path: String) { self.path = path }

    func log(_ s: String) {
        FileHandle.standardError.write(("[sendhelper] " + s + "\n").data(using: .utf8)!)
    }

    func prepareDirectory() throws {
        let dir = (path as NSString).deletingLastPathComponent
        try FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true,
                                                attributes: [.posixPermissions: 0o700])
        chmod(dir, 0o700)
        var st = stat()
        guard lstat(dir, &st) == 0, (st.st_mode & S_IFMT) == S_IFDIR, st.st_uid == getuid() else {
            throw OpError("setup", "socket directory is not a directory owned by us")
        }
    }

    func listen() throws -> Int32 {
        try prepareDirectory()
        var st = stat()
        if lstat(path, &st) == 0 {
            guard (st.st_mode & S_IFMT) == S_IFSOCK else { throw OpError("setup", "socket path exists and is not a socket") }
            unlink(path)
        }
        let fd = socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else { throw OpError("setup", "socket() failed") }
        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        let bytes = Array(path.utf8)
        guard bytes.count < MemoryLayout.size(ofValue: addr.sun_path) else { throw OpError("setup", "socket path too long") }
        withUnsafeMutableBytes(of: &addr.sun_path) { buf in
            for (i, b) in bytes.enumerated() { buf[i] = b }
            buf[bytes.count] = 0
        }
        let old = umask(0o177)
        let rc = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                bind(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size))
            }
        }
        umask(old)
        guard rc == 0 else { close(fd); throw OpError("setup", "bind failed: \(errno)") }
        chmod(path, 0o600)
        guard Darwin.listen(fd, 4) == 0 else { close(fd); throw OpError("setup", "listen failed") }
        return fd
    }

    func run() {
        let fd: Int32
        do { fd = try listen() } catch {
            log("fatal: \(error)")
            exit(1)
        }
        log("listening on \(path)")
        // One connection at a time: requests are strictly serialized.
        while true {
            let c = accept(fd, nil, nil)
            if c < 0 { continue }
            handle(c)
            close(c)
        }
    }

    func handle(_ c: Int32) {
        var uid: uid_t = 0
        var gid: gid_t = 0
        guard getpeereid(c, &uid, &gid) == 0, uid == getuid() else {
            log("rejected connection from uid \(uid)")
            return
        }
        var nosig: Int32 = 1
        setsockopt(c, SOL_SOCKET, SO_NOSIGPIPE, &nosig, socklen_t(MemoryLayout<Int32>.size))
        var tv = timeval(tv_sec: 30, tv_usec: 0)  // idle timeout per connection
        setsockopt(c, SOL_SOCKET, SO_RCVTIMEO, &tv, socklen_t(MemoryLayout<timeval>.size))

        var buffer = Data()
        var chunk = [UInt8](repeating: 0, count: 65536)
        while true {
            let n = read(c, &chunk, chunk.count)
            if n <= 0 { return }
            buffer.append(chunk, count: n)
            if buffer.count > 1_000_000 { return }
            while let nl = buffer.firstIndex(of: 0x0A) {
                let line = buffer.subdata(in: buffer.startIndex..<nl)
                buffer = Data(buffer[(nl + 1)...])
                let resp = respond(line)
                guard var out = try? JSONSerialization.data(withJSONObject: resp) else { return }
                out.append(0x0A)
                let ok = out.withUnsafeBytes { raw -> Bool in
                    var off = 0
                    while off < raw.count {
                        let w = write(c, raw.baseAddress! + off, raw.count - off)
                        if w <= 0 { return false }
                        off += w
                    }
                    return true
                }
                if !ok { return }
            }
        }
    }

    func respond(_ line: Data) -> JSON {
        guard let req = (try? JSONSerialization.jsonObject(with: line)) as? JSON,
              let op = req["op"] as? String else {
            return ["ok": false, "error": ["code": "bad_request", "message": "invalid JSON request"]]
        }
        let id = req["id"] ?? NSNull()
        lock.lock()
        if busy {
            lock.unlock()
            return ["id": id, "ok": false, "error": ["code": "busy", "message": "a previous operation is still running"]]
        }
        busy = true
        lock.unlock()

        // UI work runs on the main thread; we wait with a per-op timeout.
        let box = Box()
        let sem = DispatchSemaphore(value: 0)
        let driver = self.driver
        DispatchQueue.main.async {
            do {
                box.value = ["id": id, "ok": true, "result": try driver.dispatch(op, req)]
            } catch let e as OpError {
                box.value = ["id": id, "ok": false, "error": ["code": e.code, "message": e.message]]
            } catch {
                box.value = ["id": id, "ok": false, "error": ["code": "internal", "message": "\(error)"]]
            }
            self.lock.lock()
            self.busy = false
            self.lock.unlock()
            sem.signal()
        }
        if sem.wait(timeout: .now() + (opTimeouts[op] ?? 5)) == .timedOut {
            return ["id": id, "ok": false, "error": ["code": "timeout", "message": "operation timed out"]]
        }
        return box.value
    }
}



// MARK: - Send session: on-screen banner + user-activity guard
//
// A send takes ~10 s of real keyboard focus. session_begin shows a small
// banner ("Sending to X -- hands off") and starts watching for the user's own
// input; every UI op after that fails with "user_activity" if the user pressed
// a key, clicked or scrolled since the session began (the helper's own posted
// events are excluded). session_end shows the outcome briefly and hides it.
// The banner is a non-activating panel that ignores the mouse, so it never
// takes focus; it isn't captured either (captures are per-window).

final class Banner {
    var panel: NSPanel?
    var label: NSTextField?
    var hideAt: Date?

    func show(_ text: String, color: NSColor) {
        if panel == nil {
            let p = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 460, height: 44),
                            styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
            p.level = .statusBar
            p.isOpaque = false
            p.backgroundColor = .clear
            p.ignoresMouseEvents = true
            p.hasShadow = true
            p.hidesOnDeactivate = false
            p.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary]
            let bg = NSVisualEffectView(frame: p.contentView!.bounds)
            bg.material = .hudWindow
            bg.state = .active
            bg.wantsLayer = true
            bg.layer?.cornerRadius = 12
            bg.autoresizingMask = [.width, .height]
            let l = NSTextField(labelWithString: "")
            l.font = .systemFont(ofSize: 14, weight: .semibold)
            l.alignment = .center
            l.lineBreakMode = .byTruncatingTail
            l.frame = NSRect(x: 12, y: 12, width: 436, height: 20)
            l.autoresizingMask = [.width]
            bg.addSubview(l)
            p.contentView = bg
            panel = p
            label = l
        }
        label?.stringValue = text
        label?.textColor = color
        if let screen = NSScreen.main, let p = panel {
            let f = screen.visibleFrame
            p.setFrameOrigin(NSPoint(x: f.midX - p.frame.width / 2, y: f.maxY - p.frame.height - 12))
        }
        hideAt = nil
        panel?.orderFrontRegardless()
        panel?.display()
    }

    func hide(after seconds: Double) {
        let at = Date(timeIntervalSinceNow: seconds)
        hideAt = at
        DispatchQueue.main.asyncAfter(deadline: .now() + seconds) { [weak self] in
            guard let self = self, self.hideAt == at else { return }
            self.panel?.orderOut(nil)
        }
    }
}

let userInputTypes: [CGEventType] = [.keyDown, .leftMouseDown, .rightMouseDown, .otherMouseDown, .scrollWheel]

extension Driver {
    /// Seconds since the last user keyboard / click / scroll event (any
    /// process), and since any input at all (including mouse movement).
    func idleTimes() -> (input: Double, any: Double) {
        let input = userInputTypes.map {
            CGEventSource.secondsSinceLastEventType(.combinedSessionState, eventType: $0)
        }.min() ?? .infinity
        let any = CGEventSource.secondsSinceLastEventType(.combinedSessionState,
                                                         eventType: CGEventType(rawValue: ~0)!)
        return (input, any)
    }

    /// Throws user_activity if the user pressed a key / clicked / scrolled
    /// since the session began. Our own events are recorded in ownEvents and
    /// an input event within 0.4 s after one of them is attributed to us.
    func checkUserActivity() throws {
        guard let start = sessionStart else { return }
        let now = Date()
        let last = now.addingTimeInterval(-idleTimes().input)
        guard last > start.addingTimeInterval(0.05) else { return }
        let ours = ownEvents.contains { last >= $0.addingTimeInterval(-0.05) && last <= $0.addingTimeInterval(0.4) }
        if !ours {
            throw OpError("user_activity", "you used the keyboard or mouse during the send; stopped")
        }
    }

    func noteOwnEvent() {
        if sessionStart != nil { ownEvents.append(Date()) }
    }

    func vIdle(_ req: JSON) throws -> JSON {
        let t = idleTimes()
        return ["input_idle_s": t.input.isFinite ? t.input : 1e9, "any_idle_s": t.any.isFinite ? t.any : 1e9]
    }

    func sessionBegin(_ req: JSON) throws -> JSON {
        let text = (req["banner"] as? String) ?? "Sending a WeChat message — hands off the keyboard and mouse"
        sessionStart = Date()
        ownEvents = []
        banner.show(String(text.prefix(120)), color: .labelColor)
        return [:]
    }

    func sessionEnd(_ req: JSON) throws -> JSON {
        let ok = (req["ok"] as? Bool) ?? false
        let text = (req["banner"] as? String) ?? (ok ? "Sent" : "Stopped")
        sessionStart = nil
        ownEvents = []
        banner.show(String(text.prefix(120)), color: ok ? .systemGreen : .systemOrange)
        banner.hide(after: (req["linger_s"] as? Double) ?? 2.5)
        return [:]
    }
}

// MARK: - Vision mode (WeChat 4 exposes no accessibility tree)
//
// WeChat 4.x draws its own UI and publishes only the window buttons to
// Accessibility, so the chat title and input box can't be read through AX.
// In vision mode the helper screenshots WeChat's main window (Screen
// Recording permission) and reads regions with Apple's on-device OCR.
// Layout decisions (where the title / input are) are made by the caller; the
// helper enforces coarse bounds so that e.g. a "search" Enter can never be
// pressed while the chat input is what holds the text:
//   - v_search_enter only after v_open_search, and only if the query is
//     visible in a rect in the top 15% of the window (the search box);
//   - v_paste / v_clear only click in the lower half of the window;
//   - v_send re-reads the title rect (top 15%) and the input rect (lower
//     half) and presses the key only if both match the approved strings.
// Coordinates are window-relative points, origin top-left.

final class Pending<T> {
    let lock = NSLock()
    var value: T?
    var error: Error?
    var done = false
    func finish(_ v: T?, _ e: Error?) {
        lock.lock(); value = v; error = e; done = true; lock.unlock()
    }
    func isDone() -> Bool { lock.lock(); defer { lock.unlock() }; return done }
}

func waitFor<T>(_ p: Pending<T>, seconds: Double) -> Bool {
    let end = Date(timeIntervalSinceNow: seconds)
    while !p.isDone() && Date() < end { sleepMs(20) }
    return p.isDone()
}

struct OCRItem {
    let text: String
    let rect: CGRect   // window-relative points
    let conf: Float
}

extension Driver {
    func screenCaptureAllowed() -> Bool { return CGPreflightScreenCaptureAccess() }

    /// Screenshot of WeChat's largest on-screen normal window, or (popup) of
    /// its largest floating window, e.g. the search results list.
    func captureWindow(popup: Bool = false) throws -> (CGImage, CGSize, CGFloat) {
        let (img, frame, scale) = try captureFrame(popup: popup)
        return (img, frame.size, scale)
    }

    func captureFrame(popup: Bool) throws -> (CGImage, CGRect, CGFloat) {
        guard screenCaptureAllowed() else {
            throw OpError("no_screen_capture", "WeChatSendHelper has no Screen Recording permission")
        }
        let pc = Pending<SCShareableContent>()
        SCShareableContent.getExcludingDesktopWindows(true, onScreenWindowsOnly: true) { c, e in
            pc.finish(c, e)
        }
        guard waitFor(pc, seconds: 3), let content = pc.value else {
            throw OpError("capture_failed", "could not list windows: \(pc.error.map { "\($0)" } ?? "timeout")")
        }
        let wins = content.windows.filter {
            $0.owningApplication?.processID == pid && $0.frame.width > 100 && $0.frame.height > 60
                && (popup ? $0.windowLayer > 0 : ($0.windowLayer == 0 && $0.frame.width > 300))
        }
        guard let win = wins.max(by: { $0.frame.width * $0.frame.height < $1.frame.width * $1.frame.height }) else {
            if popup { throw OpError("no_popup", "WeChat's search results are not showing") }
            throw OpError("no_window", "WeChat main window is not on screen")
        }
        let filter = SCContentFilter(desktopIndependentWindow: win)
        let scale = CGFloat(filter.pointPixelScale)
        let cfg = SCStreamConfiguration()
        cfg.width = Int(win.frame.width * scale)
        cfg.height = Int(win.frame.height * scale)
        cfg.showsCursor = false
        cfg.ignoreShadowsSingleWindow = true
        let pi = Pending<CGImage>()
        SCScreenshotManager.captureImage(contentFilter: filter, configuration: cfg) { img, e in
            pi.finish(img, e)
        }
        guard waitFor(pi, seconds: 4), let img = pi.value else {
            throw OpError("capture_failed", "screenshot failed: \(pi.error.map { "\($0)" } ?? "timeout")")
        }
        return (img, win.frame, CGFloat(img.width) / win.frame.width)
    }

    /// OCR of `rect` (window points; nil = whole window).
    func ocr(_ rect: CGRect?, from shot: (CGImage, CGSize, CGFloat)? = nil,
             popup: Bool = false) throws -> (items: [OCRItem], size: CGSize) {
        let (img, size, scale) = try shot ?? captureWindow(popup: popup)
        var region = CGRect(origin: .zero, size: size)
        if let r = rect { region = r.intersection(region) }
        guard region.width >= 4, region.height >= 4 else { return ([], size) }
        let px = CGRect(x: region.minX * scale, y: region.minY * scale,
                        width: region.width * scale, height: region.height * scale).integral
        guard let crop = img.cropping(to: px) else { return ([], size) }
        let req = VNRecognizeTextRequest()
        req.recognitionLevel = .accurate
        req.usesLanguageCorrection = false
        req.recognitionLanguages = ["zh-Hans", "zh-Hant", "en-US"]
        try VNImageRequestHandler(cgImage: crop, options: [:]).perform([req])
        var items: [OCRItem] = []
        for o in req.results ?? [] {
            guard let c = o.topCandidates(1).first else { continue }
            let b = o.boundingBox  // normalized, origin bottom-left, relative to crop
            let r = CGRect(x: region.minX + b.minX * region.width,
                           y: region.minY + (1 - b.maxY) * region.height,
                           width: b.width * region.width, height: b.height * region.height)
            items.append(OCRItem(text: c.string, rect: r, conf: c.confidence))
        }
        return (items, size)
    }

    /// Items joined into lines (top to bottom, left to right). A lone caret
    /// read as "|" / "I" / "l" at a line end is dropped.
    func joined(_ items: [OCRItem]) -> String {
        let sorted = items.sorted { $0.rect.midY < $1.rect.midY }
        var lines: [[OCRItem]] = []
        for it in sorted {
            if let last = lines.last?.first, abs(last.rect.midY - it.rect.midY) < max(last.rect.height, it.rect.height) * 0.5 {
                lines[lines.count - 1].append(it)
            } else {
                lines.append([it])
            }
        }
        let caret = CharacterSet(charactersIn: "|Il丨")
        return lines.map { line -> String in
            var t = line.sorted { $0.rect.minX < $1.rect.minX }.map { $0.text }.joined(separator: " ")
            while let last = t.unicodeScalars.last, caret.contains(last),
                  t.count > 1 || line.count == 1 && line[0].rect.width < 6 {
                t = String(t.dropLast()).trimmingCharacters(in: .whitespaces)
                if t.isEmpty { break }
            }
            return t
        }.filter { line in
            // A line that is only the blinking text cursor is not text.
            !line.isEmpty && !line.unicodeScalars.allSatisfy { caret.contains($0) || $0 == " " }
        }.joined(separator: "\n")
    }

    func rectArg(_ req: JSON, _ key: String) throws -> CGRect {
        guard let a = req[key] as? [NSNumber], a.count == 4 else {
            throw OpError("bad_request", "\(key) must be [x, y, w, h]")
        }
        return CGRect(x: a[0].doubleValue, y: a[1].doubleValue, width: a[2].doubleValue, height: a[3].doubleValue)
    }

    func pointArg(_ req: JSON) throws -> CGPoint {
        guard let x = (req["x"] as? NSNumber)?.doubleValue, let y = (req["y"] as? NSNumber)?.doubleValue else {
            throw OpError("bad_request", "x and y required")
        }
        return CGPoint(x: x, y: y)
    }

    func windowOrigin() throws -> CGRect {
        let win = try requireWindow()
        guard let f = axFrame(win) else { throw OpError("no_window", "WeChat window has no frame") }
        return f
    }

    /// Click a window-relative point that must lie in the lower half.
    func clickLower(_ p: CGPoint) throws {
        let wf = try windowOrigin()
        guard p.x > 0, p.x < wf.width, p.y > wf.height * 0.5, p.y < wf.height else {
            throw OpError("bad_request", "point must be inside the lower half of the window")
        }
        click(CGRect(x: wf.minX + p.x - 1, y: wf.minY + p.y - 1, width: 2, height: 2))
        sleepMs(150)
    }

    // --- ops ---

    func vOCR(_ req: JSON) throws -> JSON {
        try requireReady()
        try checkUserActivity()  // e.g. Esc closed the search results mid-send
        let rect = req["rect"] == nil ? nil : try rectArg(req, "rect")
        let (items, size) = try ocr(rect, popup: (req["popup"] as? Bool) ?? false)
        let maxItems = min((req["max_items"] as? Int) ?? 200, 1000)
        return ["window": [Int(size.width), Int(size.height)],
                "text": joined(items),
                "items": items.prefix(maxItems).map { i -> JSON in
                    ["text": i.text, "x": Int(i.rect.minX), "y": Int(i.rect.minY),
                     "w": Int(i.rect.width.rounded(.up)), "h": Int(i.rect.height.rounded(.up)),
                     "conf": Double(i.conf)]
                }]
    }

    func vOpenSearch(_ req: JSON) throws -> JSON {
        let query = try str(req, "query")
        guard !query.isEmpty, query.count <= 200, !query.contains("\n") else {
            throw OpError("bad_request", "bad query")
        }
        try requireFront()
        searchArmed = false
        key(.f, cmd: true)
        sleepMs(350)
        key(.a, cmd: true)
        withClipboard(query) {
            key(.v, cmd: true)
            sleepMs(300)
        }
        searchArmed = true
        return [:]
    }

    func vSearchEnter(_ req: JSON) throws -> JSON {
        let query = try str(req, "query")
        let rect = try rectArg(req, "rect")
        guard searchArmed else { throw OpError("no_search", "call v_open_search first") }
        searchArmed = false
        try requireFront()
        let (items, size) = try ocr(rect)
        guard rect.maxY <= size.height * 0.15 else {
            throw OpError("bad_request", "search rect must be in the top 15% of the window")
        }
        let seen = joined(items).replacingOccurrences(of: " ", with: "")
        guard seen.contains(query.replacingOccurrences(of: " ", with: "")) else {
            throw OpError("precondition_failed", "the query is not visible in the search box")
        }
        key(.returnKey)
        sleepMs(500)
        return [:]
    }

    /// Right-click a message bubble to open its context menu. The point must
    /// be in the message area: below the title band, above the input box.
    func vRightClick(_ req: JSON) throws -> JSON {
        let p = try pointArg(req)
        try requireFront()
        searchArmed = false
        menuArmed = false
        let wf = try windowOrigin()
        guard p.x > wf.width * 0.15, p.x < wf.width - 4, p.y > 70, p.y < wf.height * 0.8 else {
            throw OpError("bad_request", "point must be inside the message area")
        }
        click(CGRect(x: wf.minX + p.x - 1, y: wf.minY + p.y - 1, width: 2, height: 2), right: true)
        sleepMs(400)
        menuArmed = true
        return [:]
    }

    /// Click a point (popup-relative) inside WeChat's current popup: the
    /// search results (after v_open_search) or a context menu (after
    /// v_right_click). For a context menu the caller must pass `expect`, and
    /// the OCR text at that point must be exactly it -- so e.g. "Recall" or
    /// "Delete" can never be clicked by mistake.
    func vClickPopup(_ req: JSON) throws -> JSON {
        let p = try pointArg(req)
        guard searchArmed || menuArmed else { throw OpError("no_search", "call v_open_search first") }
        let isMenu = menuArmed
        searchArmed = false
        menuArmed = false
        if isMenu {
            let expect = try str(req, "expect")
            let allowed: Set<String> = ["Quote", "引用"]
            guard allowed.contains(expect) else {
                throw OpError("bad_request", "only the Quote menu item may be clicked")
            }
            let hit = try ocr(nil, popup: true).items.first { $0.rect.insetBy(dx: -4, dy: -4).contains(p) }
            guard let h = hit, h.text.trimmingCharacters(in: .whitespaces) == expect else {
                key(.escape)
                throw OpError("precondition_failed", "the menu item at that point is not \(expect)")
            }
        }
        try requireFront()
        let (_, frame, _) = try captureFrame(popup: true)
        guard p.x > 0, p.y > 0, p.x < frame.width, p.y < frame.height else {
            throw OpError("bad_request", "point is outside the search results")
        }
        click(CGRect(x: frame.minX + p.x - 1, y: frame.minY + p.y - 1, width: 2, height: 2))
        sleepMs(500)
        return [:]
    }

    func vPaste(_ req: JSON) throws -> JSON {
        let text = try str(req, "text")
        guard !text.isEmpty, text.count <= 20000 else { throw OpError("bad_request", "bad text") }
        try requireFront()
        searchArmed = false
        try clickLower(try pointArg(req))
        withClipboard(text) {
            key(.v, cmd: true)
            sleepMs(400)
        }
        return [:]
    }

    /// Plain click in the lower half of the window (e.g. the close button of
    /// a quote attached to the input box).
    func vClick(_ req: JSON) throws -> JSON {
        try requireFront()
        searchArmed = false
        menuArmed = false
        try clickLower(try pointArg(req))
        return [:]
    }

    func vClear(_ req: JSON) throws -> JSON {
        searchArmed = false
        if let expected = req["expected_input"] as? String {
            // Undo our own paste after the user interrupted: skip the activity
            // check, but only if WeChat is still in front and the input box
            // still holds exactly the text we pasted (nothing of the user's).
            try requireReady()
            guard frontmost()?.processIdentifier == pid else {
                throw OpError("not_frontmost", "WeChat is not the frontmost app")
            }
            let rect = try rectArg(req, "input_rect")
            let winHeight = try windowOrigin().height
            let seen = joined(try ocr(rect).items)
            guard rect.minY >= winHeight * 0.5, !expected.isEmpty, seen == expected else {
                throw OpError("precondition_failed", "input box no longer holds only our text")
            }
        } else {
            try requireFront()
        }
        try clickLower(try pointArg(req))
        key(.a, cmd: true)
        key(.delete)
        sleepMs(150)
        return [:]
    }

    func vSend(_ req: JSON) throws -> JSON {
        let expectedTitle = try str(req, "expected_title")
        let expectedInput = try str(req, "expected_input")
        let titleRect = try rectArg(req, "title_rect")
        let inputRect = try rectArg(req, "input_rect")
        let keyName = (req["key"] as? String) ?? "enter"
        guard keyName == "enter" || keyName == "cmd_enter" else {
            throw OpError("bad_request", "key must be enter or cmd_enter")
        }
        guard !expectedTitle.isEmpty, !expectedInput.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            throw OpError("bad_request", "empty expected_title / expected_input")
        }
        searchArmed = false
        try requireFront()
        let shot = try captureWindow()  // one frame for both checks
        let size = shot.1
        guard titleRect.maxY <= size.height * 0.15, inputRect.minY >= size.height * 0.5 else {
            throw OpError("bad_request", "title rect must be in the top 15%, input rect in the lower half")
        }
        guard joined(try ocr(titleRect, from: shot).items) == expectedTitle else {
            throw OpError("precondition_failed", "chat title is not the approved one")
        }
        guard joined(try ocr(inputRect, from: shot).items) == expectedInput else {
            throw OpError("precondition_failed", "input box does not hold the approved text")
        }
        guard frontmost()?.processIdentifier == pid else {
            throw OpError("not_frontmost", "WeChat lost focus")
        }
        try checkUserActivity()
        key(.returnKey, cmd: keyName == "cmd_enter")
        var leftover = expectedInput
        for _ in 0..<10 {
            sleepMs(150)
            leftover = joined(try ocr(inputRect).items)
            if leftover.isEmpty { break }
        }
        return ["pressed": true, "leftover": leftover]
    }
}

// MARK: - main

let socketPath = (NSHomeDirectory() as NSString)
    .appendingPathComponent("Library/Application Support/wechat-decrypt-export/sendhelper.sock")

signal(SIGPIPE, SIG_IGN)
let application = NSApplication.shared
application.setActivationPolicy(.accessory)

if !AXIsProcessTrusted() {
    // Adds the helper to System Settings > Accessibility (unchecked) and shows
    // the system prompt once, so the user only has to flip the switch.
    let opts = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
    _ = AXIsProcessTrustedWithOptions(opts)
}

if !CGPreflightScreenCaptureAccess() {
    // Adds the helper to System Settings > Screen Recording (vision mode).
    _ = CGRequestScreenCaptureAccess()
}

let server = Server(path: socketPath)
Thread.detachNewThread { server.run() }
application.run()
