"""Funções puras de limpeza e formatação de campos do CNPJ.

"Pura" quer dizer: recebe um valor e devolve outro, sem ler arquivo, rede
ou variável global. Por isso cada função pode ser testada isoladamente.
"""

from datetime import datetime


def somente_digitos(valor: object) -> str:
    """Devolve só os dígitos de qualquer valor. None vira texto vazio."""
    if valor is None:
        return ""
    # 8610101.0 viraria "86101010" se convertido direto para texto.
    if isinstance(valor, float) and valor.is_integer():
        valor = int(valor)
    return "".join(c for c in str(valor) if c.isdigit())


def codigo(valor: object, digitos: int) -> str:
    """Converte um código em texto com zeros à esquerda (ex.: 659 -> "0659").

    Necessário porque CNPJ, CEP e códigos lidos como número perdem os zeros
    iniciais. Valor sem nenhum dígito vira texto vazio.
    """
    d = somente_digitos(valor)
    return d.zfill(digitos) if d else ""


def texto(valor: object) -> str:
    """Campo ausente vira texto vazio; o resto perde espaços nas pontas."""
    return "" if valor is None else str(valor).strip()


def formatar_cnpj(valor: object) -> str:
    """14 dígitos -> 00.000.000/0000-00. Fora disso, devolve só os dígitos."""
    d = codigo(valor, 14)
    if len(d) != 14:
        return d
    return f"{d[:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:]}"


def formatar_telefone(valor: object) -> str:
    """Formata telefone com DDD. Números incompletos voltam só com dígitos.

    A Receita às vezes grava o zero de discagem antes do DDD ("068..."),
    o que deixa um fixo com 11 dígitos e o faria parecer celular.
    Nenhum DDD começa com 0, então esse zero pode ser removido com segurança.
    """
    d = somente_digitos(valor)
    if len(d) in (11, 12) and d.startswith("0"):
        d = d[1:]
    if len(d) == 10:
        return f"({d[:2]}) {d[2:6]}-{d[6:]}"
    if len(d) == 11:
        return f"({d[:2]}) {d[2:7]}-{d[7:]}"
    # Sem DDD ou com dígitos faltando: mantém para conferência manual.
    return d


def formatar_cep(valor: object) -> str:
    """8 dígitos -> 00000-000. Qualquer outro tamanho vira texto vazio."""
    d = codigo(valor, 8)
    if len(d) != 8:
        return ""
    return f"{d[:5]}-{d[5:]}"


def formatar_data(valor: object) -> str:
    """AAAA-MM-DD -> DD/MM/AAAA. Data inválida vira texto vazio.

    Usa datetime em vez de só reordenar o texto para rejeitar
    datas impossíveis como 2026-13-45.
    """
    t = texto(valor)[:10]
    try:
        return datetime.strptime(t, "%Y-%m-%d").strftime("%d/%m/%Y")
    except ValueError:
        return ""


def formatar_natureza(codigo_natureza: object, descricao: object = "") -> str:
    """3999 + "Associação Privada" -> "399-9 - Associação Privada"."""
    c = codigo(codigo_natureza, 4)
    if len(c) != 4:
        return ""
    rotulo = f"{c[:3]}-{c[3]}"
    desc = texto(descricao)
    return f"{rotulo} - {desc}" if desc else rotulo


def montar_endereco(registro: dict) -> str:
    """Junta tipo de logradouro, logradouro, número e complemento.

    Remove vírgulas soltas no fim do logradouro, que aparecem nos dados
    da Receita (ex.: "GETULIO VARGAS,").
    """
    tipo = texto(registro.get("descricao_tipo_de_logradouro"))
    logradouro = texto(registro.get("logradouro")).rstrip(" ,")
    numero = texto(registro.get("numero"))
    complemento = texto(registro.get("complemento"))

    endereco = " ".join(p for p in (tipo, logradouro) if p)
    if numero:
        endereco += f", {numero}"
    if complemento:
        endereco += f" - {complemento}"
    return endereco.strip(" ,-")
