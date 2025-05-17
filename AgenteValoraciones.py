# -*- coding: utf-8 -*-
"""
*** Agente de Valoraciones ***

Este agente tiene dos capacidades principales:
1. Valorar planes: Cuando ha pasado cierto tiempo, lee planes activos en la BD, 
   solicita opinión al usuario y registra en BD de valoraciones.
2. Recomendar: Capacidad proactiva que periódicamente analiza valoraciones del usuario
   y recomienda lugares turísticos usando técnicas de aprendizaje automático.

@author: Sergi
"""

from multiprocessing import Process, Queue
import socket
import argparse
import datetime
import uuid
import random
import time
import threading
import logging
import json
import os

from rdflib import Namespace, Graph, Literal, URIRef
from rdflib.namespace import RDF, RDFS, XSD, FOAF
from flask import Flask, request, Response, render_template

from AgentUtil.FlaskServer import shutdown_server
from AgentUtil.Agent import Agent
from AgentUtil.ACLMessages import build_message, send_message, get_message_properties
from AgentUtil.ACL import ACL
from AgentUtil.DSO import DSO

# Configurar logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

__author__ = 'Sergi'

# Configuration stuff
parser = argparse.ArgumentParser()
parser.add_argument('--open', help="Define si el servidor está abierto al exterior o no", action='store_true',
                    default=False)
parser.add_argument('--port', type=int, help="Puerto de comunicación del agente")
parser.add_argument('--dhost', help="Host del agente de directorio")
parser.add_argument('--dport', type=int, help="Puerto del agente de directorio")

args = parser.parse_args()

# Configuración del host y puerto
if args.port is None:
    port = 9003  # Puerto distinto a otros agentes
else:
    port = args.port

if args.open:
    hostname = '0.0.0.0'
else:
    hostname = socket.gethostname()

if args.dhost is None:
    dhostname = socket.gethostname()
else:
    dhostname = args.dhost

if args.dport is None:
    dport = 9000
else:
    dport = args.dport

# Definición de los espacios de nombres
agn = Namespace("http://www.agentes.org#")
onto = Namespace("http://www.semanticweb.org/arnau/ontologies/2025/3/Entrega2/")

# Contador de mensajes
mss_cnt = 0

# Datos del Agente
AgenteValoraciones = Agent('AgenteValoraciones',
                           agn.AgenteValoraciones,
                           'http://%s:%d/comm' % (hostname, port),
                           'http://%s:%d/Stop' % (hostname, port))

# Directory agent address
DirectoryAgent = Agent('DirectoryAgent',
                       agn.Directory,
                       'http://%s:%d/Register' % (dhostname, dport),
                       'http://%s:%d/Stop' % (dhostname, dport))

# Global triplestore graph
dsgraph = Graph()
# Cargar la ontología en el grafo
try:
    dsgraph.parse("entrega2.ttl", format="turtle")
    logger.info("Ontología cargada correctamente")
except Exception as e:
    logger.error(f"Error al cargar la ontología: {e}")

cola1 = Queue()  # Cola para comunicación entre procesos

# Datos para simulación de base de datos
BD_PLANES = {}  # Estructura: {id_plan: {usuario, destino, fecha_inicio, fecha_fin, estado}}
BD_VALORACIONES = {}  # Estructura: {id_valoracion: {usuario, destino, puntuacion, comentario}}

# Directorio para almacenar datos simulados
DATA_DIR = "data"
PLANES_FILE = os.path.join(DATA_DIR, "planes.json")
VALORACIONES_FILE = os.path.join(DATA_DIR, "valoraciones.json")

# Asegurar que existe el directorio de datos
if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR)

# Cargar datos si existen
def cargar_datos():
    global BD_PLANES, BD_VALORACIONES
    
    try:
        if os.path.exists(PLANES_FILE):
            with open(PLANES_FILE, 'r') as f:
                BD_PLANES = json.load(f)
            logger.info(f"Cargados {len(BD_PLANES)} planes")
    except Exception as e:
        logger.error(f"Error al cargar planes: {e}")
    
    try:
        if os.path.exists(VALORACIONES_FILE):
            with open(VALORACIONES_FILE, 'r') as f:
                BD_VALORACIONES = json.load(f)
            logger.info(f"Cargadas {len(BD_VALORACIONES)} valoraciones")
    except Exception as e:
        logger.error(f"Error al cargar valoraciones: {e}")

