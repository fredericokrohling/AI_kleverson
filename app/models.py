"""Esquema do banco, organizado em camadas (modelo medallion).

- Cadastro: projetos e stakeholders, editados por você no console.
- Bronze: conteúdo bruto como chegou (e-mail, transcrição, mensagem). Nunca é editado.
- Silver: fatos extraídos (tarefa, decisão, risco...), sempre com trecho literal da fonte.
- Sugestões da IA: separadas, não contaminam bronze nem silver.
- Gold: virá como views SQL (KPIs calculados por código, nunca pelo LLM).
- Operação: tokens OAuth criptografados, chamadas ao LLM com custo, log de auditoria.

Por que camadas: se a IA errar ao extrair um fato, o original continua intacto no
bronze e dá para reprocessar. Nada que a IA escreve apaga o que veio da fonte.

Como ler uma coluna: `nome: Mapped[tipo_python] = mapped_column(tipo_sql, opções)`.
- Mapped[...] diz o tipo do valor em Python. "| None" significa que a coluna aceita
  vazio (NULL no banco); sem "| None" a coluna é obrigatória.
- mapped_column(...) diz como a coluna fica no banco: tipo SQL, índice, padrão etc.
- server_default é o valor que o próprio banco coloca quando ninguém informa nada.
"""

# date = só a data (dia/mês/ano); datetime = data com hora.
from datetime import date, datetime

# Decimal: número exato, usado para dinheiro e para notas de confiança (0.000 a 1.000).
from decimal import Decimal

# StrEnum: lista fechada de opções em que cada opção também é um texto.
from enum import StrEnum

# Any: "qualquer tipo"; usado nos dicionários livres (colunas JSON).
from typing import Any

# Tipos de coluna e utilitários do SQLAlchemy. Cada um corresponde a um tipo SQL.
from sqlalchemy import (
    # Inteiro grande (até 9 quintilhões); usado em tabelas que podem crescer muito.
    BigInteger,
    # Verdadeiro/falso.
    Boolean,
    # Data sem hora.
    Date,
    # Data com hora.
    DateTime,
    # Lista fechada de valores, validada pelo próprio banco.
    Enum,
    # Chave estrangeira: aponta para a linha de outra tabela.
    ForeignKey,
    # Inteiro comum.
    Integer,
    # Bytes brutos; usado para guardar dados criptografados.
    LargeBinary,
    # Número decimal exato com precisão definida.
    Numeric,
    # Texto com tamanho máximo.
    String,
    # Texto sem limite de tamanho.
    Text,
    # Regra de "não pode repetir" envolvendo uma ou mais colunas.
    UniqueConstraint,
    # func gera funções SQL; func.now() vira NOW() no banco (data e hora atuais).
    func,
)

# Tipos que só o Postgres tem: ARRAY (lista dentro de uma coluna) e JSONB (JSON indexável).
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

# Peças do mapeamento classe ↔ tabela (explicadas no topo do arquivo).
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


# Classe-mãe de todas as tabelas. O SQLAlchemy (e o Alembic) descobrem as tabelas
# olhando quem herda dela.
class Base(DeclarativeBase):
    # "pass" = nada a acrescentar; a herança já traz tudo que é preciso.
    pass


# Função auxiliar para criar colunas do tipo Enum sem repetir as mesmas opções.
# cls é a classe de opções (ex.: SourceKind); name é o nome do tipo no Postgres.
def _enum(cls: type[StrEnum], name: str) -> Enum:
    # Guarda o valor (texto em português) no banco, não o nome Python.
    # Ex.: grava "transcricao_reuniao" em vez de "MEET_TRANSCRIPT".
    return Enum(cls, name=name, values_callable=lambda e: [m.value for m in e])


# De onde veio um item bruto. Novas fontes (Teams, Zoom, Jira) entram aqui depois.
class SourceKind(StrEnum):
    # E-mail do Gmail.
    GMAIL = "gmail"
    # Evento da agenda.
    CALENDAR = "calendar"
    # Transcrição de reunião (feita pela extensão do Chrome).
    MEET_TRANSCRIPT = "transcricao_reuniao"
    # Mensagem do Google Chat.
    GOOGLE_CHAT = "google_chat"
    # Algo que você digitou à mão no console.
    MANUAL = "manual"
    # Arquivo de uma pasta monitorada do Google Drive (cada versão vira um item).
    DRIVE = "drive"


