using System;
using UnityEngine;
using UnityEngine.Video;

namespace SyncVR
{
    /// <summary>
    /// IVideoPlayer on top of Unity's built-in VideoPlayer (hardware decoding
    /// via Android MediaCodec). Renders into its own texture (APIOnly mode);
    /// VideoScreen maps that texture onto a sphere or a flat screen.
    ///
    /// To use a different decoder (e.g. AVPro Video / ExoPlayer, which
    /// time-stretch audio when the speed changes), implement IVideoPlayer the
    /// same way and swap it in SyncVRApp.
    /// </summary>
    public sealed class UnityVideoBackend : IVideoPlayer
    {
        // A seek that has not completed after this long is assumed done.
        private const double SeekTimeout = 4.0;

        private readonly VideoPlayer vp;
        private readonly AudioSource audio;
        private readonly Func<string, string> resolvePath;
        private bool prepared;
        private bool seeking;
        private double seekIssuedAt;
        private bool external;

        public string LoadedVideo { get; private set; }
        public string Error { get; private set; }

        public UnityVideoBackend(GameObject host, Func<string, string> resolvePath)
        {
            this.resolvePath = resolvePath;
            audio = host.AddComponent<AudioSource>();
            audio.playOnAwake = false;
            audio.spatialBlend = 0f;

            vp = host.AddComponent<VideoPlayer>();
            vp.playOnAwake = false;
            vp.waitForFirstFrame = true;
            vp.skipOnDrop = true;
            vp.source = VideoSource.Url;
            vp.renderMode = VideoRenderMode.APIOnly;
            vp.timeReference = VideoTimeReference.Freerun;
            vp.audioOutputMode = VideoAudioOutputMode.AudioSource;
            vp.controlledAudioTrackCount = 1;
            vp.EnableAudioTrack(0, true);
            vp.SetTargetAudioSource(0, audio);

            vp.prepareCompleted += OnPrepared;
            vp.seekCompleted += OnSeekCompleted;
            vp.errorReceived += OnError;
        }

        private void OnPrepared(VideoPlayer source)
        {
            prepared = true;
            source.Pause();
        }

        private void OnSeekCompleted(VideoPlayer source)
        {
            seeking = false;
        }

        private void OnError(VideoPlayer source, string message)
        {
            Error = message;
            LoadedVideo = null; // so the next command for this video reloads it
            Debug.LogError("[SyncVR] video error: " + message);
        }

        public bool IsPrepared { get { return prepared && vp.isPrepared; } }

        public bool IsSeeking
        {
            get
            {
                if (seeking && LocalClock.Now - seekIssuedAt > SeekTimeout) seeking = false;
                return seeking;
            }
        }

        public bool IsPlaying { get { return vp.isPlaying; } }
        public double Time { get { return vp.time; } }
        public double Length { get { return prepared ? vp.length : 0.0; } }
        public bool CanSetRate { get { return prepared && vp.canSetPlaybackSpeed; } }
        public Texture Texture { get { return prepared ? vp.texture : null; } }
        public int Width { get { return prepared ? (int)vp.width : 0; } }
        public int Height { get { return prepared ? (int)vp.height : 0; } }

        public void Load(VideoCommand cmd)
        {
            Stop();
            string path = resolvePath(cmd.Video);
            if (path == null)
            {
                Error = "video not on this headset: " + cmd.Video;
                return;
            }
            LoadedVideo = cmd.Video;
            vp.url = path;
            vp.isLooping = cmd.Loop;
            vp.Prepare();
        }

        public void Play()
        {
            if (prepared) vp.Play();
        }

        public void Pause()
        {
            if (prepared) vp.Pause();
        }

        public void Stop()
        {
            vp.Stop();
            vp.playbackSpeed = 1f;
            ClearExternalTime();
            prepared = false;
            seeking = false;
            Error = null;
            LoadedVideo = null;
        }

        public void Seek(double seconds)
        {
            if (!prepared || !vp.canSetTime) return;
            seeking = true;
            seekIssuedAt = LocalClock.Now;
            vp.time = seconds;
        }

        public void SetRate(double rate)
        {
            if (prepared && vp.canSetPlaybackSpeed) vp.playbackSpeed = (float)rate;
        }

        public void SetLooping(bool loop)
        {
            vp.isLooping = loop;
        }

        public void SetExternalTime(double seconds)
        {
            if (!external)
            {
                vp.timeReference = VideoTimeReference.ExternalTime;
                external = true;
            }
            vp.externalReferenceTime = seconds;
        }

        public void ClearExternalTime()
        {
            if (external)
            {
                vp.timeReference = VideoTimeReference.Freerun;
                external = false;
            }
        }

        public void SetVolume(float volume)
        {
            audio.volume = Mathf.Clamp01(volume);
        }
    }
}
