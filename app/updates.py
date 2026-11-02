"""Atualização automática: a página inicial se refaz quando o sistema processa algo novo.

Por que assim: recarregar a página inteira a cada 30 segundos atrapalharia (perde a
rolagem, interrompe um clique) e gastaria consultas à toa. Em vez disso, toda página do
console guarda uma "marca" do estado do sistema e pergunta ao servidor, a cada 30
segundos, se a marca mudou. A pergunta é barata (algumas consultas de máximo e
contagem). Só quando muda, o servidor avisa o navegador (evento "novidades" do HTMX), e
cada seção que escuta esse evento se busca de novo, sozinha:
- na página inicial: coleta, arquivos sem texto e Drive;
- o Processamento, só quando você está vendo a última página de hoje (se escolheu outro
  dia no calendário, a seção não muda debaixo dos seus olhos);
- o coração do menu, em qualquer página.

O que muda a marca: um e-mail ou versão do Drive nova, um anexo novo, um arquivo que
ganhou texto (ou perdeu), uma linha nova no Processamento, uma rodada de coleta que
terminou e o coração mudar de cor.
"""

# Consultas: max, count.
from sqlalchemy import func, select

# Session é a conversa com o banco.
from sqlalchemy.orm import Session

# Tipo da configuração.
from app.config import Settings

# Regra do coração vermelho.
from app.health import has_problem

# Tabelas consultadas.
from app.models import Attachment, AuditLog, BronzeItem, DriveItem, IngestionRun

# Intervalo entre as perguntas do navegador, em segundos.
REFRESH_SECONDS = 30


# Marca do estado do sistema: muda sempre que há algo novo para mostrar.
def stamp(session: Session, settings: Settings) -> str:
    """Texto curto com os contadores que importam para a página inicial."""
    # Cada valor numa lista, na ordem; None vira 0.
    parts = [
        # Última linha do Processamento.
        session.scalar(select(func.max(AuditLog.id))),
        # Último e-mail ou versão do Drive.
        session.scalar(select(func.max(BronzeItem.id))),
        # Último anexo, link ou imagem embutida.
        session.scalar(select(func.max(Attachment.id))),
        # Arquivos sem texto agora (muda quando o Claude lê um).
        session.scalar(
            select(func.count())
            .select_from(Attachment)
            .where(Attachment.extraction_error.is_not(None))
        ),
        # Arquivos do Drive com erro de download agora.
        session.scalar(
            select(func.count()).select_from(DriveItem).where(DriveItem.error.is_not(None))
        ),
        # Última rodada de coleta terminada (em segundos, para virar número).
        session.scalar(select(func.extract("epoch", func.max(IngestionRun.finished_at)))),
        # Coração vermelho (1) ou normal (0).
        int(has_problem(session, settings)),
    ]
    # Junta com "-" (ex.: "120-45-30-2-0-1761900000-0").
    return "-".join(str(int(p or 0)) for p in parts)
