# Module providing a time-of-day volatility multiplier
# Examples: 2-6am 1.5x, 10pm-midnight 1.4x, 9-10am 1.2x, weekend 1.3x, 11am-5pm 0.8x, otherwise 1.0x
import datetime

def get_time_volatility_multiplier(now: datetime.datetime = None) -> float:
    if now is None:
        now = datetime.datetime.now()
    hour = now.hour
    weekday = now.weekday()  # 0=Mon, 6=Sun
    # Weekend
    if weekday >= 5:
        return 1.3
    # 2-6am
    if 2 <= hour < 6:
        return 1.5
    # 10pm-midnight
    if 22 <= hour or hour < 0:
        return 1.4
    # 9-10am
    if 9 <= hour < 11:
        return 1.2
    # 11am-5pm
    if 11 <= hour < 17:
        return 0.8
    # Default
    return 1.0
