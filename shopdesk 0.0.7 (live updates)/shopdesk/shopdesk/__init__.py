import pathlib

APP_NAME = "NoksForge ShopDesk"


def _read_version():
    """The VERSION file in the project root is the single source of truth (releases are tagged v<VERSION>)."""
    try:
        return (pathlib.Path(__file__).resolve().parent.parent / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return "0.0.0"


__version__ = _read_version()
