# よく使うコマンド・手順

## 検証環境の3階層

**どの階層で確認したかを常に明示すること。上位で動いても下位で動く保証はない。**

| 階層 | 環境 | SDL ドライバ | 検証できること |
|---|---|---|---|
| 1. Dev Container | x86 / VNC | `x11` | UI レイアウト、ロジック層、Immich 疎通 |
| 2. 実機ホスト直実行 | ARM64 | `kmsdrm` | KMSDRM 描画、タッチ、GPIO、消灯、fps |
| 3. 実機コンテナ | ARM64 / Docker | `kmsdrm` | **最終的な運用形態のすべて** |

**最終的な性能・メモリの数値は階層3で測ったものを正とする。**
階層1で動いたことを根拠に実機の挙動を主張しない（アーキテクチャ・SDL ドライバ・GPU・RAM が
すべて異なる）。階層2で動いても、コンテナ特有の制約（DRM master、デバイス GID）は別問題。

## 開発環境（階層1）

VS Code Dev Containers を使う。`.devcontainer/devcontainer.json` が
`docker-compose.dev.yml`（`target: dev`）と `docker-compose.claude.yml` を読む。

-   GUI は VNC 経由で見る: **http://localhost:6080/vnc.html?show_dot=true**
    アプリが `pg.mouse.set_visible(False)`（`src/gui/renderer.py`。実機のタッチパネルで
    カーソルを出さないため）でカーソルを消すので、`show_dot` を付けないと noVNC 上で
    カーソルが見えない。設定はブラウザに保存されるため、別ブラウザでは再指定が要る
-   **解像度は実機と同じ 1024x600**（`.devcontainer/start-vnc.sh`）
-   実行ユーザーは `app`（uid 1000）。`SDL_VIDEODRIVER=x11` /
    `SDL_RENDER_DRIVER=opengl` / `IS_DEV_ENVIRONMENT=true` はイメージの ENV で入る
-   **Python 依存はイメージに焼いてある**（venv は作らない）。
    pygame-ce / SDL / Python のバージョンは実機と一致する
-   `PF_CONFIG_DIR` / `PF_CACHE_DIR` は設定しない。ワークスペースの
    `./config` / `./cache` へフォールバックする
-   開発用の Immich は立てない。`.env` の `IMMICH_BASE_URL` / `IMMICH_API_KEY` で
    既存のセルフホスト Immich を指す
-   **`.env` を書き換えてもコンテナ内の環境変数は更新されない**（`env_file` は
    コンテナ作成時にしか適用されない）。Rebuild せずに試すときは
    `export $(grep -E '^IMMICH_' .env | xargs)` してから実行する
-   **階層1で消灯すると黒い「復帰待ちウィンドウ」になる**（2026-09-15 から）。
    `IS_DEV_ENVIRONMENT=true` のときだけ `gui/renderer.py` の `poll_wake_window()` が
    別の pg.display コンテキストでこのウィンドウを開き、noVNC 上でのクリック・タップ・
    キー入力で `main.py` の `_poll_dev_wake_window()` が `turn_on()` する。
    SDL の破棄→再生成（`generation` の推移）はそのまま通るので、テクスチャの
    世代追従を確認する用途にも使える。**消灯そのものを挟みたくない目視では、
    従来どおり `power_saving_enabled` を false にしてよい。**
-   ホストの `~/.claude` を共有したい場合は `.env` に `HOST_CLAUDE_DIR` を設定して
    Rebuild する（未設定なら名前付きボリュームへフォールバックし、ホストに触れない）

### 別解像度の UI を目視する（階層1）

`start-vnc.sh` は 1024x600 固定（`RESOLUTION` と x11vnc の `-clip` の2か所）だが、
**書き換えずに別ディスプレイを並べれば2つの解像度を同時に見比べられる。**
UI の解像度スケーリング（`Renderer.ui_scale` / `px()`）の確認に使う。

