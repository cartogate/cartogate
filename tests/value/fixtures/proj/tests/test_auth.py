from proj.auth import authenticate, validate


def test_authenticate():
    assert authenticate("alice")


def test_validate():
    assert validate("alice")
