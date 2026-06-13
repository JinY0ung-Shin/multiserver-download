# 운영 가이드

이 문서는 외부 통신이 가능한 Windows main worker/controller 하나가 전체 작업을 제어하고,
여러 Linux/Windows worker를 사용해 Hugging Face 모델을 병렬 다운로드한 뒤,
inbound만 가능한 최종 Linux 서버로 결과를 밀어 넣는 운영 절차입니다.

## 1. 구조 이해

```text
                         Hugging Face
                    ┌─────────┬─────────┐
                    │         │         │
                  main     win1    linux1
                download  download  download
                    │         │         │
                    └──── main relay/push ────▶ final-linux destination
                                                /models/org/model
```

역할은 두 개입니다.

- Windows main controller: `uv run msdl ...`을 실행하는 Windows 서버입니다. Hugging Face, worker,
  최종 Linux 서버로 outbound 접속이 가능해야 합니다. 이 서버 자신도 `local = true`
  worker로 참여할 수 있습니다.
- worker: 인터넷으로 Hugging Face에 접근 가능한 Linux 또는 Windows 서버입니다.
  별도 서비스는 돌리지 않고, main controller가 SSH로 원격 명령을 실행합니다.
- final Linux destination: 최종 모델 파일이 저장되는 서버입니다. 이 서버는 외부로
  나가는 요청이 불가능해도 됩니다. Windows main controller에서 SSH/SCP로 접속할 수
  있으면 됩니다.

중요한 원칙은 모델 파일이 별도 중계 서버를 거치지 않는다는 점입니다. 각 worker가
Hugging Face에서 자기 몫을 임시 디렉터리에 받고, Windows main controller가 해당
파일만 `scp`로 가져온 뒤 최종 Linux destination으로 push합니다.

## 2. 최종 저장 경로와 work 경로

최종 Linux 서버가 inbound만 가능한 구조에서는 최종 저장 루트를
`--destination`으로 지정합니다.

```powershell
uv run msdl download meta-llama/Llama-3.1-70B `
  --servers .\servers.toml `
  --destination user@final-linux:/models
```

예를 들어 repo id가 `meta-llama/Llama-3.1-70B`이면 최종 경로는 아래처럼
만들어집니다.

```text
user@final-linux:/models/meta-llama/Llama-3.1-70B/
```

Windows main controller는 plan/status와 relay용 `.part` 파일을 로컬 work 경로에 둡니다.
명시하지 않으면 현재 디렉터리 아래 `.msdl-work`를 사용합니다.

```powershell
$env:MULTISERVER_DOWNLOAD_WORK_PATH = "D:\msdl-work"
```

최종 저장소가 main controller의 로컬 디스크라면 기존 방식처럼
`MULTISERVER_DOWNLOAD_SAVE_PATH` 또는 `--save-path`를 사용합니다.

```powershell
$env:MULTISERVER_DOWNLOAD_SAVE_PATH = "D:\models"
uv run msdl download meta-llama/Llama-3.1-70B --servers .\servers.toml
```

## 3. Hugging Face 토큰

public 모델이면 토큰 없이도 동작할 수 있습니다. private/gated 모델이면 토큰이
필요합니다.

토큰과 HF CLI 설치는 별개입니다. public 모델이라도 다운로드에 참여하는 서버에는
`hf` 또는 `huggingface-cli` 명령이 설치되어 있어야 합니다. 반대로 public 모델만
받는다면 `hf auth login`은 보통 필요 없습니다.

Windows main controller에서 토큰을 설정합니다.

```powershell
$env:HF_TOKEN = "hf_xxxxxxxxxxxxxxxxx"
```

worker 서버들이 이미 `hf auth login` 되어 있다면 추가 옵션 없이 실행해도 됩니다.
worker에 토큰이 없고 controller의 토큰을 원격 명령에만 전달하려면
`--forward-hf-token`을 붙입니다.

```powershell
uv run msdl download org/private-model `
  --servers .\servers.toml `
  --destination user@final-linux:/models `
  --forward-hf-token