# Tipos de fato que a camada silver reconhece (base para RAID e plano de ação).
class FactKind(StrEnum):
    # Algo que alguém precisa fazer.
    TASK = "tarefa"
    # Algo que foi decidido.
    DECISION = "decisao"
    # Algo que pode dar errado (o R do RAID).
    RISK = "risco"
    # Algo que já deu errado (o I do RAID, issue).
    ISSUE = "problema"
    # Algo assumido como verdade sem confirmação (o A do RAID).
    ASSUMPTION = "premissa"
    # Algo que depende de outra entrega (o D do RAID).
    DEPENDENCY = "dependencia"
    # Uma data combinada.
    DEADLINE = "prazo"
    # Um termo ou regra definida no projeto.
    DEFINITION = "definicao"


# Como o sistema decidiu a qual projeto um item pertence. Serve para você auditar.
class ClassificationMethod(StrEnum):
    # Mesma conversa de e-mail de um item já classificado.
    THREAD = "mesma_conversa"
    # Remetente ou participante está cadastrado no projeto.
    STAKEHOLDER = "stakeholder"
    # Encontrou uma palavra-chave do projeto no texto.
    KEYWORD = "palavra_chave"
    # As regras acima não resolveram e o Claude decidiu.
    LLM = "llm"
    # Você classificou à mão no console.
    MANUAL = "manual"


# Situação de uma sugestão da IA.
class SuggestionStatus(StrEnum):
    # Criada pela IA, ainda sem decisão sua.
    PROPOSED = "proposta"
    # Você aceitou.
    ACCEPTED = "aceita"
    # Você descartou.
    DISCARDED = "descartada"


# "Mixin": classe que só empresta colunas. Quem herda dela ganha created_at.
class Timestamped:
    # Data e hora de criação, preenchida pelo banco (NOW()) com fuso horário.
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# --- Cadastro -----------------------------------------------------------------


# Um projeto ou programa que você gerencia.
class Project(Timestamped, Base):
    # Nome da tabela no banco.
    __tablename__ = "projects"

    # Identificador único, gerado automaticamente pelo banco (1, 2, 3...).
    id: Mapped[int] = mapped_column(primary_key=True)
    # Nome do projeto; unique=True impede dois projetos com o mesmo nome.
    name: Mapped[str] = mapped_column(String(200), unique=True)
    # Código curto opcional (ex.: "PRJ-042"), também sem repetição.
    code: Mapped[str | None] = mapped_column(String(50), unique=True)
    # Outros nomes pelos quais o projeto é chamado; "{}" é lista vazia no Postgres.
    aliases: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default="{}")
    # Palavras que, se aparecem num e-mail, indicam este projeto.
    keywords: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default="{}")
    # Projeto ativo? Projetos encerrados param de receber classificação automática.
    active: Mapped[bool] = mapped_column(Boolean, server_default="true")
    # Observações livres.
    notes: Mapped[str | None] = mapped_column(Text)


# Uma pessoa com quem você trabalha (cliente, fornecedor, equipe).
class Stakeholder(Timestamped, Base):
    # Nome da tabela no banco.
    __tablename__ = "stakeholders"

    # Identificador único gerado pelo banco.
    id: Mapped[int] = mapped_column(primary_key=True)
    # Nome da pessoa.
    name: Mapped[str] = mapped_column(String(200))
    # E-mail; 320 é o tamanho máximo de um endereço de e-mail. Sem repetição.
    email: Mapped[str | None] = mapped_column(String(320), unique=True)
    # Empresa ou área.
    organization: Mapped[str | None] = mapped_column(String(200))
    # Cargo.
    role: Mapped[str | None] = mapped_column(String(200))


