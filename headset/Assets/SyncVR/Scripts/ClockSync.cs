using System.Collections.Generic;
using System.Diagnostics;

namespace SyncVR
{
    /// <summary>Monotonic local clock in seconds.</summary>
    public static class LocalClock
    {
        private static readonly double TicksToSeconds = 1.0 / Stopwatch.Frequency;

        public static double Now
        {
            get { return Stopwatch.GetTimestamp() * TicksToSeconds; }
        }
    }

    /// <summary>
    /// NTP-style estimate of the offset between the local clock and the
    /// server clock. Port of ClockSync in server/syncvr/sync_engine.py.
    /// Each ping records local send time t0, server time ts and local receive
    /// time t1; the sample with the smallest round trip is the least disturbed
    /// by queuing delay, so its offset ts - (t0 + t1) / 2 is used.
    /// Thread-safe: samples are added on the network thread.
    /// </summary>
    public sealed class ClockSync
    {
        private const int Window = 20;
        private readonly Queue<double[]> samples = new Queue<double[]>();
        private readonly object gate = new object();
        private double offset;
        private double rtt;
        private bool synced;

        public bool Synced { get { lock (gate) return synced; } }
        public double Offset { get { lock (gate) return offset; } }
        public double Rtt { get { lock (gate) return rtt; } }
        public int SampleCount { get { lock (gate) return samples.Count; } }

        public void Add(double t0, double ts, double t1)
        {
            double sampleRtt = t1 - t0;
            if (sampleRtt < 0) return;
            lock (gate)
            {
                samples.Enqueue(new[] { sampleRtt, ts - (t0 + t1) / 2.0 });
                while (samples.Count > Window) samples.Dequeue();
                double bestRtt = double.MaxValue;
                foreach (var s in samples)
                {
                    if (s[0] < bestRtt)
                    {
                        bestRtt = s[0];
                        offset = s[1];
                    }
                }
                rtt = bestRtt;
                synced = true;
            }
        }

        public void Reset()
        {
            lock (gate)
            {
                samples.Clear();
                synced = false;
                offset = rtt = 0;
            }
        }

        public double ServerTime(double local)
        {
            lock (gate) return local + offset;
        }
    }
}
