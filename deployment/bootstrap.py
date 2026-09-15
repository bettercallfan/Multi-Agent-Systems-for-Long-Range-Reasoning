"""Extract the bundled source safely; never overwrite an existing installation."""
from pathlib import Path, PurePosixPath
import zipfile


def unpack(base: Path) -> Path:
    dest = base / "system_source"
    if dest.exists():
        raise SystemExit("system_source already exists; preserve it and use a fresh directory.")
    with zipfile.ZipFile(base / "system_source.zip") as archive:
        for info in archive.infolist():
            path = PurePosixPath(info.filename)
            if path.is_absolute() or ".." in path.parts or "\\" in info.filename:
                raise ValueError("Unsafe archive member")
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError("Symlinks are not accepted")
        dest.mkdir()
        archive.extractall(dest)
    (dest / "memory").mkdir(exist_ok=True)
    (dest / "outputs/dashboard_jobs").mkdir(parents=True, exist_ok=True)
    return dest


if __name__ == "__main__":
    print(unpack(Path(__file__).resolve().parent))
