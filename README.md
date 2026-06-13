# multiserver-download

`msdl` is a single-controller CLI for downloading a Hugging Face model through
multiple internet-facing SSH workers, then collecting the files into one final
directory. The controller can also push the completed files to an inbound-only
Linux destination over SSH. Workers can be local, Linux, or Windows.

For a complete Korean operating guide, see
[docs/usage-guide.md](docs/usage-guide.md).

The data path does not go through an extra relay service:

```text
                         Hugging Face
                    ┌─────────┬─────────┐
                    │         │         │
                  main     win1    linux1
                download  download  download
                    │         │         │
                    └──── main controller relay ────▶ final Linux destination
                                                      user@final:/models
```

The controller is responsible for planning and verification only:

```text
controller
  ├─ reads HF manifest
  ├─ probes each server for free space and HF speed
  ├─ assigns bytes according to measured speed
  ├─ runs remote downloads over SSH
  ├─ pulls completed files with rsync or scp when needed
  └─ writes locally or pushes to --destination <ssh-target>:/path
```

## Install

```bash
uv sync
```

If the final Linux server cannot open outbound connections, run `msdl` on the
Windows main worker/controller that can reach Hugging Face, the workers, and the
final Linux server. The final Linux server is not listed in `servers.toml`; pass
it as `--destination user@final:/models`.

The main controller can participate as a worker without SSH:

```toml
[[servers]]
name = "main"
local = true
platform = "windows"
temp_roots = ["D:/msdl-main-tmp"]
```

Each Linux worker needs:

- SSH access from the controller
- `python3`
- `hf` or `huggingface-cli`
- enough free space in one configured temporary root

Each Windows worker needs:

- SSH access from the controller, usually OpenSSH Server
- PowerShell
- `python` or `py -3`
- `hf` or `huggingface-cli`
- enough free space in one configured temporary root, such as `D:/msdl-tmp`

The Windows main controller needs `ssh` and `scp`. In `auto` mode on Windows,
worker pulls and final-destination pushes use `scp`.

For faster per-server download, install `hf_transfer` on worker servers. `msdl`
enables `HF_HUB_ENABLE_HF_TRANSFER=1` only when the package is present.

One simple worker setup path is:

```bash
uv tool install "huggingface_hub[hf_transfer]"
```

## Configure Servers

Copy `servers.example.toml` and edit it:

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
temp_roots = ["/data/tmp", "/tmp"]

[[servers]]
name = "linux2"
platform = "linux"
ssh_target = "user@linux2"
temp_roots = ["/data/tmp", "/tmp"]
```

`local = true` means the controller itself is a worker, so `ssh_target` is not
needed. Remote workers still require `ssh_target`. `platform` defaults to
`linux` when omitted. Linux `temp_roots` are checked with `df -Pk`; Windows
`temp_roots` are checked through PowerShell. The controller picks a writable
root with enough free space and creates:

```text
<temp_root>/msdl/<job_id>/<org>/<model>/
```

## Run

Local final destination on the Windows main controller:

```powershell
$env:MULTISERVER_DOWNLOAD_SAVE_PATH = "D:\models"
uv run msdl download meta-llama/Llama-3.1-70B --servers .\servers.toml
```

Inbound-only final Linux destination from Windows PowerShell:

```powershell
$env:MULTISERVER_DOWNLOAD_WORK_PATH = "D:\msdl-work"

uv run msdl download meta-llama/Llama-3.1-70B `
  --servers .\servers.toml `
  --destination user@final:/models
```

The final layout is:

```text
/models/
  meta-llama/
    Llama-3.1-70B/
      config.json
      tokenizer.json
      model-00001-of-00030.safetensors
      ...
```

At startup, `msdl` logs:

- final destination
- revision
- selected temp directory per server
- measured speed per server
- free space per server
- assigned bytes and file counts

It also writes the plan to the local target/work directory:

```text
<target>/.msdl/<job_id>/plan.json
```

## Private Models

By default workers use their own Hugging Face credentials. If the controller has
the token and workers do not, pass:

```powershell
$env:HF_TOKEN = "hf_xxxxxxxxxxxxxxxxx"

uv run msdl download org/private-model `
  --servers .\servers.toml `
  --destination user@final:/models `
  --forward-hf-token
```

The token is forwarded only to the remote command environment and is not printed
in logs.

## Transfer Behavior

`msdl` does not transfer a whole Hugging Face cache or a large directory tree.
It pulls exactly one completed file at a time:

```text
worker temp file -> controller .incoming file when needed -> size check -> final rename/push
```

On a Windows main controller, `auto` uses `scp` for worker pulls and final
destination pushes. Interrupted `scp` transfers restart for that file.

## Development

```bash
uv sync --dev
uv run pytest
```
