using System;
using System.IO;
using UnityEngine;

namespace SyncVR
{
    /// <summary>
    /// The headset app. Builds its scene at runtime (camera, video screen,
    /// overlay), connects to the SyncVR server and follows its commands.
    /// Drop it into any scene, or let it bootstrap itself.
    /// </summary>
    public sealed class SyncVRApp : MonoBehaviour
    {
        private const float StatusInterval = 1f;
        private const float TelemetryInterval = 10f;
        private static readonly Color IdleBackground = new Color(0.06f, 0.07f, 0.09f);

        private HeadsetConfig config;
        private DeviceInfo device;
        private ServerConnection connection;
        private ContentManager content;
        private UnityVideoBackend backend;
        private SyncEngine engine;
        private VideoScreen screen;
        private Overlay overlay;
        private Camera cam;
        private Transform contentRoot;

        private string deviceName = "";
        private string serverName = "";
        private float volume = 1f;
        private float nextStatus;
        private float nextTelemetry;
        private float nextCalibrationSave;
        private readonly System.Collections.Generic.Queue<string> pendingEvents =
            new System.Collections.Generic.Queue<string>();

        [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.AfterSceneLoad)]
        private static void Bootstrap()
        {
            if (FindObjectOfType<SyncVRApp>() == null) new GameObject("SyncVR").AddComponent<SyncVRApp>();
        }

        private void Awake()
        {
            DontDestroyOnLoad(gameObject);
            Application.runInBackground = true;
            Screen.sleepTimeout = SleepTimeout.NeverSleep;

            cam = Camera.main;
            if (cam == null)
            {
                var go = new GameObject("Main Camera") { tag = "MainCamera" };
                cam = go.AddComponent<Camera>();
                go.AddComponent<AudioListener>();
            }
            cam.nearClipPlane = 0.05f;
            cam.farClipPlane = 100f;
            contentRoot = new GameObject("Content").transform;
            DontDestroyOnLoad(cam.gameObject);
            DontDestroyOnLoad(contentRoot.gameObject);

            string dataDir = DeviceInfo.DataDirectory();
            config = HeadsetConfig.Load(dataDir);
            device = new DeviceInfo();
            DeviceInfo.AcquireWifiLocks();
            content = new ContentManager(Path.Combine(dataDir, "videos"));
            backend = new UnityVideoBackend(gameObject, content.PathFor);
            engine = new SyncEngine(backend, QueueEvent);
            engine.StartLatency = PlayerPrefs.GetFloat("syncvr.start_latency", (float)engine.StartLatency);
            engine.SeekTime = PlayerPrefs.GetFloat("syncvr.seek_time", (float)engine.SeekTime);
            screen = new VideoScreen(cam, contentRoot, IdleBackground);
            overlay = new Overlay(cam);

            var hello = new JsonWriter("hello")
                .Field("proto", 1L)
                .Field("device_id", device.DeviceId)
                .Field("serial", device.Serial)
                .Field("model", device.Model)
                .Field("app_version", Application.version)
                .ToString();
            connection = new ServerConnection(config, hello);
            connection.Start();
            Debug.Log("[SyncVR] started; device " + device.DeviceId + ", videos in " + content.Folder);
        }

        private void OnDestroy()
        {
            if (connection != null) connection.Dispose();
            if (content != null) content.Cancel();
            SaveCalibration();
        }

        private void OnApplicationPause(bool paused)
        {
            if (paused)
            {
                SaveCalibration();
                return;
            }
            // The monotonic clock stops while the headset sleeps: re-measure the
            // offset, then re-cue whatever should be playing.
            if (connection != null) connection.ResyncClock();
            if (engine != null) engine.Resync();
        }

        private void QueueEvent(string level, string message)
        {
            pendingEvents.Enqueue(new JsonWriter("event").Field("level", level).Field("message", message).ToString());
        }

        private void Update()
        {
            ServerMessage msg;
            while (connection.Inbox.TryDequeue(out msg)) Handle(msg);

            if (connection.Clock.Synced)
                engine.Update(connection.Clock.ServerTime(LocalClock.Now));

            UpdateVisuals();

            string line;
            while (content.Outbox.TryDequeue(out line)) connection.Send(line);
            while (pendingEvents.Count > 0) connection.Send(pendingEvents.Dequeue());

            if (Time.unscaledTime >= nextTelemetry)
            {
                nextTelemetry = Time.unscaledTime + TelemetryInterval;
                device.Refresh(content.Folder);
            }
            if (connection.Connected && Time.unscaledTime >= nextStatus)
            {
                nextStatus = Time.unscaledTime + StatusInterval;
                connection.Send(StatusJson());
            }
            if (Time.unscaledTime >= nextCalibrationSave)
            {
                nextCalibrationSave = Time.unscaledTime + 30f;
                SaveCalibration();
            }
        }

        private void SaveCalibration()
        {
            if (engine == null) return;
            PlayerPrefs.SetFloat("syncvr.start_latency", (float)engine.StartLatency);
            PlayerPrefs.SetFloat("syncvr.seek_time", (float)engine.SeekTime);
            PlayerPrefs.Save();
        }