# Guardar datos
def guardar_datos():
    try:
        with open(PLANES_FILE, 'w') as f:
            json.dump(BD_PLANES, f)
        
        with open(VALORACIONES_FILE, 'w') as f:
            json.dump(BD_VALORACIONES, f)
        
        logger.info("Datos guardados correctamente")
    except Exception as e:
        logger.error(f"Error al guardar datos: {e}")

# Cargar datos al inicio
cargar_datos()

# Flask stuff
app = Flask(__name__)

@app.route("/comm")
def comunicacion():
    """
    Punto de entrada de comunicación para recibir peticiones
    """
    global dsgraph
    global mss_cnt

    message = request.args['content']
    gm = Graph()
    gm.parse(data=message, format='xml')
    
    msgdic = get_message_properties(gm)
    logger.debug(f"Recibido mensaje con performativa: {msgdic['performative']}")

    # Verificar si es una petición de valoración
    if msgdic['performative'] == ACL.request:
        # Buscar el contenido de la petición
        content = msgdic['content']
        
        # Buscar petición de valoración
        for s, p, o in gm.triples((None, RDF.type, onto.PeticionValoracion)):
            # Procesar petición de valoración
            usuario = None
            id_plan = None
            
            # Obtener usuario
            for s1, p1, o1 in gm.triples((s, onto.realizadaPorUsuario, None)):
                usuario = str(o1)
            
            # Obtener ID del plan
            for s1, p1, o1 in gm.triples((s, onto.sobrePlan, None)):
                id_plan = str(o1)
            
            if usuario and id_plan:
                respuesta = procesar_peticion_valoracion(usuario, id_plan, content)
                return Response(respuesta, mimetype='text/xml')
            else:
                logger.warning("Petición incompleta: falta usuario o ID del plan")
                return Response(status=400)
        
        # Buscar petición de recomendación
        for s, p, o in gm.triples((None, RDF.type, onto.PeticionRecomendacion)):
            # Procesar petición de recomendación
            usuario = None
            
            # Obtener usuario
            for s1, p1, o1 in gm.triples((s, onto.realizadaPorUsuario, None)):
                usuario = str(o1)
            
            if usuario:
                respuesta = procesar_peticion_recomendacion(usuario, content)
                return Response(respuesta, mimetype='text/xml')
            else:
                logger.warning("Petición incompleta: falta usuario")
                return Response(status=400)
    
    # Si no es una petición reconocida, devolver error
    logger.warning("Petición no reconocida")
    return Response(status=400)


@app.route("/Stop")
def stop():
    """
    Entrypoint que para el agente
    """
    tidyup()
    shutdown_server()
    return "Parando Agente de Valoraciones"


@app.route("/admin")
def admin_panel():
    """
    Panel de administración para ver planes y valoraciones
    """
    return render_template('valoraciones_admin.html', 
                           planes=BD_PLANES, 
                           valoraciones=BD_VALORACIONES)


def tidyup():
    """
    Acciones previas a parar el agente
    """
    global cola1
    cola1.put(0)
    # Guardar datos antes de terminar
    guardar_datos()


def procesar_peticion_valoracion(usuario, id_plan, content_uri):
    """
    Procesa una petición de valoración de un plan
    
    :param usuario: URI o ID del usuario
    :param id_plan: URI o ID del plan
    :param content_uri: URI del contenido para responder
    :return: Mensaje XML con la respuesta
    """
    global mss_cnt
    
    # Crear un nuevo ID para la valoración
    id_valoracion = str(uuid.uuid4())
    
    # Obtener datos del plan (simulado)
    destino = "Ciudad desconocida"
    if id_plan in BD_PLANES:
        destino = BD_PLANES[id_plan].get('destino', destino)
    
    # Simular puntuación y comentario
    puntuacion = random.randint(1, 5)
    comentarios = [
        "Excelente experiencia", "Me gustó bastante", "Aceptable", 
        "Regular, podría ser mejor", "No me gustó nada"
    ]
    comentario = comentarios[5 - puntuacion]
    
    # Registrar valoración
    BD_VALORACIONES[id_valoracion] = {
        'usuario': usuario,
        'destino': destino,
        'puntuacion': puntuacion,
        'comentario': comentario,
        'fecha': datetime.datetime.now().isoformat()
    }
    
    # Guardar datos
    guardar_datos()
    
    # Crear respuesta
    g = Graph()
    g.bind('rdf', RDF)
    g.bind('rdfs', RDFS)
    g.bind('xsd', XSD)
    g.bind('onto', onto)
    
    # Crear la respuesta
    respuesta_id = URIRef(f'respuesta_valoracion_{str(uuid.uuid4())}')
    g.add((respuesta_id, RDF.type, onto.RespuestaValoracion))
    g.add((respuesta_id, onto.deUsuario, Literal(usuario)))
    g.add((respuesta_id, onto.sobrePlan, Literal(id_plan)))
    g.add((respuesta_id, onto.puntuacion, Literal(puntuacion, datatype=XSD.integer)))
    g.add((respuesta_id, RDFS.comment, Literal(comentario)))
    g.add((respuesta_id, onto.fechaValoracion, Literal(datetime.datetime.now().isoformat(), datatype=XSD.dateTime)))
    
    # Construir mensaje completo
    mss_cnt += 1
    return build_message(g, ACL.inform, 
                         sender=AgenteValoraciones.uri, 
                         receiver=content_uri, 
                         msgcnt=mss_cnt).serialize(format='xml')


