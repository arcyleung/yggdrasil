"""Smoke tests that the package layout is installable and importable."""


def test_yggdrasil_package_importable():
    import yggdrasil

    assert hasattr(yggdrasil, "__version__")
    assert isinstance(yggdrasil.__version__, str)
    assert len(yggdrasil.__version__) >= 1


def test_domain_and_ports_packages_importable():
    import yggdrasil.domain  # noqa: F401
    import yggdrasil.ports  # noqa: F401
