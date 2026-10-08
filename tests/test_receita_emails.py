"""Testes da etapa de e-mail, sem internet: zips pequenos e uma sessão HTTP falsa."""

import io
import zipfile

import pandas as pd
import pytest
import requests

from osc_saude import receita_emails as re_
from osc_saude.config import Config
from osc_saude.excecoes import ColetaError
from osc_saude.tratamento import COLUNA_AUXILIAR


def linha_estab(basico, ordem, dv, email="", colunas=30):
    campos = [""] * colunas
    campos[0], campos[1], campos[2] = basico, ordem, dv
    if colunas > 27:
        campos[27] = email
    return ";".join(f'"{c}"' for c in campos)


def criar_zip(linhas, nome_interno="K3241.K03200Y0.D60913.ESTABELE"):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr(nome_interno, "\n".join(linhas).encode("latin-1"))
    return buffer.getvalue()


class RespostaFalsa:
    def __init__(self, conteudo, status=200, headers=None, cai_depois=None):
        self.content, self.status_code = conteudo, status
        self.headers = headers or {}
        self.cai_depois = cai_depois  # simula queda de conexão no meio do download

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            erro = requests.exceptions.HTTPError(str(self.status_code))
            erro.response = self
            raise erro

    def iter_content(self, chunk_size=1):
        dados = self.content if self.cai_depois is None else self.content[: self.cai_depois]
        for i in range(0, len(dados), chunk_size):
            yield dados[i : i + chunk_size]
        if self.cai_depois is not None:
            raise requests.exceptions.ChunkedEncodingError("conexão caiu")


class ServidorFalso:
    """Serve um arquivo, com suporte a Range. Registra cada requisição."""

    def __init__(self, conteudo, falhar_primeira_em=None):
        self.conteudo = conteudo
        self.falhar_primeira_em = falhar_primeira_em
        self.chamadas = []

    def get(self, url, stream=False, timeout=None, headers=None):
        self.chamadas.append((url, dict(headers or {})))
        inicio = 0
        faixa = (headers or {}).get("Range")
        if faixa:
            inicio = int(faixa.split("=")[1].rstrip("-"))
        if inicio >= len(self.conteudo):
            return RespostaFalsa(b"", status=416)
        resto = self.conteudo[inicio:]
        cab = {"content-length": str(len(resto))}
        status = 200
        if inicio:
            status = 206
            cab["content-range"] = f"bytes {inicio}-{len(self.conteudo) - 1}/{len(self.conteudo)}"
        cai = None
        if self.falhar_primeira_em is not None and len(self.chamadas) == 1:
            cai = self.falhar_primeira_em
        return RespostaFalsa(resto, status=status, headers=cab, cai_depois=cai)


@pytest.fixture(autouse=True)
def sem_espera(monkeypatch):
    monkeypatch.setattr(re_.time, "sleep", lambda s: None)


# ----- leitura do zip -----

def test_ler_emails_zip_acha_so_o_alvo_e_normaliza(tmp_path):
    zip_ = tmp_path / "e.zip"
    zip_.write_bytes(criar_zip([
        linha_estab("09548107", "0001", "43", "  CONTATO@Exemplo.ORG "),
        linha_estab("11111111", "0001", "11", "fora@do-alvo.com"),
        linha_estab("22222222", "0001", "22", ""),  # no alvo, mas sem e-mail
    ]))
    achados, est = re_.ler_emails_zip(zip_, {"09548107000143", "22222222000122"})
    assert achados == {"09548107000143": "contato@exemplo.org"}
    assert est == {"linhas_lidas": 3, "linhas_invalidas": 0}


def test_cnpj_basico_com_zero_a_esquerda_perdido(tmp_path):
    zip_ = tmp_path / "e.zip"
    zip_.write_bytes(criar_zip([linha_estab("548107", "1", "43", "a@b.com")]))
    achados, _ = re_.ler_emails_zip(zip_, {"00548107000143"})
    assert achados == {"00548107000143": "a@b.com"}


def test_poucas_linhas_invalidas_sao_contadas_nao_escondidas(tmp_path):
    linhas = [linha_estab("1", "1", "1", "x@y.com") for _ in range(200)]
    linhas.append(linha_estab("2", "1", "1", colunas=12))  # 1 em 201: abaixo do limite
    zip_ = tmp_path / "e.zip"
    zip_.write_bytes(criar_zip(linhas))
    _, est = re_.ler_emails_zip(zip_, set())
    assert est["linhas_invalidas"] == 1 and est["linhas_lidas"] == 201


def test_layout_diferente_interrompe(tmp_path):
    zip_ = tmp_path / "e.zip"
    zip_.write_bytes(criar_zip([linha_estab("1", "1", "1", colunas=25) for _ in range(10)]))
    with pytest.raises(ColetaError, match="mudado o layout"):
        re_.ler_emails_zip(zip_, set())


def test_arquivo_que_nao_e_zip_interrompe(tmp_path):
    arq = tmp_path / "e.zip"
    arq.write_bytes(b"isto nao e um zip")
    with pytest.raises(ColetaError, match="zip válido"):
        re_.ler_emails_zip(arq, set())


# ----- download -----

def test_download_completo(tmp_path):
    conteudo = criar_zip([linha_estab("1", "1", "1", "a@b.com")])
    servidor = ServidorFalso(conteudo)
    destino = tmp_path / "raw" / "Estabelecimentos0.zip"
    re_.baixar_zip(servidor, "http://x", destino)
    assert destino.read_bytes() == conteudo
    assert not (tmp_path / "raw" / "Estabelecimentos0.zip.part").exists()