def procesar_peticion_recomendacion(usuario, content_uri):
    """
    Procesa una petición de recomendación para un usuario
    
    :param usuario: URI o ID del usuario
    :param content_uri: URI del contenido para responder
    :return: Mensaje XML con la respuesta
    """
    global mss_cnt
    
    # Obtener valoraciones del usuario (simulado)
    valoraciones_usuario = [v for v in BD_VALORACIONES.values() if v.get('usuario') == usuario]
    
    # Destinos posibles para recomendación
    destinos_posibles = ["Barcelona", "Madrid", "Valencia", "Sevilla", "Granada", 
                        "Paris", "Roma", "Londres", "Berlin", "Amsterdam"]
    
    # Método simple de recomendación basado en datos anteriores
    recomendacion = ""
    if valoraciones_usuario:
        # Simular recomendación basada en valoraciones previas
        # Para este ejemplo, simplemente recomendaremos un destino aleatorio diferente
        destinos_visitados = [v.get('destino') for v in valoraciones_usuario]
        destinos_no_visitados = [d for d in destinos_posibles if d not in destinos_visitados]
        
        if destinos_no_visitados:
            recomendacion = random.choice(destinos_no_visitados)
        else:
            # Si ya ha visitado todos, recomendar el mejor valorado
            mejor_destino = max(valoraciones_usuario, key=lambda x: x.get('puntuacion', 0))
            recomendacion = mejor_destino.get('destino', destinos_posibles[0])
    else:
        # Si no hay valoraciones previas, recomendación aleatoria
        recomendacion = random.choice(destinos_posibles)
    
    # Crear respuesta
    g = Graph()
    g.bind('rdf', RDF)
    g.bind('rdfs', RDFS)
    g.bind('onto', onto)
    
    # Crear la respuesta
    respuesta_id = URIRef(f'respuesta_recomendacion_{str(uuid.uuid4())}')
    g.add((respuesta_id, RDF.type, onto.RespuestaRecomendacion))
    g.add((respuesta_id, onto.paraUsuario, Literal(usuario)))
    g.add((respuesta_id, onto.destinoRecomendado, Literal(recomendacion)))
    g.add((respuesta_id, onto.fechaRecomendacion, Literal(datetime.datetime.now().isoformat(), datatype=XSD.dateTime)))
    g.add((respuesta_id, RDFS.comment, Literal(f"Recomendación generada basada en el análisis de preferencias previas")))
    
    # Construir mensaje completo
    mss_cnt += 1
    return build_message(g, ACL.inform, 
                         sender=AgenteValoraciones.uri, 
                         receiver=content_uri, 
                         msgcnt=mss_cnt).serialize(format='xml')


