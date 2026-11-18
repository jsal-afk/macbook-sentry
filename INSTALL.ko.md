# Mac Sentry 설치 및 운영

[English](INSTALL.md) · [한국어 소개](README.ko.md)

macOS 14 이상이 설치된 Apple Silicon 맥, 지원되는 내장 또는 USB 카메라, 마이크가 필요합니다. 맥에 로그인한 사용자로 터미널에서 실행하세요. `root` 권한은 필요 없습니다. 이 절차로는 카메라가 켜지거나 로그인 항목이 추가되지 않습니다.

## 1. 감시를 끈 상태로 설치

1. 터미널을 엽니다. Command Line Tools가 없다면 `xcode-select --install`을 실행하고 설치 창에서 완료하세요. Homebrew가 없다면 [공식 macOS `.pkg` 설치 파일](https://docs.brew.sh/Installation)을 사용하세요.
2. 저장소 폴더에서 실행합니다.

   ```sh
   eval "$(/opt/homebrew/bin/brew shellenv)"
   brew install python@3.12 ffmpeg
   python3.12 -m venv .venv
   .venv/bin/python -B macbook_sentry.py init
   .venv/bin/python -B -m unittest -v test_candidate
   .venv/bin/python -B macbook_sentry.py status
   ```

   `init`은 Swift 마이크 도구를 빌드합니다. `status`에 `stopped`가 표시되어야 합니다. 테스트는 VideoToolbox로 처리한 합성 영상과 테스트용 텔레그램 응답을 쓰며, 실제 카메라나 아이폰, Tailscale은 사용하지 않습니다. 아래의 `sentry` 명령을 쓰려면 같은 저장소 폴더에서 터미널 탭마다 이 별칭을 등록하세요.

   ```sh
   alias sentry='.venv/bin/python -B macbook_sentry.py'
   ```

3. 기본 녹화 위치는 `~/.macbook-sentry/archive/recordings`입니다. 보관 기간을 바꾸려면 Sentry를 중지한 상태에서 다음과 같이 실행하세요.

   ```sh
   sentry configure --retention-hours 48
   ```

   기본은 24시간이고 1~720시간으로 설정할 수 있습니다. 보관 기간은 촬영 시점부터 계산하며, Sentry가 실행 중이면 집에 있을 때도 선택한 저장 위치에서 오래된 녹화가 정리됩니다. 외출 한 번에 녹화할 수 있는 최대 시간은 별도입니다(`--away-lease-seconds`, 기본 14400초, 최대 86400초). 저장 공간을 최소 5 GiB 남겨두세요. 설정은 `~/.macbook-sentry/config.json`에 저장됩니다.

   맥 미니 등에 USB 웹캠을 연결했다면 `sentry configure --camera 'Camera Name'`으로 AVFoundation에 표시되는 이름이나 번호를 지정하세요(기본 `0`). `ffmpeg -f avfoundation -list_devices true -i ''`로 기기 목록을 확인하고, 마이크는 macOS 사운드 설정에서 선택합니다.

   **외장 드라이브에 저장하려면:** 중지 상태에서 `sentry archive '/Volumes/DRIVE_NAME'`을 실행합니다. `DRIVE_NAME`은 실제로 마운트된 이름으로 바꾸세요. 드라이브에 볼륨 UUID가 있어야 하며, Sentry는 해당 드라이브에 새 `Mac-Sentry` 폴더를 만듭니다. 내장 저장공간으로 바꾸려면 `sentry archive --internal --name archive-new`를 실행합니다. 폴더 이름이 이미 사용 중이면 `--name`으로 새 이름을 지정하세요. 저장 위치를 바꿔도 기존 영상은 이동하거나 삭제하지 않습니다. 지정한 외장 드라이브가 없을 때 내장 저장공간으로 자동 전환하지 않습니다.

## 2. 사람 감지와 텔레그램 알림

같은 저장소 폴더에서 감지 모델을 설치합니다. 그다음 텔레그램 봇을 연결하면 사람 감지 알림과 클립을 받을 수 있습니다.

```sh
.venv/bin/python -m pip --isolated --disable-pip-version-check install --index-url https://pypi.org/simple -r requirements-detection.txt
(
  set -eu
  umask 077
  state="$HOME/.macbook-sentry"
  tmp=$(mktemp "$state/nano.XXXXXXXX")
  trap 'rm -f "$tmp"' EXIT
  /usr/bin/curl -q --fail --show-error --location --proto '=https' \
    'https://storage.googleapis.com/rfdetr/nano_coco/checkpoint_best_regular.pth' -o "$tmp"
  printf 'd8d6b9ee57d4d0ed2b1f305163624712a0532cb7bce0c747317984fc5457440d  %s\n' "$tmp" | shasum -a 256 -c -
  mv -f "$tmp" "$state/rf-detr-nano.pth"
)
sentry check-model
.venv/bin/python -B -m unittest -v test_detection
```

[RF-DETR 1.8.0 Nano COCO 체크포인트](https://github.com/roboflow/rf-detr/blob/1.8.0/src/rfdetr/assets/model_weights.py)를 사용합니다. 설치 후에는 모델을 다시 내려받지 않습니다. `check-model`은 실제 카메라 대신 빈 테스트 이미지로 동작을 확인합니다.

1. 텔레그램의 BotFather에 `/newbot`을 보내 Sentry 전용 봇을 만드세요. 이 봇을 그룹에 추가하거나 다른 프로그램에 연결하지 않습니다.
2. 맥에서 `sentry pair-bot`을 실행하세요. **봇 소유자가** 터미널의 숨김 입력란에 토큰을 입력하고, 화면에 표시된 `@bot`의 개인 채팅에 일회용 `/start <code>`를 그대로 보냅니다. 토큰을 채팅, 명령줄, 로그 또는 소스 폴더에 넣지 마세요. 토큰과 연결된 채팅 한 곳은 소유자만 읽을 수 있는 `~/.macbook-sentry/bot.json`에 저장됩니다. 봇의 수신 메시지를 확인하는 작업은 연결할 때만 실행됩니다.
3. `sentry test-bot`을 실행해 텍스트와 소리가 포함된 MP4 테스트 영상을 확인하세요. 카메라는 켜지지 않습니다. 영상이 재생되면 `sentry configure --detection on`으로 사람 감지를 켭니다. `sentry configure --detection off`로 끄면 영상·음성 녹화만 합니다.

토큰을 바꾸려면 Sentry를 중지하고 BotFather에서 기존 토큰을 취소한 뒤 새 토큰을 발급받으세요. `pair-bot`과 `test-bot`을 다시 실행하고 감시를 끈 상태로 시작합니다. 봇이나 수신 채팅을 바꿔도 이전 전송 대기열은 새 채팅으로 넘어가지 않습니다. 로컬 보관 기간 설정은 이미 텔레그램으로 보낸 클립에 적용되지 않습니다.

## 3. 아이폰 외출·귀가 자동화

1. 맥과 아이폰에 [Tailscale](https://tailscale.com/download)을 설치하고 같은 소유자 계정으로 로그인하세요. 아이폰이 태그 없는 소유자 기기인지, 네트워크 규칙상 맥에 접근 가능한지, 자동 날짜·시간이 켜져 있는지 확인합니다. 필요하면 MagicDNS와 HTTPS를 켜세요. 연결은 비공개지만 인증서 발급 시 맥의 MagicDNS 이름이 공개 기록에 남을 수 있습니다. 이름 노출이 걱정된다면 기기 이름에 개인정보를 넣지 마세요.
2. 맥의 MagicDNS `.ts.net` 이름과 Tailscale Serve에 표시되는 소유자 로그인 값을 아래 변수에 로컬로 설정하세요. 실제 호스트명, 로그인 값, 생성한 단축어는 저장소에 올리지 않습니다. `tailscale serve status`와 `tailscale funnel status`를 먼저 확인하세요. **8443 포트가 이미 쓰이고 있다면 기존 설정을 변경하지 말고 중단하세요.** 사용할 수 있다면 별도 터미널 탭에서 다음과 같이 설정하고 Serve를 실행해 둡니다.

   ```sh
   sentry phone-setup "$MAC_MAGICDNS_NAME" "$TAILSCALE_OWNER_LOGIN"
   tailscale serve --https=8443 --set-path=/sentry-presence http://127.0.0.1:18765
   ```

   수신기는 `127.0.0.1`에서만 대기합니다. Funnel이나 공개 경로를 사용하지 마세요. 로컬 포트 `18765`가 이미 쓰이는 경우 `phone-setup ... --listen-port PORT`로 빈 포트를 지정하고 Serve의 대상 포트도 맞춥니다. Serve에 의도한 경로가 추가됐는지, Funnel이 8443에서 열려 있지 않은지 확인하세요. 종료하려면 Serve 탭에서 Ctrl-C를 누르고 `tailscale serve status`를 확인합니다. 정확히 Sentry 경로만 남아 있을 때에만 `tailscale serve --https=8443 --set-path=/sentry-presence off`로 제거하세요. 다른 경로를 초기화하지 않습니다.
3. Sentry를 중지한 상태에서 `sentry shortcuts`와 `sentry shortcuts --diagnostic`을 실행합니다. 맥이 **Sentry Away**, **Sentry Home**과 별도 폴더의 **Test** 버전에 서명합니다. Apple의 `shortcuts sign`은 서명 중 Apple에 접속해 비공개 수신 주소와 신호 키가 포함된 단축어 내용을 보낼 수 있습니다. 봇 토큰이나 좌표는 단축어에 들어가지 않습니다. 가져오기는 누구나 할 수 있어도 신호를 보내려면 같은 소유자 계정과 설치별 키가 모두 필요하니, 파일은 AirDrop 등으로 내 아이폰에만 옮겨 파일을 열고 **단축어 추가**를 누르세요. 파일이 노출되면 `phone-setup ... --rotate`로 키를 바꿔 단축어를 다시 생성합니다. 교체할 때는 기존 자동화를 먼저 꺼두세요. 단축어는 전송 전에 실행 시각을 기록합니다. 테스트 단축어의 별도 키는 실제 운영 모드에서 거부됩니다.
4. 다른 터미널 탭에서 `sentry run --test`를 실행하세요. 이 모드에서는 녹화가 시작되지 않고 영상도 삭제되지 않습니다. 아이폰에서 **Sentry Away Test**를 누르고 `accepted: true`, `event: away`, `mode: test`가 나오는지 확인합니다. 맥의 `sentry status`에서도 `last_accepted.away`가 갱신돼야 합니다. 다음으로 **Sentry Home Test**를 따로 실행해 `last_accepted.home`이 갱신되는지 확인하세요. iOS가 연결을 묻는다면 허용합니다. 403이면 Tailscale 계정과 신호 키를 확인하세요. 시각이 맞지 않거나 중복 신호라면 두 기기의 시간을 맞춘 뒤 새로 한 번 실행하세요.
5. 아이폰 **단축어 → 자동화 → + → 떠날 때(Leave)**에서 집 위치를 정하고 **즉시 실행(Run Immediately)**과 **Sentry Away**를 선택합니다. **도착할 때(Arrive)**에는 **Sentry Home**을 연결하세요. 원한다면 집 Wi-Fi에 연결될 때도 **Sentry Home**을 실행할 수 있습니다. 자동화에는 결과 창이 뜨지 않는 일반 버전을 쓰고, **Test** 버전은 넣지 않습니다. 테스트 모드를 켠 채 아이폰이 잠긴 상태에서 외출과 귀가를 따로 확인하세요. 집 Wi-Fi 밖에서 셀룰러로 신호가 도착하는지, Wi-Fi 자동화도 설정했다면 해당 트리거가 따로 동작하는지 확인합니다. 자동화가 동작하지 않으면 맥의 수동 조작을 사용하세요. 네트워크가 끊겼다는 이유만으로 외출로 판단하지 않습니다. 마치면 `sentry stop`을 실행합니다.

## 4. 실제 기기 확인과 유지보수

- **첫 녹화 테스트:** 녹화 권한이 있는 공간에서 `sentry authorize-mic`을 실행하고 마이크 접근을 허용합니다. 한 탭에서 `sentry run`을 실행해 열어두고, 다른 탭에서 `sentry arm`, `sentry away` 순으로 실행합니다. 카메라 권한을 물으면 허용합니다. 거부했다면 시스템 설정의 **개인정보 보호 및 보안 → 카메라/마이크**에서 권한을 확인하고 감시를 끈 상태에서 다시 시작하세요. `sentry status`에 `signal: away`와 `capture_worker`가 표시되는지 확인합니다. 영상 조각이 하나 이상 저장된 후 `sentry home`을 실행하고 `capture_worker: false`와 카메라 표시등이 꺼졌는지 확인하세요. 녹화 폴더의 `.ts` 파일을 열고 영상과 소리가 모두 나오는지 확인합니다. 알림은 사람 감지를 켜고 촬영에 동의한 사람이 카메라에 들어오는 방식으로 따로 확인하세요. 감지 후 약 25초 이상 기다렸다가 텔레그램 알림과 소리가 있는 박스 영상을 확인합니다. 귀가할 때 생성 중인 클립은 원본이 남아 있다면 다음 외출 때 처리될 수 있습니다.
- **평소 사용:** `run`은 녹화 대기 상태를 켜지 않고 시작합니다. `arm`은 이후의 새 외출 신호를 받을 준비만 하고 카메라를 켜지는 않습니다. `away`와 `home`은 Tailscale 없이 맥에서 수동으로도 쓸 수 있습니다. 귀가하면 녹화를 멈추되 다음 외출 신호를 받을 준비는 유지합니다. `pause`는 녹화를 멈추고 다음 `arm`까지 대기 상태도 해제합니다. `stop` 또는 실행 탭의 Ctrl-C로 Sentry를 종료합니다. 외출 시간 한도에 도달했거나 녹화 작업이 실패했다면 원인을 확인한 뒤 다시 `arm`을 실행하세요. 늦게 도착한 이전 외출 신호가 더 최근 귀가 상태를 덮어쓰거나, 재시작·대기 전 상태를 되살리지 않습니다. 외출 신호를 반복해도 이미 시작된 외출 시간 한도는 연장되지 않습니다. 영상 정리는 귀가하거나 일시 중지해도 계속되지만, 서비스가 종료됐거나 테스트 모드일 때는 실행되지 않습니다.
- **문제 해결:** `sentry status`와 실행 탭의 출력을 확인하세요. 저장 오류가 나면 남은 공간과 녹화 폴더를 확인하고, 외장 드라이브를 쓰는 경우 같은 이름의 위치에 다시 마운트합니다. 영상이나 소리에 문제가 있으면 기본 마이크, 카메라 이름 또는 번호, 권한을 확인하세요. 맥북 내장 카메라를 쓴다면 덮개가 열려 있어야 합니다. 텔레그램은 `test-bot`으로 따로 확인하고, 아이폰은 테스트 모드에서 Serve의 포트와 경로, 소유자 계정, 외출·귀가 두 시각을 확인하세요.
- **업데이트·되돌리기:** `sentry stop` 후 `sentry status`가 `stopped`인지 확인합니다. 기존 소스와 `.venv`를 보관하고, 자격 증명이 들어 있는 `~/.macbook-sentry`는 비공개로 백업하세요. 새 소스를 별도 폴더에 준비하고 1단계를 실행한 후, 사용하는 추가 의존성도 다시 설치합니다. `init`은 기존 설정을 유지하면서 마이크 도구를 다시 빌드합니다. 녹화를 켜기 전에 점검하세요. 되돌리려면 새 Sentry를 중지하고 이전 소스와 `.venv`로 돌아갑니다. 설정 형식이 달라졌다면 해당 버전의 비공개 백업을 복원하고 이전 `init`, `run`을 실행합니다. 취소한 토큰은 복원하지 마세요. 실행 중에는 소스나 가상 환경을 바꾸지 않습니다.
- **녹화본을 남기고 제거:** Sentry를 중지하고 아이폰 자동화와 가져온 단축어를 해제하거나 삭제하세요. Serve를 썼다면 Sentry 경로만 제거합니다. 전용 봇을 더 이상 쓰지 않는다면 토큰을 취소하세요. 내장 공간에 저장한 영상 중 보관할 것은 먼저 `~/.macbook-sentry` 밖으로 옮깁니다. 그다음 소스 폴더(`.venv` 포함)와 남은 `~/.macbook-sentry`를 휴지통으로 옮기세요. 보관할 외장 녹화 폴더는 그대로 둡니다. 기존 영상은 재생할 수 있지만 자동 정리는 멈춥니다.
