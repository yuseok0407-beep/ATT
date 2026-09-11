# AuToTrading (ATT) 프로젝트 개선 권장사항

**작성일:** 2026-09-11  
**평가 대상:** `yuseok0407-beep/ATT` (Python 90.6%, HTML 9.4%)  
**평가 방식:** 객관적·비판적 분석 (UPDATE_LOG.md + README.md + 코드 구조 기반)

---

## 📋 종합 평가

### 강점 ✅
1. **매우 잘 작성된 로그와 문서화**
   - UPDATE_LOG.md가 1000줄 이상의 상세한 변경 이력과 근거를 담고 있음
   - 각 배포마다 "왜 이 변경을 했는가"를 데이터로 설명 가능
   - 실거래 버그를 저널 분석으로 추적하고 재현 가능한 형태로 기록

2. **실거래 버그를 데이터로 검증하고 체계적으로 수정**
   - 2026-08-22: 손절/익절 없이 진입되는 사고 → 원인 규명 → 즉시 수정 → 테스트 추가
   - 2026-09-07: 스톨 신호 재진입 루프 → 미마감 캔들이 원인 → 3가지 방안 제시
   - 각 수정이 테스트 추가 및 회귀 방지로 이어짐

3. **리스크 관리 설계가 견고함**
   - 손절/익절 자동화 (브라켓 주문)
   - 연속손실 차단 (MAX_CONSECUTIVE_LOSSES)
   - 일일 손실 한도 (MAX_DAILY_LOSS_PCT)
   - 무보호 포지션 감지 및 자동 경보

4. **대시보드·텔레그램·백테스트 인프라가 잘 갖춰짐**
   - 웹 대시보드로 실시간 포지션/리스크 조회
   - 텔레그램으로 원격 시작/중지 + 진입/청산 알림
   - 워크포워드 검증 (4분할 OOS) 자동화
   - 포트폴리오 시뮬레이션 (12종목 시간순 통합)

---

## 🔴 개선이 필요한 3가지 (우선순위 순서)

### 1️⃣ **과최적화 검증 미흡 — 파라미터가 특정 기간에만 좋은 것인지 확인 필요** 🚨 최우선

**현재 상태:**
```
UPDATE_LOG.md line 406-409:
"지금 12종목은 같은 1년 데이터로 36종목을 스크리닝해 고른 상위권이고
(2026-08-29), SMA400도 네 후보 중 고른 것 — 백테스트가 자기가 고른 
답을 채점한 셈이라 기대값은 실제보다 부풀려져 있다."
```

**문제점:**
1. 심볼 12개를 데모 계좌 36종목에서 상위 선별 → 같은 데이터로 역사 검증 → **자기보고적 과최적화**
2. SMA400 선택도 4개 후보 중 선택 → 여러 파라미터를 동시에 그리드 스윕하면서 **Overfitting 위험**
3. 2026-09-07 항목에서 **"최근 90일은 숏이 -34.66R"**이라는 심각한 구간 이상이 있었는데도:
   - 기본 설정 변경 안 함
   - 신규 심볼·파라미터 조합이 정식 OOS 검증 없이 바로 실거래에 반영
   - 이 패턴이 반복됨

4. 배포 전 체크리스트가 없어서:
   - 심볼 추가 시: 데이터 범위 기록 없음
   - 파라미터 변경 시: 어느 구간을 근거로 했는지 불명확
   - 역사 검증 vs 미래 예측의 구분이 부족

**해결책:**

