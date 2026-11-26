# Record-and-replay toolkit

Three Playwright tools for automating report downloads from web portals **without writing selectors and without exposing your session**. They come from the extractor of [Órbita](../README.md), refactored to run on their own.

| Tool | What it does |
|---|---|
| `receitas.py` | **Learn by demonstration.** Opens a browser on a persistent profile; you click your way to a download once and close the browser. The steps become a JSON *recipe* (what was clicked, chosen and typed, in which frame and tab). Dates you typed become relative to the day, so `01/11/2026` recorded in November becomes `{inicio_mes:%d/%m/%Y}` and still means "start of the month" next year. `--repetir NAME` replays it. |
| `gravar_pagina.py` | **A shareable DevTools Network tab.** Records what the page asks its server for and the steps up to the download, then writes a Markdown and a JSON report. |
| `mapa_rede.py` | **Where did this file come from?** For each download it tells whether the server sent the file ready-made (so it can be requested directly) or the page **assembled it from a JSON/HTML response**, and which field became which column. |

What never goes into a recipe or a report: cookies, auth headers, tokens, anything typed on a login page, passwords, and data values (names, e-mails, long text, JWT-like strings, the current user's login). Only URLs with IDs replaced by placeholders, short filter codes, field names, types and counts are kept. The filter is in `mapa_rede.oculto` and is covered by tests.

## Example

Recording a page whose "Export CSV" button fetches JSON and builds the CSV in the browser produces this (from the end-to-end test):

```
Aprendi 3 passo(s):
  0. abrir http://127.0.0.1:39337/vendas
  1. escolher B em "Tipo"
  2. digitar 'início do mês' em "Data inicial"
  3. clicar em "Exportar CSV" e baixar o arquivo do site (vendas.csv)
```

```
| Arquivo    | Linhas | Origem        | Chamada | Veredito                                                                         |
| vendas.csv | 3      | página (blob) | c2      | montado na página a partir de uma resposta JSON: 2 de 2 colunas e as 3 linhas batem |

| c2 | GET | 127.0.0.1:39337/api/dados?tipo=B&ini=01/11/2026 | 200 | application/json 99 B | 3 registros |
```

That verdict is what justifies replacing the click-through with a single request to `/api/dados`.

## Usage

```bash
pip install -r requirements.txt
python -m playwright install chromium        # or use Edge/Chrome already installed: TOOLKIT_CANAL=msedge|chrome

python receitas.py "https://portal.example.com/reports" --nome vendas --pasta Vendas   # teach once
python receitas.py --lista                                                              # list recipes
python receitas.py --repetir vendas                                                     # download again
python gravar_pagina.py "https://portal.example.com/reports" --nome vendas              # network report
```

Downloads go to `./dados` (or `TOOLKIT_DADOS`). Recipes live in `receitas/`, reports in `dados/_logs/`. The first run on a site that needs login waits up to 5 minutes for you to sign in; the session stays in the browser profile, never in the recipe.

## Tests

```bash
python -m unittest discover -s tests -v
```

17 tests: date tokenization and rendering (including leap years and ambiguous `dd.mm`/`mm.dd` dates), file naming, the privacy filter, and an end-to-end run that serves a local page, teaches a recipe, replays it in a fresh profile and checks both the CSV and the network map. The end-to-end test is skipped when Chromium isn't installed.
