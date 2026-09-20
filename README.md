# pi-photo-frame

Raspberry Pi Zero 2 W と [Immich](https://immich.app/) で作るデジタルフォトフレーム。

Immich から写真を取得してスライドショー表示し、タッチで操作できる。
人感センサーと連動して、人がいないときは画面を消し、近づくか触れば復帰する。
デスクトップ環境を持たず、SDL2 の KMSDRM ドライバで直接描画する。

**RAM 512MB の Zero 2 W で常時稼働させることが設計上の最大の制約**であり、
画像バッファの枚数と画像パイプラインの設計はすべてこの制約から逆算されている。

## 主な機能

- Immich のアルバム / お気に入り / デイリーピックアップから写真を取得
- 4種類の遷移効果（クロスフェード / 黒フェード / スライド / ワイプ）とランダム選択
- 写真の収め方を3方式から選択（内接 / 外接 / 縦横の向きが一致するときだけ外接）
- 時計・撮影日・写真カウンタ・次の送りまでのカウントダウンゲージのオーバーレイ
- タッチ操作による写真送り、メニュー・設定・アルバム選択の3画面
- AM312 人感センサーとタッチによる消灯・復帰（DRM DPMS）
- 日本語 / 英語の UI 切り替えと、日付・時刻の書式選択
- 表示解像度で確定済みの画像をディスクキャッシュし、実行時のリサイズを行わない

## 必要なもの

| | |
|---|---|
| ボード | Raspberry Pi Zero 2 W（RAM 512MB / VideoCore IV / Wi-Fi 2.4GHz のみ） |
| OS | Raspberry Pi OS Lite **ARM64 (64bit)** |
| ディスプレイ | mini HDMI 接続ディスプレイ + USB タッチパネル（1024x600） |
| センサー | AM312 PIR 人感センサー（GPIO 18、任意） |
| その他 | microSD 16GB 以上、電源アダプタ |
| サーバー | セルフホストの Immich |

**Zero 2 W に DSI コネクタは存在しない。** Raspberry Pi 公式のタッチディスプレイ（DSI 接続）は
使えないため、mini HDMI + USB タッチパネルを前提としている。

## 必須設定（これが無いと画面に何も出ない）

### 1. `SDL_RENDER_DRIVER=opengles2`

VideoCore IV が対応するのは OpenGL ES 2.0 のみ。SDL2 は既定で `opengl` を選ぶが、
このレンダラは**生成に成功したうえで描画命令だけを黙って無視する**。
`Renderer.clear()` による塗りつぶしだけが反映され、テクスチャが一切描かれない紛らわしい状態になる。

`docker-compose.yml` で指定済み。デバッグ中に環境変数を整理して消さないこと。

### 2. `cmdline.txt` のモード指定

`/boot/firmware/cmdline.txt` に次の指定が必要。

```
video=HDMI-A-1:1024x600MR@50e
```

mini HDMI 変換アダプタが 40MHz 以上のピクセルクロックを通せないため、
標準の 1024x600@60（51.5MHz）では何も表示されない。
この指定により 1024x600 @ 49.61Hz / 36.36MHz で動作する。

使用するアダプタとパネルによって必要な値は変わる。詳細な切り分けは
[SPECIFICATION.md](SPECIFICATION.md) の 9-9 を参照。

## セットアップ

### ホスト側の準備

1. `/boot/firmware/config.txt` で Full KMS（`dtoverlay=vc4-kms-v3d`）を有効にする
2. 上記の `video=` 指定を `cmdline.txt` に追加する
3. zram swap を有効にする（RAM 512MB の保険）
4. フレームバッファコンソールを切り離す（消灯・復帰のたびにコンソールの文字が
   一瞬見えるのを防ぐ）

```bash
sudo install -m 755 tools/host-setup/pf-fbcon-off.sh /usr/local/sbin/pf-fbcon-off.sh
sudo install -m 644 tools/host-setup/pf-fbcon-off.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now pf-fbcon-off.service
```

### 資格情報と設定

```bash
cp .env.sample .env                              # Immich の URL と API キーを入れる
cp config/settings.sample.json config/settings.json
```

`.env` の `GID_*` はデバイスのグループ ID で、**ホストごとに異なる**。
必ず実機で確認してから設定する。

```bash
getent group video render input gpio
```

### 起動

```bash
docker compose build     # ARM64 実機上でネイティブビルドする（QEMU は不要）
docker compose up -d
docker compose logs -f
```

自動起動は `restart: unless-stopped` による。systemd ユニットは作らない。

**Zero 2 W ではビルドに 10 分以上かかる。** その間 CPU が飽和するため、
SSH 越しに実行する場合は `setsid nohup` で切り離し、ログをポーリングして回収する。

## 操作

| 操作 | 動作 |
|---|---|
| 画面の左 20% をタップ | 前の写真 |
| 画面の右 20% をタップ | 次の写真 |
| 画面の中央をタップ | 時計・説明文・歯車ボタンを一時表示 |
| 歯車ボタン（左上）をタップ | メニューを開く |

メニューからは基本設定とアルバム選択に進める。設定はその場で反映される。

## 設定項目

`config/settings.json` で管理する。ほとんどの項目は基本設定画面から変更できる。

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
| `power_saving_enabled` | `true` | 省電力（無操作で消灯する） |
| `power_saving_timeout` | `300` | 消灯までの無操作時間（秒） |
| `display_wakeup_delay` | `3.0` | 復帰後に操作を受け付けるまでの待ち時間（秒） |
| `motion_sensor_enabled` | `true` | 人感センサーの有効・無効 |
| `photo_cache_max_mb` | `512` | 画像キャッシュの上限（`0` で無制限） |
| `cache_lifetime_hours` | `24` | 写真リストのキャッシュ有効期間（時間） |
| `daily_pickup_count` | `3` | デイリーピックアップで1日に選ぶアルバム数 |

`display_wakeup_delay` の既定値 3.0 秒は、パネルが実際に映るまでの実測（約 2.0〜2.4 秒）に
余裕を持たせた値。DPMS=On の送出が完了しても、パネルはすぐには映らない。

### 写真の収め方（`photo_fit`）

| 値 | 動作 |
|---|---|
| `contain` | 画面に内接させる。画面と縦横比が違う写真には黒帯が出る |
| `cover` | 画面に外接させる。はみ出す部分は切り取られる |
| `smart` | 写真と画面の「縦横の向き」が一致するときだけ `cover`、それ以外は `contain` |

方式ごとに別のキャッシュファイルを持つため、**切り替えた直後はその方式ぶんの
初回一巡でフレームレートが落ちる**。一度キャッシュが埋まれば垂直同期に張り付く。

## 開発

VS Code Dev Containers を使う。x86 上で動き、GUI は VNC 経由で見る。

```
http://localhost:6080/vnc.html?show_dot=true
```

アプリがマウスカーソルを非表示にする（実機のタッチパネルでカーソルを出さないため）ので、
`show_dot` を付けないと noVNC 上でカーソルが見えない。

解像度は実機と同じ 1024x600。Python 依存はイメージに焼いてあり、
pygame-ce / SDL / Python のバージョンは実機と一致する。

**検証は3階層あり、どの階層で確認したかを常に区別する必要がある。**

| 階層 | 環境 | SDL ドライバ | 検証できること |
|---|---|---|---|
| 1 | Dev Container（x86 / VNC） | `x11` | UI レイアウト、ロジック層、Immich 疎通 |
| 2 | 実機ホスト直実行（ARM64） | `kmsdrm` | KMSDRM 描画、タッチ、GPIO、消灯、fps |
| 3 | 実機コンテナ（ARM64 / Docker） | `kmsdrm` | 最終的な運用形態のすべて |

**性能とメモリの数値は階層3で測ったものを正とする。** アーキテクチャ・SDL ドライバ・
GPU・RAM がすべて異なるため、階層1で動いたことを根拠に実機の挙動を主張できない。

## ドキュメント

- [SPECIFICATION.md](SPECIFICATION.md) — 完全な仕様と、実機での検証結果
- [.claude/architecture.md](.claude/architecture.md) — 確定した技術的決定事項と禁止パターン、
  「対で更新が必要な箇所」の一覧
- [.claude/coding-style.md](.claude/coding-style.md) — コーディング規約
- [tools/verification/README.md](tools/verification/README.md) — 検証用スクリプト

実機の運用手順（SSH・デプロイ・実測コマンド）と開発記録（作業コンテキスト・既知の問題）は、
自宅環境の情報を含むため非公開リポジトリで管理している。
コードのコメントやドキュメントに `.claude/workflows.md` /
`.claude/context/known-issues.md` への参照が残っているのは出典を示すもので、
このリポジトリには含まれない。

## 技術的な要点

- **GUI は pygame-ce。** `pygame._sdl2.video` の `Renderer` / `Texture` を使い、
  遷移の合成は GPU に委ねる。フルスクリーンのアルファ合成を CPU 側（`Surface.blit`）で
  行うと Zero 2 W では成立しない
- **常駐テクスチャは3枚まで**（現在の写真 / 次の写真 / UI オーバーレイ）。
  先読みは次の1枚を超えない
- **画像は表示解像度で確定済みの状態でディスクキャッシュする。** 描画パスでリサイズしない
- **消灯は ctypes 経由の DRM DPMS。** `vcgencmd display_power` は Full KMS では無効、
  `/sys/class/graphics/fb0/blank` は SDL が DRM master を握ると無効になる。
  DRM master は1プロセスしか持てないため、消灯時は SDL を破棄して master を解放する
- **消灯中は SDL が無く pygame のイベントを取得できない。** タッチによる復帰は
  `/dev/input` の直読みで行う

## ライセンス

MIT License. [LICENSE](LICENSE) を参照。