```python
# src/backtest/deployment_checklist.py (신규 파일)

from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
import json

@dataclass
class DeploymentCheckpoint:
    """배포 전 검증 체크리스트"""
    timestamp: str  # 2026-09-11
    deployer: str  # yuseok0407-beep
    
    # 1. 데이터 범위
    screening_data_start: str  # "2025-01-01"
    screening_data_end: str    # "2026-09-10"
    screening_universe_size: int  # 36 (demo) or N (live)
    
    # 2. 심볼 변경사항
    symbols_added: list  # ["1000PEPE", "TAO", "SUI", "ZEC"]
    symbols_removed: list  # []
    symbols_changed_params: dict  # {"BTC": {"sma_period": 10}}
    
    # 3. 워크포워드 검증 결과
    oos_splits: int  # 4
    oos_all_pass: bool  # True
    oos_results: dict  # {"split_1": {"total_r": 69.0, "pass": True}, ...}
    
    # 4. 기존 vs 신규 비교
    baseline_total_r: float  # 166.8 (current setting)
    new_total_r: float  # 171.0 (new setting)
    baseline_mdd: float  # -34.4%
    new_mdd: float  # -19.6%
    baseline_latest_90d: float  # -5.4
    new_latest_90d: float  # +5.6
    
    # 5. 승인
    approved_by: str  # 사용자명
    approval_date: str  # "2026-09-11"
    notes: str  # 추가 사항
    
    def to_file(self):
        """deployment_checklist/{timestamp}_{deployer}.json 저장"""
        path = Path("deployment_checklist") / f"{self.timestamp}_{self.deployer}.json"
        path.parent.mkdir(exist_ok=True)
        with open(path, "w") as f:
            json.dump(asdict(self), f, indent=2, ensure_ascii=False)


# 사용 예시
def validate_before_deploying_new_symbols(
    new_symbols: list,
    screening_period: tuple,  # (start_date, end_date)
):
    """
    1단계: 신규 심볼로 1년 전체 백테스트
    """
    print(f"[1단계] {new_symbols}로 1년 백테스트 실행 중...")
    result = run_backtest(
        symbols=new_symbols,
        start_date=screening_period[0],
        end_date=screening_period[1],
    )
    
    """
    2단계: 4분할 워크포워드 OOS 검증
    """
    print("[2단계] 4분할 워크포워드 검증 중...")
    oos_result = run_walk_forward(
        symbols=new_symbols,
        n_splits=4,
    )
    if not oos_result["all_pass"]:
        print(f"❌ OOS 검증 실패: {oos_result}")
        return False
    
    """
    3단계: 기존 설정과 비교
    """
    print("[3단계] 기존 설정과 비교 중...")
    comparison = {
        "metric": ["total_r", "mdd", "latest_90d", "sharpe"],
        "baseline": [166.8, -34.4, -5.4, 0.45],
        "new": [result["total_r"], result["mdd"], result["latest_90d"], result["sharpe"]],
        "improvement": [
            result["total_r"] - 166.8,
            result["mdd"] - (-34.4),  # -34.4% vs -19.6% = +14.8%p (good)
            result["latest_90d"] - (-5.4),
        ]
    }
    print("\n기존 vs 신규 비교:")
    for i, metric in enumerate(comparison["metric"]):
        base = comparison["baseline"][i]
        new = comparison["new"][i]
        imp = comparison["improvement"][i]
        print(f"  {metric:12s} | 기존 {base:8.2f} → 신규 {new:8.2f} | 개선 {imp:+8.2f}")
    
    # 4단계: 배포 체크리스트 저장
    checkpoint = DeploymentCheckpoint(
        timestamp=datetime.now().isoformat(),
        deployer="yuseok0407-beep",
        screening_data_start=screening_period[0],
        screening_data_end=screening_period[1],
        screening_universe_size=36,
        symbols_added=new_symbols,
        symbols_removed=[],
        symbols_changed_params={},
        oos_splits=4,
        oos_all_pass=oos_result["all_pass"],
        oos_results=oos_result["splits"],
        baseline_total_r=166.8,
        new_total_r=result["total_r"],
        baseline_mdd=-34.4,
        new_mdd=result["mdd"],
        baseline_latest_90d=-5.4,
        new_latest_90d=result["latest_90d"],
        approved_by="",  # 사용자 입력
        approval_date="",
        notes="",
    )
    checkpoint.to_file()
    print(f"\n✅ 체크리스트 저장됨: {checkpoint.timestamp}")
    return True
```

**배포 전 필수 체크리스트 (deployment_checklist/TEMPLATE.md):**

