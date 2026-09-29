# アーキテクチャ方針

完全な仕様は `SPECIFICATION.md` を参照する。このファイルには**エージェントが常に守るべき決定事項と禁止パターン**だけを置く。

## 全体構成

Raspberry Pi Zero 2 W (ARM64) 上の **Docker コンテナ**で動作する Python シングルアプリケーション。
Immich から取得した写真をスライドショー表示し、人感センサーで省電力制御を行う。
デスクトップ環境を持たず、SDL2 の KMSDRM ドライバで直接描画する。

**実機は SSH でアクセスできる。** Docker とリモート管理 UI のエージェントが導入済みで、
リモート運用されている。仕様上の未確定事項は机上で推測せず、実機で測って確定させること。

**ロジック層と描画層を明確に分離する。** ロジック層は先行プロジェクト photo-frame
（非公開）から移植し、描画層は新規実装する。

## 最大の制約

**RAM 512MB。** 設計・実装のあらゆる判断でこれを最優先に考慮する。
削減効果はフレームワーク選択よりも「画像バッファの枚数」と「画像パイプライン」の方が大きい。

**OS が認識するのは 416MB**（CMA に 256MB 予約されるため）。そこから
Docker 基盤 約75MB と他コンテナを引いた残りがアプリの取り分。
アプリ本体の実測は maxrss 75〜83MB。`mem_limit` による強制はできない。

## 確定した技術的決定事項

1.  **GUI フレームワークは pygame-ce。**
    Kivy / LVGL / DRM直描画 / Slint を比較して決定した。
    -   Kivy が持つ Kv パーサ、プロパティ/バインディングシステム、独自イベントループ、
        グラフィックス命令ツリーを排除するのが狙い。
    -   LVGL は本体こそ軽量で v9 系は DRM+EGL のハードウェアアクセラレーションにも対応するが、
        **CPython バインディングが beta**（PyPI `lvgl` 0.1.1b0）。MicroPython 採用は
        `requests` / `Pillow` / 既存ロジック層の放棄を意味するため見送った。
    -   CPython インタプリタと画像バッファが支配的なため、LVGL の footprint 優位
        （数百KB）は誤差の範囲に収まると判断した。
2.  **描画は `pygame._sdl2.video` の `Renderer` / `Texture` を使う。**
    クロスフェードは `Texture.alpha` の変更で表現し、合成を GPU に委ねる。
    **`SDL_RENDER_DRIVER=opengles2` の指定が必須。**既定の `opengl` では
    テクスチャ描画が例外も出さずに無視され、画面に何も出ない（VideoCore IV は GLES2 のみ対応）。
    実測 294.9 fps（1024x600 フルスクリーンのアルファ合成）。
3.  **ディスプレイは mini HDMI + USB タッチ。1024x600 @ 49.61Hz。**
    Zero 2 W に DSI コネクタは存在しない。タッチは USB HID として `evdev` で読める。
    **`cmdline.txt` の `video=HDMI-A-1:1024x600MR@50e` が必須**（変換アダプタのクロック上限のため。
    消すと画面が映らない）。テクスチャ1枚は 1024 x 600 x 4 = 約 2.34MB。
4.  **画像は「表示解像度で確定済み」の状態でディスクキャッシュする。**
    実行時リサイズを行わない。
5.  **ロジック層は photo-frame から移植し、GUI 層のみ新規実装する。**
7.  **ディスプレイ消灯は ctypes 経由の DRM DPMS で行う。**
    -   `vcgencmd display_power` は Full KMS では無効（photo-frame の方式は使えない）
    -   `/sys/class/graphics/fb0/blank` は SDL が DRM master を握ると無効になる
    -   CEC はパネルが非対応
    -   **DRM master は1プロセスのみ。** 消灯時は SDL を破棄して master を解放し、
        `drmSetMaster` -> `DPMS=3` を行う。復帰は `DPMS=0` -> `drmDropMaster` -> SDL 再生成
    -   `--privileged` / `sudo` / 追加デバイスはいずれも不要
6.  **Docker コンテナとして実機上で稼働させる。**
    -   OS は Raspberry Pi OS Lite **ARM64 (64bit) で確定**。32bit との比較は行わない。
    -   自動起動は `restart: unless-stopped` による。systemd ユニットは作らない。
    -   **`mem_limit` は使用できない**（実機は `cgroup_disable=memory` で起動）。
    -   **正規のビルド経路は GitHub Actions。** `v*` タグを push すると
        `ubuntu-24.04-arm`（公開リポジトリで無料・GA の arm64 ホストランナー）が
        実機と同じ arm64 でネイティブビルドし、GHCR（`ghcr.io/seizu-dev/raspi-photo-frame`）
        へ push する。実機は `docker compose pull` で取得するだけでよい。
        **QEMU クロスビルドは引き続き不要**（ホストランナー自体が arm64 のため）。
        Zero 2 W 上でのネイティブビルドは apt/pip で 10 分以上かかり CPU 飽和で
        SSH（Cloudflare Tunnel）が落ちる（2026-09-19 にはビルド中のハングも発生した）ため、
        実機ビルド（`docker compose build`）は GHCR に届かないときのフォールバックとして残す。
    -   デバイスは `/dev/dri`（描画）、`/dev/input/*`（タッチ）、`/dev/gpiochip*`（センサー）を
        パススルーし、`group_add` で `video` / `input` / `gpio` の GID を与える。

