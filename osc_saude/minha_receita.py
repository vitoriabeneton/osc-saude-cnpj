"""Coleta na API Minha Receita, uma UF por vez, com checkpoint por UF (antiga célula 4).

Decisão consciente: o checkpoint é por UF, não por página. Se a coleta cair
no meio de uma UF, essa UF recomeça do zero. Em troca, um arquivo de UF nunca
fica pela metade, e o código fica bem mais simples. Como a maior UF leva
poucos minutos, a perda máxima é pequena.
"""

import json
import logging
import os
import time
from pathlib import Path
from urllib.parse import quote

import requests
from tqdm.auto import tqdm

from osc_saude.config import Config
from osc_saude.excecoes import ColetaError
from osc_saude.formatadores import codigo

logger = logging.getLogger(__name__)

URL_MINHA_RECEITA = "https://minhareceita.org/"
TIMEOUT_SEGUNDOS = 120
ESPERAS_NOVA_TENTATIVA = (5, 15, 45)  # espera crescente entre tentativas

# Campos removidos antes de gravar. "qsa" traz nome e faixa etária dos
# dirigentes; nenhum dos dois é usado no resultado. Guardar só o necessário
# reduz o tamanho dos arquivos e o volume de dado pessoal armazenado (LGPD).
CAMPOS_DESCARTADOS = frozenset({"qsa", "regime_tributario"})


def montar_url_base(uf: str, naturezas: list[str], campo_cnae: str, cnaes: list[str]) -> str:
    """Vários valores no mesmo parâmetro vão separados por vírgula.

    A URL é montada à mão, e não pelo params= do requests, porque o requests
    codificaria as vírgulas como %2C. Este formato é o que já foi validado na API.
    """
    return (
        f"{URL_MINHA_RECEITA}?natureza_juridica={','.join(naturezas)}"
        f"&{campo_cnae}={','.join(cnaes)}&uf={uf}&limit=1024"
    )


def buscar_pagina(
    sessao: requests.Session,
    url: str,
    contexto: str,
    esperas: tuple[int, ...] = ESPERAS_NOVA_TENTATIVA,
) -> dict:
    """Faz a requisição e devolve o JSON. Tenta de novo só em falhas temporárias.

    Temporárias (vale tentar de novo): tempo esgotado, falha de conexão,
    429 (muitas requisições) e 5xx (erro do servidor).
    Definitivas (falha na hora): 400 e demais 4xx, porque repetir a mesma
    consulta errada daria o mesmo erro.
    """
    motivo = ""
    for tentativa in range(len(esperas) + 1):
        try:
            r = sessao.get(url, timeout=TIMEOUT_SEGUNDOS)
        except requests.exceptions.Timeout:
            motivo = "tempo de resposta esgotado"
        except requests.exceptions.ConnectionError:
            motivo = "falha de conexão"
        else:
            if r.status_code == 200:
                try:
                    resposta = r.json()
                except ValueError as e:
                    raise ColetaError(f"{contexto}: a resposta não veio em JSON.\nURL: {url}") from e
                if not isinstance(resposta, dict):
                    raise ColetaError(f"{contexto}: resposta em formato inesperado.\nURL: {url}")
                return resposta
            if r.status_code == 404:
                logger.info("%s: a API respondeu 404 (sem resultados).", contexto)
                return {"data": []}
            if r.status_code == 429 or r.status_code >= 500:
                motivo = f"erro {r.status_code} no servidor"
            else:
                raise ColetaError(
                    f"{contexto}: a API recusou a consulta (erro {r.status_code}).\n"
                    f"URL: {url}\nMensagem da API: {r.text[:500]}"
                )

        if tentativa < len(esperas):
            logger.warning("%s: %s. Nova tentativa em %d s.", contexto, motivo, esperas[tentativa])
            time.sleep(esperas[tentativa])

    raise ColetaError(
        f"{contexto}: falhou após {len(esperas)} novas tentativas ({motivo}).\n"
        "As UFs já concluídas estão salvas. Rode de novo mais tarde para continuar."
    )


def limpar_registro(registro: dict) -> dict:
    return {k: v for k, v in registro.items() if k not in CAMPOS_DESCARTADOS}