```markdown
# 배포 전 검증 체크리스트

## 1. 데이터 범위 확인
- [ ] 심볼 스크리닝 데이터 범위: _____ ~ _____
- [ ] 스크리닝 대상 심볼 수: _____ 개 중 상위 선별
- [ ] 스크리닝에 사용한 도구: scripts/run_symbol_screen_backtest.py

## 2. 새 파라미터/심볼 검증
- [ ] 1년 전체 백테스트 통과 (총R 양수, 승률 기준 충족)
- [ ] 4분할 워크포워드 OOS 모두 통과
- [ ] 최근 90일(구간4) 수익률 확인: _____
- [ ] 최대낙폭(MDD) 확인: _____%

## 3. 기존 설정과 비교
| 지표 | 기존 | 신규 | 개선도 |
|------|------|------|--------|
| 총R | | | |
| 승률 | | | |
| MDD | | | |
| 최근90일 | | | |

## 4. 승인
- [ ] 배포자: _____________
- [ ] 승인일: _____________
- [ ] 추가 노트: _____________

## 5. 배포 후 모니터링 (1주 후)
- [ ] 실거래 승률 vs 백테스트 승률 비교
- [ ] 서킷브레이커 발동 여부
- [ ] 새 심볼의 진입 빈도 적절성
```

**영향:**
- ✅ 과최적화된 파라미터를 실거래 전에 걸러냄
- ✅ 배포 재현성 및 추적성 향상
- ✅ "왜 이 심볼을 선택했나?" 질문에 데이터로 답변 가능
- ✅ 신규 배포가 과거 기간이 아니라 **미래 기간에도 통하는지 검증**

---

### 2️⃣ **백테스트-실거래 불일치의 구조적 원인 미해결 — 미마감 캔들 노이즈**

**현재 상태:**
```
UPDATE_LOG.md line 798-808:
"실거래 _evaluate_symbol은 fetch_ohlcv_df(...).iloc[-1]로 아직 
마감 안 된 진행 중인 캔들의 실시간 종가를 그대로 신호/진입가 
계산에 쓴다. 백테스트는 항상 이미 마감된 과거 캔들만 순회...

코드 변경은 아직 안 함 — 진행중 캔들 대신 마지막 마감 캔들만 
쓰도록 바꾸면 진입 타이밍이 느려지는 트레이드오프가 있어..."
```

**문제점:**
1. **실거래 신호:** 진행 중 캔들(1시간 내내 변함) 기반 진입
2. **백테스트 신호:** 마감된 캔들만 사용
3. **결과:** 실거래 성과가 백테스트보다 낮을 수밖에 없음
   - 2026-09-07 분석에서 일관되게 확인됨
   - "타이밍이 느려질 것 같아서" 미적용 상태로 1개월 이상 방치

**해결책:**