def plan_valoracion():
    """
    Plan para solicitar valoraciones de planes activos
    Se ejecuta periódicamente en un hilo separado
    """
    logger.info("Iniciando plan de valoraciones")
    
    while True:
        try:
            # Buscar planes activos que han finalizado
            planes_para_valorar = {}
            fecha_actual = datetime.datetime.now()
            
            for id_plan, plan in BD_PLANES.items():
                if plan.get('estado') == 'finalizado' and not plan.get('valorado', False):
                    fecha_fin = datetime.datetime.fromisoformat(plan.get('fecha_fin'))
                    # Si el plan finalizó hace menos de 7 días, solicitar valoración
                    if (fecha_actual - fecha_fin).days <= 7:
                        planes_para_valorar[id_plan] = plan
            
            # Solicitar valoraciones
            for id_plan, plan in planes_para_valorar.items():
                usuario = plan.get('usuario')
                destino = plan.get('destino')
                logger.info(f"Solicitando valoración para plan {id_plan} del usuario {usuario} en {destino}")
                
                # Simular la valoración directamente (en un sistema real, se enviaría una notificación al usuario)
                id_valoracion = str(uuid.uuid4())
                puntuacion = random.randint(1, 5)
                comentarios = [
                    "Excelente experiencia", "Me gustó bastante", "Aceptable", 
                    "Regular, podría ser mejor", "No me gustó nada"
                ]
                comentario = comentarios[5 - puntuacion]
                
                # Registrar valoración
                BD_VALORACIONES[id_valoracion] = {
                    'usuario': usuario,
                    'destino': destino,
                    'puntuacion': puntuacion,
                    'comentario': comentario,
                    'fecha': datetime.datetime.now().isoformat()
                }
                
                # Actualizar el estado del plan
                BD_PLANES[id_plan]['valorado'] = True
            
            if planes_para_valorar:
                guardar_datos()
                logger.info(f"Procesadas {len(planes_para_valorar)} valoraciones")
            
            # Esperar antes de la siguiente iteración (1 hora en un sistema real)
            time.sleep(60)  # 60 segundos para pruebas
            
        except Exception as e:
            logger.error(f"Error en plan de valoraciones: {e}")
            time.sleep(60)


def plan_recomendacion():
    """
    Plan para generar recomendaciones proactivas
    Se ejecuta periódicamente en un hilo separado
    """
    logger.info("Iniciando plan de recomendaciones proactivas")
    
    while True:
        try:
            # Obtener lista de usuarios únicos
            usuarios = set([v.get('usuario') for v in BD_VALORACIONES.values()])
            
            for usuario in usuarios:
                # Simular criterio para enviar recomendación (por ejemplo, si no hay planes activos)
                if random.random() < 0.3:  # 30% de probabilidad para hacer pruebas
                    # Generar recomendación
                    valoraciones_usuario = [v for v in BD_VALORACIONES.values() if v.get('usuario') == usuario]
                    
                    # Destinos posibles para recomendación
                    destinos_posibles = ["Barcelona", "Madrid", "Valencia", "Sevilla", "Granada", 
                                        "Paris", "Roma", "Londres", "Berlin", "Amsterdam"]
                    
                    # Método simple de recomendación
                    if valoraciones_usuario:
                        # Basado en valoraciones previas
                        destinos_visitados = [v.get('destino') for v in valoraciones_usuario]
                        destinos_no_visitados = [d for d in destinos_posibles if d not in destinos_visitados]
                        
                        if destinos_no_visitados:
                            recomendacion = random.choice(destinos_no_visitados)
                        else:
                            # Si ya ha visitado todos
                            mejor_destino = max(valoraciones_usuario, key=lambda x: x.get('puntuacion', 0))
                            recomendacion = mejor_destino.get('destino', destinos_posibles[0])
                    else:
                        # Si no hay valoraciones previas
                        recomendacion = random.choice(destinos_posibles)
                    
                    logger.info(f"Generando recomendación proactiva para usuario {usuario}: {recomendacion}")
                    
                    # En un sistema real, aquí se enviaría la recomendación al usuario
                    # Por ejemplo, mediante algún mecanismo de notificación o mensajería
            
            # Esperar antes de la siguiente iteración (24 horas en un sistema real)
            time.sleep(120)  # 120 segundos para pruebas
            
        except Exception as e:
            logger.error(f"Error en plan de recomendaciones: {e}")
            time.sleep(60)


def agentbehavior1(cola):
    """
    Comportamiento del agente - Registrarse en el directorio
    """
    global mss_cnt
    # Registrar el agente en el servicio de directorio
    gmess = Graph()
    gmess.bind('foaf', FOAF)
    gmess.bind('dso', DSO)
    reg_obj = agn[AgenteValoraciones.name + '-Register']
    gmess.add((reg_obj, RDF.type, DSO.Register))
    gmess.add((reg_obj, DSO.Uri, AgenteValoraciones.uri))
    gmess.add((reg_obj, FOAF.name, Literal(AgenteValoraciones.name)))
    gmess.add((reg_obj, DSO.Address, Literal(AgenteValoraciones.address)))
    gmess.add((reg_obj, DSO.AgentType, DSO.RatingsAgent))

    # Lo metemos en el registro de servicios
    try:
        send_message(
            build_message(gmess, ACL.request,
                        sender=AgenteValoraciones.uri,
                        receiver=DirectoryAgent.uri,
                        content=reg_obj,
                        msgcnt=mss_cnt),
            DirectoryAgent.address
        )
        mss_cnt += 1
        logger.info("Agente registrado en el directorio")
    except Exception as e:
        logger.warning(f"No se pudo conectar con el DirectoryAgent: {e}")
        logger.warning("El agente continuará funcionando sin registro en el directorio")
    
    # Iniciar hilos para los planes
    threading.Thread(target=plan_valoracion, daemon=True).start()
    threading.Thread(target=plan_recomendacion, daemon=True).start()
    
    # Generar algunos datos de ejemplo para pruebas
    generar_datos_ejemplo()
    
    # Bucle principal del comportamiento
    while True:
        try:
            # Esperar a un mensaje en la cola
            msg = cola.get()
            if msg == 0:
                logger.info("Finalizando comportamiento del agente")
                break
        except Exception as e:
            logger.error(f"Error en el comportamiento del agente: {e}")
            break


