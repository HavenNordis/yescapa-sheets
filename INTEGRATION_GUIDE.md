# Integration Guide — WhatsApp + Google Contacts

**Estado:** Código pronto e testado localmente (31 testes a passar). Para tu executares para destravares produção e meteres o sistema novo a correr.

**Objetivo desta sessão:** que o Carlo Musso (#3435985 Krafie) e todas as reservas posteriores recebam o email pré-check-in correto, o WhatsApp (semi-automático) e o contacto no telemóvel — automaticamente.

---

## Resumo das fases

| Fase | O quê | Tempo estimado |
|---|---|---|
| **1.** Renovar Gmail token (A1) | Correr `gmail_oauth_setup.py` | ~5 min |
| **2.** Setup Google People API (B1+B2+B3) | Ativar API, scope, grupo | ~15 min |
| **3.** Gerar Contacts token | Correr `contacts_oauth_setup.py` | ~3 min |
| **4.** Tratar Carlo Musso (A2) | Marcar `manual_anterior` na sheet | ~2 min |
| **5.** Push para produção (C) | Branch + PR + merge + Railway vars | ~15 min |
| **6.** Reativar cron (A3) | Railway + verificar logs | ~5 min |

**Total: ~45 minutos.**

---

## FASE 1 — Renovar `GMAIL_REFRESH_TOKEN`

### 1.1 Garantir que tens `credentials.json`

Abre Explorador no Windows e vai a:
```
C:\Users\Haven Nordis\Documents\Claude\Projects\Automações\yescapa-sheets-clone\yescapa-sheets\
```

Confirma que existe `credentials.json`. Se NÃO existe:
- Vai a https://console.cloud.google.com/apis/credentials
- Encontra o OAuth 2.0 Client ID tipo "Desktop app"
- Clica no ícone de download (⬇) → JSON
- Renomeia para `credentials.json` e mete na pasta acima

### 1.2 Abrir terminal nessa pasta e correr

```bash
cd "C:\Users\Haven Nordis\Documents\Claude\Projects\Automações\yescapa-sheets-clone\yescapa-sheets"
pip install google-auth-oauthlib
python gmail_oauth_setup.py
```

Vai abrir o browser. **Faz login com `ops@havennordis.com`** (não com a tua conta pessoal). Aceita o consent.

### 1.3 Guardar o output

No terminal sai algo como:
```
GMAIL_CLIENT_ID=...
GMAIL_CLIENT_SECRET=...
GMAIL_REFRESH_TOKEN=1//...
```

Guarda os 3 valores num sítio seguro (não no chat).

### 1.4 Atualizar no Railway

- Railway → projeto `yescapa-sheets` → **Variables**
- Atualiza `GMAIL_REFRESH_TOKEN` com o novo valor
- (Os outros 2 mantêm-se iguais — só o REFRESH_TOKEN expirou)

**⚠️ Não reactives o cron ainda.** Falta o resto.

---

## FASE 2 — Setup Google People API

### 2.1 Ativar a API

- Vai a https://console.cloud.google.com/apis/library
- Procura "People API"
- Clica **Enable**

### 2.2 Adicionar scope ao consent screen

- Vai a https://console.cloud.google.com/apis/credentials/consent
- Clica **Edit App**
- Avança até **Scopes**
- Clica **Add or remove scopes**
- Procura e marca: `https://www.googleapis.com/auth/contacts`
- Save and continue até ao fim

### 2.3 Criar grupo "Hóspedes Yescapa"

- Abre browser **logada como `ops@havennordis.com`** (importante!)
- Vai a https://contacts.google.com
- Esquerda → **Etiquetas** → **Criar etiqueta**
- Nome: `Hóspedes Yescapa`
- Clica na etiqueta para abrir
- **Copia o URL** completo. Vai ser algo como:
  ```
  https://contacts.google.com/label/abc123def456
  ```
- O `contactGroupResourceName` é `contactGroups/abc123def456` (substitui o ID da URL).
- Guarda este valor — vais precisar dele na fase 5.

---

## FASE 3 — Gerar `CONTACTS_REFRESH_TOKEN`

### 3.1 Correr o script

Na mesma pasta do passo 1.2, corre:
```bash
python contacts_oauth_setup.py
```

Browser abre. **Login com `ops@havennordis.com`**. Aceita o consent (vai pedir permissão para Contactos desta vez).

### 3.2 Guardar output

```
CONTACTS_CLIENT_ID=...        ← mesmo do Gmail
CONTACTS_CLIENT_SECRET=...    ← mesmo do Gmail
CONTACTS_REFRESH_TOKEN=1//... ← NOVO, específico para Contacts
```

Guarda — vais usar na fase 5.

---

## FASE 4 — Tratar Carlo Musso (#3435985)

O cron está pausado e o Carlo ainda não recebeu email. Vamos garantir que quando o cron arrancar com o código novo, o Carlo é tratado corretamente.

**Opção que recomendo:** deixar o cron tratar dele automaticamente. O código novo tem o link Krafie correto, vai mandar o email bilingue completo, criar o contacto no Google e mandar a notificação WhatsApp ~2h depois. **É a melhor validação real do sistema.**

**Acção:** nada a fazer no caso do Carlo — deixa-o como está (estado vazio em PreCheckIn). O cron novo trata.

**Alternativa (se preferires controlar):** abre a folha **Reservas Yescapa → worksheet PreCheckIn** e adiciona uma linha com:

| booking_id | estado | timestamp | email_destinatario | idioma | erro |
|---|---|---|---|---|---|
| `3435985` | `manual_anterior` | (vazio) | (email Carlo) | `pt` | (vazio) |

E envias tu o email manualmente.

---

## FASE 5 — Push para produção

### 5.1 Confirmar o branch local

Abre terminal em:
```
C:\Users\Haven Nordis\Documents\Claude\Projects\Automações\yescapa-sheets-clone\yescapa-sheets\
```

```bash
git status                # ver as mudanças
git checkout -b feature/whatsapp-and-contacts
git add -A
git commit -m "Add WhatsApp notification + Google Contacts sync + fix Krafie email link"
git push -u origin feature/whatsapp-and-contacts
```

### 5.2 Criar PR no GitHub

Abre o repo no GitHub → ele sugere criar PR para `feature/whatsapp-and-contacts → main`. Cria.

Faz uma revisão rápida dos ficheiros que mudaram:
- ✅ `links_config.py` (NOVO)
- ✅ `google_contacts_sync.py` (NOVO)
- ✅ `whatsapp_notification.py` (NOVO)
- ✅ `contacts_oauth_setup.py` (NOVO)
- ✅ `templates/whatsapp_msg.txt` (NOVO)
- ✅ `templates/email_interno_ops.{html,subject,txt}` (NOVO)
- ✅ `templates/pre_check_in.{subject,txt,html}` (NOVO — cópia dos `_pt`)
- ✅ `templates/pre_check_in_{pt,en}.{txt,html}` (MODIFICADO — variáveis `$link_guia_*`)
- ✅ `pre_check_in_sender.py` (MODIFICADO — injecta `link_guia_pt`/`_en`)
- ✅ `run.py` (MODIFICADO — chama os 2 módulos novos)

### 5.3 Merge para main

Quando estiveres confortável, **Merge pull request**.

### 5.4 Adicionar env vars novas ao Railway

Railway → projeto `yescapa-sheets` → **Variables** → adicionar:

| Variável | Valor |
|---|---|
| `CONTACTS_CLIENT_ID` | (do output do passo 3.2) |
| `CONTACTS_CLIENT_SECRET` | (do output do passo 3.2) |
| `CONTACTS_REFRESH_TOKEN` | (do output do passo 3.2) |
| `CONTACTS_GROUP_RESOURCE_NAME` | `contactGroups/abc123...` (do passo 2.3) |
| `OPS_NOTIFICATION_EMAIL` | `ops@havennordis.com` (opcional — é o default) |
| `DELAY_APOS_EMAIL_MINUTES` | `120` (opcional — default) |

### 5.5 Aguardar redeploy

Railway faz redeploy automático após merge para main. Espera ~1-2 min e confirma nos **Deployments** que está verde.

---

## FASE 6 — Reativar cron + verificar

### 6.1 Reativar

Railway → projeto → encontra onde pausaste o cron job e **resume**.

### 6.2 Observar a primeira execução (próximos 15 min)

Logs → procura:
```
[whatsapp] === whatsapp_notification (DRY_RUN=False) ===
[contacts] === google_contacts_sync (DRY_RUN=False) ===
[pre-check-in] === pre_check_in_sender (DRY_RUN=False) ===
```

### 6.3 Verificar resultados

**a) Email Carlo Musso enviado:**
- Vai à folha **PreCheckIn** → linha `3435985` deve ter estado `auto_enviado_<timestamp>`
- Confirma que o Carlo recebeu o email (pergunta-lhe ou pede confirmação noutra forma)

