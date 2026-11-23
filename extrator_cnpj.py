"""Extrai dados abertos do CNPJ da Receita Federal. Requer Python 3.10+."""
import argparse
from contextlib import ExitStack
import csv
from datetime import date, datetime, timezone
import io
import json
import os
import re
import subprocess
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unicodedata
from uuid import uuid4
from zipfile import BadZipFile, ZipFile

ARQUIVOS = (
    ["Municipios.zip"]
    + [f"Estabelecimentos{i}.zip" for i in range(10)]
    + [f"Empresas{i}.zip" for i in range(10)]
)


# Para abrir no navegador: https://arquivos.receitafederal.gov.br/index.php/s/YggdBLfdninEJX9
BASE = "https://arquivos.receitafederal.gov.br/public.php/dav/files/YggdBLfdninEJX9"
TIPOS = {
    "EMPÓRIO": r"EMPORIO",
    "MERCADINHO": r"MERCADINHO",
    "MERCEARIA": r"MERCEARIA",
    "QUITANDA": r"QUITANDA",
    "MINIMERCADO": r"MINI ?MERCADO",
    "PROD. NATURAIS": r"PRODUTOS NATURAIS?",
    "CASA DE PRODUTOS": r"CASA DE PRODUTOS",
}
TIPOS = {tipo: re.compile(r"\b(?:" + regra + r")\b") for tipo, regra in TIPOS.items()}
EXCLUIR = re.compile(
    r"\b(?:ATACAREJO|ATACADAO|ATACADISTA|ATACADO|SUPER ?MERCADO|HIPER(?:MERCADO)?|"
    r"MERCADAO|SACOLAO|ASSAI|CARREFOUR|WALMART|BIG|MAKRO|RODOVALE|TUEC|SAMS CLUB|"
    r"DISTRIBUIDOR(?:A)?|DISTRIBUICAO|REPRESENTACOES|REPRESENTACAO|DEPOSITO|SUPRIMENTOS|"
    r"ARMAZEM|ARMAZENS|LANCHONETE|RESTAURANTE|PADARIA|PANIFICADORA|ACOUGUE|PIZZARIA|"
    r"HAMBURGUER(?:IA)?|FARMACIA|DROGARIA|POSTO|COMBUSTIVEL|CONVENIENCIA|SORVETERIA|"
    r"SORVETES|ACAI|CAFE|BAR|DOCERIA|CONFEITARIA|BOLOS|HORTIFRUTI(?:GRANJEIRO)?)\b"
)
CNAES_VAREJO = {"4712100", "4729699", "4723700"}
CNAES_EXCLUIDOS = {"4711301", "4711302", "4713001", "4771704"}
EMAIL = re.compile(r"[^\s@,;]+@[^\s@,;.]+(?:\.[^\s@,;.]+)+")


def baixar(pasta, competencia, arquivos=ARQUIVOS):
    pasta.mkdir(parents=True, exist_ok=True)
    for nome in arquivos:
        destino = pasta / nome
        if destino.is_file() and zip_valido(destino):
            continue
        parcial = destino.with_suffix(".zip.part")
        if destino.is_file() and not parcial.exists():
            destino.replace(parcial)
        print(f"Baixando {nome}", flush=True)
        subprocess.run([
            "curl", "--fail", "--location", "--retry", "3",
            "--retry-delay", "60", "--retry-max-time", "900",
            "--connect-timeout", "20", "--continue-at", "-",
            "--output", str(parcial), f"{BASE}/{competencia}/{nome}",
        ], check=True)
        if not zip_valido(parcial):
            raise ValueError(f"Download incompleto: {parcial}")
        parcial.replace(destino)


def zip_valido(caminho):
    try:
        with ZipFile(caminho) as arquivo:
            return bool(arquivo.infolist())
    except BadZipFile:
        return False


def linhas(caminho):
    if caminho.suffix.lower() != ".zip":
        with caminho.open(encoding="latin-1", newline="") as dados:
            yield from csv.reader(dados, delimiter=";")
        return
    with ZipFile(caminho) as arquivo:
        membros = [item for item in arquivo.infolist() if not item.is_dir()]
        if len(membros) != 1:
            raise ValueError(f"{caminho.name}: esperado um arquivo de dados no ZIP")
        with arquivo.open(membros[0]) as dados:
            with io.TextIOWrapper(dados, encoding="latin-1", newline="") as leitor:
                yield from csv.reader(leitor, delimiter=";")