```bash
# :2 に 1920x1080 を立てる（各行を && で繋がないこと。理由は後述）
nohup Xvfb :2 -screen 0 1920x1080x24 > /tmp/xvfb2.log 2>&1 &
DISPLAY=:2 nohup fluxbox > /tmp/fluxbox2.log 2>&1 &
nohup env -u WAYLAND_DISPLAY x11vnc -display :2 -rfbport 5902 -nopw -listen localhost -xkb -forever > /tmp/x11vnc2.log 2>&1 &
nohup websockify --web /usr/share/novnc/ 6081 localhost:5902 > /tmp/websockify2.log 2>&1 &
```

**http://localhost:6081/vnc.html?show_dot=true** で見る（既存の 6080 は 1024x600 のまま）。
アプリは解像度ごとに設定ディレクトリを分けて起動する。

```bash
nohup env DISPLAY=:2 PF_CONFIG_DIR=<スクラッチパッド>/cfg1920 PF_CACHE_DIR=<スクラッチパッド>/cache \
  SDL_VIDEODRIVER=x11 SDL_RENDER_DRIVER=opengl IS_DEV_ENVIRONMENT=true \
  IMMICH_BASE_URL="$(grep -E '^IMMICH_BASE_URL=' .env | cut -d= -f2-)" \
  IMMICH_API_KEY="$(grep -E '^IMMICH_API_KEY=' .env | cut -d= -f2-)" \
  python -u main.py > <スクラッチパッド>/app1920.log 2>&1 &
```

-   **`-rfbport` を明示する。** x11vnc は既定で 5900 から空きを探すため、
    既存の `:1`（5900）と混ざって意図しないポートになる
-   **`-clip` は付けない**（全画面を見たいため）
-   **各コマンドを `&&` で繋がない。** 繋ぐと `&` がリスト全体に掛かってサブシェルごと
    背景に回り、**プロセスが起動しないことがある**（「ssh に渡すコマンドの末尾 `&`」と
    同じ構文規則。ローカルでも踏む）
-   **`PF_CONFIG_DIR` を解像度ごとに分ける。** 同じディレクトリを2プロセスで共有すると
    `ConfigManager` の保存が競合する。本番の `config/settings.json` も汚さないこと
-   起動ログの **`ui_scale=`** で倍率を確認できる（1024x600 なら 1.00、1920x1080 なら 1.80）
-   確認後は Xvfb / x11vnc / websockify とアプリを止める。
    **元からある `:1` / 6080 の環境は止めないこと**

## セットアップ

依存はイメージに焼いてあるため、Dev Container では追加作業は要らない。
依存を足すときは `requirements.txt` を直して**イメージを再ビルド**する
（`.claude/architecture.md`「対で更新が必要な箇所」を確認すること）。

## 実行

```bash
# 階層1: Dev Container（VNC で見る）
python main.py

# 階層2: 実機ホスト直実行
SDL_VIDEODRIVER=kmsdrm SDL_RENDER_DRIVER=opengles2 python main.py

# 階層3: 実機コンテナ（最終的な運用形態）
docker compose up -d
```

## 実機（Raspberry Pi Zero 2 W / ARM64）

SSH でアクセスできる。Docker + リモート管理 UI のエージェント導入済み。

### ホスト側の前提設定

1. `config.txt` で Full KMS を有効にする
2. zram swap を有効にする（RAM 512MB の保険）
3. **フレームバッファコンソールを切り離す**（`tools/host-setup/`）

消灯・復帰のたびにコンソールの文字が一瞬見えるのを防ぐ。導入は次の3コマンド。

```bash
sudo install -m 755 tools/host-setup/pf-fbcon-off.sh /usr/local/sbin/pf-fbcon-off.sh
sudo install -m 644 tools/host-setup/pf-fbcon-off.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now pf-fbcon-off.service
```

