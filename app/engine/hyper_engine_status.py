# ============================================================
# File: app/engine/hyper_engine_status.py
# Autocoin OS v3-H — Engine Status (Final / Fixed)
# ============================================================

class EngineStatus:
    def __init__(self, name: str):
        self.name = name
        self.is_active = False
        self.last_signal = None
        self.last_price = None
        self.error = None

    # -----------------------------
    # Engine start
    # -----------------------------
    def start(self):
        self.is_active = True
        self.error = None

    # -----------------------------
    # Engine stop
    # -----------------------------
    def stop(self):
        self.is_active = False

    # -----------------------------
    # Status update on tick
    # -----------------------------
    def update(self, signal=None, price=None):
        if signal is not None:
            self.last_signal = signal
        if price is not None:
            self.last_price = price

    # -----------------------------
    # Error handling
    # -----------------------------
    def set_error(self, message: str):
        self.error = message
        self.is_active = False
