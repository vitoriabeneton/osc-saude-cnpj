"""Tratamento: filtra ativas, remove duplicados e formata campos (antiga célula 5).

Cada filtro conta quantos registros removeu. Esses números entram no README:
mostram o quanto o dado bruto da API precisa de limpeza.
"""

import json
import logging
from collections import Counter
from collections.abc import Iterator
from pathlib import Path

import pandas as pd

from osc_saude.config import Config
from osc_saude.excecoes import ColetaError
from osc_saude.formatadores import (
    codigo,
    formatar_cep,
    formatar_cnpj,
    formatar_data,
    formatar_natureza,
    formatar_telefone,
    montar_endereco,
    texto,
)

logger = logging.getLogger(__name__)

COLUNAS = [
    "cnpj", "razao_social", "nome_fantasia", "matriz_filial", "natureza_juridica",
    "cnae_principal", "cnae_principal_descricao", "origem_cnae", "cnaes_saude_encontrados",
    "uf", "municipio", "codigo_municipio_ibge", "endereco", "bairro", "cep",
    "telefone_1", "telefone_2", "email", "data_inicio_atividade",
]

# Coluna auxiliar (CNPJ só com dígitos), usada para cruzar com o e-mail da Receita.
# Fica fora do CSV final.
COLUNA_AUXILIAR = "_cnpj_numeros"

SITUACAO_ATIVA = "2"

# Nomes dos contadores, na ordem em que os filtros são aplicados.
INATIVO = "inativos"
FORA_NATUREZA = "fora_natureza"
SEM_CNAE_SAUDE = "sem_cnae_saude"
DUPLICADO = "duplicados"


def ler_checkpoint(caminho: Path) -> Iterator[dict]:
    """Lê um arquivo .jsonl, um registro por linha."""
    with caminho.open(encoding="utf-8") as f:
        for numero, linha in enumerate(f, start=1):
            if not linha.strip():
                continue
            try:
                yield json.loads(linha)
            except ValueError as e:
                raise ColetaError(
                    f"Arquivo {caminho}, linha {numero}: conteúdo corrompido.\n"
                    "Apague esse arquivo e rode a coleta de novo."
                ) from e


def cnaes_de_saude(registro: dict, cnaes_saude: dict[str, str]) -> tuple[str, bool, list[str]]:
    """Devolve (CNAE principal, principal é de saúde?, CNAEs de saúde encontrados).

    A lista começa pelo principal (se for de saúde), seguido dos secundários
    de saúde, sem repetir.
    """
    principal = codigo(registro.get("cnae_fiscal"), 7)
    secundarios = [
        codigo(s.get("codigo"), 7)
        for s in (registro.get("cnaes_secundarios") or [])
        if isinstance(s, dict)
    ]
    principal_saude = principal in cnaes_saude
    encontrados = ([principal] if principal_saude else []) + [
        s for s in secundarios if s in cnaes_saude
    ]
    # dict.fromkeys remove repetidos mantendo a ordem.
    return principal, principal_saude, list(dict.fromkeys(encontrados))