```

토큰은 로그에 출력하지 않습니다. 그래도 shell history나 운영 환경 정책을 고려해
장기 저장 파일에는 토큰을 직접 적지 않는 편이 안전합니다.

실행 후 현재 shell에서 토큰을 지우려면:

```powershell
Remove-Item Env:\HF_TOKEN
```

## 4. TLS 인증서 검증 우회

기본값은 TLS 인증서 검증을 켜는 것입니다. 회사 프록시나 내부망 장비 때문에
Hugging Face 인증서 검증이 실패하고, 아직 프록시 CA 인증서를 설치할 수 없는
상황에서만 임시로 아래 옵션을 사용합니다.

```powershell
uv run msdl download org/model `
  --servers .\servers.toml `
  --destination user@final-linux:/models `
  --insecure-skip-tls-verify
```

같은 동작은 환경변수로도 켤 수 있습니다.

```powershell
$env:MULTISERVER_DOWNLOAD_INSECURE_SKIP_TLS_VERIFY = "1"
```

이 옵션은 Hugging Face manifest 조회, 속도 probe, worker 다운로드 요청의 TLS
인증서 검증을 끕니다. 다운로드 본문도 같은 TLS 설정을 쓰도록 Xet 전송은 끕니다.
SSH 접속 검증이나 파일 크기 검증은 끄지 않습니다.

## 5. Windows main controller 준비

Windows main controller에서 이 저장소를 받은 뒤 의존성을 설치합니다.

```powershell
uv sync
```

필요한 기본 도구는 다음과 같습니다.

- `uv`
- `ssh`
- `scp`
- relay용 work 경로에 최소 가장 큰 파일 하나 이상을 받을 수 있는 여유 공간

전송 백엔드 기본값은 `auto`입니다.

- Windows main controller에서는 Linux worker, Windows worker, final Linux destination 모두 기본적으로 `scp`를 사용합니다.
- Windows worker가 있으면 `--transfer-backend rsync`를 강제로 지정하지 마세요.

로컬 테스트 명령:

```powershell
uv run msdl --help
uv run msdl download --help
```

### 5.1 Windows main controller + inbound-only final Linux 구조

질문한 구조는 아래 형태입니다.

```text
Hugging Face
  -> Windows main controller 임시 디렉터리
  -> Windows worker 임시 디렉터리
  -> Linux worker 1 임시 디렉터리
  -> Windows main controller가 각 파일을 relay/push
  -> final Linux /models/org/model/... 에 최종 저장
```

이 구조에서는 Windows main controller에서 `msdl`을 실행합니다. 최종 Linux 서버는
`servers.toml`에 넣지 않고 `--destination`으로만 지정합니다.

Windows main controller 준비물:

- `uv`
- `ssh`
- `scp`
- work 디스크, 예: `D:\msdl-work`

Windows PowerShell에서 확인:

```powershell
uv --version
ssh -V
Get-Command scp
```

work 경로 설정:

```powershell
$env:MULTISERVER_DOWNLOAD_WORK_PATH = "D:\msdl-work"
```

dry-run:

```powershell
uv run msdl download meta-llama/Llama-3.1-70B `
  --servers .\servers.toml `
  --destination user@final-linux:/models `
  --dry-run
```

실제 다운로드:

```powershell
uv run msdl download meta-llama/Llama-3.1-70B `
  --servers .\servers.toml `
  --destination user@final-linux:/models
```

기본 `auto` 전송 방식에서는:

- Linux worker -> Windows main controller: `scp`
- Windows worker -> Windows main controller: `scp`
- Windows main controller -> final Linux: `scp`

중요한 경로 구분:

- `--destination`: 최종 Linux의 저장 루트입니다. 예: `user@final-linux:/models`
- `--work-path` 또는 `MULTISERVER_DOWNLOAD_WORK_PATH`: Windows main controller의
  로컬 work 경로입니다. 예: `D:\msdl-work`
- Linux worker의 `temp_roots`: `/data/tmp`, `/tmp`
- Windows worker의 `temp_roots`: `D:/msdl-tmp`처럼 forward slash를 권장합니다.

SSH key를 지정해야 하면:

```powershell
uv run msdl download org/model `
  --servers .\servers.toml `
  --ssh-option "-i C:\Users\me\.ssh\company_key" `
  --ssh-option "-o StrictHostKeyChecking=accept-new"
```