**`vtcon*/bind` の unbind と `/dev/fb0` のゼロクリアは、どちらも再起動で元に戻る。**
そのため unit で毎回実行する。`Before=docker.service` にしてあるのは、
**SDL が DRM master を握ると `/dev/fb0` への書き込みが効かなくなる**ため。

### デプロイ

```bash
# ARM64 実機上でネイティブビルド（QEMU 不要）
docker compose build
docker compose up -d
docker compose logs -f
```

**ビルドは切り離して実行する。** Zero 2 W では apt と pip で 10 分以上かかり、
その間 CPU 飽和で SSH（Cloudflare Tunnel）が落ちる。`setsid nohup` でログへ流し、
短い ssh を間隔を空けて何度も試して回収する（下記「長い処理は切り離して実行する」）。

自動起動は `restart: unless-stopped` による。systemd ユニットは作らない。

### 要検証事項の実測コマンド

`.claude/context/known-issues.md` の各項目に対応する。**必ず実機で実行すること。**

```bash
# 9-7: Docker デーモンと他コンテナのメモリ実測
free -m
ps -o pid,rss,comm -C dockerd -C containerd
docker stats --no-stream

# 9-8: デバイスの GID 確認（group_add に渡す値）
getent group video input gpio
ls -l /dev/dri /dev/input /dev/gpiochip*

# 9-6: DRM master を誰が握っているか
sudo fuser -v /dev/dri/card*

# 9-5: アプリコンテナの実メモリ
docker stats --no-stream <container>

# 人感センサー: アプリが GPIO18 を掴んでいるか（ホスト側から見た客観的な確認）
gpioinfo | awk '/^gpiochip0/{c=1} c&&/line +18:/{print; exit}'
```

稼働中のアプリが line 18 を掴んでいるため、`gpioget` / `gpiomon` は `EBUSY` で失敗する。
単独で測るときは先に `docker compose stop` すること。ホストの libgpiod は v2.2.1 で
v1 と CLI 構文が異なる（`gpioget -b pull-down -c gpiochip0 18` のように `-c` でチップを指定する）。

## リモート管理 UI（Portainer）