def generar_datos_ejemplo():
    """
    Genera algunos datos de ejemplo para pruebas
    """
    global BD_PLANES, BD_VALORACIONES
    
    # Solo generar si no hay datos
    if not BD_PLANES:
        # Usuarios de ejemplo
        usuarios = ["user1", "user2", "user3"]
        
        # Destinos de ejemplo
        destinos = ["Barcelona", "Madrid", "Valencia", "Sevilla", "Paris", "Roma"]
        
        # Generar algunos planes
        for i in range(10):
            id_plan = str(uuid.uuid4())
            fecha_inicio = (datetime.datetime.now() - datetime.timedelta(days=random.randint(10, 30))).isoformat()
            fecha_fin = (datetime.datetime.now() - datetime.timedelta(days=random.randint(0, 7))).isoformat()
            
            BD_PLANES[id_plan] = {
                'usuario': random.choice(usuarios),
                'destino': random.choice(destinos),
                'fecha_inicio': fecha_inicio,
                'fecha_fin': fecha_fin,
                'estado': 'finalizado',
                'valorado': random.choice([True, False])
            }
        
        # Generar algunas valoraciones
        for i in range(5):
            id_valoracion = str(uuid.uuid4())
            usuario = random.choice(usuarios)
            destino = random.choice(destinos)
            puntuacion = random.randint(1, 5)
            comentarios = [
                "Excelente experiencia", "Me gustó bastante", "Aceptable", 
                "Regular, podría ser mejor", "No me gustó nada"
            ]
            comentario = comentarios[5 - puntuacion]
            
            BD_VALORACIONES[id_valoracion] = {
                'usuario': usuario,
                'destino': destino,
                'puntuacion': puntuacion,
                'comentario': comentario,
                'fecha': (datetime.datetime.now() - datetime.timedelta(days=random.randint(0, 30))).isoformat()
            }
        
        # Guardar datos
        guardar_datos()
        logger.info("Generados datos de ejemplo para pruebas")


