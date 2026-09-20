# Dev Container

階層1（`SPECIFICATION.md` 10.3）。x86 / X11 / VNC 上で UI レイアウトとロジック層を
確認するための環境。ARM 挙動・KMSDRM・メモリ実態は確認できない
（`docker compose build` の既定ターゲット `runtime` はこの構成に一切依存しない）。

## 起動

1. VS Code で「Dev Containers: Reopen in Container」（初回は Rebuild）。
2. `postStartCommand` が VNC 一式（Xvfb + fluxbox + x11vnc + noVNC）を自動起動する。
3. ブラウザで `http://localhost:6080/vnc.html?show_dot=true` を開くと 1024x600 のデスクトップが見える。
4. コンテナ内で `python main.py` を実行して確認する。

## Sharing your host Claude Code config (optional)

The container mounts a Claude Code config directory at `/home/app/.claude`
(the container runs as the non-root `app` user, uid 1000).

- **Default:** an isolated, writable Docker **named volume** (`claude-config`).
  Nothing on your host is read or modified. The in-container config persists
  across rebuilds.
- **Opt in to your host config:** point the mount at your host `~/.claude` so the
  container reuses your login, settings, agents, commands, skills, and MCP auth.

### How to opt in

1. Copy `.env.sample` to `.env` (if you haven't already) and set the path to your
   host `~/.claude`:

   | Host                     | Example value                  |
   | ------------------------ | ------------------------------ |
   | Linux / macOS / WSL      | `/home/you/.claude`            |
   | Windows (Docker Desktop) | `C:\Users\you\.claude`         |

2. Run **Dev Containers: Rebuild Container**.

This works identically on macOS, Linux, WSL, and Windows — it relies only on
Docker Compose variable interpolation (`${HOST_CLAUDE_DIR:-claude-config}`) and
runs **no host-side shell scripts**, so the host shell (cmd / PowerShell / sh)
does not matter.

### Notes

- Login credentials live in `~/.claude/.credentials.json` (inside `~/.claude`),
  so sharing this directory alone carries your login into the container.
  Logging out inside the container also logs you out on the host.
- `~/.claude.json` (history, user-scoped MCP servers) is **not** mounted. Add a
  second mount in `docker-compose.claude.yml` if you need it.
- Conversation history is keyed by filesystem path; the in-container project path
  differs from the host path, so past history won't fully line up.
