// JNI helpers shared by the native files.
#pragma once

#include <jni.h>

#include "log.h"

namespace syncvr {

// Returns true (and logs plus clears the exception) if a Java exception is pending.
inline bool JniFailed(JNIEnv* env, const char* what) {
    if (env->ExceptionCheck()) {
        LOGE("JNI exception during %s", what);
        env->ExceptionDescribe();
        env->ExceptionClear();
        return true;
    }
    return false;
}

}  // namespace syncvr