def test_download_retoma_depois_de_queda(tmp_path):
    conteudo = criar_zip([linha_estab(str(i), "1", "1", "a@b.com") for i in range(50)])
    servidor = ServidorFalso(conteudo, falhar_primeira_em=len(conteudo) // 2)
    destino = tmp_path / "Estabelecimentos0.zip"
    re_.baixar_zip(servidor, "http://x", destino)
    assert destino.read_bytes() == conteudo
    # A segunda requisição pediu só o que faltava.
    assert len(servidor.chamadas) == 2
    assert servidor.chamadas[1][1]["Range"] == f"bytes={len(conteudo) // 2}-"


def test_download_reaproveita_zip_ja_valido(tmp_path):
    conteudo = criar_zip([linha_estab("1", "1", "1")])
    destino = tmp_path / "Estabelecimentos0.zip"
    destino.write_bytes(conteudo)
    servidor = ServidorFalso(conteudo)
    re_.baixar_zip(servidor, "http://x", destino)
    assert servidor.chamadas == []


def test_download_404_falha_sem_repetir(tmp_path):
    class Sempre404:
        chamadas = 0

        def get(self, *a, **k):
            Sempre404.chamadas += 1
            return RespostaFalsa(b"", status=404)

    with pytest.raises(ColetaError, match="erro 404"):
        re_.baixar_zip(Sempre404(), "http://x", tmp_path / "e.zip")
    assert Sempre404.chamadas == 1


def test_download_de_pagina_html_e_rejeitado(tmp_path):
    servidor = ServidorFalso(b"<html>erro</html>")
    destino = tmp_path / "e.zip"
    with pytest.raises(ColetaError, match="não é um zip"):
        re_.baixar_zip(servidor, "http://x", destino)
    assert not destino.exists()


# ----- descoberta do formato do link -----

def test_descobrir_links_usa_o_primeiro_formato_que_funciona():
    zip_bytes = criar_zip([linha_estab("1", "1", "1")])

    class SoDav:
        def get(self, url, stream=False, timeout=None, headers=None):
            if "/public.php/dav/" in url and "/CNPJ/2026-09/" in url:
                return RespostaFalsa(zip_bytes)
            return RespostaFalsa(b"<html>", status=404)

    links = re_.descobrir_links(SoDav(), "2026-09", ["Estabelecimentos0.zip", "Estabelecimentos1.zip"])
    assert links["Estabelecimentos1.zip"].endswith("/dav/files/" + re_.TOKEN + "/Dados/Cadastros/CNPJ/2026-09/Estabelecimentos1.zip")


def test_descobrir_links_falha_com_lista_de_tentativas():
    class Nada:
        def get(self, *a, **k):
            return RespostaFalsa(b"<html>", status=404)

    with pytest.raises(ColetaError, match="Tentativas"):
        re_.descobrir_links(Nada(), "2026-09", ["Estabelecimentos0.zip"])


# ----- fluxo completo -----

def tabela(cnpjs, emails=None):
    emails = emails or [""] * len(cnpjs)
    return pd.DataFrame({"email": emails, COLUNA_AUXILIAR: cnpjs})


def test_enriquecer_desligado_nao_mexe_na_tabela(tmp_path):
    config = Config(pasta_dados=tmp_path)
    df = tabela(["09548107000143"])
    assert re_.enriquecer_com_emails(None, config, df) is df


def test_fluxo_completo_checkpoint_e_mudanca_do_alvo(tmp_path):
    conteudo = criar_zip([
        linha_estab("09548107", "0001", "43", "Novo@Exemplo.org"),
        linha_estab("04039178", "0001", "05", "santa@casa.org"),
        linha_estab("08563756", "0003", "12", "outro@x.org"),
    ])
    servidor = ServidorFalso(conteudo)
    config = Config(pasta_dados=tmp_path, modo_teste=True, enriquecer_email=True, mes_receita="2026-09", pausa_segundos=0)

    df = tabela(["09548107000143", "04039178000105"], emails=["", "da-api@x.org"])
    saida = re_.enriquecer_com_emails(servidor, config, df)
    # E-mail novo preenchido; e-mail que já veio da API não é sobrescrito.
    assert list(saida["email"]) == ["novo@exemplo.org", "da-api@x.org"]
    assert list(df["email"]) == ["", "da-api@x.org"]  # a tabela original não foi alterada
    assert (config.pasta_zips / "Estabelecimentos0.zip").exists()  # zip guardado

    # Segunda execução com o mesmo alvo: nenhuma requisição nova.
    antes = len(servidor.chamadas)
    re_.enriquecer_com_emails(servidor, config, df)
    assert len(servidor.chamadas) == antes

    # Alvo diferente: o checkpoint não vale, mas o zip guardado evita novo download.
    df2 = tabela(["09548107000143", "08563756000312"])
    saida2 = re_.enriquecer_com_emails(servidor, config, df2)
    assert len(servidor.chamadas) == antes
    assert list(saida2["email"]) == ["novo@exemplo.org", "outro@x.org"]


def test_apagar_zips_remove_o_arquivo_depois_de_processar(tmp_path):
    conteudo = criar_zip([linha_estab("09548107", "0001", "43", "a@b.org")])
    config = Config(pasta_dados=tmp_path, modo_teste=True, enriquecer_email=True,
                    mes_receita="2026-09", apagar_zips=True, pausa_segundos=0)
    re_.enriquecer_com_emails(ServidorFalso(conteudo), config, tabela(["09548107000143"]))
    assert not (config.pasta_zips / "Estabelecimentos0.zip").exists()


def test_config_exige_mes_quando_email_ligado():
    with pytest.raises(ValueError, match="AAAA-MM"):
        Config(enriquecer_email=True)
    with pytest.raises(ValueError, match="AAAA-MM"):
        Config(enriquecer_email=True, mes_receita="09/2026")
    Config(enriquecer_email=True, mes_receita="2026-09")  # válido
