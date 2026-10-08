"""Sessão HTTP compartilhada pelos módulos que acessam a internet."""

import requests

from osc_saude import __version__


def criar_sessao() -> requests.Session:
    """Cria uma sessão com User-Agent identificando o projeto.

    Session reaproveita a conexão entre requisições ao mesmo servidor,
    o que deixa centenas de chamadas seguidas mais rápidas.
    """
    sessao = requests.Session()
    sessao.headers["User-Agent"] = f"osc-saude-cnpj/{__version__}"
    return sessao
