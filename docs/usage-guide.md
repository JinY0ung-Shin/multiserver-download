# 운영 가이드

이 문서는 controller 하나를 두고, 여러 Linux 서버를 SSH worker로 사용해
Hugging Face 모델을 병렬 다운로드하는 운영 절차입니다. controller는 Linux,
macOS, Windows 모두 사용할 수 있습니다.

## 1. 구조 이해

```text
                         Hugging Face
                    ┌─────────┬─────────┐
                    │         │         │
                  ext1      ext2      ext3
                download  download  download
                    │         │         │
                    └──── controller pull ────▶ isolated/controller
                                                final model directory
```

역할은 두 개입니다.

- controller: `uv run msdl ...`을 실행하는 중심 서버입니다. 격리 서버나
  Windows PC가 이 역할을 맡을 수 있습니다.
- worker: 인터넷으로 Hugging Face에 접근 가능한 Linux 서버입니다. 별도 서비스는
  돌리지 않고, controller가 SSH로 POSIX 명령을 실행합니다.

중요한 원칙은 모델 파일이 별도 중계 서버를 거치지 않는다는 점입니다. 각 worker가
Hugging Face에서 자기 몫을 임시 디렉터리에 받고, controller가 해당 파일만
`rsync` 또는 `scp`로 가져옵니다.

## 2. 최종 저장 경로

최종 저장 루트는 controller에서 환경변수로 설정합니다.

```bash
export MULTISERVER_DOWNLOAD_SAVE_PATH=/models
```

예를 들어 repo id가 `meta-llama/Llama-3.1-70B`이면 최종 경로는 아래처럼
만들어집니다.

```text
/models/meta-llama/Llama-3.1-70B/
```

환경변수를 쓰지 않고 한 번만 지정하려면 `--save-path`를 사용할 수 있습니다.

```bash
uv run msdl download meta-llama/Llama-3.1-70B \
  --servers servers.toml \
  --save-path /models
```

둘 중 하나는 반드시 필요합니다. 지정하지 않으면 실행이 중단됩니다.

## 3. Hugging Face 토큰

public 모델이면 토큰 없이도 동작할 수 있습니다. private/gated 모델이면 토큰이
필요합니다.

controller에서 토큰을 설정합니다.

```bash
export HF_TOKEN=hf_xxxxxxxxxxxxxxxxx
```

worker 서버들이 이미 `hf auth login` 되어 있다면 추가 옵션 없이 실행해도 됩니다.
worker에 토큰이 없고 controller의 토큰을 원격 명령에만 전달하려면
`--forward-hf-token`을 붙입니다.

```bash
uv run msdl download org/private-model \
  --servers servers.toml \
  --forward-hf-token
```

토큰은 로그에 출력하지 않습니다. 그래도 shell history나 운영 환경 정책을 고려해
장기 저장 파일에는 토큰을 직접 적지 않는 편이 안전합니다.

실행 후 현재 shell에서 토큰을 지우려면:

```bash
unset HF_TOKEN
```

## 4. controller 준비

controller에서 이 저장소를 받은 뒤 의존성을 설치합니다.

```bash
uv sync
```

필요한 기본 도구는 다음과 같습니다.

- `uv`
- `ssh`
- `rsync` 또는 `scp`
- 최종 저장소에 충분한 디스크 공간

전송 백엔드 기본값은 `auto`입니다.

- Linux/macOS controller: `rsync`가 있으면 `rsync`, 없으면 `scp`를 사용합니다.
- Windows controller: 기본 OpenSSH `scp`를 먼저 사용합니다. Windows용 rsync를
  직접 설치하고 경로 변환까지 확인한 경우에만 `--transfer-backend rsync`를
  명시하는 편이 안전합니다.

로컬 테스트 명령:

```bash
uv run msdl --help
uv run msdl download --help
```

### 4.1 Windows controller 준비

Windows를 main/controller로 사용할 수 있습니다. 이 경우 Linux worker들이
Hugging Face에서 병렬 다운로드하고, Windows controller가 완성된 파일을 받아
최종 디렉터리에 저장합니다.

Windows controller 준비물:

- PowerShell 또는 Windows Terminal
- `uv`
- OpenSSH Client의 `ssh`/`scp`
- 최종 저장 디스크, 예: `D:\models`

PowerShell에서 확인:

```powershell
uv --version
ssh -V
Get-Command scp
```

최종 저장 경로 설정:

```powershell
$env:MULTISERVER_DOWNLOAD_SAVE_PATH = "D:\models"
```

dry-run:

```powershell
uv run msdl download meta-llama/Llama-3.1-70B `
  --servers .\servers.toml `
  --dry-run
```

실제 다운로드:

