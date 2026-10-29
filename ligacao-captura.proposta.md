# Proposta de ligação do emissor à captura — v3 (NÃO aplicada à produção)

Mudança no `yescapa_sheets.py`, **com envios desligados por defeito**. Independência
total das planilhas e da captura (revisão v2, item 1):

- A **cópia só-Yescapa** (`yescapa_only`) é preparada a partir de `detailed`
  **ANTES de qualquer acesso ao Sheets** — se abrir/autenticar a planilha falhar,
  a captura nova continua a poder ser entregue.
- A captura e o Sheets são envolvidos para apanhar **`Exception` e `SystemExit`**
  (o scraper pode levantar `SystemExit` sem reservas; o `SheetsClient` pode
  levantá-lo por credencial ausente). Os erros são **preservados e re-levantados
  no fim**, depois do ramo Haven.
- O ramo **Haven corre sempre** e a **drenagem de pendentes é garantida** mesmo
  que a captura nova seja inválida/levante — a construção da captura nova é
  **isolada** (`build_capture_envelope` nunca impede `delivery_cycle`).

A separação Yescapa vs diretas mantém-se: a matriz usa `yescapa_only`, copiado
antes de `bookings += diretas`.

## §1 `yescapa_sheets.py` — `main()` + gancho

```python
def main(trigger: str = "scheduled"):
    from datetime import datetime, timezone
    print("=== Yescapa → Google Sheets ===")

    # 1. Captura (instante único, com milissegundos). Apanha SystemExit do scraper.
    yp = None
    detailed = None
    snapshot_at = None
    capture_err = None
    try:
        yp = YescapaPlaywright()
        detailed = yp.run()
        now = datetime.now(timezone.utc)
        snapshot_at = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
    except (Exception, SystemExit) as e:
        capture_err = e

    # 2. Cópia SÓ-YESCAPA antes de qualquer acesso ao Sheets (independência).
    yescapa_only = None
    parse_err = None
    if detailed is not None:
        try:
            yescapa_only = [parse_booking(b) for b in detailed]
        except Exception as e:
            parse_err = e
            yescapa_only = None

    # 3. Google Sheets — comportamento INALTERADO; erro preservado p/ re-levantar.
    sheets_err = None
    if yescapa_only is not None:
        try:
            sheets = SheetsClient()
            ws = sheets.get_or_create_worksheet(GOOGLE_SHEET_NAME, WORKSHEET_NAME)
            bookings = list(yescapa_only)              # cópia só-Yescapa
            if bookings:
                diretas = sheets.read_direct_bookings(
                    GOOGLE_SHEET_NAME, list(bookings[0].keys()),
                    {str(b.get("ID", "")) for b in bookings},
                )
                if diretas:
                    bookings += diretas                # mistura só para a folha
            sheets.update_bookings(ws, bookings)
            sheets.update_log(GOOGLE_SHEET_NAME, LOGSHEET_NAME, trigger, len(bookings))
        except (Exception, SystemExit) as e:
            sheets_err = e                             # preserva, re-levanta no fim

    # 4. Haven — INDEPENDENTE: drena pendentes SEMPRE; enfileira a nova só se válida.
    try:
        _entregar_haven(yp if detailed is not None else None, yescapa_only, snapshot_at)
    except (Exception, SystemExit) as e:
        print(f"[haven] ignorado sem afetar as planilhas ({type(e).__name__})")

    # 5. Preserva os erros/behaviour originais (crash do cron) — DEPOIS do Haven.
    if sheets_err is not None:
        raise sheets_err
    if parse_err is not None:
        raise parse_err
    if capture_err is not None:
        raise capture_err
    print("\nConcluído!")


def _entregar_haven(yp, yescapa_only, snapshot_at):
    import os
    if os.getenv("HAVEN_INGEST_ENABLED", "").strip().lower() != "true":
        return  # desligado: não enfileira nem envia
    import haven_sync as HS
    import haven_queue as HQ
    import haven_emitter as HE
    q = HQ.HavenQueue(HQ.queue_path())             # recusa fallback efémero/fora-do-volume
    new_env = None
    if yp is not None:
        try:
            new_env, _erros = HE.build_capture_envelope(yp, yescapa_only, snapshot_at)
        except Exception:
            new_env = None                         # isolado: nunca impede a drenagem
    HE.delivery_cycle(q, HS.send_envelope, new_env)  # drena pendentes SEMPRE
```

## §2 Diagnóstico (guardar o total + contar falhas de detalhe)
```python
            total = int(total)
            self.archived_api_total = total        # reutilizável pelo emissor
```
```python
    def _fetch_detail(self, page, booking_id):
        try:
            resp = page.request.get(f"{API_BASE}/v4/bookings-owner/{booking_id}/",
                                    headers=self._api_headers)
            if not resp.ok:
                self.detail_failures = getattr(self, "detail_failures", 0) + 1
                return {}
            return resp.json()
        except Exception:
            self.detail_failures = getattr(self, "detail_failures", 0) + 1
            return {}
```

## §3 Persistência (Railway)
`queue_path()` exige, com `HAVEN_INGEST_ENABLED=true`, `RAILWAY_VOLUME_MOUNT_PATH`;
recusa o default efémero e qualquer caminho **fora** do volume
(links e `..` resolvidos). Falta a prova operacional de que o mesmo evento/conteúdo
sobrevive a 2 execuções e a um novo deployment (criar volume + marcador).
```
