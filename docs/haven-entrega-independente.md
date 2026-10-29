# Entrega independente Yescapa → Haven Desk (desligada)

Este robô continua a escrever a folha **Reservas** exatamente como antes. Este PR
acrescenta uma **entrega independente** das reservas Yescapa ao Haven Desk
(contrato v1.1 "entrada direta"), **desligada por defeito**.

## Estado
- `HAVEN_INGEST_ENABLED` ausente ou diferente de `true` → o gancho `_entregar_haven`
  retorna logo: não importa os módulos de envio, não abre a fila, não faz HTTP.
- Não há credenciais no repositório. Não usa chaves Supabase.
- Ativação **não** faz parte deste PR (ver "Ativação" abaixo).

## O que muda no `yescapa_sheets.py`
- `run()` reinicia em cada captura os sinais `archived_api_total` e `detail_failures`.
- `_fetch_detail` conta os detalhes que falham (sinal `detail_ok`).
- O diagnóstico de arquivadas guarda `archived_api_total` (já existia, só registava).
- `main()`:
  1. captura (apanha também `SystemExit`), com instante único em milissegundos;
  2. cópia **só-Yescapa** feita antes de qualquer acesso ao Sheets e antes de
     juntar as reservas diretas;
  3. escrita na folha **inalterada** (mesmas chamadas, ordem e mensagens);
  4. entrega Haven isolada (nunca afeta a folha), que drena pendentes mesmo que a
     captura ou a folha falhem;
  5. os erros originais (captura, parse, folha) são relançados no fim, como antes,
     para o `run.py` os registar.

## Módulos
- `haven_sync.py` — envelope v1.1, validação local, transporte HTTP (leitura limitada).
- `haven_queue.py` — fila durável (escrita atómica, quarentena, backoff, alerta 24h).
  Com a integração ligada exige `RAILWAY_VOLUME_MOUNT_PATH` e uma `HAVEN_QUEUE_DIR`
  dentro dele (links e `..` resolvidos).
- `haven_emitter.py` — orquestração: enfileira a captura nova só se completa e
  drena sempre os pendentes, com orçamento por execução.
- `ligacao-captura.proposta.md` — desenho revisto, lido pelos testes de ligação.

## Testes (CI: job `haven-emitter`, Linux, Python 3.13)
| Suíte | O que prova |
|---|---|
| `test_haven_emitter.py` | envelope, transporte, classificação v1.1, fila, fluxo composto (71) |
| `test_proposal_wiring.py` | proposta executada com o gancho real (9) |
| `test_revisao_independente.py` | revisão independente da v3 corrigida (10) |
| `test_main_real.py` | `main()` real, `parse_booking` real, só serviços externos simulados (12) |
| `test_scraper_signals.py` | `run()` real, sinais de captura, funções da folha inalteradas vs `122eea4` (5) |

Os testes não usam rede nem credenciais reais. `test_scraper_signals.py` compara a
estrutura com `yescapa_sheets.py` do commit `122eea4` (preparado pelo CI em
`/reference`) e grava um envelope fictício em `/evidence`.

## Ativação (fora deste PR)
1. Criar volume Railway; definir `RAILWAY_VOLUME_MOUNT_PATH` e `HAVEN_QUEUE_DIR`
   dentro dele; provar com dados fictícios que o mesmo `event_id`/instante/conteúdo
   sobrevive a duas execuções e a um novo deployment.
2. Haven: endpoint, credencial exclusiva, migração e escritor único.
3. Alimentação separada das reservas diretas.
4. Só então `HAVEN_INGEST_ENABLED=true` e prova captura real → Haven → calendário.

Limite conhecido: o orçamento de tempo da fila é verificado entre pedidos, não é um
prazo total rígido. Assume um único processo escritor da fila (o cron atual).
