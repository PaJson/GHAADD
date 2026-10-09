# GHAADD web viewer

A passive page that shows what a GHAADD daemon is doing: its status, the Mappings table and the Warnings, Completed,
Folder limits and Unmapped lists. It only *looks*. It has no buttons, cannot control the daemon and cannot even reach it.

The daemon **connects out** to this viewer and pushes read-only snapshots (when something changed, plus a heartbeat every
15 seconds, plus a goodbye when it stops cleanly). The viewer keeps only the latest snapshot per daemon, in memory. No
paths are ever sent. This folder is self-contained: copy it to any computer (a headless server, a NAS) that the daemon's
computer can reach. The full description is in the "Web viewer" section of the main README.

## Run it

You need a token first. On the computer that runs the daemon:

```bash
python main.py --new-viewer-token
```

Then, on the computer that shows the page, either plain Python 3.10+ (nothing to install):

```bash
python3 ghaadd_viewer.py --token <token>
```

or Docker (from this folder; the token is required):

```bash
GHAADD_VIEWER_TOKENS=<token> docker compose up -d --build
```

The page is then at `http://<this-computer>:8888/`.

| Setting | Option | Environment variable (also what Docker uses) | Default |
|---|---|---|---|
| Accepted token(s) | `--token` (repeatable) | `GHAADD_VIEWER_TOKENS` (comma separated) | required, at least 16 characters |
| Port | `--port` | `GHAADD_VIEWER_PORT` | 8888 |
| Address to listen on | `--host` | `GHAADD_VIEWER_HOST` | 0.0.0.0 |
| Seconds of silence before "no data received" | `--lost-after` | `GHAADD_VIEWER_LOST_AFTER` | 45 |

## Point the daemon at it

In the GUI: Settings, then **Viewer…**: tick "Send read-only snapshots", enter `http://<this-computer>:8888` and the
token, press **Test connection**, Save, and restart the daemon. Or in the daemon's `config.json`:

```json
"viewer": {"enabled": true, "url": "http://<this-computer>:8888", "token": "<token>"}
```

`python main.py --push-on` / `--push-off` switch the push of a running daemon.

## What the page tells you

- **live**: data arrives and everything shown is current.
- **daemon stopped**: the daemon said goodbye (a clean stop).
- **no data received**: nothing arrived for 45 seconds: the daemon, its computer or the network is down, or the push is off.
- **viewer unreachable**: the page itself cannot get data from the viewer.

Old numbers are never shown: when the data is not current, it is not displayed.

## Good to know

- The token is the only protection and travels in clear text over plain HTTP. That is fine on a trusted home network or
  inside Tailscale or WireGuard. For anything wider, put the viewer behind a reverse proxy with TLS.
- The page itself has no login: anyone who can open its port can read what the daemon sends. Keep it on a trusted network.
- Several daemons can push to one viewer (give each a different `viewer.name`); the page then offers a selector.
- The Docker image holds only `ghaadd_viewer.py`, runs as an unprivileged user and has a read-only file system. This
  folder is the build context, so nothing else can end up in the image.