def tratar_registro(
    registro: dict,
    cnaes_saude: dict[str, str],
    naturezas_validas: set[str],
    buscar_em_secundario: bool,
) -> tuple[dict | None, str | None]:
    """Aplica os filtros a um registro.

    Devolve (linha, None) se o registro passou, ou (None, motivo) se foi
    descartado. A checagem de CNPJ repetido fica fora, em montar_dataframe,
    porque depende dos registros anteriores.
    """
    # Só situação cadastral ativa.
    if texto(registro.get("situacao_cadastral")) != SITUACAO_ATIVA:
        return None, INATIVO

    # Só as naturezas jurídicas escolhidas na configuração.
    natureza = codigo(registro.get("codigo_natureza_juridica"), 4)
    if natureza not in naturezas_validas:
        return None, FORA_NATUREZA

    principal, principal_saude, encontrados = cnaes_de_saude(registro, cnaes_saude)
    if not encontrados or (not buscar_em_secundario and not principal_saude):
        return None, SEM_CNAE_SAUDE

    matriz_filial = texto(registro.get("identificador_matriz_filial"))
    cnpj14 = codigo(registro.get("cnpj"), 14)
    linha = {
        "cnpj": formatar_cnpj(cnpj14),
        "razao_social": texto(registro.get("razao_social")),
        "nome_fantasia": texto(registro.get("nome_fantasia")),
        "matriz_filial": {"1": "Matriz", "2": "Filial"}.get(matriz_filial, matriz_filial),
        "natureza_juridica": formatar_natureza(natureza, registro.get("natureza_juridica")),
        "cnae_principal": principal,
        "cnae_principal_descricao": texto(registro.get("cnae_fiscal_descricao")),
        "origem_cnae": "principal" if principal_saude else "secundario",
        "cnaes_saude_encontrados": "|".join(encontrados),
        "uf": texto(registro.get("uf")),
        "municipio": texto(registro.get("municipio")),
        "codigo_municipio_ibge": texto(registro.get("codigo_municipio_ibge")),
        "endereco": montar_endereco(registro),
        "bairro": texto(registro.get("bairro")),
        "cep": formatar_cep(registro.get("cep")),
        "telefone_1": formatar_telefone(registro.get("ddd_telefone_1")),
        "telefone_2": formatar_telefone(registro.get("ddd_telefone_2")),
        # Quando a API traz e-mail, aproveita. Quando não traz (o caso comum),
        # a etapa opcional da Receita Federal preenche depois.
        "email": texto(registro.get("email")).lower(),
        "data_inicio_atividade": formatar_data(registro.get("data_inicio_atividade")),
        COLUNA_AUXILIAR: cnpj14,
    }
    return linha, None


def montar_dataframe(config: Config, cnaes_saude: dict[str, str]) -> tuple[pd.DataFrame, Counter]:
    """Lê os checkpoints das UFs da configuração e devolve (tabela, contagem dos filtros)."""
    naturezas_validas = {codigo(n, 4) for n in config.naturezas}
    linhas: list[dict] = []
    vistos: set[str] = set()
    contagem: Counter = Counter()

    for uf in sorted(config.ufs_da_vez):
        caminho = config.pasta_checkpoints / f"{uf}.jsonl"
        if not caminho.exists():
            # Resultado parcial sem aviso seria pior que parar.
            raise ColetaError(f"UF {uf}: checkpoint não encontrado em {caminho}. Rode a coleta antes.")

        for registro in ler_checkpoint(caminho):
            contagem["lidos"] += 1
            linha, motivo = tratar_registro(
                registro, cnaes_saude, naturezas_validas, config.buscar_em_secundario
            )
            if motivo:
                contagem[motivo] += 1
                continue
            if linha[COLUNA_AUXILIAR] in vistos:
                contagem[DUPLICADO] += 1
                continue
            vistos.add(linha[COLUNA_AUXILIAR])
            linhas.append(linha)

    df = pd.DataFrame(linhas, columns=COLUNAS + [COLUNA_AUXILIAR])
    contagem["resultado"] = len(df)

    logger.info("Registros lidos:               %d", contagem["lidos"])
    logger.info("Removidos (não ativos):        %d", contagem[INATIVO])
    logger.info("Removidos (natureza fora):     %d", contagem[FORA_NATUREZA])
    logger.info("Removidos (sem CNAE de saúde): %d", contagem[SEM_CNAE_SAUDE])
    logger.info("Removidos (CNPJ repetido):     %d", contagem[DUPLICADO])
    logger.info("Estabelecimentos no resultado: %d", len(df))

    if df.empty:
        raise ColetaError("Nenhum estabelecimento passou pelos filtros. Confira config.py e os checkpoints.")
    return df, contagem
