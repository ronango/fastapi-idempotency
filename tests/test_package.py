from fastapi_idempotency import __version__


def test_version_exported() -> None:
    assert __version__ == "0.2.0rc1"
