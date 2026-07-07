# 📈 Stock News Watch (주식 뉴스 감시 & 메일 알림)

관심 있는 주식 티커(최대 10개)를 등록해 두면, **CNBC · Yahoo Finance**
에 새 뉴스가 올라올 때마다 **링크를 이메일로 보내주는** 자동 도구입니다.
메일은 폰에서 열고 링크를 눌러 바로 원문을 확인할 수 있습니다.

> 💡 **무료 기사만 보이도록** 설정돼 있습니다. 유료 구독(로그인/Pro)이 필요한
> Seeking Alpha는 소스에서 제외했고, 그 외 유료 매체(WSJ·Bloomberg 등)도
> `paywall_filters.txt` 로 걸러냅니다. 자세한 내용은 아래 "유료 기사 걸러내기" 참고.

- 실행: **GitHub Actions** (클라우드) — 내 PC가 꺼져 있어도 하루 2번(아침 8시·오후 9시, KST) 자동 확인
- 알림: **Gmail SMTP** 로 메일 발송 → `your-email@example.com` (또는 원하는 주소)로 수신
- 새 뉴스만 골라서 보냄 (이미 본 뉴스는 `seen.json` 에 기록되어 중복 발송 안 함)

---

## 동작 방식 (요약)

1. `tickers.txt` 에 적힌 티커들을 읽습니다 (최대 10개).
2. 티커마다 Google News RSS를 CNBC / Yahoo Finance 도메인으로
   필터링해 최신 기사 링크를 가져옵니다 (유료 매체 Seeking Alpha는 제외).
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
| `MAIL_TO` | 메일을 **받을** 주소 (예: `your-email@example.com`) |

### 4) Actions 켜기 & 첫 실행

1. 레포의 **Actions** 탭으로 이동 → 안내가 뜨면 워크플로 실행을 **Enable** 합니다.
2. 왼쪽에서 **Stock News Watch** 선택 → 오른쪽 **Run workflow** 버튼으로 수동 실행.
   - 첫 실행은 기준선만 기록합니다(메일 없음).
   - **한 번 더 Run workflow** 를 눌러 두 번째 실행을 해보면, 그 사이 새 뉴스가 있으면 메일이 옵니다.
3. 이후에는 **하루 2번(아침 8시·오후 9시, 한국시간) 자동**으로 돌면서 새 뉴스가 생기면 메일을 보냅니다.

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

## 원치 않는 뉴스 걸러내기 (노이즈 필터)

"콜/풋옵션 때문에 올랐다·내렸다", "오늘의 급등주", 기술적 분석(RSI·이동평균) 같은
**알맹이 없는 시세성 기사**를 자동으로 빼줍니다. 걸러낼 키워드는 `noise_filters.txt`
에 한 줄에 하나씩 적으면 됩니다 (대소문자 무시, 단어 단위 매칭):

```
option
options
premarket
technical analysis
```

- `#` 로 시작하는 줄과 빈 줄은 무시됩니다. 파일이 비어 있으면 코드 기본값이 쓰입니다.
- `"Why is AAPL stock up today"` 같은 **급등락 클릭베이트 제목**은 이 파일과 별개로
  코드에서 자동으로 걸러집니다.
- 너무 많이 걸러지면 해당 키워드를 지우고, 덜 걸러지면 키워드를 추가하세요.

> ⚠️ 이 도구는 Google News RSS에서 **제목·링크만** 받아옵니다. 본문·기자 이름(byline)이
> 없어서 "기자가 쓴 심층 기사"인지 완벽히 판별하지는 못하고, **제목 기준**으로 걸러냅니다.

---

## 유료 기사 걸러내기 (무료로 볼 수 있는 뉴스만)

로그인이나 유료 구독(Pro/Premium)이 있어야 읽을 수 있는 기사는 최대한 제외합니다.

- **Seeking Alpha**: 거의 모든 기사가 유료라서 **소스 자체에서 제외**했습니다.
  이게 유료 벽의 가장 큰 원인이었습니다.
  (구독 중이라 다시 받고 싶다면 `news_watcher.py` 의 `SOURCES` 에서 Seeking Alpha 줄의
  맨 앞 `# ` 를 지우세요.)
- **CNBC · Yahoo Finance**: 두 매체의 기사는 **대부분 무료**로 읽을 수 있습니다.
- 그 외 유료 매체(WSJ·Bloomberg·Barron's·CNBC Pro 등)는 `paywall_filters.txt`
  목록에 있으면 제외됩니다. 매체를 추가/삭제하려면 이 파일을 편집하세요 (한 줄에 하나).

> ⚠️ **한계**: Google News는 제목 끝의 매체명을 "우리가 지정한 사이트"(CNBC/Yahoo)로
> 표시합니다. 그래서 Yahoo에 재게재된 Bloomberg 기사처럼 **원 출처가 유료여도 Yahoo에선
> 무료로 열리는 경우가 많습니다.** 반대로 CNBC의 일부 `CNBC Pro` 기사는 제목만으로는
> 100% 걸러내지 못할 수 있습니다. 유료 링크가 계속 걸리면 알려주세요 — 링크의 최종
> 주소를 따라가 유료 도메인을 직접 차단하는 방식으로 강화할 수 있습니다.

---

## 최근 뉴스만 받기 (오래된 기사 제외)

Google News 검색에는 **몇 년 전 기사나 "시세 페이지" 링크**도 섞여 나옵니다.
특히 새 티커를 추가하면 그 종목의 과거 기사가 한꺼번에 딸려 옵니다. 그래서
**발행 시각 기준 최근 24시간 안에 올라온 기사만** 메일로 보냅니다.

- 하루 2번(아침 8시·오후 9시) 실행이라 실행 간격이 최대 13시간 → 24시간 창이면
  놓치는 뉴스 없이 "그날 뉴스"만 옵니다. 이미 보낸 기사는 중복으로 다시 오지 않습니다.
- 기간을 바꾸고 싶으면 `news_watcher.py` 의 `MAX_AGE_HOURS = 24` 값을 고치거나,
  워크플로에 환경변수 `MAX_AGE_HOURS` 를 지정하세요. 예: `48` (이틀), `12` (반나절).

---

## 실행 주기 바꾸기

`.github/workflows/watch.yml` 의 `cron` 값을 바꾸면 됩니다 (UTC 기준. KST = UTC+9):

- `"0 23 * * *"` → 매일 08:00 KST (아침 8시, 현재 설정)
- `"0 12 * * *"` → 매일 21:00 KST (오후 9시, 현재 설정)
- `"*/30 * * * *"` → 30분마다
- `"0 * * * *"` → 매시 정각

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
export MAIL_TO="your-email@example.com"
export EMAIL_ON_FIRST_RUN=1   # 첫 실행에도 메일 보내기
python news_watcher.py
```

---

## 파일 구성

| 파일 | 설명 |
|------|------|
| `tickers.txt` | 감시할 티커 목록 (최대 10) |
| `noise_filters.txt` | 걸러낼 "알맹이 없는 뉴스" 키워드 목록 (옵션/급등락/기술적분석 등) |
| `paywall_filters.txt` | 걸러낼 유료 매체 목록 (WSJ·Bloomberg·CNBC Pro 등) |
| `news_watcher.py` | 뉴스 수집 · 노이즈 필터 · 중복 판별 · 메일 발송 메인 스크립트 |
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
