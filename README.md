# 📈 Stock News Watch (주식 뉴스 감시 & 메일 알림)

관심 있는 주식 티커(최대 10개)를 등록해 두면, **CNBC · Yahoo Finance · Seeking Alpha**
에 새 뉴스가 올라올 때마다 **링크를 이메일로 보내주는** 자동 도구입니다.
메일은 폰에서 열고 링크를 눌러 바로 원문을 확인할 수 있습니다.

- 실행: **GitHub Actions** (클라우드) — 내 PC가 꺼져 있어도 30분마다 자동 확인
- 알림: **Gmail SMTP** 로 메일 발송 → `leejh5709@cu911.com` (또는 원하는 주소)로 수신
- 새 뉴스만 골라서 보냄 (이미 본 뉴스는 `seen.json` 에 기록되어 중복 발송 안 함)

---

## 동작 방식 (요약)

1. `tickers.txt` 에 적힌 티커들을 읽습니다 (최대 10개).
2. 티커마다 Google News RSS를 CNBC / Yahoo Finance / Seeking Alpha 도메인으로
   필터링해 최신 기사 링크를 가져옵니다.
3. `seen.json` 과 비교해 **처음 보는 기사**만 골라냅니다.
4. 새 기사가 있으면 티커별로 묶어 HTML 메일로 보냅니다.
5. 본 기사를 `seen.json` 에 기록하고 레포에 다시 커밋합니다.

> **첫 실행은 메일을 보내지 않습니다.** 현재 떠 있는 기사들을 "기준선(baseline)"으로
> 기록만 하고, 그 이후 실행부터 *새로 뜬* 기사만 메일로 보냅니다.

---

## 설치 & 설정 (처음 한 번만)

### 1) Gmail 앱 비밀번호 만들기

일반 비밀번호가 아니라 **앱 비밀번호**가 필요합니다.

1. Gmail 계정에 **2단계 인증(2-Step Verification)** 을 먼저 켭니다.
   → https://myaccount.google.com/security
2. **앱 비밀번호** 페이지로 이동: https://myaccount.google.com/apppasswords
3. 이름을 `stock-news-watch` 등으로 입력하고 **생성**을 누르면
   16자리 비밀번호(예: `abcd efgh ijkl mnop`)가 나옵니다. **공백 없이** 복사해 둡니다.

### 2) GitHub 레포 만들기

1. GitHub에 로그인 → 우측 상단 **+** → **New repository**
2. 이름 예: `stock-news-watch`, **Private** 권장 → **Create repository**
3. 이 폴더(`stock-news-watch`)의 파일들을 그 레포에 올립니다. 터미널에서:

   ```bash
   cd stock-news-watch
   git init
   git add .
   git commit -m "initial: stock news watcher"
   git branch -M main
   git remote add origin https://github.com/<내계정>/stock-news-watch.git
   git push -u origin main
   ```

### 3) Secrets(비밀값) 등록

레포 페이지 → **Settings** → **Secrets and variables** → **Actions** →
**New repository secret** 로 아래 3개를 등록합니다.

| 이름 | 값 |
|------|-----|
| `GMAIL_USER` | 메일을 **보내는** Gmail 주소 (예: `myname@gmail.com`) |
| `GMAIL_APP_PASSWORD` | 위에서 만든 16자리 앱 비밀번호 (공백 제거) |
| `MAIL_TO` | 메일을 **받을** 주소 (예: `leejh5709@cu911.com`) |

### 4) Actions 켜기 & 첫 실행

1. 레포의 **Actions** 탭으로 이동 → 안내가 뜨면 워크플로 실행을 **Enable** 합니다.
2. 왼쪽에서 **Stock News Watch** 선택 → 오른쪽 **Run workflow** 버튼으로 수동 실행.
   - 첫 실행은 기준선만 기록합니다(메일 없음).
   - **한 번 더 Run workflow** 를 눌러 두 번째 실행을 해보면, 그 사이 새 뉴스가 있으면 메일이 옵니다.
3. 이후에는 **30분마다 자동**으로 돌면서 새 뉴스가 생기면 메일을 보냅니다.

---

## 티커 바꾸기

`tickers.txt` 를 열어 원하는 티커로 수정하고 커밋/푸시하면 됩니다. 한 줄에 하나씩, 최대 10개:

```
AAPL
NVDA
TSLA
```

`#` 로 시작하는 줄과 빈 줄은 무시됩니다.

---

## 실행 주기 바꾸기

`.github/workflows/watch.yml` 의 `cron` 값을 바꾸면 됩니다 (UTC 기준):

- `"*/30 * * * *"` → 30분마다 (기본)
- `"0 * * * *"` → 매시 정각
- `"0 13 * * *"` → 매일 한국시간 밤 10시쯤 (UTC 13시)

> GitHub Actions의 스케줄은 무료 요금제에서 최소 간격 5분이며, 트래픽에 따라
> 몇 분 지연될 수 있습니다. 하루 실행량은 개인 사용 수준에서는 무료 한도 안에 들어옵니다.

---

## 로컬에서 테스트하려면 (선택)

이 PC에는 실제 Python이 설치돼 있지 않습니다. 테스트하고 싶다면 Python 3.10+ 설치 후:

```bash
pip install -r requirements.txt
# 메일 없이 동작만 확인 (새 뉴스 개수만 출력)
DRY_RUN=1 python news_watcher.py
```

실제 메일 발송 테스트:

```bash
export GMAIL_USER="myname@gmail.com"
export GMAIL_APP_PASSWORD="앱비밀번호16자리"
export MAIL_TO="leejh5709@cu911.com"
export EMAIL_ON_FIRST_RUN=1   # 첫 실행에도 메일 보내기
python news_watcher.py
```

---

## 파일 구성

| 파일 | 설명 |
|------|------|
| `tickers.txt` | 감시할 티커 목록 (최대 10) |
| `news_watcher.py` | 뉴스 수집 · 중복 판별 · 메일 발송 메인 스크립트 |
| `requirements.txt` | 파이썬 의존성 (`feedparser`) |
| `seen.json` | 이미 본 뉴스 기록 (자동 생성/갱신, 손대지 않아도 됨) |
| `.github/workflows/watch.yml` | GitHub Actions 스케줄 워크플로 |

---

## 문제 해결

- **메일이 안 와요**: Actions 탭에서 최근 실행 로그를 확인하세요.
  `GMAIL_APP_PASSWORD` 오타(공백 포함)나 2단계 인증 미설정이 가장 흔한 원인입니다.
- **첫 실행인데 메일이 없어요**: 정상입니다. 첫 실행은 기준선만 기록합니다.
- **스팸함 확인**: 첫 메일은 스팸으로 분류될 수 있으니 "스팸 아님" 처리해 주세요.
- **뉴스가 너무 많이/적게 와요**: `tickers.txt` 의 티커 수를 조절하거나 `cron` 주기를 늘리세요.
