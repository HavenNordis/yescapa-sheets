# Pré-check-in emails — setup e ativação

Este documento descreve a sequência **obrigatória** para ativar o envio
automático de emails pré-check-in para hóspedes da Yescapa.

> ⚠️ **Não saltes passos.** A ordem foi pensada para garantir que **nenhum
> hóspede que já recebeu o email manualmente volta a recebê-lo automaticamente**.

---

## Visão geral

O script `pre_check_in_sender.py` lê a worksheet `Reservas` (atualizada pelo
processo do Flávio) e a worksheet auxiliar `PreCheckIn`, e envia email a
hóspedes cuja reserva ainda não tem estado registado.

- **Fonte de verdade do estado por reserva:** worksheet `PreCheckIn`.
- **Anti-duplicado em camadas:**
  1. Backfill `manual_anterior` em todas as reservas existentes antes do go-live.
  2. Lock de linha (`enviando_<timestamp>`) antes de cada envio.
  3. Estado pós-envio (`auto_enviado_<timestamp>`) impede reenvio.
  4. Modo `DRY_RUN=true` para testar sem enviar nem escrever.

---

## Fase 1 — Configurar Gmail OAuth (uma vez)

O envio é feito como `ops@havennordis.com` via Gmail API com **OAuth user
credentials** (não service account — Gmail não suporta send-as via SA sem
domain-wide delegation do Workspace Admin).

