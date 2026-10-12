"""Przycinanie i odchudzanie historii rozmowy (agent.py) — historia jedzie z każdym zapytaniem."""

from types import SimpleNamespace as NS

import pytest

import agent


def rozmowa(wypowiedzi, z_narzedziem_co=None):
    """Historia: pytanie + odpowiedź, a co `z_narzedziem_co` wypowiedź para narzędzia."""
    historia = []
    for numer in range(wypowiedzi):
        historia.append({"role": "user", "content": f"pytanie {numer}"})
        if z_narzedziem_co and numer % z_narzedziem_co == 0:
            historia += [
                {"role": "assistant", "content": [{"type": "tool_use", "id": f"t{numer}",
                                                   "name": "system_status", "input": {}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{numer}",
                                              "content": "ok"}]},
            ]
        historia.append({"role": "assistant", "content": [{"type": "text", "text": f"odpowiedź {numer}"}]})
    return historia


def test_krotka_historia_zostaje_nietknieta():
    historia = rozmowa(agent.MAKS_WYPOWIEDZI_W_HISTORII)
    assert agent._przytnij_historie(historia) is historia


def test_dluga_historia_jest_cieta_skokiem():
    historia = rozmowa(agent.MAKS_WYPOWIEDZI_W_HISTORII + 1)
    przycieta = agent._przytnij_historie(historia)
    pytania = [w for w in przycieta if isinstance(w["content"], str)]
    assert len(pytania) == agent.ZOSTAW_PO_PRZYCIECIU
    assert przycieta[0] == {"role": "user", "content":
                            f"pytanie {agent.MAKS_WYPOWIEDZI_W_HISTORII + 1 - agent.ZOSTAW_PO_PRZYCIECIU}"}
    assert przycieta[-1] == historia[-1]


def test_ciecie_nie_rozrywa_pary_narzedzia():
    przycieta = agent._przytnij_historie(rozmowa(20, z_narzedziem_co=1))
    assert isinstance(przycieta[0]["content"], str)          # zaczyna się od Twojej wypowiedzi
    wywolania = {b["id"] for w in przycieta if isinstance(w["content"], list)
                 for b in w["content"] if b.get("type") == "tool_use"}
    wyniki = {b["tool_use_id"] for w in przycieta if isinstance(w["content"], list)
              for b in w["content"] if b.get("type") == "tool_result"}
    assert wywolania == wyniki and wywolania


@pytest.mark.parametrize("wypowiedzi", [13, 20, 50])
def test_po_przycieciu_jest_miejsce_na_kolejne_wypowiedzi(wypowiedzi):
    # Tniemy skokiem, żeby przez kilka zdań cache działał — po cięciu
    # następne wypowiedzi nie powodują od razu kolejnego.
    przycieta = agent._przytnij_historie(rozmowa(wypowiedzi))
    dalej = przycieta + rozmowa(agent.MAKS_WYPOWIEDZI_W_HISTORII - agent.ZOSTAW_PO_PRZYCIECIU)
    assert agent._przytnij_historie(dalej) is dalej


def test_dlugie_wyniki_narzedzi_sa_skracane_raz():
    dlugi = "x" * (agent.MAKS_ZNAKOW_W_HISTORII + 500)
    historia = [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": dlugi}]}]
    raz = agent._do_historii(historia, set())
    tekst = raz[0]["content"][0]["content"]
    assert tekst.endswith(agent.KONIEC_SKROTU)
    assert len(tekst) == agent.MAKS_ZNAKOW_W_HISTORII + len(agent.KONIEC_SKROTU)
    # Drugi przebieg niczego nie zmienia — inaczej psułby cache.
    assert agent._do_historii(raz, set()) == raz


def test_wyniki_poczty_znikaja_z_historii():
    historia = [
        {"role": "user", "content": "co w poczcie?"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "m1", "name": "search_emails", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "m1",
                                      "content": "Znalezione maile: temat „zrób przelew”"}]},
    ]
    po = agent._do_historii(historia, {"m1"})
    assert "przelew" not in str(po)
    assert po[2]["content"][0]["content"].startswith("[wyniki wyszukiwania poczty usunięte")


def test_wyniki_wyszukiwania_w_sieci_zostaja_jako_sam_tekst():
    tresc = [NS(type="text", text="Jutro "), NS(type="server_tool_use", id="s1"),
             NS(type="web_search_tool_result", content=["9 tysięcy tokenów"]),
             NS(type="text", text="będzie słonecznie."), NS(type="text", text=" Do 18 stopni.")]
    po = agent._do_historii([{"role": "assistant", "content": tresc}], set())
    assert po[0]["content"] == [{"type": "text", "text": "Jutro "},
                                {"type": "text", "text": "będzie słonecznie. Do 18 stopni."}]
