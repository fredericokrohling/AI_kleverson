# /// script
# requires-python = ">=3.12"
# dependencies = ["mcp>=2.3,<3", "httpx>=0.28", "pydantic>=2.7"]
# ///
"""Servidor MCP local do Kleverson A.I. (bloco 7): conversar com os dados no app do Claude.

O que é MCP: o Model Context Protocol é o padrão aberto com que o Claude chama
ferramentas de fora. O Claude Desktop (ou o Claude Code) liga este programa no seu
computador e conversa com ele pela entrada e saída padrão (stdio). Quando você pergunta
"quais riscos estão abertos no projeto X?", o Claude escolhe a ferramenta listar_fatos,
este programa chama a API do console (/api/conversa/...) pela rede Tailscale, e o
Claude lê o JSON da resposta para escrever a resposta para você.

Por que no seu computador e não no servidor: o conector remoto do claude.ai é chamado
pelos servidores da Anthropic, que não alcançam a sua rede Tailscale (e o console não
fica na internet de propósito). Rodando aqui, quem chama o console é o seu computador,
pela rede privada, com o seu login do Tailscale, exatamente como o navegador.

Por que este programa é fino: ele não tem banco, nem senha, nem regra de negócio. Toda
consulta e toda regra (uma Empresa por vez, só fatos conferidos, quem pode ir para a
agenda) ficam no servidor (app/conversa.py), onde há testes com o banco de verdade.

Como rodar (o guia docs/setup/22-conversa-mcp.md tem o passo a passo):
    KLEVERSON_URL=https://assistente.SEU-TAILNET.ts.net uv run --script mcp_local/kleverson_mcp.py
O bloco "# /// script" no topo diz ao uv quais bibliotecas este arquivo usa; ele as
instala num ambiente só dele, sem mexer no resto do projeto.
"""

# os: lê as variáveis de ambiente (o endereço do console).
import os

# Literal: lista fechada de valores aceitos num parâmetro (o Claude vê as opções).
from typing import Annotated, Any, Literal

# httpx: cliente HTTP (faz os pedidos ao console).
import httpx

# MCPServer: o servidor MCP pronto da biblioteca oficial.
from mcp.server.mcpserver import MCPServer

# ToolError: erro que o Claude recebe como texto ("a ferramenta falhou porque...").
from mcp.server.mcpserver.exceptions import ToolError

# ToolAnnotations: dicas para o app do Claude (só lê? escreve?).
from mcp.types import ToolAnnotations

# Field: descrição de cada parâmetro (o Claude lê para saber o que passar).
from pydantic import Field

# Cabeçalho que o console exige nesta API (app/routes_conversa.py explica o porquê).
CLIENT_HEADER = {"X-Kleverson-Cliente": "mcp"}
# Tempo máximo de espera por resposta, em segundos (a agenda do Google pode demorar).
TIMEOUT_SECONDS = 30

# Orientação que o Claude recebe ao conectar. Vale para a conversa inteira.
INSTRUCTIONS = """\
Você está conectado ao Kleverson A.I., o assistente de gestão de projetos da dona. Os
dados vêm de e-mails, reuniões transcritas, arquivos do Drive e da agenda.

Regras:
1. Uma Empresa (cliente) por vez. Comece com listar_empresas para saber os ids. Toda
   outra ferramenta pede a Empresa. Nunca junte dados de duas Empresas numa resposta;
   se a pergunta não disser qual, pergunte.
2. Cite a fonte de toda afirmação: tipo, título, data e o link do console que vem em
   "fonte". Sem fonte, não afirme.
3. Fatos com "conferido": false são suspeitos (o trecho não foi achado no original).
   Só apareçam se pedidos, e sempre marcados como suspeitos.
4. Datas estão no fuso de Brasília. Prazos vêm como AAAA-MM-DD.
5. A busca é por palavra (não por significado) e ignora acento. Plurais em -ão/-ões não
   se encontram: busque "reunião OR reuniões". Aspas buscam a frase exata; -palavra exclui.
6. Textos longos vêm em páginas: use "proximo_inicio" para continuar.
7. por_na_agenda escreve na agenda do Google da Empresa: só use quando a dona pedir.
"""

# O servidor MCP, com nome e orientação.
mcp = MCPServer(name="kleverson", instructions=INSTRUCTIONS)

# Cliente HTTP, criado no primeiro uso (os testes trocam por um cliente de teste).
_client: httpx.Client | None = None

# Dicas das ferramentas que só leem: o app do Claude pode chamar sem medo de efeito.
READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
# Dica da que escreve: não apaga nada, mas cria algo fora (na agenda).
WRITES = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True
)

# Tipos dos parâmetros comuns, com a descrição que o Claude lê.
Empresa = Annotated[int, Field(description="Id da Empresa (veja listar_empresas).", ge=1)]
# Dia no formato do Postgres e do Python.
Dia = Annotated[str | None, Field(description="Dia no formato AAAA-MM-DD.")]
# Posição de início (página seguinte).
Inicio = Annotated[int, Field(description="Use o proximo_inicio da resposta anterior.", ge=0)]


