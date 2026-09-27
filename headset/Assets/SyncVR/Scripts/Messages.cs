using System;
using System.Globalization;
using System.Text;

namespace SyncVR
{
    /// <summary>
    /// Every message the server can send, flattened into one class so
    /// JsonUtility can parse any of them. Fields a message does not carry keep
    /// their defaults. See docs/PROTOCOL.md.
    /// </summary>
    [Serializable]
    public class ServerMessage
    {
        public string type;

        // time_pong
        public long id;
        public double t0;
        public double ts;

        // welcome, device_info
        public string server_name;
        public string device_name;
        public string group;
        public int http_port;
        public SyncSettings settings;

        // play, pause
        public string video;
        public string projection;
        public string stereo;
        public double rotation;
        public double duration;
        public double pos;
        public double at;
        public bool loop;

        // volume
        public double value;

        // message, identify
        public string text;
        public string name;
        public double seconds;

        // sync_content, delete_content
        public ContentFile[] files;
        public bool delete_others;
        public string[] names;
    }

    [Serializable]
    public class ContentFile
    {
        public string name;
        public long size;
        public string url;
    }

    [Serializable]
    public class Beacon
    {
        public string service;
        public int proto;
        public int tcp_port;
        public int http_port;
        public string server_name;
    }

    /// <summary>Sync tuning pushed by the server (see protocol.py DEFAULT_SYNC_SETTINGS).</summary>
    [Serializable]
    public class SyncSettings
    {
        public double play_lead_ms = 1500;
        public double seek_lead_ms = 1500;
        public double pause_lead_ms = 300;
        public string correction_mode = "rate";
        public double deadband_ms = 20;
        public double rate_gain = 0.8;
        public double max_rate_adjust = 0.05;
        public double hard_seek_ms = 300;
        public double seek_mode_threshold_ms = 80;
        public double seek_cooldown_ms = 3000;
        public double settle_ms = 750;
    }

    /// <summary>A play/pause/load instruction for one video.</summary>
    public class VideoCommand
    {
        public string Video;
        public string Projection = "360";
        public string Stereo = "mono";
        public double Rotation;
        public double Duration;
        public double Pos;
        public double At;
        public bool Loop;

        public static VideoCommand From(ServerMessage m)
        {
            return new VideoCommand
            {
                Video = m.video,
                Projection = string.IsNullOrEmpty(m.projection) ? "360" : m.projection,
                Stereo = string.IsNullOrEmpty(m.stereo) ? "mono" : m.stereo,
                Rotation = m.rotation,
                Duration = m.duration,
                Pos = m.pos,
                At = m.at,
                Loop = m.loop,
            };
        }

        public VideoCommand With(double pos, double at)
        {
            var c = (VideoCommand)MemberwiseClone();
            c.Pos = pos;
            c.At = at;
            return c;
        }
    }

    /// <summary>
    /// Minimal JSON object writer for outgoing messages. Numbers are always
    /// written with the invariant culture (a headset set to e.g. German would
    /// otherwise write "1,5").
    /// </summary>
    public sealed class JsonWriter
    {
        private readonly StringBuilder sb = new StringBuilder(256);
        private bool first = true;

        public JsonWriter(string type)
        {
            sb.Append('{');
            Field("type", type);
        }

        private void Key(string key)
        {
            if (!first) sb.Append(',');
            first = false;
            WriteString(sb, key);
            sb.Append(':');
        }

        public JsonWriter Field(string key, string value)
        {
            Key(key);
            if (value == null) sb.Append("null");
            else WriteString(sb, value);
            return this;
        }

        public JsonWriter Field(string key, double value)
        {
            Key(key);
            WriteNumber(sb, value);
            return this;
        }

        public JsonWriter Field(string key, double? value)
        {
            Key(key);
            if (value.HasValue) WriteNumber(sb, value.Value);
            else sb.Append("null");
            return this;
        }

        public JsonWriter Field(string key, long value)
        {
            Key(key);
            sb.Append(value.ToString(CultureInfo.InvariantCulture));
            return this;
        }

        public JsonWriter Field(string key, bool value)
        {
            Key(key);
            sb.Append(value ? "true" : "false");
            return this;
        }

        /// <summary>Insert pre-serialized JSON (object, array or null).</summary>
        public JsonWriter Raw(string key, string json)
        {
            Key(key);
            sb.Append(string.IsNullOrEmpty(json) ? "null" : json);
            return this;
        }

        public override string ToString()
        {
            return sb.ToString() + "}";
        }

        public static void WriteNumber(StringBuilder sb, double value)
        {
            if (double.IsNaN(value) || double.IsInfinity(value)) sb.Append("null");
            else sb.Append(value.ToString("R", CultureInfo.InvariantCulture));
        }

        public static void WriteString(StringBuilder sb, string s)
        {
            sb.Append('"');
            foreach (char c in s)
            {
                switch (c)
                {
                    case '"': sb.Append("\\\""); break;
                    case '\\': sb.Append("\\\\"); break;
                    case '\n': sb.Append("\\n"); break;
                    case '\r': sb.Append("\\r"); break;
                    case '\t': sb.Append("\\t"); break;
                    default:
                        if (c < 0x20) sb.Append("\\u").Append(((int)c).ToString("x4", CultureInfo.InvariantCulture));
                        else sb.Append(c);
                        break;
                }
            }
            sb.Append('"');
        }

        public static string StringArray(System.Collections.Generic.IEnumerable<string> items)
        {
            var sb = new StringBuilder("[");
            bool firstItem = true;
            foreach (var item in items)
            {
                if (!firstItem) sb.Append(',');
                firstItem = false;
                WriteString(sb, item);
            }
            return sb.Append(']').ToString();
        }
    }
}
