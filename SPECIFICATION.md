# pi-photo-frame 仕様書

## 1. プロジェクト概要

Raspberry Pi Zero 2 W とタッチディスプレイを使用し、セルフホスト型フォト管理ツール Immich と連携するデジタルフォトフレームを構築する。
人感センサーにより、人がいる時のみ画面を点灯させる省電力機能を搭載する。

本プロジェクトは先行プロジェクト `photo-frame`（Raspberry Pi 3 Model B / Kivy）の機能仕様を踏襲した作り直しであり、
**Zero 2 W (RAM 512MB) で動作させるための軽量化**を最大の目的とする。

### 1.1. photo-frame からの主な変更点

| 項目 | photo-frame | pi-photo-frame |
|---|---|---|
| ターゲット機 | Pi 3 Model B (1GB RAM) | **Pi Zero 2 W (512MB RAM)** |
| GUI フレームワーク | Kivy 2.3.1 | **pygame-ce** |
| ディスプレイ接続 | DSI 公式タッチディスプレイ | **mini HDMI + USB タッチ** |
| 画像リサイズ | 実行時にリサイズ | **キャッシュ時に解像度確定、実行時リサイズ廃止** |
| 実行形態 | systemd で直接起動 | **Docker コンテナ**（リモート管理 UI でリモート運用） |
| OS のビット数 | — | **ARM64 (64bit) で確定** |

---

## 2. ハードウェア構成

### 2.1. 主要機材

*   **メインボード**: Raspberry Pi Zero 2 W
    *   SoC: BCM2710A1 (Cortex-A53 quad core @ 1GHz)
    *   RAM: **512MB LPDDR2**（GPU と共有）
    *   GPU: VideoCore IV (OpenGL ES 2.0 / V3D)
    *   Wi-Fi: **2.4GHz 802.11 b/g/n のみ**（5GHz 非対応）
*   **ディスプレイ**: mini HDMI 接続ディスプレイ + USB タッチパネル
*   **センサー**: AM312 (PIR 人感センサー)
*   **その他**: microSD カード (16GB 以上推奨), 電源アダプタ

### 2.2. ディスプレイ接続に関する制約（重要）

**Raspberry Pi Zero 2 W には DSI コネクタが存在しない。**
Zero 2 W のポート構成は mini HDMI / micro USB OTG / micro USB 電源 / CSI-2 カメラ / microSD / 40pin GPIO であり、
photo-frame が使用していた Raspberry Pi 公式タッチディスプレイ（DSI 接続）はそのまま流用できない。

このため本プロジェクトでは **mini HDMI 映像出力 + USB 接続タッチパネル** を前提とする。
タッチ入力は USB HID として認識されるため、`evdev` 経由で読み取り可能である。

### 2.2.1. ピクセルクロックの制約（実測・重要）

実機の構成は Pi の mini HDMI 端子に**変換アダプタ**を介してパネルを接続している。
このアダプタが高いピクセルクロックを通せず、**36.36MHz は通るが 40MHz は通らない**。

パネル自体は PC から 1024x600@60 (51.5MHz) / @75.8 (65MHz) で正常動作するため無罪であり、
**制約はアダプタ側にある**。詳細な切り分けは第9章 9-9 を参照。

このため `/boot/firmware/cmdline.txt` に以下の指定が**必須**である。

```
video=HDMI-A-1:1024x600MR@50e
```

結果として **1024x600 @ 49.61Hz / 36.36MHz** で動作する。
アダプタを交換できれば標準の 1024x600@60 に戻せる可能性がある。

### 2.3. 配線・接続 (GPIO)

GPIO ピン配置は Pi 3 Model B と同一の 40pin であり、photo-frame の配線をそのまま流用できる。

*   **AM312 接続**（2026-09-03、実機に実配線して確認済み）:
    *   VCC -> 物理17番ピン (3V3)
    *   OUT -> 物理12番ピン (GPIO 18)
    *   GND -> 物理14番ピン
    *   VCC を 5V ではなく 3.3V にしたのは、**OUT の High が電源電圧を超えないため
        GPIO へ 5V がかかる心配が無い**から。AM312 の OUT High が VCC 追従か 3.3V 固定か
        確証が取れなかったため、確証の要らない側に倒した。
    *   AM312 のピン並び順はロットで異なる。配線前に基板のシルク印刷で確認すること。
    *   階層2（実機ホスト直実行）・階層3（実機コンテナ）の双方でエッジ検知の実動作を確認済み。

### 2.4. ネットワーク帯域の制約

Zero 2 W は 2.4GHz 帯のみの Wi-Fi であり、Pi 3 Model B（5GHz 対応）より実効帯域が劣る。
Immich からの画像取得は帯域制約を前提とし、**原寸画像を取得しない**設計とする（第6章参照）。

---

## 3. ソフトウェア構成

### 3.1. OS・環境

*   **OS**: Raspberry Pi OS Lite **64bit (ARM64 / aarch64)**
    *   実機が既に ARM64 で稼働しているため確定。当初検討していた 32bit との比較は行わない。
*   **デスクトップ環境**: なし（X11 / Wayland を導入しない）
*   **Graphics Backend**: SDL2 の **KMSDRM** ドライバ（`SDL_VIDEODRIVER=kmsdrm`）
    *   コンソールから直接 DRM/KMS 経由で描画し、ウィンドウシステムを介さない
    *   `config.txt` で Full KMS ドライバを有効にすること（`dtoverlay=vc4-kms-v3d`、実機は設定済み）
*   **レンダードライバ**: **`SDL_RENDER_DRIVER=opengles2` の指定が必須**
    *   既定の `opengl` では**テクスチャ描画が黙って無視され、画面に何も出ない**（第9章 9-10）
    *   VideoCore IV が対応するのは OpenGL ES 2.0 のみ
*   **表示モード**: **1024x600 @ 49.61Hz**（`cmdline.txt` の `video=HDMI-A-1:1024x600MR@50e` による）
    *   標準の 1024x600@60 は変換アダプタのクロック上限を超えるため使えない（第2.2.1節）
    *   垂直同期が 49.61Hz のため、アニメーションの視覚的上限は約 50fps
*   **実行形態**: **Docker コンテナ**（第3.4節参照）
    *   実機には既に Docker とリモート管理 UI のエージェントが導入済みで、リモート運用されている。

### 3.2. アプリケーションフレームワーク

**pygame-ce** を採用する。

#### 採用理由

1.  Kivy と同じ **SDL2** 上に構築されているため、KMSDRM・タッチ入力まわりの知見と実機設定を流用できる。
2.  Kivy が持つ重量物 —— Kv 言語パーサ、プロパティ/バインディングシステム、独自イベントループ、グラフィックス命令ツリー ——
    を丸ごと排除でき、常駐メモリと起動時間の双方に効く。
