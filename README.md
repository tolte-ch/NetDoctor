# NetDoctor

NetDoctor는 DNS 조회, TCP 연결, HTTP 응답 시작 시간(TTFB), 다운로드 처리율,
RTT를 측정해 사용자가 이해할 수 있는 말로 네트워크 상태를 설명하는 프로젝트입니다.

현재 저장소에는 1~2주차 골격·측정 엔진과 **3주차 설명 가능한 분석기**가 구현되어 있습니다.
실제 측정은 반드시 본인이 관리하거나 측정을 허용받은 서버에만 사용해야 합니다.

## 빠른 시작

Python 3.11 이상이 필요합니다. 별도 패키지 설치 없이 저장소 루트에서 실행할 수
있습니다.

```powershell
python -m netdoctor plan
python -m netdoctor demo
python -m netdoctor demo --json
python -m netdoctor --config config/local-test.json measure
python -m netdoctor dashboard --port 8000
```

마지막 명령을 실행한 뒤 <http://127.0.0.1:8000>을 열면 대시보드 초안을 볼 수
있습니다. 종료할 때는 `Ctrl+C`를 누릅니다.

설치형 명령을 사용하고 싶다면 다음과 같이 개발 모드로 설치합니다.

```powershell
python -m pip install -e .
netdoctor plan
```

## 설정

기본 설정과 같은 형식의 편집용 예시는 [`config/default.json`](config/default.json)에
있습니다. `--config`를 생략하면 프로그램에 내장된 동일한 기본값을 사용합니다.

```powershell
python -m netdoctor --config config/default.json plan
```

반복 횟수는 프로젝트 요구사항에 맞춰 5~10회만 허용합니다. `measure` 명령은 실수로
공개 서버를 측정하지 않도록 `--config`를 반드시 요구합니다. 다운로드 URL은 본인 또는
측정을 허용한 서버로 지정하고, 최대 바이트와 최대 시간을 작게 유지하세요.

## 실제 측정

외부 서버를 사용하기 전에 로컬 테스트 서버로 전체 흐름을 확인할 수 있습니다.

첫 번째 PowerShell:

```powershell
python tools/local_test_server.py
```

두 번째 PowerShell:

```powershell
python -m netdoctor --config config/local-test.json measure
python -m netdoctor --config config/local-test.json measure --json
python -m netdoctor --config config/local-test.json measure --json --output results/local.json
```

로컬 설정은 HTTP를 사용하므로 `TLS 연결`이 `unavailable`로 표시되는 것이 정상입니다.
그 외 DNS, TCP, TTFB, 다운로드, RTT는 표본 5개와 요약 통계를 출력해야 합니다.

Codex·IDE 샌드박스나 보안 프로그램이 서로 다른 프로세스 사이의 localhost 연결을
차단한다면, 서버와 측정기를 한 Python 프로세스에서 실행하는 스모크 테스트를 사용합니다.
이 명령은 사용 가능한 임시 포트를 자동으로 선택하므로 별도 서버 창이 필요 없습니다.

```powershell
python tools/local_smoke_test.py
python tools/local_smoke_test.py --json --output results/local-smoke.json
```

실제 허가된 서버를 측정하려면 `config/default.json`을 복사해 대상 호스트, 포트,
HTTP URL, 다운로드 URL을 변경합니다. 핵심 설정은 다음과 같습니다.

```json
{
  "repetitions": 5,
  "timeout_seconds": 5.0,
  "pause_between_runs_seconds": 0.25,
  "download_max_bytes": 5000000,
  "download_max_seconds": 5.0
}
```

결과 상태는 `success`, `partial`, `unavailable`, `failed`로 구분됩니다. 일부 시도가
실패해도 성공한 표본은 보존되고 다음 지표와 다음 반복은 계속 실행됩니다.

측정 뒤에는 중앙값을 기준으로 단일·조합 규칙을 적용해 종합 평가, 주요 병목 후보,
웹 브라우징·1080p·4K 스트리밍·화상회의 적합도를 출력합니다. 각 판정에는 사용한
관측값과 단일 대상·시점 측정의 한계를 함께 표시합니다. `--json` 결과에서는
`analysis` 항목으로 같은 내용을 확인할 수 있습니다.

## 테스트

```powershell
python -m compileall -q netdoctor tests tools
python -m unittest discover -s tests -v
```

자동 테스트에는 외부 인터넷을 사용하지 않는 로컬 TCP·HTTP·다운로드 통합 테스트와
부분 실패 시나리오가 포함됩니다.

## 문서

- [`docs/week1-design.md`](docs/week1-design.md): 측정 정의, 구조, 데이터 계약, 윤리
- [`docs/week2-measurement.md`](docs/week2-measurement.md): 실제 측정 경계와 실패 처리
- [`docs/week3-analysis.md`](docs/week3-analysis.md): 분석 임계값, 조합 규칙, 불확실성
- [`docs/roadmap.md`](docs/roadmap.md): 5주 개발 로드맵과 주차별 완료 조건

## 현재 범위

- [x] 프로젝트 구조와 Python 패키지 구성
- [x] 측정 대상·반복 횟수·타임아웃 설정 및 검증
- [x] 통계와 측정 결과 데이터 모델
- [x] CLI 계획 출력과 명시적인 데모 결과
- [x] 반응형 웹 대시보드 와이어프레임
- [x] 단위 테스트
- [x] 실제 DNS/TCP/TLS/TTFB/다운로드/RTT 측정
- [x] 5~10회 반복, 부분 실패 처리, 패킷 손실률
- [x] 실제 측정 CLI와 JSON 파일 저장
- [x] 근거와 불확실성을 포함한 규칙 기반 진단
- [x] 웹·1080p·4K·화상회의 적합도 평가
- [ ] 실제 측정 API와 대시보드 연결

## 안전 원칙

본인이 권한을 가진 네트워크와 서버만 측정합니다. 학교나 타인의 인프라를 대상으로
부하 시험을 하지 않으며, 패킷 감청이나 개인정보 수집 기능은 만들지 않습니다.
