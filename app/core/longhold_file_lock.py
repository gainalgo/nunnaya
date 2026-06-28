# ============================================================
# File: app/core/longhold_file_lock.py
# Single gateway — file access lock for longhold_config.json
#
# [2026-03-15] strategy_plugins and ladder_manager used different
# locks, risking file corruption on concurrent writes.
# This module provides the sole lock, serializing all access.
# ============================================================
import threading

# Global single lock — used for all reads/writes of longhold_config.json
longhold_file_lock = threading.RLock()