3.  CPython 上で動作するため、`requests` / `Pillow` および photo-frame のロジック層資産をそのまま活用できる。

#### 不採用とした候補

*   **Kivy 継続**: 移植コストは最小だが、フレームワーク本体の重量に削減の天井がある。
*   **LVGL**: ライブラリ本体は極めて軽量で、v9 系では DRM + EGL/GLES によるハードウェアアクセラレーションにも対応する。
    しかし **CPython バインディングが beta 段階**（PyPI `lvgl` 0.1.1b0）であり、成熟しているのは MicroPython バインディングの方である。
    MicroPython を採用すると `requests` / `Pillow` および既存ロジック層（約 1,300行）が使用できなくなる。
    また LVGL の footprint 優位（数百KB）は、CPython インタプリタと画像バッファが支配的な本構成では誤差の範囲に収まる。
*   **DRM/framebuffer 直描画**: 最軽量だが UI 実装コストが過大で、クロスフェードが CPU 合成となり性能面のリスクが高い。
*   **Slint**: 組み込み向けで有望だが、Python バインディングで Pi の KMS 上に載せる実績情報が乏しい。

### 3.3. 主要ライブラリ

*   `pygame-ce` — GUI / 描画
*   `requests` — Immich API 通信
*   `Pillow` — 画像デコード・リサイズ（キャッシュ生成時）
*   `gpiod` — 人感センサー（libgpiod v2 の公式バインディング）。
    `lgpio` は cp313 aarch64 の wheel が無く、Dockerfile に builder ステージが
    必要になるため採用しない。`gpiod` の wheel は libgpiod を static link しており、
    apt の追加パッケージも要らない。
*   `evdev` — タッチ入力（SDL2 で取得できない場合のフォールバック）

### 3.4. コンテナ実行構成

アプリケーションは **Raspberry Pi Zero 2 W 上の Docker コンテナ**として稼働させる。
実機には既に Docker とリモート管理 UI のエージェントが導入されており、リモート運用されている。

#### 採用の利点

*   リモート管理 UI 経由でリモートから再デプロイ・ログ確認・再起動ができる。
*   **正規のビルド経路は GitHub Actions。** `v*` タグを push すると
    `ubuntu-24.04-arm`（公開リポジトリで無料・GA の arm64 ホストランナー）が
    実機と同じ arm64 でネイティブビルドし、GHCR（`ghcr.io/seizu-dev/raspi-photo-frame`）
    へ push する。実機は `docker compose pull` で取得するだけでよく、
    **QEMU によるクロスビルドは引き続き不要**（ホストランナー自体が arm64 のため）。
    実機（Zero 2 W）上でのネイティブビルドは apt/pip で 10 分以上かかり CPU 飽和で
    SSH（Cloudflare Tunnel）が落ちる（ビルド中のハングも観測している）ため、
    `docker compose build` は GHCR に届かないときのフォールバックとして残す
    （第10.4節参照）。
*   開発 Dev Container と実行コンテナで同じベースイメージ定義を共有できる。

#### 採用に伴うコスト（設計上の前提）

*   **Docker デーモン（`dockerd` + `containerd`）自体が約 75MB の常駐メモリを消費する**（実測）。
    この分はアプリの利用可能メモリから差し引かれる。
*   **`mem_limit` は現状使用できない。** 実機は `cgroup_disable=memory` で起動しており、
    メモリ cgroup が無効である（第5.4節参照）。当初 Docker 採用の利点として想定していたが成立しない。
*   ハードウェアへ直接アクセスするため、デバイスのパススルー設定が必要になる（下記）。

#### 実証済みのコンテナ起動条件

以下の構成で、**非 root・非特権のまま描画とタッチ入力の両方が成立する**ことを実機で確認済み。

```
--device /dev/dri              # KMSDRM 描画
--device /dev/input            # USB タッチ入力
--device /dev/gpiochip0        # AM312 人感センサー（GPIO。2026-09-03 検証済み）
--group-add 44                 # video  -> /dev/dri/card0
--group-add 992                # render -> /dev/dri/renderD128
--group-add 996                # input  -> /dev/input/*
--group-add 986                # gpio   -> /dev/gpiochip0（2026-09-03 検証済み）
--user 1000:1000               # 非 root で実行
-v /run/udev:/run/udev:ro      # 必須。無いと SDL2 が入力デバイスを列挙できない
-e SDL_VIDEODRIVER=kmsdrm
-e SDL_RENDER_DRIVER=opengles2 # 必須。既定の opengl では描画が無視される
```

GID はホスト側の実測値（第9章 9-8）。`getent group video input gpio render` で確認できる。

**`--privileged` は不要。** 上記で成立することが実証されているため、特権コンテナにしない。

`/dev/gpiochip0` と `gpio` グループは、2026-09-03 に AM312 を実機へ配線して検証済み。
実機コンテナ（階層3）の起動ログで `人感センサーの監視を開始しました
（chip=/dev/gpiochip0 pin=18）` を確認し、以降の検知ログとそれに伴う消灯からの復帰も
動作した。fps は 49.2 で低下していない。ホスト側の `gpioinfo` でも
`line 18: "GPIO18" input bias=pull-down edges=rising debounce-period=50ms
consumer="pi-photo-frame"` と、コンテナがラインを掴んでいることを確認できる。

#### ベースイメージ

`python:3.13-slim` (arm64) を基点とし、SDL2 のランタイムライブラリ
（`libsdl2-2.0-0` / `libsdl2-image` / `libsdl2-ttf`）と KMSDRM に必要な `libdrm` / `libgbm`、
および日本語フォントを追加する。ビルド専用の依存はマルチステージで最終イメージに残さない。

**実装済み（2026-09-02、実機でビルド・起動を確認）。**

*   apt 依存: `libsdl2-2.0-0` / `libsdl2-image-2.0-0` / `libsdl2-ttf-2.0-0` /
    `libdrm2` / `libgbm1` / `libegl1` / `libgles2` / `fonts-noto-cjk`
*   Python 依存はすべて cp313 aarch64 wheel が存在し、**ソースビルドは発生しない**
    （`pygame-ce==2.5.8` / `Pillow==10.4.0` / `requests==2.32.5`）。
    `pip install` には **`--only-binary=:all:` を必ず付ける**。Zero 2 W 上で
    ソースビルドが始まると詰まるため、wheel が無ければその場で失敗させる。
*   このためビルド専用ステージは現時点で不要。ただし `motion_sensor.py` 実装時に
    `lgpio` を入れる際は、**cp313 aarch64 wheel が存在しない**ためビルドステージが要る。
*   日本語フォントの実ファイルは `/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc`。
*   イメージサイズ実測: content 225MB / disk usage 789MB。

