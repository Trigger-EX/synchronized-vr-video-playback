#!/usr/bin/env bash
# Fetches the VrApi headers and loader from Oculus Mobile SDK 15.0 (VrApi 1.32). The Go's final OS
# ships VrApi 1.1.35 and its system driver refuses newer loaders ("Oculus Update Required"), so the
# loader must be <= 1.35; 1.32 is the newest such release in the mirror. Source: the lovr-org/ovr_sdk_mobile mirror, pinned to one commit, with
# every file checked against a SHA-256. The SDK is under the Oculus SDK license and is never
# committed; it lands in player-android/third_party/vrapi/ (git-ignored).
set -euo pipefail

COMMIT=f88e937390700ce7e8c0c8bc3f15edcbdda4bab4
BASE="https://raw.githubusercontent.com/lovr-org/ovr_sdk_mobile/$COMMIT"
DEST="$(cd "$(dirname "$0")/.." && pwd)/third_party/vrapi"

FILES=(
  "872477e75bb7bb2fc9b563ebbe456f01988b056f7b953b9561d25b6d7f682e3b LICENSE.txt"
  "080224e7489d84d2c62dfb0d6f47c53ffc5fab044e014f4eca630aef94c3c8f4 VrApi/Include/VrApi.h"
  "ff43b4e35da87817dcdd54ef508e2216101f7a504c6c1d67d93b57ea6fdf3f68 VrApi/Include/VrApi_Config.h"
  "c8c5d696af03f618ce5e249b920950d42d77ef9e500614d858ed21943277aa04 VrApi/Include/VrApi_Helpers.h"
  "3a62b16349d7a613648a055a1e24b6a70d66e27765fffc995f88eac55272caee VrApi/Include/VrApi_Input.h"
  "7025414dd842103d60f22c371676188cc5cd44193b4c2694d6fda69817453f24 VrApi/Include/VrApi_SystemUtils.h"
  "38d73c95ec753ff733538e08904898ec3c15d3c0adb2132134a10b59517694f2 VrApi/Include/VrApi_Types.h"
  "b785dc11c699c3127639c1188a484bbba734a51fbd59d83fa441ed4bf3f0e46e VrApi/Include/VrApi_Version.h"
  "14a009a49823ac2a1b345f03eadc6d6fd3d5e40787a8939fe01b2f5dfeb5a0fc VrApi/Include/VrApi_Vulkan.h"
  "2096d08bea6ef02bdaa3a495fab1eb1a522b5f4d974be79cfbb88dd407095046 VrApi/Libs/Android/armeabi-v7a/Release/libvrapi.so"
)

for entry in "${FILES[@]}"; do
  sha="${entry%% *}"; path="${entry#* }"; out="$DEST/$path"
  if [ -f "$out" ] && echo "$sha  $out" | sha256sum -c --status; then continue; fi
  mkdir -p "$(dirname "$out")"
  curl -fsSL --retry 3 -o "$out.part" "$BASE/$path"
  if ! echo "$sha  $out.part" | sha256sum -c --status; then
    echo "error: checksum mismatch for $path" >&2
    rm -f "$out.part"
    exit 1
  fi
  mv "$out.part" "$out"
done
echo "VrApi 1.32 ready in $DEST"
