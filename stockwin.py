"""
StockWin — scanner técnico diário para uma watchlist de ações/ETFs, com alertas
enviados para um canal do Discord via webhook.

O QUE ISTO FAZ:
  - Descarrega OHLCV diário (Yahoo Finance, via yfinance — gratuito).
  - Deteta dois padrões mecânicos por ticker:
      1) RUTURA (breakout): fecho de hoje > máximo dos últimos N dias
         anteriores, confirmado com volume acima da média.
      2) SUPORTE A SEGURAR (support hold): preço perto da mínima recente,
         mas sem nova mínima nos últimos M dias (sinal de estabilização).
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
import json
import datetime as dt
from dataclasses import dataclass

import yfinance as yf
import requests

# ---------------------------------------------------------------------------
# CONFIGURAÇÃO
# ---------------------------------------------------------------------------

# Watchlist inicial — os candidatos já analisados na conversa, mais espaço
# para adicionares outros. Usa o símbolo tal como aparece no Yahoo Finance
# (ex: ações normais = símbolo direto; ETFs europeus podem precisar de
# sufixo, ex: ".DE" para Xetra — verificar antes de adicionar).
WATCHLIST = [
    "ALNY",   # Alnylam Pharmaceuticals
    "IONQ",   # IonQ
    "SAIL",   # SailPoint
    "UBER",   # Uber Technologies
]

BREAKOUT_LOOKBACK_DAYS = 20      # janela para definir "máximo recente"
BREAKOUT_VOLUME_MULT = 1.5       # volume tem de ser > 1.5x a média para contar
SUPPORT_LOOKBACK_DAYS = 15       # janela para definir "mínima recente"
SUPPORT_NO_NEW_LOW_DAYS = 3      # dias seguidos sem nova mínima = estabilização

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")


@dataclass
class Signal:
    ticker: str
    kind: str  # "BREAKOUT" ou "SUPPORT_HOLD"
    price: float
    detail: str


def fetch_history(ticker: str, days: int = 60):
    """Descarrega OHLCV diário dos últimos `days` dias via Yahoo Finance."""
    df = yf.download(ticker, period=f"{days}d", interval="1d", progress=False, auto_adjust=False)
    if df is None or df.empty:
        return None
    return df


def check_breakout(ticker: str, df) -> Signal | None:
    if len(df) < BREAKOUT_LOOKBACK_DAYS + 2:
        return None
    closes = df["Close"]
    volumes = df["Volume"]
    today_close = float(closes.iloc[-1])
    today_volume = float(volumes.iloc[-1])

    window_high = float(closes.iloc[-(BREAKOUT_LOOKBACK_DAYS + 1):-1].max())
    avg_volume = float(volumes.iloc[-(BREAKOUT_LOOKBACK_DAYS + 1):-1].mean())

    if today_close > window_high and today_volume > BREAKOUT_VOLUME_MULT * avg_volume:
        return Signal(
            ticker=ticker,
            kind="BREAKOUT",
            price=today_close,
            detail=(
                f"Fechou a {today_close:.2f}, acima do máximo dos últimos "
                f"{BREAKOUT_LOOKBACK_DAYS} dias ({window_high:.2f}), com volume "
                f"{today_volume/avg_volume:.1f}x a média."
            ),
        )
    return None


def check_support_hold(ticker: str, df) -> Signal | None:
    if len(df) < SUPPORT_LOOKBACK_DAYS + SUPPORT_NO_NEW_LOW_DAYS:
        return None
    lows = df["Low"]
    closes = df["Close"]
    today_close = float(closes.iloc[-1])

    window_low = float(lows.iloc[-SUPPORT_LOOKBACK_DAYS:].min())
    recent_lows = lows.iloc[-SUPPORT_NO_NEW_LOW_DAYS:]
    made_new_low_recently = bool((recent_lows <= window_low + 1e-9).any() and
                                  recent_lows.idxmin() == lows.iloc[-SUPPORT_LOOKBACK_DAYS:].idxmin())

    # perto da mínima (dentro de 3%) mas sem fazer nova mínima nos últimos N dias
    near_support = today_close <= window_low * 1.03
    no_new_low = float(recent_lows.min()) > window_low * 0.999  # tolerância numérica

    if near_support and no_new_low:
        return Signal(
            ticker=ticker,
            kind="SUPPORT_HOLD",
            price=today_close,
            detail=(
                f"Perto do suporte dos últimos {SUPPORT_LOOKBACK_DAYS} dias "
                f"({window_low:.2f}), sem nova mínima nos últimos "
                f"{SUPPORT_NO_NEW_LOW_DAYS} dias — possível estabilização."
            ),
        )
    return None


def scan() -> list[Signal]:
    signals: list[Signal] = []
    for ticker in WATCHLIST:
        try:
            df = fetch_history(ticker)
        except Exception as e:
            print(f"[aviso] falhou a descarregar {ticker}: {e}", file=sys.stderr)
            continue
        if df is None:
            print(f"[aviso] sem dados para {ticker}", file=sys.stderr)
            continue
        for check in (check_breakout, check_support_hold):
            sig = check(ticker, df)
            if sig:
                signals.append(sig)
    return signals


def send_discord_alert(signals: list[Signal]):
    if not DISCORD_WEBHOOK_URL:
        print("[erro] DISCORD_WEBHOOK_URL não está definido — a saltar envio.", file=sys.stderr)
        return
    if not signals:
        return

    lines = [f"**StockWin — {dt.date.today().isoformat()}**", ""]
    for s in signals:
        emoji = "🚀" if s.kind == "BREAKOUT" else "🛡️"
        label = "Rutura confirmada" if s.kind == "BREAKOUT" else "Suporte a segurar"
        lines.append(f"{emoji} **{s.ticker}** — {label}")
        lines.append(f"   {s.detail}")
        lines.append("")

    lines.append(
        "_Isto é um alerta mecânico, não uma recomendação. Traz o ticker à "
        "conversa para validação antes de decidir._"
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
        print(f"{s.ticker}: {s.kind} — {s.detail}")
    send_discord_alert(signals)


if __name__ == "__main__":
    main()
