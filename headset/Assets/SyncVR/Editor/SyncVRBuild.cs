using System;
using System.IO;
using System.Linq;
using UnityEditor;
using UnityEditor.Build.Reporting;
using UnityEditor.PackageManager;
using UnityEditor.PackageManager.Requests;
using UnityEditor.SceneManagement;
using UnityEditorInternal.VR;
using UnityEngine;
using UnityEngine.Rendering;

// Oculus Go needs Unity's legacy built-in VR, whose APIs are marked obsolete in 2019.4.
#pragma warning disable 618

namespace SyncVR.EditorTools
{
    /// <summary>
    /// One-click project setup and APK build for Oculus Go.
    ///
    ///   SyncVR > 1. Configure for Oculus Go   (run once; installs the Oculus package)
    ///   SyncVR > 2. Build APK                 (writes Builds/SyncVRPlayer.apk)
    ///
    /// Command line, after configuring once in the editor:
    ///   Unity -batchmode -quit -projectPath headset -executeMethod SyncVR.EditorTools.SyncVRBuild.BuildFromCommandLine
    /// </summary>
    public static class SyncVRBuild
    {
        public const string PackageName = "com.syncvr.player";
        public const string Version = "0.1.0";
        private const string ScenePath = "Assets/SyncVR/Scenes/Main.unity";
        private const string ResourcesDir = "Assets/SyncVR/Resources";
        private const string OutputPath = "Builds/SyncVRPlayer.apk";
        private const string OculusPackage = "com.unity.xr.oculus.android";

        private static ListRequest listRequest;
        private static AddRequest addRequest;

        [MenuItem("SyncVR/1. Configure for Oculus Go", false, 1)]
        public static void Configure()
        {
            if (EditorUserBuildSettings.activeBuildTarget != BuildTarget.Android)
                EditorUserBuildSettings.SwitchActiveBuildTarget(BuildTargetGroup.Android, BuildTarget.Android);

            ApplyPlayerSettings();
            EnsureMaterials();
            EnsureScene();
            AssetDatabase.SaveAssets();

            // Legacy built-in VR needs the "Oculus (Android)" package; install it if missing.
            listRequest = Client.List(true);
            EditorApplication.update += WaitForPackageList;
        }

        private static void ApplyPlayerSettings()
        {
            PlayerSettings.companyName = "SyncVR";
            PlayerSettings.productName = "SyncVR Player";
            PlayerSettings.SetApplicationIdentifier(BuildTargetGroup.Android, PackageName);
            PlayerSettings.bundleVersion = Version;
            PlayerSettings.Android.bundleVersionCode = 1;

            // Oculus Go runs Android 7.1 (API 25) on a 32-bit Snapdragon 821.
            PlayerSettings.Android.minSdkVersion = AndroidSdkVersions.AndroidApiLevel25;
            PlayerSettings.Android.targetSdkVersion = AndroidSdkVersions.AndroidApiLevelAuto;
            PlayerSettings.SetScriptingBackend(BuildTargetGroup.Android, ScriptingImplementation.Mono2x);
            PlayerSettings.Android.targetArchitectures = AndroidArchitecture.ARMv7;
            PlayerSettings.Android.forceInternetPermission = true;
            PlayerSettings.Android.forceSDCardPermission = false; // videos live in the app's own folder
            PlayerSettings.defaultInterfaceOrientation = UIOrientation.LandscapeLeft;
            PlayerSettings.runInBackground = true;

            PlayerSettings.SetUseDefaultGraphicsAPIs(BuildTarget.Android, false);
            PlayerSettings.SetGraphicsAPIs(BuildTarget.Android, new[] { GraphicsDeviceType.OpenGLES3 });
            PlayerSettings.colorSpace = ColorSpace.Gamma;
            PlayerSettings.stereoRenderingPath = StereoRenderingPath.SinglePass;
        }

        private static void EnsureMaterials()
        {
            Directory.CreateDirectory(ResourcesDir);
            CreateMaterial(ResourcesDir + "/SyncVRSkybox.mat", "Skybox/Panoramic");
            CreateMaterial(ResourcesDir + "/SyncVRScreen.mat", "Unlit/Texture");
        }

        private static void CreateMaterial(string path, string shaderName)
        {
            if (AssetDatabase.LoadAssetAtPath<Material>(path) != null) return;
            var shader = Shader.Find(shaderName);
            if (shader == null)
            {
                Debug.LogError("[SyncVR] shader not found: " + shaderName);
                return;
            }
            // Materials in Resources/ make sure their shaders are included in the build.
            AssetDatabase.CreateAsset(new Material(shader), path);
        }

