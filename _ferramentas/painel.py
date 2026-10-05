#!/usr/bin/env python3
"""Gera painel/index.html a partir de _log/atividade.jsonl, contas/ e fila-linkedin.md.
Roda no Mac a cada sync. O mesmo arquivo e publicado como Artifact pelas rotinas."""
import json, pathlib, re, datetime, os
codigo = pathlib.Path(__file__).resolve().parent.parent
raiz = pathlib.Path(os.environ.get("THUNDER_BRAIN_ROOT", str(codigo))).expanduser().resolve()
def ler(p):
    try: return p.read_text(encoding="utf-8")
    except Exception: return ""

def gerar(modo="artifact", arquivo_modelo="painel_modelo.html") -> str:
    """Renderiza o painel sem escrever arquivos ou executar serviços."""
    if modo not in ("artifact", "servidor"):
        raise ValueError("modo inválido")
    eventos = []
    for l in ler(raiz/"_log"/"atividade.jsonl").splitlines():
        l = l.strip()
        if not l: continue
        try: eventos.append(json.loads(l))
        except Exception: pass
    eventos.sort(key=lambda e: e.get("ts",""), reverse=True)
    contas = []
    for d in sorted(p for p in (raiz/"contas").glob("*") if p.is_dir()):
        if d.name.startswith("_"): continue
        ficha = ler(d/"ficha.md")
        titulo = next((x[2:].strip() for x in ficha.splitlines() if x.startswith("# ")), d.name)
        est = re.search(r"^- Est[aá]gio:[ \t]*(.*)$", ficha, re.M)
        prox = re.search(r"## Pr[oó]ximo passo[ \t]*\n+(.+)", ficha)
        evs = [e for e in eventos if e.get("conta")==d.name]
        contas.append({"slug": d.name, "nome": titulo, "estagio": (est.group(1).strip() if est else ""),
            "proximo": (prox.group(1).strip() if prox and not prox.group(1).startswith("#") else ""),
            "ultimo": evs[0] if evs else None, "n": len(evs),
            "respostas": sum(1 for e in evs if e.get("tipo")=="resposta")})
    fila = []
    for l in ler(raiz/"fila-linkedin.md").splitlines():
        if l.startswith("|") and not l.startswith("|---") and "LinkedIn (URL" not in l:
            c = [x.strip() for x in l.strip().strip("|").split("|")]
            if len(c) >= 6 and any(c): fila.append(dict(zip(["conta","pessoa","url","acao","ultima","post"], c)))
    # O gerador não consulta Git nem chama processos ou serviços externos.
    sync = ""
    dados = {"gerado": datetime.datetime.now().astimezone().isoformat(timespec="minutes"), "sync": sync,
             "eventos": eventos[:300], "contas": contas, "fila": fila}
    modelo = (codigo/"_ferramentas"/arquivo_modelo).read_text(encoding="utf-8")
    saida = modelo.replace("/*DADOS*/{}", json.dumps(dados, ensure_ascii=False).replace("</","<\\/"))
    executor_linhas = ler(raiz/"_ferramentas"/"painel-executor.txt").splitlines() if modo == "artifact" else []
    executor = executor_linhas[0].strip() if executor_linhas else ""
    # Serializa como string JavaScript, incluindo aspas e fechamento de script.
    executor_js = json.dumps(executor, ensure_ascii=True).replace("</", "<\\/")
    saida = saida.replace("/*EXECUTOR*/''", executor_js if executor else "''")
    saida = saida.replace("/*MODO*/'artifact'", json.dumps(modo))
    if modo == "servidor":
        saida = re.sub(r'<link[^>]+href="https://fonts\.googleapis\.com[^>]*>\n?', "", saida)
    return saida


if __name__ == "__main__":
    saida = gerar()
    (raiz/"painel").mkdir(exist_ok=True)
    (raiz/"painel"/"index.html").write_text(saida, encoding="utf-8")
    print("painel: painel/index.html gerado")
