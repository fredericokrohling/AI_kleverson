# 4. Transcrição em tempo real (teste do bloco 1)

Resultado: durante uma reunião no Meet, um painel ao lado mostra o que cada pessoa fala,
sem nenhum bot na reunião. Ao final, a transcrição vai para o banco e você vê uma
comparação com as legendas do próprio Meet.

Pré-requisito: blocos 1 a 3 da instalação (console funcionando em `https://...ts.net`).

## 4.1 Chave do Deepgram

1. Crie uma conta em deepgram.com (há crédito gratuito para começar).
2. Crie uma chave de API e coloque em `DEEPGRAM_API_KEY` no `.env` do servidor.
3. `docker compose up -d`.

`DEEPGRAM_MODEL=nova-3` é o padrão. Se o painel mostrar erro de idioma ou modelo,
troque para `nova-2`, que tem suporte a `pt-BR` há mais tempo.

## 4.2 Instalar a extensão no Chrome (Linux Mint)

A extensão fica na pasta `extensao/` do repositório. Ela não está na Chrome Web Store;
você carrega a pasta direto.

1. No seu computador: `git clone git@github.com:fredericokrohling/AI_kleverson.git`
   (ou baixe o ZIP do GitHub e descompacte).
2. Chrome → `chrome://extensions` → ligue **Modo do desenvolvedor** (canto superior direito).
3. **Carregar sem compactação** → escolha a pasta `extensao`.
4. Fixe o ícone da extensão na barra (ícone de quebra-cabeça → alfinete).
5. Abra as **opções** da extensão: informe o endereço do console e clique em
   **Autorizar microfone**.

Para atualizar depois: `git pull` e o botão de recarregar da extensão em `chrome://extensions`.

## 4.3 Usar numa reunião

1. Entre na reunião pelo Chrome. **Use fone de ouvido**: sem fone, seu microfone capta a
   voz dos outros e ela aparece duplicada.
2. Ligue as legendas do Meet (botão **CC**), para a comparação.
3. Clique no ícone da extensão. O painel abre e o texto começa a aparecer.
4. No fim, **Parar e salvar**. O painel mostra o link da comparação.

## 4.4 O que avaliar no teste

Faça pelo menos 2 reuniões reais, de preferência uma com fornecedor externo, e anote:

| Critério | Pergunta |
| --- | --- |
| Precisão | Nomes de projetos, siglas e termos técnicos saíram certos? |
| Atraso | Quantos segundos entre a fala e o texto no painel? |
| Quem falou | O serviço separou bem as vozes? As legendas do Meet acertaram os nomes? |
| Você | Sua fala (canal do microfone) saiu completa e sem duplicar a dos outros? |
| Estabilidade | Caiu alguma vez? Em quanto tempo de reunião? |

Com isso decidimos: serviço externo, legendas do Meet ou os dois combinados (texto do
serviço, nomes das legendas).

## Limitações conhecidas

- A leitura das legendas depende da estrutura da página do Meet, que o Google muda sem
  aviso. Se a coluna de legendas vier vazia, é isso.
- Os horários dos dois canais são contados a partir do início de cada conexão; podem
  ficar alguns décimos de segundo desalinhados.
- O serviço identifica "Falante 1, 2, 3", não o nome. Associar nomes é o próximo passo.
