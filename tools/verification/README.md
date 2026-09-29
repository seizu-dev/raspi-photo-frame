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

### `gdrive_probe.py` — Google Drive API の疎通確認（PR0 / PoC・階層1）

`.claude/plans/abundant-weaving-kernighan.md` PR0 の通過条件（SA で共有フォルダが
読めること）を確かめるワンショット。サービスアカウント（SA）でトークンを取得し、
共有フォルダ（ルート）直下のサブフォルダとファイルを1段だけ列挙する。
**google-api-python-client は使わない。** google-auth でトークンだけ取得し、
Drive REST v3 は requests で直接呼ぶ（`gdrive_api.py` の実装方針と揃えるため）。

依存（google-auth）は Dev Container にもコンテナイメージにも入っていない。
`--target` で入れた site ディレクトリを `PYTHONPATH` で渡して実行する。

```bash
pip install --only-binary=:all: --target /path/to/site google-auth==2.58.1
PYTHONPATH=/path/to/site python tools/verification/gdrive_probe.py \
    --key /path/to/sa.json --root <フォルダID>
```

`--download-dir` を指定すると、JPEG / HEIC・HEIF を優先して最大 `--max-download`
（既定2）件だけ原本をストリーミング取得し、Pillow の EXIF Orientation と Drive 側の
`imageMediaMetadata.rotation` を並べて出す（HEIC は pillow_heif が import できれば
自動で登録する）。

出力するのは mimeType 別件数、`md5Checksum` 欠落件数、`imageMediaMetadata` 欠落件数
（mimeType 別）、`time` の書式サンプル、rotation の分布、fileId に `.` を含む件数、
fileId の文字集合が `^[A-Za-z0-9_-]+$` に合わない件数、ショートカット件数。
**SA 鍵の中身・アクセストークンの値はログに出さない**（トークンは長さだけ出す）。

HTTP エラーはステータスと本文の先頭500文字を出し、403/404 は共有漏れの可能性がある旨を
添える。完了マーカーは `GDRIVE_PROBE_ALLDONE`。

**鍵ファイルは `tools/` や `samples/` に置かないこと。** `.gitignore` の対象外なので
誤ってコミットされうる。

#### `thumbnailLink` の確認（`--thumb-sizes` / `--thumb-max`）

改訂後の方針（`thumbnailLink` を優先し、失敗したら原本に切り替える）の裏取り用。
対象は `imageMediaMetadata.rotation` が 0 以外の画像・HEIC/HEIF・JPEG を優先して
最大 `--thumb-max`（既定 6）件選ぶ。各ファイルについて

```
files.get?fields=name,mimeType,hasThumbnail,thumbnailLink,imageMediaMetadata&supportsAllDrives=true
```

を呼び、返ってきた `thumbnailLink` 末尾の `=sNNN` を外したベースに、
`--thumb-sizes`（既定 `w1024-h600,s1024`。カンマ区切り）の各サイズ指定を付けて
**認可ヘッダなし・ありの両方で** GET する。ステータス・Content-Type・バイト数・
Pillow で開いた寸法を出す。

向きの確認は次を1行にまとめて出す: 原本の `imageMediaMetadata` の
width/height/rotation、（`--download-dir` で原本を取得済みなら）その EXIF
Orientation と Pillow 上の寸法、サムネイルの寸法。そこから「サムネイルの縦横が、
Drive の rotation / EXIF 回転を適用した後の縦横と一致するか」を
`orientation_match=True/False/unknown` として出す（原本が未取得、または
正方形で縦横が判定できない場合は `unknown`）。

```bash
PYTHONPATH=/path/to/site python tools/verification/gdrive_probe.py \
    --key /path/to/sa.json --root <フォルダID> \
    --download-dir ./gdrive_samples --max-download 4 \
    --thumb-sizes w1024-h600,s1024 --thumb-max 6
```

取得したサムネイルは `--download-dir` 指定時に
`<id>__<サイズ指定>.<no_auth|with_auth>.<拡張子>` で保存する（目視用）。
**署名付き URL はログに出さない**（ホスト名とサイズ指定だけを出す）。
最後にサイズ指定 x 認可有無ごとの成功/失敗件数と `orientation_match` の分布を集計する。

### `heic_decode_bench.py` — 画像1枚のデコードのピークメモリ・所要時間（PR0 / PoC）

`.claude/plans/abundant-weaving-kernighan.md` PR0 の通過条件（HEIC 12MP のピーク
メモリと fps への影響が許容範囲に収まること）を確かめる。**1プロセス1ケースで
呼ぶこと。** `ru_maxrss` は一度上がると同一プロセス内では下がらないため、
複数ケースを1プロセスで測ると値が前のケースに引きずられる。

