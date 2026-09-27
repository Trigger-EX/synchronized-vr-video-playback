using UnityEngine;

namespace SyncVR
{
    /// <summary>
    /// Shows the video texture: as a 360/180 sphere via the built-in
    /// Skybox/Panoramic shader (which also handles top/bottom and side-by-side
    /// stereo per eye), or on a flat cinema screen in front of the viewer.
    /// </summary>
    public sealed class VideoScreen
    {
        // Skybox/Panoramic puts the centre of a 360 image at +X; turn it to face +Z.
        // For 180 images the shader already centres them at +Z.
        private const float Base360Yaw = 90f;
        private const float ScreenDistance = 4f;
        private const float ScreenWidth = 5.2f;

        private readonly Camera camera;
        private readonly Transform contentRoot;
        private readonly Material skybox;
        private readonly Material screenMaterial;
        private readonly GameObject screen;
        private readonly Color background;
        private string projection = "360";
        private string stereo = "mono";
        private float rotation;
        private bool visible;

        public VideoScreen(Camera camera, Transform contentRoot, Color background)
        {
            this.camera = camera;
            this.contentRoot = contentRoot;
            this.background = background;

            var sky = Resources.Load<Material>("SyncVRSkybox");
            skybox = sky != null ? new Material(sky) : new Material(Shader.Find("Skybox/Panoramic"));
            skybox.DisableKeyword("_MAPPING_6_FRAMES_LAYOUT");

            var scr = Resources.Load<Material>("SyncVRScreen");
            screenMaterial = scr != null ? new Material(scr) : new Material(Shader.Find("Unlit/Texture"));

            screen = GameObject.CreatePrimitive(PrimitiveType.Quad);
            screen.name = "FlatScreen";
            Object.Destroy(screen.GetComponent<Collider>());
            screen.transform.SetParent(contentRoot, false);
            screen.transform.localPosition = new Vector3(0f, 0f, ScreenDistance);
            screen.GetComponent<Renderer>().sharedMaterial = screenMaterial;
            screen.SetActive(false);
            Hide();
        }

        public void Configure(VideoCommand cmd)
        {
            projection = cmd.Projection ?? "360";
            stereo = cmd.Stereo ?? "mono";
            rotation = (float)cmd.Rotation;
            ApplyRotation();
            skybox.SetFloat("_ImageType", projection == "180" ? 1f : 0f);
            skybox.SetFloat("_MirrorOnBack", 0f);
            skybox.SetFloat("_Layout", stereo == "sbs" ? 1f : stereo == "tb" ? 2f : 0f);

            // Flat screens show one eye of stereo content.
            if (stereo == "sbs") SetScreenUv(new Vector2(0.5f, 1f), Vector2.zero);
            else if (stereo == "tb") SetScreenUv(new Vector2(1f, 0.5f), new Vector2(0f, 0.5f));
            else SetScreenUv(Vector2.one, Vector2.zero);
        }

        private void SetScreenUv(Vector2 scale, Vector2 offset)
        {
            screenMaterial.mainTextureScale = scale;
            screenMaterial.mainTextureOffset = offset;
        }

        /// <summary>Called when the viewer's forward direction is reset.</summary>
        public void ApplyRotation()
        {
            float baseYaw = projection == "360" ? Base360Yaw : 0f;
            // The skybox is not parented to contentRoot, so turn it by the same yaw.
            float yaw = baseYaw + rotation - contentRoot.eulerAngles.y;
            skybox.SetFloat("_Rotation", Mathf.Repeat(yaw, 360f));
        }

        public void Show(Texture texture, int width, int height)
        {
            if (texture == null)
            {
                Hide();
                return;
            }
            if (projection == "flat")
            {
                screenMaterial.mainTexture = texture;
                float aspect = height > 0 ? (float)width / height : 16f / 9f;
                if (stereo == "sbs") aspect *= 0.5f;
                else if (stereo == "tb") aspect *= 2f;
                screen.transform.localScale = new Vector3(ScreenWidth, ScreenWidth / aspect, 1f);
                screen.SetActive(true);
                if (RenderSettings.skybox != null) RenderSettings.skybox = null;
                camera.clearFlags = CameraClearFlags.SolidColor;
                camera.backgroundColor = Color.black;
            }
            else
            {
                skybox.mainTexture = texture;
                screen.SetActive(false);
                if (RenderSettings.skybox != skybox) RenderSettings.skybox = skybox;
                camera.clearFlags = CameraClearFlags.Skybox;
            }
            visible = true;
        }

        public void Hide()
        {
            if (!visible && screen.activeSelf == false && camera.clearFlags == CameraClearFlags.SolidColor) return;
            screen.SetActive(false);
            RenderSettings.skybox = null;
            camera.clearFlags = CameraClearFlags.SolidColor;
            camera.backgroundColor = background;
            visible = false;
        }
    }
}