```powershell
uv run msdl download meta-llama/Llama-3.1-70B `
  --servers .\servers.toml
```

Windows에서는 `auto`가 기본적으로 `scp`를 선택합니다. 명시하려면 아래처럼
실행합니다.

```powershell
uv run msdl download meta-llama/Llama-3.1-70B `
  --servers .\servers.toml `
  --transfer-backend scp
```

중요한 경로 구분:

- `--save-path` 또는 `MULTISERVER_DOWNLOAD_SAVE_PATH`: Windows controller의
  로컬 경로입니다. 예: `D:\models`
- `temp_roots`: Linux worker의 원격 경로입니다. 예: `/data/tmp`, `/tmp`

SSH key를 지정해야 하면:

```powershell
uv run msdl download org/model `
  --servers .\servers.toml `
  --ssh-option "-i C:\Users\me\.ssh\id_ed25519" `
  --ssh-option "-o StrictHostKeyChecking=accept-new"
```

Hugging Face repo 안에 Windows에서 만들 수 없는 파일명(`CON`, `NUL`, `:`,
`*`, `?` 등)이 있으면 Windows controller에서는 manifest 처리 단계에서 중단됩니다.
일반적인 모델 파일명은 대부분 문제없이 저장됩니다.

## 5. worker 준비

각 worker에는 아래가 필요합니다.

- controller에서 SSH 접속 가능
- Hugging Face 접속 가능
- `python3`
- `hf` 또는 `huggingface-cli`
- `scp` 백엔드만 쓸 때는 별도 전송 도구 불필요
- `rsync` 백엔드를 쓸 때는 worker에도 `rsync` 필요
- 임시 다운로드를 저장할 충분한 디스크 공간

권장 설치:

```bash
uv tool install "huggingface_hub[hf_transfer]"
```

`hf_transfer`가 있으면 worker 원격 다운로드 명령에서 자동으로
`HF_HUB_ENABLE_HF_TRANSFER=1`을 켭니다. 설치되어 있지 않아도 동작은 하지만
서버당 다운로드 속도가 낮을 수 있습니다.

SSH로 들어갔을 때 `hf`가 보이는지 확인합니다.

```bash
ssh user@ext1 'command -v hf || command -v huggingface-cli'
ssh user@ext1 'python3 --version'
ssh user@ext1 'command -v rsync || true'
```

Windows controller 기본값인 `scp` 백엔드를 쓸 예정이면 worker의 `rsync` 확인은
필수는 아닙니다.

`uv tool install` 후 SSH 비대화형 세션에서 `hf`가 안 보이면 worker의 PATH에
`~/.local/bin`이 포함되도록 shell 설정을 조정하거나, 시스템 경로에 설치하세요.

## 6. 서버 설정 파일

`servers.example.toml`을 복사해 실제 서버 목록을 만듭니다.

```bash
cp servers.example.toml servers.toml
```

예시:

```toml
[[servers]]
name = "ext1"
ssh_target = "user@ext1"
temp_roots = ["/data/tmp", "/tmp"]

[[servers]]
name = "ext2"
ssh_target = "user@ext2"
temp_roots = ["/mnt/nvme/tmp", "/tmp"]
```

필드 의미:

- `name`: 로그와 plan 파일에 표시되는 worker 이름입니다.
- `ssh_target`: controller에서 접속할 SSH 대상입니다. `~/.ssh/config` alias도
  사용할 수 있습니다.
- `temp_roots`: worker에서 임시 파일을 둘 후보 디렉터리입니다.

실행 전 각 `temp_roots`는 `df -Pk`로 검사됩니다. controller는 여유 공간이 가장
큰 사용 가능한 root를 고르고 아래 디렉터리를 만듭니다.

```text
<temp_root>/msdl/<job_id>/<org>_<model>/
```

## 7. dry-run으로 사전 점검

실제 다운로드 전에 반드시 dry-run을 먼저 실행하는 것을 권장합니다.

```bash
export MULTISERVER_DOWNLOAD_SAVE_PATH=/models

uv run msdl download meta-llama/Llama-3.1-70B \
  --servers servers.toml \
  --dry-run
```

dry-run에서 수행하는 일:

- Hugging Face manifest 조회
- controller 최종 저장소 여유 공간 확인
- worker별 `df` 확인
- worker별 Hugging Face 속도 probe
- 속도 기준 다운로드 용량 분배
- plan 파일 기록

다운로드는 하지 않습니다.

## 8. 실제 다운로드

```bash
uv run msdl download meta-llama/Llama-3.1-70B \
  --servers servers.toml
```

private/gated 모델:

```bash
export HF_TOKEN=hf_xxxxxxxxxxxxxxxxx

uv run msdl download org/private-model \
  --servers servers.toml \
  --forward-hf-token
```

