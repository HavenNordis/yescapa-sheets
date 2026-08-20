# Shortener Haven Nordis — instalação (`r.havennordis.com` via Vercel)

Transforma o link Tally longo e feio na mensagem de WhatsApp num link curto e
com marca:

```
ANTES:  https://forms.havennordis.com/pre-check-in?name=Tiago&ref=3461997&vehicle=Runa+%28CH-61-GD%29&date_in=13%2F07%2F2026&date_out=17%2F07%2F2026
DEPOIS: https://r.havennordis.com/3461997-tiago-cascais
```

## Porquê Vercel (e não Cloudflare)

O `r.havennordis.com` entra com **UM registo CNAME** na GoDaddy. **Não** é
preciso mover os nameservers do domínio. O email (Google Workspace), o site
(Netlify) e o forms (Tally) ficam **intactos**. Risco zero para o que já existe.

## Como funciona

```
Cron Railway (whatsapp_notification.py)
   │  1. constrói o URL Tally longo (igual a antes)
   │  2. faz upsert de {slug → URL longo} no Vercel Edge Config ....... redirect_store.py
   │  3. mete o link CURTO na mensagem de WhatsApp
   ▼
Hóspede clica em  r.havennordis.com/3461997-tiago-cascais
   ▼
Vercel Edge Middleware (vercel-redirect/middleware.js)
   │  lê o slug no Edge Config  →  302 redirect
   ▼
forms.havennordis.com/pre-check-in?...(Tally, pré-preenchido)
```

- O Tally fica intocado (`forms.havennordis.com` continua a ser do Tally). O
  shortener vive num subdomínio **novo e separado**, `r.havennordis.com`.
- A Middleware **não tem credenciais** e não fala com o Google — só lê o Edge Config.
- Se o store falhar ou o shortener estiver desligado, o cron usa o **URL Tally
  longo** na mensagem (degradação graciosa: o hóspede recebe sempre link válido).

---

## Parte A — Vercel (uma vez)

1. **Conta + projeto.** Login em vercel.com (com o GitHub). Importar/criar um
   projeto a partir da pasta `vercel-redirect/`.
2. **Edge Config.** Vercel → Storage → criar **Edge Config** (ex.: `havennordis-links`).
   Ligar (Connect) esse Edge Config ao projeto — o Vercel injeta sozinho a env
   var `EDGE_CONFIG` que a Middleware usa para ler.
3. **Deploy** do projeto.
4. **Domínio.** Projeto → Settings → Domains → adicionar `r.havennordis.com`.
   O Vercel mostra o **registo CNAME** a criar na GoDaddy (algo como
   `r  CNAME  cname.vercel-dns.com`). Adicionar SÓ esse registo na GoDaddy.
   (Nada de mexer em MX, TXT, nem no resto.)
5. **Teste.** Adicionar uma chave de teste no Edge Config (`teste` →
   `https://havennordis.com`) e abrir `https://r.havennordis.com/teste` →
   deve redirecionar. Slug inexistente → página 404 com marca. Apagar a chave.

## Parte B — Railway (uma vez)

Para o cron escrever no Edge Config, criar um **token de API Vercel** e env vars.

1. **Token Vercel.** Vercel → Account Settings → Tokens → criar token.
2. **IDs.** `VERCEL_EDGE_CONFIG_ID` = o id do Edge Config (`ecfg_...`).
   `VERCEL_TEAM_ID` só se o projeto estiver numa team.
3. **Env vars no Railway** (projeto `yescapa-sheets` → Variables → New Variable):

| Variável | Valor |
|---|---|
| `VERCEL_EDGE_CONFIG_ID` | `ecfg_...` |
| `VERCEL_API_TOKEN` | (token criado em B1) |
| `VERCEL_TEAM_ID` | (só se aplicável) |
| `SHORTENER_BASE_URL` | `https://r.havennordis.com` *(opcional; é o default)* |
| `SHORTENER_ENABLED` | `true` ← **pôr por último, depois da Parte A pronta** |

> Enquanto `SHORTENER_ENABLED` não for `true` (ou faltarem os `VERCEL_*`), tudo
> corre como hoje, com os URLs Tally longos. Liga/desliga sem deploy.

4. **Deploy do código.** Merge de `shortener.py`, `redirect_store.py` e
   `whatsapp_notification.py` para `main`. O Railway faz redeploy automático.

## Validar end-to-end

1. Com `DRY_RUN=true`, corre `python run.py --whatsapp` — em DRY-RUN **não** se
   escreve no store; a mensagem mostra o URL longo (esperado).
2. Com `DRY_RUN=false` e o shortener ligado, na próxima reserva elegível:
   confirma no log que não aparece `escrita no Edge Config falhou`; abre o link
   curto e verifica que cai no Tally pré-preenchido com a reserva certa.

## Manutenção

- **Mudar o domínio do Tally?** Reescreve-se o store (o cron fá-lo na próxima
  volta) ou as env vars `TALLY_FORM_URL_PT/EN`. Os links curtos não mudam.
- **Formato do slug** (`{ref}-{nome}-{ultimo_apelido}`) vive todo em
  `shortener.py`. Muda-se num só sítio.
- **Custo:** Vercel Hobby + Edge Config cabem no plano grátis para este volume.
  O Railway não ganha serviço novo — continua só com os crons.
