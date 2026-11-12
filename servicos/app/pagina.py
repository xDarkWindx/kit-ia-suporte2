"""Página do kit (http://localhost:8080): painel de controle do laboratório.

Mostra se tudo está ligado e se o robô está respondendo, guarda a chave do Dify,
abre o chat on-line de teste e tem botões para testar a ouvidoria e a API do
tribunal, sem comandos.
"""
import asyncio
import base64
import json
import os
import re
import time
from pathlib import Path

import httpx
import websockets
from fastapi import FastAPI, Form, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, Response

CONFIG = Path(os.environ.get("KIT_CONFIG", "/config"))
URL = {
    "chatwoot": os.environ.get("URL_CHATWOOT", "http://chatwoot:3000"),
    "dify": os.environ.get("URL_DIFY", "http://dify"),
    "conector": os.environ.get("URL_CONECTOR", "http://conector:8000"),
    "ouvidoria": os.environ.get("URL_OUVIDORIA", "http://ouvidoria:8100"),
    "tribunal": os.environ.get("URL_TRIBUNAL", "http://tribunal:8200"),
}
app = FastAPI(title="Página do kit")
STATIC = Path(__file__).parent / "static"
SOBRECARGA_VALE = 600  # depois de 10 minutos, um erro de sobrecarga já não diz nada
PROVEDORES = {"groq": "Groq", "gemini": "Gemini"}  # sempre aparecem no painel, mesmo fora de uso
contas = {"em": 0.0, "provedores": {}, "bloqueio": {"verificado": False, "em": None}, "token": "", "rascunho": None,
          "fila": None}  # última verificação de Dify e Chatwoot