# Liga pessoas a projetos (uma pessoa pode estar em vários projetos, e vice-versa).
class ProjectStakeholder(Base):
    # Nome da tabela no banco.
    __tablename__ = "project_stakeholders"

    # Aponta para o projeto. ondelete="CASCADE": apagou o projeto, some o vínculo.
    # As duas colunas juntas formam a chave primária (o par não pode se repetir).
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    # Aponta para a pessoa, com a mesma regra de apagar em cascata.
    stakeholder_id: Mapped[int] = mapped_column(
        ForeignKey("stakeholders.id", ondelete="CASCADE"), primary_key=True
    )
    # Papel da pessoa neste projeto (ex.: patrocinador, aprovador). Base para a RACI.
    role_in_project: Mapped[str | None] = mapped_column(String(200))


# --- Bronze -------------------------------------------------------------------


# Um item bruto, exatamente como chegou da fonte. Nunca é editado depois de gravado.
class BronzeItem(Base):
    # Nome da tabela no banco.
    __tablename__ = "bronze_items"
    # Reprocessar a mesma mensagem não duplica nada.
    __table_args__ = (UniqueConstraint("source", "external_id"),)

    # Identificador interno; BigInteger porque esta tabela cresce rápido.
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # De onde veio (Gmail, agenda, transcrição...).
    source: Mapped[SourceKind] = mapped_column(_enum(SourceKind, "source_kind"))
    # Identificador do item na fonte (ex.: id da mensagem no Gmail).
    external_id: Mapped[str] = mapped_column(String(500))
    # Identificador da conversa na fonte; index=True deixa a busca por conversa rápida.
    thread_id: Mapped[str | None] = mapped_column(String(500), index=True)
    # Quando aconteceu na fonte (envio do e-mail, início da reunião).
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    # Quem enviou ou falou.
    author: Mapped[str | None] = mapped_column(String(500))
    # Assunto ou título.
    subject: Mapped[str | None] = mapped_column(Text)
    # Texto completo; vazio por padrão.
    body_text: Mapped[str] = mapped_column(Text, server_default="")
    # Dados extras de cada fonte (destinatários, rótulos, participantes) em JSON.
    extra: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    # "Impressão digital" do conteúdo (SHA-256); detecta se algo mudou ou se repetiu.
    content_sha256: Mapped[str] = mapped_column(String(64))
    # Quando o sistema gravou o item (diferente de quando ele aconteceu).
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# Anexo de um item bruto (PDF, planilha, documento).
class Attachment(Base):
    # Nome da tabela no banco.
    __tablename__ = "attachments"

    # Identificador interno.
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # A qual item bruto o anexo pertence; apagou o item, apaga o anexo.
    bronze_item_id: Mapped[int] = mapped_column(
        ForeignKey("bronze_items.id", ondelete="CASCADE"), index=True
    )
    # Nome original do arquivo.
    filename: Mapped[str] = mapped_column(String(500))
    # Tipo do arquivo (ex.: application/pdf).
    mime_type: Mapped[str | None] = mapped_column(String(200))
    # Caminho do arquivo original na pasta de arquivos do servidor; o binário não fica
    # no banco (ver app/ingestion/storage.py).
    object_key: Mapped[str | None] = mapped_column(String(1000))
    # Texto extraído do arquivo, que é o que a IA lê.
    extracted_text: Mapped[str | None] = mapped_column(Text)
    # Se a extração falhou, o motivo fica aqui (para você saber o que não foi lido).
    extraction_error: Mapped[str | None] = mapped_column(Text)
    # Como o arquivo chegou: "anexo" (dentro do e-mail), "link_drive" (link do Drive no
    # corpo; o conteúdo vai para drive_items) ou "link_externo" (Lucid, Canva: não lido).
    origin: Mapped[str] = mapped_column(String(20), server_default="anexo")
    # Identificador na origem, quando houver (ex.: id do arquivo no Drive).
    external_ref: Mapped[str | None] = mapped_column(String(500))
    # Tamanho do arquivo original em bytes.
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    # Impressão digital do arquivo; o mesmo PDF em dez e-mails tem o mesmo valor.
    content_sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    # Quando você dispensou o aviso de "sem texto" no console; vazio = aparece na lista.
    dismissed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# Trechos da transcrição ao vivo. Ficam aqui enquanto a reunião acontece; ao finalizar,
