# -*- coding: utf-8 -*-
"""Mapa da rede da extração (modo --mapear do extrator).

Enquanto o extrator faz a extração de sempre, este módulo anota o que cada página pede ao servidor, como a aba
Rede (Network) do DevTools mostraria: endereço, método, filtros, tipo e tamanho da resposta e a forma dos dados
(nomes de campos e quantidade de registros). Para cada arquivo baixado, diz de onde ele veio:

- do servidor: um endereço que devolve o arquivo pronto (dá para pedir direto, sem clicar);
- montado na página a partir de uma resposta JSON (ou de uma tabela HTML): diz qual resposta e qual campo virou
  qual coluna, que é o que falta para montar o mesmo arquivo sem clicar.

O que NÃO vai para o mapa: cookies, cabeçalhos de autenticação, tokens, senhas, o login de quem roda e os valores
dos dados (nomes, números, textos). Ficam só endereços, códigos de filtro curtos (BR, 2026, Actions...), nomes de
campos, tipos e contagens. A comparação entre o arquivo baixado e as respostas é feita aqui, na memória, durante o
item; só o resultado (campo -> coluna, % de valores iguais) é gravado.

Saída: dados/_logs/mapa_rede_AAAAMMDD_HHMMSS.json (completo, para a extração direta) e .md (para ler)."""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import time
from collections import Counter
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

ESTATICOS = {"image", "font", "stylesheet", "script", "media", "manifest", "texttrack", "imageset", "ping", "cspviolationreport", "prefetch", "beacon"}
RE_AUTH_URL = re.compile(r"login|logon|logout|oauth|saml|adfs|/sso\b|microsoftonline|okta|/auth(orize)?\b|openid|/token\b|kerberos|ntlm", re.I)
RE_SENSIVEL = re.compile(r"pass|senha|token|auth|secret|sess|cookie|jwt|bearer|apikey|api_key|signature|^sig$|^code$|^state$|nonce|user|login|e-?mail|matric|cpf|chapa|"
                         r"^nome|name$|supervis[oa]r|lider|leader|respons|owner|coorden|executor|criado_?por|created_?by|requester|solicitante", re.I)
RE_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
RE_PESSOA = re.compile(r"^[A-ZÀ-Ý][a-zà-ÿ]+(?:\s+(?:d[aeo]s?\s+)?[A-ZÀ-Ý][a-zà-ÿ]+)+$")
RE_CHAVE = re.compile(r"^[A-Za-z_$@#][\w$@#.\-]{0,63}$")
RE_CONTAGEM = re.compile(r"^(total|count|totalcount|total_count|totalrows|total_rows|totalitems|total_items|totalrecords|rowcount|row_count|recordstotal|recordsfiltered|"
                         r"page|pages|pagesize|page_size|pagenumber|size|limit|offset|skip|take|length|draw)$", re.I)
CAB_VALOR = {"accept", "content-type", "x-requested-with", "next-action", "rsc", "next-router-prefetch", "x-http-method-override", "accept-language"}
CAB_RESP = {"content-type", "content-length", "content-disposition", "content-encoding", "x-total-count", "cache-control", "transfer-encoding", "x-powered-by", "server"}
TIPOS_ARQUIVO = re.compile(r"csv|excel|spreadsheet|octet-stream|vnd\.ms|officedocument|zip|text/plain", re.I)
MAX_CORPO = 120 * 1024 * 1024   # respostas maiores que isso: só o tamanho
MAX_CHAMADAS = 400              # linha do tempo por item
MAX_CANDIDATAS = 8              # respostas com registros guardadas na memória durante o item (para comparar com o arquivo)
LINHAS_AMOSTRA = 400            # linhas do arquivo usadas na comparação
USUARIO = (os.environ.get("USERNAME") or os.environ.get("USER") or "").strip().lower()
if len(USUARIO) < 3:
    USUARIO = ""


# ---------------------------------------------------------------- o que pode ir para o mapa

def oculto(chave, v):
    """Valor que pode aparecer no mapa: códigos e números curtos de filtro. Texto longo, e-mail, token, nome de
    pessoa, o login de quem roda e qualquer campo de nome sensível viram só a indicação do que era."""
    if v is None or isinstance(v, bool):
        return v
    k = str(chave or "")
    if RE_SENSIVEL.search(k):
        return "{oculto}"
    if isinstance(v, (int, float)):
        return v
    s = str(v)
    if len(s) > 48:
        return f"{{texto, {len(s)} car.}}"
    if RE_EMAIL.search(s) or s.startswith("eyJ") or (USUARIO and USUARIO in s.lower()) or RE_PESSOA.match(s.strip()) or re.fullmatch(r"[A-Za-z0-9+/=_-]{32,}", s):
        return "{oculto}"
    return s


def molde(caminho: str) -> str:
    """Caminho do endereço com números, códigos longos e o login trocados por marcadores ({id}, {usuario})."""
    out = []
    for seg in caminho.split("/"):
        if re.fullmatch(r"\d+", seg) or re.fullmatch(r"[0-9a-fA-F-]{16,}", seg):
            out.append("{id}")
        elif USUARIO and USUARIO in seg.lower():
            out.append("{usuario}")
        elif RE_EMAIL.search(seg):
            out.append("{email}")
        elif len(seg) > 60:
            out.append("{texto}")
        else:
            out.append(seg)
    return "/".join(out)


def endereco(url: str) -> tuple[str, str, dict]:
    """(host, caminho-molde, filtros seguros) de um endereço."""
    u = urlsplit(url)
    filtros: dict[str, str] = {}
    for k, v in parse_qsl(u.query, keep_blank_values=True):
        filtros[k] = oculto(k, v)
    return u.netloc, molde(u.path or "/"), filtros


