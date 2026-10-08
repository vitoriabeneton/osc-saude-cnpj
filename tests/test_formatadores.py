"""Testes das funções de formatação.

Os casos marcados "real" saíram do checkpoint AC.jsonl coletado na Minha Receita.
"""

import pytest

from osc_saude.formatadores import (
    codigo,
    formatar_cep,
    formatar_cnpj,
    formatar_data,
    formatar_natureza,
    formatar_telefone,
    montar_endereco,
    somente_digitos,
)


@pytest.mark.parametrize(
    "entrada, esperado",
    [
        (None, ""),
        ("12.345-678", "12345678"),
        (8610101, "8610101"),
        (8610101.0, "8610101"),  # float inteiro não pode ganhar dígito extra
        ("abc", ""),
    ],
)
def test_somente_digitos(entrada, esperado):
    assert somente_digitos(entrada) == esperado


@pytest.mark.parametrize(
    "entrada, digitos, esperado",
    [
        (659, 4, "0659"),
        ("3999", 4, "3999"),
        (None, 7, ""),
        ("", 4, ""),
    ],
)
def test_codigo(entrada, digitos, esperado):
    assert codigo(entrada, digitos) == esperado


@pytest.mark.parametrize(
    "entrada, esperado",
    [
        ("09548107000143", "09.548.107/0001-43"),  # real
        (9548107000143, "09.548.107/0001-43"),  # zero inicial perdido ao virar número
        (None, ""),
    ],
)
def test_formatar_cnpj(entrada, esperado):
    assert formatar_cnpj(entrada) == esperado


@pytest.mark.parametrize(
    "entrada, esperado",
    [
        ("6833421176", "(68) 3342-1176"),  # real, fixo
        ("68999316197", "(68) 99931-6197"),  # celular
        ("06830262165", "(68) 3026-2165"),  # real, zero de discagem antes do DDD
        ("682233568", "682233568"),  # real, incompleto: mantém dígitos
        ("", ""),
        (None, ""),
    ],
)
def test_formatar_telefone(entrada, esperado):
    assert formatar_telefone(entrada) == esperado


@pytest.mark.parametrize(
    "entrada, esperado",
    [
        ("69985000", "69985-000"),  # real
        (1310100, "01310-100"),  # zero inicial perdido ao virar número
        ("123456789", ""),
        (None, ""),
    ],
)
def test_formatar_cep(entrada, esperado):
    assert formatar_cep(entrada) == esperado


@pytest.mark.parametrize(
    "entrada, esperado",
    [
        ("2008-04-09", "09/04/2008"),  # real
        ("2008-04-09T00:00:00", "09/04/2008"),
        ("2026-13-45", ""),
        (None, ""),
    ],
)
def test_formatar_data(entrada, esperado):
    assert formatar_data(entrada) == esperado


@pytest.mark.parametrize(
    "codigo_natureza, descricao, esperado",
    [
        (3999, "Associação Privada", "399-9 - Associação Privada"),  # real
        ("3069", "", "306-9"),
        (None, "Qualquer", ""),
    ],
)
def test_formatar_natureza(codigo_natureza, descricao, esperado):
    assert formatar_natureza(codigo_natureza, descricao) == esperado


@pytest.mark.parametrize(
    "registro, esperado",
    [
        (  # real
            {"descricao_tipo_de_logradouro": "AVENIDA", "logradouro": "PRESIDENTE JUCELINO",
             "numero": "0605", "complemento": ""},
            "AVENIDA PRESIDENTE JUCELINO, 0605",
        ),
        (  # real, vírgula sobrando no logradouro
            {"descricao_tipo_de_logradouro": "AVENIDA", "logradouro": "GETULIO VARGAS,",
             "numero": "130", "complemento": "SALA/209"},
            "AVENIDA GETULIO VARGAS, 130 - SALA/209",
        ),
        ({}, ""),
    ],
)
def test_montar_endereco(registro, esperado):
    assert montar_endereco(registro) == esperado
