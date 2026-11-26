# -*- coding: utf-8 -*-
"""Configuração do navegador e esperas compartilhadas pelas ferramentas (receitas.py e gravar_pagina.py).

As receitas rodam num perfil persistente do Playwright: o login feito uma vez no navegador fica salvo no perfil,
e nenhuma senha, cookie ou token passa por este código.

Variáveis de ambiente:
  TOOLKIT_CANAL   navegador do Playwright: msedge (padrão), chrome ou "" para o Chromium embutido
  TOOLKIT_DADOS   pasta onde os arquivos baixados são salvos (padrão: ./dados)
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

PROFILE = Path.home() / "playwright_profiles" / "toolkit_receitas"
CANAL = os.environ.get("TOOLKIT_CANAL", "msedge") or None
TIMEOUT = 60_000          # espera por elementos da página (ms)
DLD_TIMEOUT = 240_000     # espera pelo download: relatórios grandes demoram a começar (ms)
CARGA_S = 180             # tabela grande ainda carregando: espera até esse tempo (s)
LOGIN_WAIT_MS = 300_000   # tempo para a pessoa concluir o login quando a página pede
INTERACTIVE_LOGIN = True  # com o navegador visível, espera o login; sem tela, falha com aviso
MAPA = None               # mapa_rede.Mapa quando a rodada também mapeia a rede


def pasta_padrao() -> Path:
    return Path(os.environ.get("TOOLKIT_DADOS") or Path.cwd() / "dados")


def carregando(page) -> bool:
    """Há indicador de carregamento visível (MUI, DataGrid, spinners, camadas 'loading')?"""
    try:
        return bool(page.evaluate("""() => [...document.querySelectorAll(
          '[role="progressbar"], .MuiCircularProgress-root, .MuiLinearProgress-root, .MuiSkeleton-root, .spinner-border, .spinner-grow, .loading, #loading, .loader, .blockUI, .dataTables_processing')]
          .some(e => e.offsetParent !== null && getComputedStyle(e).visibility !== 'hidden' && getComputedStyle(e).display !== 'none' && getComputedStyle(e).opacity !== '0')"""))
    except Exception:  # noqa: BLE001  (página navegando ou fechada: não está carregando)
        return False


def esperar_carga(page, limite_s: int = CARGA_S):
    """Enquanto a página mostra que está carregando, espera. Sem isso, a tela de carregamento parece tabela vazia
    e o clique em Exportar é barrado pela camada de carregamento."""
    fim = time.time() + limite_s
    while carregando(page) and time.time() < fim:
        page.wait_for_timeout(500)


def load(page, url: str):
    """Abre o endereço e, se cair numa tela de login, espera a pessoa entrar (a sessão fica no perfil)."""
    from playwright.sync_api import TimeoutError as PWTimeout  # noqa: PLC0415

    page.goto(url, wait_until="domcontentloaded", timeout=TIMEOUT)
    try:
        page.wait_for_load_state("networkidle", timeout=15_000)
    except PWTimeout:
        pass

    def login_visible():
        pwd = page.locator('input[type="password"]')
        return "login" in page.url.lower() or (pwd.count() > 0 and pwd.first.is_visible())

    if login_visible():
        if not INTERACTIVE_LOGIN:
            raise RuntimeError("LOGIN_NECESSARIO: rode com o navegador visível")
        logging.warning("LOGIN NECESSARIO | conclua o login no navegador. Aguardando até %d minutos...", LOGIN_WAIT_MS // 60_000)
        limite = time.time() + LOGIN_WAIT_MS / 1000
        while time.time() < limite:
            page.wait_for_timeout(1000)
            if not login_visible():
                try:
                    page.wait_for_load_state("networkidle", timeout=15_000)
                except PWTimeout:
                    pass
                logging.info("LOGIN CONCLUIDO | sessão salva no perfil persistente")
                return
        raise RuntimeError("LOGIN_NAO_CONCLUIDO: tempo limite excedido")
