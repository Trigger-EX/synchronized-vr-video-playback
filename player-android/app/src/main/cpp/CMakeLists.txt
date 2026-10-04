cmake_minimum_required(VERSION 3.22.1)
project(syncvr_native CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

set(VRAPI_DIR "${CMAKE_CURRENT_SOURCE_DIR}/../../../../third_party/vrapi/VrApi")
if(NOT EXISTS "${VRAPI_DIR}/Include/VrApi.h")
    message(FATAL_ERROR "VrApi SDK missing: run player-android/tools/fetch-vrapi.sh first")
endif()

add_library(vrapi SHARED IMPORTED)
set_target_properties(vrapi PROPERTIES
    IMPORTED_LOCATION "${VRAPI_DIR}/Libs/Android/${ANDROID_ABI}/Release/libvrapi.so"
    INTERFACE_INCLUDE_DIRECTORIES "${VRAPI_DIR}/Include")

add_library(syncvr_native SHARED
    app.cpp
    external_texture.cpp
    gl_util.cpp
    jni_bridge.cpp
    layers.cpp
    sphere_renderer.cpp)
target_compile_options(syncvr_native PRIVATE -Wall -Werror)
target_link_libraries(syncvr_native vrapi android log EGL GLESv3)
