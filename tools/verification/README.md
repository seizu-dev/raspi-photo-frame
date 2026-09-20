# 実機検証スクリプト

Raspberry Pi Zero 2 W 実機で要検証事項 9-1〜9-10 を潰すために使ったスクリプトと、
Immich API の実仕様を確定させるために使ったスクリプト。
**アプリ本体のコードではない。** 実装時の参考資料および、同種の問題が再発したときの再検証用。

`immich_probe.py` だけは**階層1（Dev Container）**で動かす。他はすべて実機用。

検証結果そのものは `SPECIFICATION.md` 第9章と `.claude/context/known-issues.md` を参照。

## 実行方法

実機へ転送し、コンテナ内で実行する。

```bash
docker run --rm \
  --device /dev/dri --device /dev/input \
  --group-add 44 --group-add 992 --group-add 996 \
  --user 1000:1000 \
  -v /run/udev:/run/udev:ro \
  -e SDL_VIDEODRIVER=kmsdrm \
  -e SDL_RENDER_DRIVER=opengles2 \
  -v $PWD:/app:ro <image> python /app/<script>.py
```

イメージは `Dockerfile`（pygame-ce 用）または `Dockerfile.drm`（modetest / edid-decode 用）から作る。

`immich_probe.py` は実機ではなく Dev Container 内でそのまま実行する。

```bash
python tools/verification/immich_probe.py
```

## 各スクリプト

### `drmdpms.py` — 9-1 ディスプレイ消灯（最重要）

`ctypes` で `libdrm.so.2` を呼び、DRM の DPMS プロパティを操作する。
**`display_manager.py` の実装はこれを土台にする。**

3段階の対照実験を含む。

1. SDL なしで DPMS Off/On → 成功
2. SDL が DRM master を保持した状態で別 fd から操作 → `EACCES (errno=13)` で拒否
3. SDL を破棄してから操作 → 成功、その後 SDL を再生成して描画も復帰

`drmModeRes` / `drmModeConnector` / `drmModePropertyRes` の ctypes 定義を含む。
DPMS 値は 0=On / 1=Standby / 2=Suspend / 3=Off。
**connector_id と DPMS プロパティ id は毎回列挙して取得すること**（実機では 33 と 2 だったが固定値にしない）。

### `touch3.py` — 9-2 タッチ入力

`FINGERDOWN` / `FINGERMOTION` / `FINGERUP` と `MOUSE*` を区別して記録し、
タッチ位置に矩形を描いて座標の対応を目視で確認できる。
`pygame._sdl2.touch.get_num_devices()` でデバイス認識も確認する。

`/run/udev` をマウントしないと SDL2 がデバイスを列挙できない点に注意。

### `verify.py` — 9-4 / 9-6 描画とフレームレート

全画面テクスチャ描画、部分描画（`dstrect`）、`Texture.alpha` によるクロスフェードを
順に実行し、fps と maxrss を測る。

### `draw2.py` — 9-10 描画方法の切り分け

`clear()` / `Texture.draw()` / `fill_rect()` / `Texture.draw(dstrect=)` を
時間で切り替えて、どれが実際に画面へ反映されるかを目視判定する。
レンダードライバの一覧とビューポート情報も出力する。

**このスクリプトで `SDL_RENDER_DRIVER=opengles2` が必須だと判明した。**
未指定だと SDL2 は既定の `opengl` を選び、VideoCore IV では
テクスチャ描画が例外も出さずに無視される。

### `immich_probe.py` — Immich API の実仕様確認（階層1）

`immich_api.py` の移植時に、実レスポンスで仕様を確定させるために使った。
**写真が 0 件になったときの切り分け手順がそのまま入っている。**

8 段階を順に確認する。

1. 接続とサーバーバージョン
2. **権限の切り分け** — Immich は権限不足に対し `403` と権限名（`Missing required
   permission: user.read` 等）を返す。したがって **`200` で 0 件なら権限不足ではない**
3. **所有アセット数**（`GET /timeline/buckets`）— 検索が 0 件になる原因の本命。
   検索は自分が所有するアセットのみを対象とし、共有アルバムの写真は含まない
4. アルバム（自分／共有の内訳、`assetCount` との一致、並び順）
5. **お気に入りとページネーション** — `size=1` で強制分割し `nextPage` の値と型を見る
6. アセット情報のキー（`dateTimeOriginal` が無いときの `fileCreatedAt` フォールバック）
7. **画像のフォーマットと解像度** — JPEG と決め打ちできない
8. `GET /api/spec.json` による仕様の裏取り

`.env` を直接読む。`env_file` はコンテナ作成時にしか適用されないため、
`.env` を書き換えてもコンテナ内の環境変数は更新されないからである。
**API キー・ホスト名・アセット ID の生値は出力しない。**

### `display_verify.py` — 9-1 消灯と復帰の実機検証（階層3）

`display_manager.py` を実機のコンテナで動かし、**消灯 → 復帰 → 再描画**を確認する。
`drmdpms.py` が方式の対照実験だったのに対し、こちらは**実装したクラスの検証**である。

赤 → 消灯12秒 → 青 → 消灯8秒 → 緑 → 消灯中に `cleanup()` という流れで、
各段階の `sysfs dpms` と fps を出力する。**目視での確認が必須。**