#### 永続化ボリューム

*   **画像キャッシュディレクトリ** — コンテナ再作成のたびに再ダウンロードすると
    2.4GHz Wi-Fi の帯域を浪費するため、必ずボリュームとして永続化する。
*   **設定ファイル**（`settings.json` / `config.py`）— 秘匿情報を含むためイメージに焼き込まない。

---

## 4. アーキテクチャ

Python によるシングルアプリケーション構成。**ロジック層と描画層を明確に分離**し、
ロジック層は photo-frame から移植、描画層は新規実装とする。

```
main.py                     アプリケーションループ / 画面遷移
src/
  immich_api.py             Immich API クライアント（移植・改修）
  photo_cache.py            表示解像度確定済み画像のディスクキャッシュ（再設計）
  daily_pickup_manager.py   デイリーピックアップ（移植）
  config_manager.py         設定の読み書き（移植）
  motion_sensor.py          AM312 人感センサー（移植）
  display_manager.py        ディスプレイ電源制御（再実装）
  gui/
    renderer.py             Renderer/Texture のラッパ
    transitions.py          遷移効果（crossfade / fade_black / slide / wipe）の描画と random の抽選
    widgets.py              ボタン・トグル・スライダー等の自作ウィジェット
    screens/                スライドショー / メニュー / 設定 / アルバム選択
```

### 4.1. photo-frame からの移植方針

| モジュール | 扱い | 備考 |
|---|---|---|
| `immich_api.py` (276行) | 改修して流用 | 原寸取得を廃し、サイズ指定取得へ変更 |
| `cache_manager.py` / `thumbnail_cache_manager.py` | 統合して再設計 | `photo_cache.py` に一本化 |
| `daily_pickup_manager.py` (67行) | ほぼそのまま | フレームワーク非依存 |
| `config_manager.py` (61行) | ほぼそのまま | フレームワーク非依存 |
| `motion_sensor.py` (98行) | そのまま | GPIO 配置は Zero 2 W も同一 |
| `display_manager.py` (101行) | 再実装 | DSI→HDMI で消灯方式が変わる |
| `gui/` 一式 + `kv/*` (約780行) | 全面書き直し | Kivy 依存のため |
| `main.py` (689行) | 再構成 | Kivy の App / Clock 依存を剥がす |

---

## 5. 描画設計

**本プロジェクトで最も重要な設計判断であり、ここを誤ると Zero 2 W で成立しない。**

### 5.1. GPU 合成の徹底

*   描画は **`pygame._sdl2.video` の `Renderer` / `Texture`** を使用する。
*   素の `pygame.Surface.blit`（CPU ブリット）に依存してはならない。
    フルスクリーンのアルファ合成を毎フレーム CPU で行うと処理が破綻する。
*   スライド遷移は **`Texture.alpha` の変更・配置矩形のオフセット・切り出し矩形による
    等倍の部分描画・`fill_rect`** の組み合わせで表現し、合成を GPU に委ねる
    （種類は 7.3 を参照）。

### 5.2. テクスチャ常駐数の上限

*   同時に保持するテクスチャは **最大3枚**（現在の写真 / 次の写真 / UI オーバーレイ）とする。
*   設定項目 `max_slides_in_memory` で調整可能とする（photo-frame から引き継ぎ）。
*   **実機の表示解像度は 1024x600**（実測）。ARGB8888 のテクスチャ 1枚あたり
    1024 x 600 x 4 = **約 2.34MB**。3枚常駐で約 7MB。

### 5.3. オーバーレイの再描画抑制

*   時計・写真カウンタ・説明文などのテキストは**毎フレーム再描画しない**。
*   内容が変化したときのみ `Surface` に描画して `Texture` を再生成し、以降は使い回す。
*   時計は秒を表示しない場合、更新は毎分1回で足りる。

### 5.4. メモリ収支の見積り

**以下はすべて実機での実測値である**（第9章 9-5 / 9-7 参照）。

RAM は次の順に消費される。アプリが使えるのは**残り**である。

| 段階 | 実測値 |
|---|---|
| 物理搭載 | 512 MB |
| **CMA 予約**（`dtoverlay=vc4-kms-v3d` の既定） | **256 MB** |
| **OS が認識する総メモリ** | **416 MB** |
| Docker 基盤（`dockerd` 32MB + `containerd` 17MB + shim × 6 で 26MB） | 約 75 MB |
| 他の常駐コンテナ（監視・トンネル・計算処理など） | 実測時 available 157〜224 MB |
| **本アプリ（pygame-ce + SDL2 + テクスチャ2枚）の maxrss** | **75〜83 MB** |

**「RAM 512MB」という表現は誤解を招く。OS から見えるのは 416MB であり、
そこから Docker 基盤と他コンテナを引いた残りがアプリの取り分になる。**

CMA 256MB は `vc4-kms-v3d` の既定値であり、512MB 機には過大な配分である。
`dtoverlay=vc4-kms-v3d,cma-128` 等で削減できる可能性があるが、
描画に必要な連続領域を確保できなくなるリスクがあるため未検証。

### 5.5. UI の解像度スケーリング

**GUI の寸法は 1024x600 を基準解像度とし、それ以上の解像度では等倍スケールする。**
実行時の画面サイズ（`pg.display.Info()` → `Renderer.size`）は解像度に依存しないが、
`HEADER_HEIGHT` / `UI_FONT_SIZE` / `MIN_HIT_SIZE` 等の GUI 寸法定数は絶対ピクセルの
ままでは高解像度パネルで相対的に小さくなり、文字が読みにくく・タッチターゲットが
押しにくくなるため。

*   倍率は **`max(1.0, min(width / 1024, height / 600))`**（下限 1.0。
    1024x600 未満への縮小は対象外）。`Renderer.create()` で画面サイズ確定直後に
    一度だけ算出し、`Renderer.ui_scale` として `size` と同じ寿命で保持する
*   `widgets.py` / `gui/screens/` / `gui/overlay.py` のレイアウト定数は
    「基準解像度 1024x600 における**論理px**」として定義し、値そのものは変更しない。
    描画・配置の直前に `Renderer.px(value) -> int` を通して**物理px**へ変換する
    （丸めは `int()` に統一。1024x600 では倍率が 1.0 になり恒等変換になる）
*   **オーバーレイの文字サイズだけはスケール対象外**（`comment_font_size` と
    そこから派生する時計・コメント・カウンタ・日付の文字サイズ。ユーザーが
    基本設定画面のスライダーで決める値のため）。`overlay.py` の寸法
    （`GEAR_SIZE` / `GEAR_MARGIN` / `PAD` / `COUNTDOWN_HEIGHT`）はスケール対象
