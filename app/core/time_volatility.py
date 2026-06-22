# 시간대별 변동성 배율 제공 모듈
# 예시: 새벽 2-6시 1.5x, 오후 10-자정 1.4x, 오전 9-10시 1.2x, 주말 1.3x, 낮 11-17시 0.8x, 기타 1.0x
import datetime

def get_time_volatility_multiplier(now: datetime.datetime = None) -> float:
    if now is None:
        now = datetime.datetime.now()
    hour = now.hour
    weekday = now.weekday()  # 0=월, 6=일
    # 주말
    if weekday >= 5:
        return 1.3
    # 새벽 2-6시
    if 2 <= hour < 6:
        return 1.5
    # 오후 10-자정
    if 22 <= hour or hour < 0:
        return 1.4
    # 오전 9-10시
    if 9 <= hour < 11:
        return 1.2
    # 낮 11-17시
    if 11 <= hour < 17:
        return 0.8
    # 기본
    return 1.0
