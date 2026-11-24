# Entrega Via Verde para o Desk

Preparada em 24/11/2026. Desligada por defeito. As variáveis Yescapa existentes não mudam.

## Configuração

- `VV_HAVEN_ENABLED=true`, `VV_HAVEN_URL=https://<origem-do-Desk>/api/integracoes/via-verde`.
- `VV_HAVEN_TOKEN`: credencial dedicada de 43–128 caracteres base64url, igual a `VIA_VERDE_INGEST_SECRET` no Desk. Nunca reutilizar o token Yescapa.
- No Desk: `VIA_VERDE_INGEST_ENABLED=true` e a credencial acima, ambos em configuração privada.
- A fila usa `HAVEN_QUEUE_DIR/via_verde_fila.json`, dentro do volume persistente `RAILWAY_VOLUME_MOUNT_PATH` já exigido pelo robô.
- A conta Via Verde tem de conseguir autenticar-se. A recusa é agora um erro, nunca uma recolha com zero passagens.

Cada ciclo do robô tenta reenviar os eventos pendentes. A recolha segue os horários existentes em `run.py`. Uma recolha só declara cobertura completa depois de consultar todas as matrículas, aplicar ambos os filtros e terminar a paginação. Uma falha não estabelece prova de atualização.

O evento é guardado antes do envio. Repetições mantêm o mesmo identificador e conteúdo; resposta ambígua conserva o evento. Não se seguem redirecionamentos do endpoint. Os registos não mostram credenciais nem o corpo de pedidos/respostas.

Uma passagem usa matrícula, data, hora e local como identidade; uma alteração de valor conserva a identidade e é recusada pelo Desk para reconciliação, evitando criar outra cobrança. A folha passa também a distinguir horas diferentes e preserva `incluir`/`notas`. Linhas históricas sem hora não são adivinhadas.

## Validação e limite

`python -m unittest test_via_verde_delivery.py`: 10 testes sintéticos sem rede. `python test_haven_emitter.py`: 72 verificações, sem falhas, da fila Yescapa que continua a usar o campo anterior. Compilação dos quatro módulos alterados aprovada.

A validação com movimentos reais continua necessária. Em 24/11 o login existente foi recusado pelo portal; não se afirma que os seletores da área autenticada, a recuperação histórica ou a entrega real estejam validados. Depois de corrigir o acesso, conferir uma reserva conhecida e o recibo no Desk antes de utilizar as cauções. As portagens estrangeiras podem exigir confronto com o extrato faturado da Via Verde.
