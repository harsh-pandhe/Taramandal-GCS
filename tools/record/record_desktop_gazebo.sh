#!/usr/bin/env bash
# tools/record/record_desktop_gazebo.sh
# Automated screen recording harness for ROS 2 / Gazebo Harmonic / PX4 SITL multi-drone runs.
#
# Usage:
#   ./tools/record/record_desktop_gazebo.sh [OUTPUT_NAME] [DURATION_SECONDS] [DISPLAY_ID]
#
# Example:
#   ./tools/record/record_desktop_gazebo.sh gazebo_flight_3drones 30 :0.0

set -euo pipefail

OUTPUT_NAME="${1:-gazebo_multi_drone_flight}"
DURATION="${2:-30}"
TARGET_DISPLAY="${3:-:0.0}"
RECORDINGS_DIR="docs/recordings"
mkdir -p "${RECORDINGS_DIR}"

OUT_MP4="${RECORDINGS_DIR}/${OUTPUT_NAME}.mp4"

echo "=========================================================="
echo "  Taramandal Automated Desktop / Gazebo Screen Recorder"
echo "=========================================================="
echo "Target Display:    ${TARGET_DISPLAY}"
echo "Duration:          ${DURATION} seconds"
echo "Output File:       ${OUT_MP4}"
echo "=========================================================="

if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "ERROR: ffmpeg is not installed or not in PATH."
    exit 1
fi

# Detect display geometry or fallback to 1920x1080
RESOLUTION="1920x1080"
if command -v xdpyinfo >/dev/null 2>&1; then
    DETECTED=$(xdpyinfo -display "${TARGET_DISPLAY}" 2>/dev/null | awk '/dimensions:/ {print $2}' || true)
    if [ -n "${DETECTED}" ]; then
        RESOLUTION="${DETECTED}"
    fi
fi

echo "[Recorder] Recording screen at ${RESOLUTION} for ${DURATION}s..."

# Use hardware/software x11grab with smooth 30fps and libx264
ffmpeg -y \
    -video_size "${RESOLUTION}" \
    -framerate 30 \
    -f x11grab \
    -draw_mouse 1 \
    -i "${TARGET_DISPLAY}" \
    -t "${DURATION}" \
    -c:v libx264 \
    -preset veryfast \
    -pix_fmt yuv420p \
    -movflags +faststart \
    "${OUT_MP4}"

echo "[Recorder] Done! High-definition video saved to ${OUT_MP4}"
