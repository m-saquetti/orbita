# -*- coding: utf-8 -*-
"""Gravar uma página: o "F12 > Rede" do DevTools, sem expor a sua sessão.

Abre o Edge no endereço que você der (perfil próprio da gravação: no primeiro uso, entre com o seu login
normalmente). Você navega e clica no download como sempre faz; quando fechar o Edge, fica gravado em
dados\\_logs\\gravacao_<nome>_AAAAMMDD_HHMMSS.md (para ler) e .json (completo):

- o que a página pediu ao servidor: endereços, filtros, tipo e forma das respostas (como a aba Rede do DevTools);
- de onde veio cada arquivo baixado (pronto do servidor ou montado na página) e qual campo virou qual coluna;
- os passos até o download: o que foi clicado (texto do botão, rótulo, id), o que foi escolhido nas listas e em
  que página/quadro, para o extrator repetir sozinho.

Os arquivos baixados ficam em dados\\_gravacoes\\<nome>\\ para você conferir (não precisa mandar).
O que NÃO é gravado: cookies, tokens, senhas, o seu login, o que se digita nos campos (só que houve digitação e
quantos caracteres) e os valores dos dados. Diferente do HAR do DevTools, o resultado pode ser enviado.

Uso: python gravar_pagina.py                          (pergunta o endereço e um nome curto)
     python gravar_pagina.py "https://..." --nome relatorio_vendas"""
from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path
from types import SimpleNamespace

from playwright.sync_api import Error as PWError
from playwright.sync_api import sync_playwright

AQUI = Path(__file__).resolve().parent
sys.path.insert(0, str(AQUI))
import mapa_rede  # noqa: E402

PERFIL = Path.home() / "playwright_profiles" / "toolkit_gravacoes"  # separado do extrator: dá para gravar com a extração rodando

# Anota cada passo e manda para o Python. Roda em todas as páginas e quadros (iframes, como o do Power BI).
# Do que se digita, só o tamanho; de senha, nada.
JS_PASSOS = r"""
(() => {
  if (window.__orbitaGravando) return;
  window.__orbitaGravando = true;
  const curto = (s, n = 90) => (s || '').replace(/\s+/g, ' ').trim().slice(0, n);
  const ruido = c => /^(css-|jss|sc-|Mui[A-Za-z]*-(focused|focusVisible|active|disabled|selected|expanded)|active|hover|focus|selected|show|open|ng-)/.test(c);
  const caminho = el => {
    const p = [];
    for (let e = el, i = 0; e && e.nodeType === 1 && i < 6; e = e.parentElement, i++) {
      let s = e.tagName.toLowerCase();
      if (e.id && !/\d{4,}|:r\w+:|^[a-f0-9-]{16,}$/i.test(e.id)) { p.unshift(s + '#' + e.id); break; }
      const c = [...e.classList].filter(c => !ruido(c)).slice(0, 2);
      if (c.length) s += '.' + c.join('.');
      const tid = e.getAttribute('data-testid'); if (tid) s += `[data-testid="${tid}"]`;
      p.unshift(s);
    }
    return p.join(' > ');
  };
  const clicavel = el => el.closest('button, a, [role=button], [role=menuitem], [role=menuitemcheckbox], [role=tab], [role=option], [role=checkbox], [role=switch], [role=gridcell], [role=columnheader], input, select, label, summary, [onclick], [data-testid]') || el;
  const descrever = (el, acao) => ({
    acao, tag: el.tagName.toLowerCase(), papel: el.getAttribute('role') || '', tipo: el.getAttribute('type') || '',
    id: el.id || '', nome: el.getAttribute('name') || '', rotulo: curto(el.getAttribute('aria-label')), titulo: curto(el.getAttribute('title')),
    testid: el.getAttribute('data-testid') || '', classes: [...el.classList].filter(c => !ruido(c)).slice(0, 4).join(' '),
    texto: /^(input|textarea|select)$/i.test(el.tagName) ? '' : curto(el.innerText || el.textContent),
    href: el.tagName === 'A' ? el.href : '', caminho: caminho(el),
  });
  const enviar = d => { try { window.__orbitaGravar(d); } catch (_) {} };
  document.addEventListener('click', e => { if (e.isTrusted) enviar(descrever(clicavel(e.target), 'clique')); }, true);
  document.addEventListener('change', e => {
    const el = e.target;
    if (!el || !el.tagName) return;
    if (el.tagName === 'SELECT') { const d = descrever(el, 'escolheu'); d.escolha = curto([...el.selectedOptions].map(o => o.text).join(', ')); return enviar(d); }
    if (!e.isTrusted) return;
    if (el.type === 'password') return enviar({ acao: 'senha', tag: 'input', caminho: caminho(el) });
    if (/^(checkbox|radio)$/.test(el.type)) { const d = descrever(el, el.checked ? 'marcou' : 'desmarcou'); return enviar(d); }
    if (/^(INPUT|TEXTAREA)$/.test(el.tagName)) { const d = descrever(el, 'digitou'); d.caracteres = (el.value || '').length; return enviar(d); }
  }, true);
})();
"""


def nome_seguro(s: str, padrao="pagina") -> str:
    s = re.sub(r"[^\w\-]+", "_", s.strip(), flags=re.UNICODE).strip("_")
    return s[:40] or padrao


def sugerir_nome(url: str) -> str:
    h, c, _ = mapa_rede.endereco(url)
    partes = [p for p in c.split("/") if p and not p.startswith("{")]
    return nome_seguro("_".join(partes[-2:]) or h.split(".")[0])