Hugging Face repo 안에 Windows worker 임시 디스크에서 만들 수 없는 파일명(`CON`,
`NUL`, `:`, `*`, `?` 등)이 있으면 Windows worker 다운로드가 실패할 수 있습니다.
일반적인 모델 파일명은 대부분 문제없이 동작합니다.

## 6. worker 준비

Windows main controller를 `local = true` worker로 쓸 때는 main 자체에
`python` 또는 `py -3`, `hf` 또는 `huggingface-cli`, 충분한 temp 디스크가
필요합니다.

Linux worker에는 아래가 필요합니다.

- controller에서 SSH 접속 가능
- Hugging Face 접속 가능
- `python3`
- `hf` 또는 `huggingface-cli`
- 임시 다운로드를 저장할 충분한 디스크 공간

final Linux destination은 최종 저장소일 뿐 worker가 아니면 HF CLI가 필요 없습니다.

### 6.1 Hugging Face CLI 설치

다운로드에 참여하는 모든 서버에 설치합니다.

- Windows main controller가 `local = true` worker이면 Windows main에도 설치
- Linux worker 2대에도 설치
- Windows worker를 추가로 쓰면 해당 Windows worker에도 설치
- final Linux destination은 worker가 아니면 설치 불필요

Windows main 또는 Windows worker:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://hf.co/cli/install.ps1 | iex"
hf --help
```

Linux worker:

```bash
curl -LsSf https://hf.co/cli/install.sh | bash
hf --help
```

Python/uv로 관리하고 싶으면 아래 방식도 사용할 수 있습니다.

```bash
uv tool install "huggingface_hub[hf_transfer]"
hf --help
```

`hf_transfer`가 있으면 worker 원격 다운로드 명령에서 자동으로
`HF_HUB_ENABLE_HF_TRANSFER=1`을 켭니다. 설치되어 있지 않아도 동작은 하지만
서버당 다운로드 속도가 낮을 수 있습니다.

public 모델만 받을 때는 설치 후 로그인 없이 사용할 수 있습니다.

```bash
hf download bert-base-uncased config.json --local-dir /tmp/hf-test
```

Windows에서는:

```powershell
hf download bert-base-uncased config.json --local-dir D:\hf-test
```

private/gated 모델이면 각 worker에서 직접 로그인하거나, Windows main
controller의 토큰을 `--forward-hf-token`으로 전달합니다.

```bash
hf auth login
```

SSH로 들어갔을 때 `hf`가 보이는지 확인합니다.

```bash
ssh user@ext1 'command -v hf || command -v huggingface-cli'
ssh user@ext1 'python3 --version'
```

`uv tool install` 후 SSH 비대화형 세션에서 `hf`가 안 보이면 worker의 PATH에
`~/.local/bin`이 포함되도록 shell 설정을 조정하거나, 시스템 경로에 설치하세요.

Windows worker에는 아래가 필요합니다.

- controller에서 SSH 접속 가능, 보통 Windows OpenSSH Server
- PowerShell
- `python` 또는 `py -3`
- `hf` 또는 `huggingface-cli`
- 임시 다운로드를 저장할 충분한 디스크 공간

Windows worker에서 uv로 설치하는 경우:

```powershell
uv tool install "huggingface_hub[hf_transfer]"
hf --help
```

Windows main controller에서 Windows worker를 확인하는 예:

```bash
ssh user@win1 'powershell -NoProfile -Command "Get-Command python; Get-Command hf; Get-PSDrive D"'
```

Windows worker는 `rsync`를 요구하지 않습니다. Windows main controller가 Windows worker의 파일을
가져올 때는 `scp`를 사용합니다.

## 7. 서버 설정 파일

`servers.example.toml`을 복사해 실제 서버 목록을 만듭니다.

```bash
cp servers.example.toml servers.toml
```

예시:

```toml
[[servers]]
name = "main"
local = true
platform = "windows"
temp_roots = ["D:/msdl-main-tmp"]

