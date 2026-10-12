"""Make the bot start on boot/login and restart itself if it crashes.

  .venv/bin/python autostart.py install    # turn auto-start on (and start the bot now)
  .venv/bin/python autostart.py uninstall  # turn it off (and stop the bot)
  .venv/bin/python autostart.py status

macOS: a launchd LaunchAgent (runs at login; restarts 60s after a crash; `caffeinate -s` keeps the
Mac awake while plugged in). Windows: a Task Scheduler task at logon running a restart loop.
Linux: a systemd user service with Restart=always.
"""
import os
import platform
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = ROOT / (".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python")
LABEL = "com.tradingbot.paper"


def run(*cmd, check=False):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if check and r.returncode:
        sys.exit(f"command failed: {' '.join(cmd)}\n{r.stderr}")
    return r


# ---- macOS ------------------------------------------------------------------------------
PLIST = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"


def mac_install():
    (ROOT / "logs").mkdir(exist_ok=True)
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    PLIST.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key><array>
    <string>/usr/bin/caffeinate</string><string>-s</string>
    <string>{PY}</string><string>{ROOT / 'run_bot.py'}</string>
  </array>
  <key>WorkingDirectory</key><string>{ROOT}</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>60</integer>
  <key>StandardOutPath</key><string>{ROOT / 'logs/autostart.out'}</string>
  <key>StandardErrorPath</key><string>{ROOT / 'logs/autostart.err'}</string>
</dict></plist>
""")
    run("launchctl", "unload", str(PLIST))
    run("launchctl", "load", "-w", str(PLIST), check=True)


def mac_uninstall():
    if PLIST.exists():
        run("launchctl", "unload", "-w", str(PLIST))
        PLIST.unlink()


def mac_status():
    r = run("launchctl", "list", LABEL)
    if r.returncode:
        return "not installed"
    pid = next((l.split("=")[1].strip(" ;") for l in r.stdout.splitlines() if '"PID"' in l), None)
    return f"installed, {'running (pid ' + pid + ')' if pid else 'NOT running right now'}"


# ---- Windows ----------------------------------------------------------------------------
BAT = ROOT / "run_forever.bat"


def win_install():
    BAT.write_text(f'@echo off\ncd /d "{ROOT}"\n:loop\n"{PY}" run_bot.py\nif %errorlevel%==0 goto :eof\n'
                   f'timeout /t 60 /nobreak >nul\ngoto loop\n')
    run("schtasks", "/Create", "/F", "/SC", "ONLOGON", "/RL", "LIMITED", "/TN", LABEL, "/TR", f'"{BAT}"', check=True)
    run("schtasks", "/Run", "/TN", LABEL)


def win_uninstall():
    run("schtasks", "/End", "/TN", LABEL)
    run("schtasks", "/Delete", "/F", "/TN", LABEL)


def win_status():
    r = run("schtasks", "/Query", "/TN", LABEL)
    return r.stdout.strip() if r.returncode == 0 else "not installed"


# ---- Linux ------------------------------------------------------------------------------
UNIT = Path.home() / ".config/systemd/user/tradingbot.service"


def linux_install():
    UNIT.parent.mkdir(parents=True, exist_ok=True)
    UNIT.write_text(f"[Unit]\nDescription=Trading bot (paper)\nAfter=network-online.target\n\n[Service]\n"
                    f"WorkingDirectory={ROOT}\nExecStart={PY} {ROOT / 'run_bot.py'}\nRestart=on-failure\nRestartSec=60\n\n"
                    f"[Install]\nWantedBy=default.target\n")
    run("systemctl", "--user", "daemon-reload", check=True)
    run("systemctl", "--user", "enable", "--now", "tradingbot", check=True)
    run("loginctl", "enable-linger", os.environ.get("USER", ""))  # keep running without a login session


def linux_uninstall():
    run("systemctl", "--user", "disable", "--now", "tradingbot")
    UNIT.unlink(missing_ok=True)


def linux_status():
    return run("systemctl", "--user", "is-active", "tradingbot").stdout.strip() or "not installed"


OS = {"Darwin": (mac_install, mac_uninstall, mac_status), "Windows": (win_install, win_uninstall, win_status),
      "Linux": (linux_install, linux_uninstall, linux_status)}

if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "status"
    install, uninstall, status = OS[platform.system()]
    if action == "install":
        install()
        print(f"auto-start ON ({platform.system()}). Status: {status()}")
    elif action == "uninstall":
        uninstall()
        print("auto-start OFF, bot stopped.")
    else:
        print(status())