@app.route("/test", methods=['GET', 'POST'])
def test_interface():
    """
    Interfaz web para probar el agente de valoraciones
    """
    if request.method == 'GET':
        # Mostrar un formulario para pruebas
        return '''
        <html>
            <head>
                <title>Test Agente Valoraciones</title>
                <style>
                    body { font-family: Arial, sans-serif; margin: 20px; }
                    .form-group { margin-bottom: 15px; }
                    label { display: block; margin-bottom: 5px; }
                    input, select { padding: 8px; width: 300px; }
                    button { padding: 10px 15px; background-color: #4CAF50; color: white; border: none; cursor: pointer; }
                    h2 { margin-top: 30px; }
                    table { border-collapse: collapse; width: 100%; }
                    th, td { border: 1px solid #ddd; padding: 8px; text-align: left; }
                    th { background-color: #f2f2f2; }
                </style>
            </head>
            <body>
                <h1>Test Agente Valoraciones</h1>
                
                <h2>Datos actuales</h2>
                
                <h3>Planes</h3>
                <table>
                    <tr>
                        <th>ID</th>
                        <th>Usuario</th>
                        <th>Destino</th>
                        <th>Estado</th>
                        <th>Valorado</th>
                    </tr>
                    {% for id_plan, plan in planes.items() %}
                    <tr>
                        <td>{{ id_plan[:8] }}...</td>
                        <td>{{ plan.usuario }}</td>
                        <td>{{ plan.destino }}</td>
                        <td>{{ plan.estado }}</td>
                        <td>{{ "Sí" if plan.valorado else "No" }}</td>
                    </tr>
                    {% endfor %}
                </table>
                
                <h3>Valoraciones</h3>
                <table>
                    <tr>
                        <th>ID</th>
                        <th>Usuario</th>
                        <th>Destino</th>
                        <th>Puntuación</th>
                        <th>Comentario</th>
                    </tr>
                    {% for id_valor, valor in valoraciones.items() %}
                    <tr>
                        <td>{{ id_valor[:8] }}...</td>
                        <td>{{ valor.usuario }}</td>
                        <td>{{ valor.destino }}</td>
                        <td>{{ valor.puntuacion }}/5</td>
                        <td>{{ valor.comentario }}</td>
                    </tr>
                    {% endfor %}
                </table>
                
                <h2>Solicitar valoración</h2>
                <form method="post" action="/test">
                    <input type="hidden" name="action" value="valoracion">
                    <div class="form-group">
                        <label>Usuario:</label>
                        <input type="text" name="usuario" required placeholder="Ej: user1">
                    </div>
                    <div class="form-group">
                        <label>Plan ID:</label>
                        <input type="text" name="id_plan" required placeholder="Ej: UUID del plan">
                    </div>
                    <button type="submit">Solicitar Valoración</button>
                </form>
                
                <h2>Solicitar recomendación</h2>
                <form method="post" action="/test">
                    <input type="hidden" name="action" value="recomendacion">
                    <div class="form-group">
                        <label>Usuario:</label>
                        <input type="text" name="usuario" required placeholder="Ej: user1">
                    </div>
                    <button type="submit">Solicitar Recomendación</button>
                </form>
            </body>
        </html>
        '''.replace('{% for id_plan, plan in planes.items() %}', '').replace('{% endfor %}', '').replace('{% for id_valor, valor in valoraciones.items() %}', '').replace('{% endfor %}', '')
    else:
        # Procesar la petición POST
        action = request.form['action']
        usuario = request.form['usuario']
        
        result = ""
        if action == 'valoracion':
            id_plan = request.form['id_plan']
            # Simular petición de valoración
            if id_plan in BD_PLANES:
                id_valoracion = str(uuid.uuid4())
                puntuacion = random.randint(1, 5)
                comentarios = ["Excelente", "Muy bueno", "Bueno", "Regular", "Malo"]
                comentario = comentarios[5 - puntuacion]
                
                BD_VALORACIONES[id_valoracion] = {
                    'usuario': usuario,
                    'destino': BD_PLANES[id_plan].get('destino', 'Desconocido'),
                    'puntuacion': puntuacion,
                    'comentario': comentario,
                    'fecha': datetime.datetime.now().isoformat()
                }
                
                BD_PLANES[id_plan]['valorado'] = True
                guardar_datos()
                
                result = f"Valoración registrada para el plan {id_plan[:8]}... con puntuación {puntuacion}/5"
            else:
                result = f"Error: El plan con ID {id_plan[:8]}... no existe"
        
        elif action == 'recomendacion':
            # Simular petición de recomendación
            destinos = ["Barcelona", "Madrid", "Valencia", "Sevilla", "Granada", "Paris", "Roma"]
            recomendacion = random.choice(destinos)
            
            result = f"Recomendación para {usuario}: Te recomendamos visitar {recomendacion}"
        
        # Mostrar resultado
        return f'''
        <html>
            <head>
                <title>Resultado</title>
                <style>
                    body {{ font-family: Arial, sans-serif; margin: 20px; }}
                    .result {{ background-color: #f0f0f0; padding: 15px; margin: 20px 0; border-radius: 5px; }}
                    .back {{ margin-top: 20px; }}
                </style>
            </head>
            <body>
                <h1>Resultado</h1>
                <div class="result">{result}</div>
                <div class="back"><a href="/test">Volver</a></div>
            </body>
        </html>
        '''


if __name__ == '__main__':
    try:
        # Poner en marcha los behaviors
        ab1 = Process(target=agentbehavior1, args=(cola1,))
        ab1.start()

        logger.info(f"Iniciando servidor en {hostname}:{port}")
        # Ponemos en marcha el servidor
        app.run(host=hostname, port=port, debug=False)

        # Esperamos a que acaben los behaviors
        ab1.join()
        logger.info('Agente de Valoraciones finalizado')
        
    except Exception as e:
        logger.error(f"Error al iniciar el agente: {e}")
        if 'ab1' in locals():
            ab1.terminate()
        print('Error en el Agente de Valoraciones')