def normalizar(texto):
    return unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode().upper()


def telefone(ddd, numero):
    ddd = re.sub(r"[^0-9]", "", ddd).lstrip("0")
    numero = re.sub(r"[^0-9]", "", numero)
    if len(ddd) == 2 and len(numero) in (8, 9) and int(numero):
        return f"({ddd}) {numero}", f"55{ddd}{numero}"
    return "", ""


def selecionar(args, db):
    if args.todos_go:
        cidades = None
    elif normalizar(args.municipio) == "GOIANIA" and args.uf == "GO":
        cidades = {"9373"}
    else:
        cidades = {codigo for codigo, cidade in linhas(args.municipios)
                   if normalizar(cidade) == normalizar(args.municipio)}
        if not cidades:
            raise ValueError(f"Município ausente da tabela da Receita: {args.municipio}")
    inicio, fim = (d.isoformat().replace("-", "") for d in (args.inicio, args.fim))

    db.execute("""CREATE TABLE contatos (
        cnpj TEXT PRIMARY KEY, raiz TEXT, goiania INTEGER, tipo TEXT, fantasia TEXT,
        abertura TEXT, cnae TEXT, endereco TEXT, numero TEXT, complemento TEXT,
        bairro TEXT, cep TEXT, municipio TEXT, uf TEXT, telefone TEXT, whatsapp TEXT,
        telefone2 TEXT, email TEXT
    )""")
    db.execute("CREATE TABLE empresas (raiz TEXT PRIMARY KEY, razao TEXT)")
    for nome in args.estabelecimentos:
        print(f"Lendo {nome}", flush=True)
        encontrados = []
        lidos = locais = no_periodo = ativos = no_nicho = 0
        for r in linhas(nome):
            lidos += 1
            if len(r) != 30:
                raise ValueError(f"{nome}: registro com {len(r)} colunas, esperado 30")
            if r[19] != args.uf:
                continue
            if cidades is not None and r[20] not in cidades:
                continue
            locais += 1
            if not inicio <= r[10] <= fim:
                continue
            no_periodo += 1
            if r[5] != "02":
                continue
            ativos += 1
            if args.somente_matrizes and r[3] != "1":
                continue
            cnae = r[11]
            nome_busca = normalizar(r[4])
            if EXCLUIR.search(nome_busca) or cnae in CNAES_EXCLUIDOS:
                continue
            tipo = next((tipo for tipo, regra in TIPOS.items() if regra.search(nome_busca)), "")
            if args.cnae and cnae not in args.cnae:
                continue
            if not tipo and cnae not in CNAES_VAREJO:
                continue
            no_nicho += 1
            tel1, wpp1 = telefone(r[21], r[22])
            tel2, wpp2 = telefone(r[23], r[24])
            email = r[27].strip()
            email = email if EMAIL.fullmatch(email) else ""
            if not (tel1 or tel2 or email):
                continue
            encontrados.append((
                "".join(r[:3]), r[0], int(r[19] == "GO" and r[20] == "9373"),
                tipo or "OUTROS", r[4].strip(), f"{r[10][6:8]}/{r[10][4:6]}/{r[10][:4]}",
                cnae, " ".join(filter(None, r[13:15])), r[15], r[16], r[17], r[18],
                r[20], r[19], tel1 or tel2, wpp1 or wpp2, tel2 if tel1 else "", email
            ))
        db.executemany("INSERT OR IGNORE INTO contatos VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       encontrados)
        print(f"{nome.name} — lidos: {lidos:,} | na região: {locais:,} | "
              f"no período: {no_periodo:,} | ativos: {ativos:,} | "
              f"no nicho: {no_nicho:,} | com contato: {len(encontrados):,}", flush=True)

    raizes = {raiz for (raiz,) in db.execute("SELECT DISTINCT raiz FROM contatos")}
    for nome in args.empresas:
        if not raizes:
            return
        print(f"Lendo {nome}", flush=True)
        encontrados = []
        for r in linhas(nome):
            if r[0] in raizes:
                encontrados.append((r[0], r[1]))
                raizes.remove(r[0])
        db.executemany("INSERT INTO empresas VALUES (?,?)", encontrados)
    if raizes:
        print(f"{len(raizes)} CNPJs sem razão social nos arquivos fornecidos.", flush=True)


def exportar(args, db, temporario):
    consulta = """SELECT c.cnpj, CASE c.goiania WHEN 1 THEN 'True' ELSE 'False' END,
                         c.tipo, c.fantasia, c.abertura, c.cnae, c.endereco, c.numero,
                         c.complemento, c.bairro, c.cep, c.municipio, c.uf, c.telefone,
                         c.whatsapp, c.email, COALESCE(e.razao, ''), c.telefone2
                  FROM contatos c LEFT JOIN empresas e ON e.raiz = c.raiz
                  ORDER BY c.goiania DESC, c.tipo, c.fantasia, c.cnpj"""
    colunas = ("CNPJ", "EM_GOIANIA", "TIPO", "NOME_FANTASIA", "DATA_ABERTURA",
               "CNAE_PRINCIPAL", "ENDERECO", "NUMERO", "COMPLEMENTO", "BAIRRO", "CEP",
               "MUNICIPIO_COD", "UF", "TELEFONE", "WHATSAPP", "EMAIL", "RAZAO_SOCIAL",
               "TELEFONE_2")
    total = 0
    with temporario.open("w", encoding="utf-8-sig", newline="") as saida:
        gravador = csv.writer(saida, delimiter=";")
        gravador.writerow(colunas)
        for linha in db.execute(consulta):
            gravador.writerow(linha)
            total += 1
    if total == 0:
        print("Nenhum resultado com os filtros escolhidos. Confira as contagens por arquivo. "
              "O CSV de destino foi preservado, caso já existisse.", flush=True)
        return 0
    os.replace(temporario, args.saida)
    print(f"{total} registros gravados em {args.saida}", flush=True)
    return total


def registrar_extracao(args, lotes, total):
    pasta = args.pasta / "historico"
    pasta.mkdir(parents=True, exist_ok=True)
    agora = datetime.now(timezone.utc)
    registro = {
        "competencia": args.baixar,
        "lotes": lotes,
        "processado_em": agora.isoformat(),
        "filtros": {
            "inicio": args.inicio.isoformat(),
            "fim": args.fim.isoformat(),
            "municipio": None if args.todos_go else args.municipio,
            "uf": args.uf,
            "cnae": sorted(set(args.cnae or [])),
            "somente_matrizes": args.somente_matrizes,
        },
        "registros": total,
        "csv": str(args.saida.resolve()) if total else None,
    }
    destino = pasta / f"{agora:%Y%m%dT%H%M%S}-{uuid4().hex}.json"
    parcial = destino.with_suffix(".tmp")
    try:
        parcial.write_text(json.dumps(registro, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(parcial, destino)
    finally:
        parcial.unlink(missing_ok=True)
    print(f"Histórico salvo em {destino}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pasta", nargs="?", type=Path, default=Path("dump"),
                        help="Pasta dos ZIPs oficiais ou arquivos .ESTABELE e .EMPRECSV")
    parser.add_argument("--baixar", metavar="AAAA-MM", help="Baixar a competência oficial com curl")
    parser.add_argument("--lote", type=int, choices=range(10), action="append",
                        help="Usar apenas EstabelecimentosN.zip. Pode repetir.")
    parser.add_argument("--limpar-downloads", action="store_true",
                        help="Usar espaço temporário para os novos ZIPs dos lotes selecionados.")
    parser.add_argument("--arquivo", type=Path, action="append",
                        help="Ler um ZIP, .ESTABELE ou .stable local. Pode repetir.")
    parser.add_argument("--saida", type=Path, default=Path("CSV/PEQUENO_VAREJO_GOIANIA_2026.csv"))
    parser.add_argument("--inicio", type=date.fromisoformat, default=date(2026, 1, 1))
    parser.add_argument("--fim", type=date.fromisoformat, default=date(2026, 11, 20))
    parser.add_argument("--municipio", default="Goiânia")
    parser.add_argument("--uf", default="GO", type=str.upper)
    parser.add_argument("--cnae", action="append", help="CNAE principal com 7 dígitos. Pode repetir.")
    parser.add_argument("--somente-matrizes", action="store_true")
    parser.add_argument("--todos-go", action="store_true", help="Incluir todo Goiás, com Goiânia primeiro")
    args = parser.parse_args()
    if args.limpar_downloads and not (args.baixar and args.lote):
        parser.error("--limpar-downloads exige --baixar e pelo menos um --lote")
    if args.arquivo and (args.baixar or args.lote):
        parser.error("--arquivo é exclusivo de --baixar e --lote")
    if args.inicio > args.fim:
        parser.error("--inicio precisa ser anterior ou igual a --fim")
    if args.cnae and any(len(c) != 7 or not c.isascii() or not c.isdigit() for c in args.cnae):
        parser.error("Cada CNAE deve conter 7 dígitos")
    if args.todos_go and args.uf != "GO":
        parser.error("--todos-go exige a UF GO")
    try:
        with ExitStack() as recursos:
            processar(args, parser, recursos)
    except (OSError, ValueError, csv.Error, sqlite3.Error, BadZipFile, subprocess.CalledProcessError) as erro:
        parser.exit(1, f"Erro: {erro}\n")


def processar(args, parser, recursos):
    lotes = [f"Estabelecimentos{i}.zip" for i in sorted(set(args.lote or []))]
    fontes = {}
    if args.baixar:
        if not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", args.baixar):
            parser.error("--baixar exige AAAA-MM")
        args.pasta = args.pasta / args.baixar
        arquivos = lotes or ARQUIVOS
        if lotes and not args.todos_go and (args.uf != "GO" or normalizar(args.municipio) != "GOIANIA"):
            arquivos = ["Municipios.zip", *lotes]
        if args.limpar_downloads:
            args.pasta.mkdir(parents=True, exist_ok=True)
            faltantes = []
            for nome in arquivos:
                caminho = args.pasta / nome
                if caminho.is_file() and zip_valido(caminho):
                    fontes[nome] = caminho
                else:
                    faltantes.append(nome)
            if faltantes:
                temporario = Path(recursos.enter_context(TemporaryDirectory(
                    prefix=".download-extrator-", dir=args.pasta
                )))
                baixar(temporario, args.baixar, faltantes)
                fontes.update({nome: temporario / nome for nome in faltantes})
        else:
            baixar(args.pasta, args.baixar, arquivos)
    args.municipios = fontes.get("Municipios.zip", args.pasta / "Municipios.zip")
    args.empresas = sorted(args.pasta.glob("*.EMPRECSV"))
    if args.arquivo:
        args.estabelecimentos = list(dict.fromkeys(args.arquivo))
        if any(c.suffix.lower() not in {".zip", ".estabele", ".stable"} for c in args.estabelecimentos):
            raise ValueError("Escolha arquivos ZIP, .ESTABELE ou .stable")
        pastas = {arquivo.parent for arquivo in args.estabelecimentos}
        args.empresas = sorted({*args.empresas, *(c for pasta in pastas
                              for padrao in ("*.EMPRECSV", "Empresas[0-9].zip")
                              for c in pasta.glob(padrao))})
    elif lotes:
        args.estabelecimentos = [fontes.get(nome, args.pasta / nome) for nome in lotes]
        faltantes = [caminho.name for caminho in args.estabelecimentos if not caminho.is_file()]
        if faltantes:
            raise ValueError("Arquivos selecionados ausentes: " + ", ".join(faltantes))
        if not args.empresas:
            args.empresas = sorted(args.pasta.glob("Empresas[0-9].zip"))
    else:
        args.estabelecimentos = sorted(
            c for c in args.pasta.iterdir()
            if c.is_file() and c.suffix.lower() in {".estabele", ".stable"}
        ) if args.pasta.is_dir() else []
        for atributo, prefixo in (("estabelecimentos", "Estabelecimentos"), ("empresas", "Empresas")):
            arquivos_zip = [args.pasta / f"{prefixo}{i}.zip" for i in range(10)]
            if not getattr(args, atributo) and any(caminho.exists() for caminho in arquivos_zip):
                faltantes = [caminho.name for caminho in arquivos_zip if not caminho.is_file()]
                if faltantes:
                    raise ValueError("Lote incompleto, faltam " + ", ".join(faltantes))
                setattr(args, atributo, arquivos_zip)
    if not args.estabelecimentos:
        raise ValueError(f"Nenhum arquivo .ESTABELE ou lote de ZIPs em {args.pasta}")
    args.saida.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=args.saida.parent) as temp:
        pasta_temp = Path(temp)
        db = sqlite3.connect(pasta_temp / "contatos.sqlite")
        try:
            selecionar(args, db)
            total = exportar(args, db, pasta_temp / "contatos.csv")
        finally:
            db.close()
    if args.baixar and lotes:
        registrar_extracao(args, lotes, total)


if __name__ == "__main__":
    main()
