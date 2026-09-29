# StockWin

Scanner técnico diário — ações da watchlist, alertas para o Discord quando há
rutura de range confirmada por volume, ou estabilização perto de suporte.
Não executa ordens, não decide sozinho — só avisa. A validação da tese
(fundamentais, notícias, insider buying) continua a ser feita a trazer o
ticker à conversa com o Claude.

## Como pôr a funcionar (5 passos)

1. **Cria um repositório novo no GitHub** (ex: `stockwin`), público ou privado
   — tanto faz para isto funcionar.
2. **Copia estes 3 ficheiros** para o repositório, mantendo a mesma estrutura
   de pastas:
   - `stockwin.py`
   - `.github/workflows/stockwin.yml`
   - `README.md` (este ficheiro, opcional)
3. **Cria o webhook do Discord**, se ainda não tiveres:
   Definições do canal → Integrações → Webhooks → Criar Webhook → copiar URL.
4. **Guarda o URL do webhook como "secret" do repositório** (nunca no
   código, por segurança):
   No GitHub → Settings do repositório → Secrets and variables → Actions →
   New repository secret → nome `DISCORD_WEBHOOK_URL`, valor = o URL copiado.
5. **Testa manualmente**: separador "Actions" do repositório → "StockWin
   diário" → "Run workflow" → confirma. Depois de correr, vê os logs — se
   houver sinais, deves receber o alerta no Discord em segundos.

Depois disto, corre sozinho todos os dias úteis às 22:00 UTC (depois do
fecho de Wall Street), sem precisares de fazer mais nada.

## Editar a watchlist

Abre `stockwin.py`, procura a lista `WATCHLIST` perto do topo, e adiciona ou
remove símbolos (o mesmo símbolo que usarias a pesquisar no Yahoo Finance).

## Ajustar sensibilidade

Também no topo do `stockwin.py`:
- `BREAKOUT_LOOKBACK_DAYS` — quantos dias definem o "máximo recente" a
  romper (mais alto = ruturas mais raras e mais significativas)
- `BREAKOUT_VOLUME_MULT` — quanto o volume tem de exceder a média (mais
  alto = menos falsos positivos, mas também menos alertas)
- `SUPPORT_LOOKBACK_DAYS` / `SUPPORT_NO_NEW_LOW_DAYS` — o mesmo princípio
  para o sinal de "suporte a segurar"

## Limitações a saber

- Os dados vêm do Yahoo Finance via `yfinance` — gratuito, mas sem garantia
  de disponibilidade contínua (se a Yahoo mudar algo, pode parar de
  funcionar sem aviso; verifica os logs do Actions de vez em quando).
- Deteta padrões mecânicos, não julga qualidade da tese. Vai gerar alguns
  falsos positivos — isso é esperado, a validação humana (ou com o Claude)
  continua a ser o filtro final antes de qualquer decisão de compra.
- Não inclui, para já, deteção automática de insider buying (Form 4 da
  SEC) — pode ser adicionado depois, se fizer sentido.
