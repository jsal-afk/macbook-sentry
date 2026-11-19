import Foundation

/// Fixed storage keyed by the presentation sample index, not callback arrival order.
final class PCMTimeline {
    private let lock = NSLock()
    private var values: [Float]
    private var stamps: [Int64]
    private var next: Int64 = 0
    private var late = 0
    private var overflow = 0

    init(capacity: Int) {
        precondition(capacity > 0)
        values = [Float](repeating: 0, count: capacity)
        stamps = [Int64](repeating: -1, count: capacity)
    }

    func push(_ input: UnsafeBufferPointer<Float>, at start: Int64) {
        lock.lock()
        defer { lock.unlock() }
        let first = max(start, next)
        let end = min(start + Int64(input.count), next + Int64(values.count))
        late += Int(max(0, min(next, start + Int64(input.count)) - max(0, start)))
        overflow += Int(max(0, start + Int64(input.count) - max(start, next + Int64(values.count))))
        guard first < end else { return }
        for index in first..<end {
            let slot = Int(index % Int64(values.count))
            if stamps[slot] != index { // Keep the first sample if source timestamps overlap.
                stamps[slot] = index
                values[slot] = input[Int(index - start)]
            }
        }
    }

    func dropped() -> (Int, Int) {
        lock.lock()
        defer { lock.unlock() }
        return (late, overflow)
    }

    @discardableResult
    func take(_ output: UnsafeMutableBufferPointer<Float>) -> Int {
        precondition(output.count <= values.count)
        lock.lock()
        defer { lock.unlock() }
        var missing = 0
        for i in output.indices {
            let index = next + Int64(i)
            let slot = Int(index % Int64(values.count))
            if stamps[slot] == index {
                output[i] = values[slot]
            } else {
                output[i] = 0
                missing += 1
            }
        }
        next += Int64(output.count)
        return missing
    }
}