## 禁止パターン

以下は方針であり、実装の都合で崩してはならない。破ると Zero 2 W で成立しない。

-   **フルスクリーンのアルファ合成を CPU 側（`pygame.Surface.blit`）で行わない。**
    1024x600 の毎フレーム合成が CPU に来ると破綻する。
-   **テクスチャを 3枚を超えて常駐させない**（現在の写真 / 次の写真 / UI オーバーレイ）。
    設定値 `max_slides_in_memory` を無視して増やさない。解放漏れにも注意する。
-   **描画パスで画像をリサイズしない。** リサイズはキャッシュ生成時に済ませる。
-   **オーバーレイ（時計・写真カウンタ・説明文）を毎フレーム再描画しない。**
    内容が変化したときのみ Surface に描いて Texture を再生成し、以降は使い回す。
-   **先読みを次の1枚より増やさない。** メモリ上限を保つため。
-   **写真リスト全体を展開してメモリに持たない。**
-   **photo-frame のコードを無検証でコピーしない。** Pi 3 Model B + DSI + Kivy 前提の
    記述が含まれる。`vcgencmd display_power` のような DSI/レガシー前提の手法は
    Zero 2 W + Full KMS でそのまま通用するとは限らない。
-   **`--privileged` に安易に倒さない。** 必要最小限のデバイスと capability で成立させる。
    どうしても不可能な場合のみ、理由を記録した上で緩和する。
-   **画像キャッシュを永続ボリュームに置かずコンテナ内に置かない。** 再作成のたびに
    再ダウンロードすると 2.4GHz Wi-Fi の帯域を浪費する。
-   **秘匿情報（`config.py` / `settings.json`）をイメージに焼き込まない。** ボリュームで渡す。
-   **Google Drive の HEIC/HEIF の原本をデコードしない。** サーバー側の縮小
    （`thumbnailLink`）が使えない場合はスキップし、JPEG/PNG/WebP に限って
    原本へフォールバックする。
-   **画素上限なしで原本をデコードしない。** `PhotoCache`（`originals=True`）は
    `MAX_ORIGINAL_PIXELS` を超える画像をデコード前にスキップする。
-   **EXIF 補正（`exif_transpose`）を Immich 経路に掛けない。** `delivers_originals`
    が True の取得元（Drive）だけに適用する。
-   **署名付き URL（Drive の `thumbnailLink` 等）とアクセストークンをログに出さない。**

## ディレクトリ構成

```
.github/workflows/release.yml  タグ push で arm64 イメージをビルドし GHCR へ push・Release 作成
Dockerfile                  実行イメージ（arm64 / python:3.13-slim ベース）
docker-compose.yml          デバイスパススルー・非root・ボリューム定義
main.py                     アプリケーションループ / 画面遷移
src/
  photo_provider.py         写真取得元（provider）の抽象化層（新規。Protocol / 工場関数 /
                            起動時の設定整合。PR1/PR2: .claude/plans/abundant-weaving-kernighan.md）
  immich_api.py             Immich API クライアント（photo-frame から移植・改修。
                            PhotoProvider Protocol を満たす）
  gdrive_api.py             Google Drive の共有フォルダを写真取得元にする
                            PhotoProvider 実装（新規。PR2）。サーバー側の縮小
                            （thumbnailLink）を優先し、原本は例外経路として扱う
  photo_cache.py            表示解像度確定済み画像のディスクキャッシュ（再設計。
                            取得元ごとに namespace でサブディレクトリを分ける）
  photo_source.py           provider とキャッシュを繋ぐ層（新規。フォールバックと表示順。
                            favorites/album/daily_pickup の振り分けもここで持つ）
  daily_pickup_manager.py   デイリーピックアップ（移植）
  config_manager.py         設定の読み書き（移植）
  motion_sensor.py          AM312 人感センサー（gpiod で再実装）
  touch_watcher.py          /dev/input の直読み（新規。消灯中の復帰に必須）
  display_manager.py        ディスプレイ電源制御（再実装）
  power_schedule.py         時間帯による省電力モードの判定（新規）
  gui/
    renderer.py             SDL のライフサイクル / テクスチャ / フォント / 入力正規化
    overlay.py              時計・カウンタ・説明文・ステータス・メモリ表示 / カウントダウンゲージ
    text.py                 文字テクスチャの生成と折り返し（overlay の _Text を切り出した共用部品）
    transitions.py          遷移効果の描画（crossfade / fade_black / slide / wipe）と random の抽選
    widgets.py              Label / Button / Toggle / Slider / Spinner / ScrollView
    screens/                slideshow.py / menu.py / settings.py / album.py
```