*   ただし下部帯（カウンタ・説明文・日付の帯）の高さ（`overlay.py` の
    `_bottom_bar_height()`）はスケール対象外に置く。中に入る文字が
    `comment_font_size` 由来でスケール対象外である以上、帯の高さも文字サイズに
    連動させたままにするのが一貫しており、`px()` を通すと帯だけが厚くなって
    文字が中央から浮く。左右の余白 `PAD` は `px()` でスケールするため、
    この帯は「左右は解像度に追従し、上下は文字サイズに追従する」非対称な
    扱いになるが、既知の不整合として受け入れている
*   `Renderer.font(size)` は物理pxを受け取る契約のまま変えない。ここで一律に
    スケールを掛けると対象外の `comment_font_size` にも掛かってしまうため

#### `mem_limit` は現状使用できない

カーネル起動パラメータに **`cgroup_disable=memory`** が設定されているため、
メモリ cgroup が無効であり、**`docker-compose.yml` の `mem_limit` は機能しない**。
`docker stats` が全コンテナで `0B / 0B` を返すのはこのためである。

有効化するには `cmdline.txt` に `cgroup_enable=memory` を追加して再起動する必要があるが、
メモリ cgroup 自体がカーネルメモリを消費するため、416MB の環境ではトレードオフになる。

---

## 6. 画像パイプライン

フレームワーク変更以上に軽量化へ寄与する部分であり、重点的に設計する。

1.  **サイズ指定取得**: Immich API から画面解像度に見合ったサイズの画像を取得し、原寸をダウンロードしない。
    転送量とデコード負荷を同時に削減する。
    **取得元は `size=preview` 一択**（`size=thumbnail` は 333x250 と小さすぎる）。
    ただし preview の解像度は一定ではなく、実測で 1920x1440 と 2560x1440 の両方を観測した。
2.  **解像度確定キャッシュ**: ダウンロード時点で画面にフィットさせた JPEG をディスクへ保存し、
    **実行時のリサイズ処理を完全に排除する**。表示方法は `photo_fit` で選択する
    3種類（`contain` / `cover` / `smart`。7.4 参照）があり、いずれも実行時リサイズを
    行わず表示解像度で確定済みの画像をキャッシュする方針は共通である。
    -   `contain`（既定）: アスペクト比を保ったまま 1024x600 に**収め**、余白は
        焼き込まない（背景の見せ方を描画層が決められるようにするため）。
        元より大きくは拡大しない。
    -   `cover`: 1024x600 を隙間なく**埋める**よう切り抜き済みの画像を
        1024x600 ちょうどで焼いて保存する。元が小さい場合は拡大する。
    -   `smart`: 写真と画面の縦横比の大小関係が一致するときだけ `cover`、
        一致しない（正方形を含む）ときは `contain` として保存する。
    **方式ごとに別のファイル名で共存させる**（`contain` は現行のファイル名のまま、
    `cover` / `smart` は別名）。これにより既存キャッシュを温存したまま新方式を
    追加でき、`photo_fit` を切り替えるとその方式ぶんの写真を再ダウンロードする
    （余った方式のファイルは `enforce_limit()` の LRU で自然に消える）。
3.  **draft デコード**: Pillow の `draft()` を併用し、JPEG を 1/2・1/4 スケールで直接デコードする。
    **`draft()` は JPEG 専用**であり、Immich の preview は WebP で返ることがある
    （実測で JPEG と WebP の両方を観測）。WebP では通常デコードにフォールバックするため、
    この最適化は常に効くわけではない。**フォーマットを JPEG と決め打ちしないこと。**
4.  **先読み**: 次の1枚のみバックグラウンドスレッドで先行取得し、2.4GHz Wi-Fi の転送遅延を隠蔽する。
    先読み枚数を増やさないことでメモリ上限を保つ。
5.  **オフライン動作**: ネットワーク切断時は直近のディスクキャッシュで動作を継続する。

---

## 7. 機能要件

photo-frame の機能仕様を踏襲する。

### 7.1. 起動・画面表示

*   OS 起動後、Docker の `restart: unless-stopped` によりコンテナが自動起動する。
    systemd ユニットは作成しない（Docker デーモンの起動に委ねる）。
*   X サーバーを介さず、KMSDRM 経由で全画面描画される。
*   起動後、即座にスライドショー画面へ遷移する。

### 7.2. Immich 連携

*   **認証**: 事前に生成した API キーを設定ファイルまたは環境変数で与える。
*   **写真ソース**: お気に入り / 指定アルバム / デイリーピックアップ から選択。
*   **表示**: 写真に説明文がある場合、画面下部にテキスト表示する。日本語フォントの同梱が必要。

### 7.3. 画面構成

| 画面 | 内容 |
|---|---|
| スライドショー | 写真表示、時計、写真カウンタ、説明文オーバーレイ、ステータス帯、カウントダウンゲージ |
| メニュー | 各画面への遷移、アプリ終了 |
| 基本設定 | 表示順、遷移効果、表示間隔、省電力設定などのトグル/スライダー |
| アルバム選択 | サムネイルグリッド + スクロール、デイリーピックアップ選択 |

※ 設定画面とアルバム選択画面のウィジェット（トグル・スライダー・グリッド）は自作が必要。

**遷移効果は `transition` で選択する**（`transition_duration` で時間を共有する）。
値は以下の5種類。

| 値 | 内容 | 進行 |
|---|---|---|
| `crossfade`（既定） | 現在の写真をフェードアウトしながら次の写真をフェードインする | 線形 |
| `fade_black` | 前半は現在の写真に全画面の黒を重ねてフェードアウトし、後半は黒から次の写真をフェードインする | 線形（黒を挟むぶん、写真が見えない時間がある） |
| `slide` | 現在の写真を左へ押し出しながら、次の写真を右から入れる | ease-in-out |
| `wipe` | 右から左へ動く境界線を境に、右側から次の写真へ切り替える | ease-in-out |
| `random` | 自動送りのたびに `crossfade` / `fade_black` / `slide` / `wipe` から抽選する（直前と同じ種類は避ける） |  |

**自動送りだけが遷移し、タッチによる手動送りは前後とも瞬間表示にする。**
逆方向は先読みしていないため必ず瞬間表示になり、順方向だけ遷移させると
挙動が非対称になるためである。逆方向も先読みして遷移させる案は
「先読みを次の1枚より増やさない」（禁止パターン）に抵触するので採らない。
遷移の種類は遷移開始時に決め、途中で変えない。不明な値は警告ログを出して
`crossfade` 扱いにする。

**いずれの遷移も、現在の写真テクスチャと次の写真テクスチャの2枚のまま実装し、
テクスチャを追加で確保したり、描画パスで拡縮したりしない。**
`Texture.alpha` の変更・配置矩形（dstrect）のオフセット・切り出し矩形（srcrect）
による等倍の部分描画・`fill_rect` の組み合わせのみで表現する。

