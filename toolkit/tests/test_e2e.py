# -*- coding: utf-8 -*-
"""Ponta a ponta: ensina uma receita numa página local (data, lista e botão que monta um CSV a partir de JSON),
repete a receita num perfil novo e confere o arquivo e o mapa da rede. Pulado sem Playwright ou sem Chromium."""
import json
import sys
import tempfile
import threading
import unittest
from datetime import date
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as _p:
        _p.chromium.launch(headless=True).close()
    PLAYWRIGHT = True
except Exception:  # noqa: BLE001
    PLAYWRIGHT = False

PAGINA = b"""<!doctype html><html><body><h1>Vendas</h1>
<label for=ini>Data inicial</label><input id=ini type=text>
<label for=tipo>Tipo</label><select id=tipo><option>A</option><option>B</option></select>
<button id=exp>Exportar CSV</button>
<script>
document.getElementById('exp').onclick = () => {
  fetch('/api/dados?tipo=' + tipo.value + '&ini=' + encodeURIComponent(ini.value)).then(r => r.json()).then(j => {
    const csv = 'id,valor\\n' + j.registros.map(r => r.id + ',' + r.valor).join('\\n');
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([csv], {type: 'text/csv'}));
    a.download = 'vendas.csv'; document.body.appendChild(a); a.click();
  });
};
</script></body></html>"""


class _Site(SimpleHTTPRequestHandler):
    chamadas: list[str] = []

    def log_message(self, *a):  # noqa: D401
        pass

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/api/dados"):
            _Site.chamadas.append(self.path)
            corpo = json.dumps({"total": 3, "registros": [{"id": i, "valor": i * 10} for i in range(1, 4)]}).encode()
            tipo = "application/json"
        else:
            corpo, tipo = PAGINA, "text/html"
        self.send_response(200)
        self.send_header("Content-Type", tipo)
        self.end_headers()
        self.wfile.write(corpo)


@unittest.skipUnless(PLAYWRIGHT, "Playwright com Chromium não disponível")
class EnsinarERepetir(unittest.TestCase):
    def setUp(self):
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), _Site)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/vendas"
        self.tmp = Path(tempfile.mkdtemp())
        _Site.chamadas = []

    def tearDown(self):
        self.srv.shutdown()

    def test_aula_vira_receita_e_a_receita_baixa_sozinha(self):
        import navegador
        import receitas

        def roteiro(page):
            page.fill("#ini", "01/11/2026")
            page.select_option("#tipo", "B")
            with page.expect_download():
                page.click("#exp")
            page.wait_for_timeout(800)

        hoje = date(2026, 11, 12)
        rec = self.tmp / "receitas"
        res = receitas.ensinar(self.url, "vendas", "Vendas", self.tmp / "dados", canal=None, headless=True, roteiro=roteiro,
                               perfil=self.tmp / "perfil_aula", hoje=hoje, pasta_receitas=rec, perguntar=False)
        self.assertIsNotNone(res)
        receita, _ = res
        acoes = [p["acao"] for p in receita["passos"]]
        self.assertEqual(sorted(acoes), ["baixar", "digitar", "escolher"])
        digitado = next(p for p in receita["passos"] if p["acao"] == "digitar")
        self.assertEqual(digitado["valor"], "{inicio_mes:%d/%m/%Y}")  # a data vira relativa ao dia

        # o mapa da aula explica de onde veio o CSV, sem guardar valores
        mapa = next((self.tmp / "dados" / "_logs").glob("gravacao_vendas_*.md")).read_text(encoding="utf-8")
        self.assertIn("montado na página a partir de uma resposta JSON", mapa)
        self.assertIn("as 3 linhas batem", mapa)

        navegador.PROFILE, navegador.CANAL = self.tmp / "perfil_repeticao", None
        arquivos = receitas.repetir("vendas", self.tmp / "dados2", headless=True, pasta_receitas=rec)
        self.assertEqual(len(arquivos), 1)
        self.assertEqual(arquivos[0].read_text().splitlines(), ["id,valor", "1,10", "2,20", "3,30"])
        self.assertTrue(all("tipo=B" in c for c in _Site.chamadas))
        self.assertEqual(len(_Site.chamadas), 2)  # uma na aula, uma na repetição


if __name__ == "__main__":
    unittest.main()
