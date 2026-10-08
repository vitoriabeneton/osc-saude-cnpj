"""E-mail a partir dos arquivos Estabelecimentos da Receita Federal (antiga célula 6).

A API Minha Receita devolve o campo email vazio, então o e-mail só vem destes
arquivos: 10 zips de cerca de 2 GB cada. A etapa é opcional (config.enriquecer_email).
"""

import csv
import hashlib
import io
import json
import logging
import os
import time
import zipfile
from pathlib import Path

import pandas as pd
import requests
from tqdm.auto import tqdm

from osc_saude.config import Config
from osc_saude.excecoes import ColetaError
from osc_saude.tratamento import COLUNA_AUXILIAR

logger = logging.getLogger(__name__)

# Link público de compartilhamento da Receita Federal (dados abertos de CNPJ).
HOST = "arquivos.receitafederal.gov.br"
TOKEN = "gn672Ad4CF8N6TK"
QUANTIDADE_ARQUIVOS = 10

ESPERAS_DOWNLOAD = (10, 30, 60, 120, 120, 180, 180, 300)

# Layout do arquivo Estabelecimentos: 30 colunas separadas por ";".
# Posições usadas: 0 a 2 (partes do CNPJ) e 27 (correio eletrônico).
NUM_COLUNAS = 30
COL_EMAIL = 27
# Mais que isso de linhas fora do layout indica que a Receita mudou o formato.
LIMITE_LINHAS_INVALIDAS = 0.01

# Formatos de link que o site da Receita já usou. Testados em ordem por descobrir_links.
MODELOS_LINK = {
    "download": lambda pasta, arq: f"https://{HOST}/index.php/s/{TOKEN}/download?path={pasta}&files={arq}",
    "dav": lambda pasta, arq: f"https://{HOST}/public.php/dav/files/{TOKEN}{pasta}/{arq}",
    "webdav": lambda pasta, arq: f"https://{TOKEN}:@{HOST}/public.php/webdav{pasta}/{arq}",
}


def nomes_dos_arquivos(modo_teste: bool) -> list[str]:
    nomes = [f"Estabelecimentos{i}.zip" for i in range(QUANTIDADE_ARQUIVOS)]
    return nomes[:1] if modo_teste else nomes


def descobrir_links(sessao: requests.Session, mes: str, nomes: list[str], pausa: float = 1.0) -> dict[str, str]:
    """Descobre qual formato de link funciona e devolve {nome do arquivo: link}.

    No notebook, uma célula de teste sobrescrevia a lista de links por baixo
    dos panos. Aqui a descoberta é uma função chamada de forma explícita,
    e só roda se algum zip realmente precisar ser baixado.
    O teste baixa só 4 bytes: um zip começa com "PK".
    """
    pastas = [f"/Dados/Cadastros/CNPJ/{mes}", f"/Dados/Cadastros/{mes}"]
    tentativas = []
    for pasta in pastas:
        for nome_modelo, montar in MODELOS_LINK.items():
            url = montar(pasta, nomes[0])
            try:
                with sessao.get(url, stream=True, timeout=60) as r:
                    inicio = next(r.iter_content(4), b"")
                    ok = r.status_code == 200 and inicio[:2] == b"PK"
                    tentativas.append(f"{nome_modelo} {pasta}: status {r.status_code}")
            except requests.RequestException as e:
                ok = False
                tentativas.append(f"{nome_modelo} {pasta}: {type(e).__name__}")
            if ok:
                logger.info("Formato de link encontrado: %s (%s).", nome_modelo, pasta)
                return {n: montar(pasta, n) for n in nomes}
            time.sleep(pausa)

    raise ColetaError(
        "Nenhum formato de link funcionou para os arquivos da Receita.\n"
        f"Confira se a pasta {mes} existe no site da Receita. Tentativas:\n  " + "\n  ".join(tentativas)
    )


def zip_valido(caminho: Path) -> bool:
    return caminho.exists() and zipfile.is_zipfile(caminho)