**`crossfade` はアスペクト比が変わるとき、次の写真に覆われない領域も
同じ進行度で黒へ落とす。** 画像は余白を焼き込まずアスペクト比を保って
保存する（10.1）ため、写真ごとにテクスチャの大きさが違う。現在の写真だけを
不透明で描くと、はみ出した帯がフェード中ずっと残り、入れ替えの1フレームで
唐突に消える。実装は `fill_rect` 最大4枚で行い、テクスチャは増やさない。

メニューを開く導線は photo-frame と同じ2段階にする。
中央 60% をタップするとオーバーレイと3本線ボタンが5秒間表示され、
その間にボタンを押すとメニューへ遷移する。誤操作を防ぐためである。

### 7.4. 設定項目

photo-frame の `settings.json` を踏襲する。

`config_manager.py` の `defaults` が実体で、`settings.sample.json` はそこから生成する。

```json
{
    "language": "ja",
    "time_format": "24h",
    "date_format": "ymd_slash",
    "interval": 10,
    "transition": "crossfade",
    "transition_duration": 1.0,
    "show_comment": true,
    "show_clock": true,
    "show_countdown": true,
    "display_mode": "sequential",
    "photo_fit": "contain",
    "source": "favorites",
    "album_id": "",
    "album_name": "",
    "max_slides_in_memory": 3,
    "show_memory_usage": false,
    "cache_lifetime_hours": 24,
    "photo_cache_max_mb": 512,
    "power_saving_enabled": true,
    "power_saving_timeout": 300,
    "display_wakeup_delay": 3.0,
    "motion_sensor_enabled": true,
    "comment_font_size": 24,
    "daily_pickup_count": 3,
    "daily_pickup_date": "",
    "daily_pickup_selected_ids": [],
    "daily_pickup_remaining_ids": []
}
```

**写真の表示方法は `photo_fit` で選択する**（既定 `contain`）。値は以下の3種類。

| 値 | 内容 |
|---|---|
| `contain`（既定） | 写真全体が収まるよう縮小する。余った辺には黒帯が出る（現行の挙動） |
| `cover` | 画面を隙間なく埋めるよう切り抜く。はみ出した部分は切り落とす |
| `smart` | 写真と画面の縦横比の大小関係が一致するときだけ `cover`、それ以外は `contain` |

方式ごとにキャッシュのファイル名が変わるため、設定を変更すると
（`main.py` が現在の写真を `SlideshowScreen.reload_current()` で取り直す）
その方式ぶんの写真の再ダウンロードが発生する。詳細は6章を参照。

末尾3つ（`daily_pickup_date` / `daily_pickup_selected_ids` / `daily_pickup_remaining_ids`）は
設定ではなく**永続化された実行時状態**であり、設定画面には出さない。

**UI は日本語 / 英語を切り替えられる**（`language`、既定 `ja`）。当初の機能仕様の
範囲外で、ユーザーの要望から追加した。文言は `src/i18n.py` の辞書に埋め込み、
`t(key)` で引く。基本設定画面で切り替えると即時反映され（SDL の世代と同じ
「作り直す」機構に相乗りする）、`menu` / `settings` / `album` の3画面と
オーバーレイ（時計・撮影日・ステータス）が新しい言語で描き直される。
Immich のアルバム名・写真の説明文は元データのため翻訳しない。

**日時の書式も言語とは独立して選べる。** `time_format`（`24h`（既定）/ `12h`）は
画面右上の時計に、`date_format`（`ymd_slash`（既定）/ `mdy_slash` / `dmy_slash` / `long`）は
写真の撮影日表示に使う。いずれも既定値は現行の表示（`%H:%M` / `2024/03/12`）と
一字一句同じ結果になるよう選んである（実機の既存 `settings.json` に自動補完で
このキーが追加されても見た目が変わらないようにするため）。`12h` の日本語表記は
0〜11時をそのまま使う（`午前0:05` / `午後0:30`）。英語の12時制のように 0時・12時を
「12」に丸める慣習は日本語には無いため、この点だけ言語で規則が異なる
（`src/i18n.py` の `format_time()` 参照）。

photo-frame では `transition` と `max_slides_in_memory` がどこからも参照されない
死んだキーになっていた。本実装では `max_slides_in_memory` を実際に使い、
`transition` は本実装で5種類の遷移効果の選択キーとして使う（既定値 `crossfade`）。

---

## 8. 省電力・人感センサー制御

*   **常時監視**: センサーが動きを検知したらタイマーをリセットし、画面を点灯する。
*   **自動消灯**: 動きが検知されなくなってから `power_saving_timeout` 秒経過後、画面を消灯する。
*   **人感センサーの無効化**: `motion_sensor_enabled`（既定 true）を false にすると
    `MotionSensor.stop()` で GPIO を解放する。true に戻すと再取得する。
    2026-09-03 の実機検証では検知範囲が想定より広く、`power_saving_timeout` を
    短くしても人がいる限り消灯しないことを確認した（仕様どおりの動作）。
*   **消灯方式**: **ctypes 経由の DRM DPMS で確定**（第9章 9-1）。`display_manager.py` が実装する。
    photo-frame が使用していた `vcgencmd display_power` は DSI 前提であり、
    HDMI + Full KMS では終了コード 0 を返すだけで何も起きない。
    `fb0/blank` は SDL が DRM master を取ると無効になり、CEC はパネルが応答しない。

    **DRM master は1プロセスしか保持できない。** そのため消灯・復帰は SDL の
    ライフサイクルと不可分であり、`DisplayManager` が順序を保証する。

    ```
    消灯: on_release_display() -> drmSetMaster() -> DPMS=Off
    復帰: DPMS=On -> drmDropMaster() -> on_recreate_display()
    ```

    消灯中は fd と master を保持し続ける。省電力のタイマー判定は `DisplayManager` に
    含めず、`motion_sensor.py` との組み合わせとともに `main.py` が担う。

*   **復帰後のウォームアップ**: **DPMS=On を送ってもパネルが映るまで約 2.0〜2.4 秒かかる**
    （実機実測）。この間に省電力タイマーを再開したりスライドを送ると、見えない時間が
    消費されてしまう。`display_wakeup_delay`（既定 3.0 秒）だけ待ってから処理を再開する。

    `turn_on()` はブロックせず即座に戻る。`DisplayManager.is_ready` が
    「点灯していて、かつ実際に見える状態か」を返すので、`main.py` はこれを見て
    タイマーとスライド送りを再開する。ウォームアップ中もメインループは回り続けるため、
    タッチ入力は取りこぼさない。

    SDL の再生成は 0.4〜2.0 秒と変動が大きく、パネル応答と並行して進む。
    実際に見えるのは遅い方であり、実測ではパネル応答が支配的だった。

