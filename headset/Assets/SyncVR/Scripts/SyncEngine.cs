using System;
using System.Collections.Generic;

namespace SyncVR
{
    /// <summary>What the sync engine needs from a video player.</summary>
    public interface IVideoPlayer
    {
        string LoadedVideo { get; }
        bool IsPrepared { get; }
        bool IsSeeking { get; }
        bool IsPlaying { get; }
        string Error { get; }
        double Time { get; }
        double Length { get; }
        bool CanSetRate { get; }
        void Load(VideoCommand cmd);
        void Play();
        void Pause();
        void Stop();
        void Seek(double seconds);
        void SetRate(double rate);
        void SetLooping(bool loop);
        void SetExternalTime(double seconds);
        void ClearExternalTime();
    }

    /// <summary>
    /// Keeps a video player on the server's schedule. This is a line-by-line
    /// port of server/syncvr/sync_engine.py (the reference implementation the
    /// simulator and tests exercise); keep the two in step.
    ///
    /// The server sends anchors: "video V is at position P at server time T".
    /// Starting and hard re-syncs are done by cueing: pick a moment C far
    /// enough ahead for a seek to finish, pause, seek to where the video must
    /// be at C, and call Play() just before C (early by the learned start-up
    /// latency). While playing, small drift is absorbed by nudging playback
    /// speed and large drift triggers a new cue.
    /// </summary>
    public sealed class SyncEngine
    {
        private const double DriftSmoothing = 0.1;
        private const int LearnSamples = 6;
        private const double LearnGain = 0.7;
        private const double RateRelease = 0.2;
        private const double LateStartTolerance = 0.05;
        // Must stay well below the time drift needs to reach hard_seek_ms, or a
        // re-cue (which resets the speed) would hide the stall.
        private const double StallTimeout = 0.25;

        private readonly IVideoPlayer player;
        private readonly Action<string, string> emitEvent;

        public SyncSettings Settings = new SyncSettings();
        public string State = "idle"; // idle|loading|ready|playing|paused|ended|error
        public VideoCommand Anchor;
        public VideoCommand PauseRequest;
        public VideoCommand VideoMsg;
        public bool PendingStart;
        public double? Drift;
        public double Rate = 1.0;
        public double SeekTime = 0.3;      // learned: how long a seek takes
        public double StartLatency = 0.1;  // learned: delay between Play() and frames moving
        public string ForcedMode;

        private double? cueAt;
        private double? seekStarted;
        private double settleUntil;
        private double lastSeek = double.NegativeInfinity;
        private bool learning;
        private readonly List<double> learnSamples = new List<double>();
        private double? lastProgressPos;
        private double lastProgressT;

        public SyncEngine(IVideoPlayer player, Action<string, string> emitEvent)
        {
            this.player = player;
            this.emitEvent = emitEvent ?? delegate { };
        }

        // ------------------------------------------------------------ commands

        private void EnsureLoaded(VideoCommand msg)
        {
            if (player.LoadedVideo != msg.Video)
            {
                player.Load(msg);
                State = "loading";
                Drift = null;
            }
            VideoMsg = msg;
        }

        public void OnPlay(VideoCommand msg)
        {
            bool same = Anchor != null && Anchor.Video == msg.Video
                        && Math.Abs(Anchor.Pos - msg.Pos) < 1e-3 && Math.Abs(Anchor.At - msg.At) < 1e-3;
            EnsureLoaded(msg);
            Anchor = msg;
            PauseRequest = null;
            if (same && State == "playing") return; // resync of the anchor we already follow
            PendingStart = true;
            cueAt = null;
        }

        public void OnPause(VideoCommand msg)
        {
            EnsureLoaded(msg);
            PauseRequest = msg;
            if (State != "playing")
            {
                Anchor = null;
                PendingStart = false;
            }
        }

        public void OnStop()
        {
            player.Stop();
            Anchor = PauseRequest = VideoMsg = null;
            PendingStart = false;
            seekStarted = null;
            State = "idle";
            SetRate(1.0);
        }

