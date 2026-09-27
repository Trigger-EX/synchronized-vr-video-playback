using System;
using System.Collections.Concurrent;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using UnityEngine;

namespace SyncVR
{
    /// <summary>
    /// Finds the server (UDP beacon or configured address), keeps one TCP
    /// connection to it and runs clock-sync pings. Everything here runs on
    /// background threads; received messages are queued for the main thread.
    /// Clock pongs are handled right on the receive thread so their arrival
    /// time is not delayed by frame rendering.
    /// </summary>
    public sealed class ServerConnection : IDisposable
    {
        public const int DefaultTcpPort = 8765;
        public const int DefaultDiscoveryPort = 8766;
        private const int ConnectTimeoutMs = 5000;
        private const int ReceiveTimeoutMs = 15000;
        private const int DiscoveryTimeoutMs = 3000;

        public readonly ConcurrentQueue<ServerMessage> Inbox = new ConcurrentQueue<ServerMessage>();
        public readonly ClockSync Clock = new ClockSync();

        private readonly HeadsetConfig config;
        private readonly string helloJson;
        private readonly object sendGate = new object();
        private Thread thread;
        private volatile bool running;
        private volatile NetworkStream stream;
        private volatile bool fastPings;
        private long pingId;

        public volatile bool Connected;
        public volatile string ServerHost;
        public volatile string Status = "Searching for server…";

        public ServerConnection(HeadsetConfig config, string helloJson)
        {
            this.config = config;
            this.helloJson = helloJson;
        }

        public void Start()
        {
            running = true;
            thread = new Thread(Run) { IsBackground = true, Name = "SyncVR network" };
            thread.Start();
            var pinger = new Thread(PingLoop) { IsBackground = true, Name = "SyncVR clock" };
            pinger.Start();
        }

        public void Dispose()
        {
            running = false;
            CloseStream();
        }

        /// <summary>Queue a line for the server. Safe from any thread; dropped when offline.</summary>
        public void Send(string json)
        {
            var s = stream;
            if (s == null) return;
            byte[] data = Encoding.UTF8.GetBytes(json + "\n");
            try
            {
                lock (sendGate) s.Write(data, 0, data.Length);
            }
            catch (Exception)
            {
                CloseStream();
            }
        }

        /// <summary>Re-measure the clock quickly (after connecting or waking from sleep).</summary>
        public void ResyncClock()
        {
            Clock.Reset();
            fastPings = true;
        }

        private void CloseStream()
        {
            var s = stream;
            stream = null;
            if (s != null)
            {
                try { s.Close(); } catch (Exception) { }
            }
        }

        private void Run()
        {
            int backoffMs = 500;
            while (running)
            {
                string host;
                int port;
                if (!string.IsNullOrEmpty(config.server))
                {
                    host = config.server;
                    port = config.port > 0 ? config.port : DefaultTcpPort;
                }
                else if (!Discover(out host, out port))
                {
                    continue;
                }

                try
                {
                    Status = "Connecting to " + host + "…";
                    Session(host, port);
                    backoffMs = 500;
                }
                catch (Exception e)
                {
                    Debug.Log("[SyncVR] connection to " + host + " ended: " + e.Message);
                }
                finally
                {
                    CloseStream();
                    if (Connected)
                    {
                        Connected = false;
                        Inbox.Enqueue(new ServerMessage { type = "_disconnected" });
                    }
                }
                if (!running) break;
                Status = "Lost server, retrying…";
                Thread.Sleep(backoffMs);
                backoffMs = Math.Min(backoffMs * 2, 5000);
            }
        }

        private void Session(string host, int port)
        {
            using (var client = new TcpClient())
            {
                client.NoDelay = true;
                var ar = client.BeginConnect(host, port, null, null);
                if (!ar.AsyncWaitHandle.WaitOne(ConnectTimeoutMs)) throw new TimeoutException("connect timeout");
                client.EndConnect(ar);
                client.ReceiveTimeout = ReceiveTimeoutMs;
                client.SendTimeout = 3000; // never let a stalled link freeze the render thread
                client.Client.SetSocketOption(SocketOptionLevel.Socket, SocketOptionName.KeepAlive, true);

                var netStream = client.GetStream();
                var hello = Encoding.UTF8.GetBytes(helloJson + "\n");
                netStream.Write(hello, 0, hello.Length);
                ServerHost = host;
                ResyncClock();
                stream = netStream;
                Connected = true;
                Status = "Connected to " + host;
                Inbox.Enqueue(new ServerMessage { type = "_connected" });

                using (var reader = new StreamReader(netStream, new UTF8Encoding(false)))
                {
                    while (running)
                    {
                        string line = reader.ReadLine();
                        double t1 = LocalClock.Now;
                        if (line == null) break;
                        if (line.Length == 0) continue;
                        ServerMessage msg;
                        try
                        {
                            msg = JsonUtility.FromJson<ServerMessage>(line);
                        }
                        catch (Exception e)
                        {
                            Debug.LogWarning("[SyncVR] bad message: " + e.Message);
                            continue;
                        }
                        if (msg == null || msg.type == null) continue;
                        if (msg.type == "time_pong")
                        {
                            Clock.Add(msg.t0, msg.ts, t1);
                            if (Clock.SampleCount >= 8) fastPings = false;
                            continue;
                        }
                        Inbox.Enqueue(msg);
                    }
                }
            }
        }

        private void PingLoop()
        {
            while (running)
            {
                Thread.Sleep(fastPings ? 100 : 2000);
                if (stream == null) continue;
                long id = Interlocked.Increment(ref pingId);
                Send(new JsonWriter("time_ping").Field("id", id).Field("t0", LocalClock.Now).ToString());
            }
        }

        private bool Discover(out string host, out int port)
        {
            host = null;
            port = DefaultTcpPort;
            Status = "Searching for server…";
            int discoveryPort = config.discovery_port > 0 ? config.discovery_port : DefaultDiscoveryPort;
            try
            {
                using (var udp = new UdpClient())
                {
                    udp.Client.SetSocketOption(SocketOptionLevel.Socket, SocketOptionName.ReuseAddress, true);
                    udp.Client.Bind(new IPEndPoint(IPAddress.Any, discoveryPort));
                    udp.Client.ReceiveTimeout = DiscoveryTimeoutMs;
                    var deadline = LocalClock.Now + DiscoveryTimeoutMs / 1000.0;
                    while (running && LocalClock.Now < deadline)
                    {
                        var from = new IPEndPoint(IPAddress.Any, 0);
                        byte[] data = udp.Receive(ref from);
                        Beacon beacon;
                        try
                        {
                            beacon = JsonUtility.FromJson<Beacon>(Encoding.UTF8.GetString(data));
                        }
                        catch (Exception)
                        {
                            continue;
                        }
                        if (beacon == null || beacon.service != "syncvr" || beacon.tcp_port <= 0) continue;
                        if (!string.IsNullOrEmpty(config.server_name) && beacon.server_name != config.server_name) continue;
                        host = from.Address.ToString();
                        port = beacon.tcp_port;
                        return true;
                    }
                }
            }
            catch (SocketException)
            {
                // Receive timed out (no beacon yet) or the network is down.
            }
            Thread.Sleep(200);
            return false;
        }
    }
}