---

## 9. 検証結果と残課題

**実機は SSH で利用できる。** Raspberry Pi Zero 2 W (ARM64) が LAN 内で稼働しており、
Docker とリモート管理 UI のエージェントが導入済み。本章の項目は机上で推測せず、実機で測って確定させること。
実測コマンドは `.claude/workflows.md` の「要検証事項の実測コマンド」を参照。

### 9-1. ディスプレイ消灯方式 — 【解決済み】

**`ctypes` で libdrm を呼び、DRM の DPMS プロパティを操作する方式を採用する。**
バックライトごと消灯することを目視で確認済み（3回の実行で同一結果）。

#### 手順

```
消灯: SDL の Renderer/Window を破棄 → drmSetMaster() → DPMS=3 (Off)
復帰: DPMS=0 (On) → drmDropMaster() → SDL を再生成
```

**`--privileged` も `sudo` も追加のデバイスパススルーも不要。**
既存の `/dev/dri` + `video` グループ + 非 root (uid 1000) のままで成立する。

#### なぜ SDL を破棄する必要があるか

**DRM master は1プロセスしか保持できない。**
SDL が master を握っている間、別 fd からの `drmSetMaster()` は
`EACCES (errno=13)` で拒否され、DPMS の変更も通らない。

| 状態 | `drmSetMaster` | `SetProperty` | 結果 |
|---|---|---|---|
| SDL なし | `rc=0` | `rc=0` | ✅ 消灯する |
| **SDL が master 保持中** | **`rc=-1 errno=13`** | **`rc=-13`** | ❌ 変化なし |
| SDL 破棄後 | `rc=0` | `rc=0` | ✅ 消灯する |

#### 副次的な利点

消灯時に SDL を破棄するため、**テクスチャとレンダラのメモリが解放される**。
416MB という制約下では、消灯中にメモリが空くことは利点になる。
人感センサーによる消灯・復帰は数分単位の頻度であり、再生成コストは問題にならない。

#### 不採用とした方式

| 方式 | アプリ非稼働時 | アプリ稼働中 | 備考 |
|---|---|---|---|
| `vcgencmd display_power 0` | ❌ 無効 | ❌ 無効 | **photo-frame が採用していた方式。Full KMS では機能しない。**<br>終了コードは 0 を返すが値が変化せず、`dpms` も `pixel` も変わらない |
| `/sys/class/graphics/fb0/blank` | ✅ 消灯する | ❌ 無効 | fbcon 経由の制御であり、SDL が DRM master を取ると<br>fbcon が非アクティブになるため切り離される。root 権限も必要 |
| `cec-ctl --standby` | ❌ | ❌ | パネルが CEC に応答しない（`Tx, Not Acknowledged, Max Retries`）。<br>`/dev/cec0` は存在するがパネル側が非対応 |

#### 実装上の注意

`pygame-ce` は DPMS 操作の API を公開していないため、`ctypes` で `libdrm.so.2` を
直接呼ぶ必要がある。使用する関数は以下。

*   `drmModeGetResources` / `drmModeGetConnector` / `drmModeGetProperty` — DPMS プロパティ ID の取得
*   `drmModeConnectorSetProperty` — DPMS 値の設定（0=On, 1=Standby, 2=Suspend, 3=Off）
*   `drmSetMaster` / `drmDropMaster` — master の取得と解放

実機での値: connector_id=33, DPMS プロパティ id=2。
ただし**これらの ID は固定値として扱わず、毎回列挙して取得すること。**

### 9-2. KMSDRM 環境でのタッチ入力 — 【解決済み】

**コンテナ内の SDL2 から `FINGERDOWN` / `FINGERUP` として取得できる。**
`evdev` 直読みへのフォールバックは不要。

必要な設定は以下の3点。

*   `--device /dev/input` のパススルー
*   `--group-add 996`（`input` グループ）
*   **`-v /run/udev:/run/udev:ro`** — これが無いと SDL2 がデバイスを列挙できない

検出結果:

```
SDL2 touch devices = 1  (device id=6)
/dev/input/event0〜3 : すべて OPEN_OK
```

#### 座標精度（実測）

正規化座標（0.0〜1.0）で届く。`e.x * 画面幅` で変換する。

| 触った位置 | 正規化 | ピクセル換算 | 期待値 |
|---|---|---|---|
| 左上 | (0.041, 0.033) | (42, 20) | (0, 0) |
| 右上 | (0.994, 0.023) | (1018, 14) | (1024, 0) |
| 右下 | (0.978, 0.947) | (1001, 568) | (1024, 600) |
| 左下 | (0.027, 0.998) | (28, 599) | (0, 600) |
| 中央 | (0.511, 0.533) | (523, 320) | (512, 300) |

**キャリブレーション補正は不要。**

`MOUSEBUTTONDOWN` は発生しない。同じ物理デバイスが `event1` / `mouse1` として
マウス互換でも露出しているが、**二重発火はしない**。

### 9-3. OS のビット数 — 【解決済み】

**ARM64 (64bit) で確定。** 実測値は以下。

```
Debian GNU/Linux 13 (trixie) / kernel 6.12.75+rpt-rpi-v8 / aarch64
Raspberry Pi Zero 2 W Rev 1.0
```

### 9-4. クロスフェードの実効フレームレート — 【解決済み】

`SDL_RENDER_DRIVER=opengles2` を指定した状態で、**294.9 fps**
（1024x600 フルスクリーン、単色テクスチャ2枚の `Texture.alpha` によるアルファ合成、10秒間で 2949 フレーム）。

ディスプレイの垂直同期は 49.61Hz であるため、**必要性能に対して約6倍の余裕がある**。
`transition_duration` に実用上の制約はない。

#### 過去の誤測定について（再発防止のため記録）

当初 **920 fps** という値を記録したが、これは **`SDL_RENDER_DRIVER` 未指定で
テクスチャが一切描画されていない状態**のループ速度であり、無効な測定値だった（9-10 参照）。

正しく描画されるようになった結果 295 fps へ落ちた。
**異常に高い fps は「描画されていない」ことの兆候である**という判断材料になる。

#### この測定値の限界

*   単色テクスチャ2枚の合成のみを測った値であり、**写真表示では JPEG デコードと
    テクスチャ生成が別途かかる**。
*   **CPU 負荷の高い常駐コンテナを停止した状態**での測定値である。通常運用ではそのコンテナが全コアを使うため競合する。

### 9-5. 常駐メモリの実測 — 【解決済み】

pygame-ce + SDL2 + テクスチャ2枚のプロセスで **maxrss 84MB**
（`opengles2` で実際に描画している状態での測定値）。第5.4節の収支と整合する。

