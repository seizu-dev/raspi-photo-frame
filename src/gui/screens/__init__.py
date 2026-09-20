"""
画面の共通契約

各画面（`SlideshowScreen` / `MenuScreen` / 今後の設定・アルバム選択画面）は
以下のメソッドを持つ想定にする（Python の Protocol は使わず、ダックタイピングで
足並みを揃える。画面は少数なので厳密な抽象基底クラスにするメリットが薄い）。

    on_enter() -> None
    on_leave() -> None
    update(now: float) -> None
    draw() -> None
    handle_input(kind: str, x: int, y: int) -> str | None

`handle_input` の `kind` は `src/gui/renderer.py` の `TAP_DOWN` / `TAP_MOVE` /
`TAP_UP` のいずれか。戻り値はここに定義するアクション定数か None
（None は「このイベントは画面が消費し、画面遷移は起きない」の意味）。
"""

# 戻る（メニュー階層を1つ閉じる）
ACTION_BACK = 'back'
# メニュー画面を開く（スライドショーの歯車ボタンから）
ACTION_MENU = 'menu'
# 基本設定画面を開く
ACTION_SETTINGS = 'settings'
# アルバム選択画面を開く
ACTION_ALBUM = 'album'
# アプリを終了する
ACTION_QUIT = 'quit'
# 設定変更を反映するために写真リスト等を再読み込みする
# （アルバム選択画面で source/album を確定したときなど）
ACTION_RELOAD = 'reload'
