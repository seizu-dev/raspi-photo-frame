#!/usr/bin/env python3
"""
1回のタップで evdev が何を報告するかを記録する（非侵襲・読み取りのみ）

`request_next()` の多重発火の原因を物理層で確定させるために使う。
アプリ（コンテナ）を止めずに実行できる。evdev は複数のプロセスへ同じ
イベントを配信するため、稼働中の SDL / touch_watcher とは競合しない。

使い方: evdev_probe.py <秒数> <デバイス> [<デバイス> ...]
"""
import select
import struct
import sys
import time

# 64bit 環境の struct input_event: tv_sec, tv_usec, type, code, value
FMT = 'llHHi'
SIZE = struct.calcsize(FMT)

EV_SYN, EV_KEY, EV_ABS, EV_REL, EV_MSC = 0x00, 0x01, 0x03, 0x02, 0x04

NAMES = {
    (EV_SYN, 0x00): 'SYN_REPORT',
    (EV_SYN, 0x02): 'SYN_MT_REPORT',
    (EV_KEY, 0x110): 'BTN_LEFT',
    (EV_KEY, 0x14a): 'BTN_TOUCH',
    (EV_ABS, 0x00): 'ABS_X',
    (EV_ABS, 0x01): 'ABS_Y',
    (EV_ABS, 0x2f): 'ABS_MT_SLOT',
    (EV_ABS, 0x35): 'ABS_MT_POSITION_X',
    (EV_ABS, 0x36): 'ABS_MT_POSITION_Y',
    (EV_ABS, 0x39): 'ABS_MT_TRACKING_ID',
}


def name_of(etype: int, code: int) -> str:
    return NAMES.get((etype, code), f'type={etype} code=0x{code:x}')


def main() -> None:
    duration = float(sys.argv[1])
    paths = sys.argv[2:]

    files = {}
    for path in paths:
        try:
            files[open(path, 'rb', buffering=0)] = path
        except OSError as exc:
            print(f'OPEN_FAILED {path}: {exc}', flush=True)

    if not files:
        print('NO_DEVICE', flush=True)
        print('PROBE_ALLDONE', flush=True)
        return

    # デバイスごとの集計。1回の接触で tracking_id が何本生まれるかが本題
    stats = {p: {'syn': 0, 'track_new': 0, 'track_end': 0,
                 'btn_down': 0, 'btn_up': 0, 'slots': set()} for p in files.values()}

    print(f'PROBE_START {time.strftime("%H:%M:%S")} duration={duration}s '
          f'devices={list(files.values())}', flush=True)

    deadline = time.monotonic() + duration
    cur_slot = {p: 0 for p in files.values()}

    while time.monotonic() < deadline:
        remain = deadline - time.monotonic()
        ready, _, _ = select.select(list(files), [], [], min(0.5, max(0.0, remain)))
        for fobj in ready:
            path = files[fobj]
            data = fobj.read(SIZE * 64)
            for off in range(0, len(data) - SIZE + 1, SIZE):
                sec, usec, etype, code, value = struct.unpack(FMT, data[off:off + SIZE])
                stamp = time.strftime('%H:%M:%S', time.localtime(sec)) + f'.{usec // 1000:03d}'
                label = name_of(etype, code)

                st = stats[path]
                if (etype, code) == (EV_ABS, 0x2f):
                    cur_slot[path] = value
                    st['slots'].add(value)
                elif (etype, code) == (EV_ABS, 0x39):
                    if value >= 0:
                        st['track_new'] += 1
                    else:
                        st['track_end'] += 1
                elif (etype, code) == (EV_KEY, 0x14a):
                    st['btn_down' if value else 'btn_up'] += 1
                elif (etype, code) == (EV_SYN, 0x00):
                    st['syn'] += 1

                if etype == EV_MSC:
                    continue  # MSC_SCAN 等はノイズなので出さない
                slot = cur_slot[path]
                print(f'{stamp} {path} slot={slot} {label} value={value}', flush=True)
            if stats[path]['syn'] and data:
                print(f'{"-" * 12} {path} SYN 区切り', flush=True)

    print('=== 集計 ===', flush=True)
    for path, st in stats.items():
        print(f'{path}: SYN_REPORT={st["syn"]} '
              f'tracking_id生成={st["track_new"]} 破棄={st["track_end"]} '
              f'BTN_TOUCH押下={st["btn_down"]} 離し={st["btn_up"]} '
              f'使われたslot={sorted(st["slots"])}', flush=True)
    print('PROBE_ALLDONE', flush=True)


if __name__ == '__main__':
    main()