## 対で更新が必要な箇所

片方だけ直すと壊れる組み合わせ。変更時は必ず両方を確認する。

-   `settings.json` / `settings.sample.json` のキー ↔ `config_manager.py` のデフォルト値
    ↔ 基本設定画面のウィジェット（**3点セット**。1つでも欠けると設定が反映されない）
-   `PhotoProvider.album_thumbnail_expires` ↔ `gui/screens/album.py` の
    `_request_stale_refresh()` ↔ 設定キー `cache_lifetime_hours` ↔ `PhotoCache` の
    サムネイルが `_touch()` されない前提（mtime = 最後に `store_thumbnail()` した時刻）。
    Immich は表紙を差し替えると `albumThumbnailAssetId` 自体が変わり
    `PhotoCache` のキャッシュキーが変わって自然に作り直されるため `False`。
    Drive はフォルダの表紙を固定キー（`albumThumbnailAssetId='cover'`）で表すため
    キーが変わらず、`True` にして `album.py` 側が `cache_lifetime_hours` 超過を
    `is_thumbnail_stale()` で判定し取り直しを依頼する。**`get_photo_path()` と違い
    `get_thumbnail_path()` は `_touch()` を呼ばない**契約を崩すと、閲覧するだけで
    mtime が更新され続け「古いのに古いと判定されない」状態になる
-   `settings.json` の `motion_sensor_enabled` ↔ `config_manager.py` の既定値 ↔
    基本設定画面のウィジェット ↔ **`main.py` の `_apply_motion_sensor_setting()`**。
    設定キーと画面だけ足して `main.py` の配線を忘れると、**トグルは動くのに GPIO が
    解放されない**（表示上は無効なのにセンサーが生きたままになる）。`main.py` 側は
    `MotionSensor.start()` の戻り値ではなく「意図した状態」を別フラグで持つ契約である
    ことにも注意する（開発環境では `start()` が常に False を返すため、戻り値だけで
    判定すると常に無効扱いになってしまう）
-   `settings.json` の `power_schedule_enabled` / `_start` / `_end` / `_off_hours`
    （4キー）↔ `config_manager.py` の既定値 ↔ 基本設定画面のウィジェット ↔
    `main.py` の `_update_power_saving()` の分岐 ↔ `power_schedule.py` の
    `normalize()` の既定値。**設定値そのものの反映には `_on_setting_changed()` への
    配線を持たない**（`power_saving_timeout` と同じく、`_update_power_saving()` が
    毎ループ `config.get()` で読むため）。**ただし `_schedule_changed_by_user` の
    印を立てるためだけに配線が要る**（次の bullet を参照。3点セットだけ足して
    満足しないこと）。
    `power_schedule_start` / `_end` は「0時からの分」（int）で持ち、Slider に
    そのまま渡す契約（`"09:30"` のような文字列は min/max/step で扱えない）。
    表示は `src/i18n.py` の `format_minutes()` が `time_format` に従って整形する
    （`gui/widgets.py` の `Slider.format_value` フックを `settings.py` が
    描画時にクロージャで渡す。ビルド時に `time_format` の値を束縛すると
    `time_format` を変えても表示が古い書式のまま残る）。
    **`power_schedule.py` の `MAX_MINUTE`（1410）↔ `settings.py` の `_ROWS` にある
    `power_schedule_start` / `_end` の Slider の `max_value`（1410）も対。** Slider は
    30分刻みで 23:30（1410分）までしか作れない（24:00 は選べない）ため、
    `normalize()` の範囲チェックもこれに合わせてある。ここを
    `MINUTES_PER_DAY - 1`（1439）のままにすると、Slider では絶対に作れない
    1411〜1439 の値（`settings.json` を手編集した場合）を正規化が「有効」として
    通してしまい、画面の表示可能範囲と設定ファイルの許容範囲がずれる
-   `main.py` の `_update_power_saving()` が `power_schedule.MODE_FORCE_OFF` へ
    遷移した瞬間に行う即消灯 ↔ **`_on_setting_changed()` が立てる
    `self._schedule_changed_by_user` の印**。基本設定画面で `power_schedule_off_hours`
    を強制消灯へタップで切り替えると、その操作自体で force_off への遷移が起きる。
    ここで無条件に消灯すると、直後の force_off 本体の分岐がタッチ入力を見て
    即座に再点灯してしまう（DPMS Off→On の連続 + `display_wakeup_delay` ぶんの
    入力無効）。**最初は `TouchWatcher.consume_input()`（タッチ入力）で見分けようと
    したが不十分だった**: Dev Container には入力デバイスが無く常に False、実機でも
    `TouchWatcher` は別スレッドのため `_update_power_saving()` と同じループ反復で
    同じ入力を読める保証が無い。`_on_setting_changed()` は
    `SettingsScreen.handle_input()` の TAP_UP から同期的に呼ばれ、run() は
    `_handle_events(now)` の直後に `_update_power_saving(now)` を呼ぶため、
    ここで印を立てれば TouchWatcher の有無やスレッドのタイミングに依存せず
    開発環境と実機で同じ挙動になる。印は `_update_power_saving()` の呼び出しごとに
    必ず1回だけ下ろす（モード遷移が起きなかった回でも下ろす。次に本当に時刻境界で
    遷移したときへ誤って持ち越さないため）。遷移時の即消灯は
    **`not schedule_changed`（印が立っていないとき）に限る**契約にしてあるので、
    この条件を外して「遷移したら常に消灯」に戻さないこと