# viram um único item do bronze (a transcrição completa).
class TranscriptSegment(Base):
    """Trecho de transcrição ao vivo, antes de a reunião virar um item do bronze.

    Cada canal é uma fonte: "voce" (seu microfone), "outros" (áudio da aba do Meet)
    e "legenda_meet" (legendas do próprio Meet, para comparação).
    """

    # Nome da tabela no banco.
    __tablename__ = "transcript_segments"

    # Identificador interno.
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # Identificador da reunião (gerado pela extensão); index para juntar os trechos rápido.
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    # De qual canal veio o trecho: "voce", "outros" ou "legenda_meet".
    channel: Mapped[str] = mapped_column(String(30))
    # Quem transcreveu (ex.: "deepgram" ou "meet").
    provider: Mapped[str] = mapped_column(String(50))
    # Quem falou, quando se sabe (ex.: "Falante 1" ou o nome da legenda do Meet).
    speaker: Mapped[str | None] = mapped_column(String(200))
    # O texto falado.
    text: Mapped[str] = mapped_column(Text)
    # Milissegundos desde o início da captura daquele canal.
    start_ms: Mapped[int | None] = mapped_column(Integer)
    # Fim do trecho, na mesma contagem.
    end_ms: Mapped[int | None] = mapped_column(Integer)
    # Quando o servidor recebeu o trecho.
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# Liga cada item bruto a um projeto.
class ItemClassification(Base):
    """A qual projeto um item bruto pertence, e como isso foi decidido."""

    # Nome da tabela no banco.
    __tablename__ = "item_classifications"

    # O item classificado; é também a chave primária (um item, uma classificação).
    bronze_item_id: Mapped[int] = mapped_column(
        ForeignKey("bronze_items.id", ondelete="CASCADE"), primary_key=True
    )
    # O projeto escolhido; vazio quando o item não é de nenhum projeto.
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"), index=True)
    # Qual regra decidiu (conversa, stakeholder, palavra-chave, IA ou você).
    method: Mapped[ClassificationMethod] = mapped_column(
        _enum(ClassificationMethod, "classification_method")
    )
    # Confiança de 0.000 a 1.000; Numeric(4, 3) = 4 dígitos, 3 depois da vírgula.
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    # Quando a decisão foi tomada.
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# --- Silver -------------------------------------------------------------------


# Um fato extraído de um item bruto (tarefa, decisão, risco...).
class Fact(Timestamped, Base):
    # Nome da tabela no banco.
    __tablename__ = "facts"

    # Identificador interno.
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # A qual projeto o fato pertence.
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"), index=True)
    # Tipo do fato (ver FactKind).
    kind: Mapped[FactKind] = mapped_column(_enum(FactKind, "fact_kind"), index=True)
    # Resumo curto.
    title: Mapped[str] = mapped_column(String(500))
    # Detalhes.
    description: Mapped[str | None] = mapped_column(Text)
    # Responsável, quando houver.
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("stakeholders.id"))
    # Prazo, quando houver; index para listar "o que vence esta semana" rápido.
    due_date: Mapped[date | None] = mapped_column(Date, index=True)
    # Situação livre (ex.: aberto, concluído).
    status: Mapped[str | None] = mapped_column(String(50))
    # De qual item bruto o fato saiu (rastreabilidade obrigatória).
    source_item_id: Mapped[int] = mapped_column(ForeignKey("bronze_items.id"), index=True)
    # Trecho literal da fonte que sustenta o fato. O código confere se ele existe no bronze.
    source_quote: Mapped[str] = mapped_column(Text)
    # True quando o código confirmou que o trecho existe mesmo na fonte (anti-alucinação).
    quote_verified: Mapped[bool] = mapped_column(Boolean, server_default="false")
    # Confiança da extração, de 0.000 a 1.000.
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    # Um fato corrigido não é apagado: aponta para o fato que o substituiu.
    superseded_by_id: Mapped[int | None] = mapped_column(ForeignKey("facts.id"))


# --- Sugestões da IA ----------------------------------------------------------