def _tentar_baixar(sessao: requests.Session, link: str, parcial: Path) -> bool:
    """Uma tentativa de download, retomando do que já existe em .part.

    Devolve True se o arquivo está completo, False se veio incompleto.
    Falhas de rede saem como exceção, tratadas por quem chama.
    """
    ja_baixado = parcial.stat().st_size if parcial.exists() else 0
    cabecalhos = {"Range": f"bytes={ja_baixado}-"} if ja_baixado else {}

    with sessao.get(link, stream=True, timeout=(30, 300), headers=cabecalhos) as r:
        if r.status_code == 416:  # Nada mais a baixar.
            return True
        r.raise_for_status()
        if r.status_code == 200 and ja_baixado:
            ja_baixado = 0  # O servidor ignorou o pedido de retomada: recomeça do zero.

        total = None
        faixa = r.headers.get("content-range", "")
        if "/" in faixa and faixa.split("/")[-1].isdigit():
            total = int(faixa.split("/")[-1])
        elif r.headers.get("content-length", "").isdigit():
            total = int(r.headers["content-length"]) + ja_baixado

        modo = "ab" if ja_baixado else "wb"
        with parcial.open(modo) as f, tqdm(
            total=total, initial=ja_baixado, unit="B", unit_scale=True, desc=f"Baixando {parcial.stem}"
        ) as barra:
            for bloco in r.iter_content(chunk_size=1024 * 1024):
                f.write(bloco)
                barra.update(len(bloco))

    return total is None or parcial.stat().st_size >= total


def baixar_zip(
    sessao: requests.Session, link: str, destino: Path, esperas: tuple[int, ...] = ESPERAS_DOWNLOAD
) -> None:
    """Baixa por streaming e retoma de onde parou se a conexão cair.

    Retomar importa aqui: perder 1,9 GB de um arquivo de 2,2 GB por uma queda
    de rede custaria quase uma hora de novo.
    """
    destino.parent.mkdir(parents=True, exist_ok=True)
    nome = destino.name
    parcial = destino.with_name(nome + ".part")

    if destino.exists():
        if zipfile.is_zipfile(destino):
            logger.info("%s: já baixado. Reaproveitando.", nome)
            return
        destino.unlink()

    ultimo_erro = ""
    for tentativa in range(len(esperas) + 1):
        try:
            if _tentar_baixar(sessao, link, parcial):
                break
            ultimo_erro = "download incompleto"
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else 0
            # 4xx é falha definitiva (link errado), exceto 429, que é "muitas requisições".
            if status < 500 and status != 429:
                raise ColetaError(f"Falha ao baixar {nome} (erro {status}).\nLink: {link}") from e
            ultimo_erro = f"erro {status} no servidor"
        except requests.exceptions.RequestException as e:
            ultimo_erro = f"conexão interrompida ({type(e).__name__})"

        if tentativa < len(esperas):
            gb = (parcial.stat().st_size if parcial.exists() else 0) / 1e9
            logger.warning("%s: %s. %.2f GB já baixados. Retomando em %d s.", nome, ultimo_erro, gb, esperas[tentativa])
            time.sleep(esperas[tentativa])
    else:
        raise ColetaError(
            f"Falha ao baixar {nome} depois de várias tentativas ({ultimo_erro}).\n"
            "O que já foi baixado está guardado. Rode de novo para continuar."
        )

    os.replace(parcial, destino)
    if not zipfile.is_zipfile(destino):
        destino.unlink()
        raise ColetaError(f"{nome} baixou, mas não é um zip válido. Rode de novo para baixar outra vez.")


def ler_emails_zip(caminho: Path, alvo: set[str]) -> tuple[dict[str, str], dict[str, int]]:
    """Lê o CSV direto do zip, sem descompactar, e devolve ({CNPJ: e-mail}, estatísticas).

    Usa o módulo csv em vez do pandas porque assim cada linha fora do layout
    pode ser contada. O notebook usava on_bad_lines="skip", que descartava
    linhas em silêncio: o e-mail de um CNPJ procurado podia sumir sem aviso.
    """
    nome = caminho.name
    achados: dict[str, str] = {}
    lidas = invalidas = 0
    try:
        with zipfile.ZipFile(caminho) as z:
            membros = [m for m in z.namelist() if not m.endswith("/")]
            if not membros:
                raise ColetaError(f"O arquivo {nome} está vazio.")
            with z.open(membros[0]) as bruto:
                # newline="" é exigido pelo módulo csv para tratar quebras de linha dentro de campos.
                texto = io.TextIOWrapper(bruto, encoding="latin-1", newline="")
                leitor = csv.reader(texto, delimiter=";", quotechar='"')
                for linha in tqdm(leitor, desc=f"Lendo {nome}", unit=" linhas", unit_scale=True, mininterval=2):
                    lidas += 1
                    if len(linha) != NUM_COLUNAS:
                        invalidas += 1
                        continue
                    cnpj = linha[0].zfill(8) + linha[1].zfill(4) + linha[2].zfill(2)
                    if cnpj in alvo:
                        email = linha[COL_EMAIL].strip().lower()
                        if email:
                            achados[cnpj] = email
    except zipfile.BadZipFile as e:
        raise ColetaError(f"{nome} não é um zip válido ou está corrompido.") from e
    except (csv.Error, UnicodeError) as e:
        raise ColetaError(f"Falha ao ler {nome}: {e}") from e

    if lidas == 0:
        raise ColetaError(f"O arquivo {nome} não tem linhas.")
    if invalidas / lidas > LIMITE_LINHAS_INVALIDAS:
        raise ColetaError(
            f"{nome}: {invalidas} de {lidas} linhas não têm {NUM_COLUNAS} colunas.\n"
            "A Receita pode ter mudado o layout do arquivo. Confira antes de usar os e-mails."
        )
    if invalidas:
        logger.warning("%s: %d de %d linhas fora do layout foram ignoradas.", nome, invalidas, lidas)
    return achados, {"linhas_lidas": lidas, "linhas_invalidas": invalidas}


