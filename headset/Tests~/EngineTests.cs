// Offline tests for the headset sync engine (no Unity needed). Unity ignores
// folders ending in "~", so this never ends up in the app. Run with:
//
//   mcs -out:/tmp/enginetests.exe Tests~/EngineTests.cs \
//       Assets/SyncVR/Scripts/SyncEngine.cs Assets/SyncVR/Scripts/Messages.cs \
//       Assets/SyncVR/Scripts/ClockSync.cs && mono /tmp/enginetests.exe
//
// The same scenarios run against the Python reference engine in
// server/tests/test_sync_engine.py.
using System;
using System.Collections.Generic;
using SyncVR;

namespace SyncVR.Tests
{
    /// <summary>A decoder on a virtual clock with start/seek/load delays and a crystal error.</summary>
    internal sealed class FakePlayer : IVideoPlayer
    {
        public double Now;
        public double StartLatency = 0.08;
        public double SeekDuration = 0.2;
        public double LoadTime = 0.3;
        public double RateError;          // e.g. 0.003 = runs 0.3% fast
        public bool FreezesWhenRateChanged;
        public bool FileMissing;
        public double VideoLength = 600;

        private double pos, posAt, rate = 1;
        private bool playing, prepared, looping;
        private double startAt = -1, seekDoneAt = -1, seekTarget, loadDoneAt = -1;
        public int Seeks;
        public string LoadedVideo { get; private set; }
        public string Error { get; private set; }

        public bool IsPrepared { get { return prepared; } }
        public bool IsSeeking { get { return seekDoneAt >= 0; } }
        public bool IsPlaying { get { return playing; } }
        public double Length { get { return prepared ? VideoLength : 0; } }
        public bool CanSetRate { get { return true; } }
        public double Time { get { Advance(); return pos; } }
        public double TruePosition { get { Advance(); return pos; } }

        private void Advance()
        {
            if (playing && seekDoneAt < 0 && prepared)
            {
                double speed = FreezesWhenRateChanged && Math.Abs(rate - 1) > 1e-9 ? 0 : rate * (1 + RateError);
                pos += (Now - posAt) * speed;
                if (pos >= VideoLength)
                {
                    if (looping) pos %= VideoLength;
                    else { pos = VideoLength; playing = false; }
                }
            }
            posAt = Now;
        }

        public void Tick(double now)
        {
            Now = now;
            Advance();
            if (loadDoneAt >= 0 && now >= loadDoneAt) { prepared = true; loadDoneAt = -1; }
            if (seekDoneAt >= 0 && now >= seekDoneAt) { pos = seekTarget; seekDoneAt = -1; }
            if (startAt >= 0 && now >= startAt) { playing = true; startAt = -1; }
        }

        public void Load(VideoCommand cmd)
        {
            Stop();
            if (FileMissing) { Error = "video not on this headset"; return; }
            LoadedVideo = cmd.Video;
            loadDoneAt = Now + LoadTime;
        }

        public void Play() { Advance(); if (prepared) startAt = Now + StartLatency; }
        public void Pause() { Advance(); playing = false; startAt = -1; }
        public void Stop() { prepared = false; playing = false; startAt = seekDoneAt = loadDoneAt = -1; pos = 0; LoadedVideo = null; Error = null; }
        public void Seek(double t) { Advance(); Seeks++; seekTarget = t; seekDoneAt = Now + SeekDuration; }
        public void SetRate(double r) { Advance(); rate = r; }
        public void SetLooping(bool loop) { looping = loop; }
        public void SetExternalTime(double t) { }
        public void ClearExternalTime() { }
    }

    internal static class Program
    {
        private const double Frame = 1.0 / 72.0;
        private static int failures;

        private static VideoCommand Cmd(double pos, double at, bool loop = false, double duration = 600)
        {
            return new VideoCommand { Video = "v.mp4", Pos = pos, At = at, Loop = loop, Duration = duration };
        }

        private static double Run(SyncEngine engine, FakePlayer player, double from, double to)
        {
            double t = from;
            for (; t < to; t += Frame)
            {
                player.Tick(t);
                engine.Update(t);
            }
            player.Tick(t);
            return t;
        }

        private static double Error(SyncEngine engine, FakePlayer player, double now)
        {
            return player.TruePosition - engine.ExpectedPosition(now).Value;
        }

        private static void Check(string name, bool ok, string detail)
        {
            Console.WriteLine((ok ? "PASS " : "FAIL ") + name + " (" + detail + ")");
            if (!ok) failures++;
        }

        private static void ScheduledStart()
        {
            var p = new FakePlayer();
            var e = new SyncEngine(p, null);
            e.OnPlay(Cmd(10, 1.5));
            double t = Run(e, p, 0, 8);
            double err = Error(e, p, t);
            // Residual drift below the deadband (20 ms) is deliberately left alone.
            Check("scheduled start converges", e.State == "playing" && Math.Abs(err) < 0.02, "error " + (err * 1000).ToString("0.0") + " ms");
            Check("start latency learned", Math.Abs(e.StartLatency - 0.08) < 0.03, "estimate " + (e.StartLatency * 1000).ToString("0") + " ms");
        }

        private static void LateJoin()
        {
            var p = new FakePlayer { SeekDuration = 0.4 };
            var e = new SyncEngine(p, null);
            e.OnPlay(Cmd(0, -95));
            double t = Run(e, p, 0, 8);
            double err = Error(e, p, t);
            Check("late join converges", e.State == "playing" && Math.Abs(err) < 0.01, "error " + (err * 1000).ToString("0.0") + " ms, pos " + p.TruePosition.ToString("0.0"));
            Check("seek time measured", e.SeekTime >= 0.39, "estimate " + (e.SeekTime * 1000).ToString("0") + " ms");
        }

