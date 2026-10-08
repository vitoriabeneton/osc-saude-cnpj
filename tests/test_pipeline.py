"""Testes do ponto de entrada. Sessão HTTP falsa: nenhum acesso à internet."""

import logging
from collections import Counter
from datetime import date

import pandas as pd
import pytest

from osc_saude import pipeline
from osc_saude.tratamento import COLUNA_AUXILIAR, COLUNAS

REGISTRO = {
    "cnpj": "09548107000143",
    "situacao_cadastral": 2,
    "codigo_natureza_juridica": 3999,
    "natureza_juridica": "Associação Privada",
    "cnae_fiscal": 8610101,
    "cnae_fiscal_descricao": "Hospital",
    "cnaes_secundarios": [],
    "razao_social": "ASSOCIAÇÃO TESTE",
    "identificador_matriz_filial": 1,
    "uf": "AC",
    "ddd_telefone_1": "6833421176",
    "cep": 69985000,
    "data_inicio_atividade": "2008-04-09",
    "email": None,
}


class Resp:
    def __init__(self, status, corpo):
        self.status_code, self._corpo, self.text = status, corpo, str(corpo)

    def json(self):
        return self._corpo

    def raise_for_status(self):
        pass


class SessaoFalsa:
    def __init__(self, status_receita=200):
        self.status_receita = status_receita

    def get(self, url, timeout=None, **kwargs):
        if "servicodados.ibge.gov.br" in url:
            return Resp(200, [{"id": "8610101", "descricao": "Hospital"}])
        if self.status_receita != 200:
            return Resp(self.status_receita, "consulta inválida")
        return Resp(200, {"data": [REGISTRO]})


@pytest.fixture(autouse=True)
def sem_espera(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)


def tabela():
    linhas = []
    for cnpj, uf, cnae, desc, origem in [
        ("1", "AC", "8610101", "Hospital", "principal"),
        ("2", "AC", "8610101", "Hospital", "principal"),
        ("3", "SP", "8630501", "Ambulatório", "secundario"),
    ]:
        linha = dict.fromkeys(COLUNAS, "")
        linha.update(cnpj=cnpj, uf=uf, cnae_principal=cnae, cnae_principal_descricao=desc,
                     origem_cnae=origem, email="a@b.org" if cnpj == "1" else "", razao_social="Á Ç")
        linha[COLUNA_AUXILIAR] = cnpj
        linhas.append(linha)
    return pd.DataFrame(linhas)


# ----- exportação -----

def test_exportar_csv_formato_nome_e_colunas(tmp_path):
    caminho = pipeline.exportar_csv(tabela(), tmp_path / "saida", False, hoje=date(2026, 10, 8))
    assert caminho.name == "osc_saude_2026-10-08.csv"
    bruto = caminho.read_bytes()
    assert bruto.startswith(b"\xef\xbb\xbf")  # marca UTF-8 que o Excel reconhece
    texto = bruto.decode("utf-8-sig")
    cabecalho, primeira = texto.splitlines()[:2]
    assert cabecalho.split(";") == COLUNAS  # sem a coluna auxiliar
    assert "Á Ç" in primeira
    assert not list(tmp_path.rglob("*.tmp"))


def test_exportar_csv_modo_teste_tem_sufixo(tmp_path):
    caminho = pipeline.exportar_csv(tabela(), tmp_path, True, hoje=date(2026, 10, 8))
    assert caminho.name == "osc_saude_2026-10-08_TESTE.csv"


# ----- resumo -----

def test_resumo_traz_contagens_e_ordena_com_desempate():
    contagem = Counter(lidos=10, inativos=6, fora_natureza=0, sem_cnae_saude=1, duplicados=0)
    texto = pipeline.gerar_resumo(tabela(), contagem, com_email=True)
    assert "Registros lidos:               10" in texto
    assert "Estabelecimentos no resultado: 3" in texto
    assert "Com e-mail: 1 de 3" in texto
    # O CNAE mais frequente (2 registros) vem antes do outro (1).
    assert texto.index("8610101  Hospital") < texto.index("8630501  Ambulatório")


def test_resumo_sem_email_nao_menciona_email():
    texto = pipeline.gerar_resumo(tabela(), Counter(lidos=3), com_email=False)
    assert "Com e-mail" not in texto


# ----- linha de comando -----

def config_de(argv):
    parser = pipeline.criar_parser()
    return pipeline.construir_config(parser.parse_args(argv), parser)


def test_argumentos_viram_configuracao():
    c = config_de(["--teste", "--ufs", "ac", "sp", "--so-principal", "--com-email",
                   "--mes-receita", "2026-09", "--apagar-zips", "--pausa", "0.5"])
    assert c.modo_teste and c.ufs == ("AC", "SP")  # UF em minúsculas é aceita
    assert not c.buscar_em_secundario and c.enriquecer_email and c.apagar_zips
    assert c.mes_receita == "2026-09" and c.pausa_segundos == 0.5


def test_padroes_sao_conservadores():
    c = config_de([])
    assert not c.modo_teste and not c.enriquecer_email and not c.apagar_zips
    assert c.buscar_em_secundario and len(c.ufs) == 27


@pytest.mark.parametrize("argv", [["--ufs", "XX"], ["--com-email"], ["--com-email", "--mes-receita", "09/2026"]])
def test_argumentos_invalidos_encerram_com_codigo_2(argv):
    with pytest.raises(SystemExit) as e:
        config_de(argv)
    assert e.value.code == 2


# ----- execução completa -----

def test_main_de_ponta_a_ponta(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(pipeline, "criar_sessao", lambda: SessaoFalsa())
    codigo = pipeline.main(["--teste", "--dados", str(tmp_path), "--pausa", "0"])
    assert codigo == 0

    csv = list((tmp_path / "saida").glob("osc_saude_*_TESTE.csv"))
    assert len(csv) == 1
    linhas = csv[0].read_text(encoding="utf-8-sig").splitlines()
    assert len(linhas) == 2 and "09.548.107/0001-43" in linhas[1]
    assert (tmp_path / "saida" / (csv[0].stem + "_resumo.txt")).exists()
    assert "Estabelecimentos no resultado: 1" in capsys.readouterr().out
    assert (tmp_path / "logs" / "pipeline.log").exists()
    assert logging.getLogger("osc_saude").handlers == []  # logging encerrado


def test_main_erro_esperado_devolve_1_sem_traceback(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(pipeline, "criar_sessao", lambda: SessaoFalsa(status_receita=400))
    with caplog.at_level(logging.ERROR, logger="osc_saude"):
        codigo = pipeline.main(["--teste", "--dados", str(tmp_path), "--pausa", "0"])
    assert codigo == 1
    assert "recusou a consulta" in caplog.text
    assert "Traceback" not in caplog.text


def test_main_ctrl_c_devolve_130(tmp_path, monkeypatch):
    def interrompe(config):
        raise KeyboardInterrupt

    monkeypatch.setattr(pipeline, "executar", interrompe)
    assert pipeline.main(["--teste", "--dados", str(tmp_path)]) == 130


def test_main_erro_inesperado_devolve_1_com_traceback(tmp_path, monkeypatch, caplog):
    def quebra(config):
        raise RuntimeError("falha inesperada")

    monkeypatch.setattr(pipeline, "executar", quebra)
    with caplog.at_level(logging.ERROR, logger="osc_saude"):
        assert pipeline.main(["--teste", "--dados", str(tmp_path)]) == 1
    assert "RuntimeError" in caplog.text