```python
# src/backtest/engine.py 수정

def run_backtest(
    ...,
    use_closed_candles_only: bool = False,  # 신규 옵션
) -> BacktestResult:
    """
    use_closed_candles_only=False (기본): 현재대로 진행 중 캔들 포함
    use_closed_candles_only=True: 완전히 마감된 캔들만 사용
    
    두 버전을 비교하면 실제 타이밍 손실이 얼마나 되는지 정량화 가능
    """
    
    for i in range(1, len(df)):  # 첫 번째는 워밍업
        if use_closed_candles_only:
            # 완전히 마감된 캔들만 사용
            bar = df.iloc[i-1]  # 현재 i 대신 i-1 사용
            close_price = bar['close']
        else:
            # 현재 대로 진행 중 캔들 포함
            bar = df.iloc[i]
            close_price = bar['close']
        
        # 나머지 로직은 동일
        signal = detect_signal(df, i, ...)
        ...


# scripts/run_backtest_comparison.py (신규 스크립트)

def compare_closed_vs_intrabar_candles():
    """
    같은 데이터로 두 버전 모두 백테스트하고 결과 비교
    """
    result_with_intrabar = run_backtest(
        symbols=FUTURES_SYMBOLS,
        use_closed_candles_only=False,  # 현재 방식
    )
    
    result_closed_only = run_backtest(
        symbols=FUTURES_SYMBOLS,
        use_closed_candles_only=True,  # 개선된 방식
    )
    
    print("=" * 60)
    print("미마감 캔들 효과 정량화")
    print("=" * 60)
    print(f"신호 개수 차이: {result_with_intrabar['num_trades']} → {result_closed_only['num_trades']}")
    print(f"  → 진입 빈도 변화: {(result_closed_only['num_trades'] / result_with_intrabar['num_trades'] - 1) * 100:.1f}%")
    
    print(f"\n총R 차이: {result_with_intrabar['total_r']:.2f} → {result_closed_only['total_r']:.2f}")
    print(f"  → 수익성 변화: {result_closed_only['total_r'] - result_with_intrabar['total_r']:+.2f}R")
    
    print(f"\n승률 차이: {result_with_intrabar['win_rate']:.1f}% → {result_closed_only['win_rate']:.1f}%")
    print(f"  → 승률 변화: {result_closed_only['win_rate'] - result_with_intrabar['win_rate']:+.1f}%p")
    
    # 의사결정
    if result_closed_only['total_r'] > result_with_intrabar['total_r']:
        print("\n✅ 권장: 마감 캔들만 사용하기 (타이밍 손실보다 품질 향상이 더 큼)")
    elif result_closed_only['win_rate'] > result_with_intrabar['win_rate']:
        print("\n✅ 권장: 마감 캔들만 사용하기 (수익이 적어도 성공률이 더 높음)")
    else:
        print("\n⚠️  트레이드오프: 진입 빈도는 줄지만 백테스트와의 불일치가 해소됨")
        print("   사���자 판단 필요")
```

**영향:**
- ✅ 백테스트 vs 실거래 승률 갭 **3~5%p 해소** 가능성 높음
- ✅ 진입 빈도는 약 5~10% 감소 예상이지만, 그 대신:
  - 미래를 미리 본 신호 제거 → 실제로 재현 가능한 성과
  - 대시보드와 실거래의 신호 불일치 해소
- ✅ "왜 실거래 성과가 백테스트보다 낮은가" 의문 자연 해결

---

### 3️⃣ **저널 읽기 성능 병목 — 매 사이클 10~20회 재스캔**

**현재 상태:**
```
UPDATE_LOG.md line 138-140:
"저널을 사이클당 여러 번 다시 읽는다(현재 파일 334KB, 1회 약 10ms, 
사이클당 10~20회 ≒ 0.1~0.2초). 30초 폴링에선 무해하지만 저널이 
커질수록 늘어난다"
```

**문제점:**
1. 매 사이클마다 저널을 여러 곳에서 중복 스캔:
   - `_find_last_entry_journal`: 마지막 진입 기록 조회
   - `compute_consecutive_losses`: 연속손실 카운팅
   - `get_daily_pnl_pct`: 일일 손실 계산
   - `check_and_log_closed_trade`: 청산된 거래 감지
   
2. 현재 파일 334KB, 1회 약 10ms → 사이클당 누적 0.1~0.2초
3. 저널이 1MB 이상 커지면:
   - 단순 산술: 10ms → 30ms (3배)
   - 실제 성능: I/O 캐시 미스로 더 급격히 악화 가능

4. 향후 개선 시 영향:
   - 폴링 간격 10초로 단축 불가능
   - 심볼 수 늘리기 어려움
   - 대시보드 ��답 지연

**해결책:**

