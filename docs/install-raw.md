# Running without Docker

[← README](../README.md)

Requires Python 3.11+.

```bash
# The shared library must be cloned next to this repo
git clone https://github.com/OolaaPleur/matrix-element-call-common
git clone https://github.com/OolaaPleur/matrix-element-call-scrobbler
cd matrix-element-call-scrobbler

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt   # resolves ../matrix-element-call-common automatically

cp config/config.example.toml config/config.toml
nano config/config.toml           # settings (optional, the defaults work)
python scripts/setup.py
python main.py
```

> [!NOTE]
> `requirements.txt` references `../matrix-element-call-common` (the [shared library](https://github.com/OolaaPleur/matrix-element-call-common)). Both repos must be cloned into the same parent directory.

Logs go to `logs/scrobbler.log` (INFO and up, rotating, 10 MB × 5 files). SIGTERM/Ctrl+C stop the bot cleanly.

To change setup's answers later, stop the bot and run `python scripts/setup.py` again. To restore the cross-signing keys only: `python -m matrix_bot_common.restore_keys`, then restart the bot.

Windows: not tested. The bot is plain Python, so the same steps should work in PowerShell (`.venv\Scripts\Activate.ps1` instead of `source .venv/bin/activate`); reports welcome.

## systemd user service

To keep it running, `~/.config/systemd/user/matrix-scrobbler.service` (adjust the paths to where you cloned it):

```ini
[Unit]
Description=Matrix Element Call scrobbler
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=%h/matrix-element-call-scrobbler
ExecStart=%h/matrix-element-call-scrobbler/.venv/bin/python main.py
Environment=PYTHONUNBUFFERED=1
Restart=on-failure
RestartSec=30

[Install]
WantedBy=default.target
```

```bash
systemctl --user enable --now matrix-scrobbler
loginctl enable-linger "$USER"   # keep it running while you're logged out
```