def assinatura_do_alvo(alvo: set[str]) -> str:
    """Resumo único do conjunto de CNPJs procurados."""
    return hashlib.sha1("\n".join(sorted(alvo)).encode()).hexdigest()


def ler_checkpoint_email(caminho: Path, assinatura: str) -> dict[str, str] | None:
    """Devolve os e-mails salvos, ou None se o checkpoint não existe ou não vale mais.

    O checkpoint guarda só os e-mails dos CNPJs procurados naquele momento.
    Se o conjunto mudou (por exemplo, mais UFs coletadas), reaproveitá-lo
    faria faltar e-mail dos CNPJs novos, sem nenhum aviso.
    """
    if not caminho.exists():
        return None
    try:
        with caminho.open(encoding="utf-8") as f:
            dados = json.load(f)
    except ValueError:
        logger.warning("%s está corrompido. O arquivo será processado de novo.", caminho.name)
        return None
    if dados.get("assinatura_alvo") != assinatura:
        logger.info("%s: a lista de CNPJs mudou. O arquivo será processado de novo.", caminho.name)
        return None
    return dados.get("emails", {})


def gravar_checkpoint_email(caminho: Path, assinatura: str, emails: dict[str, str], estatisticas: dict) -> None:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    temporario = caminho.with_name(caminho.name + ".tmp")
    conteudo = {"assinatura_alvo": assinatura, **estatisticas, "emails": emails}
    with temporario.open("w", encoding="utf-8") as f:
        json.dump(conteudo, f, ensure_ascii=False)
    os.replace(temporario, caminho)


def enriquecer_com_emails(sessao: requests.Session, config: Config, df: pd.DataFrame) -> pd.DataFrame:
    """Preenche a coluna email com os dados da Receita. Devolve uma cópia da tabela.

    Para cada arquivo: usa o checkpoint se valer, senão usa o zip já baixado,
    senão baixa. E-mail que já veio da API não é sobrescrito.
    """
    if not config.enriquecer_email:
        logger.info("Etapa de e-mail desligada. A coluna email fica com o que a API trouxe.")
        return df

    alvo = set(df[COLUNA_AUXILIAR])
    assinatura = assinatura_do_alvo(alvo)
    pasta_emails = config.pasta_checkpoints / "emails"
    nomes = nomes_dos_arquivos(config.modo_teste)
    links: dict[str, str] | None = None
    emails_total: dict[str, str] = {}

    for nome in nomes:
        checkpoint = pasta_emails / f"{nome}.json"
        achados = ler_checkpoint_email(checkpoint, assinatura)

        if achados is None:
            zip_local = config.pasta_zips / nome
            if not zip_valido(zip_local):
                if links is None:
                    links = descobrir_links(sessao, config.mes_receita, nomes, config.pausa_segundos)
                baixar_zip(sessao, links[nome], zip_local)
            achados, estatisticas = ler_emails_zip(zip_local, alvo)
            gravar_checkpoint_email(checkpoint, assinatura, achados, estatisticas)
            if config.apagar_zips:
                zip_local.unlink()
            logger.info("%s: %d e-mails encontrados.", nome, len(achados))
        else:
            logger.info("%s: já processado (%d e-mails). Pulando.", nome, len(achados))

        emails_total.update(achados)

    resultado = df.copy()
    da_receita = resultado[COLUNA_AUXILIAR].map(emails_total).fillna("")
    resultado["email"] = resultado["email"].where(resultado["email"] != "", da_receita)
    logger.info("Estabelecimentos com e-mail: %d de %d.", (resultado["email"] != "").sum(), len(resultado))
    return resultado
