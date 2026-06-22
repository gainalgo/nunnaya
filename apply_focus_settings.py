# -*- coding: utf-8 -*-
"""
FOCUS 설정 일괄 적용 스크립트 (4대 서버 공용)
- 실행 중인 봇에 안전하게 적용 (POST /config API 사용 — 서버가 스냅샷 자동 백업)
- state(포지션)는 절대 건드리지 않음
- 사용법: 각 서버에서  python apply_focus_settings.py
  (포트가 8010이 아니면: python apply_focus_settings.py 8020)

[2026-06-13 적용 내용 — Claude 분석 기반]
 1) max_daily_sl            100 → 10   : 일일 SL 10회 시 정지 (6/12 SL 20회 출혈 차단)
 2) max_daily_plans         999 → 30   : 일일 진입 상한 (보조 브레이크)
 3) scanner_max_exposure_pct 90 → 70   : 동시 노출 자본 한도 축소
 4) inflection_setup_enabled  → ON     : 변곡 자리 점수 (천장stall 감점/바닥변곡 가점)
 5) retest_setup_enabled      → ON     : 돌파→눌림→지지 가점
 6) final_30m15m_bypass_conviction 55 → 75 : 점수흡수 면제 상향 (가산 인플레 보정)
 7) final_d1_bypass_conviction     50 → 78 : 동일 (API 문서 권장값)
"""
import sys, json, urllib.request, urllib.parse

PORT = sys.argv[1] if len(sys.argv) > 1 else "8010"
BASE = f"http://127.0.0.1:{PORT}/api/strategy/focus"

PATCH = {
    "max_daily_sl": 10,
    "max_daily_plans": 30,
    "scanner_max_exposure_pct": 70,
    "inflection_setup_enabled": "true",
    "retest_setup_enabled": "true",
    "final_30m15m_bypass_conviction": 75,
    "final_d1_bypass_conviction": 78,
}
KEYS = list(PATCH.keys())

def get_config():
    with urllib.request.urlopen(f"{BASE}/config", timeout=10) as r:
        data = json.loads(r.read().decode("utf-8"))
    return data.get("config", data)

def show(cfg, title):
    print(f"\n=== {title} ===")
    for k in KEYS:
        print(f"  {k:35s} = {cfg.get(k)}")

def main():
    try:
        before = get_config()
    except Exception as e:
        print(f"[오류] 봇 접속 실패 ({BASE}) — 포트 확인: {e}")
        sys.exit(1)
    show(before, "변경 전")

    qs = urllib.parse.urlencode(PATCH)
    req = urllib.request.Request(f"{BASE}/config?{qs}", method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            print(f"\n[POST /config] HTTP {r.status} — 서버가 snapshot 자동 백업함")
    except Exception as e:
        print(f"[오류] 설정 적용 실패: {e}")
        sys.exit(1)

    after = get_config()
    show(after, "변경 후")

    ok = all(str(after.get(k)).lower() == str(v).lower() for k, v in {
        "max_daily_sl": 10, "max_daily_plans": 30, "scanner_max_exposure_pct": 70,
        "inflection_setup_enabled": True, "retest_setup_enabled": True,
        "final_30m15m_bypass_conviction": 75, "final_d1_bypass_conviction": 78,
    }.items())
    print("\n결과:", "✅ 7개 항목 모두 적용 확인" if ok else "⚠️ 일부 항목 불일치 — 위 변경 후 값을 확인하세요")

if __name__ == "__main__":
    main()
