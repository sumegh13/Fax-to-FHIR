from fhirmap import normalize as nz


def test_dates_in_fax_formats():
    assert nz.to_date("2/4/1945") == "1945-02-04"
    assert nz.to_date("02/04/1945") == "1945-02-04"
    assert nz.to_date("02-04-1945") == "1945-02-04"
    assert nz.to_date("1945-02-04") == "1945-02-04"
    assert nz.to_date(" 2 / 4 / 1945 ") == "1945-02-04"


def test_impossible_or_garbled_dates_are_rejected():
    assert nz.to_date("13/45/1990") is None
    assert nz.to_date("2/30/2020") is None
    assert nz.to_date("1l/3O/1966") is None
    assert nz.to_date("") is None


def test_two_digit_year_is_never_in_the_future():
    assert nz.to_date("1/1/99") == "1999-01-01"
    assert nz.to_date("1/1/05") == "2005-01-01"


def test_gender_codes():
    assert nz.to_gender("M") == "male"
    assert nz.to_gender("f") == "female"
    assert nz.to_gender("7") is None


def test_phone_needs_ten_digits():
    assert nz.to_phone("(555) 377-1773") == "555-377-1773"
    assert nz.to_phone("1-555-377-1773") == "555-377-1773"
    assert nz.to_phone("555-377-773") is None


def test_address_parsed_or_kept_as_text():
    a = nz.to_address("982 Effertz Esplanade, Needham, MA 02494")
    assert a["line"] == ["982 Effertz Esplanade"] and a["city"] == "Needham"
    assert a["state"] == "MA" and a["postalCode"] == "02494"
    assert nz.to_address("somewhere unreadable") == {"text": "somewhere unreadable"}


def test_numbers():
    assert nz.to_number("99.4") == 99.4
    assert nz.to_number("1,234") == 1234
    assert nz.to_int("30") == 30
    assert nz.to_int("2.5") is None
    assert nz.to_int("/") is None
