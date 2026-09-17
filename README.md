# daylaw-auto-bid / keyword-cleaner

네이버 검색광고 `서원데이` 캠페인의 광고그룹과 키워드를 조회하고,
최근 성과 기준으로 불필요 키워드를 분류/백업한 뒤 선택적으로 삭제하는 도구입니다.

## 안전 원칙

- 기본 실행은 **DRY RUN**이며 삭제하지 않습니다.
- 광고그룹명 자체와 `광고그룹명+사기/피해/피해금/팀미션/부업`은 성과가 0이어도 보호합니다.
- 나머지 키워드는 최근 `STATS_DAYS`일의 **완료된 일자** 기준으로 판단합니다.
- 노출 0 + 클릭 0만 `DELETE_CANDIDATE`입니다.
- 삭제 전 CSV + 원본 JSON을 먼저 저장합니다.
- 실제 삭제 시 같은 기간 통계를 즉시 다시 조회하고, 여전히 0/0인 경우에만 삭제합니다.
- 실제 삭제는 `--delete --confirm-delete DELETE`를 모두 입력해야 동작합니다.

## 설치

```bash
pip install -r requirements.txt
cp .env.example .env
```

`.env`에 네이버 검색광고 API 정보를 입력합니다.

```env
NAVER_API_KEY=...
NAVER_SECRET_KEY=...
NAVER_CUSTOMER_ID=...
TARGET_CAMPAIGN_NAME=서원데이
```

`.env`는 `.gitignore`에 포함되어 있으므로 GitHub에 올리지 마세요.

## 1. 안전 분석 실행

```bash
python run_keyword_cleaner.py
```

결과는 다음 위치에 저장됩니다.

- `data/backups/keyword_scan_YYYYMMDD_HHMMSS.csv`
- `data/backups/keyword_scan_YYYYMMDD_HHMMSS.json`

상태:

- `KEEP`: 핵심 보호 키워드 또는 클릭 발생
- `WATCH`: 노출은 있으나 클릭 0
- `DELETE_CANDIDATE`: 보호 키워드가 아니며 최근 기간 노출 0 + 클릭 0

## 2. 소량 실삭제 테스트

처음 실삭제는 반드시 개수를 제한해서 테스트하세요.

```bash
python run_keyword_cleaner.py --delete --confirm-delete DELETE --max-delete 20
```

## 3. 전체 후보 삭제

분석 결과를 충분히 검토한 뒤에만 사용합니다.

```bash
python run_keyword_cleaner.py --delete --confirm-delete DELETE
```

삭제 결과는 `logs/deleted_keywords.csv`에 기록됩니다.

## 기본 보호 키워드

광고그룹명이 `ABC`라면 아래는 항상 보호됩니다.

- `ABC`
- `ABC사기`, `ABC 사기`
- `ABC피해`, `ABC 피해`
- `ABC피해금`, `ABC 피해금`
- `ABC팀미션`, `ABC 팀미션`
- `ABC부업`, `ABC 부업`

공백/하이픈 등은 정규화해서 비교합니다.
추가 보호 키워드는 `.env`의 `EXTRA_PROTECTED_KEYWORDS`에 쉼표로 넣을 수 있습니다.

## 참고

네이버 검색광고 API 공식 Python 샘플의 인증 방식
(`X-Timestamp`, `X-API-KEY`, `X-Customer`, `X-Signature`)과
`/ncc/campaigns`, `/ncc/adgroups`, `/ncc/keywords`, `/stats`,
`DELETE /ncc/keywords/{keywordId}` 구조를 사용합니다.
