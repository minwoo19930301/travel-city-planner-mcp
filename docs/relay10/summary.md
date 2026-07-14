# 릴리스 증거 감사

결론: **PASS**입니다. 목적은 커밋 `6cfd8a8`의 릴리스 증거와 구현을 읽기 전용으로 확인하는 것입니다.

## 전제조건과 실행 환경

HEAD는 `6cfd8a8a87e4fb62eab552ce59706ac02b03aaaf`입니다.  
작업 트리는 비어 있습니다. 제품 파일 변경은 없습니다.

현재 실행의 `verification.json`이 있습니다.  
`status: pass`, `passed: true`, `authorized: true`입니다.  
검증은 설정된 다섯 명령만 실행한 기록입니다.

## 검증 근거

다섯 명령이 모두 종료값 0으로 통과했습니다.

- `git diff --check`
- `node --check viewer/app.js`
- `../.venv/bin/python -m pytest`: 56개 통과
- `../.venv/bin/python scripts/smoke_mcp.py`
- `npm run test:browser`: 7개 통과

reviewer는 명령을 다시 실행하지 않고 같은 실행의 증거, 소스, 테스트 정의를 읽었습니다.  
69개 여행지와 고유 JPEG 배경을 확인했습니다.  
로컬 Gmarket Sans 300·500·700 WOFF2를 확인했습니다.  
파리 출발, 이동, 빈 도착의 순서와 2,000킬로미터 이상 이동일의 휴식을 확인했습니다.  
구조화된 토큰 오류, 도구 서버 격리, 계획 식별자 검사를 확인했습니다.  
Google Maps의 출발지·도착지와 대중교통·도보·자동차 경로를 확인했습니다.  
뷰어와 독립 내보내기가 `WRONG` 지도 주소를 재생성하는 회귀 정의를 확인했습니다.  
FX 상태, 서울·현지 시계, 기본 회화, 320픽셀 화면, XSS 방어, CI 정의를 확인했습니다.

## 남은 위험과 실패 시 조치

로컬 회귀는 외부 FX 서비스와 Google Maps의 미래 가용성을 보장하지 않습니다.  
Git의 임시 디렉터리 EPERM 진단은 이 읽기 전용 환경의 제한입니다. 제품 실패로 판단하지 않습니다.

검증 명령 하나라도 실패하면 RELEASE를 중단합니다.  
HEAD가 바뀌거나 작업 트리가 더러워지면 이 증거를 폐기합니다.  
깨끗한 체크아웃에서 새 Relay10 실행을 시작합니다.  
실제 결함이면 별도 커밋에서 수정과 회귀를 추가한 뒤 다시 검증합니다.

## 다음 단계

커밋 `6cfd8a8`의 RELEASE를 진행할 수 있습니다.  
현재 `verification.json`과 reviewer 판정을 함께 보존합니다.  
독자 열 명 점검은 문서 명료도만 판단합니다. 검증 결과와 reviewer의 사실 판정을 바꾸지 않습니다.

MCP: 모델 컨텍스트 프로토콜. HTTP: 웹 통신 규약. HTTPS: 암호화된 웹 통신 규약. FX: 환율 기능. CI: 변경 사항 자동 검증. XSS: 악성 스크립트 삽입 공격. EPERM: 권한 부족 진단. PASS: 검증 통과. FAIL: 검증 실패. HEAD: 현재 커밋. JPEG: 사진 파일 형식. MIME: 파일 형식 식별 방식. WOFF2: 웹 글꼴 파일 형식. WRONG: 잘못된 검증값. CSS: 화면 모양을 정하는 언어. DOM: 브라우저 문서 구조. JSON: 구조화 데이터 형식. PR: 변경 검토 요청. LICENSES: 라이선스 파일 폴더. OFL-1: 공개 글꼴 라이선스 버전 표기. HTML-: 웹 문서 형식 접두어. HTML: 웹 문서 형식. API: 프로그램 간 연결 규칙. URL: 웹 주소. UTC: 세계 표준시. KRW: 원화. JPY: 엔화. CLI: 명령줄 도구. CSP: 콘텐츠 보안 정책. CORS: 웹 출처 접근 정책. JS: 자바스크립트. UUID: 고유 식별자. README: 저장소 안내 문서. PYTHON: 파이썬 실행 환경 변수. NO_COLOR: 색상 비활성화 환경 변수. FORCE_COLOR: 색상 강제 환경 변수. READ-ONLY: 읽기 전용. RELEASE: 릴리스. EVIDENCE: 증거. AUDIT: 감사. NOT: 부정 조건. A-: 출발지 표기의 일부.