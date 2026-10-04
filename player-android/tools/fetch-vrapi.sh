#!/usr/bin/env bash
# Fetches the VrApi headers and loader from Oculus Mobile SDK 19.0 (VrApi 1.36, the last release
# that supports Oculus Go). Source: the lovr-org/ovr_sdk_mobile mirror, pinned to one commit, with
# every file checked against a SHA-256. The SDK is under the Oculus SDK license and is never
# committed; it lands in player-android/third_party/vrapi/ (git-ignored).
set -euo pipefail

COMMIT=447c81456784bf079498f27eeb487b38557ccbeb
BASE="https://raw.githubusercontent.com/lovr-org/ovr_sdk_mobile/$COMMIT"
DEST="$(cd "$(dirname "$0")/.." && pwd)/third_party/vrapi"

FILES=(
  "35518fc41431e61f4ed737705a65dfadd6e2ec9a54ed2700de4f7c3b66a459db LICENSE.txt"
  "21dc8c8cb5fa595d81336640f2fea79eaa9b8971a0195f3e9851fb1c79c16620 VrApi/Include/VrApi.h"
  "ff43b4e35da87817dcdd54ef508e2216101f7a504c6c1d67d93b57ea6fdf3f68 VrApi/Include/VrApi_Config.h"
  "1a18495e8818bd67ee7a24a7515c6c862521eebd21c1740ee62cdb1c1eee7933 VrApi/Include/VrApi_Helpers.h"
  "036f46f6acd0dc41e90e3d219d51ddefc2bbda26f5a6dc68fd9806b1bbba1e43 VrApi/Include/VrApi_Input.h"
  "7025414dd842103d60f22c371676188cc5cd44193b4c2694d6fda69817453f24 VrApi/Include/VrApi_SystemUtils.h"
  "cf1d0195f43fe40aad28ff45b9a5247ed257a6d452fb04713df8b25da44a6a79 VrApi/Include/VrApi_Types.h"
  "905ad4ee01b4fd0ddd8f007cc62d54a8afd51737ed4a15d4c0391dec7f9684fe VrApi/Include/VrApi_Version.h"
  "14a009a49823ac2a1b345f03eadc6d6fd3d5e40787a8939fe01b2f5dfeb5a0fc VrApi/Include/VrApi_Vulkan.h"
  "6a6cc3e0ff06c682694af6301bd937aed63e8ef09d90d746aa494beebd76a073 VrApi/Libs/Android/armeabi-v7a/Release/libvrapi.so"
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
echo "VrApi 1.36 ready in $DEST"
