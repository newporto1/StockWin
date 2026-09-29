"""
StockWin — scanner técnico diário sobre um universo largo de ações (S&P 500),
com alertas enviados para um canal do Discord via webhook.

O QUE ISTO FAZ:
  - Obtém a lista atual do S&P 500 (fonte pública, gratuita — GitHub), para
    varrer um universo largo em vez de uma lista fixa de tickers.
  - Descarrega OHLCV diário de todos eles de uma vez (Yahoo Finance, via
    yfinance — gratuito).
  - Deteta dois padrões mecânicos CONFIRMADOS (não de um único dia) por
    ticker:
      1) RUTURA (breakout): fechos SEGUIDOS acima do máximo recente,
         confirmados com volume acima da média.
      2) SUPORTE A SEGURAR (support hold): mínima feita há alguns dias,
         sem nova mínima desde então (estabilização real, não de 1 dia).
  - Exclui os tickers já comprados (EXCLUDE_TICKERS) — esses já têm stop/TP
    próprios e são geridos à parte, não faz sentido StockWin alertar sobre
    eles como "nova oportunidade".
  - Manda um alerta para o Discord quando uma das condições bate certo.

O QUE ISTO NÃO FAZ (por desenho, não por limitação técnica):
  - Não decide se a rutura ou o suporte são "boa oportunidade" — isso exige
    ler contexto (notícias, insider buying, tese fundamental), que é o
    trabalho que continua a ser feito manualmente na conversa com o Claude,
    depois do alerta chegar.
  - Não executa ordens. Só avisa.

USO:
  python stockwin.py
  (as variáveis de configuração estão logo a seguir; o webhook vem de uma
  variável de ambiente DISCORD_WEBHOOK_URL, nunca escrita no código)
"""
from __future__ import annotations
import os
import sys
import datetime as dt
from dataclasses import dataclass

import pandas as pd
import yfinance as yf
import requests

# ---------------------------------------------------------------------------
# CONFIGURAÇÃO
# ---------------------------------------------------------------------------

# Tickers já comprados, geridos à parte com o próprio stop-loss/take-profit —
# excluídos da procura de NOVAS oportunidades para não gerar ruído.
EXCLUDE_TICKERS = {"ALNY", "IONQ", "SAIL", "UBER"}

# Símbolos extra fora do S&P 500 (ex: candidatos mais especulativos/recentes,
# ou apostas que já tens noutra conta) vêm de extra_tickers.txt, no mesmo
# repositório — um ticker por linha. Para adicionar um novo, edita SÓ esse
# ficheiro de texto; não precisas de voltar a colar este script.
EXTRA_TICKERS_FILE = "extra_tickers.txt"

# Lista de reserva, só usada se a obtenção do S&P 500 falhar (ex: sem rede).
# Um punhado de nomes grandes e líquidos de vários setores, para o scanner
# não ficar completamente às escuras nesse dia.
FALLBACK_UNIVERSE = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA", "AMD", "INTC",
    "JPM", "V", "MA", "UNH", "JNJ", "PFE", "MRK", "ABBV", "XOM", "CVX",
    "WMT", "COST", "HD", "MCD", "NKE", "DIS", "BA", "CAT", "GE", "CRM", "ADBE",
]

BREAKOUT_LOOKBACK_DAYS = 20       # janela para definir "máximo recente"
BREAKOUT_VOLUME_MULT = 1.5        # volume tem de ser > 1.5x a média no dia da rutura
BREAKOUT_CONFIRM_DAYS = 2         # nº de fechos SEGUIDOS acima do nível para confirmar
BREAKOUT_TARGET_PCT = 0.08        # alvo técnico por omissão: +8% desde a entrada
BREAKOUT_DURATION = "1 a 3 semanas"   # janela típica para ruturas se resolverem

SUPPORT_LOOKBACK_DAYS = 15        # janela para definir "mínima recente"
SUPPORT_CONFIRM_DAYS = 3          # nº de dias sem nova mínima para confirmar estabilização
SUPPORT_NEAR_PCT = 0.03           # "perto do suporte" = dentro de 3% da mínima
SUPPORT_STOP_BUFFER_PCT = 0.02    # stop fica 2% abaixo do suporte, não exatamente em cima
SUPPORT_TARGET_PCT = 0.08         # alvo técnico por omissão: +8% desde a entrada
SUPPORT_DURATION = "3 a 6 semanas"    # janela típica para consolidações se resolverem

MAX_ALERTS_PER_RUN = 15           # limite de sinais por mensagem, para não sobrecarregar o Discord

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

SP500_SOURCE_URL = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"


@dataclass
class Signal:
    ticker: str
    kind: str          # "BREAKOUT" ou "SUPPORT_HOLD"
    entry: float        # preço de referência para entrada (fecho do dia do sinal)
    stop_loss: float
    take_profit: float
    duration: str        # intervalo previsto, em texto (ex: "1 a 3 semanas")
    rationale: str        # explicação TÉCNICA automática — não é pesquisa fundamentada


