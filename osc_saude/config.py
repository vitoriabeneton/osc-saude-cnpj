"""Configuração da coleta: filtros e caminhos (antiga célula 1)."""

from dataclasses import dataclass
from pathlib import Path

# Naturezas jurídicas (4 dígitos, sem hífen).
NATUREZAS_OSC = (
    "3999",  # 399-9 Associação Privada
    "3069",  # 306-9 Fundação Privada
    "3301",  # 330-1 Organização Social (OS)
    # "3220",  # 322-0 Organização Religiosa
)

# Grupos de CNAE somados à divisão 86 (atenção à saúde humana).
GRUPOS_CNAE_EXTRA = (
    "871",  # Assistência a idosos, deficientes e convalescentes em residências
    "872",  # Assistência psicossocial e à saúde (distúrbios psíquicos, dependência química)
)

TODAS_UFS = (
    "AC", "AL", "AP", "AM", "BA", "CE", "DF", "ES", "GO", "MA", "MT", "MS", "MG", "PA",
    "PB", "PR", "PE", "PI", "RJ", "RN", "RS", "RO", "RR", "SC", "SP", "SE", "TO",
)


@dataclass(frozen=True)
class Config:
    """Todos os parâmetros de uma execução, num objeto só.

    frozen=True impede alterar a configuração depois de criada. No notebook,
    uma célula podia sobrescrever uma variável global que outra usava
    (foi o que acontecia com LINKS_ESTABELECIMENTOS). Aqui isso não é possível.
    """

    naturezas: tuple[str, ...] = NATUREZAS_OSC
    grupos_cnae_extra: tuple[str, ...] = GRUPOS_CNAE_EXTRA
    # True: CNAE de saúde no principal OU no secundário. False: só no principal.
    buscar_em_secundario: bool = True
    ufs: tuple[str, ...] = TODAS_UFS
    # Modo teste: só a primeira UF e no máximo 2 páginas, em pasta separada.
    modo_teste: bool = False
    pausa_segundos: float = 1.0
    pasta_dados: Path = Path("data")

    def __post_init__(self) -> None:
        # Falha cedo: uma UF digitada errada daria uma coleta vazia sem aviso.
        invalidas = [uf for uf in self.ufs if uf not in TODAS_UFS]
        if invalidas:
            raise ValueError(f"UF inválida: {', '.join(invalidas)}")
        if not self.ufs:
            raise ValueError("Informe pelo menos uma UF.")

    @property
    def ufs_da_vez(self) -> tuple[str, ...]:
        return self.ufs[:1] if self.modo_teste else self.ufs

    @property
    def max_paginas(self) -> int | None:
        return 2 if self.modo_teste else None

    @property
    def campo_cnae(self) -> str:
        """Parâmetro da Minha Receita: "cnae" busca no principal e nos secundários."""
        return "cnae" if self.buscar_em_secundario else "cnae_fiscal"

    @property
    def pasta_checkpoints(self) -> Path:
        # O modo teste usa pasta própria para não se misturar com a coleta completa.
        base = self.pasta_dados / "checkpoints"
        return base / "teste" if self.modo_teste else base