-   `gui/renderer.py` の `ui_scale` / `px()` ↔ レイアウト定数を使う側
    （`gui/widgets.py` / `gui/screens/menu.py` / `settings.py` / `album.py` /
    `gui/overlay.py`）。これらのモジュールの寸法定数は「基準解像度 1024x600 に
    おける論理px」であり、`Widget.rect` を組み立てる時点や `Renderer.fill_rect()` /
    `draw_rect()` を直接呼ぶ箇所で `px()` を通す契約になっている
    （SPECIFICATION.md 5.5）。**新しい寸法定数を足したときに `px()` を通し忘れると、
    その要素だけ高解像度で小さいまま残る**（1024x600 では倍率が 1.0 になり誤りが
    表面化しないため、高解像度での目視でしか気づけない）。逆に `Label`/`Button` の
    `font_size` のように「渡す側は論理px、ウィジェット内部で1回だけ `px()` する」
    契約のものに呼び出し側で `px()` した値を渡すと二重適用になる。
    **オーバーレイの文字サイズ（`comment_font_size` とそこから派生する
    `RATIO_*`）だけはスケール対象外**なので、`overlay.py` に新しい文字要素を
    足すときは寸法（スケール対象）と文字サイズ（対象外）を混同しないこと。
    **`overlay.py` の `_bottom_bar_height()` も同じ理由でスケール対象外**
    （中に入る文字が `comment_font_size` 由来のため、帯の高さも文字サイズに
    連動させたままにしてある。見た目は寸法定数だが `px()` を通さない例外）。
    見た目に釣られて `px()` を足すと、文字は元のサイズのまま帯だけ厚くなり
    中央からずれる
-   `SPECIFICATION.md` の決定事項 ↔ `.claude/architecture.md` の確定事項
-   要検証事項を潰したとき: `SPECIFICATION.md` 第9章 ↔ `.claude/context/known-issues.md`
-   `display_manager.py` の消灯方式 ↔ SPECIFICATION.md 9-1 の検証結果
    ↔ `docker-compose.yml` のデバイスパススルー（消灯にデバイスが要る方式を選んだ場合）
-   `requirements.txt` ↔ `Dockerfile` の apt 依存（pygame-ce は SDL2 のランタイムを必要とする）
-   `max_slides_in_memory` ↔ テクスチャ実サイズ（1024x600x4 = 2.34MB/枚）↔ 9-5 の実測値
-   `photo_cache_max_mb` ↔ `photo_cache.py` の `enforce_limit()` ↔ 基本設定画面のウィジェット
    （`gui/screens/settings.py` の `_ROWS`。**3点セットは揃っている**）。
    **`0` は「無制限」を意味する特別値**で、`enforce_limit()` は `limit_mb <= 0` で
    何もせず戻る。スライダーは数値をそのまま表示するため、意味は表示名の側に
    埋め込んである（`（0で無制限）`）。片方だけ変えると 0 の解釈がずれる
-   `gui/renderer.py` の `generation` ↔ テクスチャを保持する側
    （`slideshow.py` の `_recreate_if_needed` / `overlay.py` の `_Text`）。
    **消灯で SDL を破棄するとテクスチャは全て無効になる。** 世代の変化を見て
    作り直す実装が片方でも欠けると、復帰後に真っ黒になるか
    `Parameter 'texture' is invalid` で落ちる
-   `gui/renderer.py` の復帰待ちウィンドウ（`open_wake_window` / `poll_wake_window` /
    `close_wake_window`）↔ `main.py` の `_poll_dev_wake_window()` と
    `IS_DEV_ENVIRONMENT` のゲート ↔ `Renderer.create()` / `destroy()` 冒頭の
    `close_wake_window()`。**開発環境（Dev Container / x11）専用**で、実機で開くと
    SDL が DRM master を握ってしまい消灯そのものが壊れるため、判定は
    `main.py` と `gui/renderer.py` の両方で `IS_DEV_ENVIRONMENT` を直接見る
    （`DisplayManager.available` では代用しない。libdrm の読み込み失敗でも
    False になりうるため）。復帰待ちウィンドウは `generation` を変えない
    （進めるのは `destroy()` だけ）。`create()` / `destroy()` 冒頭の
    `close_wake_window()` を外すと、pg.display の二重初期化や終了時の閉じ漏れになる
