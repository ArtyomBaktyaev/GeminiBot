"""Safe scalar expression evaluator for vector graphs. Never uses eval/exec."""
import ast
import math
import operator

FUNCTIONS = {name: getattr(math, name) for name in ("sin", "cos", "tan", "exp", "log", "sqrt", "asin", "acos", "atan")}
FUNCTIONS["abs"] = abs
OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}


def compile_expression(expression):
    if not isinstance(expression, str) or len(expression) > 180:
        raise ValueError("Слишком длинное выражение")
    tree = ast.parse(expression.replace("^", "**"), mode="eval")
    if len(list(ast.walk(tree))) > 80:
        raise ValueError("Слишком сложное выражение")

    def run(node, x):
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            result = node.value
        elif isinstance(node, ast.Name) and node.id in ("x", "pi", "e"):
            result = {"x": x, "pi": math.pi, "e": math.e}[node.id]
        elif isinstance(node, ast.UnaryOp) and type(node.op) in (ast.UAdd, ast.USub):
            result = run(node.operand, x) * (-1 if isinstance(node.op, ast.USub) else 1)
        elif isinstance(node, ast.BinOp) and type(node.op) in OPS:
            result = OPS[type(node.op)](run(node.left, x), run(node.right, x))
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow):
            a, b = run(node.left, x), run(node.right, x)
            if abs(b) > 30 or (abs(a) > 1e6 and b > 2):
                raise ValueError("Слишком большая степень")
            result = float(a) ** float(b)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FUNCTIONS and len(node.args) == 1 and not node.keywords:
            result = FUNCTIONS[node.func.id](run(node.args[0], x))
        else:
            raise ValueError("Недопустимое математическое выражение")
        if type(result) not in (int, float) or not math.isfinite(result) or abs(result) > 1e12:
            raise ValueError("Неконечное или слишком большое значение")
        return result

    return lambda x: run(tree.body, x)


def expand_plot(data):
    if not isinstance(data, dict):
        raise ValueError("Ожидается объект графика")
    if "domain" not in data:
        return data
    domain = data["domain"]
    series = data.get("series")
    if not isinstance(domain, list) or len(domain) != 2 or not all(type(v) in (int, float) and math.isfinite(v) and abs(v) <= 1e9 for v in domain):
        raise ValueError("Некорректный диапазон")
    a, b = domain
    if not a < b or b-a < 1e-9 or not isinstance(series, list) or not 1 <= len(series) <= 5:
        raise ValueError("Некорректный диапазон или число кривых")
    x = [a + (b-a)*i/240 for i in range(241)]
    result = []
    for item in series:
        if not isinstance(item, dict):
            raise ValueError("Некорректная кривая")
        try:
            fn = compile_expression(item["expr"])
            y = [fn(v) for v in x]
        except (ArithmeticError, SyntaxError, TypeError) as e:
            raise ValueError("Функция не определена на выбранном диапазоне") from e
        result.append({"name": item["name"], "y": y})
    return {**data, "x": x, "series": result}
