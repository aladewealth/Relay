from relay.extraction import amount_candidates, date_candidates, generate_candidates, reference_candidates


def test_reference_formats():
    refs = {c["value"]: c["kind"] for c in reference_candidates(
        "Order #A-4471, also ORD-123 and order-55120, invoice INV-20931, ticket TKT-8812, invoice number: 7781-AB")}
    assert refs["#A-4471"] == "order"
    assert refs["ORD-123"] == "order"
    assert refs["INV-20931"] == "invoice"
    assert refs["TKT-8812"] == "ticket"
    assert "7781-AB" in refs


def test_amounts_and_currency():
    a = amount_candidates("I paid $199.00, then 99 USD, then ₦185,000 and €12.50")
    assert "$199.00" in a and "99 USD" in a and "₦185,000" in a and "€12.50" in a


def test_dates():
    d = date_candidates("Charged on Nov 3rd, again on 2026-11-10 and 11/12, and yesterday")
    assert "Nov 3rd" in d and "2026-11-10" in d and "11/12" in d and "yesterday" in d


def test_products_exclude_names_and_refs():
    c = generate_candidates("Hi, I am Jane Doe. My Lumen Cam Pro (order #A-4471) broke. Email jane@x.com", "Jane Doe")
    assert "Lumen Cam Pro" in c["products"]
    assert not any("Jane" in p for p in c["products"])
    assert not any("A-4471" in p for p in c["products"])
    assert c["emails"] == ["jane@x.com"]


def test_no_candidates():
    c = generate_candidates("thanks, all good now")
    assert c["references"] == [] and c["amounts"] == [] and c["products"] == []
