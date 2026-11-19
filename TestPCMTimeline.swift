import Foundation

@main struct TestPCMTimeline {
    static func main() {
        let ring = PCMTimeline(capacity: 8)
        func push(_ x: [Float], at index: Int64) {
            x.withUnsafeBufferPointer { ring.push($0, at: index) }
        }
        func take(_ n: Int) -> [Float] {
            var out = [Float](repeating: -1, count: n)
            out.withUnsafeMutableBufferPointer { _ = ring.take($0) }
            return out
        }
        push([0.25, 0.5, 0.75], at: 0)
        precondition(take(3) == [0.25, 0.5, 0.75])
        precondition(take(3) == [0, 0, 0])
        push([0.6, 0.7], at: 4) // Late samples must not play after their gap.
        push([0.8, 0.9], at: 7)
        precondition(take(3) == [0, 0.8, 0.9])
        push([0.1, 0.2], at: 17) // Outside bounded storage; no overwrite at the wrap.
        push([1, 0.5], at: 9)
        precondition(take(8) == [1, 0.5, 0, 0, 0, 0, 0, 0])
        precondition(take(2) == [0, 0])
        let timed = PCMTimeline(capacity: 24_000)
        var output = [Float](repeating: -1, count: 960)
        var missing = 0
        // A 4 s callback blackout inside 17 s of 48 kHz PCM. Never
        // replay samples that arrive after their presentation slot.
        for step in 0..<850 {
            let sample = Float(step + 1) / 1_000
            let index = Int64(step * 960)
            let dropout = (325..<525).contains(step)
            if !dropout {
                let input = [Float](repeating: sample, count: 960)
                input.withUnsafeBufferPointer { timed.push($0, at: index) }
            }
            missing += output.withUnsafeMutableBufferPointer { timed.take($0) }
            precondition(output.allSatisfy { $0 == (dropout ? 0 : sample) })
        }
        precondition(missing == 192_000)
        print("17 s PCM: 816000 samples, exactly 192000 missing positions, all other samples unchanged")
        print("PCM preserved on-time samples, zeroed missing samples and rejected late/overflow samples")
    }
}