[[servers]]
name = "win1"
platform = "windows"
ssh_target = "user@win1"
temp_roots = ["D:/msdl-tmp"]

[[servers]]
name = "linux1"
platform = "linux"
ssh_target = "user@linux1"
temp_roots = ["/mnt/nvme/tmp", "/tmp"]

[[servers]]
name = "linux2"
platform = "linux"
ssh_target = "user@linux2"
temp_roots = ["/data/tmp", "/tmp"]
```

필드 의미:

- `name`: 로그와 plan 파일에 표시되는 worker 이름입니다.
- `local`: main controller 자신을 worker로 쓸 때 `true`로 둡니다. 이 경우
  `ssh_target`은 필요 없습니다. main이 Windows이면 `platform = "windows"`로 둡니다.
- `platform`: `linux` 또는 `windows`입니다. 생략하면 `linux`입니다.
- `ssh_target`: 원격 worker에만 필요합니다. `~/.ssh/config` alias도 사용할 수
  있습니다.
- `temp_roots`: worker에서 임시 파일을 둘 후보 디렉터리입니다.

최종 Linux destination은 `servers.toml`에 넣지 않습니다. 아래처럼 실행 옵션으로만
넘깁니다.

```bash
--destination user@final-linux:/models
```

실행 전 각 `temp_roots`는 platform별 방식으로 검사됩니다. Linux worker는
`df -Pk`, Windows worker는 PowerShell의 drive free space를 사용합니다.
Windows main controller는 여유 공간이 가장 큰 사용 가능한 root를 고르고 아래 디렉터리를
만듭니다.

```text
<temp_root>/msdl/<job_id>/<org>_<model>/
```

Windows worker의 경로도 설정 파일에서는 `D:/msdl-tmp`처럼 forward slash를 쓰는
편이 안전합니다.

## 8. dry-run으로 사전 점검

실제 다운로드 전에 반드시 dry-run을 먼저 실행하는 것을 권장합니다.

```powershell
$env:MULTISERVER_DOWNLOAD_WORK_PATH = "D:\msdl-work"

uv run msdl download meta-llama/Llama-3.1-70B `
  --servers .\servers.toml `
  --destination user@final-linux:/models `
  --dry-run
```

dry-run에서 수행하는 일:

- Hugging Face manifest 조회
- final Linux destination SSH 접속, 디렉터리 생성, 여유 공간 확인
- Windows main controller work 경로 여유 공간 확인
- worker별 임시 디스크 여유 공간 확인
- worker별 Hugging Face 속도 probe
- worker별 `hf` 또는 `huggingface-cli` 존재 확인
- 원격 worker에서 Windows main controller로 작은 probe 파일 pull 검증
- Windows main controller에서 final Linux destination으로 작은 probe 파일 push 및 rename 검증
- 속도 기준 다운로드 용량 분배
- plan 파일 기록

모델 파일 다운로드는 하지 않습니다. 단, 검증을 위해 아주 작은 `.msdl-preflight-*`
파일을 worker temp 경로와 final destination에 썼다가 삭제합니다.

## 9. 실제 다운로드

```powershell
uv run msdl download meta-llama/Llama-3.1-70B `
  --servers .\servers.toml `
  --destination user@final-linux:/models
```

private/gated 모델:

```powershell
$env:HF_TOKEN = "hf_xxxxxxxxxxxxxxxxx"

uv run msdl download org/private-model `
  --servers .\servers.toml `
  --destination user@final-linux:/models `
  --forward-hf-token
```

특정 revision을 고정하려면:

```powershell
uv run msdl download org/model `
  --servers .\servers.toml `
  --destination user@final-linux:/models `
  --revision 0123456789abcdef
```