### 9-6. コンテナから DRM master を取得し描画できるか — 【解決済み・目視確認済み】

**`--privileged` なしで成立する。** 非 root（uid 1000）でも成功。`/dev/tty0` の追加も不要。

**ただし `SDL_RENDER_DRIVER=opengles2` の指定が必須**（9-10 参照）。
これが無いとテクスチャ描画が無視され、画面に何も出ない。

`group_add` の効果は対照試験で確認済み。

| 試験 | 実行ユーザー | group_add | 結果 |
|---|---|---|---|
| B | root | あり | OPEN_OK（root は権限を迂回するため判定に使えない） |
| C | 非 root | **なし** | **Permission denied** |
| D | 非 root | **あり** | **OPEN_OK** |

#### 目視で確認した描画（すべて成功）

*   全画面テクスチャ描画（`Texture.draw()`）
*   部分描画（`Texture.draw(dstrect=...)`）— 四隅のマーカーと動くバー
*   `Texture.alpha` によるクロスフェード

#### 検証時の反省

当初この項目を「成功」と記録したが、その時点で確認できていたのは
**`ren.clear()` による全画面塗りつぶしのみ**であり、テクスチャ描画は目視確認していなかった。
SDL2 が `RESULT: OK` を返したことを根拠にしてしまった。
**描画の成否は必ず目視で確認すること。**

### 9-10. SDL の描画が反映されない（レンダードライバの選択） — 【解決済み】

#### 症状

`ren.clear()` による全画面塗りつぶしだけが画面に反映され、
`Texture.draw()` も `Renderer.fill_rect()` も**例外を出さずに無視される**。

#### 原因

SDL2 が既定で選択するレンダードライバが `opengl` であるため。
実機で列挙されるドライバは以下の順である。

```
[0] opengl   [1] opengles2   [2] opengles   [3] software
```

**VideoCore IV / V3D が対応するのは OpenGL ES 2.0 であり、デスクトップ版 OpenGL ではない。**
それにもかかわらず `opengl` レンダラの生成自体は成功してしまい、
描画命令だけが黙って無視される状態になる。
`clear()` が効いていたのは、GL コンテキストが不完全でも通る操作だったためと考えられる。

#### 解決策（必須設定）

```
SDL_RENDER_DRIVER=opengles2
```

#### 対照実験の結果

各ドライバで全画面テクスチャを描画し、目視で判定した。

| `SDL_RENDER_DRIVER` | 結果 |
|---|---|
| `software` | ❌ 描画されない |
| **`opengles2`** | ✅ **描画される** |
| `opengl`（既定） | ❌ 描画されない |
| 従来 API（`display.set_mode` + `Surface.fill`） | ❌ 描画されない |

| 試験 | 実行ユーザー | group_add | 結果 |
|---|---|---|---|
| B | root | あり | OPEN_OK（root は権限を迂回するため判定に使えない） |
| C | 非 root | **なし** | **Permission denied** |
| D | 非 root | **あり** | **OPEN_OK** |

### 9-7. Docker デーモンの常駐メモリ — 【解決済み】

| 項目 | 実測値 |
|---|---|
| OS が認識する総メモリ | **416 MB**（512MB ではない） |
| うち CMA 予約 | **256 MB**（`dtoverlay=vc4-kms-v3d` の既定値） |
| `dockerd` | 32.2 MB |
| `containerd` | 16.9 MB |
| `containerd-shim` × 6 | 約 26 MB |
| **Docker 基盤 合計** | **約 75 MB** |
| CPU 負荷の高い常駐コンテナ稼働時の available | 約 157 MB |
| CPU 負荷の高い常駐コンテナ停止時の available | 約 200〜224 MB |

### 9-8. デバイスの GID — 【解決済み】

| グループ | GID | 対象デバイス |
|---|---|---|
| `video` | **44** | `/dev/dri/card0` |
| `render` | **992** | `/dev/dri/renderD128` |
| `input` | **996** | `/dev/input/*` |
| `gpio` | **986** | `/dev/gpiochip0` |

ログインユーザーは上記すべてと `docker`(985) に所属済み。

### 9-9. ディスプレイが 1024x600 で表示されない — 【解決済み】

**当初の仕様書に存在しなかった問題。実機検証で発覚した。**

#### 症状

OS 起動の初期段階では表示されるが、約21秒後に vc4-drm がフレームバッファを引き取ると
画面が消灯し、以降まったく表示されない。

#### 切り分けの経過

| 検証 | 結果 | 結論 |
|---|---|---|
| 720x400@70 (28.3MHz) | ✅ 表示 | — |
| 640x480@75 (31.5MHz) | ✅ 表示 | — |
| 800x600@60 (40.0MHz) | ❌ | — |
| 1024x600@60 (51.5MHz) | ❌ | パネルの EDID 推奨モードなのに失敗 |
| 832x624@74 (57.3MHz, 同期極性が負) | ❌ | **同期極性は無関係と判明** |
| `config_hdmi_boost` 5 → 11 | 効果なし | 送信側の駆動強度では解決しない |
| 電源・スロットリング (`get_throttled`) | `0x0` | **電源不足ではない**（5V 2.4A、低電圧履歴なし） |
| **PC に接続して 1024x600@60 / @75.8** | **✅ 表示** | **パネルは 65MHz まで動作可能** |

#### 原因

**Pi の mini HDMI 変換アダプタが高いピクセルクロックを通せない。**
アダプタの限界は **36.36MHz は通り、40MHz は通らない**範囲にある。
高周波ほど先に失敗するという、接続経路が限界的なときの典型的な症状。

パネル自体は PC から 65MHz で正常動作するため無罪。EDID の
「max dotclock 150 MHz」「preferred 1024x600@60」の申告は正しいが、
**経路がそれを通せない**。

#### 解決策（必須設定）

2つの制約の狭い両立点を突く必要がある。

| 制約 | 範囲 |
|---|---|
| アダプタが通せるクロック | 36.36MHz 可 / 40MHz 不可 |
| パネルが要求する垂直周波数 | 約 50Hz 以上（EDID 申告 50-76Hz） |
| 1024x600@50 の最小クロック（低ブランキング） | 36.36MHz |

`/boot/firmware/cmdline.txt` に以下を追加する（**必須**）。

```
video=HDMI-A-1:1024x600MR@50e
```

結果として得られるモードは以下。

```
1024x600  49.61Hz  htotal=1184 vtotal=619  clock=36356 kHz  type: userdef
```

#### 検証済みの動作

*   Linux コンソールが 1024x600 で表示される
*   **pygame-ce (KMSDRM) からも同モードで描画できる**
*   アプリ実行中もピクセルクロックは 36,356,000 Hz を維持し、
    SDL2 は preferred (51.5MHz) ではなく userdef モードを選択する

