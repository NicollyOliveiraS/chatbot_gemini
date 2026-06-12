import sys

if sys.platform != "win32":
    try:
        from gevent import monkey
        monkey.patch_all()
    except ImportError:
        print("Gevent não instalado!")


from flask import Flask, request, jsonify
from flask_socketio import SocketIO, emit
from google import genai
from google.genai import types
from dotenv import load_dotenv
import os
import time

# Carrega as variáveis ocultas do arquivo .env (como a chave da API do Gemini)
load_dotenv()

# Define qual versão da IA vamos usar. O modelo "flash" é rápido e ideal para chatbots.
MODELO = "gemini-3.1-flash-lite"

# Aqui definimos o "Prompt de Sistema". É a personalidade e as regras que o bot deve seguir.
instrucoes = """
Você é um assistente virtual que auxilia nos treinos de musculação. De sujestoes de treinos e dicas."""

# Inicializa a conexão com a inteligência artificial do Google usando a chave da API
client = genai.Client(api_key=os.getenv("GENAI_KEY"))

# Cria o nosso aplicativo web principal (o servidor)
app = Flask(__name__)

# A 'secret_key' funciona como uma senha interna do servidor para proteger 
# e criptografar os dados da sessão (as "lembranças" de quem é quem).
app.secret_key = "ch@tb07"

# Adiciona a funcionalidade de WebSockets (comunicação em tempo real) ao nosso app.
# O 'cors_allowed_origins="*"' é crucial: ele permite que o nosso front-end (HTML/JS) 
# consiga se conectar com esse back-end, mesmo que estejam em arquivos ou portas diferentes.
socketio = SocketIO(app, cors_allowed_origins="*", manage_session=False, ping_timeout=30, ping_interval=15)

# Dicionário que funciona como a "memória temporária" do servidor. 
# Ele guarda a conversa de cada aluno separadamente usando um ID único.
active_chats = {}

def get_user_chat(sid, force_model=None):
    """
    Função principal de gerenciamento de usuários.
    Ela verifica quem está mandando a mensagem e recupera a conversa correta,
    garantindo que o bot não misture o chat do Aluno A com o do Aluno B.
    Usa o request.sid do Socket.IO como identificador único de cada conexão.
    Suporta fallback automático de modelo se o principal estiver congestionado.
    """
    # Se for forçado ou se o ID não existe/está nulo, criamos ou recriamos a conversa
    if sid not in active_chats or active_chats[sid] is None or force_model:
        models_to_try = [force_model] if force_model else ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-2.5-flash-lite"]
        last_error = None
        for model in models_to_try:
            try:
                print(f"Criando chat Gemini ({model}) para sid: {sid}")
                chat_session = client.chats.create(
                    model=model,
                    config=types.GenerateContentConfig(system_instruction=instrucoes)
                )
                active_chats[sid] = (chat_session, model)
                print(f"Chat criado com sucesso usando {model}")
                return chat_session
            except Exception as e:
                app.logger.warning(f"Erro ao criar chat com o modelo {model}: {e}")
                last_error = e
        if last_error:
            raise last_error
            
    # Retorna o objeto de chat (que é a primeira posição da tupla (chat, modelo))
    return active_chats[sid][0]

# Rota simples para verificar se o servidor está rodando.
# Ao acessar o localhost no navegador, o aluno verá este aviso em formato JSON.
@app.route('/')
def root():
    return jsonify({
        "api-websocket": "chatbot",
        "status": "ok"
    })


# ------------------------------------------------------------------
# EVENTOS SOCKET.IO (Onde a mágica do tempo real acontece)
# ------------------------------------------------------------------

@socketio.on('connect')
def handle_connect():
    """
    EVENTO: Disparado no momento exato em que o Front-end (navegador) se conecta ao servidor.
    """
    sid = request.sid
    print(f"Cliente conectado: {sid}")
    
    try:
        # Tenta criar o chat do usuário assim que ele entra
        get_user_chat(sid)
        print(f"Chat inicializado para sid: {sid}")
        
        # O comando 'emit' serve para enviar um pacote de dados do servidor PARA o front-end.
        emit('status_conexao', {'data': 'Conectado com sucesso!', 'session_id': sid})
    except Exception as e:
        app.logger.error(f"Erro durante o evento connect para {sid}: {e}", exc_info=True)
        emit('erro', {'erro': 'Falha ao inicializar a sessão de chat no servidor.'})