일부 파일만 받으려면:

```powershell
uv run msdl download org/model `
  --servers .\servers.toml `
  --destination user@final-linux:/models `
  --include "*.safetensors" `
  --include "config.json" `
  --include "tokenizer*"
```

## 10. 시작 로그에서 확인할 것

시작하면 아래 항목이 로그에 나와야 합니다.

```text
repo: org/model
revision: main
local work target: D:\msdl-work\org\model
final target: user@final-linux:/models/org/model
manifest: 42 files, 132.4 GiB
probe main: temp=D:\msdl-main-tmp\msdl\... free=900.0 GiB speed=240.0 MiB/s
probe win1: temp=D:/msdl-tmp/msdl/... free=900.0 GiB speed=210.0 MiB/s
probe linux1: temp=/mnt/nvme/tmp/msdl/... free=700.0 GiB speed=120.0 MiB/s
download plan:
  main -> 28 files, 90.0 GiB
  win1 -> 26 files, 84.0 GiB
  linux1 -> 16 files, 48.4 GiB
```

확인 포인트:

- `final target`이 의도한 경로인지
- worker별 `temp`가 기대한 디스크인지
- worker별 `free`가 충분한지
- 빠른 worker에 더 많은 용량이 배정됐는지
- plan 파일 경로가 생성됐는지

plan 파일은 아래에 저장됩니다.

```text
<main work target>/.msdl/<job_id>/plan.json
```

진행도 파일도 같은 job 디렉터리에 저장됩니다.

```text
<main work target>/.msdl/<job_id>/status.json
```

## 11. 중간 진행도 확인

다운로드 중에는 로그에 파일 단위 진행과 전체 누적 진행도가 같이 표시됩니다.

```text
win1 download model-00001-of-00030.safetensors (4.6 GiB); running: 0/30 files, 0.0 B/138.0 GiB (0.00%)
win1 done model-00001-of-00030.safetensors; running: 1/30 files, 4.6 GiB/138.0 GiB (3.33%)
```

별도 터미널에서 현재 진행도를 확인하려면:

```bash
uv run msdl status meta-llama/Llama-3.1-70B
```

출력 예시:

```text
running: 12/30 files, 55.2 GiB/138.0 GiB (40.00%)
job: 1a2b3c4d5e6f
repo: meta-llama/Llama-3.1-70B
revision: main
target: user@final-linux:/models/meta-llama/Llama-3.1-70B
updated: 2026-06-13T12:34:56+00:00
servers:
  main: 7/12 files, 32.0 GiB/58.0 GiB, downloading=model-00012-of-00030.safetensors
  linux1: 5/12 files, 23.0 GiB/55.0 GiB, downloading=model-00013-of-00030.safetensors
  linux2: 4/10 files, 18.4 GiB/46.0 GiB, transferring=model-00014-of-00030.safetensors
  win1: 3/8 files, 13.8 GiB/37.0 GiB
```

계속 갱신해서 보려면:

```bash
uv run msdl status meta-llama/Llama-3.1-70B --watch
```

갱신 주기를 지정할 수도 있습니다.

```bash
uv run msdl status meta-llama/Llama-3.1-70B --watch 2
```

특정 job을 지정하려면:

```bash
uv run msdl status meta-llama/Llama-3.1-70B --job-id 1a2b3c4d5e6f
```

자동화에서 쓰려면 JSON으로 출력합니다.

```bash
uv run msdl status meta-llama/Llama-3.1-70B --json
```

remote destination 모드에서 `status` 명령은 `MULTISERVER_DOWNLOAD_WORK_PATH`를
사용합니다. 환경변수를 쓰지 않으면 `--work-path`를 같이 넘기면 됩니다. local
final mode에서는 `MULTISERVER_DOWNLOAD_SAVE_PATH` 또는 `--save-path`를 사용합니다.

```powershell
uv run msdl status meta-llama/Llama-3.1-70B --save-path /models
uv run msdl status meta-llama/Llama-3.1-70B --work-path D:\msdl-work
```

## 12. 전송 방식

`msdl`은 HF cache 전체나 모델 루트 전체를 동기화하지 않습니다. worker가 파일 하나를
다운로드하면 main controller가 그 파일 하나만 처리합니다.

```text
remote worker temp file
  -> main controller .msdl/incoming/<path>.part
  -> size check
  -> final Linux <path>.part
  -> remote size check
  -> final path로 rename
