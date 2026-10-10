# GHAADD web viewer

A passive page that shows what a GHAADD daemon is doing: its status, the Mappings table and the Warnings, Completed,
Folder limits and Unmapped lists. It only *looks*. It has no buttons, cannot control the daemon and cannot even reach it.

The daemon **connects out** to this viewer and pushes read-only snapshots (when something changed, plus a heartbeat every
15 seconds, plus a goodbye when it stops cleanly). The viewer keeps only the latest snapshot per daemon, in memory. No
paths are ever sent. This folder is self-contained: copy it to any computer (a headless server, a NAS) that the daemon's
computer can reach. The full description is in the "Web viewer" section of the main README.

## Tokens: one per daemon

Every daemon needs a token. Make one on the daemon's computer (one per daemon is best):

```bash
python main.py --new-viewer-token
```

The viewer reads the accepted tokens from **`config/.env`** in this folder: the only file it ever reads (there is no
option to point it elsewhere); it can also take tokens from `--token` and the environment. In the file, one line holds
them all, separated by commas, semicolons or new lines:

```
GHAADD_VIEWER_TOKENS=home-pc=<token one>; nas=<token two>; <a plain token>
```

- `name=token`: this token is only accepted from the daemon with that name (`viewer.name` in the daemon's config.json,
  by default the computer's name; case does not matter). A token that leaks cannot be used to pose as another daemon.
- a plain `token`: accepted from any daemon (the same as `*=token`).
- A token must be at least 16 characters. A token you made up that contains `=` needs a name in front of it.

**The file is re-read whenever it changes.** To let a new daemon in, add its token; to lock one out, delete its token.
No restart. (`config/.env.example` shows the format.)

A refused daemon is not silent: the page shows a **Rejected connections** panel with the name it claimed, its address, why
it was refused (token not accepted, or not allowed for that name) and how often it tried. The daemon's own Viewer window
shows the same reason. The viewer's console says it once per sender.

## Run it

Plain Python 3.10+ (nothing to install), with your tokens in `config/.env` (copy `config/.env.example` first):

```bash
python3 ghaadd_viewer.py
```

Or give the tokens directly (they are not re-read, so changing them needs a restart):

```bash
python3 ghaadd_viewer.py --token home-pc=<token one> --token <a plain token>
```

Or Docker, from this folder:

```bash
cp config/.env.example config/.env      # then put your tokens in config/.env
docker compose up -d --build
```

The page is then at `http://<this-computer>:8888/`.

**Docker and the token file:** the compose file mounts this folder's `config` *folder* (read-only) at `/app/config`,
which is where the viewer looks, not the single `.env` file. A single-file mount follows the file's identity, so an
editor that saves by replacing the file (vim, VS Code, `sed -i`) would leave the container looking at the old copy. With
the folder mounted, an edit is always seen. If the viewer says "No token given" it names the exact path it looked in.

The page's icon (`assets/ghaadd.ico` and `assets/ghaadd.png`, copies of the app's own) is served from the `assets` folder
beside the script; the compose file mounts that read-only at `/app/assets` too. The page works without it, it just has no
icon. Only those two fixed files are ever served from there.

| Setting | Option | Environment variable | Default |
|---|---|---|---|
| Accepted token(s) | `--token` (repeatable) | `GHAADD_VIEWER_TOKENS` | none |
| Port | `--port` | `GHAADD_VIEWER_PORT` | 8888 |
| Address to listen on | `--host` | `GHAADD_VIEWER_HOST` | 0.0.0.0 |
| Seconds of silence before "no data received" | `--lost-after` | `GHAADD_VIEWER_LOST_AFTER` | 45 |
| Days before a lost or stopped daemon is forgotten (0 = never) | `--forget-after-days` | `GHAADD_VIEWER_FORGET_AFTER_DAYS` | 7 |

Tokens from all sources count together. At least one valid token is required, and the viewer refuses to start while an
entry in the start-up tokens or in the file is too short (it says which, never printing the token). Entries that become
bad while it runs are reported and skipped; the good ones keep working.

## Point the daemon at it

In the GUI: Settings, then **Viewer…**: tick "Send read-only snapshots", enter `http://<this-computer>:8888`, the
daemon's name and its token, press **Test connection** (it also checks a name-tied token against the name), Save, and
restart the daemon. Or in the daemon's `config.json`:

```json
"viewer": {"enabled": true, "url": "http://<this-computer>:8888", "token": "<token>", "name": "home-pc"}
```

`python main.py --push-on` / `--push-off` switch the push of a running daemon.

## Monitoring it (Homer, Uptime Kuma, a script)

`GET /api/health` answers **200** when something is live and nothing was lost, otherwise **503**, with a small JSON body
(counts and a one-line reason, never any data), so a plain up/down check can tell whether a daemon is really sending:

```json
{"ok": false, "problem": "lost: home-pc", "live": 0, "lost": 1, "stopped": 0, "total": 1}
```

- Healthy: at least one daemon is live and none is *lost* (silent without a goodbye).
- A daemon that stopped cleanly (it said goodbye) is ignored while another one is live; if all of them stopped, or none
  has ever sent data, it is 503.
- `/api/health?name=home-pc` checks just that daemon, and it must be live.
- HEAD and GET both work. In Homer: `type: Ping`, `url:` the viewer page, `endpoint: http://<viewer>:8888/api/health`.
  `/healthz` stays a plain "the viewer process is up" check.

A daemon that is lost or stopped is dropped from the list after `--forget-after-days` (default 7), so retired or
renamed daemons do not pile up (the list holds at most 20) and do not keep the health check red.

## What the page tells you

- **live**: data arrives and everything shown is current. Several daemons: a selector appears in the header.
- **daemon stopped**: the daemon said goodbye (a clean stop).
- **no data received**: nothing arrived for 45 seconds: the daemon, its computer or the network is down, the push is off,
  or its token was removed (look at the Rejected connections panel).
- **viewer unreachable**: the page itself cannot get data from the viewer.

Old numbers are never shown: when the data is not current, it is not displayed.

## Good to know

- The token is the only protection and travels in clear text over plain HTTP. That is fine on a trusted home network or
  inside Tailscale or WireGuard. For anything wider, put the viewer behind a reverse proxy with TLS.
- The page itself has no login: anyone who can open its port can read what the daemon sends, and see the rejected
  connections. Keep it on a trusted network.
- The Docker image holds only `ghaadd_viewer.py`, runs as an unprivileged user and has a read-only file system. This
  folder is the build context, so nothing else can end up in the image.
