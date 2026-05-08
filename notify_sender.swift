// NotifySender — .app bundle 컨텍스트에서 macOS 알림을 송출하는 helper.
//
// Python 측의 rumps/AppKit 호출은 macOS 14+ 에서 process executable 이름이
// 그대로 알림 표시자(예: "python")로 노출되는 문제가 있다. 이 helper 는
// AutoMeetingNote.app/Contents/MacOS/ 에 위치한 정식 .app bundle executable
// 이므로 알림 송출 시 표시자가 "AutoMeetingNote" 로 잡힌다.
//
// 사용:
//   AutoMeetingNoteNotifySender --title=<title> [--subtitle=<sub>] --message=<msg>
//
// 1순위: UserNotifications 프레임워크(UNUserNotificationCenter) — macOS 26
//        포함 최신 권장 API. 권한 미허용 시 NSUserNotification 으로 폴백.
// 2순위: NSUserNotification — deprecated 이지만 .app bundle 컨텍스트에서는
//        macOS 26 에서도 송출자 이름이 정상적으로 잡힌다.

import AppKit
import Foundation
import UserNotifications

func parseArgs() -> (title: String, subtitle: String, message: String) {
    var title = "AutoMeetingNote"
    var subtitle = ""
    var message = ""
    for arg in CommandLine.arguments.dropFirst() {
        if let r = arg.range(of: "--title=") {
            title = String(arg[r.upperBound...])
        } else if let r = arg.range(of: "--subtitle=") {
            subtitle = String(arg[r.upperBound...])
        } else if let r = arg.range(of: "--message=") {
            message = String(arg[r.upperBound...])
        }
    }
    if message.isEmpty {
        message = title
    }
    return (title, subtitle, message)
}

func sendViaLegacy(title: String, subtitle: String, message: String) {
    let n = NSUserNotification()
    n.title = title
    if !subtitle.isEmpty { n.subtitle = subtitle }
    n.informativeText = message
    n.soundName = NSUserNotificationDefaultSoundName
    NSUserNotificationCenter.default.deliver(n)
    // deliver 는 비동기 큐잉이라 즉시 종료 시 누락 가능 — 짧게 대기
    Thread.sleep(forTimeInterval: 0.4)
}

func sendViaUserNotifications(
    title: String, subtitle: String, message: String, completion: @escaping () -> Void
) {
    let center = UNUserNotificationCenter.current()
    center.requestAuthorization(options: [.alert, .sound]) { granted, _ in
        if !granted {
            sendViaLegacy(title: title, subtitle: subtitle, message: message)
            completion()
            return
        }
        let content = UNMutableNotificationContent()
        content.title = title
        if !subtitle.isEmpty { content.subtitle = subtitle }
        content.body = message
        content.sound = UNNotificationSound.default
        let request = UNNotificationRequest(
            identifier: UUID().uuidString, content: content, trigger: nil
        )
        center.add(request) { error in
            if error != nil {
                sendViaLegacy(title: title, subtitle: subtitle, message: message)
            }
            completion()
        }
    }
}

let (title, subtitle, message) = parseArgs()

let semaphore = DispatchSemaphore(value: 0)
sendViaUserNotifications(title: title, subtitle: subtitle, message: message) {
    semaphore.signal()
}
// 최대 3초 대기 후 종료 (UNUserNotificationCenter 권한 다이얼로그 비동기 처리 여유)
_ = semaphore.wait(timeout: .now() + 3.0)
exit(0)