        public void OnSettings(SyncSettings settings)
        {
            if (settings != null) Settings = settings;
            ForcedMode = null;
        }

        /// <summary>Re-cue the current anchor, e.g. after the app was suspended.</summary>
        public void Resync()
        {
            if (Anchor != null && (State == "playing" || State == "ready"))
            {
                PendingStart = true;
                cueAt = null;
            }
        }

        // -------------------------------------------------------------- update

        public double? ExpectedPosition(double now)
        {
            if (Anchor == null) return null;
            double pos = Anchor.Pos + (now - Anchor.At);
            double length = Length();
            if (length > 0 && Anchor.Loop) pos = Mod(pos, length);
            return pos;
        }

        public double Length()
        {
            if (player.Length > 0) return player.Length;
            if (Anchor != null && Anchor.Duration > 0) return Anchor.Duration;
            return 0.0;
        }

        private static double Mod(double a, double b)
        {
            double r = a % b;
            return r < 0 ? r + b : r;
        }

        private void SetRate(double rate)
        {
            if (Math.Abs(rate - Rate) > 1e-4)
            {
                Rate = rate;
                player.SetRate(rate);
            }
        }

        private void Settle(double now, bool learn)
        {
            settleUntil = now + Settings.settle_ms / 1000.0;
            Drift = null;
            learning = learn;
            learnSamples.Clear();
            lastProgressPos = null;
        }

        private void Seek(double now, double pos)
        {
            player.Seek(pos);
            seekStarted = now;
        }

        private void TrackSeek(double now)
        {
            if (seekStarted.HasValue && !player.IsSeeking)
            {
                double took = now - seekStarted.Value;
                seekStarted = null;
                // Rise immediately, decay slowly: better to cue a little too early.
                SeekTime = took > SeekTime ? took : 0.9 * SeekTime + 0.1 * took;
            }
        }

        public double CueMargin()
        {
            return Math.Min(5.0, 1.5 * SeekTime + 0.15);
        }

        /// <summary>Call once per rendered frame with the current server time.</summary>
        public void Update(double now)
        {
            TrackSeek(now);
            if (!string.IsNullOrEmpty(player.Error))
            {
                State = "error";
                return;
            }
            if (State == "loading")
            {
                if (!player.IsPrepared) return;
                State = "paused";
                if (Anchor == null && PauseRequest == null && VideoMsg != null)
                    PauseRequest = VideoMsg.With(0.0, now);
            }

            if (PauseRequest != null && now >= PauseRequest.At)
            {
                var req = PauseRequest;
                PauseRequest = null;
                SetRate(1.0);
                player.Pause();
                Seek(now, req.Pos);
                Anchor = null;
                PendingStart = false;
                State = "paused";
                return;
            }

            if (Anchor == null) return;

            if (PendingStart)
            {
                Start(now);
                return;
            }

            if (State == "playing") Correct(now);
        }

        private void Start(double now)
        {
            if (!cueAt.HasValue)
            {
                double at = Anchor.At;
                if (at - now < CueMargin()) at = now + CueMargin(); // late: pick a reachable point further on
                cueAt = at;
                SetRate(1.0);
                player.SetLooping(Anchor.Loop);
                player.Pause();
                Seek(now, Wrap(ExpectedPosition(at).Value));
                if (State != "playing") State = "ready";
            }
            if (player.IsSeeking || now < cueAt.Value - StartLatency) return;
            if (now > cueAt.Value + LateStartTolerance)
            {
                cueAt = null; // the seek took too long; cue again further ahead
                return;
            }
            player.Play();
            PendingStart = false;
            cueAt = null;
            State = "playing";
            Settle(now, true);
        }

        private double Wrap(double pos)
        {
            double length = Length();
            if (length > 0 && Anchor != null && Anchor.Loop) return Mod(pos, length);
            if (length > 0) return Math.Max(0.0, Math.Min(pos, length - 0.05));
            return Math.Max(0.0, pos);
        }

