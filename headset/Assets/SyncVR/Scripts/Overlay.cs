using UnityEngine;

namespace SyncVR
{
    /// <summary>
    /// Text floating in front of the viewer: the idle/status screen, operator
    /// messages and the "identify" banner. It lazily follows the viewer's
    /// gaze so it can't be lost behind them.
    /// </summary>
    public sealed class Overlay
    {
        private const float Distance = 2.2f;
        private const float FollowAngle = 25f;

        private readonly Camera camera;
        private readonly Transform pivot;
        private readonly TextMesh status;
        private readonly TextMesh message;
        private readonly AudioSource beeper;
        private float messageUntil;
        private bool following;

        public Overlay(Camera camera)
        {
            this.camera = camera;
            pivot = new GameObject("Overlay").transform;
            status = MakeText("Status", new Vector3(0f, -0.1f, Distance), 0.012f, new Color(0.85f, 0.9f, 0.95f));
            message = MakeText("Message", new Vector3(0f, 0.45f, Distance), 0.016f, Color.white);

            beeper = pivot.gameObject.AddComponent<AudioSource>();
            beeper.playOnAwake = false;
            beeper.spatialBlend = 0f;
            beeper.clip = MakeBeep();
        }

        private TextMesh MakeText(string name, Vector3 position, float size, Color color)
        {
            var go = new GameObject(name);
            go.transform.SetParent(pivot, false);
            go.transform.localPosition = position;
            var text = go.AddComponent<TextMesh>();
            var font = Resources.GetBuiltinResource<Font>("Arial.ttf");
            text.font = font;
            go.GetComponent<MeshRenderer>().sharedMaterial = font.material;
            text.fontSize = 64;
            text.characterSize = size;
            text.anchor = TextAnchor.MiddleCenter;
            text.alignment = TextAlignment.Center;
            text.color = color;
            return text;
        }

        private static AudioClip MakeBeep()
        {
            const int rate = 44100;
            var samples = new float[rate];
            for (int i = 0; i < samples.Length; i++)
            {
                float t = (float)i / rate;
                // Three short 880 Hz pips.
                bool on = (t % 0.33f) < 0.18f;
                samples[i] = on ? 0.5f * Mathf.Sin(2f * Mathf.PI * 880f * t) : 0f;
            }
            var clip = AudioClip.Create("beep", samples.Length, 1, rate, false);
            clip.SetData(samples, 0);
            return clip;
        }

        public void SetStatus(string text, bool show)
        {
            if (status.text != text) status.text = text;
            if (status.gameObject.activeSelf != show) status.gameObject.SetActive(show);
        }

        public void ShowMessage(string text, float seconds)
        {
            message.text = WordWrap(text ?? "", 32);
            messageUntil = seconds > 0 && !string.IsNullOrEmpty(text) ? Time.unscaledTime + seconds : 0f;
            following = true;
        }

        public void Identify(string name, float seconds)
        {
            ShowMessage(name, seconds);
            beeper.Play();
        }

        public void Update()
        {
            message.gameObject.SetActive(Time.unscaledTime < messageUntil);
            // Keep the text roughly in front of the viewer.
            float yaw = camera.transform.eulerAngles.y;
            float diff = Mathf.DeltaAngle(pivot.eulerAngles.y, yaw);
            if (Mathf.Abs(diff) > FollowAngle) following = true;
            if (following)
            {
                float next = Mathf.MoveTowardsAngle(pivot.eulerAngles.y, yaw, 120f * Time.unscaledDeltaTime);
                pivot.rotation = Quaternion.Euler(0f, next, 0f);
                if (Mathf.Abs(Mathf.DeltaAngle(next, yaw)) < 1f) following = false;
            }
            pivot.position = camera.transform.position;
        }

        private static string WordWrap(string text, int width)
        {
            var words = text.Split(' ');
            var sb = new System.Text.StringBuilder();
            int line = 0;
            foreach (var word in words)
            {
                if (line > 0 && line + word.Length + 1 > width)
                {
                    sb.Append('\n');
                    line = 0;
                }
                else if (line > 0)
                {
                    sb.Append(' ');
                    line++;
                }
                sb.Append(word);
                line += word.Length;
            }
            return sb.ToString();
        }
    }
}
