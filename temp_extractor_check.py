from git import Repo
from pathlib import Path
import subprocess
import shutil
import re

tmp = Path('data/raw/debug_test')

tmp.parent.mkdir(parents=True, exist_ok=True)

if tmp.exists():
    shutil.rmtree(tmp, ignore_errors=True)

print('Cloning RxJava for debug...')

result = subprocess.run(
    [
        'git',
        'clone',
        '--depth=50',
        '--no-tags',
        '-q',
        'https://github.com/ReactiveX/RxJava.git',
        str(tmp)
    ],
    timeout=120,
    capture_output=True,
    text=True
)

if result.returncode != 0:
    print(result.stderr)
    raise RuntimeError("Clone failed")

repo = Repo(str(tmp))

commits = repo.iter_commits(max_count=50)

print('Repository loaded successfully')

# Find first commit with fix keyword
import re
pattern = re.compile(r'\b(fix|bug|issue|patch|resolve)\b', re.IGNORECASE)
for commit in commits:
    message = commit.message
    if isinstance(message, bytes):
        message = message.decode("utf-8", errors="ignore")
    if len(commit.parents) == 1 and isinstance(message, str) and pattern.search(message):
        print(f'\nFound bug commit: {commit.hexsha[:8]}')
        print(f'Message: {message[:80]}')
        parent = commit.parents[0]
        
        # Test diff
        try:
            diffs = parent.diff(commit, create_patch=True)
            print(f'Diffs count: {len(list(diffs))}')
            for d in parent.diff(commit, create_patch=True):
                print(f'  change_type={d.change_type} b_path={d.b_path}')
                if d.b_path and d.b_path.endswith('.java') and d.change_type == 'M':
                    print('  -> Trying file content extraction...')
                    try:
                        blob = commit.tree / d.b_path
                        raw = blob.data_stream.read()
                        print(f'  -> fixed_code OK: {len(raw)} bytes')
                    except Exception as e:
                        print(f'  -> fixed_code FAILED: {e}')
                    try:
                        blob2 = parent.tree / d.b_path
                        raw2 = blob2.data_stream.read()
                        print(f'  -> buggy_code OK: {len(raw2)} bytes')
                    except Exception as e:
                        print(f'  -> buggy_code FAILED: {e}')
        except Exception as e:
            print(f'Diff FAILED: {e}')
        break

shutil.rmtree(tmp, ignore_errors=True)
print('Done.')