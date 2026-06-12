# multiserver-download

`msdl` is a single-controller CLI for downloading a Hugging Face model through
multiple internet-facing SSH workers, then collecting the files into one final
directory on the isolated/controller server.

For a complete Korean operating guide, see
[docs/usage-guide.md](docs/usage-guide.md).

The data path does not go through an extra relay service:

```text
                         Hugging Face
                    ┌─────────┬─────────┐
                    │         │         │
                  ext1      ext2      ext3
                download  download  download
                    │         │         │
                    └──── controller pull ────▶ isolated/controller
                                                $MULTISERVER_DOWNLOAD_SAVE_PATH
```

The controller is responsible for planning and verification only:

```text
controller
  ├─ reads HF manifest
  ├─ probes each server with df and a small HF speed test
  ├─ assigns bytes according to measured speed
  ├─ runs remote downloads over SSH
  ├─ pulls completed files with rsync or scp
  └─ writes files to $SAVE_PATH/<org>/<model>
```

## Install

```bash
uv sync
```

Each worker server needs:

- SSH access from the controller
- `python3`
- `hf` or `huggingface-cli`
- enough free space in one configured temporary root

The controller needs `ssh` plus one transfer tool. On Linux/macOS, `auto`
prefers `rsync` and falls back to `scp`. On Windows, `auto` prefers the built-in
OpenSSH `scp` because native rsync path handling is inconsistent unless you
install and configure a Windows rsync distribution explicitly.

Workers only need `rsync` when the controller transfer backend is `rsync`.
Workers do not need `rsync` when the controller uses `scp`.

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
name = "ext1"
ssh_target = "user@ext1"
temp_roots = ["/data/tmp", "/tmp"]
```

`temp_roots` are checked with `df -Pk`. The controller picks a writable root
with enough free space and creates:

```text
<temp_root>/msdl/<job_id>/<org>/<model>/
```

## Run

Set the final save root on the controller:

```bash
export MULTISERVER_DOWNLOAD_SAVE_PATH=/models
uv run msdl download meta-llama/Llama-3.1-70B --servers servers.toml
```

Windows PowerShell controller example:

```powershell
$env:MULTISERVER_DOWNLOAD_SAVE_PATH = "D:\models"
uv run msdl download meta-llama/Llama-3.1-70B --servers .\servers.toml
```

The worker `temp_roots` still use Linux paths, for example `/data/tmp` or
`/tmp`, because workers are controlled over SSH.

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

It also writes the plan to:

```text
<target>/.msdl/<job_id>/plan.json
```

## Private Models

By default workers use their own Hugging Face credentials. If the controller has
the token and workers do not, pass:

```bash
uv run msdl download org/private-model --servers servers.toml --forward-hf-token
```

The token is forwarded only to the remote command environment and is not printed
in logs.

## Transfer Behavior

`msdl` does not transfer a whole Hugging Face cache or a large directory tree.
It pulls exactly one completed file at a time:

```text
remote temp file -> local .incoming file -> size check -> final rename
```

With `rsync`, the default flags are optimized for large model files:

```text
--partial --append-verify --whole-file --no-compress
```

This avoids the expensive initial scan pattern that makes rsync slow on huge
cache directories. With `scp`, the transfer is simpler and works well on a
Windows controller, but interrupted file transfers restart instead of using
rsync's append verification.

## Development

```bash
uv sync --dev
uv run pytest
```
