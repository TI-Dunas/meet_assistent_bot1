"""
Bot que entra em uma reunião do Google Meet, liga as legendas automáticas
e registra tudo o que é dito (com o nome de quem falou) em um arquivo de texto.

Observação: a interface do Meet muda com frequência. Se algo parar de funcionar,
os pontos a ajustar são os padrões de texto dos botões (PADROES) e o script
JS_LEGENDAS, que lê a área de legendas da página.
"""

import os
import re
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

from playwright.sync_api import sync_playwright

# Textos dos botões em português e inglês (expressões regulares, sem diferenciar maiúsculas)
PADROES = {
    "sem_midia": r"continuar sem|continue without|sem microfone|without microphone",
    "mic_off": r"desativar (o )?microfone|turn off microphone",
    "cam_off": r"desativar (a )?c[aâ]mera|turn off camera",
    "entrar": r"pedir para participar|solicitar participa|participar agora|ask to join|join now",
    "sair": r"sair da chamada|leave call",
    "legendas": r"ativar (as )?legendas|mostrar legendas|turn on captions",
}

# Mensagens do Meet que indicam que o bot não vai conseguir entrar
BLOQUEIOS = (
    r"n[aã]o (é possível|pode|foi possível) participar|you can.?t join|"
    r"solicita[cç][aã]o .{0,40}(negada|recusada)|denied your request|negou sua solicita|"
    r"ningu[eé]m respondeu|no one responded|removed you|removeu voc[eê]|"
    r"reuni[aã]o n[aã]o existe|check your meeting code|verifique o c[oó]digo"
)

ARGS_NAVEGADOR = [
    "--disable-blink-features=AutomationControlled",
    "--lang=pt-BR",
    # Evita que o Chrome "congele" a página quando a janela fica atrás de outras
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
]
# Rodando como root (comum em servidores), o Chromium exige --no-sandbox
if hasattr(os, "geteuid") and os.geteuid() == 0:
    ARGS_NAVEGADOR.append("--no-sandbox")

# Lê a área de legendas e devolve uma lista de blocos {id, falante, texto}.
# Cada bloco recebe um id fixo enquanto o elemento existir na página, para que
# o texto possa ser atualizado (o Meet corrige as palavras enquanto a pessoa fala).
JS_LEGENDAS = r"""
() => {
  const sel = '[role="region"][aria-label*="egenda" i], [role="region"][aria-label*="aption" i], [role="region"][aria-label*="ubt" i]';
  const region = document.querySelector(sel);
  if (!region) return null;
  if (!window.__ids) { window.__ids = new WeakMap(); window.__n = 0; }

  // Cada fala costuma ter um avatar (img); o bloco é o ancestral que contém nome + texto.
  const blocos = new Set();
  region.querySelectorAll('img').forEach(img => {
    let el = img.parentElement;
    while (el && el !== region && !(el.innerText || '').trim().includes('\n')) el = el.parentElement;
    if (el && el !== region) blocos.add(el);
  });
  if (blocos.size === 0) blocos.add(region);

  const out = [];
  for (const blk of blocos) {
    const linhas = (blk.innerText || '').split('\n').map(s => s.trim()).filter(Boolean);
    if (linhas.length === 0) continue;
    if (!window.__ids.has(blk)) window.__ids.set(blk, ++window.__n);
    const temNome = linhas.length > 1 && blk !== region;
    out.push({
      id: window.__ids.get(blk),
      falante: temNome ? linhas[0] : '',
      texto: (temNome ? linhas.slice(1) : linhas).join(' '),
    });
  }
  return out;
}
"""


