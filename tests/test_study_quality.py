import unittest
from study_plot_expr import compile_expression, expand_plot
from study_figures import plot
from study_quality import document_issues, requested_curves
from study_markdown import parse_markdown
from study_pdf import document_html, readable_title

class QualityTests(unittest.TestCase):
    def test_expression(self):
        self.assertAlmostEqual(compile_expression("x**3-3*x")(2), 2)
        self.assertAlmostEqual(compile_expression("sin(pi/2)")(0), 1)
        for expr in ("__import__('os')", "x.__class__", "2**100000", "[x for x in (1,2)]"):
            with self.assertRaises((ValueError, SyntaxError)):
                compile_expression(expr)(1)
        self.assertIn("<polyline", plot({"title":"test", "domain":[-2,2],"series":[{"name":"f","expr":"x**3-3*x"}]}))
    def test_checks(self):
        req = "PDF 10–15 страниц, графики исходной функции, первой и второй производных, схема и таблица"
        self.assertEqual(requested_curves(req), 3)
        self.assertGreaterEqual(len(document_issues(req, "# Тема\nКороткий ответ")), 4)
    def test_layout(self):
        html = document_html("Исследование_функций", parse_markdown("# Исследование функций\n\n## 1. Определения\n\n**Определение 1 (Производная):** текст\n\nДоказательство: Начало\n\n1. Шаг\n\nСледовательно, ответ. □"))
        self.assertEqual(readable_title("Исследование_функций"), "Исследование функций")
        self.assertIn('class="def-block"', html)
        self.assertIn('class="proof-step"', html)
        self.assertNotIn('>1. Определения<', html)

if __name__ == "__main__":
    unittest.main()