-   `gui/screens/album.py` の `_sync_visible_cells()` ↔ `_AlbumCell.release_labels()`
    ↔ `THUMBNAIL_MARGIN_ROWS` / `LABEL_MARGIN_ROWS`。
    **アルバム選択画面は「可視範囲の周辺だけテクスチャを持つ」契約**で、
    サムネイル（1枚 333KB。前後1行）とアルバム名（小さい。前後3行）で
    保持する範囲が違う。解放の駆動は `_sync_visible_cells()` の1か所にあり、
    セル側の受け口が `release_labels()`。片方だけ触ると、テクスチャが解放されず
    アルバム数に比例して積み上がるか（600件で1,177本を実測）、逆に可視範囲の
    セルまで解放して毎フレーム作り直しになる。**解放は「前回の範囲との差分」で
    行っている**（`self._label_range`）ため、範囲の求め方を変えるときは
    覚えておく側も必ず合わせること。アルバム名の余白を広く取っているのは
    往復スクロールでの再生成を減らすためで、一方向スクロールでは差が出ない
-   `touch_watcher.py` ↔ `docker-compose.yml` の `--device /dev/input` と
    `group_add`（input GID 996）。**消灯中は SDL が無く pygame のイベントを
    取得できない**ため、これが唯一のタッチ復帰手段になる
-   `display_wakeup_delay` ↔ `display_manager.py` の `is_ready` ↔ `main.py` の
    タイマー再開・スライド送り ↔ 基本設定画面のウィジェット
    （`gui/screens/settings.py` の `_ROWS`。**3点セットは揃っている**）。
    **パネル応答の実測は約 2.0〜2.4 秒**なので、既定値 3.0 秒を下げるときは
    `turn_on()` の戻りが「見えるようになった」ことを意味しない点に注意する
-   `photo_cache.py` の「余白を焼き込まない」方針（`_encode_jpeg` の `thumbnail()`）
    ↔ `slideshow.py` の `_dst_rect()` / `gui/transitions.py` の `_draw_crossfade()` /
    `rect_difference()`（`crossfade` のみが使う）。**余白が無い＝写真ごとにテクスチャの大きさが違う**ため、
    `crossfade` 中に次の写真へ覆われない帯を自前で黒へ落としている。余白を焼き込む方式へ
    変えるならこの処理は不要になり、逆に配置の式（`_dst_rect`）を複製するとフェードの
    塗り位置がずれる。**`gui/transitions.py` の各遷移の描画関数は `_dst_rect()` を
    `slideshow.py` から受け取って使う契約**で、`slide` / `wipe` のオフセットや
    切り出し矩形もこの1か所の等倍配置を起点に計算する。式を複製すると
    遷移の種類ごとに配置がずれる
-   `gui/transitions.py` の遷移名一覧（`crossfade` / `fade_black` / `slide` / `wipe`。
    `random` はこの4種から抽選する側で新しい遷移名そのものではない）↔
    `gui/screens/settings.py` の `_ROWS` にある Spinner の options ↔
    `config_manager.py` の `transition` の既定値 ↔ `SPECIFICATION.md` 7.3 の一覧表。
    遷移を1つ増やすときは、描画関数の追加だけでなく **`random` の抽選対象にも
    加える**（`transitions.py` 側の抽選リストを直さないと新しい種類が設定から選べても
    `random` では出てこない）。3点セットに `SPECIFICATION.md` も含めた4点で揃える
-   `photo_source.py` の `cache_key()`（`daily_pickup` のときだけ日付を含む）
    ↔ `main.py` の `_check_date_rollover()` ↔ `daily_pickup_manager.py` の
    `get_today_album_ids()`。**デイリーピックアップは日付でローテーションするが、
    スライドショーは写真リストを一巡しても再取得しない。** 実機は
    `restart: unless-stopped` の常時稼働なので、日付の変化を見て取得を起こす側が
    欠けると**再起動するまで初日の選択が表示され続ける**（実機で18時間半それが
    起きているのを実測した）。逆に `daily_pickup_date` を空にして呼ぶと
    「同じ日のまま選び直す」挙動になり、ローテーションのキューを余計に消費する。
    検知は消灯中も回す契約（深夜0時は通常消灯しており、そこで**写真リストの取得**を
    済ませておくと復帰時点で新しいリストがすぐ適用される。**写真本体の
    キャッシュ充填は復帰後**になる。先読みは `set_photos()` 起点で、それを呼ぶ
    `_collect_photo_list()` が消灯中は実行されないため）。
    **過去日付の掃除（`photo_cache.py` の `cleanup_list_cache()` を
    `photo_source.load_list()` から呼ぶ）もこの組に含まれる。** キーの作り方を
    変えると掃除の接頭辞（`daily_pickup_`）が合わなくなって空振りし、
    JSON が日付ごとに溜まり続ける
-   `photo_cache.py` の `display_size` ↔ 実機の表示解像度 1024x600
    （解像度を変えたらキャッシュ済みの画像はすべて作り直しになる）
