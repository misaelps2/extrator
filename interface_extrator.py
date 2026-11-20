"""Interface local para extração de um ou dois arquivos de estabelecimentos."""
from pathlib import Path
from queue import Empty, Queue
import subprocess
import sys
from threading import Thread
import tkinter as tk
from tkinter import filedialog, messagebox, ttk


def executar(comando, eventos):
    try:
        with subprocess.Popen(
            comando, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors="replace",
        ) as processo:
            for linha in processo.stdout:
                if linha.strip():
                    eventos.put(linha.rstrip())
            eventos.put(processo.wait() == 0)
    except OSError as erro:
        eventos.put(str(erro))
        eventos.put(False)


def main():
    janela = tk.Tk()
    janela.title("Extrator CNPJ | Sify")
    janela.minsize(600, 410)
    painel = ttk.Frame(janela, padding=20)
    painel.pack(fill="both", expand=True)
    ttk.Label(painel, text="Pequeno varejo em Goiânia", font=("TkDefaultFont", 16, "bold")).pack(anchor="w")
    ttk.Label(painel, text="Escolha um ou dois arquivos de estabelecimentos.").pack(anchor="w", pady=(4, 12))

    lotes = tk.Listbox(painel, selectmode="multiple", exportselection=False, height=10)
    for i in range(10):
        lotes.insert("end", f"Estabelecimentos{i}.zip")
    lotes.pack(fill="x")

    config = ttk.Frame(painel)
    config.pack(fill="x", pady=12)
    baixar = tk.BooleanVar(value=True)
    ttk.Checkbutton(config, text="Baixar ZIPs selecionados", variable=baixar).pack(side="left")
    ttk.Label(config, text="Competência").pack(side="left", padx=(16, 4))
    competencia = ttk.Entry(config, width=10)
    competencia.insert(0, "2026-11")
    competencia.pack(side="left")

    log = tk.Text(painel, height=8, wrap="word", state="disabled")
    eventos = Queue()
    ativo = False

    def atualizar():
        nonlocal ativo
        for _ in range(200):
            try:
                evento = eventos.get_nowait()
            except Empty:
                break
            if isinstance(evento, bool):
                ativo = False
                botao.state(["!disabled"])
                status.set("CSV concluído" if evento else "Falha na extração. Confira o registro abaixo.")
            else:
                log.configure(state="normal")
                log.insert("end", evento + "\n")
                log.see("end")
                log.configure(state="disabled")
        janela.after(150, atualizar)

    def iniciar():
        nonlocal ativo
        selecao = lotes.curselection()
        if not 1 <= len(selecao) <= 2:
            messagebox.showinfo("Seleção", "Escolha um ou dois arquivos.", parent=janela)
            return
        pasta = "dump"
        if not baixar.get():
            pasta = filedialog.askdirectory(title="Escolha a pasta dos ZIPs", parent=janela)
            if not pasta:
                return
        sugestao = "goiania_lotes_" + "_".join(map(str, selecao)) + ".csv"
        saida = filedialog.asksaveasfilename(
            title="Salvar CSV", initialfile=sugestao, defaultextension=".csv",
            filetypes=[("CSV", "*.csv")], parent=janela,
        )
        if not saida:
            return
        extrator = Path(__file__).with_name("extrator_cnpj.py").resolve()
        comando = [sys.executable, str(extrator), pasta, "--saida", saida]
        if baixar.get():
            comando.extend(["--baixar", competencia.get().strip()])
        for lote in selecao:
            comando.extend(["--lote", str(lote)])
        log.configure(state="normal")
        log.delete("1.0", "end")
        log.configure(state="disabled")
        botao.state(["disabled"])
        status.set("Processando…")
        ativo = True
        Thread(target=executar, args=(comando, eventos), daemon=True).start()

    def fechar():
        if ativo:
            messagebox.showinfo("Extração em andamento", "Aguarde a conclusão para fechar esta janela.", parent=janela)
        else:
            janela.destroy()

    botao = ttk.Button(painel, text="Extrair CSV", command=iniciar)
    botao.pack(anchor="w")
    status = tk.StringVar(value="O CSV cobre os arquivos selecionados.")
    ttk.Label(painel, textvariable=status).pack(anchor="w", pady=6)
    log.pack(fill="both", expand=True)
    janela.protocol("WM_DELETE_WINDOW", fechar)
    atualizar()
    janela.mainloop()


if __name__ == "__main__":
    main()