def tipo(v) -> str:
    if v is None:
        return "nulo"
    if isinstance(v, bool):
        return "booleano"
    if isinstance(v, int):
        return "inteiro"
    if isinstance(v, float):
        return "decimal"
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return "vazio"
        if re.match(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2})?", s):
            return "data AAAA-MM-DD"
        if re.match(r"^\d{1,2}/\d{1,2}/\d{4}", s):
            return "data DD/MM/AAAA"
        if re.fullmatch(r"-?\d+([.,]\d+)?", s):
            return "número em texto"
        return "texto"
    if isinstance(v, list):
        return "lista"
    if isinstance(v, dict):
        return "objeto"
    return type(v).__name__


def suspeita(k) -> bool:
    s = str(k)
    return bool(RE_PESSOA.match(s) or RE_EMAIL.search(s) or (USUARIO and USUARIO in s.lower()) or not RE_CHAVE.match(s))


def esquema(v, chave=None, prof=0):
    """Forma de um JSON sem os valores: campos, tipos, tamanhos de listas e contagens (total, page, size...)."""
    if isinstance(v, dict):
        ks = list(v.keys())
        ruins = [k for k in ks if suspeita(k)]
        if len(ks) > 120 or (ruins and len(ruins) >= max(2, len(ks) * 0.2)):
            return {"tipo": "objeto", "chaves": len(ks), "obs": "as chaves parecem dados (omitidas)"}
        if prof >= 5:
            return {"tipo": "objeto", "chaves": len(ks)}
        campos = {}
        for k in ks:
            if k in ruins:
                continue
            x = v[k]
            if RE_CONTAGEM.match(str(k)) and isinstance(x, (int, float)) and not isinstance(x, bool):
                campos[k] = {"tipo": tipo(x), "valor": x}
            else:
                campos[k] = esquema(x, k, prof + 1)
        out = {"tipo": "objeto", "campos": campos}
        if ruins:
            out["omitidas"] = len(ruins)
        return out
    if isinstance(v, list):
        n = len(v)
        if not n:
            return {"tipo": "lista", "n": 0}
        amostra = v[:300]
        if all(isinstance(x, dict) for x in amostra[:50]):
            tipos: dict[str, Counter] = {}
            for r in amostra:
                if not isinstance(r, dict):
                    continue
                for k, x in r.items():
                    tipos.setdefault(k, Counter())[tipo(x)] += 1
            if len(tipos) > 150 or sum(1 for k in tipos if suspeita(k)) >= max(2, len(tipos) * 0.2):
                return {"tipo": "lista de registros", "n": n, "campos": len(tipos), "obs": "os nomes dos campos parecem dados (omitidos)"}
            campos = {}
            for k, c in tipos.items():
                if suspeita(k):
                    continue
                cheio = [t for t, _ in c.most_common() if t not in ("nulo", "vazio")]
                campos[k] = cheio[0] if cheio else "sempre vazio"
                sub = next((r[k] for r in amostra if isinstance(r, dict) and isinstance(r.get(k), (dict, list)) and r.get(k)), None)
                if sub is not None and prof < 4:
                    campos[k] = esquema(sub, k, prof + 1)
            return {"tipo": "lista de registros", "n": n, "campos": campos}
        if all(isinstance(x, list) for x in amostra[:50]):
            larg = Counter(len(x) for x in amostra if isinstance(x, list)).most_common(1)[0][0]
            return {"tipo": "lista de linhas (sem nomes de campo)", "n": n, "colunas": larg,
                    "tipos": [Counter(tipo(x[i]) for x in amostra if isinstance(x, list) and len(x) > i).most_common(1)[0][0] for i in range(min(larg, 80))]}
        return {"tipo": "lista", "n": n, "item": tipo(v[0])}
    return {"tipo": tipo(v)}


def corpo_seguro(v, chave=None, prof=0):
    """Corpo enviado (filtros de um POST) com os valores seguros e o resto como tipo."""
    if isinstance(v, dict):
        if len(v) > 60 or prof > 5:
            return {"{objeto}": len(v)}
        return {k: corpo_seguro(x, k, prof + 1) for k, x in v.items() if not suspeita(k)}
    if isinstance(v, list):
        return [corpo_seguro(x, chave, prof + 1) for x in v[:15]] + ([f"... +{len(v) - 15}"] if len(v) > 15 else [])
    return oculto(chave, v)


# ---------------------------------------------------------------- leitura das respostas e dos arquivos