# Algo que a IA propõe (ex.: novo risco, ajuste de prazo). Fica separado dos fatos.
class AISuggestion(Timestamped, Base):
    # Nome da tabela no banco.
    __tablename__ = "ai_suggestions"

    # Identificador interno.
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # Projeto ao qual a sugestão se refere.
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"), index=True)
    # Tipo da sugestão, em texto livre (ex.: "novo_risco").
    kind: Mapped[str] = mapped_column(String(100))
    # Conteúdo da sugestão em JSON (o formato muda conforme o tipo).
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    # Por que a IA sugeriu isso.
    rationale: Mapped[str | None] = mapped_column(Text)
    # Situação; começa como "proposta".
    status: Mapped[SuggestionStatus] = mapped_column(
        _enum(SuggestionStatus, "suggestion_status"),
        server_default=SuggestionStatus.PROPOSED.value,
    )
    # Quando você aceitou ou descartou.
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# --- Operação -----------------------------------------------------------------


# Credenciais de acesso ao Google (e, no futuro, a outros serviços).
class OAuthToken(Base):
    # Nome da tabela no banco.
    __tablename__ = "oauth_tokens"
    # Uma linha por serviço e conta; logar de novo atualiza em vez de duplicar.
    __table_args__ = (UniqueConstraint("provider", "account_email"),)

    # Identificador interno.
    id: Mapped[int] = mapped_column(primary_key=True)
    # Qual serviço (ex.: "google").
    provider: Mapped[str] = mapped_column(String(50))
    # Qual conta foi conectada.
    account_email: Mapped[str] = mapped_column(String(320))
    # Permissões que o Google realmente concedeu.
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text))
    # Tokens criptografados com TOKEN_ENCRYPTION_KEY; nunca em texto puro.
    # O refresh token é de longa duração: serve para pedir novos access tokens.
    refresh_token_enc: Mapped[bytes] = mapped_column(LargeBinary)
    # O access token dura cerca de 1 hora e é o que vai em cada chamada à API.
    access_token_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    # Quando o access token expira.
    access_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Última atualização; onupdate faz o SQLAlchemy renovar a data a cada alteração.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


# Registro de cada chamada ao Claude: quanto custou e para quê. Base do teto de gasto.
class LLMCall(Base):
    # Nome da tabela no banco.
    __tablename__ = "llm_calls"

    # Identificador interno.
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # Quando a chamada foi feita; index para somar o gasto do mês rápido.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    # Para que serviu (ex.: "classificacao", "teste").
    purpose: Mapped[str] = mapped_column(String(100))
    # Qual modelo respondeu.
    model: Mapped[str] = mapped_column(String(100))
    # Tokens enviados ao modelo.
    input_tokens: Mapped[int] = mapped_column(Integer, server_default="0")
    # Tokens gerados pelo modelo.
    output_tokens: Mapped[int] = mapped_column(Integer, server_default="0")
    # Tokens lidos do cache (mais baratos).
    cache_read_tokens: Mapped[int] = mapped_column(Integer, server_default="0")
    # Tokens gravados no cache (um pouco mais caros).
    cache_write_tokens: Mapped[int] = mapped_column(Integer, server_default="0")
    # Custo calculado em dólares, com 6 casas decimais.
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), server_default="0")
    # Por que o modelo parou (terminou, atingiu limite de tamanho etc.).
    stop_reason: Mapped[str | None] = mapped_column(String(50))
    # Id da requisição na Anthropic, útil para pedir suporte.
    request_id: Mapped[str | None] = mapped_column(String(100))
    # Mensagem de erro, se a chamada falhou.
    error: Mapped[str | None] = mapped_column(Text)


