"""Interface local para download e extração de dados públicos do CNPJ."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from queue import Empty, Queue
import re
import shutil
import subprocess
import sys
from threading import Thread
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from urllib.request import Request, urlopen

from extrator_cnpj import BASE

FUNDO = "#101820"
PAINEL = "#192630"
TEXTO = "#edf5f7"
SUAVE = "#aac0cb"
DESTAQUE = "#5de1b7"


def tamanho_zip(competencia, lote):
    nome = f"Estabelecimentos{lote}.zip"
    try:
        with urlopen(Request(f"{BASE}/{competencia}/{nome}", method="HEAD"), timeout=10) as resposta:
            tamanho = int(resposta.headers.get("Content-Length", "0"))
            if resposta.headers.get_content_type() == "application/zip" and tamanho > 0:
                return tamanho
    except (OSError, ValueError):
        pass
    return None


def tamanho_formatado(bytes_):
    unidade, divisor = ("GB", 10**9) if bytes_ >= 10**9 else ("MB", 10**6)
    return f"{bytes_ / divisor:.2f}".replace(".", ",") + f" {unidade}"


def consultar_tamanhos(competencia, eventos):
    with ThreadPoolExecutor(max_workers=4) as pool:
        tamanhos = list(pool.map(lambda lote: tamanho_zip(competencia, lote), range(10)))
    eventos.put(("tamanhos", (competencia, tamanhos)))


def lotes_processados(competencia, pasta=Path("dump")):
    if not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", competencia):
        return set()
    processados = set()
    for caminho in (pasta / competencia / "historico").glob("*.json"):
        try:
            registro = json.loads(caminho.read_text(encoding="utf-8"))
            if registro["competencia"] == competencia:
                processados.update(registro["lotes"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return processados


def executar(comando, eventos):
    resultado = "Falha na extração. Confira os detalhes abaixo."
    try:
        with subprocess.Popen(
            comando, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors="replace",
        ) as processo:
            for linha in processo.stdout:
                linha = linha.rstrip()
                if not linha:
                    continue
                eventos.put(("log", linha))
                if linha.startswith("Nenhum resultado"):
                    resultado = "Nenhum resultado para os filtros escolhidos."
                elif " registros gravados em " in linha:
                    resultado = linha
            if processo.wait() != 0:
                resultado = "Falha na extração. Confira os detalhes abaixo."
    except OSError as erro:
        eventos.put(("log", str(erro)))
    eventos.put(("fim", resultado))


def main():
    janela = tk.Tk()
    janela.title("Extrator CNPJ | Sify")
    janela.geometry("790x760")
    janela.minsize(680, 675)
    janela.configure(background=FUNDO)
    tema = ttk.Style(janela)
    tema.theme_use("clam")
    tema.configure("TFrame", background=FUNDO)
    tema.configure("TLabel", background=FUNDO, foreground=TEXTO)
    tema.configure("Nota.TLabel", foreground=SUAVE)
    tema.configure("TCheckbutton", background=FUNDO, foreground=TEXTO)
    tema.map("TCheckbutton", background=[("active", FUNDO)], foreground=[("active", TEXTO)])
    tema.configure("Treeview", background=PAINEL, fieldbackground=PAINEL, foreground=TEXTO,
                   rowheight=25, borderwidth=0)
    tema.configure("Treeview.Heading", background="#263947", foreground=TEXTO, relief="flat")
    tema.map("Treeview", background=[("selected", DESTAQUE)], foreground=[("selected", FUNDO)])
    tema.configure("Acao.TButton", background=DESTAQUE, foreground=FUNDO, padding=8)
    tema.map("Acao.TButton", background=[("active", "#91efcf"), ("disabled", PAINEL)])
    tema.configure("Horizontal.TProgressbar", background=DESTAQUE, troughcolor=PAINEL,
                   bordercolor=PAINEL, lightcolor=DESTAQUE, darkcolor=DESTAQUE)

    painel = ttk.Frame(janela, padding=22)
    painel.pack(fill="both", expand=True)
    ttk.Label(painel, text="Extrator de CNPJ", font=("TkDefaultFont", 22, "bold")).pack(anchor="w")
    ttk.Label(painel, text="Pequeno varejo • Selecione até dois ZIPs com Ctrl + clique.",
              style="Nota.TLabel").pack(anchor="w", pady=(2, 12))

    config = ttk.Frame(painel)
    config.pack(fill="x", pady=(0, 8))
    competencia = tk.StringVar(value="2026-11")
    ttk.Label(config, text="Competência").pack(side="left")
    ttk.Entry(config, width=9, textvariable=competencia).pack(side="left", padx=8)
    atualizar_botao = ttk.Button(config, text="Consultar tamanhos")
    atualizar_botao.pack(side="left")

    lotes = ttk.Treeview(painel, columns=("arquivo", "tamanho", "historico"), show="headings", height=10)
    lotes.heading("arquivo", text="ZIP da Receita Federal")
    lotes.heading("tamanho", text="Tamanho no servidor")
    lotes.heading("historico", text="Histórico")
    lotes.column("arquivo", width=290)
    lotes.column("historico", width=120)
    lotes.column("tamanho", width=170, anchor="e")
    for i in range(10):
        lotes.insert("", "end", iid=str(i), values=(f"Estabelecimentos{i}.zip", "—"))
    lotes.pack(fill="x")
    resumo = tk.StringVar()
    ttk.Label(painel, textvariable=resumo, style="Nota.TLabel").pack(anchor="w", pady=(6, 8))

    filtros = ttk.Frame(painel)
    filtros.pack(fill="x", pady=6)
    inicio = tk.StringVar(value="2026-01-01")
    fim = tk.StringVar(value="2026-11-20")
    todos_go = tk.BooleanVar()
    for rotulo, valor in (("De", inicio), ("Até", fim)):
        ttk.Label(filtros, text=rotulo).pack(side="left")
        ttk.Entry(filtros, width=12, textvariable=valor).pack(side="left", padx=(5, 12))
    ttk.Checkbutton(filtros, text="Todo Goiás (padrão: Goiânia)", variable=todos_go).pack(side="left")

    limpar = tk.BooleanVar()
    ttk.Checkbutton(painel, text="Liberar espaço após a extração (ZIPs desta execução)",
                    variable=limpar).pack(anchor="w", pady=(3, 0))

    acoes = ttk.Frame(painel)
    acoes.pack(fill="x", pady=(8, 5))
    extrair = ttk.Button(acoes, text="Baixar e extrair", style="Acao.TButton")
    extrair.pack(side="left")
    local = ttk.Button(acoes, text="Abrir ZIP ou .ESTABELE local")
    local.pack(side="left", padx=8)
    barra = ttk.Progressbar(painel, mode="indeterminate")
    barra.pack(fill="x", pady=(5, 3))
    status = tk.StringVar(value="Selecione os lotes ou abra um arquivo local.")
    ttk.Label(painel, textvariable=status, wraplength=650).pack(anchor="w", pady=(2, 6))
    log = tk.Text(painel, height=6, wrap="word", state="disabled", background=PAINEL,
                  foreground=TEXTO, relief="flat", padx=8, pady=6)
    log.pack(fill="both", expand=True)

    eventos = Queue()
    tamanhos = {}
    consultando = False
    ativo = False

    def mostrar_resumo(*_):
        selecao = lotes.selection()
        total = [tamanhos.get((competencia.get().strip(), lote)) for lote in selecao]
        texto = "Selecione os ZIPs que deseja baixar."
        if selecao:
            texto = f"{len(selecao)} selecionado(s) • Tamanho total "
            texto += tamanho_formatado(sum(total)) if all(total) else "indisponível"
        livre = tamanho_formatado(shutil.disk_usage(Path.cwd()).free)
        resumo.set(f"{texto}   ·   {livre} livres no disco de download")

    def consultar():
        nonlocal consultando
        mes = competencia.get().strip()
        if not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", mes):
            messagebox.showinfo("Competência", "Use o formato AAAA-MM.", parent=janela)
            return
        consultando = True
        atualizar_botao.state(["disabled"])
        for lote in lotes.get_children():
            lotes.set(lote, "tamanho", "Consultando…")
        if not ativo:
            barra.start(15)
        Thread(target=consultar_tamanhos, args=(mes, eventos), daemon=True).start()

    def atualizar_tabela(*_):
        mes = competencia.get().strip()
        processados = lotes_processados(mes)
        for lote in lotes.get_children():
            valor = tamanhos.get((mes, lote))
            lotes.set(lote, "tamanho", tamanho_formatado(valor) if valor else "—")
            nome = f"Estabelecimentos{lote}.zip"
            lotes.set(lote, "historico", "Já processado" if nome in processados else "—")
        mostrar_resumo()

    def atualizar():
        nonlocal ativo, consultando
        for _ in range(200):
            try:
                tipo, valor = eventos.get_nowait()
            except Empty:
                break
            if tipo == "tamanhos":
                mes, valores = valor
                tamanhos.update({(mes, str(i)): tamanho for i, tamanho in enumerate(valores)})
                consultando = False
                atualizar_botao.state(["!disabled"])
                atualizar_tabela()
                if not ativo:
                    barra.stop()
            elif tipo == "fim":
                ativo = False
                extrair.state(["!disabled"])
                local.state(["!disabled"])
                status.set(valor)
                atualizar_tabela()
                if not consultando:
                    barra.stop()
            else:
                log.configure(state="normal")
                log.insert("end", valor + "\n")
                log.see("end")
                log.configure(state="disabled")
        janela.after(125, atualizar)

    def iniciar(arquivos_locais=False):
        nonlocal ativo
        selecao = lotes.selection()
        arquivos = ()
        if arquivos_locais:
            arquivos = filedialog.askopenfilenames(
                title="Escolha os arquivos locais", parent=janela,
                filetypes=[("Arquivos do CNPJ", ("*.zip", "*.ZIP", "*.ESTABELE", "*.estabele", "*.stable", "*.STABLE"))],
            )
            if not arquivos:
                return
            if len(arquivos) > 2:
                messagebox.showinfo("Seleção", "Escolha até dois arquivos.", parent=janela)
                return
        elif not 1 <= len(selecao) <= 2:
            messagebox.showinfo("Seleção", "Escolha um ou dois ZIPs.", parent=janela)
            return
        saida = filedialog.asksaveasfilename(
            title="Salvar CSV", initialfile="pequeno_varejo.csv", defaultextension=".csv",
            filetypes=[("CSV", "*.csv")], parent=janela,
        )
        if not saida:
            return
        extrator = Path(__file__).with_name("extrator_cnpj.py").resolve()
        comando = [sys.executable, str(extrator), "--saida", saida,
                   "--inicio", inicio.get().strip(), "--fim", fim.get().strip()]
        if todos_go.get():
            comando.append("--todos-go")
        if arquivos_locais:
            for arquivo in arquivos:
                comando.extend(["--arquivo", arquivo])
        else:
            comando.extend(["--baixar", competencia.get().strip()])
            if limpar.get():
                comando.append("--limpar-downloads")
            for lote in selecao:
                comando.extend(["--lote", lote])
        log.configure(state="normal")
        log.delete("1.0", "end")
        log.configure(state="disabled")
        extrair.state(["disabled"])
        local.state(["disabled"])
        status.set("Processando arquivos…")
        ativo = True
        barra.start(15)
        Thread(target=executar, args=(comando, eventos), daemon=True).start()

    def fechar():
        if ativo:
            messagebox.showinfo("Extração em andamento", "Aguarde a conclusão para fechar esta janela.", parent=janela)
        else:
            janela.destroy()

    atualizar_botao.configure(command=consultar)
    extrair.configure(command=iniciar)
    local.configure(command=lambda: iniciar(arquivos_locais=True))
    lotes.bind("<<TreeviewSelect>>", mostrar_resumo)
    competencia.trace_add("write", atualizar_tabela)
    janela.protocol("WM_DELETE_WINDOW", fechar)
    atualizar_tabela()
    janela.after(100, consultar)
    atualizar()
    janela.mainloop()


if __name__ == "__main__":
    main()