        private static void ClockErrorCorrectedByRate()
        {
            var p = new FakePlayer { RateError = 0.004 };
            var e = new SyncEngine(p, null);
            e.OnPlay(Cmd(0, 1.5));
            double t = Run(e, p, 0, 120);
            double err = Error(e, p, t);
            Check("0.4% fast decoder held in sync by rate", Math.Abs(err) < 0.025 && p.Seeks <= 2, "error " + (err * 1000).ToString("0.0") + " ms, seeks " + p.Seeks);
        }

        private static void SeekModeCorrects()
        {
            var p = new FakePlayer { RateError = 0.004 };
            var e = new SyncEngine(p, null);
            e.Settings.correction_mode = "seek";
            e.OnPlay(Cmd(0, 1.5));
            double t = Run(e, p, 0, 120);
            double err = Error(e, p, t);
            Check("seek mode keeps drift under threshold", Math.Abs(err) < 0.09 && Math.Abs(e.Rate - 1) < 1e-9, "error " + (err * 1000).ToString("0.0") + " ms, seeks " + p.Seeks);
        }

        private static void PauseAndResume()
        {
            var p = new FakePlayer();
            var e = new SyncEngine(p, null);
            e.OnPlay(Cmd(0, 1.5));
            double t = Run(e, p, 0, 6);
            e.OnPause(Cmd(7.25, t + 0.3));
            t = Run(e, p, t, t + 2);
            Check("pause lands on exact frame", e.State == "paused" && Math.Abs(p.TruePosition - 7.25) < 1e-9, "pos " + p.TruePosition.ToString("0.000"));
            e.OnPlay(Cmd(7.25, t + 1.5));
            t = Run(e, p, t, t + 6);
            double err = Error(e, p, t);
            Check("resume after pause in sync", e.State == "playing" && Math.Abs(err) < 0.01, "error " + (err * 1000).ToString("0.0") + " ms");
        }

        private static void LoopWraps()
        {
            var p = new FakePlayer { VideoLength = 10, RateError = 0.002 };
            var e = new SyncEngine(p, null);
            e.OnPlay(Cmd(0, 1.5, loop: true, duration: 10));
            double t = Run(e, p, 0, 34.3);
            double raw = Error(e, p, t);
            double err = ((raw + 5) % 10 + 10) % 10 - 5;
            Check("looping video stays in sync across the loop point", e.State == "playing" && Math.Abs(err) < 0.03, "error " + (err * 1000).ToString("0.0") + " ms");
        }

        private static void EndsWithoutLoop()
        {
            var p = new FakePlayer { VideoLength = 5 };
            var e = new SyncEngine(p, null);
            e.OnPlay(Cmd(0, 1.5, duration: 5));
            Run(e, p, 0, 9);
            Check("non-looping video reports ended", e.State == "ended", "state " + e.State);
        }

        private static void StallFallsBackToSeek()
        {
            var p = new FakePlayer { RateError = 0.004, FreezesWhenRateChanged = true };
            var events = new List<string>();
            var e = new SyncEngine(p, (level, msg) => events.Add(msg));
            e.OnPlay(Cmd(0, 1.5));
            double t = Run(e, p, 0, 60);
            double err = Error(e, p, t);
            Check("player that freezes on speed change falls back to seeks", e.ForcedMode == "seek" && events.Count == 1 && Math.Abs(err) < 0.1,
                  "mode " + e.ForcedMode + ", error " + (err * 1000).ToString("0.0") + " ms");
        }

        private static void MissingFile()
        {
            var p = new FakePlayer { FileMissing = true };
            var e = new SyncEngine(p, null);
            e.OnPlay(Cmd(0, 1.5));
            Run(e, p, 0, 1);
            Check("missing video reports error", e.State == "error", "state " + e.State);
            p.FileMissing = false;
            e.OnPlay(Cmd(0, 3));
            double t = Run(e, p, 1, 8);
            Check("retry after content arrives plays", e.State == "playing" && Math.Abs(Error(e, p, t)) < 0.02, "state " + e.State);
        }

        private static void ResyncAfterSleep()
        {
            var p = new FakePlayer();
            var e = new SyncEngine(p, null);
            e.OnPlay(Cmd(0, 1.5));
            double t = Run(e, p, 0, 5);
            p.Pause(); // the OS paused the decoder while the app was suspended
            t = Run(e, p, t, t + 10);
            e.Resync();
            t = Run(e, p, t, t + 5);
            double err = Error(e, p, t);
            Check("resync after suspend", e.State == "playing" && Math.Abs(err) < 0.01, "error " + (err * 1000).ToString("0.0") + " ms");
        }

        private static void JsonNumbersAreInvariant()
        {
            var prev = System.Threading.Thread.CurrentThread.CurrentCulture;
            System.Threading.Thread.CurrentThread.CurrentCulture = new System.Globalization.CultureInfo("de-DE");
            string json = new JsonWriter("status").Field("x", 1.5).Field("s", "a\"b\n").Field("n", (double?)null).ToString();
            System.Threading.Thread.CurrentThread.CurrentCulture = prev;
            Check("JSON writer ignores locale and escapes", json == "{\"type\":\"status\",\"x\":1.5,\"s\":\"a\\\"b\\n\",\"n\":null}", json);
        }

        private static int Main()
        {
            ScheduledStart();
            LateJoin();
            ClockErrorCorrectedByRate();
            SeekModeCorrects();
            PauseAndResume();
            LoopWraps();
            EndsWithoutLoop();
            StallFallsBackToSeek();
            MissingFile();
            ResyncAfterSleep();
            JsonNumbersAreInvariant();
            Console.WriteLine(failures == 0 ? "all passed" : failures + " failed");
            return failures == 0 ? 0 : 1;
        }
    }
}
