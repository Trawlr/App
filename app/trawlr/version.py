from pathlib import Path

_VERSION_FILE = Path(__file__).resolve().parent.parent / 'VERSION'


def get_build_number():
    try:
        return _VERSION_FILE.read_text().strip()
    except Exception:
        return "dev"
