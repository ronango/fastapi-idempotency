from fastapi_idempotency import __version__


def test_version_exported() -> None:
    assert __version__ == "0.1.0.dev0"