#### 注意事項

*   モード一覧上の `preferred` は依然 #0 (51.5MHz) である。`userdef` が選ばれることは
    実測で確認したが、SDL やライブラリの版が変わった場合は再確認すること。
*   **垂直同期が 49.61Hz** であるため、アニメーションの視覚的上限は約 50fps。
*   `config_hdmi_boost` は**既定値 5 のままでよい**。切り分けの過程で 11 に上げたが、
    除去して再起動しても 1024x600 の表示は維持されることを確認済み。効果は無かった。

#### 失敗した試み（再試行しないための記録）

*   `video=HDMI-A-1:1024x600MR@40e`（28.9MHz / 39.5Hz）
    → バックライトは点灯するが映像は出ない。**垂直 39.5Hz がパネルの下限 50Hz を下回るため。**
    クロックを下げれば良いという判断は誤りで、垂直周波数の下限も同時に満たす必要がある。

---

## 10. システム構成・デプロイ

### 10.1. 実機

Raspberry Pi Zero 2 W (ARM64) が LAN 内で稼働しており、**SSH でアクセスできる**。
Docker とリモート管理 UI のエージェントが導入済みで、リモート運用されている。

*   **Full KMS 有効化**: `config.txt` にて設定（ホスト側）。
*   **zram swap**: 512MB の保険として有効化する（ホスト側）。
*   **自動起動**: `docker-compose.yml` の `restart: unless-stopped` による。

### 10.2. 開発環境

photo-frame の Dev Container + VNC 構成（Xvfb + fluxbox + x11vnc + noVNC）を移植した。
pygame-ce も SDL2 ベースであるため、`SDL_VIDEODRIVER=x11` に切り替えるだけで
Windows 側の VNC（`http://localhost:6080/vnc.html?show_dot=true`）で UI レイアウトとロジック層を確認できる。
Kivy 由来の依存（GStreamer 一式 / `libmtdev` / wayland / `npm`）は持ち込んでいない。

*   **Dockerfile は `base` → `dev` → `runtime` の3ステージ構成。** `runtime` を最終ステージに
    置くことで、本番の `docker compose build`（`target` 未指定）は従来どおり `runtime` を生成する。
    `dev` は `docker-compose.dev.yml`（`target: dev`）からのみ使う。
*   **開発用の Immich は立てない。** 既存のセルフホスト Immich を `.env` の
    `IMMICH_BASE_URL` / `IMMICH_API_KEY` で指す。
*   **Python 依存はイメージに焼く（venv 無し）。** `base` ステージで pip install 済みのため、
    階層1と階層3で同一バージョンを保証する。
*   **解像度は実機と同じ 1024x600。** `.devcontainer/start-vnc.sh` の Xvfb / x11vnc も
    1024x600 で起動する。
*   **`SDL_RENDER_DRIVER` は階層1と階層3で異なる値になる。** `dev` は x86 / llvmpipe
    向けの `opengl`、`runtime` は VideoCore IV 向けの `opengles2`。混同して本番の値を
    書き換えないこと（`.claude/architecture.md`「対で更新が必要な箇所」参照）。
*   **ホストの `~/.claude` を任意でマウントできる。** `docker-compose.claude.yml`
    （リポジトリルート）が `.env` の `HOST_CLAUDE_DIR` 未設定時は名前付きボリューム
    `claude-config` に、設定時はそのパスを `/home/app/.claude` へ bind mount する。
*   関連ファイル: `Dockerfile`（3ステージ） / `docker-compose.dev.yml`（階層1用サービス。
    デバイスパススルー無し） / `docker-compose.claude.yml`（Claude 設定マウント） /
    `.devcontainer/devcontainer.json` / `.devcontainer/start-vnc.sh` / `.devcontainer/README.md`。
    いずれも本番用の `docker-compose.yml` には手を入れていない。

### 10.3. 検証環境の3階層

**どの階層で確認したかを常に明示すること。上位で動いても下位で動く保証はない。**

| 階層 | 環境 | 検証できること | 検証できないこと |
|---|---|---|---|
| 1. Dev Container | x86 / x11 / VNC | UI レイアウト、ロジック層、Immich 疎通 | ARM 挙動、KMSDRM、メモリ実態 |
| 2. 実機ホスト直実行 | ARM64 / kmsdrm | KMSDRM 描画、タッチ、GPIO、消灯、fps | コンテナ特有の制約（9-6 / 9-8） |
| 3. 実機コンテナ | ARM64 / Docker | **最終的な運用形態のすべて** | — |

**最終的な性能・メモリの数値は階層3で測ったものを正とする。**

### 10.4. デプロイ手順

1.  リポジトリを実機へ配置し、`.env.sample` → `.env`、
    `config/settings.sample.json` → `config/settings.json` をコピーする。
    `env_file` は必須指定のため、`.env` が無いと起動しない。
2.  イメージを用意する。**正規の経路は GHCR から pull すること**
    （`docker compose pull`）。`v*` タグを push すると `.github/workflows/release.yml`
    が `ubuntu-24.04-arm` ホストランナーで arm64 イメージをビルドし
    `ghcr.io/seizu-dev/raspi-photo-frame` へ push する。**GHCR パッケージは
    初回は非公開で作られるため、GitHub の Package settings で Public にしておく**
    （非公開のままだと実機で `docker login` が必要になる）。
    GHCR に届いていないタグや手元の修正を試すときは、実機ネイティブビルド
    （`docker compose build`。QEMU 不要）へフォールバックできる。
    **ビルド中は CPU 飽和で SSH（Cloudflare Tunnel）が落ちるため、
    `setsid nohup` で切り離してログをポーリングで回収する。**
3.  `docker compose up -d` で起動する。デバイスパススルーと `group_add` は
    `docker-compose.yml` に記述済み。**`mem_limit` は指定しない**（第5.4節。
    `cgroup_disable=memory` のため機能しない）。
4.  `docker compose logs -f` で確認する。**fps が垂直同期 49.61Hz 付近に収まっていること**が
    実際に表示されている根拠になる。`docker stats` は `0B / 0B` を返すため、
    メモリはアプリが出力する `VmRSS` を見る。
5.  リモート管理 UI 経由でログ確認・再デプロイ・再起動を行う。

自動起動は `restart: unless-stopped` による。GID（`video` 44 / `render` 992 /
`input` 996 / `gpio` 986）はホスト固有のため、別機ではまず
`getent group video render input gpio` で確認し、`.env` で上書きする。

**通常の更新**は `docker compose pull && docker compose up -d`。
**新しいバージョンをリリースするとき**は `git tag vX.Y.Z && git push origin vX.Y.Z`
で GitHub Actions を起動する。