@socketio.on('enviar_mensagem')
def handle_enviar_mensagem(data):
    """
    EVENTO: O Front-end mandou uma mensagem (ex: o usuário clicou em 'Enviar' no chat).
    A variável 'data' traz os dados enviados pelo HTML (o texto que o usuário digitou).
    Inclui retry com backoff exponencial e fallback automático para outros modelos.
    """
    sid = request.sid
    try:
        # Pega o texto de dentro do dicionário enviado pelo JS
        mensagem_usuario = data.get("mensagem")
        app.logger.info(f"Mensagem recebida de {sid}: {mensagem_usuario}")

        # Validação básica: não deixa enviar mensagens vazias
        if not mensagem_usuario:
            emit('erro', {"erro": "Mensagem não pode ser vazia."})
            return

        # Lista de modelos para tentar (o principal + alternativas)
        MODELOS_FALLBACK = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-2.5-flash-lite"]
        MAX_RETRIES = 2  # tentativas por modelo
        
        resposta_texto = None
        ultimo_erro = None

        for modelo in MODELOS_FALLBACK:
            for tentativa in range(MAX_RETRIES):
                try:
                    # Pega ou cria o chat com o modelo atual
                    user_chat = get_user_chat(sid, force_model=modelo if modelo != MODELOS_FALLBACK[0] else None)
                    if user_chat is None:
                        emit('erro', {"erro": "Sessão de chat não pôde ser estabelecida."})
                        return

                    # Envia a mensagem para o Gemini
                    resposta_gemini = user_chat.send_message(mensagem_usuario)

                    # Extrai o texto da resposta
                    resposta_texto = (
                        resposta_gemini.text
                        if hasattr(resposta_gemini, 'text')
                        else resposta_gemini.candidates[0].content.parts[0].text
                    )
                    break  # Sucesso! Sai do loop de retries

                except Exception as e:
                    ultimo_erro = e
                    erro_str = str(e).lower()
                    
                    # Se for erro 503 (sobrecarregado) ou 429 (limite de requisições), tenta de novo
                    if '503' in erro_str or 'unavailable' in erro_str or '429' in erro_str or 'resource_exhausted' in erro_str:
                        wait_time = (tentativa + 1) * 2  # 2s, 4s
                        app.logger.warning(f"Modelo {modelo} sobrecarregado (tentativa {tentativa+1}/{MAX_RETRIES}). Aguardando {wait_time}s...")
                        time.sleep(wait_time)
                        
                        # Limpa o chat atual para forçar recriação com o próximo modelo
                        if sid in active_chats:
                            active_chats[sid] = None
                        continue
                    else:
                        # Se for outro tipo de erro, não tenta de novo
                        raise e
            
            if resposta_texto:
                break  # Sucesso! Sai do loop de modelos
            else:
                app.logger.warning(f"Modelo {modelo} falhou após {MAX_RETRIES} tentativas. Tentando próximo modelo...")
                # Limpa o chat para forçar recriação com o próximo modelo
                if sid in active_chats:
                    active_chats[sid] = None

        if resposta_texto:
            # Envia a resposta de volta para o front-end
            emit('nova_mensagem', {"remetente": "bot", "texto": resposta_texto, "session_id": sid})
            app.logger.info(f"Resposta enviada para {sid}")
        else:
            # Todos os modelos falharam
            emit('erro', {"erro": "Todos os modelos do Gemini estão temporariamente sobrecarregados. Por favor, tente novamente em alguns segundos."})

    except Exception as e:
        app.logger.error(f"Erro ao processar 'enviar_mensagem' para {sid}: {e}", exc_info=True)
        # Se algo quebrar (ex: falha de internet), avisamos o front-end educadamente.
        emit('erro', {"erro": f"Ocorreu um erro no servidor: {str(e)}"})


@socketio.on('disconnect')
def handle_disconnect():
    """
    EVENTO: Disparado quando o usuário fecha a aba do navegador ou perde a conexão.
    """
    sid = request.sid
    print(f"Cliente desconectado: {sid}")
    # Limpa o chat da memória quando o usuário desconecta
    if sid in active_chats:
        del active_chats[sid]
        print(f"Chat removido da memória para sid: {sid}")


# Inicia o servidor. Lê a porta do ambiente (necessário para o Render) ou usa a 5001 por padrão.
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    # No Render, precisamos escutar em 0.0.0.0. Localmente usa 127.0.0.1.
    host = "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"
    socketio.run(app, host=host, port=port, debug=True)
