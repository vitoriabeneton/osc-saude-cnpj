"""Exceções próprias do projeto."""


class ColetaError(Exception):
    """Falha que impede a coleta de continuar.

    Substitui o antigo interromper(), que levantava SystemExit. A diferença:
    SystemExit encerra o programa na hora, e quem chamou a função não tem
    chance de reagir. Uma exceção comum pode ser capturada, testada e,
    só no ponto de entrada (pipeline.py), virar mensagem e código de saída.

    A mensagem deve dizer o que aconteceu e o que fazer em seguida.
    """
