"""Interface local para download e extração de dados públicos do CNPJ."""
import csv
import json
from pathlib import Path
from queue import Empty, Queue
import re
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory, mkdtemp
from time import sleep
from threading import Thread
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from urllib.parse import unquote
from urllib.request import Request, urlopen
from xml.etree import ElementTree

from extrator_cnpj import BASE

FUNDO = "#101820"
PAINEL = "#192630"
TEXTO = "#edf5f7"
SUAVE = "#aac0cb"
DESTAQUE = "#5de1b7"


MES = r"[0-9]{4}-(0[1-9]|1[0-2])"


def listar_receita(pasta=""):
    pedido = Request(f"{BASE}/{pasta}", headers={"Depth": "1"}, method="PROPFIND")
    with urlopen(pedido, timeout=15) as resposta:
        raiz = ElementTree.parse(resposta).getroot()
    itens = {}
    for entrada in raiz.findall("{DAV:}response"):
        href = entrada.findtext("{DAV:}href", "")
        nome = unquote(href.rstrip("/").rsplit("/", 1)[-1])
        tamanho = entrada.findtext(".//{DAV:}getcontentlength")
        itens[nome] = int(tamanho) if tamanho and tamanho.isdigit() else None
    return itens


def tamanho_formatado(bytes_):
    unidade, divisor = ("GB", 10**9) if bytes_ >= 10**9 else ("MB", 10**6)
    return f"{bytes_ / divisor:.2f}".replace(".", ",") + f" {unidade}"


def consultar_tamanhos(competencia, eventos, descobrir_meses):
    try:
        itens = listar_receita(f"{competencia}/")
        tamanhos = [itens.get(f"Estabelecimentos{i}.zip") for i in range(10)]
        eventos.put(("catalogo", (competencia, tamanhos)))
        if descobrir_meses:
            meses = sorted((nome for nome in listar_receita() if re.fullmatch(MES, nome)), reverse=True)
            eventos.put(("meses", meses))
    except (OSError, ValueError, ElementTree.ParseError) as erro:
        eventos.put(("log", f"Consulta à Receita interrompida. {erro}"))
    finally:
        eventos.put(("consulta_concluida", None))


def lotes_processados(competencia, pasta=Path("dump")):
    if not re.fullmatch(MES, competencia):
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


def executar(comando, eventos, avisar_fim=True):
    resultado = "Falha na extração. Confira os detalhes abaixo."
    sucesso = False
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
            sucesso = processo.wait() == 0
            if not sucesso:
                resultado = "Falha na extração. Confira os detalhes abaixo."
    except OSError as erro:
        eventos.put(("log", str(erro)))
    if avisar_fim:
        eventos.put(("fim", resultado))
    return sucesso


def consolidar(saida, parciais):
    vistos = set()
    with TemporaryDirectory(dir=saida.parent) as pasta:
        consolidado = Path(pasta) / "consolidado.csv"
        with consolidado.open("w", encoding="utf-8-sig", newline="") as destino:
            escritor = None
            for parcial in parciais:
                if not parcial.is_file():
                    continue
                with parcial.open(encoding="utf-8-sig", newline="") as arquivo:
                    leitor = csv.DictReader(arquivo, delimiter=";")
                    if escritor is None:
                        escritor = csv.DictWriter(destino, fieldnames=leitor.fieldnames, delimiter=";")
                        escritor.writeheader()
                    for linha in leitor:
                        cnpj = linha["CNPJ"]
                        if cnpj not in vistos:
                            escritor.writerow(linha)
                            vistos.add(cnpj)
        if vistos:
            consolidado.replace(saida)
    return len(vistos)


def executar_fila(comando, eventos, fila, saida):
    try:
        saida.parent.mkdir(parents=True, exist_ok=True)
        pasta = Path(mkdtemp(prefix=f"{saida.stem}_lotes_", dir=saida.parent))
        parciais = []
        for posicao, (mes, lote) in enumerate(fila, start=1):
            if posicao > 1:
                eventos.put(("log", "Aguardando 15 segundos antes do próximo lote."))
                sleep(15)
            eventos.put(("log", f"Lote {posicao}/{len(fila)} • {mes} / Estabelecimentos{lote}.zip"))
            parcial = pasta / f"{mes}_lote{lote}.csv"
            if not executar([*comando, "--baixar", mes, "--lote", lote, "--saida", str(parcial)],
                            eventos, avisar_fim=False):
                eventos.put(("fim", f"Fila interrompida no lote {posicao}. CSVs concluídos em {pasta}."))
                return
            parciais.append(parcial)
        total = consolidar(saida, parciais)
        if total:
            eventos.put(("fim", f"{total} CNPJs únicos gravados em {saida}"))
        else:
            pasta.rmdir()
            eventos.put(("fim", "Nenhum resultado nos lotes selecionados."))
    except (OSError, ValueError, csv.Error) as erro:
        eventos.put(("log", str(erro)))
        eventos.put(("fim", "Falha ao processar a fila. Confira o registro abaixo."))