특정 revision을 고정하려면:

```bash
uv run msdl download org/model \
  --servers servers.toml \
  --revision 0123456789abcdef
```

일부 파일만 받으려면:

```bash
uv run msdl download org/model \
  --servers servers.toml \
  --include "*.safetensors" \
  --include "config.json" \
  --include "tokenizer*"
```

## 9. 시작 로그에서 확인할 것

시작하면 아래 항목이 로그에 나와야 합니다.

```text
repo: org/model
revision: main
save root: /models
final target: /models/org/model
manifest: 42 files, 132.4 GiB
probe ext1: temp=/data/tmp/msdl/... free=900.0 GiB speed=210.0 MiB/s
probe ext2: temp=/mnt/nvme/tmp/msdl/... free=700.0 GiB speed=120.0 MiB/s
download plan:
  ext1 -> 26 files, 84.0 GiB
  ext2 -> 16 files, 48.4 GiB
```

확인 포인트:

- `final target`이 의도한 경로인지
- worker별 `temp`가 기대한 디스크인지
- worker별 `free`가 충분한지
- 빠른 worker에 더 많은 용량이 배정됐는지
- plan 파일 경로가 생성됐는지

plan 파일은 아래에 저장됩니다.

```text
<final target>/.msdl/<job_id>/plan.json
```

진행도 파일도 같은 job 디렉터리에 저장됩니다.

```text
<final target>/.msdl/<job_id>/status.json
```

## 10. 중간 진행도 확인

다운로드 중에는 로그에 파일 단위 진행과 전체 누적 진행도가 같이 표시됩니다.

```text
ext1 download model-00001-of-00030.safetensors (4.6 GiB); running: 0/30 files, 0.0 B/138.0 GiB (0.00%)
ext1 done model-00001-of-00030.safetensors; running: 1/30 files, 4.6 GiB/138.0 GiB (3.33%)
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
target: /models/meta-llama/Llama-3.1-70B
updated: 2026-06-13T12:34:56+00:00
servers:
  ext1: 5/12 files, 23.0 GiB/55.0 GiB, downloading=model-00013-of-00030.safetensors
  ext2: 4/10 files, 18.4 GiB/46.0 GiB, transferring=model-00014-of-00030.safetensors
  ext3: 3/8 files, 13.8 GiB/37.0 GiB
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

`status` 명령도 `MULTISERVER_DOWNLOAD_SAVE_PATH`를 사용합니다. 환경변수를 쓰지
않으려면 `--save-path`를 같이 넘기면 됩니다.

```bash
uv run msdl status meta-llama/Llama-3.1-70B --save-path /models
```

## 11. 전송 방식

`msdl`은 HF cache 전체나 모델 루트 전체를 동기화하지 않습니다. worker가 파일 하나를
다운로드하면 controller가 그 파일 하나만 가져옵니다.

```text
worker temp file
  -> controller .msdl/incoming/<path>.part
  -> size check
  -> final path로 rename
```

전송 백엔드는 `auto`, `rsync`, `scp` 중 하나입니다.

- Linux/macOS controller의 `auto`: `rsync` 우선, 없으면 `scp`
- Windows controller의 `auto`: `scp` 우선, 없으면 `rsync`

`rsync`를 사용할 때 기본 옵션:

```text
--partial --append-verify --whole-file --no-compress
```

의미:

- `--partial`: 중간에 끊긴 파일을 남겨 재시도 비용을 줄입니다.
- `--append-verify`: 이어받은 뒤 검증합니다.
- `--whole-file`: LAN 전송에서 불필요한 delta 계산을 피합니다.
- `--no-compress`: 이미 압축에 가까운 safetensors를 다시 압축하지 않습니다.

따라서 초기 rsync 스캔이 느려지는 “큰 디렉터리 전체 비교” 패턴을 피합니다.

`scp`는 Windows controller에서 가장 단순하게 동작하는 기본 선택입니다. 다만
중간에 끊긴 파일을 rsync처럼 append-verify로 이어받지는 못하고, 해당 파일 전송을
다시 시작합니다.

## 12. 재시도와 중단 후 복구

이미 최종 경로에 있고 크기가 맞는 파일은 다음 실행에서 skip합니다.

```text
ext1 skip existing model-00001-of-00030.safetensors
```

중단 후 같은 명령을 다시 실행하면:

- 최종 완료 파일은 skip
- 미완료 파일은 다시 worker에 배정
- 같은 파일의 `.part`가 남아 있고 `rsync` 백엔드를 쓰면 append-verify 재개에 사용
- `scp` 백엔드는 `.part`를 덮어쓰며 다시 전송

worker 임시 파일을 지우지 않고 남기려면:

```bash
uv run msdl download org/model \
  --servers servers.toml \
  --keep-remote
