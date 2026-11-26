# -*- coding: utf-8 -*-
"""Testes das partes puras do toolkit: datas relativas das receitas e o que o mapa da rede deixa sair.

Rodar: python -m unittest discover -s toolkit/tests   (não precisa de navegador nem de rede)"""
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import mapa_rede  # noqa: E402
import receitas  # noqa: E402

HOJE = date(2026, 11, 12)


class DatasRelativas(unittest.TestCase):
    def test_tokens_conhecidos(self):
        self.assertEqual(receitas.dia_do_token("hoje", HOJE), HOJE)
        self.assertEqual(receitas.dia_do_token("hoje-1", HOJE), date(2026, 11, 11))
        self.assertEqual(receitas.dia_do_token("inicio_mes", HOJE), date(2026, 11, 1))
        self.assertEqual(receitas.dia_do_token("fim_mes", HOJE), date(2026, 11, 30))
        self.assertEqual(receitas.dia_do_token("inicio_mes_anterior", HOJE), date(2026, 10, 1))
        self.assertEqual(receitas.dia_do_token("fim_mes_anterior", HOJE), date(2026, 10, 31))
        self.assertEqual(receitas.dia_do_token("inicio_ano", HOJE), date(2026, 1, 1))

    def test_fim_de_fevereiro_em_ano_bissexto(self):
        self.assertEqual(receitas.dia_do_token("fim_mes", date(2028, 2, 10)), date(2028, 2, 29))

    def test_token_do_dia_prefere_nome_ao_deslocamento(self):
        self.assertEqual(receitas.token_do_dia(date(2026, 1, 1), HOJE), "inicio_ano")
        self.assertEqual(receitas.token_do_dia(date(2026, 11, 5), HOJE), "hoje-7")

    def test_tokenizar_e_renderizar_fazem_o_caminho_de_volta(self):
        t = receitas.tokenizar("de 01/01/2026 até 12/11/2026", HOJE)
        self.assertEqual(t, "de {inicio_ano:%d/%m/%Y} até {hoje:%d/%m/%Y}")
        # um mês depois, a mesma receita pede o período atualizado
        self.assertEqual(receitas.renderizar(t, date(2026, 12, 15)), "de 01/01/2026 até 15/12/2026")

    def test_data_ambigua_usa_a_leitura_mais_perto_de_hoje(self):
        # 11.10.2026 pode ser 11/out ou 10/nov: 10/nov está mais perto de 12/nov
        self.assertEqual(receitas.tokenizar("11.10.2026", HOJE), "{hoje-2:%m.%d.%Y}")

    def test_ano_corrente_no_endereco_vira_token(self):
        url = receitas.tokenizar_endereco("https://exemplo.com/relatorio?year=2026&tipo=A", HOJE)
        self.assertIn("year={ano}", url)
        self.assertEqual(receitas.renderizar(url, date(2027, 3, 1)), "https://exemplo.com/relatorio?year=2027&tipo=A")

    def test_endereco_sem_data_fica_igual(self):
        url = "https://exemplo.com/x?tipo=A&b=1"
        self.assertEqual(receitas.tokenizar_endereco(url, HOJE), url)


class NomesDeArquivo(unittest.TestCase):
    def test_caracteres_invalidos_no_windows(self):
        self.assertEqual(receitas.nome_de_arquivo('a:b/c*d?.csv'), "a_b_c_d_.csv")

    def test_ponto_de_data_nao_e_extensao(self):
        self.assertEqual(receitas.extensao("Preview 11.12.2026"), "")
        self.assertEqual(receitas.extensao("export.xlsx"), ".xlsx")

    def test_nome_seguro(self):
        self.assertEqual(receitas.nome_seguro("  Relatório de vendas! "), "Relatório_de_vendas")
        self.assertEqual(receitas.nome_seguro(""), "pagina")


class MapaNaoVaza(unittest.TestCase):
    def test_campos_sensiveis_somem(self):
        for chave in ("password", "access_token", "Authorization", "email", "cpf", "createdBy", "nome"):
            self.assertEqual(mapa_rede.oculto(chave, "qualquer"), "{oculto}", chave)

    def test_valores_sensiveis_somem_mesmo_em_campo_neutro(self):
        self.assertEqual(mapa_rede.oculto("q", "ana.silva@exemplo.com"), "{oculto}")
        self.assertEqual(mapa_rede.oculto("q", "eyJhbGciOiJIUzI1NiJ9.e30.x"), "{oculto}")
        self.assertEqual(mapa_rede.oculto("q", "Maria da Silva"), "{oculto}")
        self.assertEqual(mapa_rede.oculto("q", "A" * 40), "{oculto}")

    def test_filtros_curtos_e_numeros_ficam(self):
        self.assertEqual(mapa_rede.oculto("status", "Open"), "Open")
        self.assertEqual(mapa_rede.oculto("ano", 2026), 2026)
        self.assertEqual(mapa_rede.oculto("q", "x" * 60), "{texto, 60 car.}")

    def test_molde_do_caminho(self):
        self.assertEqual(mapa_rede.molde("/api/orders/12345/items"), "/api/orders/{id}/items")
        self.assertEqual(mapa_rede.molde("/api/doc/3f2a9c4e-1b2d-4e5f-9a8b-7c6d5e4f3a2b"), "/api/doc/{id}")
        self.assertEqual(mapa_rede.molde("/users/ana@exemplo.com"), "/users/{email}")

    def test_endereco_separa_host_caminho_e_filtros_seguros(self):
        host, caminho, filtros = mapa_rede.endereco("https://dados.exemplo.com/api/v1/report/42?year=2026&token=abc&user=ana")
        self.assertEqual(host, "dados.exemplo.com")
        self.assertEqual(caminho, "/api/v1/report/{id}")
        self.assertEqual(filtros, {"year": "2026", "token": "{oculto}", "user": "{oculto}"})

    def test_corpo_de_post_sem_chaves_suspeitas(self):
        corpo = {"filtros": {"status": "Open", "senha": "x"}, "Maria Souza": 1, "pagina": 2}
        seguro = mapa_rede.corpo_seguro(corpo)
        self.assertEqual(seguro["filtros"], {"status": "Open", "senha": "{oculto}"})
        self.assertNotIn("Maria Souza", seguro)
        self.assertEqual(seguro["pagina"], 2)


if __name__ == "__main__":
    unittest.main()