```

`local = true`인 Windows main worker가 받은 파일은 main의 worker temp에서 바로 final
Linux로 push합니다.

전송 백엔드는 `auto`, `rsync`, `scp` 중 하나입니다.

- Linux worker -> Windows main controller의 `auto`: `scp`
- Windows worker -> Windows main controller의 `auto`: `scp`
- Windows main controller -> final Linux의 `auto`: `scp`
- `--transfer-backend rsync`: Windows worker가 있으면 실행을 중단합니다.

Windows main controller에서는 `auto`가 `scp`를 선택합니다. `scp`는 단순하고
Windows 기본 OpenSSH 환경에서 잘 맞지만, 중간에 끊긴 파일을 append-verify로
이어받지는 못하고 해당 파일 전송을 다시 시작합니다.

## 13. 재시도와 중단 후 복구

이미 최종 경로에 있고 크기가 맞는 파일은 다음 실행에서 skip합니다.

```text
ext1 skip existing model-00001-of-00030.safetensors
```

중단 후 같은 명령을 다시 실행하면:

- 최종 완료 파일은 skip
- 미완료 파일은 다시 worker에 배정
- `scp` 백엔드는 `.part`를 덮어쓰며 다시 전송

worker 임시 파일을 지우지 않고 남기려면:

```bash
uv run msdl download org/model \
  --servers servers.toml \
  --keep-remote
```

기본값은 최종 전송이 끝난 worker 임시 파일을 삭제합니다.

## 14. 자주 쓰는 옵션

```text
--servers servers.toml        worker 설정 파일
--revision REV                branch, tag, commit 고정
--include PATTERN             받을 파일 패턴, 여러 번 사용 가능
--exclude PATTERN             제외할 파일 패턴, 여러 번 사용 가능
--save-path PATH              local final mode에서 최종 저장 루트 지정
--work-path PATH              remote destination mode에서 main local work 루트 지정
--destination USER@HOST:/PATH 최종 Linux destination 저장 루트
--speed-test-mib N            worker별 속도 측정 크기, 기본 64MiB
--skip-speed-test             속도 측정 없이 동일 가중치로 분배
--reserve-gib N               worker temp root에 남길 여유 공간, 기본 5GiB
--forward-hf-token            controller의 HF_TOKEN을 worker 원격 명령에 전달
--insecure-skip-tls-verify    Hugging Face HTTPS 인증서 검증을 임시로 끔
--ssh-option OPTION           ssh 옵션 추가, 여러 번 사용 가능
--transfer-backend auto|rsync|scp
                              worker pull과 destination push 방식, 기본 auto
--keep-remote                 전송 후 worker 임시 파일 보존
--dry-run                     preflight, probe, plan만 실행
-v                            debug 로그 출력
```

진행도 조회 옵션:

```text
msdl status org/model
--job-id JOB                  특정 job 상태 조회
--watch [SECONDS]             주기적으로 상태 갱신
--json                        raw status JSON 출력
--save-path PATH              local final mode 상태 루트 지정
--work-path PATH              remote destination mode 상태 루트 지정
```

SSH 옵션 예시:

```powershell
uv run msdl download org/model `
  --servers .\servers.toml `
  --ssh-option "-i C:\Users\me\.ssh\company_key" `
  --ssh-option "-o StrictHostKeyChecking=accept-new"
```

## 15. 문제 해결

`set MULTISERVER_DOWNLOAD_SAVE_PATH or pass --save-path`

: local final mode에서 최종 저장 경로가 지정되지 않은 상태입니다. remote
destination mode에서는 `--destination`을 넘기면 됩니다.

