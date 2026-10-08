"""Ponto de entrada: encadeia coleta, tratamento, e-mail opcional e exportação.

Uso (na raiz do projeto, com o ambiente virtual ativo):

    python -m osc_saude.pipeline --teste
    python -m osc_saude.pipeline
    python -m osc_saude.pipeline --com-email --mes-receita 2026-09
    python -m osc_saude.pipeline --help
"""

import argparse
import logging
import os
import sys
from collections import Counter
from datetime import date
from pathlib import Path

import pandas as pd

from osc_saude.config import Config
from osc_saude.excecoes import ColetaError
from osc_saude.ibge import carregar_cnaes_saude
from osc_saude.minha_receita import coletar
from osc_saude.rede import criar_sessao
from osc_saude.receita_emails import enriquecer_com_emails
from osc_saude.tratamento import COLUNAS, montar_dataframe

# O nome é escrito por extenso de propósito. Com "python -m osc_saude.pipeline",
# __name__ vale "__main__", e o logger ficaria fora do pacote "osc_saude",
# onde a configuração de logging é feita: as mensagens sumiriam.
logger = logging.getLogger("osc_saude.pipeline")

NOME_DO_PACOTE = "osc_saude"


# ----- exportação e resumo -----

def exportar_csv(df: pd.DataFrame, pasta: Path, modo_teste: bool, hoje: date | None = None) -> Path:
    """Grava o CSV final, sem colunas auxiliares.

    Separador ";" e codificação utf-8-sig (UTF-8 com marca no início): é o par
    que o Excel em português abre com acentos e colunas certos ao dar duplo clique.
    Grava num .tmp e renomeia no fim, para não deixar um CSV pela metade.
    """
    hoje = hoje or date.today()
    sufixo = "_TESTE" if modo_teste else ""
    caminho = pasta / f"osc_saude_{hoje:%Y-%m-%d}{sufixo}.csv"
    temporario = caminho.with_name(caminho.name + ".tmp")
    try:
        pasta.mkdir(parents=True, exist_ok=True)
        df[COLUNAS].fillna("").to_csv(temporario, sep=";", index=False, encoding="utf-8-sig")
        os.replace(temporario, caminho)
    except OSError as e:
        raise ColetaError(
            f"Não consegui gravar {caminho}.\nSe o arquivo estiver aberto no Excel, feche e rode de novo.\nDetalhe: {e}"
        ) from e
    return caminho


def gerar_resumo(df: pd.DataFrame, contagem: Counter, com_email: bool) -> str:
    """Texto com os números principais do resultado. Não contém dados de contato."""
    linhas = [
        f"Registros lidos:               {contagem['lidos']}",
        f"Removidos (não ativos):        {contagem['inativos']}",
        f"Removidos (natureza fora):     {contagem['fora_natureza']}",
        f"Removidos (sem CNAE de saúde): {contagem['sem_cnae_saude']}",
        f"Removidos (CNPJ repetido):     {contagem['duplicados']}",
        f"Estabelecimentos no resultado: {len(df)}",
        "",
        "Top 10 UFs:",
        df["uf"].value_counts().head(10).to_string(),
        "",
        "Top 10 CNAEs principais:",
    ]
    cnaes = (
        df.groupby(["cnae_principal", "cnae_principal_descricao"]).size().reset_index(name="n")
        # Desempate pelo código, para o resultado não mudar de uma execução para outra.
        .sort_values(["n", "cnae_principal"], ascending=[False, True]).head(10)
    )
    for _, c in cnaes.iterrows():
        linhas.append(f"{c['n']:>6}  {c['cnae_principal']}  {c['cnae_principal_descricao']}")
    linhas += ["", "Contagem por origem do CNAE de saúde:", df["origem_cnae"].value_counts().to_string()]
    if com_email:
        linhas += ["", f"Com e-mail: {(df['email'] != '').sum()} de {len(df)}"]
    return "\n".join(linhas)


# ----- execução -----

def executar(config: Config) -> tuple[Path, str]:
    """Roda todas as etapas. Devolve (caminho do CSV, texto do resumo)."""
    sessao = criar_sessao()
    cnaes = carregar_cnaes_saude(sessao, config.grupos_cnae_extra, config.pausa_segundos)
    coletar(sessao, config, cnaes)
    df, contagem = montar_dataframe(config, cnaes)
    df = enriquecer_com_emails(sessao, config, df)

    caminho = exportar_csv(df, config.pasta_dados / "saida", config.modo_teste)
    resumo = gerar_resumo(df, contagem, config.enriquecer_email)
    # O resumo vai para um arquivo ao lado do CSV: o terminal rola e some, o arquivo fica.
    caminho.with_name(caminho.stem + "_resumo.txt").write_text(resumo + "\n", encoding="utf-8")
    return caminho, resumo


