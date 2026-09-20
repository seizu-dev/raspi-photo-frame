"""
Immich API の疎通確認（階層1: Dev Container から実 Immich へ）

immich_api.py の移植時に、実レスポンスで仕様を確定させるために使った。
写真が表示されない・0 件になるといった問題が起きたとき、
「サーバーの不調」「権限不足」「対象が空」のどれなのかを切り分けられる。

秘匿情報（API キー・ホスト名・アセット ID の生値）は出力しない。
.env を直接読むのは、env_file がコンテナ作成時にしか適用されず、
.env を書き換えてもコンテナ内の環境変数が更新されないため。

使い方:
    python tools/verification/immich_probe.py
"""
import sys
from io import BytesIO

import requests

ENV_PATH = '/workspaces/pi-photo-frame/.env'


def load_env(path: str = ENV_PATH) -> dict[str, str]:
    """ .env を素朴にパースする（python-dotenv は依存に入れていない） """
    env = {}
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


class Probe:
    def __init__(self, base_url: str, api_key: str) -> None:
        self.base = base_url.rstrip('/')
        self.headers = {'x-api-key': api_key, 'Accept': 'application/json'}

    def call(self, method: str, path: str, **kw):
        """ 生のレスポンスを返す。403/404 も握らず呼び出し側で判定する """
        return requests.request(method, self.base + '/api' + path,
                                headers=self.headers, timeout=30, **kw)

    def search(self, payload: dict) -> dict:
        r = self.call('POST', '/search/metadata', json=payload)
        if not r.ok:
            return {'_http': r.status_code, '_body': r.text[:160]}
        return r.json().get('assets') or {}


def section(title: str) -> None:
    print(f'\n=== {title} ===')


