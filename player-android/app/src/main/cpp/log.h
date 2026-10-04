// Logging helpers; everything goes to logcat under the "SyncVR" tag.
#pragma once

#include <android/log.h>

#define SYNCVR_TAG "SyncVR"
#define LOGI(...) __android_log_print(ANDROID_LOG_INFO, SYNCVR_TAG, __VA_ARGS__)
#define LOGW(...) __android_log_print(ANDROID_LOG_WARN, SYNCVR_TAG, __VA_ARGS__)
#define LOGE(...) __android_log_print(ANDROID_LOG_ERROR, SYNCVR_TAG, __VA_ARGS__)
