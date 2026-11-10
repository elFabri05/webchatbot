"""PII masking: what must be removed and what must survive."""
import json

import pytest

from app.pii import Masker


@pytest.mark.parametrize("raw", [
    "600111005", "600 111 005", "600-111-005", "600.111.005", "+34600111005",
    "+34 600 111 005", "0034 600 111 005", "912345678", "+44 20 7946 0958",
])
def test_phone_formats_masked(raw):
    assert Masker()(f"mi número es {raw}, gracias") == "mi número es [PHONE], gracias"


@pytest.mark.parametrize("text", [
    "2026-11-10 12:00", "2026-11-10T12:00:00Z", "2026-11-09 06:30:59.654321",
    "ACC-50005 LN-1005 INC-2041 CUS-005", "rx -27.3 dBm, 15 cortes, 240 min",
    "NODO-NORTE-01", "web-3f9c2a7b61d0",
])
def test_ids_dates_and_measurements_survive(text):
    assert Masker()(text) == text


def test_customer_record_masked_by_key_and_name_learned():
    m = Masker()
    out = json.loads(m(json.dumps({"found": True, "customer": {
        "customer_id": "CUS-005", "name": "Ana López", "phone": "+34600111005",
        "account_id": "ACC-50005", "line_id": "LN-1005", "plan": "Fibra 600 Mb"}})))
    assert out["customer"] == {"customer_id": "CUS-005", "name": "[NAME]", "phone": "[PHONE]",
                               "account_id": "ACC-50005", "line_id": "LN-1005",
                               "plan": "Fibra 600 Mb"}
    # The name is now known, with or without accents / capitals.
    assert m("Hola Ana, ¿eres ANA LOPEZ?") == "Hola [NAME], ¿eres [NAME] [NAME]?"
    assert m("Tu plan es Fibra 600 Mb") == "Tu plan es Fibra 600 Mb"


def test_node_name_is_not_pii():
    node = {"node_id": "NODO-NORTE-01", "name": "Nodo Norte 1", "zone": "Norte"}
    assert Masker()(node) == node


def test_introduced_name_and_extras():
    m = Masker()
    assert m("Hola, me llamo Carlos Rodríguez y no tengo internet") == \
        "Hola, me llamo [NAME] y no tengo internet"
    assert m("soy el 600111004") == "soy el [PHONE]"
    assert m("escríbeme a carlos.r@example.com, DNI 12345678Z") == \
        "escríbeme a [EMAIL], DNI [ID]"
    assert m("gracias, Carlos") == "gracias, [NAME]"


def test_nested_structures_and_input_untouched():
    data = {"messages": [{"role": "user", "content": [{"type": "text", "text": "tel 600111005"}]}]}
    out = Masker()(data)
    assert out["messages"][0]["content"][0]["text"] == "tel [PHONE]"
    assert data["messages"][0]["content"][0]["text"] == "tel 600111005"