# O cliente HTTP do console.
def client() -> httpx.Client:
    # Usa a variável global (uma conexão reaproveitada em toda a conversa).
    global _client
    # Já criado: devolve.
    if _client is not None:
        return _client
    # Endereço do console na rede Tailscale.
    base_url = os.environ.get("KLEVERSON_URL", "").strip().rstrip("/")
    # Sem endereço: o Claude mostra a frase, e você sabe o que corrigir.
    if not base_url:
        raise ToolError(
            "KLEVERSON_URL não configurada: ponha o endereço do console na configuração "
            "do Claude Desktop (guia docs/setup/22-conversa-mcp.md, passo 22.3)."
        )
    # Cria: endereço base, cabeçalho próprio, tempo máximo. A conexão é HTTPS, com o
    # certificado do Tailscale conferido (verify=True é o padrão e fica).
    _client = httpx.Client(base_url=base_url, headers=CLIENT_HEADER, timeout=TIMEOUT_SECONDS)
    # Devolve.
    return _client


# Frase de erro a partir da resposta do console.
def _error_text(response: httpx.Response) -> str:
    # Tenta ler o JSON.
    try:
        # Corpo da resposta.
        body = response.json()
    # Não é JSON (uma página de erro do Tailscale, por exemplo).
    except ValueError:
        return f"O console respondeu {response.status_code}: {response.text[:300]}"
    # Erro das consultas: {"erro": "..."}.
    if isinstance(body, dict) and body.get("erro"):
        return str(body["erro"])
    # Erro de validação do FastAPI: {"detail": [{"loc": [...], "msg": "..."}]}.
    detail = body.get("detail") if isinstance(body, dict) else None
    # Lista de problemas: "campo: motivo; campo: motivo".
    if isinstance(detail, list):
        return "; ".join(f"{d.get('loc', ['?'])[-1]}: {d.get('msg', '')}" for d in detail)
    # 403: o console não reconheceu você.
    if response.status_code == 403:
        return (
            "O console recusou o acesso (403). Confira se o Tailscale está ligado neste "
            f"computador com a sua conta. Detalhe: {detail}"
        )
    # Outro.
    return f"O console respondeu {response.status_code}: {detail or body}"


# Faz um pedido ao console e devolve o JSON. Parâmetros vazios não vão na URL.
def call(method: str, path: str, **params: Any) -> dict:
    # Tira os vazios (None) e converte True/False para o formato da URL.
    query = {
        k: ("true" if v is True else "false" if v is False else v)
        for k, v in params.items()
        if v is not None
    }
    # Tenta.
    try:
        # O pedido.
        response = client().request(method, f"/api/conversa{path}", params=query)
    # Sem rede, Tailscale desligado, servidor parado.
    except httpx.HTTPError as exc:
        raise ToolError(
            "Não consegui falar com o console. O Tailscale está ligado neste computador e "
            f"o servidor está no ar? Detalhe: {exc}"
        ) from exc
    # Erro do console: a frase vai para o Claude.
    if response.status_code >= 400:
        raise ToolError(_error_text(response))
    # Devolve o JSON.
    return response.json()


# --- Ferramentas ---------------------------------------------------------------------------
# O texto de cada função (docstring) é a descrição que o Claude lê para escolher.


# Empresas e projetos.
@mcp.tool(annotations=READ_ONLY)
def listar_empresas() -> dict:
    """Lista as Empresas (clientes), os projetos de cada uma e quantos fatos abertos e
    alertas há. Chame primeiro: as outras ferramentas pedem o id da Empresa."""
    # GET /api/conversa/empresas.
    return call("GET", "/empresas")


# Busca por palavra.
@mcp.tool(annotations=READ_ONLY)
def buscar(
    empresa: Empresa,
    palavras: Annotated[
        str, Field(description='Palavras a buscar. "frase exata", OR, -excluir.', max_length=200)
    ],
    projeto: Annotated[int | None, Field(description="Id do projeto.")] = None,
    fonte: Annotated[
        Literal["email", "transcricao", "drive"] | None, Field(description="Só uma fonte.")
    ] = None,
    desde: Dia = None,
    ate: Dia = None,
    inicio: Inicio = 0,
) -> dict:
    """Busca palavras em e-mails, transcrições de reunião, arquivos do Drive e anexos de
    uma Empresa. Devolve os mais relevantes com um trecho (palavra achada entre « »).
    Para o texto inteiro, use ler_fonte com o id da fonte (e anexo_id, se for anexo)."""
    # GET /api/conversa/buscar.
    return call(
        "GET",
        "/buscar",
        empresa=empresa,
        q=palavras,
        projeto=projeto,
        fonte=fonte,
        desde=desde,
        ate=ate,
        inicio=inicio,
    )


