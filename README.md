# daylaw-auto-bid / keyword-cleaner

네이버 검색광고 계정의 모든 캠페인/광고그룹/키워드를 조회하고,
성과와 등록일을 기준으로 불필요 키워드를 분류·백업한 뒤 선택적으로 삭제하는 도구입니다.

## 현재 안전 규칙

기본값은 아래처럼 보수적으로 잡습니다.

- 모든 캠페인을 이름과 관계없이 조회합니다.
- 기본 실행은 **DRY RUN**이며 아무것도 삭제하지 않습니다.
- 광고그룹명 자체와 `광고그룹명+사기/피해/피해금/팀미션/부업`은 **영구 보호**합니다.
- 키워드 등록 후 **21일 미만이면 무조건 KEEP**입니다.
- 일반 키워드는 최근 **21일 노출 0 + 클릭 0**이어야 삭제 검토 대상이 됩니다.
- 그 상태라도 최근 **90일 안에 노출 또는 클릭 기록이 한 번이라도 있으면 WATCH**로 보존합니다.
- 등록일을 읽을 수 없는 키워드는 안전을 위해 WATCH로 보존합니다.
- 위 보호장치를 모두 통과한 경우만 `DELETE_CANDIDATE`가 됩니다.
- 삭제 전 전체 결과 CSV + 원본 JSON을 저장합니다.
- 실삭제 직전 최근 21일/90일 통계를 다시 조회해 조건을 재검증합니다.
- 실제 삭제는 `--delete --confirm-delete DELETE`를 모두 넣어야만 동작합니다.

## 설정

`.env`에는 API 인증값을 넣고, 정리 기준은 다음 기본값을 사용합니다.

```env
NAVER_API_KEY=...
NAVER_SECRET_KEY=...
NAVER_CUSTOMER_ID=...

RECENT_DAYS=21
MIN_KEYWORD_AGE_DAYS=21
HISTORY_DAYS=90
PROTECTED_SUFFIXES=사기,피해,피해금,팀미션,부업
```

예전 `.env`에 `STATS_DAYS=14`가 남아 있어도 현재 버전은 그 값을 사용하지 않습니다.
`.env`는 `.gitignore`에 포함되어 있으므로 GitHub에 올리지 않습니다.

## 안전 분석

```bash
python run_keyword_cleaner.py
```

결과는 다음 위치에 저장됩니다.

- `data/backups/keyword_scan_YYYYMMDD_HHMMSS.csv`
- `data/backups/keyword_scan_YYYYMMDD_HHMMSS.json`

상태는 세 가지입니다.

- `KEEP`: 핵심 보호 키워드, 등록 21일 미만, 최근 클릭 발생
- `WATCH`: 등록일 불명, 최근 노출 있음, 또는 90일 내 과거 활동 있음
- `DELETE_CANDIDATE`: 핵심 아님 + 등록 21일 이상 + 최근 21일 0/0 + 최근 90일 0/0

## 첫 실삭제 테스트

처음에는 반드시 소량만 삭제합니다.

```bash
python run_keyword_cleaner.py --delete --confirm-delete DELETE --max-delete 20
```

삭제 결과는 `logs/deleted_keywords.csv`에 기록됩니다.

## 전체 후보 삭제

분석 결과와 소량 테스트를 충분히 검토한 뒤에만 사용합니다.

```bash
python run_keyword_cleaner.py --delete --confirm-delete DELETE
```

## 기본 보호 키워드 예시

광고그룹명이 `ABC`라면 아래는 성과와 관계없이 보호됩니다.

- `ABC`
- `ABC사기`, `ABC 사기`
- `ABC피해`, `ABC 피해`
- `ABC피해금`, `ABC 피해금`
- `ABC팀미션`, `ABC 팀미션`
- `ABC부업`, `ABC 부업`

공백/하이픈 등은 정규화해서 비교합니다.
추가 보호 키워드는 `.env`의 `EXTRA_PROTECTED_KEYWORDS`에 쉼표로 넣을 수 있습니다.