# Cada rodada de um coletor (hoje, o do Gmail). Serve para três coisas: saber de onde a
# próxima rodada continua, mostrar no console se a coleta está saudável e provar o
# critério de pronto do bloco 2 ("um mês importado sem erro").
class IngestionRun(Base):
    # Nome da tabela no banco.
    __tablename__ = "ingestion_runs"

    # Identificador interno.
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # Qual fonte foi coletada; index para achar a última rodada de cada fonte rápido.
    source: Mapped[SourceKind] = mapped_column(_enum(SourceKind, "source_kind"), index=True)
    # Qual conta foi lida.
    account_email: Mapped[str | None] = mapped_column(String(320))
    # Quando a rodada começou.
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Quando terminou; vazio enquanto está rodando (ou se o processo morreu no meio).
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Início da janela pedida ao Gmail ("e-mails depois de...").
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Fim da janela: o momento em que a lista foi pedida. A próxima rodada parte daqui.
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # "em_andamento", "ok" (sem nenhum erro) ou "com_erros".
    status: Mapped[str] = mapped_column(String(20), server_default="em_andamento")
    # Quantas mensagens a busca devolveu na janela (novas e já conhecidas).
    messages_seen: Mapped[int] = mapped_column(Integer, server_default="0")
    # Quantas eram novas e foram gravadas no bronze.
    messages_new: Mapped[int] = mapped_column(Integer, server_default="0")
    # Quantos arquivos (anexos e links do Drive) foram gravados.
    attachments_saved: Mapped[int] = mapped_column(Integer, server_default="0")
    # Quantos desses arquivos não tiveram o texto extraído (o original fica guardado).
    extraction_errors: Mapped[int] = mapped_column(Integer, server_default="0")
    # Quantas mensagens falharam por inteiro (rede, API); são tentadas de novo depois.
    message_errors: Mapped[int] = mapped_column(Integer, server_default="0")
    # Detalhes dos erros de mensagem (id e motivo), para diagnóstico.
    error_detail: Mapped[str | None] = mapped_column(Text)


# Configurações que você muda pelo console (modelo do Claude, esforço, idioma da
# transcrição). Ficam no banco, e não no .env, para valer sem reiniciar nada. Quando uma
# chave não existe aqui, vale o padrão do .env (ver app/runtime_settings.py).
class AppSetting(Base):
    # Nome da tabela no banco.
    __tablename__ = "app_settings"

    # Nome da configuração (ex.: "llm_model"); é a chave primária.
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    # Valor em JSON (texto, número ou lista, conforme a configuração).
    value: Mapped[Any] = mapped_column(JSONB)
    # Quando foi alterada pela última vez.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


# --- Google Drive e tags ------------------------------------------------------


# Uma etiqueta que diz a que o conteúdo se refere (ex.: "Projeto CRM", "Arquitetura").
class Tag(Timestamped, Base):
    # Nome da tabela no banco.
    __tablename__ = "tags"

    # Identificador interno.
    id: Mapped[int] = mapped_column(primary_key=True)
    # Nome da tag; não pode repetir.
    name: Mapped[str] = mapped_column(String(100), unique=True)


# Uma pasta do Drive que você mandou monitorar (a "raiz" do monitoramento).
class DriveFolder(Base):
    # Nome da tabela no banco.
    __tablename__ = "drive_folders"

    # Identificador interno.
    id: Mapped[int] = mapped_column(primary_key=True)
    # Id da pasta no Google Drive; a mesma pasta não entra duas vezes.
    drive_id: Mapped[str] = mapped_column(String(200), unique=True)
    # Nome da pasta no Drive.
    name: Mapped[str] = mapped_column(String(1000))
    # Monitorando? Remover pelo console só desliga (os arquivos e as tags ficam).
    active: Mapped[bool] = mapped_column(Boolean, server_default="true")
    # Quando foi cadastrada.
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Última varredura completa; vazio = ainda não varrida (a próxima rodada faz na hora).
    last_scan_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Erro da última varredura, se houve.
    last_error: Mapped[str | None] = mapped_column(Text)


