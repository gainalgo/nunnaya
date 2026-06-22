# ============================================================
# File: app/core/longhold_file_lock.py
# 단일 게이트웨이 — longhold_config.json 파일 접근 락
#
# [2026-03-15] strategy_plugins와 ladder_manager가 서로 다른 락을
# 사용하여 동시 쓰기 시 파일 손상 위험이 있었음.
# 이 모듈이 유일한 락을 제공하여 모든 접근을 직렬화.
# ============================================================
import threading

# 전역 단일 락 — longhold_config.json 읽기/쓰기 모두 이 락을 사용
longhold_file_lock = threading.RLock()
