"""Verify the original bundle before copying it to an execution directory."""
import hashlib
from pathlib import Path, PurePosixPath
import sys


def verify(root):
    failures=[];count=0
    for line in (root/'SHA256SUMS.txt').read_text(encoding='utf-8').splitlines():
        expected,relative=line.split('  ',1)
        p=PurePosixPath(relative)
        if p.is_absolute() or '..' in p.parts:raise ValueError('Invalid manifest path')
        path=root/relative
        if not path.is_file():failures.append(relative);continue
        h=hashlib.sha256()
        with path.open('rb') as f:
            for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
        count+=1
        if h.hexdigest()!=expected:failures.append(relative)
    print(f'Checked {count} files; mismatches/missing: {len(failures)}')
    for name in failures:print(name)
    return not failures


if __name__=='__main__':sys.exit(0 if verify(Path(__file__).resolve().parent) else 1)