-   `settings.json` の `photo_fit` ↔ `config_manager.py` の既定値 ↔
    基本設定画面のウィジェット（`gui/screens/settings.py` の `_ROWS`）↔
    `photo_cache.py` の `_photo_path()` の接尾辞と `_encode_jpeg()` の contain/cover
    分岐 ↔ `main.py` の `_on_setting_changed()`。**接尾辞の付け方を変えるとキャッシュが
    総入れ替えになる。** `contain` を接尾辞なしに保つことでのみ既存キャッシュが生きる。
    全方式へ一律で接尾辞を付けると 676枚が丸ごと再ダウンロードになり、
    2.4GHz Wi-Fi の帯域を浪費しないという方針に反する。
    **`cover` のテクスチャは 1024x600 ちょうど**なので `slideshow.py` の `_dst_rect()` が
    全画面を返し、`gui/transitions.py` の `rect_difference()` が空リストになる
    （crossfade の帯塗りが自動的に無効化される）。**smart では contain と cover が
    混在する**ため、この既存の汎用処理を特殊化してはいけない。
    `main.py` の配線を忘れると、**設定を変えても一巡するまで見た目が変わらない**
-   `docker-compose.yml` の `environment` ↔ `SDL_RENDER_DRIVER=opengles2`
    （消すと画面が真っ暗になる。デバッグ時に環境変数を整理して消さないこと）
-   `docker-compose.yml` の `volumes` ↔ `/run/udev:/run/udev:ro`
    （消すと SDL2 がタッチデバイスを列挙できなくなる）
-   `display_manager.py` の消灯実装 ↔ SDL の Window/Renderer のライフサイクル
    （消灯時に破棄し復帰時に再生成するため、テクスチャの保持と再生成も連動する）
-   `PF_CONFIG_DIR` / `PF_CACHE_DIR` ↔ `docker-compose.yml` の `volumes`
    （`./config:/config` の bind mount、`photo-cache:/cache` の named volume）
    ↔ ロジック層のパス解決（未設定時は `./config` / `./cache` にフォールバックし、
    階層1の Dev Container でも相対パスのまま動く契約。`Dockerfile` は `ENV` で
    `/config` / `/cache` を既定値として焼き込む）
-   `Dockerfile` のステージ順（`base` → `dev` → `runtime`。`runtime` が最後）
    ↔ `docker-compose.yml`（本番）の既定ターゲット（`target` 未指定 = 最終ステージ = `runtime`）
    ↔ `docker-compose.dev.yml` の `target: dev`。
    `runtime` を最後に置く前提を崩すと、`target` を指定していない本番の
    `docker compose build` が別のステージ（VNC 入りの `dev` 等）をビルドしてしまう。
-   `.github/workflows/release.yml` の `target: runtime` / `platforms: linux/arm64`
    ↔ `Dockerfile` のステージ名（`runtime`）↔ `docker-compose.yml` の `image:`
    （`ghcr.io/seizu-dev/raspi-photo-frame`）。イメージ名やステージ名を変えると、
    ワークフロー・`docker-compose.yml`・`tools/verification/album_bench_runner.sh`
    の3か所がずれる（後者は `IMAGE` 環境変数で上書きできるが既定値は揃えてある）。
-   `dev` ステージの `SDL_RENDER_DRIVER=opengl`（x86 / llvmpipe 用）↔ `runtime` ステージ /
    `docker-compose.yml` の `SDL_RENDER_DRIVER=opengles2`（VideoCore IV 用）。
    階層1と階層3で値が異なるのが正しい。デバッグ中に一方の値をもう一方へ揃えないこと。
-   `settings.json` の `language` / `time_format` / `date_format` ↔ `config_manager.py` の
    既定値 ↔ 基本設定画面のウィジェット（`gui/screens/settings.py` の `_ROWS`）の
    **3点セットが3組** ↔ `SPECIFICATION.md` 7.4。既定値はそれぞれ現行の表示
    （`%H:%M` / `2024/03/12`）と一字一句同じ結果になるよう選んである。ここを崩すと、
    実機の既存 `settings.json` に自動補完でキーが増えたときに見た目が変わってしまう
-   `src/i18n.py` の `generation()` ↔ 3画面（`menu.py` / `settings.py` / `album.py`）の
    `_recreate_if_needed()`。**片方が欠けると言語を切り替えてもその画面だけ古い言語の
    まま残る。** 3画面とも `(renderer.generation, i18n.generation())` の組で世代を持ち、
    どちらかが変わったら作り直す同じ形に揃えてある。`main.py` の `_on_setting_changed()`
    は `language` の変更を受けて `i18n.set_language()` を呼ぶだけで、3画面への反映は
    この世代比較に任せている（`main.py` 側からは画面を直接触らない）。**オーバーレイは
    この機構に乗っていない**ため、`main.py` が `overlay.invalidate()` を別途呼ぶ
    （写真が変わるまで古い言語のテクスチャが残ってしまうため）