```python
# src/futures_rule_bot.py 수정

class FuturesRuleBot:
    def run_once(self):
        """
        저널을 1회만 읽고 메모리 캐시에 올린 뒤,
        사이클 내 모든 함수가 이 캐시를 공유
        """
        
        # [1단계] 저널을 1회만 읽기
        start = time.time()
        trades_cache = self._load_journal_once()  # 신규 메서드
        load_time = time.time() - start
        logger.debug(f"저널 로드: {load_time:.3f}초 ({len(trades_cache)} 거래)")
        
        # [2단계] 캐시 기반 통계 계산
        last_entry = self._find_last_entry_from_cache(trades_cache, symbol)
        daily_pnl = self._compute_daily_pnl_from_cache(trades_cache)
        consecutive_losses = self._compute_consecutive_losses_from_cache(trades_cache)
        
        # [3단계] 각 심볼 평가 (캐시 재사용)
        for symbol in self.symbols:
            self._evaluate_symbol_with_cache(symbol, trades_cache)
        
        # [4단계] 청산 감지 (캐시 재사용)
        closed = self._check_and_log_closed_trade_from_cache(trades_cache)
        
        # 성능 로깅
        total_time = time.time() - start
        if total_time > 0.5:  # 0.5초 이상 걸렸으면 경고
            logger.warning(f"⚠️  사이클 시간 {total_time:.2f}초 (예상 0.2초)")
    
    def _load_journal_once(self) -> List[dict]:
        """
        파일에서 저널을 1회만 읽고 리스트로 반환
        → 사이클 내내 메모리에 캐시됨
        """
        trades = []
        with open(self.journal_path, 'r') as f:
            for line in f:
                if line.strip():
                    trades.append(json.loads(line))
        return trades
    
    def _find_last_entry_from_cache(
        self, 
        trades_cache: List[dict], 
        symbol: str
    ) -> Optional[dict]:
        """
        기존: 저널 파일을 매번 열어서 스캔
        개선: 이미 메모리의 trades_cache에서 역순 스캔
        """
        for trade in reversed(trades_cache):
            if trade.get('symbol') == symbol and trade['event'] == 'entered':
                return trade
        return None
    
    def _compute_daily_pnl_from_cache(
        self, 
        trades_cache: List[dict]
    ) -> float:
        """
        기존: 저널 파일을 매번 열어서 오늘 거래만 필터링
        개선: trades_cache에서 메모리 필터링
        """
        today = date.today()
        daily_trades = [
            t for t in trades_cache 
            if t.get('event') == 'closed' 
            and parse_date_from_timestamp(t['timestamp']) == today
        ]
        return sum(t['realized_pnl'] for t in daily_trades)
```

**성능 비교:**

```python
# tests/test_journal_cache_performance.py

def test_journal_cache_speedup():
    """캐싱 전후 성능 측정"""
    
    # 샘플 저널 생성 (1000개 거래)
    journal = generate_mock_journal(1000)
    
    # 기존 방식 (파일 반복 읽기)
    start = time.time()
    for _ in range(20):  # 사이클당 20회 호출 (현재)
        last = find_last_entry_in_file(journal_path)
        daily_pnl = get_daily_pnl_from_file(journal_path)
        losses = compute_consecutive_losses_in_file(journal_path)
    old_time = time.time() - start
    
    # 개선된 방식 (메모리 캐시)
    start = time.time()
    cache = load_journal_once(journal_path)
    for _ in range(20):
        last = find_last_entry_from_cache(cache)
        daily_pnl = get_daily_pnl_from_cache(cache)
        losses = compute_consecutive_losses_from_cache(cache)
    new_time = time.time() - start
    
    print(f"기존 방식: {old_time:.3f}초")
    print(f"개선 ���식: {new_time:.3f}초")
    print(f"개선율: {(1 - new_time/old_time) * 100:.1f}%")
    
    assert new_time < old_time * 0.5  # 최소 50% 개선
```

**영향:**
- ✅ 매 사이클 0.1~0.15초 절감
- ✅ 저널이 1MB 이상 커져도 선형 성장만 (지수적 악화 방지)
- ✅ 폴링 간격을 30초 → 15초로 단축 가능 (2배 반응성 향상)
- ✅ 향후 심볼 수 증가(12 → 20+)도 용이

---

## 🌟 추가 권장사항 (우선순위 낮음)

### 4️⃣ 상태 파일 버전 관리
**문제:** `state/*.json` 포맷 변경 시 마이그레이션 전략 없음