        private static void EnsureScene()
        {
            if (!File.Exists(ScenePath))
            {
                Directory.CreateDirectory(Path.GetDirectoryName(ScenePath));
                var scene = EditorSceneManager.NewScene(NewSceneSetup.DefaultGameObjects, NewSceneMode.Single);
                // SyncVRApp bootstraps itself at runtime; the scene only needs a camera.
                foreach (var light in UnityEngine.Object.FindObjectsOfType<Light>())
                    UnityEngine.Object.DestroyImmediate(light.gameObject);
                EditorSceneManager.SaveScene(scene, ScenePath);
            }
            EditorBuildSettings.scenes = new[] { new EditorBuildSettingsScene(ScenePath, true) };
        }

        private static void WaitForPackageList()
        {
            if (!listRequest.IsCompleted) return;
            EditorApplication.update -= WaitForPackageList;
            bool installed = listRequest.Status == StatusCode.Success &&
                             listRequest.Result.Any(p => p.name == OculusPackage);
            if (installed)
            {
                EnableOculus();
                return;
            }
            Debug.Log("[SyncVR] installing " + OculusPackage + "…");
            addRequest = Client.Add(OculusPackage);
            EditorApplication.update += WaitForPackageAdd;
        }

        private static void WaitForPackageAdd()
        {
            if (!addRequest.IsCompleted) return;
            EditorApplication.update -= WaitForPackageAdd;
            if (addRequest.Status == StatusCode.Success)
            {
                Debug.Log("[SyncVR] installed " + addRequest.Result.packageId);
                // The package registers the Oculus SDK after the domain reload; enable it then.
                EditorApplication.delayCall += EnableOculus;
            }
            else
            {
                Debug.LogError("[SyncVR] could not install " + OculusPackage + ": " + addRequest.Error.message +
                               "\nInstall 'Oculus (Android)' from Window > Package Manager, then run this menu again.");
            }
        }

        private static void EnableOculus()
        {
            PlayerSettings.virtualRealitySupported = true;
            VREditor.SetVREnabledOnTargetGroup(BuildTargetGroup.Android, true);
            VREditor.SetVREnabledDevicesOnTargetGroup(BuildTargetGroup.Android, new[] { "Oculus" });
            AssetDatabase.SaveAssets();
            if (OculusEnabled())
            {
                Debug.Log("[SyncVR] project configured for Oculus Go. Use SyncVR > 2. Build APK.");
            }
            else
            {
                Debug.LogError("[SyncVR] could not enable the Oculus SDK automatically. Open Project Settings > Player > " +
                               "XR Settings (Android tab), tick 'Virtual Reality Supported' and add 'Oculus'.");
            }
        }

        private static bool OculusEnabled()
        {
            return PlayerSettings.virtualRealitySupported &&
                   VREditor.GetVREnabledDevicesOnTargetGroup(BuildTargetGroup.Android).Contains("Oculus");
        }

        [MenuItem("SyncVR/2. Build APK", false, 2)]
        public static void BuildFromMenu()
        {
            if (Build() && !Application.isBatchMode)
                EditorUtility.RevealInFinder(OutputPath);
        }

        public static void BuildFromCommandLine()
        {
            bool ok = Build();
            EditorApplication.Exit(ok ? 0 : 1);
        }

        private static bool Build()
        {
            if (EditorUserBuildSettings.activeBuildTarget != BuildTarget.Android)
                EditorUserBuildSettings.SwitchActiveBuildTarget(BuildTargetGroup.Android, BuildTarget.Android);
            ApplyPlayerSettings();
            EnsureMaterials();
            EnsureScene();
            if (!OculusEnabled())
            {
                Debug.LogError("[SyncVR] Oculus VR is not enabled. Run SyncVR > 1. Configure for Oculus Go first.");
                return false;
            }
            Directory.CreateDirectory(Path.GetDirectoryName(OutputPath));
            var report = BuildPipeline.BuildPlayer(new BuildPlayerOptions
            {
                scenes = new[] { ScenePath },
                locationPathName = OutputPath,
                target = BuildTarget.Android,
                options = BuildOptions.None,
            });
            bool ok = report.summary.result == BuildResult.Succeeded;
            if (ok) Debug.Log("[SyncVR] built " + OutputPath + " (" + report.summary.totalSize / (1024 * 1024) + " MB)");
            else Debug.LogError("[SyncVR] build failed: " + report.summary.result);
            return ok;
        }
    }
}