```powershell
$env:MULTISERVER_DOWNLOAD_WORK_PATH = "D:\msdl-work"
uv run msdl download org/model --servers .\servers.toml --destination user@final-linux:/models
```

`missing hf or huggingface-cli on worker`

: worker에서 HF CLI가 보이지 않습니다.

```bash
ssh user@ext1 'command -v hf || command -v huggingface-cli'
ssh user@win1 'powershell -NoProfile -Command "Get-Command hf; Get-Command huggingface-cli"'
```

필요하면 worker에 설치합니다.

```bash
ssh user@ext1 'uv tool install "huggingface_hub[hf_transfer]"'
```

또는 공식 installer를 사용할 수 있습니다.

```bash
ssh user@ext1 'curl -LsSf https://hf.co/cli/install.sh | bash'
```

`no usable temp root found`

: `temp_roots`가 없거나 권한/디스크 문제가 있습니다.

```bash
ssh user@ext1 'df -h /data/tmp /tmp'
ssh user@ext1 'mkdir -p /data/tmp/msdl-test && rmdir /data/tmp/msdl-test'
ssh user@win1 'powershell -NoProfile -Command "New-Item -ItemType Directory -Force D:/msdl-test; Remove-Item D:/msdl-test"'
```

Windows worker에서 이 오류가 나면 `servers.toml`의 해당 서버에
`platform = "windows"`가 있는지, `temp_roots`가 실제 드라이브인지 확인하세요.

`not enough local free space`

: local final mode에서는 최종 저장 루트에 manifest 총 용량만큼 여유가 필요합니다.
remote destination mode에서는 main work 경로에 최소 가장 큰 파일 하나 이상을
받을 수 있는 여유가 필요합니다.

```powershell
Get-PSDrive D
```

`not enough remote free space`

: final Linux destination에 manifest 총 용량만큼 여유가 없습니다.

```bash
ssh user@final-linux 'df -h /models'
```

`403` 또는 gated/private 모델 접근 실패

: 토큰 권한, 모델 접근 승인, `--forward-hf-token` 여부를 확인합니다.

```powershell
$env:HF_TOKEN = "hf_xxxxxxxxxxxxxxxxx"
uv run msdl download org/private-model --servers .\servers.toml --destination user@final-linux:/models --forward-hf-token
```

속도 probe가 너무 오래 걸림

: probe 크기를 줄이거나 임시로 동일 가중치를 사용합니다.

```powershell
uv run msdl download org/model `
  --servers .\servers.toml `
  --destination user@final-linux:/models `
  --speed-test-mib 16 `
  --dry-run

uv run msdl download org/model `
  --servers .\servers.toml `
  --destination user@final-linux:/models `
  --skip-speed-test `
  --dry-run
```

진행도 파일이 안 보임

: 아직 다운로드 plan 생성 전이거나, 다른 work 경로를 보고 있을 가능성이 큽니다.

```powershell
echo $env:MULTISERVER_DOWNLOAD_WORK_PATH
Get-ChildItem -Recurse "$env:MULTISERVER_DOWNLOAD_WORK_PATH\org\model\.msdl" -Filter status.json
```

latest job이 아니라 특정 job을 보고 싶으면 `--job-id`를 사용합니다.

## 16. 권장 운영 순서

1. worker별 SSH 접속과 HF CLI, Python 확인
2. `servers.toml` 작성
3. Windows main controller에서 `MULTISERVER_DOWNLOAD_WORK_PATH` 설정
4. private/gated 모델이면 `HF_TOKEN` 설정
5. `--destination user@final-linux:/models --dry-run` 실행
6. `preflight: all checks passed` 로그와 plan 파일 확인
7. 실제 다운로드 실행
8. 다른 터미널에서 `msdl status org/model --watch`로 진행도 확인
9. 최종 경로와 `.download-complete.json` 확인

최종 확인:

```bash
ssh user@final-linux 'find /models/org/model -maxdepth 2 -type f | sort'
ssh user@final-linux 'cat /models/org/model/.download-complete.json'
```
