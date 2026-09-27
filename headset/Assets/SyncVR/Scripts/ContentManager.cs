using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Text;
using System.Threading;
using UnityEngine;

namespace SyncVR
{
    /// <summary>
    /// The headset's local video folder: inventory, downloads from the server
    /// (resumable, one file at a time, on a background thread) and deletion.
    /// Files copied in with `adb push` are picked up the same way.
    /// </summary>
    public sealed class ContentManager
    {
        private const string PartSuffix = ".part";
        private static readonly HashSet<string> VideoExtensions =
            new HashSet<string>(StringComparer.OrdinalIgnoreCase) { ".mp4", ".m4v", ".mov", ".mkv", ".webm" };

        public readonly string Folder;

        /// <summary>Finished jobs, as ready-to-send JSON messages for the main thread.</summary>
        public readonly ConcurrentQueue<string> Outbox = new ConcurrentQueue<string>();

        private readonly object gate = new object();
        private Thread worker;
        private int generation; // bumped to cancel the running job
        private string currentName;
        private long currentReceived;
        private long currentTotal;

        public ContentManager(string folder)
        {
            Folder = folder;
            Directory.CreateDirectory(folder);
            ServicePointManager.DefaultConnectionLimit = 4;
        }

        public static bool IsSafeName(string name)
        {
            return !string.IsNullOrEmpty(name) && name.Length <= 200 && !name.StartsWith(".")
                   && name.IndexOfAny(new[] { '/', '\\', '\0' }) < 0 && !name.EndsWith(PartSuffix);
        }

        public string PathFor(string name)
        {
            if (!IsSafeName(name)) return null;
            string path = Path.Combine(Folder, name);
            return File.Exists(path) ? path : null;
        }

        public bool Busy
        {
            get { lock (gate) return worker != null && worker.IsAlive; }
        }

        /// <summary>JSON for the "download" status field, or null when idle.</summary>
        public string ProgressJson()
        {
            lock (gate)
            {
                if (currentName == null) return null;
                var sb = new StringBuilder("{\"name\":");
                JsonWriter.WriteString(sb, currentName);
                sb.Append(",\"received\":").Append(currentReceived).Append(",\"total\":").Append(currentTotal).Append('}');
                return sb.ToString();
            }
        }

        public string InventoryJson()
        {
            var sb = new StringBuilder("{\"type\":\"inventory\",\"files\":[");
            bool first = true;
            foreach (var file in new DirectoryInfo(Folder).GetFiles())
            {
                if (!VideoExtensions.Contains(file.Extension) || !IsSafeName(file.Name)) continue;
                if (!first) sb.Append(',');
                first = false;
                sb.Append("{\"name\":");
                JsonWriter.WriteString(sb, file.Name);
                sb.Append(",\"size\":").Append(file.Length).Append('}');
            }
            return sb.Append("]}").ToString();
        }

        public long BytesMissing(ContentFile[] files)
        {
            long missing = 0;
            foreach (var f in files)
            {
                if (!IsSafeName(f.name)) continue;
                var path = Path.Combine(Folder, f.name);
                long have = File.Exists(path) ? new FileInfo(path).Length : 0;
                if (have != f.size)
                {
                    var part = path + PartSuffix;
                    long partial = File.Exists(part) ? new FileInfo(part).Length : 0;
                    missing += Math.Max(0, f.size - partial);
                }
            }
            return missing;
        }

        /// <summary>
        /// Make the folder match <paramref name="files"/>: download what is
        /// missing and, if asked, delete other videos (except <paramref name="inUse"/>).
        /// </summary>
        public void Sync(ContentFile[] files, bool deleteOthers, string inUse)
        {
            int gen = Interlocked.Increment(ref generation);
            Thread previous;
            lock (gate) previous = worker;
            // Never block the render thread: the new job waits for the old one to notice it was cancelled.
            var t = new Thread(() =>
            {
                if (previous != null) previous.Join();
                Work(gen, files ?? new ContentFile[0], deleteOthers, inUse);
            })
            {
                IsBackground = true,
                Name = "SyncVR downloads",
            };
            lock (gate) worker = t;
            t.Start();
        }

