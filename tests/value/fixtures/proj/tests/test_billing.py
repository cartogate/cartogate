from proj.billing import charge


def test_charge():
    assert charge(5)