```

기본값은 최종 전송이 끝난 worker 임시 파일을 삭제합니다.

## 13. 자주 쓰는 옵션

```text
--servers servers.toml        worker 설정 파일
--revision REV                branch, tag, commit 고정
--include PATTERN             받을 파일 패턴, 여러 번 사용 가능
--exclude PATTERN             제외할 파일 패턴, 여러 번 사용 가능
--save-path PATH              env 대신 최종 저장 루트 지정
--speed-test-mib N            worker별 속도 측정 크기, 기본 64MiB
--skip-speed-test             속도 측정 없이 동일 가중치로 분배
--reserve-gib N               worker temp root에 남길 여유 공간, 기본 5GiB
--forward-hf-token            controller의 HF_TOKEN을 worker 원격 명령에 전달
--ssh-option OPTION           ssh 옵션 추가, 여러 번 사용 가능
--transfer-backend auto|rsync|scp
                              worker에서 controller로 가져오는 방식, 기본 auto
--keep-remote                 전송 후 worker 임시 파일 보존
--dry-run                     probe와 plan만 실행
-v                            debug 로그 출력
```

진행도 조회 옵션:

```text
msdl status org/model
--job-id JOB                  특정 job 상태 조회
--watch [SECONDS]             주기적으로 상태 갱신
--json                        raw status JSON 출력
--save-path PATH              env 대신 최종 저장 루트 지정
```

SSH 옵션 예시:

```bash
uv run msdl download org/model \
  --servers servers.toml \
  --ssh-option "-i ~/.ssh/company_key" \
  --ssh-option "-o StrictHostKeyChecking=accept-new"
```

## 14. 문제 해결

`set MULTISERVER_DOWNLOAD_SAVE_PATH or pass --save-path`

: controller에서 최종 저장 경로가 지정되지 않은 상태입니다.

```bash
export MULTISERVER_DOWNLOAD_SAVE_PATH=/models
```

`missing hf or huggingface-cli on worker`

: worker에서 HF CLI가 보이지 않습니다.

```bash
ssh user@ext1 'command -v hf || command -v huggingface-cli'
```

필요하면 worker에 설치합니다.

```bash
ssh user@ext1 'uv tool install "huggingface_hub[hf_transfer]"'
```

`no usable temp root found`

: `temp_roots`가 없거나 권한/디스크 문제가 있습니다.

```bash
ssh user@ext1 'df -h /data/tmp /tmp'
ssh user@ext1 'mkdir -p /data/tmp/msdl-test && rmdir /data/tmp/msdl-test'
```

`not enough local free space`

: controller의 최종 저장 루트에 manifest 총 용량만큼 여유가 없습니다.

```bash
df -h "$MULTISERVER_DOWNLOAD_SAVE_PATH"
```

`403` 또는 gated/private 모델 접근 실패

: 토큰 권한, 모델 접근 승인, `--forward-hf-token` 여부를 확인합니다.

```bash
export HF_TOKEN=hf_xxxxxxxxxxxxxxxxx
uv run msdl download org/private-model --servers servers.toml --forward-hf-token
```

속도 probe가 너무 오래 걸림

: probe 크기를 줄이거나 임시로 동일 가중치를 사용합니다.

```bash
uv run msdl download org/model \
  --servers servers.toml \
  --speed-test-mib 16 \
  --dry-run

uv run msdl download org/model \
  --servers servers.toml \
  --skip-speed-test \
  --dry-run
```

진행도 파일이 안 보임

: 아직 다운로드 plan 생성 전이거나, 다른 저장 경로를 보고 있을 가능성이 큽니다.

```bash
echo "$MULTISERVER_DOWNLOAD_SAVE_PATH"
find "$MULTISERVER_DOWNLOAD_SAVE_PATH/org/model/.msdl" -name status.json -print
```

latest job이 아니라 특정 job을 보고 싶으면 `--job-id`를 사용합니다.

## 15. 권장 운영 순서

1. worker별 SSH 접속과 `hf`, `python3` 확인
2. `servers.toml` 작성
3. controller에서 `MULTISERVER_DOWNLOAD_SAVE_PATH` 설정
4. private/gated 모델이면 `HF_TOKEN` 설정
5. `--dry-run` 실행
6. 시작 로그와 plan 파일 확인
7. 실제 다운로드 실행
8. 다른 터미널에서 `msdl status org/model --watch`로 진행도 확인
9. 최종 경로와 `.download-complete.json` 확인

최종 확인:

```bash
find "$MULTISERVER_DOWNLOAD_SAVE_PATH/org/model" -maxdepth 2 -type f | sort
cat "$MULTISERVER_DOWNLOAD_SAVE_PATH/org/model/.download-complete.json"
```