        private void Correct(double now)
        {
            if (player.IsSeeking || now < settleUntil) return;
            double expected = ExpectedPosition(now).Value;
            double length = Length();
            if (length > 0 && !Anchor.Loop && expected >= length - 0.25)
            {
                if (expected >= length) State = "ended";
                return;
            }
            double actual = player.Time;
            if (Stalled(now, actual)) return;
            double raw = actual - expected;
            if (length > 0 && Anchor.Loop) raw = Mod(raw + length / 2.0, length) - length / 2.0;
            Drift = Drift.HasValue ? Drift.Value + DriftSmoothing * (raw - Drift.Value) : raw;

            if (learning)
            {
                // Right after a cued start, drift is exactly how wrong our
                // start-latency estimate was (negative = started late).
                learnSamples.Add(raw);
                if (learnSamples.Count >= LearnSamples)
                {
                    learnSamples.Sort();
                    double err = learnSamples[learnSamples.Count / 2];
                    StartLatency = Math.Min(1.0, Math.Max(0.0, StartLatency - LearnGain * err));
                    learning = false;
                }
            }

            var s = Settings;
            string mode = ForcedMode ?? s.correction_mode;
            if (mode == "rate" && !player.CanSetRate) mode = "seek";
            double drift = Drift.Value;
            double threshold = mode == "seek" ? s.seek_mode_threshold_ms : s.hard_seek_ms;
            if (Math.Abs(drift) > threshold / 1000.0)
            {
                if (now - lastSeek >= s.seek_cooldown_ms / 1000.0)
                {
                    lastSeek = now;
                    PendingStart = true;
                    cueAt = null;
                }
                return;
            }
            if (mode == "rate")
            {
                double deadband = s.deadband_ms / 1000.0;
                if (Math.Abs(drift) > deadband)
                {
                    double adjust = Math.Max(-s.max_rate_adjust, Math.Min(s.max_rate_adjust, s.rate_gain * drift));
                    SetRate(1.0 - adjust);
                }
                else if (Math.Abs(drift) < deadband * RateRelease)
                {
                    SetRate(1.0);
                }
            }
            if (mode == "external") player.SetExternalTime(expected);
            else player.ClearExternalTime();
        }

        /// <summary>Detect players that freeze when their speed is changed.</summary>
        private bool Stalled(double now, double actual)
        {
            if (!lastProgressPos.HasValue || Math.Abs(actual - lastProgressPos.Value) > 1e-6)
            {
                lastProgressPos = actual;
                lastProgressT = now;
                return false;
            }
            if (Rate != 1.0 && now - lastProgressT > StallTimeout)
            {
                ForcedMode = "seek";
                SetRate(1.0);
                emitEvent("warn", "video stalled while changing speed; using seek-only correction");
                Settle(now, false);
                return true;
            }
            return false;
        }

        public string ReportedState
        {
            get { return State == "playing" && PendingStart ? "syncing" : State; }
        }

        /// <summary>Adds the engine's fields to a status message.</summary>
        public void WriteStatus(JsonWriter w, double now)
        {
            double? expected = ExpectedPosition(now);
            double length = Length();
            w.Field("state", ReportedState)
             .Field("video", VideoMsg != null ? VideoMsg.Video : null)
             .Field("position", player.LoadedVideo != null ? Math.Round(player.Time, 3) : (double?)null)
             .Field("expected", expected.HasValue && State == "playing" ? Math.Round(expected.Value, 3) : (double?)null)
             .Field("duration", length > 0 ? Math.Round(length, 3) : (double?)null)
             .Field("drift_ms", Drift.HasValue ? Math.Round(Drift.Value * 1000.0, 1) : (double?)null)
             .Field("rate", Math.Round(Rate, 4))
             .Field("mode", ForcedMode ?? Settings.correction_mode)
             .Field("seek_time_ms", Math.Round(SeekTime * 1000.0))
             .Field("start_latency_ms", Math.Round(StartLatency * 1000.0));
        }
    }
}
