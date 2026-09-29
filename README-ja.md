# raspi-photo-frame

[English](README.md) | **日本語**

Raspberry Pi Zero 2 W と [Immich](https://immich.app/) で作るデジタルフォトフレームです。

Immich から写真を取得してスライドショー表示し、タッチで操作できます。
人感センサーと連動して、人がいないときは画面を消し、近づくか触れば復帰します。
デスクトップ環境を持たず、SDL2 の KMSDRM ドライバで直接描画します。

**RAM 512MB の Zero 2 W で常時稼働させることが設計上の最大の制約**であり、
画像バッファの枚数と画像パイプラインの設計はすべてこの制約から逆算しています。

## 主な機能

- Immich のアルバム / お気に入り / デイリーピックアップから写真を取得します
  （Google Drive の共有フォルダも取得元にできます。下記の番外編を参照）
- 4種類の遷移効果（クロスフェード / 黒フェード / スライド / ワイプ）とランダム選択に対応します
- 写真の収め方を3方式（内接 / 外接 / 縦横の向きが一致するときだけ外接）から選べます
- 時計・撮影日・写真カウンタ・次の送りまでのカウントダウンゲージを重ねて表示します
- タッチで写真を送れます。メニュー・設定・アルバム選択の3画面を備えています
- AM312 人感センサーとタッチで消灯・復帰します（DRM DPMS）
- UI の言語（日本語 / 英語）と、日付・時刻の書式を切り替えられます
- 表示解像度で確定済みの画像をディスクキャッシュするため、実行時のリサイズが発生しません

## 必要なもの

| | |
|---|---|
| ボード | Raspberry Pi Zero 2 W（RAM 512MB / VideoCore IV / Wi-Fi 2.4GHz のみ） |
| OS | Raspberry Pi OS Lite **ARM64 (64bit)** |
| ディスプレイ | mini HDMI 接続ディスプレイ + USB タッチパネル（1024x600） |
| センサー | AM312 PIR 人感センサー（GPIO 18、任意） |
| その他 | microSD 16GB 以上、電源アダプタ |
| サーバー | セルフホストの Immich（または Google Drive の共有フォルダ。番外編を参照） |

**Zero 2 W に DSI コネクタは存在しません。** Raspberry Pi 公式のタッチディスプレイ
（DSI 接続）は使えないため、mini HDMI + USB タッチパネルを前提としています。

## 必須設定（これが無いと画面に何も出ません）

### 1. `SDL_RENDER_DRIVER=opengles2`

VideoCore IV が対応するのは OpenGL ES 2.0 のみです。SDL2 は既定で `opengl` を選びますが、
このレンダラは**生成に成功したうえで描画命令だけを黙って無視します。**
`Renderer.clear()` による塗りつぶしだけが反映され、テクスチャが一切描かれないという
紛らわしい状態になります。

`docker-compose.yml` で指定済みです。デバッグ中に環境変数を整理して消さないでください。

### 2. `cmdline.txt` のモード指定

`/boot/firmware/cmdline.txt` に次の指定が必要です。

```
video=HDMI-A-1:1024x600MR@50e
```

開発に使った環境では mini HDMI 変換アダプタが 40MHz 以上のピクセルクロックを通せず、
標準の 1024x600@60（51.5MHz）では何も表示されませんでした。
この指定により 1024x600 @ 49.61Hz / 36.36MHz で動作します。

必要な値は使用するアダプタとパネルによって変わります。詳しい切り分けは
[SPECIFICATION.md](SPECIFICATION.md) の 9-9 を参照してください。

## セットアップ

この節は既定の経路（Immich を写真取得元にする場合）を扱います。Google Drive の
共有フォルダを使いたい場合は、まずこの節を最後まで終えてから、下記の
[番外編](#番外-google-drive-を写真の取得元にする)を参照してください。変更点は
環境変数がいくつか増えるだけです。

### ホスト側の準備

1. `/boot/firmware/config.txt` で Full KMS（`dtoverlay=vc4-kms-v3d`）を有効にします
2. 上記の `video=` 指定を `cmdline.txt` に追加します
3. zram swap を有効にします（RAM 512MB の保険）
4. フレームバッファコンソールを切り離します（消灯・復帰のたびにコンソールの文字が
   一瞬見えるのを防ぐため）

```bash
sudo install -m 755 tools/host-setup/pf-fbcon-off.sh /usr/local/sbin/pf-fbcon-off.sh
sudo install -m 644 tools/host-setup/pf-fbcon-off.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now pf-fbcon-off.service
```

### 資格情報と設定

```bash
cp .env.sample .env                              # Immich の URL と API キーを入れます
cp config/settings.sample.json config/settings.json
```

Immich の API キーには次の3つの権限があれば足ります（Immich 2.7.5 の OpenAPI スペックで確認）。

| 権限 | 使う API | 用途 |
|---|---|---|
| `album.read` | `GET /api/albums`、`GET /api/albums/{id}` | アルバム一覧・アルバムの写真・デイリーピックアップ |
| `asset.read` | `POST /api/search/metadata` | お気に入り（`source: "favorites"`） |
| `asset.view` | `GET /api/assets/{id}/thumbnail` | 写真本体（`preview`）とサムネイル |

`asset.download` などそれ以外の権限は使いません。権限が足りないと Immich は `403` と
`Missing required permission: <権限名>` を返します（`tools/verification/immich_probe.py` で切り分けられます）。

`.env` の `GID_*` はデバイスのグループ ID で、**ホストごとに異なります。**
必ず実機で確認してから設定してください。

```bash
getent group video render input gpio
```

### 起動

イメージはタグ push を契機に GitHub Actions が arm64 ネイティブビルドして GHCR へ
公開しているので、通常は pull するだけで済みます。

```bash
docker compose pull
docker compose up -d
docker compose logs -f
```

自動起動は `restart: unless-stopped` によります。systemd ユニットは作りません。

**実機でのネイティブビルドはフォールバックです**（そのタグの GHCR イメージがまだ無い場合や、
手元の変更を試したい場合など）。

```bash
docker compose build     # ARM64 実機上でネイティブビルドします（QEMU は不要です）
docker compose up -d
```

**Zero 2 W ではビルドに 10 分以上かかります。** その間 CPU が飽和するため、
SSH 越しに実行する場合は `setsid nohup` で切り離し、ログをポーリングして回収してください。

#### リポジトリを clone せず、イメージだけで動かす

リポジトリを clone したくない場合は、次の内容を `docker-compose.yml` として保存し、
公開イメージで起動できます。これはリポジトリの `docker-compose.yml` の要約で、
手作業で内容を合わせています。**食い違いがあれば、リポジトリのファイルを正としてください。**

```yaml
services:
  app:
    image: ghcr.io/seizu-dev/raspi-photo-frame:latest
    restart: unless-stopped   # 起動時に自動起動する。systemd ユニットは無い
    init: true                # ゾンビプロセスを回収し、SIGTERM がアプリへ確実に届くようにする

    user: "1000:1000"         # 非rootで動く。--privileged は不要
    group_add:                # GID はホスト固有。getent group video render input gpio で確認する
      - "${GID_VIDEO:-44}"    # video  -> /dev/dri/card0（描画）
      - "${GID_RENDER:-992}"  # render -> /dev/dri/renderD128（描画）
      - "${GID_INPUT:-996}"   # input  -> /dev/input/*（タッチ）
      - "${GID_GPIO:-986}"    # gpio   -> /dev/gpiochip0（人感センサー）

    devices:
      - /dev/dri:/dev/dri              # KMSDRM 描画
      - /dev/input:/dev/input          # USB タッチ入力
      - /dev/gpiochip0:/dev/gpiochip0  # 人感センサー用。無ければこの行を外してよい

    volumes:
      - /run/udev:/run/udev:ro         # 無いと SDL2 が入力デバイスを列挙できない
      - ./config:/config               # settings.json の置き場所（bind mount。イメージには焼かない）
      - photo-cache:/cache             # named volume。再作成のたびに再ダウンロードしないため
      - /etc/localtime:/etc/localtime:ro  # tzdata を別途入れずにローカル時刻にする

    environment:
      SDL_VIDEODRIVER: kmsdrm
      SDL_RENDER_DRIVER: opengles2     # 必須。既定の opengl では描画命令が黙って無視される
      PF_CONFIG_DIR: /config
      PF_CACHE_DIR: /cache

    env_file:
      - .env                           # IMMICH_BASE_URL / IMMICH_API_KEY 等（.env.sample 参照）

    logging:
      driver: json-file
      options: { max-size: "10m", max-file: "3" }   # SD カードでログが無制限に増えないようにする

volumes:
  photo-cache:
```

このファイルと同じ場所に `.env`（`.env.sample` から作成）と `config/settings.json`
（`config/settings.sample.json` から作成）も必要です。この2つはリポジトリから
取得するか、上記「資格情報と設定」の内容に沿って自分で用意してください。

## 操作

| 操作 | 動作 |
|---|---|
| 画面の左 20% をタップ | 前の写真 |
| 画面の右 20% をタップ | 次の写真 |
| 画面の中央をタップ | 時計・説明文・歯車ボタンを一時表示 |
| 歯車ボタン（左上）をタップ | メニューを開く |

メニューからは基本設定とアルバム選択に進めます。設定はその場で反映されます。

## 設定項目

`config/settings.json` で管理します。ほとんどの項目は基本設定画面から変更できます。

| キー | 既定値 | 内容 |
|---|---|---|
| `language` | `ja` | UI の言語（`ja` / `en`） |
| `time_format` | `24h` | 時計の書式（`24h` / `12h`） |
| `date_format` | `ymd_slash` | 撮影日の書式（`ymd_slash` / `mdy_slash` / `dmy_slash` / `long`） |
| `interval` | `10` | 写真の表示間隔（秒） |
| `transition` | `crossfade` | 遷移効果（`crossfade` / `fade_black` / `slide` / `wipe` / `random`） |
| `transition_duration` | `1.0` | 遷移にかける時間（秒） |
| `display_mode` | `sequential` | 表示順（`sequential` / `random`） |
| `photo_fit` | `contain` | 写真の収め方（`contain` / `cover` / `smart`） |
| `source` | `favorites` | 写真ソース（`favorites` / `album` / `daily_pickup`） |
| `album_id` | `''` | `source` が `album` のときのアルバム ID |
| `show_clock` | `true` | 時計の表示 |
| `show_comment` | `true` | 撮影日と説明文の表示 |
| `show_countdown` | `true` | 次の送りまでのカウントダウンゲージ |
| `comment_font_size` | `24` | 説明文の文字サイズ（px） |
| `power_saving_enabled` | `true` | 省電力（無操作で消灯します） |
| `power_saving_timeout` | `300` | 消灯までの無操作時間（秒） |
| `display_wakeup_delay` | `3.0` | 復帰後に操作を受け付けるまでの待ち時間（秒） |
| `motion_sensor_enabled` | `true` | 人感センサーの有効・無効 |
| `power_schedule_enabled` | `false` | 時間帯を指定して常時点灯にする（サイネージ運用向け） |
| `power_schedule_start` | `540` | 常時点灯の開始（0時からの分。`540` = 09:00） |
| `power_schedule_end` | `1080` | 常時点灯の終了（0時からの分。`1080` = 18:00） |
| `power_schedule_off_hours` | `normal` | 時間帯外の動作（`normal` / `force_off`） |
| `photo_cache_max_mb` | `512` | 画像キャッシュの上限（`0` で無制限） |
| `cache_lifetime_hours` | `24` | 写真リストのキャッシュ有効期間（時間） |
| `daily_pickup_count` | `3` | デイリーピックアップで1日に選ぶアルバム数 |

`display_wakeup_delay` の既定値 3.0 秒は、パネルが実際に映るまでの実測（約 2.0〜2.4 秒）に
余裕を持たせた値です。**DPMS=On の送出が完了しても、パネルはすぐには映りません。**

### 写真の収め方（`photo_fit`）

| 値 | 動作 |
|---|---|
| `contain` | 画面に内接させます。画面と縦横比が違う写真には黒帯が出ます |
| `cover` | 画面に外接させます。はみ出す部分は切り取られます |
| `smart` | 写真と画面の「縦横の向き」が一致するときだけ `cover`、それ以外は `contain` にします |

方式ごとに別のキャッシュファイルを持つため、**切り替えた直後はその方式ぶんの
初回一巡でフレームレートが落ちます。** 一度キャッシュが埋まれば垂直同期に張り付きます。

## 開発

VS Code Dev Containers を使います。x86 上で動き、GUI は VNC 経由で見られます。

```
http://localhost:6080/vnc.html?show_dot=true
```

アプリがマウスカーソルを非表示にする（実機のタッチパネルでカーソルを出さないため）ので、
`show_dot` を付けないと noVNC 上でカーソルが見えません。

解像度は実機と同じ 1024x600 です。Python 依存はイメージに焼いてあり、
pygame-ce / SDL / Python のバージョンは実機と一致します。

**検証は3階層あり、どの階層で確認したかを常に区別する必要があります。**

| 階層 | 環境 | SDL ドライバ | 検証できること |
|---|---|---|---|
| 1 | Dev Container（x86 / VNC） | `x11` | UI レイアウト、ロジック層、Immich 疎通 |
| 2 | 実機ホスト直実行（ARM64） | `kmsdrm` | KMSDRM 描画、タッチ、GPIO、消灯、fps |
| 3 | 実機コンテナ（ARM64 / Docker） | `kmsdrm` | 最終的な運用形態のすべて |

**性能とメモリの数値は階層3で測ったものを正とします。** アーキテクチャ・SDL ドライバ・
GPU・RAM がすべて異なるため、階層1で動いたことを根拠に実機の挙動を主張することはできません。

## ドキュメント

- [SPECIFICATION.md](SPECIFICATION.md) — 完全な仕様と、実機での検証結果
- [.claude/architecture.md](.claude/architecture.md) — 確定した技術的決定事項と禁止パターン、
  「対で更新が必要な箇所」の一覧
- [.claude/coding-style.md](.claude/coding-style.md) — コーディング規約
- [.claude/workflows.md](.claude/workflows.md) — 実機の運用手順（SSH・デプロイ・実測コマンド）と
  検証環境の3階層
- [tools/verification/README.md](tools/verification/README.md) — 検証用スクリプト

`.claude/workflows.md` はこのリポジトリに含まれていますが、実際に使う接続先は
`$PF_HOST` / `$PF_HOST_TUNNEL` / `$PF_REMOTE_DIR` / `$PF_REMOTE_HOME` という
プレースホルダ変数になっています。自分の環境で使うときは、同じ変数名で自分の
接続先を定義すればよいです。

非公開なのは開発記録と接続先の実値だけです。具体的には `.claude/context/` 配下の
`current-sprint.md`（作業コンテキスト）/ `known-issues.md`（既知の問題・実測値）/
`environment.md`（接続先の実値）。これらは自宅環境の情報を含むため別リポジトリで
管理し、`.claude/context/` へ clone して重ねる構成になっています。
コードのコメントやドキュメントに `.claude/context/known-issues.md` への参照が
残っているのは出典を示すもので、clone していない環境にはそのファイルは
存在しません。**`.claude/context/` が無くてもビルド・実行には一切影響しません。**

## 技術的な要点

- **GUI は pygame-ce です。** `pygame._sdl2.video` の `Renderer` / `Texture` を使い、
  遷移の合成を GPU に委ねています。フルスクリーンのアルファ合成を CPU 側
  （`Surface.blit`）で行うと Zero 2 W では成立しません
- **常駐テクスチャは3枚までです**（現在の写真 / 次の写真 / UI オーバーレイ）。
  先読みも次の1枚を超えません
- **画像は表示解像度で確定済みの状態でディスクキャッシュします。** 描画パスでは
  リサイズを行いません
- **消灯は ctypes 経由の DRM DPMS で行います。** `vcgencmd display_power` は Full KMS では
  無効で、`/sys/class/graphics/fb0/blank` は SDL が DRM master を握ると効かなくなります。
  DRM master は1プロセスしか持てないため、消灯時は SDL を破棄して master を解放します
- **消灯中は SDL が無いため pygame のイベントを取得できません。** タッチによる復帰は
  `/dev/input` の直読みで行っています

## 番外: Google Drive を写真の取得元にする

Immich の代わりに、Google Drive の共有フォルダから写真を表示することもできます。
個人の Google アカウントではなくサービスアカウント（SA）で認証し、ルートフォルダ
直下のサブフォルダをアルバムとして読み込みます。

### 設定手順

1. Google Cloud のコンソールでプロジェクトを用意し、**Google Drive API** を有効化します。
2. **サービスアカウント**を作成し、JSON 鍵をダウンロードします。
3. 鍵ファイルをコンテナから読める場所に置きます（例えば `config/` 直下。
   `settings.json` と同じ bind mount 先です）。パーミッションは絞り（`chmod 600`）、
   uid 1000 から読めることを確認してください。**このファイルはコミットしないでください**
   （`.gitignore` は `config/` 内の JSON ファイルをサンプル以外すべて除外済みです）。
4. 表示したいフォルダを、サービスアカウントのメールアドレス（JSON 鍵の
   `client_email`）へ「閲覧者」権限で共有します。
5. そのフォルダの ID（Drive の URL の末尾）を控えます。

上記の docker-compose の例は既に `./config:/config` を bind mount しているので、
ボリューム設定を変えなくても鍵ファイルをコンテナから読めます。

### `.env`

```bash
PF_PHOTO_PROVIDER=gdrive
GDRIVE_ROOT_FOLDER_ID=<手順5で控えたフォルダ ID>
# 鍵が既定のパス（config/gdrive-service-account.json）に無い場合のみ:
# GDRIVE_SA_KEY_FILE=/config/gdrive-service-account.json
```

### フォルダ構成の規則

- ルート直下の各サブフォルダを1つのアルバムとして扱います。
- ルート直下に直接置かれた写真は、「未分類」という1つの仮想アルバムにまとめられます。
- フォルダの直下だけを見ます。孫フォルダやショートカットは対象外です。

### 制約

- 対応形式は JPEG / PNG / WebP / HEIC / HEIF です。
- Google Drive には「お気に入り」に相当する取得元はありません。アルバム選択と
  デイリーピックアップのみが使えます。
- 表紙はフォルダ内の写真を名前順に並べた先頭の画像です。

HEIC/HEIF の扱い（とその理由）の詳細は [SPECIFICATION.md](SPECIFICATION.md) の
9-11 節を参照してください。

## ライセンス

MIT License. [LICENSE](LICENSE) を参照してください。