def main():
    janela = tk.Tk()
    janela.title("Extrator CNPJ | Sify")
    janela.geometry("820x830")
    janela.minsize(700, 730)
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
    ttk.Label(painel, text="Pequeno varejo • Selecione ZIPs com Ctrl + clique e monte a fila por competência.",
              style="Nota.TLabel").pack(anchor="w", pady=(2, 12))

    config = ttk.Frame(painel)
    config.pack(fill="x", pady=(0, 8))
    competencia = tk.StringVar(value="2026-11")
    ttk.Label(config, text="Competência").pack(side="left")
    meses = ttk.Combobox(config, width=9, textvariable=competencia)
    meses.pack(side="left", padx=8)
    atualizar_botao = ttk.Button(config, text="Consultar tamanhos")
    atualizar_botao.pack(side="left")

    lotes = ttk.Treeview(painel, columns=("arquivo", "tamanho", "historico", "fila"), show="headings", height=10)
    lotes.heading("arquivo", text="ZIP da Receita Federal")
    lotes.heading("tamanho", text="Tamanho no servidor")
    lotes.heading("historico", text="Histórico")
    lotes.heading("fila", text="Fila")
    lotes.column("fila", width=70, anchor="center")
    lotes.column("arquivo", width=290)
    lotes.column("historico", width=120)
    lotes.column("tamanho", width=170, anchor="e")
    for i in range(10):
        lotes.insert("", "end", iid=str(i), values=(f"Estabelecimentos{i}.zip", "—"))
    lotes.pack(fill="x")
    resumo = tk.StringVar()
    ttk.Label(painel, textvariable=resumo, style="Nota.TLabel").pack(anchor="w", pady=(6, 8))

    controles_fila = ttk.Frame(painel)
    controles_fila.pack(fill="x")
    adicionar = ttk.Button(controles_fila, text="Adicionar à fila")
    adicionar.pack(side="left")
    remover = ttk.Button(controles_fila, text="Remover selecionados")
    remover.pack(side="left", padx=6)
    zerar = ttk.Button(controles_fila, text="Limpar fila")
    zerar.pack(side="left")

    filtros = ttk.Frame(painel)
    filtros.pack(fill="x", pady=6)
    inicio = tk.StringVar(value="2026-01-01")
    fim = tk.StringVar(value="2026-11-20")
    todos_go = tk.BooleanVar()
    for rotulo, valor in (("De", inicio), ("Até", fim)):
        ttk.Label(filtros, text=rotulo).pack(side="left")
        ttk.Entry(filtros, width=12, textvariable=valor).pack(side="left", padx=(5, 12))
    ttk.Checkbutton(filtros, text="Todo Goiás (padrão: Goiânia)", variable=todos_go).pack(side="left")

    limpar = tk.BooleanVar(value=True)
    ttk.Checkbutton(painel, text="Liberar espaço após a extração (ZIPs desta execução)",
                    variable=limpar).pack(anchor="w", pady=(3, 0))

    acoes = ttk.Frame(painel)
    acoes.pack(fill="x", pady=(8, 5))
    extrair = ttk.Button(acoes, text="Executar fila", style="Acao.TButton")
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
    fila = set()

    def mostrar_resumo(*_):
        selecao = lotes.selection()
        total = [tamanhos.get((competencia.get().strip(), lote)) for lote in selecao]
        texto = "Selecione os ZIPs que deseja baixar."
        if selecao:
            texto = f"{len(selecao)} selecionado(s) • Tamanho total "
            texto += tamanho_formatado(sum(total)) if all(total) else "indisponível"
        livre = tamanho_formatado(shutil.disk_usage(Path.cwd()).free)
        resumo.set(f"{texto}   ·   {len(fila)} na fila   ·   {livre} livres")

    def consultar():
        nonlocal consultando
        if ativo or consultando:
            return
        mes = competencia.get().strip()
        if not re.fullmatch(MES, mes):
            messagebox.showinfo("Competência", "Use o formato AAAA-MM.", parent=janela)
            return
        consultando = True
        atualizar_botao.state(["disabled"])
        for lote in lotes.get_children():
            lotes.set(lote, "tamanho", "Consultando…")
        if not ativo:
            barra.start(15)
        Thread(target=consultar_tamanhos, args=(mes, eventos, not meses["values"]), daemon=True).start()

    def atualizar_tabela(*_):
        mes = competencia.get().strip()
        processados = lotes_processados(mes)
        for lote in lotes.get_children():
            valor = tamanhos.get((mes, lote))
            lotes.set(lote, "tamanho", tamanho_formatado(valor) if valor else "—")
            nome = f"Estabelecimentos{lote}.zip"
            lotes.set(lote, "historico", "Já processado" if nome in processados else "—")
            lotes.set(lote, "fila", "✓" if (mes, lote) in fila else "—")
        mostrar_resumo()

    def atualizar():
        nonlocal ativo, consultando
        for _ in range(200):
            try:
                tipo, valor = eventos.get_nowait()
            except Empty:
                break
            if tipo == "meses":
                meses.configure(values=valor)
            elif tipo == "catalogo":
                mes, valores = valor
                tamanhos.update({(mes, str(i)): tamanho for i, tamanho in enumerate(valores)})
            elif tipo == "consulta_concluida":
                consultando = False
                atualizar_botao.state(["!disabled"])
                atualizar_tabela()
                if not ativo:
                    barra.stop()
            elif tipo == "fim":
                ativo = False
                extrair.state(["!disabled"])
                local.state(["!disabled"])
                atualizar_botao.state(["!disabled"])
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

    def editar_fila(acao):
        mes = competencia.get().strip()
        if acao == "limpar":
            fila.clear()
        elif re.fullmatch(MES, mes):
            escolhidos = {(mes, lote) for lote in lotes.selection()}
            if acao == "adicionar":
                fila.update(escolhidos)
            else:
                fila.difference_update(escolhidos)
        atualizar_tabela()

    def iniciar(arquivos_locais=False):
        nonlocal ativo
        if consultando:
            messagebox.showinfo("Receita Federal", "Aguarde a consulta em andamento.", parent=janela)
            return
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
        elif not fila:
            messagebox.showinfo("Fila", "Adicione os lotes que deseja processar.", parent=janela)
            return
        saida = filedialog.asksaveasfilename(
            title="Salvar CSV", initialfile="pequeno_varejo.csv", defaultextension=".csv",
            filetypes=[("CSV", "*.csv")], parent=janela,
        )
        if not saida:
            return
        extrator = Path(__file__).with_name("extrator_cnpj.py").resolve()
        comando = [sys.executable, str(extrator),
                   "--inicio", inicio.get().strip(), "--fim", fim.get().strip()]
        if todos_go.get():
            comando.append("--todos-go")
        if arquivos_locais:
            comando.extend(["--saida", saida])
            for arquivo in arquivos:
                comando.extend(["--arquivo", arquivo])
            alvo, argumentos = executar, (comando, eventos)
        else:
            if limpar.get():
                comando.append("--limpar-downloads")
            ordenados = sorted(fila, key=lambda item: (item[0], -int(item[1])), reverse=True)
            alvo, argumentos = executar_fila, (comando, eventos, ordenados, Path(saida))
        log.configure(state="normal")
        log.delete("1.0", "end")
        log.configure(state="disabled")
        extrair.state(["disabled"])
        local.state(["disabled"])
        atualizar_botao.state(["disabled"])
        status.set("Processando arquivos…")
        ativo = True
        barra.start(15)
        Thread(target=alvo, args=argumentos, daemon=True).start()

    def fechar():
        if ativo:
            messagebox.showinfo("Extração em andamento", "Aguarde a conclusão para fechar esta janela.", parent=janela)
        else:
            janela.destroy()

    atualizar_botao.configure(command=consultar)
    meses.bind("<<ComboboxSelected>>", lambda _: consultar())
    adicionar.configure(command=lambda: editar_fila("adicionar"))
    remover.configure(command=lambda: editar_fila("remover"))
    zerar.configure(command=lambda: editar_fila("limpar"))
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
