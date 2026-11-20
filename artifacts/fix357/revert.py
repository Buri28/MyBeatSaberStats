"""修正前の状態を一時コピーに作る（edit*.py の置き換えを逆向きに当てる）。テストが修正前に失敗することの確認用。"""
import os
import sys

root = sys.argv[1]


def edit(p, pairs):
    p = os.path.join(root, p)
    b = open(p, 'rb').read().decode('utf-8')
    crlf = '\r\n' in b
    s = b.replace('\r\n', '\n')
    for a, c in pairs:
        if s.count(c) != 1:
            print('skip (already reverted?)', p, c[:50].replace('\n', ' '))
            continue
        s = s.replace(c, a)
    if crlf:
        s = s.replace('\n', '\r\n')
    open(p, 'wb').write(s.encode('utf-8'))


MARKER = "open(p,'wb').write(s.encode('utf-8'))\n"
for f in sys.argv[2:]:
    code = open(f, encoding='utf-8').read()
    exec(code[code.index(MARKER) + len(MARKER):], {'edit': edit})
