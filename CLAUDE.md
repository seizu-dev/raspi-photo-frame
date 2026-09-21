# pi-photo-frame

Raspberry Pi Zero 2 W + Immich のデジタルフォトフレーム。
先行プロジェクト photo-frame（Pi 3 Model B / Kivy。非公開）の機能仕様を
踏襲した作り直しで、**RAM 512MB で動作させるための軽量化**が最大の目的。

@.claude/architecture.md
@.claude/coding-style.md
@.claude/workflows.md

作業コンテキスト（`current-sprint.md`）・既知の問題（`known-issues.md`）・
実機の接続情報（`environment.md`）は自宅環境の情報を含むため**非公開リポジトリで管理**し、
`.claude/context/` へ clone して重ねる構成になっている。これらは `CLAUDE.local.md`
（`.gitignore` 対象）から読み込む。コード中のコメントやドキュメントに
`.claude/context/known-issues.md` への参照が残っているのはその出典を示すもので、
clone していない環境にはそのファイルは存在しない。

完全な仕様は `SPECIFICATION.md` を参照する。

## Quick facts

- 言語: Python 3
- FW: pygame-ce（`pygame._sdl2.video` の Renderer / Texture を使う）
- Graphics: SDL2 KMSDRM（実機）/ x11（devcontainer）。X11・Wayland は実機に入れない
- **`SDL_RENDER_DRIVER=opengles2` が必須**（既定の opengl では描画が黙って無視される）
- Hardware: Raspberry Pi Zero 2 W（RAM 512MB / VideoCore IV / Wi-Fi 2.4GHz のみ）
- Display: **1024x600 @ 49.61Hz**（`video=HDMI-A-1:1024x600MR@50e` が必須）／USB タッチ
- Sensor: AM312 PIR（GPIO 18）
- OS: Raspberry Pi OS Lite **ARM64 (64bit)**
- 実行形態: **Docker コンテナ**（`restart: unless-stopped`。**`mem_limit` は cgroup 無効のため使用不可**）
- 実機: **SSH でアクセス可。** Docker + リモート管理 UI のエージェント導入済みでリモート運用中
- Photo Service: Immich (Self-Hosted)
- CI: `v*` タグ push で GitHub Actions（arm64 ホストランナー）が実機と同じ arm64 イメージを
  ビルドして GHCR へ push し、GitHub Release を作る（`.github/workflows/release.yml`）

## エージェントへの指示

- **状態の維持**: 進捗・技術的決定・完了タスクは `.claude/context/` 配下に記録し、
  最新の状態に保つこと（「セッション終了して」で `session-record` スキルが使える）。
  **ただし `.claude/context/` は非公開リポジトリの clone であり、常に存在するとは限らない。**
  clone していない環境では記録先が無いので、書き込まずにその旨をユーザーへ知らせること。
- **軽量化の優先順位**: RAM 512MB が最大の制約。削減効果はフレームワーク選択よりも
  「画像バッファの枚数」と「画像パイプライン」の方が大きい。**OS が見えるのは 416MB**。
  512MB は OS → Docker デーモン → 他コンテナ → 本アプリ の順に消費される。
- **実機で測れることは測る。** 実機は SSH で使える。要検証事項を机上の推測で埋めない。
  検証は3階層（Dev Container / 実機ホスト直実行 / 実機コンテナ）あり、
  **どの階層で確認したかを必ず明示する**こと。
- **禁止パターンは `.claude/architecture.md` を必ず確認すること。**
  特にフルスクリーンのアルファ合成を CPU 側（`Surface.blit`）で行ってはならない。
- **参照元**: 機能仕様で迷ったら先行プロジェクト photo-frame の実装を参照する（非公開）。
  ただし Pi 3 Model B + DSI + Kivy 前提なので、GUI 層の設計は流用しないこと。