def main() -> int:
    env = load_env()
    if not env.get('IMMICH_BASE_URL') or not env.get('IMMICH_API_KEY'):
        print('IMMICH_BASE_URL / IMMICH_API_KEY が .env にありません')
        return 1
    if env['IMMICH_API_KEY'] == 'change-me':
        print('.env がサンプルのままです。実値を入れてください')
        return 1

    p = Probe(env['IMMICH_BASE_URL'], env['IMMICH_API_KEY'])

    section('1. 接続とサーバーバージョン')
    r = p.call('GET', '/server/version')
    print('  GET /server/version:', f'HTTP {r.status_code}', r.json() if r.ok else r.text[:120])
    if not r.ok:
        print('  → 接続できないため以降を中止する')
        return 1

    section('2. 権限の切り分け')
    # Immich は権限不足に対し 403 + 権限名を返す。200 が返るなら権限はある。
    # 「200 なのに 0 件」は権限不足ではなく対象が空であることを意味する。
    for method, path, payload in [('GET', '/users/me', None),
                                  ('GET', '/api-keys', None),
                                  ('POST', '/search/statistics', {})]:
        r = p.call(method, path, **({'json': payload} if payload is not None else {}))
        note = ''
        if r.status_code == 403:
            try:
                note = r.json().get('message', '')
            except Exception:
                note = r.text[:80]
        print(f'  {method} {path}: HTTP {r.status_code} {note}')

    section('3. 所有アセット数（タイムライン）')
    # 200 で 0 件なら「権限はあるが自分の写真が無い」。検索が 0 件になる原因の本命。
    r = p.call('GET', '/timeline/buckets', params={'size': 'MONTH'})
    if r.ok:
        buckets = r.json()
        print('  buckets:', len(buckets), '/ 合計:', sum(b.get('count', 0) for b in buckets))
    else:
        print('  HTTP', r.status_code, r.text[:120])

    section('4. アルバム')
    own = p.call('GET', '/albums').json()
    shared = p.call('GET', '/albums', params={'shared': 'true'}).json()
    merged = {a['id']: a for a in own + shared}
    print('  自分のアルバム:', len(own), '/ 共有アルバム:', len(shared), '/ マージ後:', len(merged))
    if merged:
        biggest = max(merged.values(), key=lambda a: a.get('assetCount', 0))
        print('  最大アルバムの assetCount:', biggest.get('assetCount'),
              '/ order:', repr(biggest.get('order')))
        full = p.call('GET', f"/albums/{biggest['id']}").json()
        items = full.get('assets', [])
        print('  実際に返るアセット数:', len(items), '（assetCount と一致:',
              len(items) == biggest.get('assetCount'), '）')
        # アルバム API はページ分割されない。並びは常に降順で返る前提を確認する
        dates = [(i.get('exifInfo') or {}).get('dateTimeOriginal') or i.get('fileCreatedAt')
                 for i in items]
        dates = [d for d in dates if d]
        print('  raw の並びが降順:', dates == sorted(dates, reverse=True))

    section('5. お気に入りとページネーション')
    fav = p.search({'isFavorite': True})
    print('  total:', fav.get('total'), '/ items:', len(fav.get('items') or []),
          '/ nextPage:', repr(fav.get('nextPage')))
    if (fav.get('total') or 0) >= 2:
        # size=1 で強制的に分割し、nextPage の実際の値と型を確定させる
        seen, page, pages = [], 1, 0
        while page and pages < 10:
            r2 = p.search({'isFavorite': True, 'size': 1, 'page': page})
            np = r2.get('nextPage')
            print(f'    page={page}: items={len(r2.get("items") or [])} '
                  f'nextPage={np!r} 型={type(np).__name__}')
            seen += [i['id'] for i in (r2.get('items') or [])]
            pages += 1
            if not np:
                break
            page = int(np)
        print('  巡回ページ数:', pages, '/ 収集件数:', len(seen),
              '/ 重複あり:', len(seen) != len(set(seen)))
    else:
        print('  → お気に入りが 2 件未満のためページ分割を検証できない')

    section('6. アセット情報のキー')
    sample = (fav.get('items') or [None])[0]
    if sample is None and merged:
        sample = (p.call('GET', f"/albums/{biggest['id']}").json().get('assets') or [None])[0]
    if sample:
        exif = sample.get('exifInfo') or {}
        print('  exifInfo.dateTimeOriginal:', '有' if exif.get('dateTimeOriginal') else '無/空')
        print('  fileCreatedAt:', '有' if sample.get('fileCreatedAt') else '無/空',
              '（dateTimeOriginal が無いときのフォールバック先）')
        print('  description:', '有' if sample.get('description') else '無/空')
        print('  visibility:', repr(sample.get('visibility')), '/ type:', sample.get('type'))

    section('7. 画像のフォーマットと解像度')
    # フォーマットは JPEG と決め打ちできない。同一サーバーでも JPEG と WebP が混在する。
    if sample:
        for size in ('preview', 'thumbnail'):
            r = requests.get(f"{p.base}/api/assets/{sample['id']}/thumbnail?size={size}",
                             headers=p.headers, timeout=30)
            if not r.ok:
                print(f'  {size}: HTTP {r.status_code}')
                continue
            try:
                from PIL import Image
                im = Image.open(BytesIO(r.content))
                print(f'  {size}: {len(r.content)} バイト / format={im.format} '
                      f'size={im.size} mode={im.mode}')
            except Exception as e:
                print(f'  {size}: {len(r.content)} バイト / Pillow で開けない: {e}')

    section('8. OpenAPI スペックによる仕様の裏取り')
    # 事前知識で断定せず、サーバー自身のスペックを読む
    r = p.call('GET', '/spec.json')
    if r.ok:
        spec = r.json()
        print('  spec version:', spec.get('info', {}).get('version'))
        paths = spec.get('paths', {})
        print('  /search 系のパス:', sorted(k for k in paths if k.startswith('/search')))
        print('  GET /assets の有無:', 'get' in (paths.get('/assets') or {}),
              '（Immich 2.x では存在しない）')
        dto = spec.get('components', {}).get('schemas', {}).get('MetadataSearchDto', {})
        props = dto.get('properties', {})
        print('  MetadataSearchDto の必須:', dto.get('required') or 'なし')
        print('  page / size / isFavorite の存在:',
              all(k in props for k in ('page', 'size', 'isFavorite')))
    else:
        print('  HTTP', r.status_code)

    print('\nPROBE DONE')
    return 0


if __name__ == '__main__':
    sys.exit(main())
