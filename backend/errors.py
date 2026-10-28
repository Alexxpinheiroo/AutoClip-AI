"""Erros compartilhados pelo pipeline de cortes."""


class PipelineCancelled(Exception):
    """Erro levantado quando o usuário cancela um job entre etapas."""