def gravar(url: str, nome: str, base: Path, canal="msedge", headless=False, roteiro=None, perfil=PERFIL):
    """Abre a página e grava até o Edge ser fechado. 'roteiro' (só nos testes) faz os cliques no lugar da pessoa."""
    logs = base / "_logs"
    pasta_dl = base / "_gravacoes" / nome
    pasta_dl.mkdir(parents=True, exist_ok=True)
    mapa = mapa_rede.Mapa(logs, prefixo=f"gravacao_{nome}", titulo=f"Gravação de página · {nome}")
    job = SimpleNamespace(key=nome, group="Gravação", name=mapa_rede.endereco(url)[0], url=url, kind="gravacao")
    with sync_playwright() as p:
        try:
            ctx = p.chromium.launch_persistent_context(str(perfil), channel=canal, headless=headless, accept_downloads=True, no_viewport=True, args=["--start-maximized"])
        except PWError as e:
            msg = str(e).splitlines()[0]
            if "already in use" in msg or "ProcessSingleton" in msg or "lock" in msg.lower():
                print("\nO Edge da gravação já está aberto em outra janela. Feche-o e rode de novo.")
            else:
                print(f"\nNão consegui abrir o Edge: {msg}")
            return None
        mapa.ligar(ctx)
        mapa.comecar(job)

        def passo(fonte, info):
            mapa.clique(info or {}, (fonte.get("frame").url if fonte and fonte.get("frame") else ""))

        ctx.expose_binding("__orbitaGravar", passo)
        ctx.add_init_script(JS_PASSOS)

        def ao_baixar(dl):
            alvo = pasta_dl / (dl.suggested_filename or "download")
            k = 2
            while alvo.exists():
                alvo = pasta_dl / f"{Path(dl.suggested_filename).stem}_{k}{Path(dl.suggested_filename).suffix}"
                k += 1
            try:
                dl.save_as(alvo)
            except PWError as e:
                print(f"  download não concluído: {str(e).splitlines()[0][:90]}")
                return
            mapa.clique({"acao": "baixou", "texto": alvo.suffix.lstrip(".").upper() + " " + mapa_rede._kb(alvo.stat().st_size)})
            mapa.marcar_pagina(dl.page)
            mapa.arquivo(dl, alvo)
            arqs = mapa.itens[nome]["arquivos"]
            print(f"  baixado: {alvo.name} -> {arqs[-1].get('veredito', '') if arqs else ''}")
            mapa.gravar()

        def ao_navegar(page, frame):
            if frame == page.main_frame and frame.url.startswith("http"):
                mapa.clique({"acao": "abriu", "href": frame.url})

        def ligar_pagina(page):
            page.on("download", ao_baixar)
            page.on("websocket", mapa._websocket)
            page.on("framenavigated", lambda fr: ao_navegar(page, fr))

        for pg in ctx.pages:
            ligar_pagina(pg)
        ctx.on("page", ligar_pagina)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        print(f"\nAbrindo {url}\nNo Edge: entre com o seu login se pedir, navegue e clique no download como sempre.\n"
              "Quando terminar, FECHE O EDGE para gravar (ou Ctrl+C aqui).\n")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=120000)
        except PWError as e:
            print(f"  aviso: a página demorou para abrir ({str(e).splitlines()[0][:80]}); siga no Edge.")
        if roteiro is not None:
            roteiro(page)
            for pg in list(ctx.pages):
                pg.close()
        # espera a pessoa fechar o Edge (todas as janelas); o Edge pode continuar rodando em segundo plano,
        # por isso a conta é pelas janelas abertas, não pelo fim do processo
        while True:
            abertas = [pg for pg in ctx.pages if not pg.is_closed()]
            if not abertas:
                break
            try:
                abertas[0].wait_for_timeout(500)
            except PWError:
                continue
            except KeyboardInterrupt:
                print("\nEncerrando a gravação...")
                break
        mapa.terminar(job, "GRAVADO", None)
        try:
            ctx.close()
        except Exception:  # noqa: BLE001
            pass
    return mapa


def main():
    from navegador import pasta_padrao  # noqa: PLC0415
    ap = argparse.ArgumentParser(description="Grava o que uma página pede ao servidor e os passos até o download (sem cookies, tokens, login nem valores).")
    ap.add_argument("url", nargs="?", help="endereço da página")
    ap.add_argument("--nome", help="nome curto da gravação (ex.: relatorio_vendas)")
    ap.add_argument("--saida", help="pasta de dados (padrão: ./dados ou TOOLKIT_DADOS)")
    a = ap.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    base = Path(a.saida) if a.saida else pasta_padrao()
    url, nome = a.url, a.nome
    feitos = []
    while True:
        if not url:
            url = input("Endereço da página (Enter vazio para sair): ").strip().strip('"')
            if not url:
                break
        if not re.match(r"^https?://", url, re.I):
            url = "https://" + url
        if not nome:
            sug = sugerir_nome(url)
            nome = nome_seguro(input(f"Nome curto para esta gravação [{sug}]: ") or sug)
        nome = nome_seguro(nome)
        m = gravar(url, nome, base)
        if m is not None:
            js, md = m.gravar()
            feitos += [md, js]
            print(f"\nGravação pronta:\n  {md}\n  {js}\nArquivos baixados em {base / '_gravacoes' / nome}\n")
        if a.url:
            break
        url = nome = None
        if input("Gravar outra página? (s/N): ").strip().lower() not in ("s", "sim", "y"):
            break
    if feitos:
        print("Mande os arquivos de gravação (.md e .json): não têm cookies, tokens, login nem valores dos dados.")


if __name__ == "__main__":
    main()