```bash
# JPEG のみ（Pillow は Dev Container に入っているのでそのまま動く）
python tools/verification/heic_decode_bench.py photo.jpg --no-heif

# HEIC を含む場合（pillow_heif を PYTHONPATH 経由で渡す）
PYTHONPATH=/path/to/site python tools/verification/heic_decode_bench.py \
    photo.heic --mode decode --target 1024x600
```

計測点ごとに `resource.getrusage().ru_maxrss` と `/proc/self/status` の
VmRSS / VmHWM、経過秒を1行ずつ出す（開始 / Pillow import 後 / google-auth 系
import 後（`--mode import-only` のみ）/ pillow_heif 登録後 / `Image.open` 後 /
draft 後（JPEG のみ）/ `load()` 後 / `exif_transpose` 後 / `thumbnail()` 後 /
JPEG エンコード後）。最後に `BENCH_RESULT` で始まる1行 JSON でサマリを出す。
`Image.MAX_IMAGE_PIXELS` はいじらない。`DecompressionBombError` が出たら
例外のクラス名と MRO を出して終了コード 3 で終わる。完了マーカーは
`HEIC_BENCH_ALLDONE`。

### `gdrive_poc_runner.sh` — 実機用ランナー（PR0 / PoC・階層3）

`gdrive_probe.py` / `heic_decode_bench.py` が要る依存（Pillow / pillow-heif /
google-auth / requests）を `docker run` で site ディレクトリへ入れたうえで、
`$WORK_DIR/samples/` の画像ごとに `heic_decode_bench.py` を
（import-only 1回・decode を `MALLOC_ARENA_MAX` 未設定と `=2` の2通り）実行する。
実行中は `vmstat 1` を記録し、終了時に必ず止める。

```bash
mkdir -p ~/work/gdpoc/samples  # JPEG 12MP・HEIC 12MP・HEIC 48MP・PNG 等を事前に置く
setsid nohup bash tools/verification/gdrive_poc_runner.sh \
    > gdrive_poc_runner.log 2>&1 < /dev/null &
grep -q RUNNER_ALLDONE gdrive_poc_runner.log

# HEIC のピークメモリを測りたい場合（pillow-heif の要求を満たす Pillow を明示する）
export PILLOW_VERSION=11.1.0
setsid nohup bash tools/verification/gdrive_poc_runner.sh \
    > gdrive_poc_runner.log 2>&1 < /dev/null &
```

**`PILLOW_VERSION` は必須ではなくなった。** 既定値は `requirements.txt` と同じ
**10.4.0**。PR0 の調査結果（依存追加なし方針、Pillow 更新も pillow-heif の
本採用も見送り）に合わせた既定値で、通常の実行（`gdrive_probe.py` の疎通確認や
JPEG サンプルの計測）はこのままでよい。

**pillow-heif 1.8.0 系は Pillow>=11.1 を要求する。** ランナーは `PILLOW_VERSION` の
メジャー.マイナーを見て、11.1 未満なら **pillow-heif をインストールせず**、その旨を
ログに出したうえで decode ケースの呼び出しにも `--no-heif` を渡す（HEIC サンプルは
`Image.open()` が開けずに失敗するが、JPEG/PNG サンプルの計測は継続できる）。
HEIC のピークメモリを測るときだけ `PILLOW_VERSION=11.1.0` 以上を明示すること。

**アプリは停止しない**（稼働中の状態で測る。停止して測る場合は呼び出し側で
`docker compose stop` してから実行する）。CPU を占有する別プロセスにも触らない。
実行開始時と終了時に `free -m` / `docker ps` を出すので、
測定時にどちらの状態だったかはログから確認できる。

### PoC の結論: HEIC は原本デコードせず `thumbnailLink` に頼る

PR0 の実機測定（階層1 / x86）で、HEIC を原本からデコードした場合のピークメモリは
**48MP で約 602MB、20MP で約 211MB** だった。実機（OS が見える 416MB、空き
150〜220MB）ではこの経路は成り立たない。一方で `thumbnailLink` に `=w1024-h600`
を付けて要求すると、Drive 側でちょうど contain の寸法へ縮小され、HEIC も
JPEG/PNG へ変換された状態で返る（認可ヘッダが無くても取得できた）。この結果を受けて
**`thumbnailLink` を優先し、失敗したときだけ JPEG/PNG/WebP の原本経路へ切り替える
方針**に決まった。HEIC は原本経路の対象外（`thumbnailLink` が失敗したらスキップして
ログに残す）。Pillow の更新と pillow-heif の本採用（依存追加）はこの方針により
見送りになった。詳細は `.claude/plans/abundant-weaving-kernighan.md` と
`.claude/context/known-issues.md` を参照。

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