-   `gui/screens/settings.py` の `_ROWS` の表示名は**翻訳キー**（`SettingsScreen._build()`
    の中で `t()` を呼んで解決する）↔ 直接文字列を書くとモジュール読み込み時の言語で
    固定され、言語を切り替えても古いままになる。`menu.py` の3ボタンの文言も同じ理由で
    `_build()` の中で解決している
-   `gui/widgets.py` の `Spinner` の `labels` ↔ `options`。**並びがずれると
    別の値を保存する**（`labels[i]` を `options[i]` の表示名として引くため）。
    `Spinner.value` は `labels` を渡しても内部値のまま変わらない契約で、
    `settings.py` の `_process_changes()` が `widget.value` をそのまま
    `ConfigManager.set()` に渡すため、ここが表示名になると**設定ファイルに
    日本語が書き込まれてしまう**。`_display_text()` は `value` が `options` に
    無い場合（壊れた設定ファイル等）に備えて `str(value)` へフォールバックする
    （`draw()` の中で例外を投げるとメインループごと落ち、`restart: unless-stopped`
    の再起動ループになりうるため）
-   `time_format` / `date_format` の取りうる値（`i18n.TIME_FORMATS` /
    `i18n.DATE_FORMATS`）↔ `i18n.format_time()` / `i18n.format_date()` の分岐 ↔
    `gui/screens/settings.py` の `_ROWS` にある Spinner の options（遷移効果の項と
    同じ4点セット。ここでは `SPECIFICATION.md` を含めた4点目は「取りうる値」を
    `SPECIFICATION.md` 7.4 にも書くことを指す）
-   `gui/screens/album.py` の `_VIRTUAL_ENTRIES` の `name_key`
    （`'album.favorites'` / `'album.daily_pickup'`）↔ `_AlbumCell._ensure_wrapped()`
    が `name_key` を見て `t()` で解決する経路 ↔ `_confirm_selection()` が仮想エントリの
    `album_name` に `''` を保存する契約。**仮想エントリに `albumName` の literal を
    直接持たせない**（モジュール読み込み時に固定され、言語を切り替えても
    `_entries` に入ったまま古い言語の名前が残るため）。`album_name` に `''` を
    保存する契約を保つことで、言語を切り替えても設定ファイルに古い言語の
    文字列が残る事故が起きない（Immich の実アルバム名は `name_key` を持たないので
    この経路の対象外のまま。元データなので翻訳しない）。言語が変わったときは
    `AlbumScreen._recreate_if_needed()` が `_build_header()` の呼び直しと
    `_AlbumCell.invalidate_wrapped()`（折り返し文字列側のキャッシュ破棄。
    テクスチャ側だけを破棄する `release_labels()` とは別物）の両方を行う。
    **SDL の世代の変化だけならテクスチャの破棄で足りるが、言語の変化はそれに加えて
    この2つが要る**（アルバム一覧の再取得は言語が変わっても絶対に行わない）。
    **`gdrive_api.py` の `fetch_albums()` が返す「未分類」仮想アルバム
    （ルート直下に画像が直接あるときだけ先頭へ差し込む）も同じ `name_key`
    契約に乗る**（キー `album.gdrive_root`）。既存の `_VIRTUAL_ENTRIES`
    （`favorites`/`daily_pickup`。固定リスト）とは別物で、`is_virtual` は
    **付けない**（`is_virtual` は「サムネイルを要求しない」という別の意味を持ち、
    未分類アルバムはサムネイルを持つため）。`id` に実在のフォルダIDを使うため
    `_confirm_selection()` の `else` 分岐（`source='album'`）へ自然に落ち、
    `entry.get('albumName') or ''` により `album_name=''` が保存される
    （`albumName` を持たせず `name_key` だけ持たせてあるため）。この経路は
    album.py に変更を加えずに動く
-   Google Drive のサービスアカウント鍵ファイル ↔ `.gitignore` の
    `/config/*.json`（`!/config/settings.sample.json` で例外）↔
    `gdrive_api.py` の `DEFAULT_SA_KEY_FILENAME`（`gdrive-service-account.json`）。
    ファイル名を変えるときは3箇所を揃える