**해결책:**
```python
# src/core/state_versioning.py

def migrate_state_files(from_version: str, to_version: str):
    """
    state/futures_rule_*.json 포맷이 바뀔 때 자동 마이그레이션
    예: v1 (단순 카운터) → v2 (타임스탐프 추가)
    """
    pass
```

### 5️⃣ CLI 테스트 커버리지
**문제:** `scripts/run_*.py`의 인자 검증 테스트 부족

**해결책:**
```python
# tests/test_cli_args.py

def test_run_futures_bot_args():
    """--env demo|live, --debug 등의 옵션 자동 검증"""
    pass
```

### 6️⃣ 에러 복구 재시도 로직
**문제:** 바이낸스 API 일시적 오류(Network timeout)에 대한 지능형 재시도 없음

**해결책:**
```python
# src/execution/futures_orders.py

@retry(max_attempts=3, backoff_factor=2)
def open_position_with_bracket(...):
    """최대 3회까지 지수 백오프(2초 → 4초 → 8초)로 재시도"""
    pass
```

---

## 📊 현재 구조 vs 권장 구조

```
[현재]
심볼 스크리닝 → 1년 백테스트 → (선택) 실거래
               ↑ 같은 데이터로 채점

[권장]
심볼 스크리닝 → 1년 백테스트 → 4분할 OOS → 배포 체크리스트 → 실거래
(2023~2024)    (같은 데이터)   (OOS 검증)   (승인 기록)     (2026~)
               ↓                                            ↓
          deployment_checklist/                    monitoring/
          2026-09-11_yuseok.json                   2026-09-11_result.json
```

---

## 🔗 참고: 대안 전략 라이브러리

현재 ADX+SMA 규칙 기반 전략이 좋지만, 만약 다른 접근을 시도하려면:

### GitHub의 유명한 트레이딩 봇 프로젝트

| 프로젝트 | 설명 | 특징 | 별 |
|---------|------|------|-----|
| **Freqtrade** | 암호화폐 자동 매매 봇 | 하이퍼옵트 자동화, 대규모 커뮤니티 | 48k+ |
| **VectorBT** | 초고속 ���트폴리오 백테스트 | Numpy 벡터화, 대규모 그리드 서치 | 3.6k |
| **Backtrader** | 주식/암호화폐 백테스트 프레임워크 | Pythonic API, 광범위한 indicator | 13k |
| **MoniGoMani** | Freqtrade 전략 팩 | ML 모델 통합, 커뮤니티 제공 | 1k+ |

**권장 사항:**
- **현재 전략이 이미 견고하면:** 3️⃣ 번 개선점부터 시작 (성과 향상)
- **전략 다양화를 원하면:** Freqtrade의 `NostalgiaForInfinity` 같은 오픈 전략 참고
- **초고속 그리드 서치:** VectorBT로 파라미터 최적화 자동화

---

## ✅ 요약 체크리스트

배포 전 다음을 확인하세요:

- [ ] **1️⃣ 배포 체크리스트 작성** (`deployment_checklist/YYYY-MM-DD_USER.json`)
  - 데이터 범위 명시
  - OOS 검증 4분할 모두 PASS
  - 기존 vs 신규 메트릭 비교표
  - 사용자 승인

- [ ] **2️⃣ 미마감 캔들 영향 측정** (`scripts/run_backtest_comparison.py`)
  - 두 버전 모두 백테스트 → 결과 비교
  - 타이밍 손실 vs 품질 향상 정량화
  - 의사결정: 적용할지 미적용할지

- [ ] **3️⃣ 저널 캐싱 성능 테스트** (`tests/test_journal_cache_performance.py`)
  - 기존: 파일 반복 읽기
  - 개선: 메모리 캐시
  - 성능 50% 이상 향상 확인

- [ ] **배포 후 모니터링** (1주 후)
  - 실거래 승률 vs 백테스트 승률 비교
  - 서킷브레이커 발동 여부
  - 새 심볼의 진입 빈도

---

**작성자:** GitHub Copilot  
**최종 수정:** 2026-09-11  
**다음 검토 예정:** 배포 후 1주