class _Tabelas(HTMLParser):
    """Tabelas de uma página HTML: linhas de células (texto)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tabelas, self._pilha, self._linha, self._cel = [], [], None, None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._pilha.append([])
        elif tag == "tr" and self._pilha:
            self._linha = []
        elif tag in ("td", "th") and self._linha is not None:
            self._cel = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cel is not None and self._linha is not None:
            self._linha.append(" ".join("".join(self._cel).split()))
            self._cel = None
        elif tag == "tr" and self._linha is not None and self._pilha:
            if self._linha:
                self._pilha[-1].append(self._linha)
            self._linha = None
        elif tag == "table" and self._pilha:
            self.tabelas.append(self._pilha.pop())

    def handle_data(self, data):
        if self._cel is not None:
            self._cel.append(data)


def tabela_html(corpo: bytes):
    """Maior tabela do HTML como registros {coluna: valor} (cabeçalho na primeira linha)."""
    p = _Tabelas()
    try:
        p.feed(corpo.decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        return None
    if not p.tabelas:
        return None
    t = max(p.tabelas, key=len)
    if len(t) < 2:
        return None
    cab = [c or f"#{i}" for i, c in enumerate(t[0])]
    return [dict(zip(cab, linha)) for linha in t[1:]]


def ler_corpo(ct: str, corpo: bytes):
    """(forma, objeto) de uma resposta."""
    ct = (ct or "").lower()
    ini = corpo[:1]
    if "json" in ct or (ini in (b"{", b"[") and "html" not in ct):
        try:
            return "json", json.loads(corpo.decode("utf-8-sig", "replace"))
        except Exception:  # noqa: BLE001
            pass
    if "x-component" in ct:  # resposta de servidor do React (Next.js): linhas "id:conteúdo"
        objs = []
        for linha in corpo.decode("utf-8", "replace").splitlines():
            m = re.match(r"^[0-9a-f]+:(.*)$", linha)
            if m and m.group(1)[:1] in "[{":
                try:
                    objs.append(json.loads(m.group(1)))
                except Exception:  # noqa: BLE001
                    pass
        return "rsc", objs
    if "html" in ct:
        return "html", None
    if TIPOS_ARQUIVO.search(ct):
        return "arquivo", None
    return "outro", None


def listas_de_registros(obj, caminho="$", prof=0, achou=None):
    """Listas de registros dentro de um JSON: (caminho, lista). Linhas sem nome de campo viram {#0, #1, ...}."""
    achou = [] if achou is None else achou
    if prof > 7:
        return achou
    if isinstance(obj, list) and obj:
        amostra = obj[:50]
        if all(isinstance(x, dict) for x in amostra):
            achou.append((caminho + "[]", obj))
            for k in list(amostra[0].keys())[:40]:  # listas dentro do primeiro registro (ex.: grupos com filhos)
                listas_de_registros(amostra[0][k], f"{caminho}[0].{k}", prof + 1, achou)
        elif all(isinstance(x, list) for x in amostra) and len(obj) > 1:
            achou.append((caminho + "[]", obj))
        else:
            for i, x in enumerate(obj[:10]):
                listas_de_registros(x, f"{caminho}[{i}]", prof + 1, achou)
    elif isinstance(obj, dict):
        for k, x in list(obj.items())[:200]:
            if isinstance(x, (dict, list)):
                listas_de_registros(x, f"{caminho}.{k}", prof + 1, achou)
    return achou


def ler_arquivo(p: Path):
    """(cabeçalho, linhas de amostra, total de linhas) de um CSV; xlsx: só o cabeçalho e o total, se der."""
    suf = p.suffix.lower()
    if suf == ".csv" or suf == ".txt":
        raw = p.read_bytes()
        txt = None
        for enc in ("utf-8-sig", "cp1252", "latin-1"):
            try:
                txt = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if txt.lstrip("\ufeff").lower().startswith("sep="):  # linha de dica do Excel antes do cabeçalho
            txt = txt.split("\n", 1)[1] if "\n" in txt else ""
        prim = txt.split("\n", 1)[0]
        sep = max([";", ",", "\t", "|"], key=prim.count)
        r = csv.reader(io.StringIO(txt), delimiter=sep)
        cab = next(r, [])
        linhas, total = [], 0
        for linha in r:
            if not any(x.strip() for x in linha):
                continue
            total += 1
            if len(linhas) < LINHAS_AMOSTRA:
                linhas.append(linha)
        return [c.strip().lstrip("﻿") for c in cab], linhas, total
    if suf in (".xlsx", ".xlsm"):
        try:
            import openpyxl  # noqa: PLC0415
            wb = openpyxl.load_workbook(p, read_only=True, data_only=True)
            ws = max(wb.worksheets, key=lambda w: (w.max_row or 0))
            it = ws.iter_rows(values_only=True)
            cab = [str(c) if c is not None else "" for c in next(it, [])]
            linhas, total = [], 0
            for linha in it:
                if not any(c not in (None, "") for c in linha):
                    continue
                total += 1
                if len(linhas) < LINHAS_AMOSTRA:
                    linhas.append(["" if c is None else str(c) for c in linha])
            wb.close()
            return cab, linhas, total
        except Exception:  # noqa: BLE001
            return None, None, None
    return None, None, None


def formas(v) -> tuple:
    """Formas comparáveis de um valor (datas ambíguas DD/MM e MM/DD entram das duas maneiras)."""
    if v is None:
        return ()
    if isinstance(v, bool):
        return ("true" if v else "false",)
    if isinstance(v, (int, float)):
        if isinstance(v, float) and v != v:
            return ()
        f = float(v)
        return (str(int(f)) if f.is_integer() else f"{f:.6g}",)
    if isinstance(v, (dict, list)):
        return ()
    s = str(v).strip()
    if not s or s.lower() in ("null", "none", "nan", "-", "n/a"):
        return ()
    if s.lower() in ("yes", "sim", "true", "verdadeiro"):
        return ("true",)
    if s.lower() in ("no", "não", "nao", "false", "falso"):
        return ("false",)
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return (f"{m[1]}-{m[2]}-{m[3]}",)
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        a, b, ano = int(m[1]), int(m[2]), m[3]
        out = []
        if b <= 12:
            out.append(f"{ano}-{b:02d}-{a:02d}")
        if a <= 12:
            out.append(f"{ano}-{a:02d}-{b:02d}")
        return tuple(out)
    if re.fullmatch(r"-?\d{1,3}(\.\d{3})*,\d+", s):          # 1.234,5
        t = s.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"-?\d{1,3}(,\d{3})+(\.\d+)?", s):     # 1,234.5
        t = s.replace(",", "")
    elif re.fullmatch(r"-?\d+,\d+", s):
        t = s.replace(",", ".")
    else:
        t = s.rstrip("%") if re.fullmatch(r"-?\d+(\.\d+)?%", s) else s
    if re.fullmatch(r"-?\d+(\.\d+)?", t):
        try:
            f = float(t)
            return (str(int(f)) if f.is_integer() else f"{f:.6g}",)
        except ValueError:
            pass
    return (s.lower()[:200],)


def achatar(r) -> dict:
    """Registro com um nível de objetos aninhados aberto (a.b); linhas sem nome de campo viram #0, #1..."""
    if isinstance(r, list):
        return {f"#{i}": x for i, x in enumerate(r)}
    if not isinstance(r, dict):
        return {}
    out = {}
    for k, x in r.items():
        if isinstance(x, dict):
            for k2, x2 in list(x.items())[:40]:
                out[f"{k}.{k2}"] = x2
        else:
            out[k] = x
    return out