def coletar_uf(
    sessao: requests.Session,
    url_base: str,
    uf: str,
    pausa: float = 1.0,
    max_paginas: int | None = None,
) -> list[dict]:
    """Percorre todas as páginas da UF seguindo o cursor devolvido pela API."""
    registros: list[dict] = []
    cursor = None
    cursores_vistos: set[str] = set()
    pagina = 1

    while True:
        # O cursor é repetido exatamente como veio, só codificado para a URL.
        url = url_base if cursor is None else f"{url_base}&cursor={quote(str(cursor), safe='')}"
        resposta = buscar_pagina(sessao, url, f"UF {uf}, página {pagina}")
        dados = resposta.get("data") or []
        registros.extend(limpar_registro(r) for r in dados if isinstance(r, dict))
        cursor = resposta.get("cursor")

        # Sem cursor ou página vazia: acabou.
        if not cursor or not dados:
            break
        # Proteção contra laço infinito caso a API devolva o mesmo cursor.
        if str(cursor) in cursores_vistos:
            raise ColetaError(f"UF {uf}, página {pagina}: a API repetiu um cursor. Coleta interrompida.")
        cursores_vistos.add(str(cursor))

        if max_paginas is not None and pagina >= max_paginas:
            logger.info("UF %s: limite de %d páginas (modo teste).", uf, max_paginas)
            break
        pagina += 1
        time.sleep(pausa)

    return registros


def gravar_jsonl_atomico(caminho: Path, registros: list[dict]) -> None:
    """Grava num .tmp e renomeia no fim.

    Se o programa cair durante a gravação, sobra só o .tmp, e a UF
    não é tomada como concluída na próxima execução.
    """
    temporario = caminho.with_name(caminho.name + ".tmp")
    with temporario.open("w", encoding="utf-8") as f:
        for reg in registros:
            f.write(json.dumps(reg, ensure_ascii=False) + "\n")
    os.replace(temporario, caminho)


def contar_linhas(caminho: Path) -> int:
    with caminho.open(encoding="utf-8") as f:
        return sum(1 for _ in f)


def verificar_filtros(pasta: Path, filtros: dict) -> None:
    """Impede misturar checkpoints coletados com filtros diferentes.

    Na primeira execução grava os filtros em _filtros.json. Nas seguintes,
    compara: se mudaram, os arquivos de UF já salvos não valem mais.
    """
    pasta.mkdir(parents=True, exist_ok=True)
    arquivo = pasta / "_filtros.json"
    if arquivo.exists():
        with arquivo.open(encoding="utf-8") as f:
            if json.load(f) != filtros:
                raise ColetaError(
                    "Os filtros mudaram desde a última coleta.\n"
                    f"Apague a pasta {pasta} e rode de novo."
                )
        return
    with arquivo.open("w", encoding="utf-8") as f:
        json.dump(filtros, f, ensure_ascii=False, indent=1)


def coletar(sessao: requests.Session, config: Config, cnaes: dict[str, str]) -> dict[str, int]:
    """Coleta todas as UFs da configuração. Devolve {UF: quantidade de registros}."""
    naturezas = sorted(codigo(n, 4) for n in config.naturezas)
    lista_cnaes = sorted(cnaes)
    filtros = {"natureza_juridica": naturezas, "campo_cnae": config.campo_cnae, "cnaes": lista_cnaes}

    pasta = config.pasta_checkpoints
    verificar_filtros(pasta, filtros)

    totais: dict[str, int] = {}
    for uf in tqdm(config.ufs_da_vez, desc="UFs", unit="UF"):
        destino = pasta / f"{uf}.jsonl"
        if destino.exists():
            totais[uf] = contar_linhas(destino)
            logger.info("%s: já coletada (%d registros). Pulando.", uf, totais[uf])
            continue

        url_base = montar_url_base(uf, naturezas, config.campo_cnae, lista_cnaes)
        registros = coletar_uf(sessao, url_base, uf, config.pausa_segundos, config.max_paginas)
        gravar_jsonl_atomico(destino, registros)

        totais[uf] = len(registros)
        logger.info("%s: %d registros.", uf, len(registros))
        time.sleep(config.pausa_segundos)

    logger.info("Total geral: %d registros em %d UFs.", sum(totais.values()), len(totais))
    return totais
