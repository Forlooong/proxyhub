"""Build a deployment archive from an explicit source/dependency allowlist."""
from pathlib import Path
import tarfile

root = Path(__file__).resolve().parents[1]
destination = root / '.cache' / 'proxyhub-app.tar'
destination.parent.mkdir(exist_ok=True)
paths = [root/'src', root/'packages/converter/dist', root/'node_modules/yaml', root/'package.json', root/'LICENSE', root/'NOTICE.md']
with tarfile.open(destination, 'w') as archive:
    for path in paths:
        files = path.rglob('*') if path.is_dir() else [path]
        for file in files:
            if file.is_file() and not file.is_symlink() and '__pycache__' not in file.parts and file.suffix != '.pyc':
                archive.add(file, arcname=file.relative_to(root), recursive=False)
print('Packaged application and locked YAML runtime; no local credentials or evidence caches included.')
