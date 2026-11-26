# -*- coding: utf-8 -*-
"""Receitas: a ferramenta aprende uma página nova vendo você baixar uma vez.

Ensinar (python receitas.py): abre o navegador num perfil persistente do Playwright (o login feito uma vez fica salvo),
no endereço que você der. Você faz o caminho até o download como sempre faz (clica, escolhe nas listas, digita as
datas, baixa) e fecha o navegador. Os passos ficam em receitas\\<nome>.json e, dali em diante, python receitas.py
--repetir <nome> baixa essa página sozinho (ou um extrator maior chama executar() como mais um item).

O que a receita guarda: onde clicar (texto do botão, rótulo, id, caminho na página e em que quadro ou aba), o que
escolher nas listas, o que digitar (datas viram relativas: hoje, início do ano, início do mês...) e o nome do arquivo.
O que NÃO guarda: senha, login, nada digitado em página de login, cookies e tokens. O arquivo da receita fica só
no seu computador; o que dá para mandar é a gravação em dados\\_logs\\gravacao_<nome>_*.md (sem valores).

Uso: python receitas.py                                      (pergunta o endereço, o nome e a pasta)
     python receitas.py "https://..." --nome vendas --pasta Vendas
     python receitas.py --lista                              as receitas aprendidas
     python receitas.py --repetir vendas                     baixa de novo, sozinho"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

AQUI = Path(__file__).resolve().parent
sys.path.insert(0, str(AQUI))
PASTA = AQUI / "receitas"
VERSAO = 1

# ---------------------------------------------------------------- datas relativas

RE_DATA = re.compile(r"(?<!\d)(\d{1,4})([./-])(\d{1,2})\2(\d{2,4})(?!\d)")
RE_TOKEN = re.compile(r"\{(hoje(?:[+-]\d+)?|inicio_mes(?:_anterior)?|fim_mes(?:_anterior)?|inicio_ano(?:_anterior)?|fim_ano|ano)(?::([^{}]*))?\}")
NOMES_DIA = {"hoje": "hoje", "inicio_mes": "início do mês", "inicio_mes_anterior": "início do mês passado", "fim_mes": "fim do mês",
             "fim_mes_anterior": "fim do mês passado", "inicio_ano": "início do ano", "inicio_ano_anterior": "início do ano passado", "fim_ano": "fim do ano"}


def _fim_mes(d: date) -> date:
    return (d.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)


def dia_do_token(tok: str, hoje: date) -> date:
    if tok.startswith("hoje"):
        return hoje + timedelta(days=int(tok[4:] or 0))
    ini_mes = hoje.replace(day=1)
    fim_ant = ini_mes - timedelta(days=1)
    return {"inicio_mes": ini_mes, "fim_mes": _fim_mes(hoje), "inicio_mes_anterior": fim_ant.replace(day=1), "fim_mes_anterior": fim_ant,
            "inicio_ano": date(hoje.year, 1, 1), "inicio_ano_anterior": date(hoje.year - 1, 1, 1), "fim_ano": date(hoje.year, 12, 31)}[tok]


def token_do_dia(d: date, hoje: date) -> str:
    """O nome relativo de uma data: o que ela é em relação a hoje (o mesmo dia no ano que vem vira 'hoje-N')."""
    if d == hoje:
        return "hoje"
    for tok in ("inicio_ano", "inicio_mes", "fim_mes_anterior", "inicio_mes_anterior", "fim_mes", "fim_ano", "inicio_ano_anterior"):
        if dia_do_token(tok, hoje) == d:
            return tok
    n = (d - hoje).days
    return f"hoje{n:+d}"


def _leituras(m) -> list[tuple[date, str]]:
    """As formas possíveis de ler uma data escrita (dia/mês ou mês/dia, ano com 2 ou 4 dígitos) e o formato de cada uma."""
    a, sep, b, c = m.group(1), m.group(2), m.group(3), m.group(4)
    out = []
    if len(a) == 4:
        try:
            out.append((date(int(a), int(b), int(c)), f"%Y{sep}%m{sep}%d"))
        except ValueError:
            pass
        return out
    if len(c) not in (2, 4) or len(a) > 2:
        return out
    ano = int(c) + (2000 if len(c) == 2 else 0)
    fa = "%y" if len(c) == 2 else "%Y"
    for dd, mm, fmt in ((a, b, f"%d{sep}%m{sep}{fa}"), (b, a, f"%m{sep}%d{sep}{fa}")):
        try:
            out.append((date(ano, int(mm), int(dd)), fmt))
        except ValueError:
            pass
    return out


def tokenizar(texto: str, hoje: date | None = None) -> str:
    """Troca as datas escritas por datas relativas a hoje: '01/01/2026' em 12/11/2026 vira '{inicio_ano:%d/%m/%Y}'.
    Quando dia/mês e mês/dia são possíveis (11.10.2026), vale a leitura mais perto de hoje."""
    if not texto:
        return texto
    hoje = hoje or date.today()
    if texto.strip() == str(hoje.year):
        return texto.replace(str(hoje.year), "{ano}")

    def troca(m):
        opcoes = [(d, f) for d, f in _leituras(m) if abs((d - hoje).days) <= 800]
        if not opcoes:
            return m.group(0)
        d, f = min(opcoes, key=lambda x: (abs((x[0] - hoje).days), x[1].startswith("%m")))
        return "{" + token_do_dia(d, hoje) + ":" + f + "}"

    return RE_DATA.sub(troca, texto)


def tokenizar_endereco(url: str, hoje: date | None = None) -> str:
    """No endereço, só os filtros mudam com o dia: datas e o ano corrente (?year=2026 vira ?year={ano})."""
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit  # noqa: PLC0415
    hoje = hoje or date.today()
    u = urlsplit(url)
    if not u.query:
        return url
    q = [(k, "{ano}" if v == str(hoje.year) else tokenizar(v, hoje)) for k, v in parse_qsl(u.query, keep_blank_values=True)]
    if all(v == ov for (_, v), (_, ov) in zip(q, parse_qsl(u.query, keep_blank_values=True))):
        return url  # nada mudou: o endereço fica exatamente como foi digitado
    return urlunsplit(u._replace(query=urlencode(q, safe="{}:%/")))


def renderizar(texto, hoje: date | None = None):
    """O contrário: '{inicio_ano:%d/%m/%Y}' vira a data de hoje em diante (01/01 do ano corrente)."""
    if not isinstance(texto, str) or "{" not in texto:
        return texto
    hoje = hoje or date.today()

    def troca(m):
        tok, fmt = m.group(1), m.group(2)
        if tok == "ano":
            return str(hoje.year)
        return dia_do_token(tok, hoje).strftime(fmt or "%d/%m/%Y")

    return RE_TOKEN.sub(troca, texto)


def legivel(texto: str) -> str:
    """'{inicio_ano:%d/%m/%Y}' → 'início do ano' (para mostrar a receita)."""
    def troca(m):
        tok = m.group(1)
        if tok == "ano":
            return "ano corrente"
        if tok.startswith("hoje") and tok != "hoje":
            n = int(tok[4:])
            return f"hoje {'-' if n < 0 else '+'} {abs(n)} dia{'s' if abs(n) > 1 else ''}"
        return NOMES_DIA.get(tok, tok)
    return RE_TOKEN.sub(troca, texto or "")


# ---------------------------------------------------------------- o que se grava na página

# Anota os passos em todas as páginas e quadros (iframes, como o do Power BI) e manda para o Python.
# Digitação: o valor, menos em campo de senha, de login ou dentro de página de login (aí só que houve digitação).
JS_ENSINAR = r"""
(() => {
  if (window.__orbitaEnsinando) return;
  window.__orbitaEnsinando = true;
  const curto = (s, n = 90) => (s || '').replace(/\s+/g, ' ').trim().slice(0, n);
  const ruido = c => /^(css-|jss|sc-|Mui-|Mui[A-Za-z]*-(focused|focusVisible|active|disabled|selected|expanded|checked)|active|hover|focus|selected|show|open|ng-|is-)/.test(c);
  const instavel = id => /\d{4,}|:r\w*:|^[a-f0-9-]{16,}$|^(mui|react|rc|headlessui|radix|downshift)[-_:]/i.test(id);
  const caminho = el => {
    const p = [];
    for (let e = el, i = 0; e && e.nodeType === 1 && i < 6; e = e.parentElement, i++) {
      let s = e.tagName.toLowerCase();
      if (e.id && !instavel(e.id)) { p.unshift(s + '#' + (window.CSS && CSS.escape ? CSS.escape(e.id) : e.id)); break; }
      const c = [...e.classList].filter(c => !ruido(c) && /^[A-Za-z_-][\w-]*$/.test(c)).slice(0, 2);
      if (c.length) s += '.' + c.join('.');
      const tid = e.getAttribute('data-testid'); if (tid && !/["\\]/.test(tid)) s += `[data-testid="${tid}"]`;
      p.unshift(s);
    }
    return p.join(' > ');
  };
  const clicavel = el => el.closest('button, a, [role=button], [role=menuitem], [role=menuitemcheckbox], [role=menuitemradio], [role=tab], [role=option], [role=checkbox], [role=switch], [role=radio], [role=gridcell], [role=columnheader], [role=treeitem], [role=link], [role=combobox], input, select, textarea, label, summary, [onclick], [data-testid]') || el;
  const textoDe = el => /^(input|textarea|select)$/i.test(el.tagName) ? '' : curto(el.innerText || el.textContent);
  const visivel = e => e.offsetParent !== null || e.getClientRects().length > 0;
  const indiceTexto = (el, t) => { if (!t) return 0; const xs = [...document.getElementsByTagName(el.tagName)].filter(e => visivel(e) && curto(e.innerText || e.textContent) === t); const i = xs.indexOf(el); return i < 0 ? 0 : i; };
  const indiceCaminho = (el, c) => { try { const xs = [...document.querySelectorAll(c)].filter(visivel); const i = xs.indexOf(el); return i < 0 ? 0 : i; } catch (_) { return 0; } };
  const paginaDeLogin = () => /login|logon|signin|sign-in|sso|oauth|adfs|saml|microsoftonline|okta/i.test(location.href);
  const sensivel = el => {
    if (/^(password|email)$/i.test(el.type || '')) return true;
    const s = [el.name, el.id, el.getAttribute('autocomplete'), el.getAttribute('aria-label'), el.getAttribute('placeholder')].join(' ');
    return /user|login|logon|usu[aá]rio|senha|passw|e-?mail|matr[ií]cula|cpf|token|secret|otp|c[oó]digo de (acesso|verifica)/i.test(s) || paginaDeLogin();
  };
  const campoDe = el => {
    if (!/^(INPUT|SELECT|TEXTAREA)$/.test(el.tagName) && !/combobox|textbox|listbox|spinbutton|searchbox/.test(el.getAttribute('role') || '')) return '';
    const l = el.labels && el.labels[0];
    if (l) { const x = l.cloneNode(true); x.querySelectorAll('input, select, textarea').forEach(e => e.remove()); return curto(x.textContent, 60); }
    const lb = el.getAttribute('aria-labelledby');
    return lb ? curto(lb.split(/\s+/).map(i => document.getElementById(i)).filter(Boolean).map(e => e.textContent).join(' '), 60) : '';
  };
  const descrever = (el, acao) => {
    const t = textoDe(el), c = caminho(el);
    return { acao, tag: el.tagName.toLowerCase(), papel: el.getAttribute('role') || '', tipo: el.getAttribute('type') || '',
      id: el.id && !instavel(el.id) ? el.id : '', nome: el.getAttribute('name') || '', rotulo: curto(el.getAttribute('aria-label')), titulo: curto(el.getAttribute('title')),
      testid: el.getAttribute('data-testid') || '', placeholder: curto(el.getAttribute('placeholder')), campo: campoDe(el), texto: t, nt: indiceTexto(el, t), caminho: c, n: indiceCaminho(el, c),
      href: el.tagName === 'A' ? (el.getAttribute('href') || '') : '', classes: [...el.classList].filter(c => !ruido(c)).slice(0, 4).join(' '), login: paginaDeLogin() };
  };
  const enviar = d => { try { window.__orbitaEnsinar(d); } catch (_) {} };
  const digitavel = el => el && (el.tagName === 'TEXTAREA' || (el.tagName === 'INPUT' && !/^(checkbox|radio|file|range|color|button|submit|reset|image|hidden)$/i.test(el.type || '')));
  const pend = new Map(), ultimo = new WeakMap();
  const valorDe = el => el.value == null ? '' : String(el.value);
  const digitado = el => {
    clearTimeout(pend.get(el)); pend.delete(el);
    const v = valorDe(el);
    if (ultimo.get(el) === v) return;
    ultimo.set(el, v);
    const d = descrever(el, 'digitou');
    if (sensivel(el)) { d.sensivel = true; d.caracteres = v.length; } else d.valor = v.slice(0, 300);
    enviar(d);
  };
  const descarregar = () => { for (const el of [...pend.keys()]) digitado(el); };
  let rotulo = null, ultimoClique = 0;
  document.addEventListener('input', e => { const el = e.target; if (!digitavel(el)) return; clearTimeout(pend.get(el)); pend.set(el, setTimeout(() => digitado(el), 900)); }, true);
  document.addEventListener('pointerdown', () => descarregar(), true);
  document.addEventListener('keydown', e => {
    const el = e.target;
    if (!e.isTrusted || e.key !== 'Enter' || !digitavel(el)) return;
    descarregar();
    if (ultimo.get(el) !== valorDe(el)) digitado(el);
    const d = descrever(el, 'enter'); if (sensivel(el)) d.sensivel = true; enviar(d);
  }, true);
  document.addEventListener('click', e => {
    if (!e.isTrusted) return;
    const el = clicavel(e.target);
    if (/^(SELECT|OPTION|HTML|BODY)$/.test(el.tagName)) return;
    const agora = performance.now();
    if (el.tagName === 'INPUT' && rotulo && rotulo.alvo === el && agora - rotulo.t < 150) return;  // o clique que o rótulo repassa ao campo
    if (el.tagName === 'LABEL') rotulo = { alvo: el.control, t: agora };
    ultimoClique = agora;
    const d = descrever(el, 'clique');
    if (digitavel(el) && sensivel(el)) d.sensivel = true;  // clicar no campo de login: fica de fora como a digitação
    enviar(d);
  }, true);
  document.addEventListener('change', e => {
    const el = e.target;
    if (!el || !el.tagName) return;
    if (el.tagName === 'SELECT') { const d = descrever(el, 'escolheu'); d.escolha = [...el.selectedOptions].map(o => curto(o.text, 200)); d.valores = [...el.selectedOptions].map(o => o.value); return enviar(d); }
    if (/^(checkbox|radio)$/i.test(el.type || '')) { if (performance.now() - ultimoClique < 400) return; return enviar(descrever(el, el.checked ? 'marcou' : 'desmarcou')); }
    if (digitavel(el)) digitado(el);
  }, true);
})();
"""

CAMPOS_ALVO = ("tag", "papel", "tipo", "id", "nome", "rotulo", "titulo", "testid", "placeholder", "campo", "texto", "nt", "caminho", "n", "href")
ACOES = {"clique": "clicar", "escolheu": "escolher", "digitou": "digitar", "enter": "enter", "marcou": "marcar", "desmarcou": "desmarcar"}


def nome_seguro(s: str, padrao="pagina") -> str:
    s = re.sub(r"[^\w\-]+", "_", (s or "").strip(), flags=re.UNICODE).strip("_")
    return s[:40] or padrao


def extensao(nome: str) -> str:
    """'.csv' de 'x.csv'; nada de 'Preview 11.12.2026' (o '.2026' é parte da data, não extensão)."""
    suf = Path(nome or "").suffix
    return suf if re.fullmatch(r"\.[A-Za-z][A-Za-z0-9]{1,4}", suf) else ""


def nome_de_arquivo(s: str) -> str:
    """Nome de arquivo válido no Windows, sem mexer em espaços e pontos do meio (padrões de nome de arquivo dependem deles)."""
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", s or "").strip(" .")
    return s or "download"


def resumo_passo(p: dict) -> str:
    a = p.get("alvo") or {}
    campo = a.get("tag") in ("input", "select", "textarea") or a.get("papel") in ("combobox", "textbox", "listbox", "searchbox", "spinbutton")
    ordem = ("campo", "rotulo", "placeholder", "titulo", "nome", "texto", "id") if campo else ("texto", "rotulo", "titulo", "campo", "placeholder", "nome", "id")
    quem = next((a[k] for k in ordem if a.get(k)), None) or a.get("caminho") or a.get("tag") or "?"
    quem = f'"{quem[:60]}"'
    acao = p["acao"]
    onde = " (no quadro)" if p.get("quadro") else ""
    onde += f" (aba {p['aba'] + 1})" if p.get("aba") else ""
    if acao == "escolher":
        return f"escolher {', '.join(p.get('escolha') or [])} em {quem}{onde}"
    if acao == "digitar":
        return f"digitar {legivel(p.get('valor', ''))!r} em {quem}{onde}"
    if acao == "baixar":
        arqs = ", ".join(renderizar(x.get("arquivo")) or f"o arquivo do site ({x.get('exemplo', '?')})" for x in p.get("arquivos", []))
        return (f"clicar em {quem}{onde} e baixar " if p.get("alvo") else "baixar ") + arqs
    return {"clicar": "clicar em", "enter": "Enter em", "marcar": "marcar", "desmarcar": "desmarcar"}.get(acao, acao) + f" {quem}{onde}"


# ---------------------------------------------------------------- ensinar

class Aula:
    """Os passos de uma aula, em ordem, com a aba e o quadro de cada um."""

    def __init__(self, hoje: date):
        self.hoje = hoje
        self.passos: list[dict] = []
        self.abas: list = []
        self.fora = {"login": 0, "senha": 0}
        self.t0 = time.time()

    def aba(self, pg) -> int:
        if pg not in self.abas:
            self.abas.append(pg)
        return self.abas.index(pg)

    def passo(self, fonte, info: dict):
        from mapa_rede import RE_AUTH_URL, endereco  # noqa: PLC0415
        info = info or {}
        pg, fr = (fonte or {}).get("page"), (fonte or {}).get("frame")
        url_fr = fr.url if fr is not None else ""
        if info.get("login") or RE_AUTH_URL.search(url_fr or ""):
            self.fora["login"] += 1  # página de login: nada entra na receita (o extrator usa a sessão salva do Edge)
            return
        if info.get("sensivel"):
            if info.get("acao") != "clique":
                self.fora["senha"] += 1
            return
        acao = ACOES.get(info.get("acao"))
        if acao is None:
            return
        p = {"acao": acao, "alvo": {k: info[k] for k in CAMPOS_ALVO if info.get(k) not in (None, "")}, "t": round(time.time() - self.t0, 1)}
        if pg is not None:
            p["aba"] = self.aba(pg)
        if fr is not None and pg is not None and fr != pg.main_frame:
            h, c, _ = endereco(url_fr)
            p["quadro"] = {"url": h + c, "nome": fr.name or ""}
        if acao == "escolher":
            p["escolha"], p["valores"] = info.get("escolha") or [], info.get("valores") or []
        if acao == "digitar":
            p["valor"] = tokenizar(str(info.get("valor", "")), self.hoje)
        self.passos.append(p)

    def baixou(self, sugerido: str, salvo: Path | None):
        """O download sai do último clique (ou Enter) antes dele."""
        arq = {"exemplo": sugerido, "arquivo": None}
        if salvo is not None:
            arq["salvo_em"] = str(salvo)
        for p in reversed(self.passos):
            if p["acao"] in ("clicar", "enter", "baixar"):
                if p["acao"] == "baixar":
                    p["arquivos"].append(arq)
                else:
                    p["acao"], p["arquivos"], p["clique"] = "baixar", [arq], p["acao"]
                return
        self.passos.append({"acao": "baixar", "alvo": None, "arquivos": [arq], "t": round(time.time() - self.t0, 1)})


def arrumar(passos: list[dict]) -> tuple[list[dict], list[str]]:
    """Limpa a aula: digitação repetida no mesmo campo fica só com o valor final, o que veio depois do último download
    sai, e avisa do que não vai se repetir bem (datas escolhidas no calendário)."""
    avisos = []
    out: list[dict] = []
    for p in passos:
        if out and p["acao"] == "digitar" and out[-1]["acao"] == "digitar" and out[-1].get("alvo") == p.get("alvo") and out[-1].get("quadro") == p.get("quadro"):
            out[-1] = p
            continue
        out.append(p)
    out = [p for k, p in enumerate(out) if not (p["acao"] == "clicar" and k + 1 < len(out) and out[k + 1]["acao"] == "digitar" and out[k + 1].get("alvo", {}).get("caminho") == (p.get("alvo") or {}).get("caminho")
                                                and out[k + 1].get("quadro") == p.get("quadro") and (p.get("alvo") or {}).get("tag") in ("input", "textarea"))]  # o clique só punha o cursor no campo
    ult = max((i for i, p in enumerate(out) if p["acao"] == "baixar"), default=-1)
    out = out[: ult + 1]
    cal = [p for p in out if p["acao"] == "clicar" and (p.get("alvo") or {}).get("papel") in ("gridcell", "button", "option", "") and re.fullmatch(r"\d{1,2}", (p.get("alvo") or {}).get("texto", ""))]
    if cal:
        avisos.append("Parece que uma data foi escolhida clicando no calendário: o extrator vai clicar no mesmo número de dia todo dia. "
                      "Para a data acompanhar o dia, ensine de novo digitando a data no campo.")
    for p in out:
        p.pop("t", None)
        if p.get("clique") != "enter":  # o download sai de um clique (o normal) ou de um Enter num campo
            p.pop("clique", None)
        for a in p.get("arquivos", []):
            a.pop("salvo_em", None)
    return out, avisos


def pastas_de_dados(base: Path) -> list[str]:
    try:
        return sorted(p.name for p in base.iterdir() if p.is_dir() and not p.name.startswith(("_", ".")))
    except OSError:
        return []


def ensinar(url: str, nome: str, pasta: str, base: Path, canal: str | None = "", headless=False, roteiro=None, perfil: Path | None = None,
            hoje: date | None = None, pasta_receitas: Path | None = None, perguntar=True):
    """Abre o endereço no Edge do extrator e aprende até o Edge ser fechado. Devolve (receita, caminho) ou None.
    'roteiro' (só nos testes) faz os cliques no lugar da pessoa."""
    from playwright.sync_api import Error as PWError  # noqa: PLC0415
    from playwright.sync_api import sync_playwright  # noqa: PLC0415

    import mapa_rede  # noqa: PLC0415

    if perfil is None:
        from navegador import PROFILE as perfil  # noqa: PLC0415  (o mesmo perfil da repetição: a receita roda com os mesmos logins)
    if canal == "":
        from navegador import CANAL as canal  # noqa: PLC0415
    hoje = hoje or date.today()
    pasta_receitas = pasta_receitas or PASTA
    destino = base / pasta
    aula = Aula(hoje)
    mapa = mapa_rede.Mapa(base / "_logs", prefixo=f"gravacao_{nome}", titulo=f"Aula do extrator · {nome}")

    _J = SimpleNamespace(key=nome, group="Aula", name=nome, kind="receita", url=url)
    salvos: list[Path] = []
    with sync_playwright() as p:
        try:
            ctx = p.chromium.launch_persistent_context(str(perfil), channel=canal, headless=headless, accept_downloads=True, no_viewport=True, args=["--start-maximized"])
        except PWError as e:
            msg = str(e).splitlines()[0]
            if "already in use" in msg or "ProcessSingleton" in msg or "lock" in msg.lower():
                print("\nO Edge do extrator já está aberto (a extração está rodando?). Espere terminar ou feche-o e rode de novo.")
            else:
                print(f"\nNão consegui abrir o Edge: {msg}")
            return None
        mapa.ligar(ctx)
        mapa.comecar(_J)

        def ao_passo(fonte, info):
            aula.passo(fonte, info)
            if not (info or {}).get("login") and not (info or {}).get("sensivel"):
                fr = (fonte or {}).get("frame")
                mapa.clique(info or {}, fr.url if fr is not None else "")

        ctx.expose_binding("__orbitaEnsinar", ao_passo)
        ctx.add_init_script(JS_ENSINAR)

        def ao_baixar(dl):
            sug = dl.suggested_filename or "download"
            alvo = destino / nome_de_arquivo(sug)
            try:
                destino.mkdir(parents=True, exist_ok=True)
                tmp = alvo.with_name(alvo.name + ".parcial")
                dl.save_as(tmp)
                os.replace(tmp, alvo)
            except Exception as e:  # noqa: BLE001
                print(f"  download não concluído: {str(e).splitlines()[0][:90]}")
                aula.baixou(sug, None)
                return
            salvos.append(alvo)
            aula.baixou(sug, alvo)
            mapa.marcar_pagina(dl.page)
            mapa.arquivo(dl, alvo)
            print(f"  baixado: {alvo.name} (em dados\\{pasta})")

        def ligar_pagina(pg):
            aula.aba(pg)
            pg.on("download", ao_baixar)
            pg.on("websocket", mapa._websocket)

        for pg in list(ctx.pages)[1:]:
            try:
                pg.close()
            except PWError:
                pass
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        ligar_pagina(page)
        ctx.on("page", ligar_pagina)
        print(f"\nAbrindo {url}\nNo Edge: faça o caminho até o download como sempre faz (filtros, datas, botão de exportar) e baixe.\n"
              "Pode baixar mais de um arquivo. Quando terminar, FECHE O EDGE (ou Ctrl+C aqui).\n")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=120000)
        except PWError as e:
            print(f"  aviso: a página demorou para abrir ({str(e).splitlines()[0][:80]}); siga no Edge.")
        if roteiro is not None:
            roteiro(page)
            for pg in list(ctx.pages):
                pg.close()
        while True:
            abertas = [pg for pg in ctx.pages if not pg.is_closed()]
            if not abertas:
                break
            try:
                abertas[0].wait_for_timeout(500)
            except PWError:
                continue
            except KeyboardInterrupt:
                print("\nEncerrando a aula...")
                break
        mapa.terminar(_J, "AULA", None)
        try:
            ctx.close()
        except Exception:  # noqa: BLE001
            pass
    mapa.gravar()

    passos, avisos = arrumar(aula.passos)
    if aula.fora["login"]:
        print(f"\n({aula.fora['login']} passo(s) na página de login ficaram de fora: o extrator usa a sessão salva do Edge.)")
    if aula.fora["senha"]:
        print(f"({aula.fora['senha']} digitação(ões) em campo de login ou senha ficaram de fora.)")
    if not any(p["acao"] == "baixar" for p in passos):
        print("\nNenhum download aconteceu: não há o que aprender. Rode de novo e baixe o arquivo antes de fechar o Edge.")
        return None
    receita = {"versao": VERSAO, "nome": nome, "grupo": "Extras", "rotulo": nome, "pasta": pasta, "endereco": tokenizar_endereco(url, hoje),
               "ensinada_em": datetime.now().isoformat(timespec="seconds"), "ligada": True, "passos": passos}
    print(f"\nAprendi {len(passos)} passo(s):")
    print(f"  0. abrir {legivel(receita['endereco'])}")
    for i, pz in enumerate(passos, 1):
        print(f"  {i}. {resumo_passo(pz)}")
    for a in avisos:
        print(f"\nAtenção: {a}")
    if perguntar:
        arquivos = [a for pz in passos for a in pz.get("arquivos", [])]
        print("\nNome dos arquivos no dia a dia (Enter mantém o nome que o site der, como quando você baixa à mão):")
        for i, a in enumerate(arquivos, 1):
            r = input(f"  {i}. {a['exemplo']} -> ").strip().strip('"')
            if r:
                if not extensao(r) and extensao(a["exemplo"]):
                    r += extensao(a["exemplo"])
                a["arquivo"] = tokenizar(nome_de_arquivo(r), hoje)
                if a["arquivo"] != r:
                    print(f"     as datas acompanham o dia: hoje sairia {renderizar(a['arquivo'], hoje)}")
                if salvos and i <= len(salvos) and salvos[i - 1].exists():  # o arquivo da aula já fica com o nome escolhido
                    novo = salvos[i - 1].with_name(renderizar(a["arquivo"], hoje))
                    if novo != salvos[i - 1]:
                        os.replace(salvos[i - 1], novo)
    caminho = gravar_receita(receita, pasta_receitas)
    return receita, caminho


def gravar_receita(receita: dict, pasta: Path | None = None) -> Path:
    pasta = pasta or PASTA
    pasta.mkdir(parents=True, exist_ok=True)
    p = pasta / f"{receita['nome']}.json"
    if p.exists():  # a aula anterior fica guardada
        antigas = pasta / "_antigas"
        antigas.mkdir(exist_ok=True)
        shutil.copy2(p, antigas / f"{p.stem}_{datetime.now():%Y%m%d_%H%M%S}.json")
    tmp = p.with_suffix(".json.parcial")
    tmp.write_text(json.dumps(receita, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)
    return p


# ---------------------------------------------------------------- carregar

def carregar(pasta: Path | None = None) -> list[dict]:
    """As receitas aprendidas (extrator\\receitas\\*.json). Arquivo com defeito é ignorado com aviso; nunca derruba o extrator."""
    pasta = pasta or PASTA
    out = []
    if not pasta.is_dir():
        return out
    for p in sorted(pasta.glob("*.json")):
        try:
            r = json.loads(p.read_text(encoding="utf-8-sig"))
            if not isinstance(r, dict) or not r.get("endereco") or not isinstance(r.get("passos"), list):
                raise ValueError("faltam 'endereco' ou 'passos'")
            if not any(x.get("acao") == "baixar" for x in r["passos"]):
                raise ValueError("nenhum passo de download")
            r.setdefault("nome", p.stem)
            r["nome"] = nome_seguro(r["nome"], p.stem)
            r.setdefault("pasta", "Extras")
            r.setdefault("grupo", "Extras")
            r["_arquivo"] = str(p)
            out.append(r)
        except Exception as e:  # noqa: BLE001
            print(f"AVISO | receita {p.name} ignorada: {str(e).splitlines()[0][:120]}", file=sys.stderr)
    return out


# ---------------------------------------------------------------- repetir (na extração)

PAPEL_DA_TAG = {"button": "button", "a": "link", "select": "combobox", "textarea": "textbox", "summary": "button"}
PAPEL_DO_TIPO = {"checkbox": "checkbox", "radio": "radio", "submit": "button", "button": "button", "reset": "button", "text": "textbox", "search": "searchbox",
                 "": "textbox", "number": "spinbutton", "email": "textbox", "tel": "textbox", "url": "textbox"}


def _papel(a: dict) -> str:
    if a.get("papel"):
        return a["papel"]
    tag = a.get("tag", "")
    if tag == "input":
        return PAPEL_DO_TIPO.get((a.get("tipo") or "").lower(), "")
    if tag == "a" and not a.get("href"):
        return ""
    return PAPEL_DA_TAG.get(tag, "")


def _aspas(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _estrategias(fr, a: dict):
    """(rótulo, localizador, índice): do mais específico para o mais genérico. O índice desempata quando há vários iguais."""
    tag = a.get("tag") or "*"
    if a.get("testid"):
        yield "testid", fr.locator(f'[data-testid="{_aspas(a["testid"])}"]'), None
    if a.get("id"):
        yield "id", fr.locator(f'[id="{_aspas(a["id"])}"]'), None
    nome = a.get("rotulo") or a.get("campo") or a.get("texto") or a.get("titulo")
    papel = _papel(a)
    if papel and nome:
        yield "papel", fr.get_by_role(papel, name=nome, exact=True), None
    for campo, attr in (("rotulo", "aria-label"), ("titulo", "title")):
        if a.get(campo):
            yield attr, fr.locator(f'{tag}[{attr}="{_aspas(a[campo])}"]'), None
    if a.get("campo"):
        yield "rótulo do campo", fr.get_by_label(a["campo"], exact=True), None
    for campo, attr in (("nome", "name"), ("placeholder", "placeholder")):
        if a.get(campo):
            yield attr, fr.locator(f'{tag}[{attr}="{_aspas(a[campo])}"]'), None
    if a.get("texto"):
        t = re.escape(a["texto"])
        rx = re.compile(r"^\s*" + t + (r"" if len(a["texto"]) >= 90 else r"\s*$"))
        yield "texto", fr.locator(tag).filter(has_text=rx), a.get("nt", 0)
    if a.get("caminho"):
        yield "caminho", fr.locator(a["caminho"]), a.get("n", 0)
    if a.get("href") and not a["href"].startswith(("javascript:", "#")):
        yield "href", fr.locator(f'a[href="{_aspas(a["href"])}"]'), None


def _visiveis(loc, aceitar_oculto=False) -> list:
    try:
        n = min(loc.count(), 40)
    except Exception:  # noqa: BLE001  (seletor que não vale nesta página)
        return []
    out = []
    for i in range(n):
        x = loc.nth(i)
        try:
            if aceitar_oculto or x.is_visible():
                out.append(x)
        except Exception:  # noqa: BLE001
            pass
    return out


def localizar(fr, a: dict, aceitar_oculto=False):
    """O elemento do passo nesta página: a primeira forma que acha um só; havendo vários, vale o índice gravado
    (texto ou caminho); sem índice, o primeiro."""
    reserva = None
    for rot, loc, idx in _estrategias(fr, a):
        vs = _visiveis(loc, aceitar_oculto)
        if not vs:
            continue
        if len(vs) == 1:
            return vs[0]
        if idx is not None:
            return vs[min(idx, len(vs) - 1)]
        reserva = reserva or vs[0]
    return reserva


def quadro(pg, q: dict | None):
    if not q:
        return pg.main_frame
    from mapa_rede import endereco  # noqa: PLC0415
    por_nome = None
    for f in pg.frames:
        if f == pg.main_frame:
            continue
        try:
            h, c, _ = endereco(f.url)
        except Exception:  # noqa: BLE001
            continue
        if h + c == q.get("url"):
            return f
        if q.get("nome") and f.name == q["nome"]:
            por_nome = f
    return por_nome


def executar(page, j, folder: Path, ex):
    """Repete a receita: abre o endereço (esperando o login, se pedir), faz os passos na ordem e salva cada download.
    'ex' é o módulo do extrator (load, esperar_carga, TIMEOUT, DLD_TIMEOUT, MAPA). Devolve (arquivos, falhas, vazios)."""
    from playwright.sync_api import Error as PWError  # noqa: PLC0415

    r = j.receita
    ctx = page.context
    novas, downloads = [], []
    abas = {0: page}  # aba da aula -> aba desta rodada (uma janela de login que abriu na aula pode não abrir hoje)

    def ao_baixar(dl):
        downloads.append(dl)

    def nova_aba(pg):
        novas.append(pg)
        pg.on("download", ao_baixar)

    page.on("download", ao_baixar)
    ctx.on("page", nova_aba)
    arquivos: list[Path] = []
    total = len(r["passos"])
    i = 0
    try:
        ex.load(page, renderizar(r["endereco"]))
        ex.esperar_carga(page)
        for i, p in enumerate(r["passos"], 1):
            desc = resumo_passo(p)
            try:
                _passo(page, p, abas, novas, downloads, arquivos, folder, ex, r, i, total, desc)
            except RuntimeError:
                raise
            except Exception as e:  # erro do navegador no meio do passo: diz qual passo era
                if "has been closed" in str(e) or "Target closed" in str(e):
                    raise
                raise RuntimeError(f"receita {r['nome']}, passo {i} de {total} ({desc}): {str(e).splitlines()[0][:90]}") from e
    finally:
        try:
            page.remove_listener("download", ao_baixar)
            ctx.remove_listener("page", nova_aba)
        except Exception:  # noqa: BLE001
            pass
        for pg in novas:  # abas abertas pela receita: o próximo item começa só com a principal
            try:
                pg.close()
            except PWError:
                pass
    return arquivos, [], []


def _passo(page, p, abas, novas, downloads, arquivos, folder, ex, r, i, total, desc):
    """Um passo da receita: acha a aba (a que o passo anterior abriu, se for o caso) e faz a ação; no download, salva os arquivos."""
    k = p.get("aba", 0)
    fim = time.time() + 30
    while k not in abas:  # a aba que o passo anterior abriu: a mais nova ainda sem dono
        livres = [x for x in novas if x not in abas.values() and not x.is_closed()]
        if livres:
            abas[k] = livres[-1]
            break
        if time.time() > fim:
            raise RuntimeError(f"receita {r['nome']}, passo {i} de {total} ({desc}): a aba nova não abriu")
        page.wait_for_timeout(250)
    pg = abas[k]
    if pg.is_closed():
        raise RuntimeError(f"receita {r['nome']}, passo {i} de {total} ({desc}): a aba fechou antes do passo")
    if p["acao"] == "baixar":
        n0 = len(downloads)
        if p.get("alvo"):
            _agir(pg, p, "enter" if p.get("clique") == "enter" else "clicar", ex, r, i, total, desc)
        esperam = len(p.get("arquivos") or [1])
        limite = time.time() + ex.DLD_TIMEOUT / 1000
        while len(downloads) < n0 + esperam:
            if time.time() > limite:
                raise RuntimeError(f"receita {r['nome']}, passo {i} de {total} ({desc}): o download não começou em {ex.DLD_TIMEOUT // 60000} min")
            pg.wait_for_timeout(250)
        for dl, a in zip(downloads[n0:n0 + esperam], p.get("arquivos") or [{}]):
            arquivos.append(salvar(dl, folder, a.get("arquivo"), ex))
    else:
        _agir(pg, p, p["acao"], ex, r, i, total, desc)


def _agir(pg, p, acao, ex, r, i, total, desc):
    """Acha o elemento (esperando ele aparecer) e faz a ação do passo."""
    a = p.get("alvo") or {}
    oculto = acao in ("marcar", "desmarcar")
    espera = ex.TIMEOUT / 1000
    fim = time.time() + espera
    loc = None
    while loc is None:
        fr = quadro(pg, p.get("quadro"))
        if fr is not None:
            loc = localizar(fr, a, oculto)
        if loc is not None:
            break
        if time.time() > fim:
            raise RuntimeError(f"receita {r['nome']}, passo {i} de {total} ({desc}): não apareceu na página em {int(espera)} s")
        ex.esperar_carga(pg, 60)
        pg.wait_for_timeout(300)
    ex.esperar_carga(pg, 120)
    if acao == "clicar":
        try:
            loc.scroll_into_view_if_needed(timeout=5000)
        except Exception:  # noqa: BLE001
            pass
        loc.click(timeout=ex.TIMEOUT)
    elif acao == "enter":
        loc.press("Enter")
    elif acao == "digitar":
        valor = renderizar(p.get("valor", ""))
        try:
            loc.fill(valor, timeout=ex.TIMEOUT)
        except Exception:  # noqa: BLE001  (campo com máscara: digita tecla a tecla)
            loc.click()
            loc.press("Control+A")
            loc.press_sequentially(valor, delay=30)
        try:
            loc.dispatch_event("change")
        except Exception:  # noqa: BLE001
            pass
    elif acao == "escolher":
        rotulos = p.get("escolha") or []
        try:
            loc.select_option(label=rotulos if len(rotulos) > 1 else rotulos[0], timeout=ex.TIMEOUT)
        except Exception:  # noqa: BLE001  (o texto da opção mudou: tenta pelo valor)
            vals = p.get("valores") or []
            loc.select_option(value=vals if len(vals) > 1 else vals[0], timeout=ex.TIMEOUT)
    elif acao in ("marcar", "desmarcar"):
        loc.set_checked(acao == "marcar", force=True, timeout=ex.TIMEOUT)
    pg.wait_for_timeout(400)
    ex.esperar_carga(pg, 120)


def salvar(dl, folder: Path, fixo: str | None, ex) -> Path:
    """Salva com o nome que o site dá (como no download à mão) ou com o nome fixo da receita (datas do dia).
    Só troca o arquivo anterior quando o novo chegou inteiro."""
    folder.mkdir(parents=True, exist_ok=True)
    sug = dl.suggested_filename or "download"
    nome = renderizar(fixo) if fixo else sug
    if fixo and not extensao(nome) and extensao(sug):
        nome += extensao(sug)
    p = folder / nome_de_arquivo(nome)
    tmp = p.with_name(p.name + ".parcial")
    dl.save_as(tmp)
    if tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"Arquivo vazio: {p.name}")
    os.replace(tmp, p)
    if getattr(ex, "MAPA", None) is not None:
        ex.MAPA.arquivo(dl, p)
    logging.info("SALVO | %s | %s bytes", p, p.stat().st_size)
    return p


# ---------------------------------------------------------------- linha de comando

def _perguntar_pasta(base: Path, sugestao: str) -> str:
    existentes = pastas_de_dados(base)
    if existentes:
        print("Pastas em dados: " + ", ".join(existentes))
    r = input(f"Pasta em dados onde o arquivo vai, a mesma de quando você baixa à mão (. = direto em dados) [{sugestao}]: ").strip().strip('"').strip("\\/")
    return r or sugestao


def repetir(nome: str, base: Path, headless=False, pasta_receitas: Path | None = None) -> list[Path]:
    """Roda uma receita aprendida fora de um extrator maior: abre o perfil persistente, repete os passos e salva os
    downloads em base/<pasta da receita>. Devolve os arquivos salvos."""
    from playwright.sync_api import sync_playwright  # noqa: PLC0415

    import navegador  # noqa: PLC0415

    rs = {r["nome"]: r for r in carregar(pasta_receitas)}
    if nome not in rs:
        raise SystemExit(f"Receita '{nome}' não encontrada. Veja as aprendidas com --lista.")
    r = rs[nome]
    navegador.INTERACTIVE_LOGIN = not headless
    destino = base / r.get("pasta", ".")
    destino.mkdir(parents=True, exist_ok=True)
    job = SimpleNamespace(key=nome, group="Receitas", name=nome, kind="receita", url=r["endereco"], receita=r)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(str(navegador.PROFILE), channel=navegador.CANAL, headless=headless,
                                                   accept_downloads=True, no_viewport=True, args=["--start-maximized"])
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.set_default_timeout(navegador.TIMEOUT)
            res = executar(page, job, destino, navegador)
        finally:
            ctx.close()
    arquivos = res[0] if isinstance(res, tuple) else res
    for a in arquivos or []:
        print(f"  salvo: {a}")
    return list(arquivos or [])


def main():
    from navegador import pasta_padrao  # noqa: PLC0415
    ap = argparse.ArgumentParser(description="Ensina a ferramenta a baixar uma página: você baixa uma vez, ela repete sozinha.")
    ap.add_argument("url", nargs="?", help="endereço da página")
    ap.add_argument("--nome", help="nome curto (ex.: vendas)")
    ap.add_argument("--pasta", help="pasta dentro de dados (ex.: Vendas)")
    ap.add_argument("--saida", help="pasta de dados (padrão: ./dados ou TOOLKIT_DADOS)")
    ap.add_argument("--lista", action="store_true", help="mostra as receitas aprendidas")
    ap.add_argument("--repetir", metavar="NOME", help="repete uma receita aprendida e salva os downloads")
    ap.add_argument("--sem-tela", action="store_true", help="com --repetir: navegador sem janela (exige login já salvo)")
    a = ap.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    base = Path(a.saida) if a.saida else pasta_padrao()
    if a.lista:
        rs = carregar()
        if not rs:
            print("Nenhuma receita ainda. Rode python receitas.py para ensinar a primeira.")
        for r in rs:
            print(f"\n{r['nome']} -> dados\\{r['pasta']}{'' if r.get('ligada', True) else ' (desligada)'}  [{Path(r['_arquivo']).name}]")
            print(f"  0. abrir {legivel(r['endereco'])}")
            for i, p in enumerate(r["passos"], 1):
                print(f"  {i}. {resumo_passo(p)}")
        return 0
    if a.repetir:
        repetir(nome_seguro(a.repetir), base, headless=a.sem_tela)
        return 0
    url, nome, pasta = a.url, a.nome, a.pasta
    print("Ensinar: você baixa uma vez, a ferramenta repete sozinha.\n")
    while True:
        if not url:
            url = input("Endereço da página (Enter vazio para sair): ").strip().strip('"')
            if not url:
                break
        if not re.match(r"^https?://", url, re.I):
            # host:porta sem esquema costuma ser sistema interno em HTTP; o resto vai por HTTPS
            url = "http://" + url if re.match(r"^[\w.-]+:\d+", url) else "https://" + url
        if not nome:
            from gravar_pagina import sugerir_nome  # noqa: PLC0415
            sug = sugerir_nome(url)
            nome = input(f"Nome curto para esta página [{sug}]: ").strip() or sug
        nome = nome_seguro(nome)
        if (PASTA / f"{nome}.json").exists():
            print(f"(Já existe uma receita '{nome}': a nova substitui, e a anterior fica em receitas\\_antigas.)")
        if not pasta:
            pasta = _perguntar_pasta(base, nome.replace("_", " ").title())
        res = ensinar(url, nome, pasta, base)
        if res is not None:
            receita, caminho = res
            print(f"\nReceita gravada: {caminho}\nPara baixar de novo: python receitas.py --repetir {nome}")
            if input("\nTestar agora? A ferramenta repete sozinha, com o navegador visível. (S/n): ").strip().lower() not in ("n", "nao", "não"):
                repetir(nome, base)
        if a.url:
            break
        url = nome = pasta = None
        if input("\nEnsinar outra página? (s/N): ").strip().lower() not in ("s", "sim", "y"):
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