1. Em [Google Cloud Console](https://console.cloud.google.com/):
   - Selecionar o projeto onde já está a service account `GOOGLE_CREDENTIALS_JSON`
     (ou criar projeto novo "Haven Nordis").
   - **APIs & Services → Library**: ativar **Gmail API**.
   - **APIs & Services → OAuth consent screen**:
     - User type: External
     - App name: `Haven Nordis Pre-Check-In`
     - Support email: `ops@havennordis.com`
     - Scopes: adicionar `.../auth/gmail.send`
     - Test users: adicionar `ops@havennordis.com`
   - **APIs & Services → Credentials → Create credentials → OAuth Client ID**:
     - Application type: **Desktop app**
     - Name: `Haven Nordis pre-check-in sender`
     - Após criar, fazer **Download JSON** e renomear para `credentials.json`.

2. Local (na máquina da Joana, uma vez):
   ```bash
   pip install google-auth-oauthlib
   python gmail_oauth_setup.py
   ```
   Abre o browser → autorizar com `ops@havennordis.com` → terminal mostra
   `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`, `GMAIL_REFRESH_TOKEN`.

3. Apagar `credentials.json` local (já não é preciso — o refresh token chega).

---

## Fase 2 — Adicionar variáveis ao Railway

No painel do projeto Railway, secção **Variables**:

| Variável                 | Valor                                                            |
|--------------------------|------------------------------------------------------------------|
| `GMAIL_CLIENT_ID`        | (do output do `gmail_oauth_setup.py`)                            |
| `GMAIL_CLIENT_SECRET`    | (idem)                                                           |
| `GMAIL_REFRESH_TOKEN`    | (idem)                                                           |
| `TALLY_FORM_URL`         | `https://tally.so/r/zx2ORZ` (opcional, este é o default)         |
| `SENDER_EMAIL`           | `ops@havennordis.com` (opcional, default)                        |
| `SENDER_NAME`            | `Haven Nordis` (opcional, default)                               |
| `PRE_CHECK_IN_WORKSHEET` | `PreCheckIn` (opcional, default)                                 |
| `DRY_RUN`                | `true` (durante testes, depois remover ou pôr `false`)           |

---

## Fase 3 — DNS para deliverability (uma vez, no domínio)

Para os emails não caírem em spam dos hóspedes (especialmente Gmail e Outlook),
o domínio `havennordis.com` precisa de:

- **SPF**: `TXT @ "v=spf1 include:_spf.google.com ~all"`
- **DKIM**: gerar chave em Google Workspace Admin → Apps → Google Workspace →
  Gmail → Authenticate email → criar registo DNS TXT com a chave gerada.
- **DMARC**: `TXT _dmarc "v=DMARC1; p=quarantine; rua=mailto:ops@havennordis.com"`
  (começar com `quarantine`, evoluir para `reject` depois de algumas semanas
  sem falsos positivos).

Estes 3 registos são adicionados no painel do registar do domínio
(GoDaddy / Cloudflare / Google Domains / etc.). Trabalho de ~20 min.

---

## Fase 4 — Backfill manual_anterior (CRÍTICO)

**Não saltar este passo.** Antes da primeira execução em produção, garantir
que NENHUMA reserva atual vai disparar email.

1. Abrir a spreadsheet "Reservas Yescapa" no Google Sheets.
2. **Criar uma worksheet nova chamada `PreCheckIn`** com cabeçalhos:

   | A: booking_id | B: estado | C: timestamp | D: email_destinatario | E: idioma | F: erro |

   (Alternativamente, ao primeiro `run_pre_check_in()` com `DRY_RUN=true`,
   o script cria a worksheet automaticamente.)

3. Na coluna `A`, **listar todos os IDs de reserva existentes** na worksheet
   `Reservas` (pode ser feito com fórmula `=Reservas!A2:A` colada como valores).
4. Na coluna `B`, escrever `manual_anterior` em todas as linhas.
5. Verificar com `=COUNTA(A:A)-1` que o número de linhas bate com o número
   de reservas em `Reservas`.

---

## Fase 5 — Dry-run (validação obrigatória antes de envios reais)

1. Confirmar `DRY_RUN=true` no Railway.
2. Adicionar à worksheet `Reservas` uma linha de teste (ou usar booking real
   recente onde tenhas controlo do email):
   - `Hóspede Email`: o teu email pessoal
   - Outros campos: preencher para o template ficar realista
3. Adicionar essa booking ID à `PreCheckIn` **com estado vazio** (para o
   script considerar enviável). Ou simplesmente não adicionar — qualquer
   booking_id que NÃO esteja na PreCheckIn vai ser tratado como "novo".

   ⚠️ Confirmar antes que TODAS as outras reservas têm `manual_anterior`.

4. Correr manualmente no Railway (ou via SSH local):
   ```bash
   DRY_RUN=true python run.py --pre-check-in
   ```
5. Inspecionar logs:
   - Confirmar que aparece **apenas a linha de teste** com `[DRY-RUN] enviaria a ...`
   - Confirmar que **NENHUMA reserva real existente** é mencionada.
6. Se algo estranho aparecer, **investigar antes de avançar**.

---

## Fase 6 — Envio real de teste (1 envio)

1. Manter a linha de teste com email da Joana.
2. **Remover `DRY_RUN`** do Railway (ou pôr `DRY_RUN=false`).
3. Correr:
   ```bash
   python run.py --pre-check-in
   ```
4. Verificar:
   - Email chegou à inbox da Joana, bem formatado.
   - O link Tally tem parâmetros pré-preenchidos.
   - Na worksheet `PreCheckIn`, a linha de teste passou a `auto_enviado_<timestamp>`.
5. Marcar a linha de teste com `nao_enviar` ou apagá-la.

---

## Fase 7 — Produção

A integração no `run.py` já chama `run_pre_check_in()` automaticamente
depois de cada `run_sync()`. Portanto:

- O cron `email-trigger` (a cada 15 min) já vai correr `--email` + sync +
  envio de pré-check-in.
- Os crons `sync-morning` e `sync-evening` também vão fazer envios.

Não é preciso cron novo no `railway.toml`.

**Monitorização nas primeiras 24h:**
- Ver os logs do Railway de cada execução.
- Espreitar a worksheet `PreCheckIn` para confirmar que estados ficam corretos.
- Se aparecer `auto_falhou_*`, investigar a coluna `erro`.

---

## Comportamento de cada estado na worksheet `PreCheckIn`

| Valor da coluna `estado`       | O que o script faz da próxima vez                           |
|--------------------------------|-------------------------------------------------------------|
| `(vazio)`                      | Trata como nova → envia e marca `auto_enviado_<timestamp>`  |
| `manual_anterior`              | Ignora — nunca envia                                        |
| `nao_enviar`                   | Ignora — flag manual da Joana para bloquear                 |
| `auto_enviado_<timestamp>`     | Ignora — já foi enviado com sucesso                         |
| `auto_falhou_<timestamp>_...`  | Ignora (não tenta de novo automaticamente). Para retry,     |
|                                | apagar manualmente o valor da célula → fica vazio →         |
|                                | script tentará de novo na próxima execução.                 |
| `enviando_<timestamp>`         | Estado temporário que indica falha entre lock e envio       |
|                                | (script crashou). Apagar manualmente para retry.            |

---

## Como adicionar um email "1 semana antes do check-in" depois

Quando o ciclo do pré-check-in estiver estável e quiseres adicionar lembretes
(48h antes do check-in, pedido de review pós-estadia, etc.), o padrão é:

1. Nova worksheet auxiliar (ex.: `Lembrete48h`) com mesma estrutura.
2. Novo módulo `lembrete_48h_sender.py` que importa as funções comuns do
   `pre_check_in_sender.py`.
3. Filtro por `data_in` em vez de "estado vazio" — só age quando
   `data_in - now() ∈ [47h, 49h]`.
4. Adicionar `run_lembrete_48h()` ao `run.py` na cadeia depois do sync.

Padrão modular = cada email novo é uma worksheet + módulo, sem mexer no que
já funciona.
