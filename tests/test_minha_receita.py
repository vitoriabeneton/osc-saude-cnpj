"""Testes da coleta, com uma sessão HTTP falsa (nenhum acesso à internet)."""

import json

import pytest
import requests

from osc_saude import minha_receita
from osc_saude.config import Config
from osc_saude.excecoes import ColetaError


class RespostaFalsa:
    def __init__(self, status=200, corpo=None, texto=""):
        self.status_code = status
        self._corpo = corpo
        self.text = texto

    def json(self):
        if self._corpo is None:
            raise ValueError("sem JSON")
        return self._corpo


class SessaoFalsa:
    """Devolve as respostas da lista, na ordem. Exceções na lista são levantadas."""

    def __init__(self, respostas):
        self.respostas = list(respostas)
        self.urls = []

    def get(self, url, timeout=None):
        self.urls.append(url)
        item = self.respostas.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture(autouse=True)
def sem_espera(monkeypatch):
    # Os testes não podem dormir de verdade.
    monkeypatch.setattr(minha_receita.time, "sleep", lambda s: None)


def test_buscar_pagina_ok():
    sessao = SessaoFalsa([RespostaFalsa(200, {"data": [1]})])
    assert minha_receita.buscar_pagina(sessao, "u", "ctx") == {"data": [1]}


def test_buscar_pagina_tenta_de_novo_em_erro_temporario():
    sessao = SessaoFalsa([
        requests.exceptions.Timeout(),
        RespostaFalsa(503),
        RespostaFalsa(200, {"data": []}),
    ])
    assert minha_receita.buscar_pagina(sessao, "u", "ctx") == {"data": []}
    assert len(sessao.urls) == 3


def test_buscar_pagina_400_falha_sem_repetir():
    sessao = SessaoFalsa([RespostaFalsa(400, texto="consulta inválida")])
    with pytest.raises(ColetaError, match="400"):
        minha_receita.buscar_pagina(sessao, "u", "ctx")
    assert len(sessao.urls) == 1


def test_buscar_pagina_404_vira_resultado_vazio():
    sessao = SessaoFalsa([RespostaFalsa(404)])
    assert minha_receita.buscar_pagina(sessao, "u", "ctx") == {"data": []}


def test_buscar_pagina_desiste_depois_das_tentativas():
    sessao = SessaoFalsa([requests.exceptions.ConnectionError()] * 4)
    with pytest.raises(ColetaError, match="falhou após"):
        minha_receita.buscar_pagina(sessao, "u", "ctx")


def test_coletar_uf_segue_cursor_e_descarta_campos_pessoais():
    sessao = SessaoFalsa([
        RespostaFalsa(200, {"data": [{"cnpj": "1", "qsa": [{"nome": "X"}]}], "cursor": "a b"}),
        RespostaFalsa(200, {"data": [{"cnpj": "2", "regime_tributario": []}]}),
    ])
    regs = minha_receita.coletar_uf(sessao, "http://x/?a=1", "AC")
    assert regs == [{"cnpj": "1"}, {"cnpj": "2"}]
    # O cursor vai codificado para a URL.
    assert sessao.urls[1].endswith("&cursor=a%20b")


def test_coletar_uf_respeita_limite_de_paginas():
    pagina = RespostaFalsa(200, {"data": [{"cnpj": "1"}], "cursor": "c1"})
    sessao = SessaoFalsa([pagina, RespostaFalsa(200, {"data": [{"cnpj": "2"}], "cursor": "c2"})])
    regs = minha_receita.coletar_uf(sessao, "http://x/?a=1", "AC", max_paginas=2)
    assert len(regs) == 2
    assert len(sessao.urls) == 2


def test_coletar_uf_detecta_cursor_repetido():
    resp = {"data": [{"cnpj": "1"}], "cursor": "mesmo"}
    sessao = SessaoFalsa([RespostaFalsa(200, resp)] * 3)
    with pytest.raises(ColetaError, match="repetiu"):
        minha_receita.coletar_uf(sessao, "http://x/?a=1", "AC")


def test_filtros_diferentes_bloqueiam_checkpoint_antigo(tmp_path):
    minha_receita.verificar_filtros(tmp_path, {"cnaes": ["1"]})
    minha_receita.verificar_filtros(tmp_path, {"cnaes": ["1"]})  # igual: passa
    with pytest.raises(ColetaError, match="filtros mudaram"):
        minha_receita.verificar_filtros(tmp_path, {"cnaes": ["2"]})


def test_coletar_grava_pula_uf_pronta_e_nao_deixa_tmp(tmp_path):
    config = Config(ufs=("AC", "AL"), pasta_dados=tmp_path, pausa_segundos=0)
    pasta = config.pasta_checkpoints
    pasta.mkdir(parents=True)
    (pasta / "AC.jsonl").write_text('{"cnpj": "1"}\n', encoding="utf-8")

    sessao = SessaoFalsa([RespostaFalsa(200, {"data": [{"cnpj": "2"}, {"cnpj": "3"}]})])
    totais = minha_receita.coletar(sessao, config, {"8610101": "x"})

    assert totais == {"AC": 1, "AL": 2}
    assert len(sessao.urls) == 1  # AC foi pulada
    assert not list(pasta.glob("*.tmp"))
    linhas = (pasta / "AL.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(x)["cnpj"] for x in linhas] == ["2", "3"]


def test_config_rejeita_uf_invalida():
    with pytest.raises(ValueError, match="XX"):
        Config(ufs=("AC", "XX"))
