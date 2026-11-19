import Foundation
import AVFoundation
import Darwin

@main enum MicrophoneCapture {
static func main() throws {
// Only this explicit setup command may ask for permission; it does not record.
if CommandLine.arguments.dropFirst().first == "--authorize" {
    switch AVCaptureDevice.authorizationStatus(for: .audio) {
    case .notDetermined:
        AVCaptureDevice.requestAccess(for: .audio) { allowed in
            fputs(allowed ? "Microphone authorized\n" : "Microphone denied\n", stderr)
            exit(allowed ? 0 : 3)
        }
        RunLoop.main.run()
        exit(3)
    case .authorized:
        fputs("Microphone authorized\n", stderr)
        exit(0)
    default:
        fputs("Allow microphone access in System Settings → Privacy & Security\n", stderr)
        exit(3)
    }
}

// Normal capture never prompts for permission.
guard AVCaptureDevice.authorizationStatus(for: .audio) == .authorized else {
    fputs("Microphone authorization is not already granted; refusing to prompt.\n", stderr)
    exit(3)
}
let seconds = CommandLine.arguments.count > 1 ? Double(CommandLine.arguments[1]) ?? 6 : 6
let engine = AVAudioEngine()
let input = engine.inputNode
let inputFormat = input.outputFormat(forBus: 0)
let rate = inputFormat.sampleRate.rounded()
let chunk = Int(rate / 50)
// The tap can deliver several input periods in one callback.
let lead = 1.15
func hostSeconds() -> Double {
    AVAudioTime.seconds(forHostTime: mach_absolute_time())
}
guard (8_000...192_000).contains(rate), inputFormat.channelCount > 0,
      inputFormat.commonFormat == .pcmFormatFloat32, !inputFormat.isInterleaved else {
    fputs("No active microphone format.\n", stderr)
    exit(4)
}
let timeline = PCMTimeline(capacity: Int(rate * 2))
engine.prepare()
let epoch = hostSeconds()
fputs("input_rate=\(inputFormat.sampleRate) channels=\(inputFormat.channelCount) format=\(inputFormat.commonFormat.rawValue)\n", stderr)
input.installTap(onBus: 0, bufferSize: 1024, format: inputFormat) { buffer, at in
    guard at.isHostTimeValid, let samples = buffer.floatChannelData?[0] else { return }
    let index = Int64(((AVAudioTime.seconds(forHostTime: at.hostTime) - epoch) * rate).rounded())
    timeline.push(UnsafeBufferPointer(start: samples, count: Int(buffer.frameLength)), at: index)
}
try engine.start()
// Keep the existing pipe header for native-rate negotiation.
try FileHandle.standardOutput.write(contentsOf: Data("\(Int(rate))\n".utf8))
let stopAt = hostSeconds() + seconds
let diagnostics = ProcessInfo.processInfo.environment["SENTRY_MIC_DIAGNOSTICS"] == "1"
var block = [Float](repeating: 0, count: chunk)
var frames: Int64 = 0
var missing: Int64 = 0
var gap = 0
let gapThreshold = Int(rate / 2)
var hasInput = false
while hostSeconds() < stopAt {
    let due = epoch + Double(frames + Int64(chunk)) / rate + lead
    let wait = min(due, stopAt) - hostSeconds()
    if wait > 0 {
        Thread.sleep(forTimeInterval: wait)
        continue
    }
    let absent = block.withUnsafeMutableBufferPointer { timeline.take($0) }
    hasInput = hasInput || absent < chunk
    gap = absent == chunk ? gap + chunk : 0
    if gap >= gapThreshold && gap - chunk < gapThreshold { fputs("microphone_gap_sample=\(frames - Int64(gap - chunk))\n", stderr) }
    if !hasInput && frames >= Int64(rate * 3) {
        fputs("Microphone did not deliver samples.\n", stderr)
        break
    }
    try block.withUnsafeBytes { try FileHandle.standardOutput.write(contentsOf: $0) }
    missing += Int64(absent)
    frames += Int64(chunk)
    if diagnostics && (frames / Int64(rate * 3)) != ((frames - Int64(chunk)) / Int64(rate * 3)) {
        let (late, overflow) = timeline.dropped()
        fputs("pcm_clock frames=\(frames) missing=\(missing) late=\(late) overflow=\(overflow)\n", stderr)
    }
}
engine.stop()
input.removeTap(onBus: 0)
fputs("pcm_frames=\(frames) missing_frames=\(missing)\n", stderr)
if !hasInput { exit(4) }
}
}
