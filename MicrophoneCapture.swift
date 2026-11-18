import Foundation
import AVFoundation
import Darwin

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

guard AVCaptureDevice.authorizationStatus(for: .audio) == .authorized else {
    fputs("Microphone authorization is not already granted; refusing to prompt.\n", stderr)
    exit(3)
}
let seconds = CommandLine.arguments.count > 1 ? Double(CommandLine.arguments[1]) ?? 6 : 6
let engine = AVAudioEngine()
let input = engine.inputNode
let inputFormat = input.outputFormat(forBus: 0)
guard (8000...192000).contains(inputFormat.sampleRate), inputFormat.channelCount > 0,
      inputFormat.commonFormat == .pcmFormatFloat32, !inputFormat.isInterleaved else {
    fputs("No active microphone format\n", stderr)
    exit(4)
}
let writeQueue = DispatchQueue(label: "local.microphone.pcm")
let lock = NSLock()
var totalFrames: Int64 = 0
var totalBuffers: Int64 = 0
fputs("input_rate=\(inputFormat.sampleRate) channels=\(inputFormat.channelCount) format=\(inputFormat.commonFormat.rawValue)\n", stderr)
FileHandle.standardOutput.write(Data("\(Int(inputFormat.sampleRate.rounded()))\n".utf8))
input.installTap(onBus: 0, bufferSize: 1024, format: inputFormat) { buffer, _ in
    guard let samples = buffer.floatChannelData?[0] else { return }
    let bytes = Data(bytes: samples, count: Int(buffer.frameLength) * MemoryLayout<Float>.size)
    lock.lock()
    totalFrames += Int64(buffer.frameLength)
    totalBuffers += 1
    lock.unlock()
    writeQueue.async {
        FileHandle.standardOutput.write(bytes)
    }
}
try engine.start()
let start = Date()
while Date().timeIntervalSince(start) < seconds {
    RunLoop.current.run(until: Date().addingTimeInterval(0.1))
}
engine.stop()
input.removeTap(onBus: 0)
writeQueue.sync {}
lock.lock()
let frames = totalFrames
let buffers = totalBuffers
lock.unlock()
fputs("frames=\(frames) buffers=\(buffers) pcm_seconds=\(Double(frames)/inputFormat.sampleRate) elapsed=\(Date().timeIntervalSince(start))\n", stderr)