実機を Portainer で運用している場合、公式 MCP サーバー
[portainer/portainer-mcp](https://github.com/portainer/portainer-mcp) が使える。

- **マイナーバージョンを Portainer 本体と一致させること**（例: MCP 2.45.x ↔ Portainer 2.45.x）
- 必要な設定: Portainer の URL、API キー（My Account → Access tokens）
- Docker API プロキシ機能があり、コンテナのログ確認や再デプロイを MCP 経由で行える
- 平文 HTTP での配備は信頼できるプライベートネットワーク内に限る（公式ドキュメントの警告）

## Windows ホストでの注意

- Bash ツール（Git Bash）と PowerShell で挙動が異なる。
- コンテナ内パスを含む docker コマンドは、Git Bash が `/src` などを Windows パスに
  書き換えて失敗することがあるため PowerShell から実行する。

## サブエージェントの使い分け (Routing)

`.claude/agents/` に5体を配置している。以下は本プロジェクト内の作業にのみ適用する。

### プラン作成時

- プラン本文には原則として実コードを書かない。方針・影響ファイル・手順・トレードオフを
  自然言語で明示すれば足りる。実際のコード生成は承認後、実装エージェントに委ねる。
  - 例外: 型・制約だけの定義、正規表現、データ構造の形、関数シグネチャなど短い断片は許容する。
- 設計・方針判断が必要な場面では、汎用の Plan エージェントではなく `advisor` を使う。

### 承認後の実装

変更の性質で使い分ける。メインスレッドが直接編集せず、委任すること。

- **`implementer`** — 形が決まっている機械的な実装。
  設定ファイル、`settings.json` のキー追加、photo-frame からのモジュール移植、
  devcontainer 設定、Dockerfile / docker-compose.yml、`config.txt`、ドキュメント更新、スコープの明確な修正。
- **`coder`** — プランが方針だけ書いてコードを意図的に書いていない新規ロジックの書き起こし。
  キャッシュキー設計、遷移エフェクトの描画ループ、自作ウィジェットの当たり判定、
  タッチイベントの解釈、スレッド間の受け渡しなど。

### 実装後

- **`evaluator`** — 差分レビューと検証の妥当性評価。自分では修正しない。
  特に「CPU 合成の混入」「テクスチャ常駐数の超過」「実行時リサイズの復活」を重点的に見る。

### 調査

- **`researcher`** — 事実収集のみ。ライブラリの現行仕様の裏取り、Immich API の実レスポンス確認、
  photo-frame の既存実装の確認、実機ログや RSS の集計、git 履歴の追跡。

## セッション記録

`.claude/skills/session-record/SKILL.md` を配置済み。
「セッション終了して」「context更新して」等で `.claude/context/` 配下を更新できる。

## 非公開オーバーレイの運用

**このリポジトリは公開リポジトリであり、これ自体が作業ツリーの本体である。**
自宅環境の情報（実機の接続情報）と開発記録（作業コンテキスト・既知の問題）は
**非公開リポジトリで管理し、`.claude/context/` へ clone して重ねる**構成になっている。

`.claude/context/` に入るもの:

```
current-sprint.md   作業コンテキスト（進捗・技術的決定・完了タスク）
known-issues.md     既知の問題・注意事項
environment.md      実機の接続情報の実値（下記「実機への接続」の変数はここで定義する）
CLAUDE.local.md      非公開側の指示を読み込むテンプレート
hooks/               秘匿情報の検査 hook（pre-commit / commit-msg）
```

### セットアップ手順

```bash
# 1. 非公開リポジトリを .claude/context/ へ clone する
git clone <非公開リポジトリのURL> .claude/context

# 2. 秘匿情報の検査 hook を有効にする
git config core.hooksPath .claude/context/hooks

# 3. 非公開側の指示を読み込むテンプレートをリポジトリルートへコピーする
cp .claude/context/CLAUDE.local.md ./CLAUDE.local.md
```

**`.claude/context/` と `CLAUDE.local.md` はどちらも `.gitignore` 対象**なので、
公開リポジトリ側のコミットには絶対に入らない。

**`.claude/context/` が無くてもアプリのビルド・実行には一切影響しない。**
無いと開発記録（このファイルの多くの節が参照している知見の蓄積）が読めなくなるだけである。

秘匿情報の検査は pre-commit / commit-msg hook が行う。hook 本体とパターン定義は
非公開側にあるため、**`core.hooksPath` の設定を忘れると検査が働かない**ことに注意する。

## 実機への接続（重要）

**接続先の実値（`PF_HOST` / `PF_HOST_TUNNEL` / `PF_REMOTE_DIR` / `PF_REMOTE_HOME`）は
`.claude/context/environment.md` で定義する。** `.claude/context/` は非公開リポジトリなので
このリポジトリには含まれない。自分の環境で使うときは、同じ変数名で自分の接続先を
定義すればよい。

### Dev Container からは LAN 直結で入る（推奨）

**`$PF_HOST` へ直接 SSH できる**（`ユーザー名@LAN内IPアドレス` の形）。
VS Code が SSH エージェントを転送しているため、1Password の鍵がそのまま使える。

```bash
ssh "$PF_HOST" '<command>'
```

**Cloudflare Tunnel（`$PF_HOST_TUNNEL`）は Dev Container からは使えない。**
名前は Cloudflare の Anycast IP に解決されるが TCP 22 は開いておらず、
トンネルを抜けるには `cloudflared` の ProxyCommand が要る（コンテナに入っていない）。

### Windows ホストから接続する場合

**Git Bash の `ssh` は使えない。** 1Password の SSH エージェント（Windows 名前付きパイプ
`\.\pipe\openssh-ssh-agent`）を掴めないため、公開鍵認証が通らない。

```bash
# Windows ネイティブの ssh を使う
/c/Windows/System32/OpenSSH/ssh.exe "$PF_HOST_TUNNEL" '<command>'
```

接続先 `$PF_HOST_TUNNEL` は Cloudflare Tunnel のホスト名に解決される。
サーバー側はパスワード認証が無効で、公開鍵のみ。

### リポジトリを実機へ転送する

実機は git で取得する構成にしていないため、tar を ssh へ流し込む。**転送元のパスを必ず明示する**
（作業ディレクトリが戻っていて先行プロジェクトのファイルを送る事故を起こした）。

```bash
tar -czf - -C /path/to/pi-photo-frame Dockerfile docker-compose.yml requirements.txt .dockerignore main.py | ssh "$PF_HOST_TUNNEL" "tar -xzf - -C $PF_REMOTE_DIR"
```

実機側では `.env.sample` → `.env`、`config/settings.sample.json` → `config/settings.json`
をコピーしてから起動する（`env_file` は必須指定のため `.env` が無いと起動しない）。

### スクリプトを渡すときは base64 で

PowerShell 経由だと日本語が文字化けし、Git Bash から直接渡すと引用符が壊れる。

```bash
B64=$(base64 -w0 script.sh)
/c/Windows/System32/OpenSSH/ssh.exe "$PF_HOST_TUNNEL" "echo $B64 | base64 -d | bash"
```

### 長い処理は切り離して実行する

トンネルは長時間の処理中に `websocket: bad handshake` で切断される。
実機側で `setsid nohup` により切り離し、ログをポーリングして回収する。

```bash
# 実行側（末尾に完了マーカーを出す）
setsid nohup bash "$PF_REMOTE_HOME/work/run.sh" > "$PF_REMOTE_HOME/work/run.log" 2>&1 < /dev/null &

# 回収側（マーカーを grep する。pgrep は自分自身にマッチするので使わない）
grep -q "ALLDONE" run.log
```

作業ディレクトリは `/tmp` ではなく `$PF_REMOTE_HOME/` 配下に置く（再起動で消えないため）。

## 実機での描画テスト

アプリの描画確認は compose で行う。

```bash
docker compose up -d
docker compose logs -f      # driver / render_driver / fps / VmRSS が出る
docker compose down
```

**fps が垂直同期（49.61Hz）付近に収まっていることが「実際に表示されている」根拠**になる。
900 超なら描画されていないことを疑う。

DRM の状態を直接見る場合は `modetest` 入りの別イメージを使う
（`tools/verification/Dockerfile.drm`）。

```bash
# コネクタとモード一覧 / CRTC の状態
docker run --rm --device /dev/dri --group-add 44 --group-add 992 <drm-image> modetest -M vc4 -c
docker run --rm --device /dev/dri --group-add 44 --group-add 992 <drm-image> modetest -M vc4 -p

# テストパターンを一定時間表示（stdin を開いたままにする）
docker run --rm -i --device /dev/dri --group-add 44 --group-add 992 <drm-image> sh -c "sleep 45 | modetest -M vc4 -s 33:1024x600"
```

## アルバムグリッドの性能ベンチ（階層3）

実機の Immich はアルバムが十数件しか無いため、数百件でのスクロール性能は
`tools/verification/album_grid_bench.py` で**アルバムを合成して**測る。
本番コードは変更せず、本物の `Renderer` と `AlbumScreen` を機械的に駆動する。

**HDMI は1枚しかないのでアプリを止める必要がある。** ランナーが
`docker compose stop` → 件数ごとに `docker run` → `docker compose start` を行い、
**trap で失敗してもアプリを必ず起動し直す**。

```bash
# 実機で（Cloudflare Tunnel の切断を避けるため切り離す）
cd "$PF_REMOTE_DIR"
setsid nohup bash album_bench_runner.sh 150 600 > album_bench.log 2>&1 < /dev/null &
grep -q RUNNER_ALLDONE album_bench.log
```

-   ベンチ本体はイメージに入っていない（`.dockerignore` が `tools` を除外している）。
    ランナーがホストの `tools/` を `/app/tools` にマウントする。**ソースを直した場合は
    先に `deploy.sh` でイメージを焼き直す**（ベンチが読むのはイメージ側の `src/`）
-   **`--cache-dir` に本番の `/cache` を渡してはならない。** `AlbumScreen` は取得した
    一覧で `cleanup_thumbnails()` を呼ぶため、合成アルバムの一覧を渡すと本番の
    サムネイルが全て消える。ランナーは `bench/cache` を使う
-   測定が終わったらログと `bench/` を消す（キャッシュが 20MB ほど残る）

**このベンチは端から端まで一方向にスクロールする。** そのため
「行が可視範囲へ再入したときのコスト」を抑える工夫（解放の余白）の効果は
ここには現れない。往復の効果を見るなら階層1で生成回数を数える方が確実
（`.claude/context/known-issues.md` 参照）。

## 表示トラブルの切り分け手順

**「ソフトが成功を返す」ことは「映っている」ことの証明にならない。** 必ず目視で確認する。

```bash
# 実際に出力されているピクセルクロック（目視に頼らない客観指標）
vcgencmd measure_clock pixel

# 電源・スロットリング（0x0 なら問題なし）
vcgencmd get_throttled

# コネクタの状態（connected は EDID が読めただけで、映っている証明ではない）
cat /sys/class/drm/card0-HDMI-A-1/status
cat /sys/class/drm/card0-HDMI-A-1/modes

# 誰が DRM master を握っているか
sudo fuser -v /dev/dri/card0
```

fps が異常に高い（900超）場合、ページフリップ待ちが発生しておらず
**表示されていない可能性**を疑う。実機の垂直同期は 49.61Hz。

## コンテナ起動オプション（描画＋タッチ）

**実証済みの指定はすべて `docker-compose.yml` に落としてある。** 単発の検証で
`docker run` を組み立てる必要はない。必要な指定と理由は同ファイルのコメントを読む。

要点だけ再掲する。

-   `--device /dev/dri` / `--device /dev/input`、`group_add` は video 44 / render 992 /
    input 996 / gpio 986（ホスト固有。`getent group video render input gpio` で確認）
-   **`SDL_RENDER_DRIVER=opengles2` が無いと描画が黙って無視される**
-   **`/run/udev:/run/udev:ro` が無いとタッチデバイスを列挙できない**
-   非 root（uid 1000）・非特権で成立する。`--privileged` は使わない

`tools/verification/` のスクリプトを単発で回す場合の `docker run` 形式は
`tools/verification/README.md` にある。

### レンダードライバを確認する

```python
import pygame._sdl2.video as v
for i, d in enumerate(v.get_drivers()):
    print(i, d)
# 実機: [0] opengl  [1] opengles2  [2] opengles  [3] software
# 既定は先頭の opengl が選ばれるが、VideoCore IV では描画が無視される
```

## ディスプレイ消灯（DRM DPMS）

`vcgencmd display_power` も `fb0/blank` も CEC も使えない（`.claude/context/known-issues.md` 参照）。
`ctypes` で `libdrm.so.2` を呼び、DPMS プロパティを操作する。

```
消灯: SDL の Renderer/Window を破棄 -> drmSetMaster() -> DPMS=3 (Off)
復帰: DPMS=0 (On) -> drmDropMaster() -> SDL を再生成
```

**DRM master は1プロセスしか保持できない。** SDL が握ったまま別 fd から
`drmSetMaster` すると `EACCES (errno=13)` で拒否される。

検証スクリプトは実機の `$PF_REMOTE_HOME/pf96/drmdpms.py` にある（3段階の対照実験付き）。
DPMS の値は 0=On / 1=Standby / 2=Suspend / 3=Off。
connector_id と DPMS プロパティ id は毎回列挙して取得すること（固定値にしない）。