def conjuntos(registros) -> dict:
    """Valores (nas formas comparáveis) de cada campo dos primeiros 5.000 registros. Fica só na memória, durante o
    item, para comparar com o arquivo; a resposta inteira é descartada."""
    amostra = registros[:5000]
    chaves: Counter = Counter()
    for r in amostra[:300]:
        chaves.update(achatar(r).keys())
    conj: dict[str, set] = {k: set() for k in list(chaves)[:200]}
    for r in amostra:
        a = achatar(r)
        for k, s in conj.items():
            for f in formas(a.get(k)):
                if len(s) < 20000:
                    s.add(f)
    return conj


def casar(cab, linhas, conj):
    """Qual campo das respostas virou qual coluna do arquivo: para cada coluna, o campo com mais valores iguais."""
    mapa, sem, vazias = {}, [], 0
    for i, c in enumerate(cab):
        vals = {formas(linha[i]) for linha in linhas if i < len(linha)}
        vals = [f for f in vals if f][:300]
        if not vals:
            vazias += 1
            continue
        melhor, nota = None, 0.0
        for k, s in conj.items():
            if not s:
                continue
            n = sum(1 for f in vals if any(x in s for x in f))
            r = n / len(vals)
            if r > nota:
                melhor, nota = k, r
        if melhor is not None and nota >= 0.6:
            mapa[c] = [melhor, round(nota, 2)]
        else:
            sem.append(c)
    return mapa, sem, vazias


# ---------------------------------------------------------------- o mapa

