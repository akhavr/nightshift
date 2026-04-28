# Global Watchdog Design

Monitor multiple nightshift watcher instances across projects.

## Overview

Each `nightshift watcher` registers itself in `~/.nightshift/projects.d/{project}.yaml`:

```yaml
path: /home/user/src/my-project
log: /home/user/src/my-project/.nightshift/watcher.log
pid: 12345
started: 2026-04-27T10:00:00+00:00
```

The global watchdog scans these registrations and monitors all watchers.

## Components

### 1. Watchdog Script (`~/.nightshift/watchdog.py`)

```python
# Core loop:
# 1. Scan ~/.nightshift/projects.d/*.yaml
# 2. For each registration:
#    - Check if PID is alive (os.kill(pid, 0))
#    - If dead: alert "Watcher crashed for {project}"
#    - Tail log file for error patterns
# 3. Sleep and repeat
```

### 2. Error Detection

Patterns to alert on:
- `Exception` / `Traceback`
- `bug doesn't exist` (repeated, not one-off)
- `failed` (excluding "no label added or removed")
- Session stuck in same state for >30 min
- Disk space warnings

### 3. Alerting

Use existing notifier infrastructure:
- Telegram (primary)
- Webhook (optional)

Config in `~/.nightshift/watchdog.yaml`:
```yaml
poll_interval_s: 60
alert_cooldown_s: 300  # don't spam same error
notifications:
  - kind: telegram
    token: $TELEGRAM_BOT_TOKEN
    chat_id: $TELEGRAM_CHAT_ID
```

### 4. Stale Registration Cleanup

If PID is dead and `started` is >24h old, remove the registration file.

## CLI

```bash
nightshift watchdog              # start global watchdog
nightshift watchdog --list       # list registered watchers
nightshift watchdog --check      # one-shot health check
```

## Implementation

1. `host/watchdog/` package:
   - `scanner.py` - read registrations, check PIDs
   - `log_monitor.py` - tail logs, detect errors
   - `alerter.py` - send notifications
   - `main.py` - entry point

2. Registration format is already implemented in `host/watcher/registration.py`

3. Notifier infrastructure exists in `adapters/notifiers/`

## Deployment

Run as systemd user service or cron:

```bash
# systemd
systemctl --user enable nightshift-watchdog
systemctl --user start nightshift-watchdog

# or cron (every minute)
* * * * * python ~/.nightshift/watchdog.py --check
```
