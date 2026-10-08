"""Lista oficial de CNAEs de saúde, buscada na API do IBGE (antiga célula 3)."""

import logging
import time

import requests

from osc_saude.excecoes import ColetaError
from osc_saude.formatadores import codigo, texto

logger = logging.getLogger(__name__)

URL_IBGE = "https://servicodados.ibge.gov.br/api/v2/cnae"


def urls_cnae_saude(grupos_extra: tuple[str, ...]) -> list[str]:
    """Divisão 86 (atenção à saúde humana) mais os grupos extras."""
    return [f"{URL_IBGE}/divisoes/86/subclasses"] + [
        f"{URL_IBGE}/grupos/{g}/subclasses" for g in grupos_extra
    ]


def carregar_cnaes_saude(
    sessao: requests.Session, grupos_extra: tuple[str, ...], pausa: float = 1.0
) -> dict[str, str]:
    """Devolve {código de 7 dígitos: descrição} de todas as subclasses de saúde.

    Buscar no IBGE em vez de digitar a lista à mão garante que ela
    acompanha revisões oficiais da CNAE.
    """
    cnaes: dict[str, str] = {}
    for url in urls_cnae_saude(grupos_extra):
        try:
            r = sessao.get(url, timeout=60)
            r.raise_for_status()
            itens = r.json()
        except (requests.RequestException, ValueError) as e:
            # ValueError cobre resposta que não é JSON válido.
            raise ColetaError(
                f"Não consegui ler a lista de CNAEs do IBGE.\nEndereço: {url}\nDetalhe: {e}"
            ) from e

        if not isinstance(itens, list) or not itens:
            raise ColetaError(
                f"O IBGE não retornou subclasses para este endereço:\n{url}\n"
                "Confira os códigos em GRUPOS_CNAE_EXTRA, em config.py."
            )

        for item in itens:
            if not isinstance(item, dict):
                continue
            cod = codigo(item.get("id"), 7)
            if cod:
                cnaes[cod] = texto(item.get("descricao"))

        time.sleep(pausa)

    logger.info("%d CNAEs de saúde carregados do IBGE.", len(cnaes))
    return cnaes
