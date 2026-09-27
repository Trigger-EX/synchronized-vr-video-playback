using System;
using System.IO;
using UnityEngine;

namespace SyncVR
{
    /// <summary>
    /// Optional per-headset settings from config.json in the app's data folder,
    /// i.e. /sdcard/Android/data/com.syncvr.player/files/config.json on the
    /// headset (written by `python -m syncvr adb configure`). With no file the
    /// headset finds the server by itself.
    /// </summary>
    [Serializable]
    public class HeadsetConfig
    {
        public string server = "";       // fixed server IP/host instead of discovery
        public int port;                 // server headset port (default 8765)
        public int discovery_port;       // UDP beacon port (default 8766)
        public string server_name = "";  // only accept beacons from this server

        public static HeadsetConfig Load(string directory)
        {
            string path = Path.Combine(directory, "config.json");
            try
            {
                if (File.Exists(path))
                {
                    var cfg = JsonUtility.FromJson<HeadsetConfig>(File.ReadAllText(path));
                    if (cfg != null) return cfg;
                }
            }
            catch (Exception e)
            {
                Debug.LogWarning("[SyncVR] ignoring unreadable " + path + ": " + e.Message);
            }
            return new HeadsetConfig();
        }
    }
}