2026-09-02 の実測: `sysfs dpms` が On/Off に追随し、`drmSetMaster` は4回とも成功
（`EACCES` なし）、fps は **49.5 / 49.7 / 49.7**。消灯を挟んで SDL を作り直しても
垂直同期が保たれることを確認した。

### `warm_cache.py` — 性能検証用: ディスクキャッシュの全量充填

現在の写真ソース（`settings.json` の `source` / `album_id`）に含まれる写真を
すべてディスクキャッシュへ充填するワンショットスクリプト。「fps の落ち込みは
キャッシュ未充填の初回一巡だけの現象である」という仮説を検証するため、
定常状態を人為的に作るのが役割。**pygame は import しない。**

```bash
python tools/verification/warm_cache.py
```

1枚ごとにヒット/新規取得と所要時間をログに出し、最後にサマリと完了マーカー
`WARM_ALLDONE` を出力する。実機では `setsid nohup` で切り離して実行し、
ログをポーリングして回収する（`.claude/workflows.md` 参照）。

### `evdev_probe.py` — タッチの生イベント観測（階層2 / 非侵襲）

`/dev/input/event*` を直読みし、1回の接触で `ABS_MT_TRACKING_ID` が何本生まれるかを
記録する。「1回のタップで写真が複数枚送られる」疑いを物理層で切り分けるために作った。

```bash
# 実機ホストで。第1引数は記録する秒数
python3 tools/verification/evdev_probe.py 180 /dev/input/event0 /dev/input/event1
```

**pygame も SDL も要らず、アプリを止めずに実行できる。** evdev は複数のプロセスへ
同じイベントを配信するため、稼働中の SDL や `touch_watcher.py` と競合しない。
完了マーカーは `PROBE_ALLDONE`。

2026-09-03 の観測結果: **1回の接触につき `tracking_id` は必ず1本**（`slot=0` 固定）で、
パネルのゴーストタッチは無い。`event1`（マウスエミュレーション側）は
**1件もイベントを出していない**。

### `album_grid_bench.py` / `album_bench_runner.sh` — アルバムグリッドの性能（階層3）

実アルバムを循環複製して任意件数を合成し、**本物の `Renderer` と `AlbumScreen` を
機械的に駆動する**ベンチ。実機の Immich はアルバムが十数件しか無いため、
「数百件でスクロールが保つか」を測るにはこれが要る。**本番コードは変更しない**
（`AlbumScreen` の非公開メンバを直接読む。計測スクリプトに限った割り切り）。

```bash
# 単発（リポジトリルートから）
python tools/verification/album_grid_bench.py --count 300 \
    --cache-dir ./bench-cache --config-dir ./bench-config

# 実機で件数をまとめて（アプリを止めて測り、trap で必ず起動し直す）
setsid nohup bash album_bench_runner.sh 50 150 300 600 > album_bench.log 2>&1 < /dev/null &
grep -q RUNNER_ALLDONE album_bench.log
```

**`--cache-dir` に本番の `/cache` を渡してはならない。** `AlbumScreen` は取得した
アルバム一覧で `cleanup_thumbnails()` を呼ぶため、合成アルバムの一覧を渡すと
本番のサムネイルが全て消える（スクリプト側でも `/cache` は拒否する）。

既定でサムネイルを事前充填してから測る（`--no-prefill` で初回訪問の測定になる）。
取得ワーカーは直列で1枚あたり約1秒かかるため、充填しないと
「スクロール性能」ではなく「ダウンロード待ち」を測ることになる。
完了マーカーは `ALBUMBENCH_ALLDONE`。

2026-09-08 の測定結果は `.claude/context/known-issues.md` を参照。
**600 件でも fps 中央値 49.7 / サムネイル常駐 24 枚で頭打ち。**

## 教訓

**ソフトが成功を返すことは、画面に映っていることの証明にならない。**

`SDL_RENDER_DRIVER` 未指定の状態でも `RESULT: OK` が返り、920fps という数値まで出ていたが、
実際には何も描画されていなかった。正しいドライバを指定したら 294.9fps に落ち、
そこで初めて実際に描画されていると確認できた。

**性能値が「良すぎる」ときは、まず測定対象が本当に動いているかを確認すること。**

**0 件は「壊れている」とは限らない。**

Immich の検索が全条件で 0 件を返したとき、最初に「検索 API が機能していない」と
結論づけたが誤りだった。実際は所有アセットが 0 枚で、0 件が正しい結果だった。
`403` が返るかどうかで権限を切り分け、`GET /timeline/buckets` で対象の有無を直接数えれば、
「壊れている」「権限が無い」「対象が空」を区別できる。

**判定に使った値が有効かを確認すること。** 上の切り分けの途中で
「自分が所有するアセットは 0 件」と報告したが、その根拠は `ownerId == my_id` の比較で、
`my_id` は `GET /users/me` が 403 だったため `None` だった。
**比較そのものが無意味だったのに、結論の根拠として使ってしまった。**

**fps が高いのは「描画されていない証拠」ではなく「同期していない証拠」。**

`display_verify.py` の初回実行で fps が 1015 と出た。本ファイルの指針どおり異常を疑ったが、
原因は検証スクリプトが `sdl2.Renderer(win)` に `vsync=True` を指定していなかっただけだった。
指定して測り直すと 49.5〜49.7 に収まった。**測定条件を先に疑うこと。**