def load_extra_tickers() -> list[str]:
    """Lê extra_tickers.txt (um símbolo por linha; # inicia comentário)."""
    if not os.path.exists(EXTRA_TICKERS_FILE):
        return []
    tickers = []
    with open(EXTRA_TICKERS_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            tickers.append(line.upper())
    return tickers


def fetch_universe() -> list[str]:
    """
    Lista de tickers a varrer: constituintes atuais do S&P 500 (fonte pública,
    gratuita), mais os tickers de extra_tickers.txt, menos EXCLUDE_TICKERS.
    Se a obtenção da lista do S&P 500 falhar (ex: sem rede, fonte em baixo),
    usa FALLBACK_UNIVERSE em vez de o scan falhar por completo.
    """
    try:
        df = pd.read_csv(SP500_SOURCE_URL)
        tickers = df["Symbol"].astype(str).str.replace(".", "-", regex=False).tolist()
    except Exception as e:
        print(f"[aviso] falhou a obter a lista do S&P 500 ({e}) — a usar lista de reserva.", file=sys.stderr)
        tickers = list(FALLBACK_UNIVERSE)

    extra = load_extra_tickers()
    universe = sorted((set(tickers) | set(extra)) - EXCLUDE_TICKERS)
    return universe


def fetch_universe_history(tickers: list[str], days: int = 60):
    """Descarrega OHLCV diário de TODOS os tickers de uma vez (muito mais
    eficiente do que um pedido por ticker para um universo largo)."""
    data = yf.download(
        tickers, period=f"{days}d", interval="1d", group_by="ticker",
        progress=False, auto_adjust=False, threads=True,
    )
    return data


def extract_ticker_df(data, ticker: str):
    """Isola o OHLCV de um ticker a partir do resultado de fetch_universe_history."""
    if isinstance(data.columns, pd.MultiIndex):
        if ticker not in data.columns.get_level_values(0):
            return None
        df = data[ticker]
    else:
        # só acontece se o universo tiver 1 único ticker
        df = data
    df = df.dropna(how="all")
    if df.empty or "Close" not in df.columns or df["Close"].dropna().empty:
        return None
    return df


def check_breakout(ticker: str, df) -> Signal | None:
    """
    Rutura CONFIRMADA: exige BREAKOUT_CONFIRM_DAYS fechos seguidos acima do
    máximo anterior (não só o de hoje), com o pico de volume a acontecer
    nalgum desses dias de confirmação. Isto evita disparar num único dia de
    pico que depois reverte (o problema que já vimos manualmente com a INTC).
    """
    needed = BREAKOUT_LOOKBACK_DAYS + BREAKOUT_CONFIRM_DAYS + 1
    if len(df) < needed:
        return None
    closes = df["Close"]
    volumes = df["Volume"]

    # janela "antes" da rutura, para definir o nível a romper
    pre_closes = closes.iloc[-(BREAKOUT_LOOKBACK_DAYS + BREAKOUT_CONFIRM_DAYS):-BREAKOUT_CONFIRM_DAYS]
    pre_volumes = volumes.iloc[-(BREAKOUT_LOOKBACK_DAYS + BREAKOUT_CONFIRM_DAYS):-BREAKOUT_CONFIRM_DAYS]
    window_high = float(pre_closes.max())
    avg_volume = float(pre_volumes.mean())

    # os últimos BREAKOUT_CONFIRM_DAYS dias, que têm de confirmar a rutura
    confirm_closes = closes.iloc[-BREAKOUT_CONFIRM_DAYS:]
    confirm_volumes = volumes.iloc[-BREAKOUT_CONFIRM_DAYS:]
    today_close = float(confirm_closes.iloc[-1])

    all_above = bool((confirm_closes > window_high).all())
    had_volume_spike = bool((confirm_volumes > BREAKOUT_VOLUME_MULT * avg_volume).any())

    if all_above and had_volume_spike:
        entry = today_close
        stop_loss = window_high  # antiga resistência passa a suporte
        take_profit = entry * (1 + BREAKOUT_TARGET_PCT)
        return Signal(
            ticker=ticker,
            kind="BREAKOUT",
            entry=entry,
            stop_loss=stop_loss,
            take_profit=take_profit,
            duration=BREAKOUT_DURATION,
            rationale=(
                f"{BREAKOUT_CONFIRM_DAYS} fechos seguidos acima do máximo dos "
                f"{BREAKOUT_LOOKBACK_DAYS} dias anteriores ({window_high:.2f}), com "
                f"volume confirmado (pico > {BREAKOUT_VOLUME_MULT}x a média) — nível "
                f"antigo de resistência passa a suporte."
            ),
        )
    return None


def check_support_hold(ticker: str, df) -> Signal | None:
    """
    Suporte CONFIRMADO: a mínima da janela tem de ter acontecido ANTES dos
    últimos SUPPORT_CONFIRM_DAYS dias — ou seja, exige que já se passaram
    esses dias todos SEM nova mínima, não apenas o dia em que a mínima foi
    feita. É a correção direta ao caso da UBER (alertava com só 1 dia).
    """
    needed = SUPPORT_LOOKBACK_DAYS + SUPPORT_CONFIRM_DAYS
    if len(df) < needed:
        return None
    lows = df["Low"]
    closes = df["Close"]
    today_close = float(closes.iloc[-1])

    # mínima definida ANTES da janela de confirmação
    pre_lows = lows.iloc[-(SUPPORT_LOOKBACK_DAYS + SUPPORT_CONFIRM_DAYS):-SUPPORT_CONFIRM_DAYS]
    window_low = float(pre_lows.min())

    # nos últimos SUPPORT_CONFIRM_DAYS dias, não pode ter havido nova mínima
    confirm_lows = lows.iloc[-SUPPORT_CONFIRM_DAYS:]
    no_new_low = float(confirm_lows.min()) > window_low - 1e-9

    # e o preço ainda tem de estar perto do suporte (não fugiu para cima)
    near_support = today_close <= window_low * (1 + SUPPORT_NEAR_PCT)

    if no_new_low and near_support:
        entry = today_close
        stop_loss = window_low * (1 - SUPPORT_STOP_BUFFER_PCT)
        take_profit = entry * (1 + SUPPORT_TARGET_PCT)
        return Signal(
            ticker=ticker,
            kind="SUPPORT_HOLD",
            entry=entry,
            stop_loss=stop_loss,
            take_profit=take_profit,
            duration=SUPPORT_DURATION,
            rationale=(
                f"Suporte em {window_low:.2f} confirmado — {SUPPORT_CONFIRM_DAYS} dias "
                f"seguidos sem nova mínima, fecho perto do suporte. Possível zona de "
                f"estabilização, mas sem confirmação fundamental."
            ),
        )
    return None


def scan() -> list[Signal]:
    universe = fetch_universe()
    print(f"A analisar {len(universe)} tickers...")

    try:
        data = fetch_universe_history(universe)
    except Exception as e:
        print(f"[erro] falhou a descarregar o universo: {e}", file=sys.stderr)
        return []

    signals: list[Signal] = []
    for ticker in universe:
        try:
            df = extract_ticker_df(data, ticker)
        except Exception as e:
            print(f"[aviso] falhou a isolar dados de {ticker}: {e}", file=sys.stderr)
            continue
        if df is None:
            continue
        for check in (check_breakout, check_support_hold):
            try:
                sig = check(ticker, df)
            except Exception as e:
                print(f"[aviso] falhou a verificar {ticker}: {e}", file=sys.stderr)
                continue
            if sig:
                signals.append(sig)
    return signals


def send_discord_alert(signals: list[Signal]):
    if not DISCORD_WEBHOOK_URL:
        print("[erro] DISCORD_WEBHOOK_URL não está definido — a saltar envio.", file=sys.stderr)
        return
    if not signals:
        return

    shown = signals[:MAX_ALERTS_PER_RUN]
    omitted = len(signals) - len(shown)

    lines = [f"**StockWin — {dt.date.today().isoformat()}** ({len(signals)} sinal(is))", ""]
    for s in shown:
        emoji = "🚀" if s.kind == "BREAKOUT" else "🛡️"
        label = "Rutura confirmada" if s.kind == "BREAKOUT" else "Suporte a segurar"
        lines.append(f"{emoji} **{s.ticker}** — {label}")
        lines.append(f"   Compra: {s.entry:.2f}")
        lines.append(f"   TP: {s.take_profit:.2f}")
        lines.append(f"   SL: {s.stop_loss:.2f}")
        lines.append(f"   Duração prevista: {s.duration}")
        lines.append(f"   {s.rationale}")
        lines.append("")

    if omitted > 0:
        lines.append(f"_(+{omitted} sinal(is) adicional(is) não mostrados nesta mensagem)_")
        lines.append("")

    lines.append(
        "_TP/SL/duração são estimativas TÉCNICAS automáticas, não pesquisa "
        "fundamentada — traz o ticker à conversa para validar (notícias, "
        "insider buying, tese) antes de decidir._"
    )

    payload = {"content": "\n".join(lines)}
    resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=15)
    if resp.status_code >= 300:
        print(f"[erro] Discord respondeu {resp.status_code}: {resp.text}", file=sys.stderr)


def main():
    signals = scan()
    if not signals:
        print("Nenhum sinal hoje.")
        return
    for s in signals:
        print(f"{s.ticker}: {s.kind} — compra {s.entry:.2f} / TP {s.take_profit:.2f} / SL {s.stop_loss:.2f} / {s.duration}")
    send_discord_alert(signals)


if __name__ == "__main__":
    main()