@contextmanager
def tela_virtual():
    """Em servidores sem interface gráfica, cria uma tela virtual (Xvfb) para o navegador.

    O Meet costuma bloquear navegadores no modo headless, então o bot roda um
    navegador "normal" desenhando numa tela que só existe na memória.
    """
    if not sys.platform.startswith("linux") or os.environ.get("DISPLAY"):
        yield
        return
    if not shutil.which("Xvfb"):
        raise RuntimeError("Servidor sem tela e sem Xvfb. Instale com: apt install -y xvfb")
    numero = 99
    while Path(f"/tmp/.X11-unix/X{numero}").exists():
        numero += 1
    proc = subprocess.Popen(
        ["Xvfb", f":{numero}", "-screen", "0", "1280x800x24", "-nolisten", "tcp"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(1.5)
    os.environ["DISPLAY"] = f":{numero}"
    print(f"Tela virtual iniciada (DISPLAY=:{numero}).")
    try:
        yield
    finally:
        proc.terminate()
        os.environ.pop("DISPLAY", None)


def _abrir_contexto(p, perfil):
    """Abre o navegador com um perfil persistente (guarda o login do Google)."""
    opcoes = dict(
        user_data_dir=str(perfil),
        headless=False,
        args=ARGS_NAVEGADOR,
        ignore_default_args=["--enable-automation"],
        viewport={"width": 1280, "height": 800},
        locale="pt-BR",
    )
    try:
        # Prefere o Google Chrome instalado (o Google bloqueia menos que o Chromium)
        return p.chromium.launch_persistent_context(channel="chrome", **opcoes)
    except Exception:
        return p.chromium.launch_persistent_context(**opcoes)


def fazer_login(perfil):
    """Abre o navegador para o usuário entrar na conta Google uma única vez."""
    with sync_playwright() as p:
        ctx = _abrir_contexto(p, perfil)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://accounts.google.com/")
        input("Faça login na janela que abriu e depois pressione Enter aqui... ")
        ctx.close()
    print("Login salvo no perfil:", perfil)


def mesclar(antigo, novo):
    """Combina a versão anterior de uma fala com a versão atualizada."""
    if not antigo or novo == antigo:
        return novo or antigo
    # Caso comum: o texto cresceu ou teve palavras corrigidas
    n = min(60, len(antigo), len(novo))
    if SequenceMatcher(None, antigo[:n], novo[:n]).ratio() > 0.7:
        return novo if len(novo) >= len(antigo) * 0.7 else antigo
    # O início do texto saiu da tela: o fim do antigo é o começo do novo
    for k in range(min(len(antigo), len(novo)), 9, -1):
        if antigo[-k:] == novo[:k]:
            return antigo + novo[k:]
    idx = antigo.rfind(novo[:15])
    if idx >= 0:
        return antigo[:idx] + novo
    return antigo + " " + novo


class MeetBot:
    def __init__(self, link, nome, perfil, pasta_saida, max_minutos=180, debug=False):
        self.link = link
        self.nome = nome
        self.perfil = Path(perfil)
        self.max_minutos = max_minutos
        self.debug = debug
        pasta = Path(pasta_saida)
        pasta.mkdir(parents=True, exist_ok=True)
        carimbo = datetime.now().strftime("%Y-%m-%d_%H-%M")
        self.arquivo = pasta / f"reuniao_{carimbo}_transcricao.txt"
        self.falas = {}  # id -> {"hora", "falante", "texto"} (mantém a ordem de chegada)

    # ---------- utilidades ----------
    def _botao(self, page, chave):
        return page.get_by_role("button", name=re.compile(PADROES[chave], re.I)).first

    def _clicar(self, page, chave, timeout=3000):
        try:
            btn = self._botao(page, chave)
            btn.wait_for(state="visible", timeout=timeout)
            btn.click()
            return True
        except Exception:
            return False

    # ---------- etapas ----------
    def _entrar(self, page):
        print("Abrindo a reunião...")
        page.goto(self.link)
        page.wait_for_load_state("domcontentloaded")
        time.sleep(4)

        self._clicar(page, "sem_midia", 5000)

        # Campo de nome (só aparece quando não há conta Google logada)
        try:
            campo = page.get_by_role("textbox").first
            campo.wait_for(state="visible", timeout=3000)
            campo.fill(self.nome)
        except Exception:
            pass

        self._clicar(page, "mic_off", 1500)
        self._clicar(page, "cam_off", 1500)

        if not self._clicar(page, "entrar", 15000):
            raise RuntimeError(
                "Não encontrei o botão para entrar. O link é válido? "
                "Se o Meet disse que você não pode participar, rode com --login e use uma conta Google."
            )

        foto = self.arquivo.parent / "tela_atual.png"
        print("Aguardando ser admitido na reunião (até 10 minutos)...")
        print(f"A tela do bot é salva a cada 15 s em: {foto}")
        limite = time.time() + 600
        ultima_foto = 0
        while time.time() < limite:
            try:
                if self._botao(page, "sair").is_visible():
                    print("Dentro da reunião.")
                    return
                texto = page.inner_text("body")
            except Exception:
                texto = ""
            if re.search(BLOQUEIOS, texto, re.I):
                page.screenshot(path=str(foto))
                msg = re.search(BLOQUEIOS, texto, re.I).group(0)
                raise RuntimeError(f"O Meet não deixou o bot entrar (mensagem: \"{msg}\"). Veja {foto}")
            if time.time() - ultima_foto > 15:
                try:
                    page.screenshot(path=str(foto))
                except Exception:
                    pass
                ultima_foto = time.time()
            time.sleep(2)
        raise RuntimeError("Ninguém admitiu o bot em 10 minutos.")

    def _ativar_legendas(self, page):
        time.sleep(2)
        if not self._clicar(page, "legendas", 5000):
            page.keyboard.press("c")  # atalho do Meet para legendas
        print("Legendas ativadas (confira o idioma nas configurações de legenda do Meet).")

    def _registrar(self, bloco):
        f = self.falas.get(bloco["id"])
        if f is None:
            self.falas[bloco["id"]] = {
                "hora": datetime.now().strftime("%H:%M:%S"),
                "falante": bloco["falante"],
                "texto": bloco["texto"],
            }
        else:
            f["texto"] = mesclar(f["texto"], bloco["texto"])
            if bloco["falante"]:
                f["falante"] = bloco["falante"]

    def _salvar(self):
        linhas = []
        for f in self.falas.values():
            quem = f["falante"] or "Participante"
            linhas.append(f"[{f['hora']}] {quem}: {f['texto']}")
        self.arquivo.write_text("\n".join(linhas) + "\n", encoding="utf-8")

    def _capturar(self, page):
        print("Registrando a conversa. Pressione Ctrl+C para encerrar.")
        inicio = time.time()
        ultimo_save = inicio
        ultima_tentativa_legenda = inicio
        sem_botao_sair = 0
        salvou_debug = False

        while True:
            agora = time.time()
            if agora - inicio > self.max_minutos * 60:
                print("Tempo máximo atingido.")
                break

            # A reunião acabou (ou o bot foi removido) quando o botão de sair some
            try:
                sem_botao_sair = 0 if self._botao(page, "sair").count() else sem_botao_sair + 1
            except Exception:
                break
            if sem_botao_sair >= 8:
                print("A reunião terminou ou o bot saiu.")
                break

            try:
                blocos = page.evaluate(JS_LEGENDAS)
            except Exception:
                break

            if blocos is None:
                # Área de legendas não encontrada: tenta ligar de novo a cada 30 s
                if agora - ultima_tentativa_legenda > 30:
                    print("Área de legendas não encontrada, tentando ativar de novo...")
                    self._ativar_legendas(page)
                    ultima_tentativa_legenda = agora
            else:
                for b in blocos:
                    self._registrar(b)
                if self.debug and not salvou_debug and blocos:
                    html = page.evaluate(
                        "() => document.querySelector('[role=\"region\"][aria-label*=\"egenda\" i], "
                        "[role=\"region\"][aria-label*=\"aption\" i]')?.outerHTML || ''"
                    )
                    Path(self.arquivo.with_suffix(".debug.html")).write_text(html, encoding="utf-8")
                    salvou_debug = True

            if agora - ultimo_save > 15:
                self._salvar()
                ultimo_save = agora
            time.sleep(0.7)

    def executar(self):
        """Entra, captura até a reunião acabar e devolve o caminho da transcrição."""
        with tela_virtual(), sync_playwright() as p:
            ctx = _abrir_contexto(p, self.perfil)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            try:
                self._entrar(page)
                self._ativar_legendas(page)
                self._capturar(page)
            except KeyboardInterrupt:
                print("\nEncerrando a pedido do usuário...")
            except Exception:
                # Sem tela, uma captura ajuda a ver onde o bot travou
                foto = self.arquivo.with_suffix(".erro.png")
                try:
                    page.screenshot(path=str(foto))
                    print(f"Captura da tela no momento do erro: {foto}")
                except Exception:
                    pass
                raise
            finally:
                self._salvar()
                try:
                    self._clicar(page, "sair", 2000)
                    ctx.close()
                except Exception:
                    pass
        print(f"Transcrição salva em: {self.arquivo} ({len(self.falas)} falas)")
        return self.arquivo