        private void Handle(ServerMessage m)
        {
            switch (m.type)
            {
                case "_connected":
                    connection.Send(content.InventoryJson());
                    nextStatus = 0f;
                    break;
                case "_disconnected":
                    break;
                case "welcome":
                    serverName = m.server_name ?? "";
                    deviceName = m.device_name ?? "";
                    engine.OnSettings(m.settings);
                    break;
                case "settings":
                    engine.OnSettings(m.settings);
                    break;
                case "device_info":
                    deviceName = m.device_name ?? deviceName;
                    break;
                case "play":
                {
                    var cmd = VideoCommand.From(m);
                    screen.Configure(cmd);
                    engine.OnPlay(cmd);
                    break;
                }
                case "pause":
                {
                    var cmd = VideoCommand.From(m);
                    screen.Configure(cmd);
                    engine.OnPause(cmd);
                    break;
                }
                case "stop":
                    engine.OnStop();
                    break;
                case "volume":
                    volume = Mathf.Clamp01((float)m.value);
                    backend.SetVolume(volume);
                    break;
                case "recenter":
                    Recenter();
                    break;
                case "message":
                    overlay.ShowMessage(m.text, (float)m.seconds);
                    break;
                case "identify":
                    overlay.Identify(string.IsNullOrEmpty(m.name) ? deviceName : m.name, m.seconds > 0 ? (float)m.seconds : 8f);
                    break;
                case "sync_content":
                    StartSync(m.files ?? new ContentFile[0], m.delete_others);
                    break;
                case "cancel_downloads":
                    content.Cancel();
                    break;
                case "delete_content":
                {
                    var skipped = content.Delete(m.names, backend.LoadedVideo);
                    foreach (var name in skipped) QueueEvent("warn", "not deleting " + name + ": it is loaded");
                    connection.Send(content.InventoryJson());
                    break;
                }
            }
        }

        private void StartSync(ContentFile[] files, bool deleteOthers)
        {
            long needed = content.BytesMissing(files);
            device.Refresh(content.Folder);
            if (device.StorageFree >= 0 && needed > device.StorageFree - 200L * 1024 * 1024)
            {
                QueueEvent("error", string.Format("not enough space: need {0:0.0} GB, {1:0.0} GB free",
                    needed / 1e9, device.StorageFree / 1e9));
                connection.Send(new JsonWriter("downloads_finished").Raw("ok", "[]")
                    .Raw("failed", JsonWriter.StringArray(Array.ConvertAll(files, f => f.name))).ToString());
                return;
            }
            content.Sync(files, deleteOthers, backend.LoadedVideo);
        }

        /// <summary>Make the direction the viewer is facing the front of the content.</summary>
        private void Recenter()
        {
            float yaw = cam.transform.eulerAngles.y;
            contentRoot.rotation = Quaternion.Euler(0f, yaw, 0f);
            screen.ApplyRotation();
        }

        private void UpdateVisuals()
        {
            string state = engine.State;
            bool showVideo = backend.LoadedVideo != null && backend.IsPrepared &&
                             (state == "playing" || state == "paused" || state == "ready" || state == "ended");
            if (showVideo) screen.Show(backend.Texture, backend.Width, backend.Height);
            else screen.Hide();

            overlay.SetStatus(StatusText(state), !showVideo);
            overlay.Update();
        }

        private string StatusText(string state)
        {
            string title = string.IsNullOrEmpty(deviceName) ? "SyncVR" : deviceName;
            string line;
            if (!connection.Connected) line = connection.Status;
            else if (state == "loading") line = "Loading…";
            else if (state == "error") line = "Cannot play this video.\n" + (backend.Error ?? "");
            else line = (string.IsNullOrEmpty(serverName) ? "Connected" : "Connected to " + serverName) +
                        "\nWaiting for the operator";
            string download = content.ProgressJson() != null ? "\nReceiving content…" : "";
            return title + "\n\n" + line + download;
        }

        private string StatusJson()
        {
            double now = connection.Clock.ServerTime(LocalClock.Now);
            var w = new JsonWriter("status");
            engine.WriteStatus(w, now);
            w.Field("battery", (double)device.Battery)
             .Field("charging", device.Charging)
             .Field("temp_c", device.TemperatureC >= 0 ? (double?)device.TemperatureC : null)
             .Field("storage_free", device.StorageFree)
             .Field("wifi_rssi", (long)device.WifiRssi)
             .Field("volume", (double)volume)
             .Field("rtt_ms", connection.Clock.Synced ? Math.Round(connection.Clock.Rtt * 1000.0, 1) : (double?)null)
             .Field("clock_synced", connection.Clock.Synced)
             .Field("fps", Math.Round(1.0 / Math.Max(1e-3, Time.smoothDeltaTime), 1))
             .Raw("download", content.ProgressJson())
             .Field("error", engine.State == "error" ? backend.Error : null);
            if (device.Worn.HasValue) w.Field("worn", device.Worn.Value);
            return w.ToString();
        }
    }
}