# ----- linha de comando -----

def criar_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m osc_saude.pipeline",
        description="Coleta OSCs de saúde ativas a partir de dados públicos de CNPJ e gera um CSV.",
    )
    p.add_argument("--teste", action="store_true",
                   help="Só a primeira UF, no máximo 2 páginas, em pasta separada. Bom para validar o ambiente.")
    p.add_argument("--ufs", nargs="+", type=str.upper, metavar="UF",
                   help="UFs a coletar (ex.: --ufs AC SP). Padrão: todas.")
    p.add_argument("--so-principal", action="store_true",
                   help="Exige CNAE de saúde no principal. Padrão: aceita também nos secundários.")
    p.add_argument("--com-email", action="store_true",
                   help="Busca e-mails nos arquivos da Receita Federal (baixa cerca de 5 GB).")
    p.add_argument("--mes-receita", metavar="AAAA-MM",
                   help="Pasta mais recente no site da Receita (ex.: 2026-09). Obrigatório com --com-email.")
    p.add_argument("--apagar-zips", action="store_true",
                   help="Apaga cada zip da Receita depois de processar. Padrão: guarda em data/raw.")
    p.add_argument("--dados", type=Path, default=Path("data"), metavar="PASTA",
                   help="Pasta de checkpoints e resultados. Padrão: data")
    p.add_argument("--pausa", type=float, default=1.0, metavar="SEGUNDOS",
                   help="Pausa entre requisições. Padrão: 1.0")
    return p


def construir_config(args: argparse.Namespace, parser: argparse.ArgumentParser) -> Config:
    if args.com_email and not args.mes_receita:
        parser.error("--com-email exige --mes-receita AAAA-MM (ex.: --mes-receita 2026-09).")
    opcoes = dict(
        modo_teste=args.teste,
        buscar_em_secundario=not args.so_principal,
        enriquecer_email=args.com_email,
        mes_receita=args.mes_receita,
        apagar_zips=args.apagar_zips,
        pasta_dados=args.dados,
        pausa_segundos=args.pausa,
    )
    if args.ufs:
        opcoes["ufs"] = tuple(args.ufs)
    try:
        return Config(**opcoes)
    except ValueError as e:
        parser.error(str(e))


def configurar_logging(arquivo: Path) -> None:
    """Mensagens na tela e num arquivo de log.

    A configuração é feita no logger do pacote, e não no logger raiz, para
    não interferir em bibliotecas de terceiros nem em quem importa o projeto.
    """
    encerrar_logging()
    pacote = logging.getLogger(NOME_DO_PACOTE)
    pacote.setLevel(logging.INFO)
    formato = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S")
    arquivo.parent.mkdir(parents=True, exist_ok=True)
    for handler in (logging.StreamHandler(), logging.FileHandler(arquivo, encoding="utf-8")):
        handler.setFormatter(formato)
        pacote.addHandler(handler)


def encerrar_logging() -> None:
    pacote = logging.getLogger(NOME_DO_PACOTE)
    for handler in list(pacote.handlers):
        pacote.removeHandler(handler)
        handler.close()


def main(argv: list[str] | None = None) -> int:
    """Devolve o código de saída: 0 sucesso, 1 erro, 130 interrompido (Ctrl+C)."""
    parser = criar_parser()
    config = construir_config(parser.parse_args(argv), parser)
    configurar_logging(config.pasta_dados / "logs" / "pipeline.log")
    try:
        caminho, resumo = executar(config)
    except ColetaError as e:
        # Erro esperado e já explicado na própria mensagem: sem traceback.
        logger.error("%s", e)
        return 1
    except KeyboardInterrupt:
        logger.warning("Interrompido. O que já foi coletado continua salvo; rode de novo para continuar.")
        return 130
    except Exception:
        # Erro inesperado: aqui o traceback é necessário para achar a causa.
        logger.exception("Erro inesperado.")
        return 1
    else:
        print("\n" + resumo)
        print(f"\nCSV salvo em: {caminho}")
        return 0
    finally:
        encerrar_logging()


if __name__ == "__main__":
    sys.exit(main())