class Mapa:
    def __init__(self, pasta: Path, prefixo: str = "mapa_rede", titulo: str = "Mapa da rede da extração"):
        self.pasta = Path(pasta)
        self.pasta.mkdir(parents=True, exist_ok=True)
        carimbo = datetime.now()
        self.titulo = titulo
        self.arq_json = self.pasta / f"{prefixo}_{carimbo:%Y%m%d_%H%M%S}.json"
        self.arq_md = self.arq_json.with_suffix(".md")
        self.inicio = carimbo
        self.grupos: dict[tuple, dict] = {}
        self.itens: dict[str, dict] = {}
        self.ordem: list[str] = []
        self.atual: str | None = None
        self.t0 = time.time()
        self.rotulo: dict = {}
        self.candidatas: list = []
        self.auth: dict[str, dict] = {}
        self.estaticos: Counter = Counter()
        self.erros = 0

    # ------------------------------------------------------------ ligação com o navegador
    def ligar(self, ctx, page=None):
        ctx.on("request", self._pedido)
        ctx.on("requestfinished", lambda r: self._fim(r, None))
        ctx.on("requestfailed", lambda r: self._fim(r, "falhou"))
        if page is not None:
            try:
                page.on("websocket", self._websocket)
            except Exception:  # noqa: BLE001
                pass

    def comecar(self, j):
        # as respostas guardadas do item anterior continuam: páginas que geram vários arquivos (abas, gerências)
        # costumam exportar do que já carregaram, sem pedir os dados de novo
        self.atual = j.key
        self.t0 = time.time()
        if j.key not in self.itens:
            self.ordem.append(j.key)
        self.itens[j.key] = {"item": j.key, "grupo": j.group, "nome": j.name, "pagina": endereco(j.url)[0] + endereco(j.url)[1], "filtros_pagina": endereco(j.url)[2],
                             "tipo": j.kind, "inicio": datetime.now().isoformat(timespec="seconds"), "chamadas": [], "arquivos": [], "websockets": [], "pagina_usa": {},
                             "cliques": []}

    def terminar(self, j, status, page=None):
        it = self.itens.get(j.key)
        if it is None:
            return
        it["status"] = status
        it["duracao_s"] = round(time.time() - self.t0, 1)
        self.marcar_pagina(page)
        self.atual = None
        self.gravar()

    def marcar_pagina(self, page):
        """Que tecnologia a página usa (React, MUI DataGrid, DataTables, Power BI...): orienta a extração direta."""
        it = self.itens.get(self.atual)
        if page is None or it is None:
            return
        try:
            usa = page.evaluate("""() => ({
                  next: !!(window.__NEXT_DATA__ || document.querySelector('script[src*="/_next/"]')),
                  react: !!(document.querySelector('[data-reactroot], #root, #__next') || window.React),
                  mui_datagrid: !!document.querySelector('.MuiDataGrid-root'),
                  datatables: !!(window.jQuery && jQuery.fn && jQuery.fn.dataTable),
                  plotly_dash: !!(window.dash_clientside || document.querySelector('#_dash-app-content, #react-entry-point')),
                  angular: !!(window.angular || document.querySelector('[ng-version], [ng-app]')),
                  vue: !!(window.Vue || document.querySelector('[data-v-app]')),
                  highcharts: !!window.Highcharts, echarts: !!window.echarts, chartjs: !!window.Chart,
                  armazenamento_sessao: Object.keys(sessionStorage || {}).length, armazenamento_local: Object.keys(localStorage || {}).length,
                  msal: Object.keys(sessionStorage || {}).concat(Object.keys(localStorage || {})).some(k => /msal|login\\.windows|accesstoken/i.test(k)),
                  powerbi: !!(window.powerbi || document.querySelector('iframe[src*="powerbi"], iframe[src*="reportEmbed"]')),
                  iframes: document.querySelectorAll('iframe').length,
                })""")
            for k, v in usa.items():
                it["pagina_usa"][k] = it["pagina_usa"].get(k) or v
        except Exception:  # noqa: BLE001
            pass

    def clique(self, info: dict, frame_url: str = ""):
        """Um passo da gravação (clique, escolha numa lista, digitação, página aberta), já sem valores."""
        it = self.itens.get(self.atual)
        if it is None or len(it["cliques"]) >= 300:
            return
        passo = {"t": round(time.time() - self.t0, 1), "acao": str(info.get("acao") or "clique")[:20]}
        for k in ("tag", "papel", "tipo", "id", "nome", "rotulo", "titulo", "testid", "texto", "escolha", "caminho", "classes"):
            v = info.get(k)
            if v in (None, ""):
                continue
            v = str(v)
            if k in ("texto", "escolha", "rotulo", "titulo"):
                v = oculto(k, v)
            elif k in ("id", "nome", "testid") and (RE_EMAIL.search(v) or (USUARIO and USUARIO in v.lower())):
                v = "{oculto}"
            passo[k] = v[:160]
        if info.get("caracteres") is not None:
            passo["caracteres"] = int(info["caracteres"])
        if info.get("href"):
            h, c, f = endereco(str(info["href"]))
            passo["link"] = h + c + ("?" + "&".join(f"{a}={b}" for a, b in f.items()) if f else "")
        if frame_url and not frame_url.startswith("about:"):
            h, c, _ = endereco(frame_url)
            passo["quadro"] = h + c
        it["cliques"].append(passo)

    # ------------------------------------------------------------ eventos
    def _pedido(self, req):
        try:
            self.rotulo[req] = (self.atual, time.time())
        except Exception:  # noqa: BLE001
            pass

    def _websocket(self, ws):
        try:
            h, c, _ = endereco(ws.url)
            if self.atual in self.itens:
                self.itens[self.atual]["websockets"].append(h + c)
        except Exception:  # noqa: BLE001
            pass

    def _fim(self, req, falha):
        try:
            self._registrar(req, falha)
        except Exception as e:  # noqa: BLE001  o mapa nunca derruba a extração
            self.erros += 1
            if self.erros <= 5:
                logging.info("MAPA | não registrei uma chamada: %s", str(e).splitlines()[0][:100])
        finally:
            self.rotulo.pop(req, None)

    def _registrar(self, req, falha):
        item, t_ini = self.rotulo.get(req, (self.atual, time.time()))
        url = req.url
        if url.startswith(("data:", "blob:", "chrome-extension:", "edge:", "about:")):
            return
        rt = req.resource_type
        host, caminho, filtros = endereco(url)
        if rt in ESTATICOS:
            self.estaticos[host] += 1
            return
        if RE_AUTH_URL.search(url):
            g = self._grupo("autenticação", req.method, host, caminho, rt)
            g["vezes"] += 1
            g["obs"] = "endereço de login ou autenticação: só o caminho é registrado"
            return
        resp = None
        try:
            resp = req.response()
        except Exception:  # noqa: BLE001
            pass
        g = self._grupo("dados", req.method, host, caminho, rt)
        g["vezes"] += 1
        if item:
            g["itens"].add(item)
        for k, v in filtros.items():
            vs = g["filtros"].setdefault(k, [])
            if v not in vs and len(vs) < 12:
                vs.append(v)
        self._autenticacao(req, host)
        try:
            hs = req.headers
        except Exception:  # noqa: BLE001
            hs = {}
        for k, v in hs.items():
            kl = k.lower()
            if kl in ("cookie", "authorization", "proxy-authorization", "referer"):
                continue
            if kl in CAB_VALOR:
                g["cabecalhos"][kl] = oculto(kl, v)
            elif kl.startswith(("x-", "next-")) or kl not in ("user-agent", "accept-encoding", "origin", "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform", "upgrade-insecure-requests"):
                g["cabecalhos"].setdefault(kl, "(presente)")
        if req.method in ("POST", "PUT", "PATCH") and g["corpo_enviado"] is None:
            g["corpo_enviado"] = self._corpo_enviado(req, hs)
        reg = {"t": round(t_ini - self.t0, 1) if item == self.atual else None, "g": g["id"], "tipo": rt}
        rh = {k.lower(): v for k, v in (resp.headers or {}).items()} if resp is not None else {}
        baixou = "attachment" in rh.get("content-disposition", "").lower()
        if falha and not baixou:
            try:
                reg["falha"] = (req.failure or "")[:80]
            except Exception:  # noqa: BLE001
                reg["falha"] = "falhou"
            g["status"]["falhou"] += 1
        if resp is not None:
            st = resp.status
            g["status"]["download" if baixou else str(st)] += 1
            reg["status"] = st
            if baixou:
                reg["download"] = True
            for k in CAB_RESP:
                if k in rh:
                    val = rh[k]
                    if k == "content-disposition":
                        val = re.sub(r"(filename\*?=)[^;]*", lambda m: m.group(1) + "{nome do arquivo}", val) if USUARIO and USUARIO in val.lower() else val[:120]
                    g["resposta_cabecalhos"][k] = val[:120]
            if "set-cookie" in rh:
                g["resposta_cabecalhos"]["set-cookie"] = "(presente)"
            if st in (401, 403) and "www-authenticate" in rh:
                self.auth.setdefault(host, {"esquemas": Counter(), "cookies": 0, "pedidos": 0})["esquemas"]["desafio " + rh["www-authenticate"].split()[0]] += 1
            ct = rh.get("content-type", "")
            g["tipos_resposta"].add(ct.split(";")[0].strip() or "?")
            try:
                t = req.timing
                if t and t.get("responseEnd", -1) > 0:
                    reg["ms"] = round(t["responseEnd"])
                    g["ms_max"] = max(g["ms_max"], reg["ms"])
            except Exception:  # noqa: BLE001
                pass
            corpo = None
            if 200 <= st < 300 and not baixou and rt in ("xhr", "fetch", "document", "other", "eventsource") and _tamanho(rh) <= MAX_CORPO:
                try:
                    corpo = resp.body()
                except Exception:  # noqa: BLE001  (download, redirecionamento, recurso já liberado)
                    corpo = None
            if corpo is not None and (corpo or not _tamanho(rh)):
                n = len(corpo)
                reg["bytes"] = n
                g["bytes_min"] = n if g["bytes_min"] is None else min(g["bytes_min"], n)
                g["bytes_max"] = max(g["bytes_max"], n)
                self._analisar(g, reg, ct, corpo, item)
        if item in self.itens:
            ch = self.itens[item]["chamadas"]
            if len(ch) < MAX_CHAMADAS:
                ch.append(reg)

    def _autenticacao(self, req, host):
        """Como a página se identifica para o servidor: esquema do cabeçalho Authorization (Bearer, Negotiate...)
        e se vão cookies. Nada do conteúdo."""
        a = self.auth.setdefault(host, {"esquemas": Counter(), "cookies": 0, "pedidos": 0})
        a["pedidos"] += 1
        if a["pedidos"] > 60:  # o suficiente para saber o padrão do servidor
            return
        try:
            todos = req.all_headers()
        except Exception:  # noqa: BLE001
            return
        au = todos.get("authorization") or todos.get("Authorization")
        if au:
            a["esquemas"][au.split()[0][:20]] += 1
        else:
            a["esquemas"]["sem Authorization"] += 1
        if todos.get("cookie"):
            a["cookies"] += 1
        del todos

    def _corpo_enviado(self, req, hs):
        try:
            pd_ = req.post_data
        except Exception:  # noqa: BLE001
            pd_ = None
        if not pd_:
            return {"forma": "vazio"}
        ct = (hs.get("content-type") or "").lower()
        if "json" in ct or pd_[:1] in "{[":
            try:
                return {"forma": "json", "conteudo": corpo_seguro(json.loads(pd_))}
            except Exception:  # noqa: BLE001
                pass
        if "x-www-form-urlencoded" in ct:
            return {"forma": "formulário", "conteudo": {k: oculto(k, v) for k, v in parse_qsl(pd_, keep_blank_values=True) if not suspeita(k)}}
        return {"forma": ct.split(";")[0] or "texto", "tamanho": len(pd_)}

    def _analisar(self, g, reg, ct, corpo, item):
        forma, obj = ler_corpo(ct, corpo)
        reg["forma"] = forma
        if forma in ("json", "rsc"):
            if g["esquema"] is None or reg["bytes"] >= g["bytes_esquema"]:
                g["esquema"] = esquema(obj) if forma == "json" else {"tipo": "resposta React (RSC)", "partes": len(obj)}
                g["bytes_esquema"] = reg["bytes"]
            listas = listas_de_registros(obj)
            if listas:
                cam, lst = max(listas, key=lambda x: len(x[1]))
                reg["registros"] = len(lst)
                reg["caminho"] = cam
                g["registros_max"] = max(g["registros_max"], len(lst))
                g["caminho_registros"] = cam
                if item == self.atual:
                    self._guardar(g["id"], cam, lst, "json")
        elif forma == "html":
            reg["linhas_tabela_html"] = corpo.count(b"<tr")
            if item == self.atual and reg["linhas_tabela_html"] > 1:
                regs = tabela_html(corpo)
                if regs:
                    reg["registros"] = len(regs)
                    g["registros_max"] = max(g["registros_max"], len(regs))
                    self._guardar(g["id"], "tabela HTML", regs, "html")
        elif forma == "arquivo":
            if item == self.atual:
                self.candidatas.append({"g": g["id"], "caminho": None, "registros": None, "origem": "arquivo", "bytes": reg["bytes"], "item": self.atual})
                self._aparar()

    def _guardar(self, gid, caminho, registros, origem):
        self.candidatas.append({"g": gid, "caminho": caminho, "registros": len(registros), "conj": conjuntos(registros), "origem": origem, "bytes": None, "item": self.atual})
        self._aparar()

    def _aparar(self):
        """Só as maiores respostas com registros ficam na memória durante o item (as de arquivo pronto guardam só o tamanho)."""
        regs = [c for c in self.candidatas if c["registros"] is not None]
        arqs = [c for c in self.candidatas if c["registros"] is None]
        fica = {id(c) for c in regs[-MAX_CANDIDATAS:]} | {id(c) for c in arqs[-MAX_CANDIDATAS:]}  # as mais recentes: o arquivo sai do que a página acabou de pedir
        if len(fica) < len(self.candidatas):
            self.candidatas = [c for c in self.candidatas if id(c) in fica]

    def _grupo(self, natureza, metodo, host, caminho, rt):
        k = (natureza, metodo, host, caminho)
        g = self.grupos.get(k)
        if g is None:
            g = {"id": f"c{len(self.grupos) + 1}", "natureza": natureza, "metodo": metodo, "host": host, "caminho": caminho, "tipo": rt, "vezes": 0, "itens": set(),
                 "filtros": {}, "cabecalhos": {}, "corpo_enviado": None, "status": Counter(), "tipos_resposta": set(), "resposta_cabecalhos": {}, "bytes_min": None,
                 "bytes_max": 0, "ms_max": 0, "esquema": None, "bytes_esquema": 0, "registros_max": 0, "caminho_registros": None}
            self.grupos[k] = g
        return g

    def _grupo_por_url(self, url):
        host, caminho, _ = endereco(url)
        for (nat, _m, h, c), g in self.grupos.items():
            if nat == "dados" and h == host and c == caminho:
                return g
        return None

    # ------------------------------------------------------------ o arquivo baixado
    def arquivo(self, dl, p: Path):
        """Chamado pelo extrator depois de salvar um download: de onde ele veio e, se foi montado na página,
        qual resposta e quais campos viraram as colunas."""
        try:
            self._arquivo(dl, Path(p))
        except Exception as e:  # noqa: BLE001
            logging.info("MAPA | não analisei %s: %s", Path(p).name, str(e).splitlines()[0][:100])

    def _arquivo(self, dl, p: Path):
        it = self.itens.get(self.atual)
        if it is None:
            return
        try:
            url = dl.url or ""
        except Exception:  # noqa: BLE001
            url = ""
        info = {"arquivo": p.name, "bytes": p.stat().st_size, "t": round(time.time() - self.t0, 1)}
        cab, linhas, total = ler_arquivo(p)
        if cab is not None:
            info["linhas"] = total
            info["colunas"] = cab
        if url.startswith("http"):
            g = self._grupo_por_url(url)
            info["origem"] = "servidor"
            info["veredito"] = "o arquivo vem pronto do servidor: dá para pedir direto"
            info["chamada"] = g["id"] if g else None
            h, c, f = endereco(url)
            info["endereco_download"] = {"host": h, "caminho": c, "filtros": f}
        else:
            info["origem"] = "página (" + (url.split(":", 1)[0] or "?") + ")"
            # 1) a página pediu o arquivo pronto ao servidor e só o salvou
            mesmo = [c for c in self.candidatas if c["origem"] == "arquivo" and c["bytes"] and abs(c["bytes"] - info["bytes"]) <= max(64, info["bytes"] * 0.01)]
            if mesmo:
                info["chamada"] = mesmo[-1]["g"]
                if mesmo[-1].get("item") not in (None, self.atual):
                    info["resposta_do_item"] = mesmo[-1]["item"]
                info["veredito"] = "a página pede o arquivo pronto ao servidor (e só salva): dá para pedir direto"
            elif cab and linhas is not None:
                # 2) montado na página a partir de dados: qual resposta casa com as colunas
                melhor = None
                for c in self.candidatas:
                    if not c["registros"]:
                        continue
                    mapa, sem, vazias = casar(cab, linhas, c["conj"])
                    cheias = len(cab) - vazias
                    nota = len(mapa) / cheias if cheias else 0
                    if melhor is None or nota > melhor[0] or (nota == melhor[0] and abs(c["registros"] - (total or 0)) <= abs(melhor[1]["registros_resposta"] - (total or 0))):
                        melhor = (nota, {"chamada": c["g"], "caminho": c["caminho"], "fonte": c["origem"], "registros_resposta": c["registros"],
                                         "colunas_casadas": len(mapa), "colunas_com_valor": cheias, "mapa_colunas": mapa, "sem_par": sem,
                                         "resposta_do_item": c.get("item") if c.get("item") not in (None, self.atual) else None})
                if melhor:
                    nota, m = melhor
                    info.update({k: v for k, v in m.items() if v is not None})
                    mesma_qtd = m["registros_resposta"] == total
                    if nota >= 0.75 and mesma_qtd:
                        info["veredito"] = (f"montado na página a partir de {'uma tabela HTML' if m['fonte'] == 'html' else 'uma resposta JSON'}: {m['colunas_casadas']} de "
                                            f"{m['colunas_com_valor']} colunas e as {total} linhas batem" + (" (as outras a página transforma: ver colunas sem par)" if m["sem_par"] else ""))
                    elif nota >= 0.6:
                        info["veredito"] = (f"provavelmente montado a partir da chamada {m['chamada']} ({m['colunas_casadas']} de {m['colunas_com_valor']} colunas; "
                                            f"{m['registros_resposta']} registros na resposta e {total} linhas no arquivo)")
                    else:
                        info["veredito"] = "nenhuma resposta do servidor casou com as colunas do arquivo"
                else:
                    info["veredito"] = "nenhuma resposta com registros até aqui (os dados podem vir por outro caminho: ver as chamadas do item)"
            else:
                info["veredito"] = "origem não identificada"
        if info.get("resposta_do_item"):
            info["veredito"] += f" (resposta carregada no item {info['resposta_do_item']})"
        it["arquivos"].append(info)
        logging.info("MAPA | %s | %s", p.name, info.get("veredito", ""))

    # ------------------------------------------------------------ saída
    def _json(self):
        grupos = []
        for g in self.grupos.values():
            x = dict(g)
            x["itens"] = sorted(g["itens"])
            x["status"] = dict(g["status"])
            x["tipos_resposta"] = sorted(g["tipos_resposta"])
            x.pop("bytes_esquema", None)
            grupos.append(x)
        auth = {h: {"esquemas": dict(a["esquemas"]), "pedidos_com_cookie": a["cookies"], "pedidos_vistos": a["pedidos"]} for h, a in self.auth.items()}
        return {"titulo": self.titulo, "gerado_em": datetime.now().isoformat(timespec="seconds"), "inicio": self.inicio.isoformat(timespec="seconds"),
                "observacao": "Sem cookies, cabeçalhos de autenticação, tokens, login nem valores dos dados. Filtros curtos (códigos) e nomes de campos ficam.",
                "autenticacao": auth, "estaticos_por_host": dict(self.estaticos), "itens": [self.itens[k] for k in self.ordem], "chamadas": grupos, "erros_do_mapa": self.erros}

    def gravar(self):
        try:
            dados = self._json()
            tmp = self.arq_json.with_suffix(".json.parcial")
            tmp.write_text(json.dumps(dados, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
            os.replace(tmp, self.arq_json)
            self.arq_md.write_text(relatorio(dados), encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            logging.info("MAPA | não gravei o mapa: %s", str(e).splitlines()[0][:100])
        return self.arq_json, self.arq_md


def _tamanho(rh) -> int:
    try:
        return int(rh.get("content-length") or 0)
    except ValueError:
        return 0


def _tam(g):
    return g["bytes_max"] or _tamanho(g.get("resposta_cabecalhos") or {}) or None


def _kb(n):
    if n is None:
        return "-"
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / 1024 / 1024:.1f} MB"


def _n(v):
    return f"{v:,}".replace(",", ".") if isinstance(v, int) else str(v)


def relatorio(d: dict) -> str:
    """Versão para ler do mapa (Markdown)."""
    gid = {g["id"]: g for g in d["chamadas"]}
    L = [f"# {d.get('titulo', 'Mapa da rede da extração')} · {d['inicio'][:16].replace('T', ' ')}", "",
         d["observacao"], ""]
    itens = d["itens"]
    arqs = [a for it in itens for a in it["arquivos"]]
    srv = sum(1 for a in arqs if "direto" in a.get("veredito", ""))
    mont = sum(1 for a in arqs if a.get("veredito", "").startswith("montado"))
    prov = sum(1 for a in arqs if a.get("veredito", "").startswith("provavelmente"))
    st = Counter(it.get("status", "?") for it in itens)
    L += ["## Resumo", "",
          f"- Itens: {len(itens)} ({', '.join(f'{k} {v}' for k, v in st.most_common())})",
          f"- Arquivos baixados: {len(arqs)} · prontos no servidor (dá para pedir direto): {srv} · montados na página com tudo batendo: {mont} · prováveis: {prov} · "
          f"sem origem clara: {len(arqs) - srv - mont - prov}"]
    for h, a in d["autenticacao"].items():
        esq = ", ".join(f"{k} ({v})" for k, v in a["esquemas"].items())
        L.append(f"- {h}: {esq}; cookies em {a['pedidos_com_cookie']} de {min(a['pedidos_vistos'], 60)} pedidos olhados")
    L.append("")
    L += ["## Por item", ""]
    for it in itens:
        L.append(f"### {it['item']} · {it['grupo']} · {it['nome']}")
        L.append(f"{it.get('status', '?')} · {it.get('duracao_s', '?')} s · página `{it['pagina']}`" + (f" · usa: {', '.join(k for k, v in it['pagina_usa'].items() if v is True)}" if it.get("pagina_usa") else ""))
        L.append("")
        if it["arquivos"]:
            L += ["| Arquivo | Linhas | Origem | Chamada | Veredito |", "|---|---:|---|---|---|"]
            for a in it["arquivos"]:
                L.append(f"| {a['arquivo']} | {_n(a.get('linhas', '-'))} | {a.get('origem', '')} | {a.get('chamada') or '-'} | {a.get('veredito', '')} |")
            L.append("")
            for a in it["arquivos"]:
                if a.get("sem_par"):
                    L.append(f"- {a['arquivo']}: colunas sem par: {', '.join(a['sem_par'][:20])}")
            L.append("")
        usados = Counter(c["g"] for c in it["chamadas"])
        dados = [gid[k] for k in usados if k in gid and gid[k]["natureza"] == "dados" and gid[k]["tipo"] in ("xhr", "fetch", "document", "other", "eventsource")]
        if dados:
            L += ["| Chamada | Método | Endereço | Vezes | Status | Resposta | Registros |", "|---|---|---|---:|---|---|---:|"]
            for g in sorted(dados, key=lambda g: -(_tam(g) or 0))[:14]:
                filtros = "&".join(f"{k}={'|'.join(map(str, v[:4]))}" for k, v in g["filtros"].items())
                L.append(f"| {g['id']} | {g['metodo']} | `{g['host']}{g['caminho']}{'?' + filtros if filtros else ''}` | {usados[g['id']]} | "
                         f"{', '.join(f'{k}×{v}' for k, v in g['status'].items())} | {', '.join(g['tipos_resposta'])} {_kb(_tam(g))} | {_n(g['registros_max']) if g['registros_max'] else '-'} |")
            L.append("")
        if it.get("cliques"):
            L += ["Passos gravados (o que foi clicado, sem valores):", "", "| # | s | Ação | Elemento | Texto / rótulo | Caminho | Quadro |", "|---:|---:|---|---|---|---|---|"]
            for k, c in enumerate(it["cliques"], 1):
                el = " ".join(x for x in (c.get("tag", ""), f"[{c['papel']}]" if c.get("papel") else "", f"#{c['id']}" if c.get("id") else "",
                                          f"name={c['nome']}" if c.get("nome") else "", f"testid={c['testid']}" if c.get("testid") else "") if x)
                txt = " · ".join(x for x in (c.get("texto"), f"rótulo: {c['rotulo']}" if c.get("rotulo") else "", f"título: {c['titulo']}" if c.get("titulo") else "",
                                             f"escolheu: {c['escolha']}" if c.get("escolha") else "", f"{c['caracteres']} caracteres" if c.get("caracteres") is not None else "",
                                             f"link: {c['link']}" if c.get("link") else "") if x)
                L.append(f"| {k} | {c['t']} | {c['acao']} | {el} | {txt.replace('|', '/')} | {('`' + c['caminho'] + '`') if c.get('caminho') else ''} | {c.get('quadro', '')} |")
            L.append("")
        if it["websockets"]:
            L.append(f"- WebSocket: {', '.join(sorted(set(it['websockets'])))}")
            L.append("")
    L += ["## Todas as chamadas de dados", "", "| Chamada | Método | Endereço | Vezes | Itens | Resposta | Registros | Corpo enviado |", "|---|---|---|---:|---|---|---:|---|"]
    for g in sorted(d["chamadas"], key=lambda g: (g["host"], g["caminho"])):
        if g["natureza"] != "dados" or g["tipo"] not in ("xhr", "fetch", "document", "other", "eventsource"):
            continue
        corpo = g.get("corpo_enviado")
        corpo_txt = "" if not corpo else (corpo["forma"] + (": " + json.dumps(corpo.get("conteudo"), ensure_ascii=False)[:140] if corpo.get("conteudo") is not None else ""))
        L.append(f"| {g['id']} | {g['metodo']} | `{g['host']}{g['caminho']}` | {g['vezes']} | {', '.join(g['itens'][:6])}{' ...' if len(g['itens']) > 6 else ''} | "
                 f"{', '.join(g['tipos_resposta'])} até {_kb(_tam(g))} | {_n(g['registros_max']) if g['registros_max'] else '-'} | {corpo_txt.replace('|', '/')} |")
    aut = [g for g in d["chamadas"] if g["natureza"] == "autenticação"]
    if aut:
        L += ["", "## Autenticação (só os caminhos)", ""] + [f"- {g['metodo']} `{g['host']}{g['caminho']}` ×{g['vezes']}" for g in aut]
    L += ["", f"Recursos estáticos ignorados (imagens, fontes, scripts): {', '.join(f'{h} {n}' for h, n in d['estaticos_por_host'].items())}", ""]
    return "\n".join(L)