# Um arquivo ou subpasta encontrado dentro de uma pasta monitorada. Espelha a árvore
# do Drive: cada item aponta para a pasta-mãe pelo id do Drive.
class DriveItem(Base):
    # Nome da tabela no banco.
    __tablename__ = "drive_items"

    # Identificador interno.
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # Id no Google Drive; sem repetição.
    drive_id: Mapped[str] = mapped_column(String(200), unique=True)
    # A pasta monitorada que o encontrou; vazio quando o arquivo chegou por link num e-mail.
    folder_id: Mapped[int | None] = mapped_column(ForeignKey("drive_folders.id"), index=True)
    # Id do Drive da pasta-mãe; vazio na própria pasta monitorada. index: listar o
    # conteúdo de uma pasta fica rápido.
    parent_drive_id: Mapped[str | None] = mapped_column(String(200), index=True)
    # Nome do arquivo ou da pasta.
    name: Mapped[str] = mapped_column(String(1000))
    # Caminho legível a partir da pasta monitorada (ex.: "CRM/Contratos/plano.xlsx").
    path: Mapped[str] = mapped_column(Text, server_default="")
    # Tipo MIME no Drive.
    mime_type: Mapped[str | None] = mapped_column(String(200))
    # É pasta?
    is_folder: Mapped[bool] = mapped_column(Boolean, server_default="false")
    # Última alteração no Drive.
    modified_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Número de versão do Drive; muda a cada alteração do arquivo.
    version: Mapped[str | None] = mapped_column(String(50))
    # Tamanho em bytes (o Drive não informa para Docs, Sheets e Slides).
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    # Link para abrir no Drive.
    web_link: Mapped[str | None] = mapped_column(Text)
    # True quando sumiu da pasta (apagado ou movido para fora); o histórico continua.
    removed: Mapped[bool] = mapped_column(Boolean, server_default="false")
    # Última varredura em que foi visto.
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Versão mais recente gravada no bronze.
    bronze_item_id: Mapped[int | None] = mapped_column(ForeignKey("bronze_items.id"))
    # Impressão digital do texto da versão mais recente: se não mudou, não grava de novo.
    content_sha256: Mapped[str | None] = mapped_column(String(64))
    # Motivo de o arquivo não ter sido lido (grande demais, tipo não suportado...).
    error: Mapped[str | None] = mapped_column(Text)
    # Quando você dispensou o aviso de "sem texto" no console; vazio = aparece na lista.
    dismissed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# Tag colocada diretamente num arquivo ou numa pasta. A herança (pasta → conteúdo) não é
# gravada aqui: a view drive_item_effective_tags calcula na hora da consulta.
class DriveItemTag(Base):
    # Nome da tabela no banco.
    __tablename__ = "drive_item_tags"

    # O arquivo ou pasta; apagou o item, some o vínculo.
    drive_item_id: Mapped[int] = mapped_column(
        ForeignKey("drive_items.id", ondelete="CASCADE"), primary_key=True
    )
    # A tag; apagou a tag, some de todos os itens (CASCADE).
    tag_id: Mapped[int] = mapped_column(ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True)
    # Quando foi colocada.
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# --- Saúde ----------------------------------------------------------------------


# Resultado de um teste de saúde (um serviço, um momento).
class HealthCheck(Base):
    # Nome da tabela no banco.
    __tablename__ = "health_checks"

    # Identificador interno.
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # Quando o teste rodou; index para achar o último de cada serviço rápido.
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    # Qual serviço (ex.: "gmail", "status:github").
    service: Mapped[str] = mapped_column(String(50), index=True)
    # True = funcionando; False = com problema; vazio = não deu para saber.
    ok: Mapped[bool | None] = mapped_column(Boolean)
    # Quanto tempo a resposta levou, em milissegundos.
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    # Explicação curta do resultado.
    detail: Mapped[str | None] = mapped_column(Text)


# Diário de tudo que o sistema faz sozinho. Como você não aprova cada ação, é aqui
# que você confere depois o que aconteceu.
class AuditLog(Base):
    # Nome da tabela no banco.
    __tablename__ = "audit_log"

    # Identificador interno.
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # Quando aconteceu.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    # Quem fez (ex.: "sistema", seu e-mail).
    actor: Mapped[str] = mapped_column(String(100))
    # O que foi feito (ex.: "google.conectado").
    action: Mapped[str] = mapped_column(String(100), index=True)
    # Em quê (ex.: id do item, e-mail da conta).
    target: Mapped[str | None] = mapped_column(String(500))
    # Detalhes livres em JSON.
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")


# **details junta todos os argumentos extras com nome num dicionário.
# Ex.: audit(s, "sistema", "x", modelo="opus") gera details={"modelo": "opus"}.
def audit(session, actor: str, action: str, target: str | None = None, **details: Any) -> None:
    """Registra uma ação no log de auditoria (quem fez o quê, em quê)."""
    # session.add só prepara a gravação; ela acontece quando quem chamou faz commit.
    session.add(AuditLog(actor=actor, action=action, target=target, details=details))
