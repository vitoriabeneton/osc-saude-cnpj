"""Testes do tratamento. Registros sintéticos, no formato da Minha Receita."""

import json

import pytest

from osc_saude.config import Config
from osc_saude.excecoes import ColetaError
from osc_saude.tratamento import (
    COLUNA_AUXILIAR,
    COLUNAS,
    cnaes_de_saude,
    montar_dataframe,
    tratar_registro,
)

CNAES = {"8610101": "Hospital", "8630501": "Ambulatório", "8711502": "Idosos"}
NATUREZAS = {"3999", "3069"}


def registro(**mudancas):
    """Registro ativo, de natureza válida, com CNAE principal de saúde."""
    base = {
        "cnpj": "09548107000143",
        "situacao_cadastral": 2,
        "codigo_natureza_juridica": 3999,
        "natureza_juridica": "Associação Privada",
        "cnae_fiscal": 8610101,
        "cnae_fiscal_descricao": "Hospital",
        "cnaes_secundarios": [],
        "razao_social": "ASSOCIACAO TESTE",
        "identificador_matriz_filial": 1,
        "uf": "AC",
        "ddd_telefone_1": "6833421176",
        "cep": 69985000,
        "data_inicio_atividade": "2008-04-09",
        "email": None,
    }
    base.update(mudancas)
    return base


def test_registro_valido_vira_linha_formatada():
    linha, motivo = tratar_registro(registro(), CNAES, NATUREZAS, True)
    assert motivo is None
    assert linha["cnpj"] == "09.548.107/0001-43"
    assert linha["natureza_juridica"] == "399-9 - Associação Privada"
    assert linha["matriz_filial"] == "Matriz"
    assert linha["telefone_1"] == "(68) 3342-1176"
    assert linha["cep"] == "69985-000"
    assert linha["origem_cnae"] == "principal"
    assert linha["email"] == ""
    assert set(COLUNAS) <= set(linha) and linha[COLUNA_AUXILIAR] == "09548107000143"


@pytest.mark.parametrize(
    "mudancas, motivo_esperado",
    [
        ({"situacao_cadastral": 8}, "inativos"),
        ({"situacao_cadastral": 3}, "inativos"),
        ({"codigo_natureza_juridica": 2062}, "fora_natureza"),
        ({"cnae_fiscal": 4711302}, "sem_cnae_saude"),
    ],
)
def test_motivos_de_descarte(mudancas, motivo_esperado):
    linha, motivo = tratar_registro(registro(**mudancas), CNAES, NATUREZAS, True)
    assert linha is None
    assert motivo == motivo_esperado


def test_cnae_so_no_secundario():
    reg = registro(cnae_fiscal=9499500, cnaes_secundarios=[{"codigo": 8630501, "descricao": "x"}])
    # Buscando também no secundário: entra, com a origem registrada.
    linha, motivo = tratar_registro(reg, CNAES, NATUREZAS, True)
    assert motivo is None and linha["origem_cnae"] == "secundario"
    # Só no principal: descartado.
    linha, motivo = tratar_registro(reg, CNAES, NATUREZAS, False)
    assert linha is None and motivo == "sem_cnae_saude"


def test_cnaes_de_saude_sem_repetir_e_principal_primeiro():
    reg = registro(cnaes_secundarios=[
        {"codigo": 8630501}, {"codigo": 8610101}, {"codigo": 8630501}, {"codigo": 1111111},
    ])
    principal, principal_saude, achados = cnaes_de_saude(reg, CNAES)
    assert principal == "8610101" and principal_saude
    assert achados == ["8610101", "8630501"]


def test_email_da_api_e_aproveitado_em_minusculas():
    linha, _ = tratar_registro(registro(email="  CONTATO@Exemplo.ORG "), CNAES, NATUREZAS, True)
    assert linha["email"] == "contato@exemplo.org"


def gravar_checkpoint(config, uf, registros):
    pasta = config.pasta_checkpoints
    pasta.mkdir(parents=True, exist_ok=True)
    (pasta / f"{uf}.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in registros), encoding="utf-8"
    )


def test_montar_dataframe_conta_cada_filtro(tmp_path):
    config = Config(ufs=("AC",), pasta_dados=tmp_path)
    gravar_checkpoint(config, "AC", [
        registro(),
        registro(),  # CNPJ repetido
        registro(cnpj="11111111000111", situacao_cadastral=8),
        registro(cnpj="22222222000122", codigo_natureza_juridica=2062),
        registro(cnpj="33333333000133", cnae_fiscal=4711302),
        registro(cnpj="44444444000144", cnae_fiscal=8711502),
    ])
    df, contagem = montar_dataframe(config, CNAES)

    assert contagem["lidos"] == 6
    assert contagem["inativos"] == 1
    assert contagem["fora_natureza"] == 1
    assert contagem["sem_cnae_saude"] == 1
    assert contagem["duplicados"] == 1
    assert contagem["resultado"] == len(df) == 2
    # As contas fecham: lidos = removidos + resultado.
    removidos = sum(contagem[k] for k in ("inativos", "fora_natureza", "sem_cnae_saude", "duplicados"))
    assert contagem["lidos"] == removidos + len(df)


def test_checkpoint_ausente_interrompe(tmp_path):
    config = Config(ufs=("AC",), pasta_dados=tmp_path)
    with pytest.raises(ColetaError, match="checkpoint não encontrado"):
        montar_dataframe(config, CNAES)


def test_checkpoint_corrompido_interrompe(tmp_path):
    config = Config(ufs=("AC",), pasta_dados=tmp_path)
    config.pasta_checkpoints.mkdir(parents=True)
    (config.pasta_checkpoints / "AC.jsonl").write_text("{quebrado\n", encoding="utf-8")
    with pytest.raises(ColetaError, match="corrompido"):
        montar_dataframe(config, CNAES)


def test_resultado_vazio_interrompe(tmp_path):
    config = Config(ufs=("AC",), pasta_dados=tmp_path)
    gravar_checkpoint(config, "AC", [registro(situacao_cadastral=8)])
    with pytest.raises(ColetaError, match="Nenhum estabelecimento"):
        montar_dataframe(config, CNAES)
