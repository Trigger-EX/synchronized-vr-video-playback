using System;
using System.Collections.Generic;
using UnityEngine;
using UnityEngine.XR;

namespace SyncVR
{
    /// <summary>
    /// Headset telemetry (battery, temperature, storage, Wi-Fi signal, whether
    /// it is being worn) and Android Wi-Fi locks. Main thread only.
    /// </summary>
    public sealed class DeviceInfo
    {
        public readonly string DeviceId;
        public readonly string Serial;
        public readonly string Model;

        public float Battery = -1f;
        public bool Charging;
        public float TemperatureC = -1f;
        public long StorageFree = -1;
        public int WifiRssi;
        public bool? Worn;

#if UNITY_ANDROID && !UNITY_EDITOR
        // Held for the lifetime of the app; releasing them happens on garbage collection.
        private static AndroidJavaObject wifiLock;
        private static AndroidJavaObject multicastLock;
#endif

        public DeviceInfo()
        {
            Model = SystemInfo.deviceModel;
            Serial = ReadSerial();
            // The serial matches what `adb devices` shows, which makes headsets easy to map.
            DeviceId = !string.IsNullOrEmpty(Serial) && Serial != "unknown" ? Serial : SystemInfo.deviceUniqueIdentifier;
        }

        private static string ReadSerial()
        {
#if UNITY_ANDROID && !UNITY_EDITOR
            try
            {
                using (var build = new AndroidJavaClass("android.os.Build"))
                    return build.GetStatic<string>("SERIAL");
            }
            catch (Exception e)
            {
                Debug.LogWarning("[SyncVR] no serial: " + e.Message);
            }
#endif
            return "";
        }

        /// <summary>
        /// The app's folder on shared storage (/sdcard/Android/data/&lt;package&gt;/files),
        /// which `adb push` can write to without root. Application.persistentDataPath
        /// may point to internal storage depending on player settings, so ask Android.
        /// </summary>
        public static string DataDirectory()
        {
#if UNITY_ANDROID && !UNITY_EDITOR
            try
            {
                using (var activity = Activity())
                using (var dir = activity.Call<AndroidJavaObject>("getExternalFilesDir", (object)null))
                {
                    if (dir != null) return dir.Call<string>("getAbsolutePath");
                }
            }
            catch (Exception e)
            {
                Debug.LogWarning("[SyncVR] no external files dir: " + e.Message);
            }
#endif
            return Application.persistentDataPath;
        }

        /// <summary>
        /// Keep Wi-Fi in high-performance mode (power saving adds large,
        /// random latency that hurts clock sync) and let broadcast discovery
        /// packets through.
        /// </summary>
        public static void AcquireWifiLocks()
        {
#if UNITY_ANDROID && !UNITY_EDITOR
            try
            {
                using (var wifi = WifiManager())
                {
                    const int WIFI_MODE_FULL_HIGH_PERF = 3;
                    wifiLock = wifi.Call<AndroidJavaObject>("createWifiLock", WIFI_MODE_FULL_HIGH_PERF, "SyncVR");
                    wifiLock.Call("acquire");
                    multicastLock = wifi.Call<AndroidJavaObject>("createMulticastLock", "SyncVR");
                    multicastLock.Call("acquire");
                }
            }
            catch (Exception e)
            {
                Debug.LogWarning("[SyncVR] could not acquire Wi-Fi locks: " + e.Message);
            }
#endif
        }

#if UNITY_ANDROID && !UNITY_EDITOR
        private static AndroidJavaObject Activity()
        {
            using (var player = new AndroidJavaClass("com.unity3d.player.UnityPlayer"))
                return player.GetStatic<AndroidJavaObject>("currentActivity");
        }

        private static AndroidJavaObject WifiManager()
        {
            using (var activity = Activity())
            using (var context = activity.Call<AndroidJavaObject>("getApplicationContext"))
                return context.Call<AndroidJavaObject>("getSystemService", "wifi");
        }
#endif

        public void Refresh(string storagePath)
        {
            Battery = SystemInfo.batteryLevel;
            Charging = SystemInfo.batteryStatus == BatteryStatus.Charging || SystemInfo.batteryStatus == BatteryStatus.Full;
            Worn = ReadUserPresence();
#if UNITY_ANDROID && !UNITY_EDITOR
            try
            {
                using (var activity = Activity())
                using (var filter = new AndroidJavaObject("android.content.IntentFilter", "android.intent.action.BATTERY_CHANGED"))
                using (var intent = activity.Call<AndroidJavaObject>("registerReceiver", null, filter))
                {
                    if (intent != null) TemperatureC = intent.Call<int>("getIntExtra", "temperature", -10) / 10f;
                }
            }
            catch (Exception) { }
            try
            {
                using (var stat = new AndroidJavaObject("android.os.StatFs", storagePath))
                    StorageFree = stat.Call<long>("getAvailableBytes");
            }
            catch (Exception) { }
            try
            {
                using (var wifi = WifiManager())
                using (var info = wifi.Call<AndroidJavaObject>("getConnectionInfo"))
                    WifiRssi = info.Call<int>("getRssi");
            }
            catch (Exception) { }
#endif
        }

        private static readonly List<InputDevice> Devices = new List<InputDevice>();

        private static bool? ReadUserPresence()
        {
            InputDevices.GetDevicesAtXRNode(XRNode.Head, Devices);
            foreach (var d in Devices)
            {
                bool present;
                if (d.TryGetFeatureValue(CommonUsages.userPresence, out present)) return present;
            }
#pragma warning disable 618
            switch (XRDevice.userPresence)
            {
                case UserPresenceState.Present: return true;
                case UserPresenceState.NotPresent: return false;
                default: return null;
            }
#pragma warning restore 618
        }
    }
}
