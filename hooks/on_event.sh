#!/bin/bash
# =====================================================================
# on_event.sh — Shairport-Sync Session Hook
# =====================================================================
# Called by shairport-sync at session/playback state transitions.
# Writes the current state to a file that the Python controller reads.
#
# Usage (configured in shairport-sync.conf):
#   on_event.sh connected      — AirPlay client connected
#   on_event.sh disconnected   — AirPlay client disconnected
#   on_event.sh playing        — Audio playback started
# =====================================================================

echo "$1" > /tmp/airplay_state