# Fatos.
@mcp.tool(annotations=READ_ONLY)
def listar_fatos(
    empresa: Empresa,
    tipo: Annotated[
        Literal[
            "tarefa",
            "decisao",
            "risco",
            "problema",
            "premissa",
            "dependencia",
            "prazo",
            "definicao",
        ]
        | None,
        Field(description="Tipo do fato."),
    ] = None,
    projeto: Annotated[int | None, Field(description="Id do projeto.")] = None,
    situacao: Annotated[
        Literal["abertos", "todos"],
        Field(description="abertos = só o que está aberto (decisões não têm situação)."),
    ] = "abertos",
    responsavel: Annotated[
        str | None, Field(description='"eu" para os da dona, ou parte do nome.')
    ] = None,
    prazo_ate: Dia = None,
    desde: Dia = None,
    incluir_suspeitos: Annotated[
        bool, Field(description="Inclui fatos cujo trecho não foi achado no original.")
    ] = False,
    inicio: Inicio = 0,
) -> dict:
    """Lista fatos apurados de uma Empresa: tarefas, decisões, riscos, problemas,
    premissas, dependências, prazos e definições. Cada fato traz o trecho literal da
    fonte, o responsável, quem disse e o prazo. Com prazo primeiro, do mais perto.
    Para decisões, use situacao="todos"."""
    # GET /api/conversa/fatos.
    return call(
        "GET",
        "/fatos",
        empresa=empresa,
        tipo=tipo,
        projeto=projeto,
        situacao=situacao,
        responsavel=responsavel,
        prazo_ate=prazo_ate,
        desde=desde,
        suspeitos=incluir_suspeitos,
        inicio=inicio,
    )


# Alertas.
@mcp.tool(annotations=READ_ONLY)
def alertas_abertos(empresa: Empresa) -> dict:
    """Alertas abertos de uma Empresa: pedidos, decisões e riscos vindos de lideranças,
    pelas regras da página Alertas. Os novos e mais fortes vêm primeiro."""
    # GET /api/conversa/alertas.
    return call("GET", "/alertas", empresa=empresa)


# Briefing.
@mcp.tool(annotations=READ_ONLY)
def briefing(empresa: Empresa, dia: Dia = None) -> dict:
    """O briefing diário de uma Empresa (hoje, se o dia não for dado): agenda com
    lideranças, alertas novos, prazos, o que mudou por projeto e pendências."""
    # GET /api/conversa/briefing.
    return call("GET", "/briefing", empresa=empresa, dia=dia)


# Reuniões.
@mcp.tool(annotations=READ_ONLY)
def reunioes(
    empresa: Empresa,
    desde: Dia = None,
    ate: Dia = None,
    titulo: Annotated[str | None, Field(description="Parte do título.")] = None,
) -> dict:
    """Reuniões de uma Empresa num período (padrão: duas semanas para trás e uma para a
    frente), com participantes e as transcrições (leia com ler_fonte)."""
    # GET /api/conversa/reunioes.
    return call("GET", "/reunioes", empresa=empresa, desde=desde, ate=ate, titulo=titulo)


# Ler uma fonte.
@mcp.tool(annotations=READ_ONLY)
def ler_fonte(
    empresa: Empresa,
    fonte_id: Annotated[int, Field(description='O "id" de uma "fonte" de outra resposta.')],
    anexo_id: Annotated[int | None, Field(description="Id de um anexo desta fonte.")] = None,
    inicio: Inicio = 0,
    conversa_inteira: Annotated[
        bool, Field(description="E-mail: inclui as mensagens antigas citadas abaixo.")
    ] = False,
) -> dict:
    """Lê o texto de uma fonte (e-mail, transcrição, arquivo do Drive) ou de um anexo
    dela, em páginas. Traz também destinatários, participantes, anexos e os fatos já
    apurados dela."""
    # GET /api/conversa/fontes/{id}.
    return call(
        "GET",
        f"/fontes/{fonte_id}",
        empresa=empresa,
        anexo=anexo_id,
        inicio=inicio,
        conversa_inteira=conversa_inteira,
    )


# Pôr na agenda (a única que escreve).
@mcp.tool(annotations=WRITES)
def por_na_agenda(
    empresa: Empresa,
    fato_id: Annotated[int, Field(description='O "id" de um fato com prazo.')],
) -> dict:
    """Põe o prazo de um fato na agenda do Google da Empresa: evento de dia inteiro,
    livre, particular, sem convidados. Só para fato conferido, aberto e com prazo. Use
    apenas quando a dona pedir."""
    # POST /api/conversa/fatos/{id}/agenda.
    return call("POST", f"/fatos/{fato_id}/agenda", empresa=empresa)


# Rodado direto (pelo Claude Desktop ou pelo uv): conversa por stdio.
if __name__ == "__main__":
    # Liga o servidor; ele fica esperando o Claude até a conversa fechar.
    mcp.run()
