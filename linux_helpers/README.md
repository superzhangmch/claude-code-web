# linux_helpers

systemd glue for running `cc_web` on a Linux box. The macOS side of this lives in
`../macos_helpers/`; the pieces differ because there is no launchd and no iTerm2
here — the **tmux bridge** takes over, so your `claude` sessions must run inside
tmux or the server cannot see them at all.

| File | Role |
|---|---|
| `cc-web-https-start.sh` | Picks up the Tailscale IP, refreshes the cert, execs uvicorn with TLS on 8443. Edit `CERT_NAME`. |
| `cc-web-https.service` | The **user** unit that runs it. |

```sh
cp cc-web-https-start.sh ~/claude-code-web/        # then edit CERT_NAME inside
mkdir -p ~/.config/systemd/user
cp cc-web-https.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now cc-web-https.service
journalctl --user -u cc-web-https -f
loginctl enable-linger $USER      # keep it running when you're not logged in
```

A **user** unit, not a system one: it has to run as you, with your `~/.claude`,
your venv and your tmux server.

Two things this setup deliberately gets right, both learned the hard way:

- **Every failure path sleeps before exiting.** `Restart=always` restarts
  instantly, so a hopeless respin (port already taken, tailscale down, no cert)
  spins forever. The macOS side of this project did exactly that for 5 days —
  ~10s per attempt, 44k failed binds, a 36 MB log. The script sleeps 60s
  (`CC_WEB_RETRY_SLEEP` overrides) and the unit adds `RestartSec=15`.
- **One instance per machine.** The script pre-checks for a running `cc_web`, and
  `cc_web.py` itself holds an flock on `~/.claude/cc_web.lock` (a second instance
  exits with code 3). Two instances share every stateful file under `~/.claude`
  and silently overwrite each other's summaries and bindings.
  The instance pre-check matches on `ps` args, NOT `pgrep -f "uvicorn cc_web:app"`
  — that pattern also matches any shell, ssh command or grep whose command line
  merely mentions the string, and one such false positive took the service down.

`tailscale cert` needs root on Linux. Grant it once, in a real terminal
(`sudo` cannot prompt from a non-interactive shell):

```sh
sudo tailscale set --operator=$USER
```

Then `.venv/bin/python doctor.py` will confirm it, along with everything else.
