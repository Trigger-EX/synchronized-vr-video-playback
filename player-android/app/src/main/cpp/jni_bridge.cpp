#include <jni.h>

#include "VrApi.h"

extern "C" JNIEXPORT jstring JNICALL
Java_com_syncvr_player_NativeBridge_vrApiVersion(JNIEnv* env, jclass) {
    return env->NewStringUTF(vrapi_GetVersionString());
}