        public void Cancel()
        {
            Interlocked.Increment(ref generation);
        }

        private bool Cancelled(int gen)
        {
            return gen != Volatile.Read(ref generation);
        }

        public List<string> Delete(string[] names, string inUse)
        {
            var skipped = new List<string>();
            foreach (var name in names ?? new string[0])
            {
                if (!IsSafeName(name)) continue;
                if (name == inUse)
                {
                    skipped.Add(name);
                    continue;
                }
                TryDelete(Path.Combine(Folder, name));
                TryDelete(Path.Combine(Folder, name + PartSuffix));
            }
            return skipped;
        }

        private static void TryDelete(string path)
        {
            try
            {
                if (File.Exists(path)) File.Delete(path);
            }
            catch (IOException e)
            {
                Debug.LogWarning("[SyncVR] could not delete " + path + ": " + e.Message);
            }
        }

        private void Work(int gen, ContentFile[] files, bool deleteOthers, string inUse)
        {
            var ok = new List<string>();
            var failed = new List<string>();
            try
            {
                if (deleteOthers)
                {
                    var wanted = new HashSet<string>();
                    foreach (var f in files) wanted.Add(f.name);
                    foreach (var file in new DirectoryInfo(Folder).GetFiles())
                    {
                        string name = file.Name.EndsWith(PartSuffix) ? file.Name.Substring(0, file.Name.Length - PartSuffix.Length) : file.Name;
                        if (!wanted.Contains(name) && name != inUse) TryDelete(file.FullName);
                    }
                }
                foreach (var f in files)
                {
                    if (Cancelled(gen)) break;
                    if (!IsSafeName(f.name))
                    {
                        failed.Add(f.name ?? "?");
                        continue;
                    }
                    string dest = Path.Combine(Folder, f.name);
                    if (File.Exists(dest) && new FileInfo(dest).Length == f.size)
                    {
                        ok.Add(f.name);
                        continue;
                    }
                    try
                    {
                        Download(gen, f, dest);
                        ok.Add(f.name);
                    }
                    catch (Exception e)
                    {
                        if (!Cancelled(gen)) Debug.LogWarning("[SyncVR] download of " + f.name + " failed: " + e.Message);
                        failed.Add(f.name);
                    }
                }
            }
            finally
            {
                lock (gate) currentName = null;
                Outbox.Enqueue(InventoryJson());
                Outbox.Enqueue(new JsonWriter("downloads_finished")
                    .Raw("ok", JsonWriter.StringArray(ok))
                    .Raw("failed", JsonWriter.StringArray(failed))
                    .Field("cancelled", Cancelled(gen))
                    .ToString());
            }
        }

        private void Download(int gen, ContentFile f, string dest)
        {
            string part = dest + PartSuffix;
            long have = File.Exists(part) ? new FileInfo(part).Length : 0;
            if (have > f.size)
            {
                File.Delete(part);
                have = 0;
            }
            lock (gate)
            {
                currentName = f.name;
                currentReceived = have;
                currentTotal = f.size;
            }
            if (have < f.size)
            {
                var req = (HttpWebRequest)WebRequest.Create(f.url);
                req.Timeout = 15000;
                req.ReadWriteTimeout = 30000;
                if (have > 0) req.AddRange(have);
                using (var resp = (HttpWebResponse)req.GetResponse())
                {
                    bool partial = resp.StatusCode == HttpStatusCode.PartialContent;
                    if (!partial) have = 0;
                    using (var input = resp.GetResponseStream())
                    using (var output = new FileStream(part, partial ? FileMode.Append : FileMode.Create, FileAccess.Write))
                    {
                        var buffer = new byte[256 * 1024];
                        int n;
                        while ((n = input.Read(buffer, 0, buffer.Length)) > 0)
                        {
                            if (Cancelled(gen)) throw new OperationCanceledException();
                            output.Write(buffer, 0, n);
                            have += n;
                            lock (gate) currentReceived = have;
                        }
                    }
                }
            }
            if (have != f.size) throw new IOException("got " + have + " bytes, expected " + f.size);
            if (File.Exists(dest)) File.Delete(dest);
            File.Move(part, dest);
        }
    }
}