def ler(nome: str) -> dict:
    try:
        return json.loads((CONFIG / nome).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


@app.get("/", response_class=HTMLResponse)
def inicio():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    cw = ler("chatwoot.json")
    return html.replace("{{LOGIN}}", cw.get("login", "equipe@suporte-lab.com.br")) \
               .replace("{{SENHA}}", cw.get("senha", "Suporte@2026")) \
               .replace("{{PUB_DIFY}}", os.environ.get("PUB_DIFY", "http://localhost:8081")) \
               .replace("{{PUB_CHATWOOT}}", os.environ.get("PUB_CHATWOOT", "http://localhost:3000"))


@app.get("/api/status")
async def status():
    async def ok(url: str) -> bool:
        try:
            async with httpx.AsyncClient(timeout=4) as h:
                r = await h.get(url)
                return r.status_code < 500
        except httpx.HTTPError:
            return False
    return {
        "chatwoot": await ok(URL["chatwoot"] + "/api"),
        "dify": await ok(URL["dify"] + "/console/api/setup"),
        "conector": await ok(URL["conector"] + "/saude"),
        "ouvidoria": await ok(URL["ouvidoria"] + "/manifestacoes"),
        "tribunal": await ok(URL["tribunal"] + "/registros"),
        "chatwoot_configurado": bool(ler("chatwoot.json").get("website_token")),
        "dify_configurado": bool(ler("dify.json").get("dify_key")),
        "kit_atualizado": ler("atualizacao.json").get("situacao", "ok") == "ok",
        # o painel liga o balão do chat assim que o Chatwoot fica pronto, sem precisar recarregar
        "website_token": ler("chatwoot.json").get("website_token", ""),
    }


async def entrar_no_dify(h: httpx.AsyncClient) -> dict:
    """Entra no console do Dify com o login do kit e devolve o cabeçalho que as próximas chamadas exigem."""
    senha = (CONFIG / ".senha").read_text(encoding="utf-8").strip()
    r = await h.post(URL["dify"] + "/console/api/login",
                     json={"email": ler("chatwoot.json").get("login"), "remember_me": True,
                           "password": base64.b64encode(senha.encode()).decode()})
    r.raise_for_status()
    return {"X-CSRF-Token": h.cookies.get("csrf_token") or h.cookies.get("__Host-csrf_token") or ""}


async def provedores_de_ia() -> dict:
    """Pergunta ao Dify quais contas estão cadastradas e onde o robô publicado usa cada uma (não gasta cota)."""
    provedores = {id_: {"nome": nome, "conta": None, "usos": []} for id_, nome in PROVEDORES.items()}
    try:
        async with httpx.AsyncClient(timeout=10) as h:
            cab = await entrar_no_dify(h)
            r = await h.get(URL["dify"] + "/console/api/workspaces/current/model-providers", headers=cab)
            r.raise_for_status()
            for p in provedores.values():
                p["conta"] = False
            for p in r.json()["data"]:  # langgenius/groq/groq -> groq
                id_ = p["provider"].split("/")[1]
                provedores.setdefault(id_, {"nome": id_.capitalize(), "conta": False, "usos": []})
                rotulo = p.get("label") or {}
                provedores[id_]["nome"] = rotulo.get("pt_BR") or rotulo.get("en_US") or provedores[id_]["nome"]
                provedores[id_]["conta"] = (p.get("custom_configuration") or {}).get("status") == "active"
            r = await h.get(f"{URL['dify']}/console/api/apps/{ler('dify.json').get('app_id')}/workflows/publish", headers=cab)
            r.raise_for_status()
        for n in r.json()["graph"]["nodes"]:
            if modelo := n["data"].get("model"):
                id_ = modelo["provider"].split("/")[1]
                provedores.setdefault(id_, {"nome": id_.capitalize(), "conta": False, "usos": []})
                provedores[id_]["usos"].append({"no": n["data"].get("title", ""), "modelo": modelo["name"]})
    except (OSError, ValueError, KeyError, TypeError, IndexError, httpx.HTTPError):
        pass  # sem o app_id (chave colada à mão) ou Dify ocupado: fica com o que já se sabe
    return provedores


async def token_chatwoot(h: httpx.AsyncClient, cw: dict) -> str:
    """Entra no Chatwoot só uma vez e guarda o token: o Chatwoot bloqueia logins repetidos (HTTP 429)."""
    if not contas["token"]:
        r = await h.post(URL["chatwoot"] + "/auth/sign_in", json={"email": cw.get("login"), "password": cw.get("senha")})
        r.raise_for_status()
        contas["token"] = r.json()["data"]["access_token"]
    return contas["token"]


async def ultimo_bloqueio() -> dict:
    """Quando o Chatwoot avisou pela última vez que não conseguiu chamar o robô, e se deu para verificar."""
    cw = ler("chatwoot.json")
    try:
        async with httpx.AsyncClient(timeout=10) as h:
            cab = {"api_access_token": await token_chatwoot(h, cw)}
            base = f"{URL['chatwoot']}/api/v1/accounts/{cw.get('account_id')}/conversations"
            r = await h.get(base, headers=cab, params={"status": "all", "sort_by": "last_activity_at_desc"})
            if r.status_code == 401:
                contas["token"] = ""  # token vencido: entra de novo na próxima verificação
            r.raise_for_status()
            lista = r.json()["data"]["payload"]
            if not lista:
                return {"verificado": True, "em": None}
            r = await h.get(f"{base}/{lista[0]['id']}/messages", headers=cab)
            r.raise_for_status()
        em = max((m["created_at"] for m in r.json()["payload"]
                  if m["message_type"] == 2 and re.search(r"erro com o robô|agent bot", m["content"] or "")), default=None)
        return {"verificado": True, "em": em}
    except (ValueError, KeyError, httpx.HTTPError):
        return {"verificado": False, "em": None}


def _essencia(grafo: dict) -> tuple:
    """O que muda o comportamento do fluxo: dados dos nós e ligações (ignora posição e seleção na tela)."""
    nos = sorted((n["id"], json.dumps({k: v for k, v in n["data"].items() if k not in ("selected", "desc")},
                                      sort_keys=True, ensure_ascii=False)) for n in grafo.get("nodes", []))
    arestas = sorted((e["source"], e.get("sourceHandle"), e["target"]) for e in grafo.get("edges", []))
    return nos, arestas


async def rascunho_pendente() -> dict | None:
    """Se o rascunho do Bot STJ no Dify tem mudanças que ainda não foram publicadas (o chat usa a versão publicada)."""
    app_id = ler("dify.json").get("app_id")
    if not app_id:
        return None
    try:
        async with httpx.AsyncClient(timeout=10) as h:
            cab = await entrar_no_dify(h)
            base = f"{URL['dify']}/console/api/apps/{app_id}/workflows"
            rascunho, publicado = await h.get(base + "/draft", headers=cab), await h.get(base + "/publish", headers=cab)
            rascunho.raise_for_status(); publicado.raise_for_status()
        r, p = rascunho.json(), publicado.json()
        mudou = _essencia(r.get("graph") or {}) != _essencia(p.get("graph") or {})
        return {"pendente": mudou, "editado_em": r.get("updated_at"), "publicado_em": p.get("updated_at") or p.get("created_at")}
    except (OSError, ValueError, KeyError, TypeError, httpx.HTTPError):
        return None


async def fila_humana() -> dict | None:
    """Quantas conversas estão abertas (com atendentes humanos) no Chatwoot."""
    cw = ler("chatwoot.json")
    try:
        async with httpx.AsyncClient(timeout=10) as h:
            cab = {"api_access_token": await token_chatwoot(h, cw)}
            r = await h.get(f"{URL['chatwoot']}/api/v1/accounts/{cw.get('account_id')}/conversations/meta",
                            headers=cab, params={"status": "open"})
            r.raise_for_status()
        meta = r.json().get("meta") or {}
        pub = os.environ.get("PUB_CHATWOOT", "http://localhost:3000")
        return {"abertas": meta.get("all_count", 0), "sem_responsavel": meta.get("unassigned_count", 0),
                "link": f"{pub}/app/accounts/{cw.get('account_id')}/dashboard"}
    except (ValueError, KeyError, TypeError, httpx.HTTPError):
        return None


def resumo_24h(robo: dict) -> dict | None:
    eventos = [e for e in robo.get("eventos") or [] if e[0] > time.time() - 86400]
    if not eventos:
        return None
    ok = [e for e in eventos if e[1]]
    return {"total": len(eventos), "ok": len(ok), "falhas": len(eventos) - len(ok),
            "media": round(sum(e[2] for e in ok) / len(ok), 1) if ok else None}


def situacao_do_robo(robo: dict, provedores: dict, bloqueio: dict) -> dict:
    """Resume se o robô está respondendo e o que fazer, com o estado de cada provedor. {hora} vira a hora local no navegador."""
    agora = time.time()
    em_uso = {id_: p for id_, p in provedores.items() if p["usos"]}
    erro = robo.get("erro") or {}
    if erro.get("provedor") and em_uso and erro["provedor"] not in em_uso:
        erro = {}  # erro de um provedor que o robô não usa mais
    tipo = None if erro.get("tipo") == "limite" else erro.get("tipo")
    limites = dict(robo.get("limites") or {})
    if "desconhecido" in limites and len(em_uso) == 1:  # o erro não dizia o provedor, mas só há um em uso
        limites.setdefault(next(iter(em_uso)), limites.pop("desconhecido"))
    def ativo(limite: dict) -> bool:
        return limite.get("volta_em") is None or limite["volta_em"] > agora

    itens, cartoes = [], []
    for id_, p in provedores.items():
        usado, ia = bool(p["usos"]), p["nome"]
        if not usado and not p["conta"] and id_ not in limites:
            continue  # conta não conectada e fora do robô: não interessa aqui
        recusada = usado and tipo == "conta" and erro.get("provedor") in (id_, None)
        if recusada:
            conta = ("bad", "chave recusada")
        elif p["conta"]:
            conta = ("ok", "conectada")
        elif p["conta"] is None:
            conta = ("warn", "não verificada")
        else:
            conta = ("bad" if usado else "warn", "não conectada")
        limite = limites.get(id_)
        volta = None
        if limite and ativo(limite):
            situacao, volta = ("bad" if usado else "warn", "atingido"), limite.get("volta_em")
        elif limite:
            situacao = ("warn", "a confirmar")
        elif usado and robo.get("ok_em"):
            situacao = ("ok", "sem erro")
        else:
            situacao = ("warn", "sem dados") if usado else ("", "sem dados")
        cartoes.append({"nome": ia, "usos": p["usos"],
                        "conta": {"cls": conta[0], "txt": conta[1]},
                        "limite": {"cls": situacao[0], "txt": situacao[1], "volta_em": volta}})
    bloqueado = bool(bloqueio["em"] and bloqueio["em"] > (robo.get("recebida_em") or 0))
    if bloqueado:
        itens.append({"nome": "Chat chega ao robô", "cls": "bad", "txt": "bloqueado"})
    elif robo.get("recebida_em"):
        itens.append({"nome": "Chat chega ao robô", "cls": "ok", "txt": "sim"})
    elif not bloqueio["verificado"]:
        itens.append({"nome": "Chat chega ao robô", "cls": "warn", "txt": "não verificado"})
    else:
        itens.append({"nome": "Chat chega ao robô", "cls": "warn", "txt": "sem mensagens"})

    sem_conta = [p["nome"] for p in em_uso.values() if p["conta"] is False]
    if tipo == "conta":
        sem_conta = sem_conta or [provedores[erro["provedor"]]["nome"] if erro.get("provedor") in provedores else "modelo de IA"]
    no_limite = {id_: l for id_, l in limites.items() if ativo(l) and (id_ in em_uso or id_ == "desconhecido")}
    vencidos = [id_ for id_, l in limites.items() if not ativo(l) and id_ in em_uso]
    ia = " e ".join(provedores[id_]["nome"] if id_ in provedores else "modelo de IA" for id_ in no_limite)
    voltas = [l["volta_em"] for l in no_limite.values() if l.get("volta_em")]
    volta_em = max(voltas) if len(voltas) == len(no_limite) and voltas else None
    if sem_conta:
        nomes = " e ".join(sem_conta)
        cls, titulo = "bad", f"Robô parado: a conta do {nomes} não está conectada."
        acao = (f"No Dify: seu nome › Configurações › Provedor de modelo › {sem_conta[0]}, e cole a chave. Para usar outro "
                "provedor, troque o modelo nos nós de IA do app Bot STJ e publique.")
    elif bloqueado:
        cls, titulo = "bad", "Robô parado: o Chatwoot não está conseguindo chamar o robô."
        acao = ("As conversas estão indo direto para o atendente humano. Confira se o Chatwoot foi ligado com "
                "SAFE_FETCH_ALLOW_PRIVATE_NETWORK ou chame a professora.")
    elif no_limite:
        parcial = len(em_uso) > 1 and all(id_ in em_uso for id_ in no_limite) and len(no_limite) < len(em_uso)
        nos = ", ".join(u["no"] for id_ in no_limite for u in provedores.get(id_, {"usos": []})["usos"])
        cls, titulo = "bad", (f"Robô com falha parcial: o limite do {ia} foi atingido." if parcial
                              else f"Robô parado: o limite do {ia} foi atingido.")
        volta = "Deve voltar em {hora}" if volta_em else "Deve voltar quando o provedor renovar a cota"
        afetados = f"Nó afetado: {nos}. " if parcial and nos else ""
        acao = (afetados + volta + ". Até lá, as conversas que passam por esse modelo vão para o atendente humano. "
                "Para voltar antes, troque o modelo no Dify e publique.")
    elif tipo == "modelo":
        cls, titulo = "bad", "Robô parado: um modelo escolhido não existe mais no provedor."
        acao = "No Dify, abra o app Bot STJ, escolha outro modelo nos nós de IA e clique em Publicar."
    elif tipo == "json":
        nos = [u for p in em_uso.values() for u in p["usos"] if "classific" in u["no"].lower()]
        no = f"{nos[0]['no']} ({nos[0]['modelo']})" if nos else "Classificador de escopo"
        cls, titulo = "bad", "Robô parado: o modelo do classificador não respondeu no formato que o Dify exige."
        acao = (f"O nó {no} precisa de um modelo que devolva JSON sem 'pensar em voz alta'. No Dify, abra o app "
                "Bot STJ, troque o modelo desse nó (ex.: nvidia/nemotron-3.5-bolt-e4b no NVIDIA NIM, ou openai/gpt-oss-20b no Groq) "
                "e clique em Publicar.")
    elif tipo == "chave_dify":
        cls, titulo = "bad", "Robô parado: a chave do Dify está faltando ou foi recusada."
        acao = "Gere uma nova chave e salve no item 3 abaixo."
    elif tipo == "dify":
        cls, titulo = "bad", "Robô parado: o conector não conseguiu falar com o Dify."
        acao = "Confira se o Dify está OK na lista acima."
    elif tipo == "outro":
        cls, titulo = "bad", "A última pergunta ao robô falhou."  # detalhes completos em /diagnostico
        detalhe = (erro.get("detalhe") or "").replace("{", "(").replace("}", ")")
        acao = (f"Erro: {detalhe}. " if detalhe else "") + \
            "Veja o nó que falhou em Diagnóstico (link abaixo) ou no Dify: Estúdio › Bot STJ › Logs."
    elif tipo == "sobrecarga" and agora - erro.get("em", 0) < SOBRECARGA_VALE:
        nome = provedores[erro["provedor"]]["nome"] if erro.get("provedor") in provedores else "provedor de IA"
        cls, titulo = "warn", f"Robô instável: o {nome} está sobrecarregado."
        acao = "Costuma passar em alguns minutos. Enquanto isso, algumas conversas podem ir para o atendente humano."
    elif vencidos:
        cls, titulo = "warn", "Um limite de IA já deve ter sido renovado."
        acao = "Clique em Testar agora para confirmar."
    elif robo.get("ok_em") and tipo is None:
        cls, titulo = "ok", "Robô funcionando."
        acao = "Última resposta da IA em {ok}."
    else:
        cls, titulo = "warn", "Ainda não sei se o robô está respondendo."
        acao = "Mande uma mensagem no chat ou clique em Testar agora."
    cartoes.sort(key=lambda c: not c["usos"])  # os que o robô usa primeiro
    return {"cls": cls, "titulo": titulo, "acao": acao, "volta_em": volta_em, "ok_em": robo.get("ok_em"), "itens": itens,
            "provedores": cartoes, "ultima": robo.get("ultima") if robo.get("ok_em") and tipo is None else None}


@app.get("/api/robo")
async def robo():
    # O painel chama a cada 5 s, mas Dify e Chatwoot só são consultados a cada 30 s (exige login)
    if time.time() - contas["em"] > 30 or not contas["provedores"]:
        contas.update(em=time.time(), provedores=await provedores_de_ia(), bloqueio=await ultimo_bloqueio(),
                      rascunho=await rascunho_pendente(), fila=await fila_humana())
    robo_json = ler("robo.json")
    return situacao_do_robo(robo_json, contas["provedores"], contas["bloqueio"]) | {
        "rascunho": contas["rascunho"], "fila": contas["fila"], "resumo": resumo_24h(robo_json)}


@app.post("/api/robo/testar")
async def testar_robo():
    try:
        async with httpx.AsyncClient(timeout=100) as h:
            r = await h.post(URL["conector"] + "/testar")
        return r.json()
    except (ValueError, httpx.HTTPError):
        return {"ok": False}


@app.post("/api/chave")
def salvar_chave(chave: str = Form(...)):
    chave = chave.strip()
    if not re.fullmatch(r"app-[A-Za-z0-9]{10,}", chave):
        return JSONResponse({"ok": False, "msg": "A chave do Dify começa com app- seguida de letras e números. Copie de novo em Publicar › Acesso à API."}, 400)
    (CONFIG / "dify.json").write_text(json.dumps({"dify_key": chave}), encoding="utf-8")
    return {"ok": True, "msg": "Chave salva. O bot do chat on-line já usa o Dify."}


def corpo(r: httpx.Response):
    """Resposta do serviço simulado; se não vier JSON (ex.: erro 500 em texto), mostra o texto."""
    try:
        return r.json()
    except ValueError:
        return r.text


@app.post("/api/teste/ouvidoria")
async def teste_ouvidoria(tipo: str = Form(...), texto: str = Form(...)):
    try:
        async with httpx.AsyncClient(timeout=10) as h:
            r = await h.post(URL["ouvidoria"] + "/manifestacoes", json={"tipo": tipo, "texto": texto, "canal": "chat"})
    except httpx.HTTPError:
        return {"status": None, "resposta": "A ouvidoria simulada não respondeu. Confira o item 1 do painel."}
    return {"status": r.status_code, "resposta": corpo(r)}


@app.post("/api/teste/tribunal")
async def teste_tribunal(numero: str = Form(...), token: str = Form(""), vezes: int = Form(1)):
    cab = {"Authorization": f"Bearer {token}"} if token else {}
    resultados = []
    try:
        async with httpx.AsyncClient(timeout=10) as h:
            for _ in range(max(1, min(vezes, 15))):
                r = await h.get(f"{URL['tribunal']}/processos/{numero}", headers=cab)
                resultados.append({"status": r.status_code, "resposta": corpo(r)})
    except httpx.HTTPError:
        resultados.append({"status": None, "resposta": "A API do tribunal simulada não respondeu. Confira o item 1 do painel."})
    return resultados


def fim_do_arquivo(nome: str, linhas: int = 40) -> str:
    try:
        return "".join((CONFIG / nome).read_text(encoding="utf-8", errors="replace").splitlines(True)[-linhas:])
    except OSError:
        return ""


def curto(valor, limite: int = 3000):
    """Texto de uma saída do Dify para a página: strings como estão, o resto em JSON, sempre com limite."""
    if valor in (None, "", {}, []):
        return None
    texto = valor if isinstance(valor, str) else json.dumps(valor, ensure_ascii=False, indent=1)
    return texto if len(texto) <= limite else texto[:limite] + " …(cortado)"


async def execucoes_no_dify() -> dict:
    """Últimas execuções do Bot STJ no Dify (as mesmas da tela Logs): pergunta, resposta do bot e o que cada nó fez."""
    app_id = ler("dify.json").get("app_id")
    if not app_id:
        return {"erro": "O painel não sabe o app_id do Bot STJ (chave colada à mão). Veja os Logs no próprio Dify."}
    try:
        async with httpx.AsyncClient(timeout=15) as h:
            cab = await entrar_no_dify(h)
            base = f"{URL['dify']}/console/api/apps/{app_id}"
            r = await h.get(base + "/advanced-chat/workflow-runs", headers=cab,
                            params={"limit": 10, "triggered_from": "app-run"})
            r.raise_for_status()
            execucoes = []
            for run in r.json().get("data", []):
                item = {k: run.get(k) for k in ("id", "status", "elapsed_time", "total_tokens", "created_at")}
                d = await h.get(f"{base}/workflow-runs/{run['id']}", headers=cab)
                if d.is_success:
                    det = d.json()
                    entradas, saidas = det.get("inputs") or {}, det.get("outputs") or {}
                    item["erro"] = det.get("error")
                    item["pergunta"] = curto(entradas.get("sys.query") if isinstance(entradas, dict) else None, 500)
                    item["resposta"] = curto(saidas.get("answer") if isinstance(saidas, dict) and "answer" in saidas else saidas)
                n = await h.get(f"{base}/workflow-runs/{run['id']}/node-executions", headers=cab)
                nos = n.json().get("data", []) if n.is_success else []
                item["nos"] = [{
                    "titulo": x.get("title"), "tipo": x.get("node_type"), "status": x.get("status"),
                    "erro": x.get("error"), "segundos": x.get("elapsed_time"),
                    "tokens": (x.get("execution_metadata") or {}).get("total_tokens"),
                    "modelo": ((x.get("process_data") or {}).get("model_name")
                               or ((x.get("inputs") or {}).get("model_name") if isinstance(x.get("inputs"), dict) else None)),
                    "saida": curto(x.get("outputs")),
                    "processo": curto({k: v for k, v in (x.get("process_data") or {}).items() if k != "prompts"}, 1500),
                } for x in sorted(nos, key=lambda x: x.get("index") or 0)]
                execucoes.append(item)
            return {"execucoes": execucoes}
    except (OSError, ValueError, KeyError, TypeError, httpx.HTTPError) as e:
        return {"erro": f"Não consegui ler os Logs do Dify ({type(e).__name__}). Veja em Estúdio › Bot STJ › Logs."}


# --- Reproduzir o classificador ---------------------------------------------------------------
# Quando o classificador do Dify não acha o JSON na resposta, ele descarta o texto do modelo: nem os
# Logs mostram. Para ver a resposta crua, o painel mantém um app separado no Dify ("Diagnóstico do
# classificador (kit)") com um nó LLM que recebe EXATAMENTE as mensagens que o classificador monta
# (copiadas do Dify 1.17.1, graphon/nodes/question_classifier/template_prompts.py), com o mesmo modelo.
APP_DIAG = "Diagnóstico do classificador (kit)"
QC_SISTEMA = (
    "\n### Job Description',\n"
    "You are a text classification engine that analyzes text data and assigns "
    "categories based on user input or automatically determined categories.\n"
    "### Task\n"
    "Your task is to assign one categories ONLY to the input text and only one "
    "category may be assigned returned in the output.\n"
    "Additionally, you need to extract the key words from the text that are "
    "related to the classification.\n"
    "### Format\n"
    "The input text is in the variable input_text. Categories are specified as a "
    "category list with two filed category_id and category_name in the variable "
    "categories. Classification instructions may be included to improve the "
    "classification accuracy.\n"
    "### Constraint\n"
    "DO NOT include anything other than the JSON array in your response.\n"
    "### Memory\n"
    "Here are the chat histories between human and assistant, inside "
    "<histories></histories> XML tags.\n"
    "<histories>\n"
    "\n"
    "</histories>\n"
)
QC_USUARIO_1 = (
    '\n    {"input_text": ["I recently had a great experience with your '
    'company. The service was prompt and the staff was very friendly."],\n'
    '    "categories": [{"category_id":"f5660049-284f-41a7-b301-fd24176a711c",'
    '"category_name":"Customer Service"},'
    '{"category_id":"8d007d06-f2c9-4be5-8ff6-cd4381c13c60",'
    '"category_name":"Satisfaction"},'
    '{"category_id":"5fbbbb18-9843-466d-9b8e-b9bfbb9482c8",'
    '"category_name":"Sales"},'
    '{"category_id":"23623c75-7184-4a2e-8226-466c2e4631e4",'
    '"category_name":"Product"}],\n'
    '    "classification_instructions": ["classify the text based on the '
    'feedback provided by customer"]}\n'
)
QC_ASSISTENTE_1 = (
    "\n```json\n"
    '    {"keywords": ["recently", "great experience", "company", '
    '"service", "prompt", "staff", "friendly"],\n'
    '    "category_id": "f5660049-284f-41a7-b301-fd24176a711c",\n'
    '    "category_name": "Customer Service"}\n'
    "```\n"
)
QC_USUARIO_2 = (
    '\n    {"input_text": ["bad service, slow to bring the food"],\n'
    '    "categories": [{"category_id":"80fb86a0-4454-4bf5-924c-f253fdd83c02",'
    '"category_name":"Food Quality"},'
    '{"category_id":"f6ff5bc3-aca0-4e4a-8627-e760d0aca78f",'
    '"category_name":"Experience"},'
    '{"category_id":"cc771f63-74e7-4c61-882e-3eda9d8ba5d7",'
    '"category_name":"Price"}],\n'
    '    "classification_instructions": []}\n'
)
QC_ASSISTENTE_2 = (
    "\n```json\n"
    '    {"keywords": ["bad service", "slow", "food", "tip", '
    '"terrible", "waitresses"],\n'
    '    "category_id": "f6ff5bc3-aca0-4e4a-8627-e760d0aca78f",\n'
    '    "category_name": "Experience"}\n'
    "```\n"
)


def acha_json_como_o_dify(texto: str) -> str:
    """Mesma regra do Dify (graphon/utils/json_in_md_parser.py) para dizer se o classificador aceitaria a resposta."""
    t = re.sub(r"<think[^>]*>[\s\S]*?</think>", "", texto, flags=re.I).strip()
    inicio = -1
    for marca in ["```json", "```", "``", "`", "{", "["]:
        inicio = t.find(marca)
        if inicio != -1:
            if t[inicio] not in "{[":
                inicio += len(marca)
            break
    fim = -1
    if inicio != -1:
        for marca in ["```", "``", "`", "}", "]"]:
            fim = t.rfind(marca, inicio)
            if fim != -1:
                if t[fim] in "}]":
                    fim += 1
                break
    if inicio == -1 or fim == -1 or inicio >= fim:
        return "could not find json block in the output."
    try:
        obj = json.loads(t[inicio:fim].strip())
    except ValueError as e:
        return f"got invalid json object. error: {e}"
    if isinstance(obj, list) and len(obj) == 1 and isinstance(obj[0], dict):
        obj = obj[0]
    if not isinstance(obj, dict) or "category_id" not in obj:
        return "JSON encontrado, mas sem category_id: o Dify cai na primeira categoria."
    return ""


def app_de_diagnostico(no: dict) -> dict:
    """DSL do app auxiliar: Início -> LLM com as mensagens do classificador -> Resposta com o texto cru."""
    d = no["data"]
    categorias = json.dumps([{"category_id": c["id"], "category_name": c["name"]} for c in d.get("classes", [])],
                            ensure_ascii=False)
    instrucao = (d.get("instruction") or "").replace("{{#", "{ {#")  # sem variáveis do outro fluxo
    usuario_3 = ('\n    {"input_text": ["{{#sys.query#}}"],\n'
                 f'    "categories": {categorias},\n'
                 f'    "classification_instructions": ["{instrucao}"]}}\n')

    def nodo(id_, x, data):
        return {"id": id_, "type": "custom", "data": {**data, "desc": "", "selected": False},
                "position": {"x": x, "y": 200}, "positionAbsolute": {"x": x, "y": 200},
                "sourcePosition": "right", "targetPosition": "left", "width": 244, "height": 90, "selected": False}

    def aresta(a, b, ta, tb):
        return {"id": f"{a}-source-{b}", "source": a, "sourceHandle": "source", "target": b, "targetHandle": "target",
                "type": "custom", "zIndex": 0,
                "data": {"isInIteration": False, "isInLoop": False, "sourceType": ta, "targetType": tb}}

    grafo = {
        "nodes": [
            nodo("inicio", 30, {"type": "start", "title": "Início", "variables": []}),
            nodo("modelo", 330, {
                "type": "llm", "title": "Mesmo modelo e mensagens do classificador",
                "model": d["model"], "reasoning_format": "tagged",
                "context": {"enabled": False, "variable_selector": []}, "variables": [], "vision": {"enabled": False},
                "prompt_template": [
                    {"role": "system", "text": QC_SISTEMA}, {"role": "user", "text": QC_USUARIO_1},
                    {"role": "assistant", "text": QC_ASSISTENTE_1}, {"role": "user", "text": QC_USUARIO_2},
                    {"role": "assistant", "text": QC_ASSISTENTE_2}, {"role": "user", "text": usuario_3}]}),
            nodo("resposta", 630, {"type": "answer", "title": "Resposta crua", "answer": "{{#modelo.text#}}",
                                   "variables": []}),
        ],
        "edges": [aresta("inicio", "modelo", "start", "llm"), aresta("modelo", "resposta", "llm", "answer")],
        "viewport": {"x": 0, "y": 0, "zoom": 0.8},
    }
    recursos = {"file_upload": {}, "opening_statement": "", "retriever_resource": {"enabled": False},
                "sensitive_word_avoidance": {"enabled": False}, "speech_to_text": {"enabled": False},
                "suggested_questions": [], "suggested_questions_after_answer": {"enabled": False},
                "text_to_speech": {"enabled": False, "language": "", "voice": ""}}
    return {"app": {"name": APP_DIAG, "mode": "advanced-chat", "icon": "🔎", "icon_background": "#F3EFE7",
                    "description": "Criado pelo painel do kit: mostra a resposta crua do modelo do classificador.",
                    "use_icon_as_answer_icon": False},
            "kind": "app", "version": "0.7.0", "dependencies": [],
            "workflow": {"conversation_variables": [], "environment_variables": [], "features": recursos,
                         "graph": grafo}}


async def preparar_app_de_diagnostico(h: httpx.AsyncClient, cab: dict, dsl: dict) -> str:
    """Cria o app auxiliar na primeira vez; depois só troca o fluxo (o modelo do classificador pode ter mudado)."""
    diag = ler("diagnostico.json")
    app_id, chave = diag.get("app_id"), diag.get("chave")
    base = f"{URL['dify']}/console/api/apps"
    assinatura = json.dumps(dsl["workflow"]["graph"], sort_keys=True, ensure_ascii=False)
    if app_id and chave and diag.get("assinatura") == assinatura:
        return chave  # mesmo modelo e mesmas mensagens do teste anterior: já está publicado
    if app_id and chave and (r := await h.get(f"{base}/{app_id}/workflows/draft", headers=cab)).is_success:
        rascunho = r.json()
        (await h.post(f"{base}/{app_id}/workflows/draft", headers=cab,
                      json={"graph": dsl["workflow"]["graph"], "features": rascunho["features"],
                            "hash": rascunho["hash"]})).raise_for_status()
    else:  # app apagado ou nunca criado
        r = await h.post(f"{base}/imports", headers=cab,
                         json={"mode": "yaml-content", "yaml_content": json.dumps(dsl, ensure_ascii=False)})
        imp = r.json()
        if r.status_code == 202:
            r = await h.post(f"{base}/imports/{imp['id']}/confirm", headers=cab)
            imp = r.json()
        if r.status_code >= 400 or not imp.get("app_id"):
            raise ValueError(f"importação do app de diagnóstico falhou: HTTP {r.status_code} {r.text[:300]}")
        app_id = imp["app_id"]
        r = await h.post(f"{base}/{app_id}/api-keys", headers=cab)
        r.raise_for_status()
        chave = r.json()["token"]
    (await h.post(f"{base}/{app_id}/workflows/publish", headers=cab,
                  json={"marked_name": "Painel do kit", "marked_comment": ""})).raise_for_status()
    (CONFIG / "diagnostico.json").write_text(json.dumps({"app_id": app_id, "chave": chave, "assinatura": assinatura}),
                                             encoding="utf-8")
    return chave


@app.get("/api/diagnostico/modelos")
async def modelos_disponiveis():
    """Modelos de texto (LLM) das contas conectadas no Dify, para o comparador da página de diagnóstico."""
    try:
        async with httpx.AsyncClient(timeout=20) as h:
            cab = await entrar_no_dify(h)
            r = await h.get(f"{URL['dify']}/console/api/workspaces/current/models/model-types/llm", headers=cab)
            r.raise_for_status()
            atual = {}
            if app_id := ler("dify.json").get("app_id"):
                p = await h.get(f"{URL['dify']}/console/api/apps/{app_id}/workflows/publish", headers=cab)
                if p.is_success:
                    no = next((n for n in p.json()["graph"]["nodes"] if n["data"].get("type") == "question-classifier"), None)
                    atual = no["data"]["model"] if no else {}
    except (OSError, ValueError, KeyError, TypeError, httpx.HTTPError) as e:
        return JSONResponse({"erro": f"Não consegui listar os modelos no Dify: {type(e).__name__}"}, 502)
    modelos = []
    for prov in r.json().get("data", []):
        if prov.get("status") != "active":
            continue
        nome = (prov.get("label") or {}).get("pt_BR") or (prov.get("label") or {}).get("en_US") or prov["provider"]
        for m in prov.get("models", []):
            if m.get("status") == "active" and (m.get("model_properties") or {}).get("mode", "chat") == "chat":
                modelos.append({"provedor": prov["provider"], "provedor_nome": nome, "modelo": m["model"],
                                "atual": prov["provider"] == atual.get("provider") and m["model"] == atual.get("name")})
    return {"modelos": modelos}


@app.post("/api/diagnostico/classificador")
async def reproduzir_classificador(pergunta: str = Form(...), sem_raciocinio: str = Form(""), outro_modelo: str = Form(""),
                                   outro_provedor: str = Form("")):
    """Testa o classificador; opcionalmente sem o reasoning_effort e/ou com outro modelo (de qualquer conta do Dify)."""
    pergunta = pergunta.strip()[:500] or "oi"
    app_id = ler("dify.json").get("app_id")
    if not app_id:
        return JSONResponse({"erro": "O painel não sabe o app_id do Bot STJ (chave colada à mão)."}, 400)
    try:
        async with httpx.AsyncClient(timeout=120) as h:
            cab = await entrar_no_dify(h)
            r = await h.get(f"{URL['dify']}/console/api/apps/{app_id}/workflows/publish", headers=cab)
            r.raise_for_status()
            no = next((n for n in r.json()["graph"]["nodes"] if n["data"].get("type") == "question-classifier"), None)
            if not no:
                return JSONResponse({"erro": "O Bot STJ publicado não tem nó classificador."}, 400)
            modelo = dict(no["data"]["model"])  # cópia: o Bot STJ não é alterado
            parametros = dict(modelo.get("completion_params") or {})
            if sem_raciocinio:
                parametros.pop("reasoning_effort", None)
            if outro_modelo.strip():
                modelo["name"] = outro_modelo.strip()
            if outro_provedor.strip():
                # Comparador: todos os modelos com os mesmos parâmetros (os do nó podem não valer em outro
                # provedor). Fica a temperatura do nó e, só no gpt-oss, o reasoning_effort=low que o kit usa nele.
                modelo["provider"], modelo["mode"] = outro_provedor.strip(), "chat"
                parametros = {"temperature": parametros.get("temperature", 0.2)}
                if "gpt-oss" in modelo["name"] and not sem_raciocinio:
                    parametros["reasoning_effort"] = "low"
            modelo["completion_params"] = parametros
            no = {**no, "data": {**no["data"], "model": modelo}}
            chave = await preparar_app_de_diagnostico(h, cab, app_de_diagnostico(no))
            inicio = time.time()
            r = await h.post(f"{URL['dify']}/v1/chat-messages", headers={"Authorization": f"Bearer {chave}"},
                             json={"inputs": {}, "query": pergunta, "response_mode": "blocking", "user": "painel-diagnostico"})
            base = {"no": no["data"].get("title"), "modelo": modelo.get("name"), "provedor": modelo.get("provider"),
                    "parametros": modelo.get("completion_params") or {}, "pergunta": pergunta,
                    "segundos": round(time.time() - inicio, 1)}
            if not r.is_success:
                try:
                    msg = r.json().get("message") or r.text
                except ValueError:
                    msg = r.text
                if re.search(r"model_not_found|does not exist|decommissioned", msg, re.I):
                    return {**base, "erro": "Este modelo não existe mais no provedor (foi retirado ou a conta não tem "
                                            "acesso). O Dify ainda o lista porque a lista vem do plugin.",
                            "detalhe": msg[:1000]}
                return {**base, "erro": f"O modelo recusou a chamada (HTTP {r.status_code}): {msg[:1000]}"}
            dados = r.json()
            texto = dados.get("answer", "")
            sem_pensamento = re.sub(r"<think[^>]*>[\s\S]*?</think>", "", texto, flags=re.I)
            categoria = re.search(r'"category_id"\s*:\s*"([^"]+)"', sem_pensamento)
            return {**base, "texto": texto, "uso": (dados.get("metadata") or {}).get("usage"),
                    "veredito": acha_json_como_o_dify(texto), "categoria": categoria.group(1) if categoria else None}
    except (OSError, ValueError, KeyError, TypeError, StopIteration, httpx.HTTPError) as e:
        return JSONResponse({"erro": f"Não consegui reproduzir no Dify: {type(e).__name__}: {e}"[:600]}, 502)


@app.get("/api/diagnostico")
async def diagnostico():
    robo = ler("robo.json")
    return {
        "agora": time.time(),
        "robo": {k: robo.get(k) for k in ("ok_em", "recebida_em", "erro", "limites")},
        "historico": robo.get("historico") or [],
        "dify": await execucoes_no_dify(),
        "provedores": contas["provedores"] or await provedores_de_ia(),
        "atualizacao": ler("atualizacao.json"),
        "inicio_log": fim_do_arquivo("inicio.log"),
        "conhecimento_log": fim_do_arquivo("conhecimento.log", 20),
    }


@app.get("/diagnostico", response_class=HTMLResponse)
def pagina_de_diagnostico():
    return (STATIC / "diagnostico.html").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------------------------
# Chat on-line servido pelo próprio painel. No Codespaces, quem abre a porta 3000 pelo navegador
# vê antes o aviso "You are about to access a development port"; o cookie que libera esse aviso
# vale só na aba onde ele foi aceito (Partitioned), nunca dentro do painel. Por isso o balão,
# que é um iframe da porta 3000, ficava preso no aviso e não aparecia. Passando pelo painel
# (mesmo endereço da página), não há aviso. Estas rotas ficam por último: as do painel têm prioridade.
SEM_REPASSE = {"host", "content-length", "transfer-encoding", "connection", "keep-alive", "upgrade",
               "accept-encoding", "content-encoding"}
cliente = {"http": None}


@app.websocket("/cable")
async def chatwoot_tempo_real(ws: WebSocket):
    """Websocket do Chatwoot: é por ele que a resposta do bot chega ao balão sem recarregar."""
    protocolos = [p.strip() for p in ws.headers.get("sec-websocket-protocol", "").split(",") if p.strip()]
    destino = re.sub(r"^http", "ws", URL["chatwoot"]) + "/cable"
    try:
        # Origin igual ao endereço interno: o Chatwoot só aceita websocket do próprio endereço
        async with websockets.connect(destino, subprotocols=protocolos or None, origin=URL["chatwoot"]) as cw:
            await ws.accept(subprotocol=cw.subprotocol)

            async def ida():
                while True:
                    await cw.send(await ws.receive_text())

            async def volta():
                async for m in cw:
                    await ws.send_text(m if isinstance(m, str) else m.decode())

            tarefas = [asyncio.create_task(ida()), asyncio.create_task(volta())]
            await asyncio.wait(tarefas, return_when=asyncio.FIRST_COMPLETED)
            for t in tarefas:
                t.cancel()
    except (OSError, websockets.WebSocketException, WebSocketDisconnect):
        pass
    finally:
        try:
            await ws.close()
        except RuntimeError:
            pass  # já fechado


@app.api_route("/{caminho:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"], include_in_schema=False)
async def chatwoot_pelo_painel(caminho: str, request: Request):
    """Repassa ao Chatwoot o que o balão pede: /packs/js/sdk.js, /widget, /api/v1/widget/..., /vite/..."""
    if cliente["http"] is None:
        cliente["http"] = httpx.AsyncClient(timeout=30)
    cab = {k: v for k, v in request.headers.items() if k.lower() not in SEM_REPASSE}
    try:
        r = await cliente["http"].request(request.method, f"{URL['chatwoot']}/{caminho}", params=request.query_params,
                                          headers=cab, content=await request.body())
    except httpx.HTTPError:
        return Response("O Chatwoot não respondeu. Confira o item 1 do painel.", status_code=502)
    resp = Response(content=r.content, status_code=r.status_code)
    for k, v in r.headers.multi_items():
        if k.lower() not in SEM_REPASSE:
            resp.headers.append(k, v)
    return resp