**b) Contacto criado:**
- Vai a contacts.google.com (logada `ops@`) → etiqueta "Hóspedes Yescapa"
- Deve aparecer: `Carlo Musso · Krafie · 10/06–17/06`
- Sincronizar no teu telemóvel: abrir Contactos (se tens a conta ops@ adicionada com sync) ou aguardar próximo sync

**c) Notificação WhatsApp interna (espera ~2h):**
- Após 2h do email ter saído, vais receber email em `ops@havennordis.com` com botão "Abrir WhatsApp com a mensagem"
- Clicas → abre o WhatsApp Web/app com a mensagem PT+EN bilingue pronta
- Carregas em "enviar" → Carlo recebe

### 6.4 Monitorizar 24h

Verifica logs do cron a cada hora nas primeiras horas. Se algum erro aparecer, copia e diagnosticamos.

---

## Validação final — todos os hóspedes futuros

Próxima reserva nova que entrar via Yescapa vai:

1. Aparecer na folha Reservas (sync existente)
2. Receber email pré-check-in **bilingue com link YouTube certo da carrinha** (Fjord/Runa/Celta com `pEwfTeeLwGc`+`VXnCT8dLDus`, Krafie com `OKneOLVQM3c`+`q3k37lBj554`)
3. Ter contacto criado no Google com formato `Nome · Carrinha · datas`
4. Disparar email interno para ops@ ~2h depois, com botão wa.me para envio manual da mensagem WhatsApp

Tudo passivo. ✅

---

## Se algo correr mal

- **Erro Gmail "invalid_grant"** → o refresh_token expirou de novo (raro, mas acontece se revogares acessos). Recorrer fase 1.
- **Erro Contacts "insufficient scope"** → o scope `contacts` não foi adicionado ao consent screen ou re-autorização falhou. Recorrer 2.2 + 3.1.
- **Carlo recebeu email mas com link errado** → produção ainda está em código antigo. Confirma que o merge aconteceu e que o redeploy foi feito (Railway → Deployments).
- **Contacto não aparece no Google** → confirma o `CONTACTS_GROUP_RESOURCE_NAME` no Railway. O ID na URL (`https://contacts.google.com/label/XYZ`) tem de ser prefixado por `contactGroups/`.
- **Sem email para ops@ depois de 2h** → confirma na folha **WhatsApp** o estado da linha. Se for `a_aguardar`, é porque o cron ainda não voltou a correr — espera. Se for `auto_falhou_...`, copia o erro.

---

Quando estiveres a fazer cada fase, dá-me notícia. Se algum passo der erro, copia exatamente para aqui e diagnostico.
