"""Testes da leitura de CNAEs do IBGE, com sessão falsa."""

import pytest
import requests

from osc_saude import ibge
from osc_saude.excecoes import ColetaError


class Resp:
    def __init__(self, corpo, erro=False):
        self._corpo, self._erro = corpo, erro

    def raise_for_status(self):
        if self._erro:
            raise requests.HTTPError("500")

    def json(self):
        return self._corpo


class Sessao:
    def __init__(self, respostas):
        self.respostas = list(respostas)

    def get(self, url, timeout=None):
        return self.respostas.pop(0)


@pytest.fixture(autouse=True)
def sem_espera(monkeypatch):
    monkeypatch.setattr(ibge.time, "sleep", lambda s: None)


def test_carrega_e_junta_divisao_e_grupos():
    sessao = Sessao([
        Resp([{"id": "8610101", "descricao": "Hospital"}]),
        Resp([{"id": 8711502, "descricao": "Idosos"}]),
    ])
    assert ibge.carregar_cnaes_saude(sessao, ("871",)) == {"8610101": "Hospital", "8711502": "Idosos"}


def test_lista_vazia_do_ibge_vira_erro():
    with pytest.raises(ColetaError, match="não retornou"):
        ibge.carregar_cnaes_saude(Sessao([Resp([])]), ())


def test_falha_http_vira_erro():
    with pytest.raises(ColetaError, match="Não consegui"):
        ibge.carregar_cnaes_saude(Sessao([Resp(None, erro=True)]), ())