-   写真取得元の抽象化（PR1/PR2。`.claude/plans/abundant-weaving-kernighan.md`）で
    生まれた組。取得元をさらに追加するとき（将来のローカルフォルダ対応等）は
    以下をすべて確認する。
    -   `PF_PHOTO_PROVIDER` 環境変数 ↔ `photo_provider.py` の `create_provider()` の
        分岐 ↔ `.env.sample` のコメント ↔ README の資格情報節（`GDRIVE_SA_KEY_FILE` /
        `GDRIVE_ROOT_FOLDER_ID` を含む）
    -   `PhotoProvider.cache_namespace` ↔ `PhotoCache.__init__` の `namespace` 引数
        （`photos/<ns>/` 等へのサブディレクトリ分け）↔ `enforce_limit()` /
        `get_total_size()` が見るルート（`self._photos_root` = 常に
        `cache_dir/photos` 直下。**namespace を持つインスタンスでも全取得元ぶんを
        合算する**契約を崩さない）↔ `cleanup_thumbnails()` / `cleanup_list_cache()`
        は逆に**自分の namespace 直下だけ**（非再帰の `glob`）を見る契約。
        **Immich は `cache_namespace = ''` で既存キャッシュのパスと1バイトも
        変わらない**ことが PR1 の通過条件だった
    -   `PhotoProvider.supports_favorites` ↔ `gui/screens/album.py` の
        `_fetch_albums_worker()`（お気に入りの仮想エントリを出すかどうか）↔
        `_restore_selection()`（固定 index ではなく `_find_entry_index()` で探す。
        お気に入りが無い取得元では仮想エントリの並びが1つ繰り上がるため）↔
        `photo_provider.reconcile_settings()`（`source == 'favorites'` なのに
        非対応の取得元へ切り替わったときに `daily_pickup` へ寄せる保険）
    -   `PhotoProvider.delivers_originals` ↔ `PhotoCache.__init__` の `originals`
        引数 ↔ `_encode_jpeg()` 内の EXIF 補正・画素上限（`MAX_ORIGINAL_PIXELS`）。
        **True の取得元（Drive）にしか掛けない。Immich 経路（False）には
        `exif_transpose` を一切掛けない**（Immich の preview は既に正しい向きで
        返るため、二重回転の恐れがある）。`main.py` / `warm_cache.py` は
        `provider.delivers_originals` を `PhotoCache(..., originals=...)` へ
        そのまま渡す配線を持つ
    -   Drive の `thumbnailLink` の要求寸法（`=wW-hH`）↔ `PhotoCache.display_size`
        （`gdrive_api.py` の `set_display_size()` で伝える。provider は
        `PhotoCache` より前に生成されるため、実際の表示解像度が分かるのは
        `main.py`/`warm_cache.py` が `PhotoCache` を作った後になる）↔
        `photo_cache.py` の `resolve_fit()`（Drive 側も同じ関数を再利用し、
        別の判定式を複製しない）。**`imageMediaMetadata.width/height` は
        回転前の値、`rotation` は 90° 単位の回数**（PoC 実測）なので、
        `gdrive_api.py` はこの2つを組み合わせて「見た目の向き」を求めてから
        `resolve_fit()` に渡す契約になっている
    -   **HEIC/HEIF の原本はデコードしない。** `gdrive_api.py` の
        `ORIGINAL_MIME_ALLOWED`（JPEG/PNG/WebP のみ）↔ `_skipped_originals`
        （一度スキップしたら `files.get` すら呼ばず即座に諦める）。
        48MP の HEIC を原本からデコードするとピーク約602MB（PoC 実測）になり、
        実機の RAM 416MB では成り立たないため
    -   **署名付き URL（thumbnailLink・原本のダウンロード URL）とアクセストークンを
        ログに出さない。** `gdrive_api.py` の `_redact()`（ホスト名だけを残す）を
        必ず経由する
    -   `PhotoCache.store_thumbnail()` の短辺 `THUMBNAIL_MAX_SHORT_SIDE`（250px）
        への縮小（拡大はしない）↔ `gui/screens/album.py` の `_AlbumCell` が
        描くサムネイル領域（232x232 の正方形セルへ中央クロップして表示するため、
        250px を下回ることは想定していない）。**Immich のサムネイル
        （333x250 / 444x250）は短辺がちょうど 250px なのでこの処理で縮小されない**
        （寸法が変わらないことが PR1 の通過条件だった）
    -   `_DECODE_LOCK`（`photo_cache.py` のモジュールレベルのロック）↔
        Pillow でデコードする全箇所（`_encode_jpeg()` 経由の写真本体・サムネイルの
        両方）。プロセス全体で1本にしてあるのは、原本を扱う取得元（PR2）の
        同時デコードでピークメモリが跳ね上がるのを防ぐため
    -   `active_provider`（`config_manager.py` の既定値 `""`。画面のウィジェットは
        持たない実行時状態） ↔ `photo_provider.reconcile_settings()`（前回と取得元が
        変わったときだけ `album_id` 等を初期化する）。**`""` → `'immich'` の遷移では
        何も初期化しない**（既存の実機の `settings.json` がアップデートのたびに
        消えるのを防ぐため）
    -   `src/immich_api.py` の `fetch_assets_info()` / `_fetch_daily_pickup_assets_info()`
        等の**旧経路は後方互換のためそのまま残してあり、`PhotoSource` はもう
        呼ばない**（`tools/verification/album_grid_bench.py` の
        `SyntheticAlbumAPI(ImmichAPI)` が `fetch_albums()` / `download_asset()` を
        直接使い続けるため）。ImmichAPI の既存メソッドを消したり改名したりしないこと
