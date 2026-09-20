# 実行用イメージ。python:3.13-slim をベースに arm64 実機上でネイティブビルドする（QEMU不要）。
# base -> dev -> runtime の3ステージ構成。runtime を最終ステージに置くことで、
# `docker compose build`（target 未指定）は従来どおり runtime を生成する。
FROM python:3.13-slim AS base

# ランタイム依存のみ（ビルド専用の依存は入れない。GPIO系は今回のスコープ外）。
# tools/verification/Dockerfile で実機動作が確認済みの組み合わせをそのまま使う。
#   libsdl2-*        : pygame-ce が要求する SDL2 本体・画像・フォント読み込み
#   libdrm2/libgbm1   : KMSDRM 描画に必要
#   libegl1/libgles2  : SDL_RENDER_DRIVER=opengles2（VideoCore IV は GLES2 のみ対応）に必要
#   fonts-noto-cjk    : 写真の説明文表示（7.2）で日本語を描画するため。
#                        実ファイルは /usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc
#                        （2026-09-02 に実機のイメージ内で確認済み。photo-frame と同じパス）
RUN apt-get update && apt-get install -y --no-install-recommends \
      libsdl2-2.0-0 libsdl2-image-2.0-0 libsdl2-ttf-2.0-0 \
      libdrm2 libgbm1 libegl1 libgles2 \
      fonts-noto-cjk \
 && rm -rf /var/lib/apt/lists/*

# uid/gid 1000 の非rootユーザーを作成する。実証済みの --user 1000:1000 に合わせる。
RUN groupadd -g 1000 app && useradd -u 1000 -g 1000 -m -s /usr/sbin/nologin app

# /cache と /config を先に作り、uid 1000 所有にしてから USER を切り替える。
# named volume は初回マウント時にイメージ側のディレクトリ所有権を引き継ぐため、
# これで非rootのまま書き込める。
RUN mkdir -p /cache /config && chown -R app:app /cache /config

WORKDIR /app

# requirements.txt だけ先に COPY してから pip install する（レイヤキャッシュのため。
# main.py 等アプリコードを変更してもここは再実行されない）。
COPY requirements.txt .

# --only-binary=:all: は必須。Zero 2 W 上でうっかりソースビルドが始まると
# 数十分単位で詰まるうえ 416MB の RAM ではビルドが落ちうる。wheel が無ければ
# ビルドがその場で失敗するのが正しい挙動（事故を検知させる）。
RUN pip install --only-binary=:all: --no-cache-dir -r requirements.txt

# USER はここでは切り替えない（dev ステージで追加の apt を実行するため）。
# runtime ステージ側で改めて USER app に切り替える。


# ---- 開発用ステージ（階層1 / Dev Container + VNC）----
# photo-frame の Dev Container 構成を土台にしているが、Kivy 由来の依存
# （GStreamer 一式 / libmtdev / wayland / npm）は持ち込まない。
# pygame-ce も SDL2 ベースのため SDL_VIDEODRIVER=x11 に切り替えるだけで
# x11vnc 越しに UI レイアウトを確認できる。
# openssh-client は git のコミット署名に必要（gitconfig が gpg.format=ssh のため
# ssh-keygen を呼ぶ）。署名鍵は VS Code が転送する SSH エージェント
# （SSH_AUTH_SOCK=/tmp/vscode-ssh-auth-*.sock）から取る。runtime には入れない。
FROM base AS dev

RUN apt-get update && apt-get install -y --no-install-recommends \
      xvfb fluxbox x11vnc novnc websockify x11-apps \
      libgl1 libgl1-mesa-dri \
      git openssh-client procps less sudo \
 && rm -rf /var/lib/apt/lists/*

# base では app のシェルを nologin にしているが、Dev Container は remoteUser=app で
# ターミナルを開くため bash へ変える。runtime ステージは nologin のまま。
RUN echo "app ALL=(root) NOPASSWD:ALL" > /etc/sudoers.d/app \
 && chmod 0440 /etc/sudoers.d/app \
 && usermod -s /bin/bash app

# x86 / llvmpipe（ソフトウェアレンダラ）向けの設定であり、実機で必須の opengles2 とは別物。
# 混同して本番の値（docker-compose.yml の SDL_RENDER_DRIVER=opengles2）を
# 書き換えないこと（.claude/architecture.md「対で更新が必要な箇所」参照）。
ENV SDL_VIDEODRIVER=x11
ENV SDL_RENDER_DRIVER=opengl
ENV DISPLAY=:1
ENV IS_DEV_ENVIRONMENT=true

WORKDIR /workspaces/pi-photo-frame
USER app

# ソースは COPY しない。docker-compose.dev.yml でワークスペースを bind mount する。
CMD ["sleep", "infinity"]


# ---- 実行用ステージ（本番）----
# 必ず最終ステージに置く。`docker compose build`（target 未指定）はこのステージを作る。
FROM base AS runtime

COPY --chown=app:app . /app

USER app

# イメージ既定値。docker-compose.yml 側でも明示する（管理 UI 上で値が見えるようにするため）。
ENV SDL_VIDEODRIVER=kmsdrm
# 既定の opengl ではテクスチャ描画が例外も出さずに無視される（SPECIFICATION.md 9-10）。
ENV SDL_RENDER_DRIVER=opengles2
# docker logs に即時反映させる
ENV PYTHONUNBUFFERED=1
# 未設定時は main.py 側で ./config / ./cache にフォールバックする契約（architecture.md 参照）
ENV PF_CONFIG_DIR=/config
ENV PF_CACHE_DIR=/cache

CMD ["python", "main.py"]
