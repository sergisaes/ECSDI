# -*- coding: utf-8 -*-
"""
*** Agente Mantenedor de Planes ***

Este agente mantiene una base de datos de planes activos y los actualiza según
las condiciones climáticas. Se comunica con AgenteClima para verificar el tiempo
y modificar actividades exteriores por interiores cuando sea necesario.

@author: Sergi
"""

from multiprocessing import Process, Queue
import socket
import argparse
import datetime
import uuid
import logging
import time
import traceback
import requests
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

# Configuration stuff
parser = argparse.ArgumentParser()
parser.add_argument('--open', help="Define si el servidor está abierto al exterior o no", action='store_true', default=False)
parser.add_argument('--port', type=int, help="Puerto de comunicación del agente")
parser.add_argument('--dhost', help="Host del agente de directorio")
parser.add_argument('--dport', type=int, help="Puerto del agente de directorio")

args = parser.parse_args()

# Configuración del host y puerto
if args.port is None:
    port = 9011  # Puerto para AgenteMantenedorPlanes
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
AgenteMantenedorPlanes = Agent('AgenteMantenedorPlanes',
                     agn.AgenteMantenedorPlanes,
                     'http://%s:%d/comm' % (hostname, port),
                     'http://%s:%d/Stop' % (hostname, port))

# Directory agent address
DirectoryAgent = Agent('DirectoryAgent',
                      agn.Directory,
                      'http://%s:%d/Register' % (dhostname, dport),
                      'http://%s:%d/Stop' % (dhostname, dport))

# Global triplestore graph - Base de datos RDF de planes activos
planes_db = Graph()
planes_db.bind('rdf', RDF)
planes_db.bind('rdfs', RDFS)
planes_db.bind('onto', onto)
planes_db.bind('xsd', XSD)

# Cargar la ontología en el grafo
try:
    planes_db.parse("entrega2.ttl", format="turtle")
    logger.info("Ontología cargada correctamente")
except Exception as e:
    logger.error(f"Error al cargar la ontología: {e}")

# Archivo para persistencia
DB_FILE = "planes_activos.rdf"

# Cargar planes previos si existe el archivo
if os.path.exists(DB_FILE):
    try:
        planes_db.parse(DB_FILE, format="xml")
        logger.info(f"Cargados {len(planes_db)} triples desde {DB_FILE}")
    except Exception as e:
        logger.error(f"Error al cargar base de datos: {e}")

# Cola para comunicación entre procesos
cola1 = Queue()

# Flask app
app = Flask(__name__)

@app.route("/comm")
def comunicacion():
    """
    Punto de entrada de comunicación para recibir planes y peticiones
    """
    global planes_db
    global mss_cnt

    message = request.args['content']
    gm = Graph()
    gm.parse(data=message, format='xml')
    
    msgdic = get_message_properties(gm)
    logger.debug(f"[DEPURACIÓN] Recibido mensaje con performativa: {msgdic['performative']}")

    # Si es un mensaje tipo request
    if msgdic['performative'] == ACL.request:
        content = msgdic['content']
        
        # Primero verificamos qué tipo de mensaje es basado en su contenido RDF
        es_registro_plan = False
        es_consulta_planes = False
        
        # Verificar si es un registro de plan
        for s, p, o in gm.triples((None, RDF.type, onto.RegistroPlan)):
            es_registro_plan = True
            break
            
        # Verificar si es una consulta de planes
        for s, p, o in gm.triples((None, RDF.type, onto.ConsultaPlanes)):
            es_consulta_planes = True
            break
        
        logger.info(f"[DEPURACIÓN] Tipo de mensaje: registro_plan={es_registro_plan}, consulta_planes={es_consulta_planes}")
            
        # Procesar según el tipo de mensaje
        if es_registro_plan:
            # Procesamiento para registro de plan
            for s, p, o in gm.triples((None, RDF.type, onto.RegistroPlan)):
                plan_uri = None
                # Extraer URI del plan a registrar
                for s1, p1, o1 in gm.triples((s, onto.planARegistrar, None)):
                    plan_uri = o1
                    
                if plan_uri:
                    # Copiar todo el grafo del plan a nuestra base de datos
                    triples_count = 0
                    for s1, p1, o1 in gm.triples((plan_uri, None, None)):
                        planes_db.add((s1, p1, o1))
                        triples_count += 1
                        
                    # Copiar también todas las propiedades de los componentes del plan
                    for s1, p1, o1 in gm.triples((None, None, None)):
                        if s1 != s and s1 != content:  # No copiar la petición en sí
                            planes_db.add((s1, p1, o1))
                    
                    # Añadir estado "activo" al plan
                    planes_db.remove((plan_uri, onto.estado, None))  # Eliminar cualquier estado previo
                    planes_db.add((plan_uri, onto.estado, Literal("activo")))
                    
                    # Añadir timestamp
                    planes_db.add((plan_uri, onto.timestamp, Literal(datetime.datetime.now().isoformat(), datatype=XSD.dateTime)))
                    
                    # Guardar en el archivo para persistencia
                    planes_db.serialize(DB_FILE, format="xml")
                    
                    logger.info(f"Plan registrado: {plan_uri} con {triples_count} triples")
                    
                    # Responder confirmación
                    g = Graph()
                    g.bind('rdf', RDF)
                    g.bind('onto', onto)
                    
                    respuesta_id = URIRef(f'confirmacion_{str(uuid.uuid4())}')
                    g.add((respuesta_id, RDF.type, onto.ConfirmacionRegistro))
                    g.add((respuesta_id, onto.planRegistrado, plan_uri))
                    g.add((respuesta_id, RDFS.comment, Literal(f"Plan registrado correctamente")))
                    
                    mss_cnt += 1
                    return Response(build_message(g, ACL.inform,
                                   sender=AgenteMantenedorPlanes.uri,
                                   receiver=msgdic['sender'],
                                   content=respuesta_id,
                                   msgcnt=mss_cnt).serialize(format='xml'),
                                   mimetype='text/xml')
        
        elif es_consulta_planes:
            # Procesamiento para consulta de planes
            for s, p, o in gm.triples((None, RDF.type, onto.ConsultaPlanes)):
                # Construir respuesta con todos los planes activos
                g = Graph()
                g.bind('rdf', RDF)
                g.bind('onto', onto)
                
                respuesta_id = URIRef(f'respuesta_planes_{str(uuid.uuid4())}')
                g.add((respuesta_id, RDF.type, onto.RespuestaConsultaPlanes))
                
                # Extraer planes activos
                planes_encontrados = 0
                for plan_uri, _, _ in planes_db.triples((None, RDF.type, onto.Plan)):
                    # Verificar si está activo
                    estado = planes_db.value(subject=plan_uri, predicate=onto.estado)
                    if estado and str(estado) == "activo":
                        g.add((respuesta_id, onto.contienePlan, plan_uri))
                        planes_encontrados += 1
                        
                        # Copiar todos los datos del plan a la respuesta
                        for s1, p1, o1 in planes_db.triples((plan_uri, None, None)):
                            g.add((s1, p1, o1))
                
                logger.info(f"Consultados {planes_encontrados} planes activos")
                
                mss_cnt += 1
                return Response(build_message(g, ACL.inform,
                               sender=AgenteMantenedorPlanes.uri,
                               receiver=msgdic['sender'],
                               content=respuesta_id,
                               msgcnt=mss_cnt).serialize(format='xml'),
                               mimetype='text/xml')
    
    # Si no sabemos cómo manejar la petición
    logger.warning(f"Petición desconocida: tipo={msgdic.get('performative')}, es_registro={es_registro_plan if 'es_registro_plan' in locals() else 'No verificado'}, es_consulta={es_consulta_planes if 'es_consulta_planes' in locals() else 'No verificado'}")
    return Response(status=400)

@app.route("/Stop")
def stop():
    """
    Entrypoint que para el agente
    """
    tidyup()
    shutdown_server()
    return "Parando AgenteMantenedorPlanes"

def tidyup():
    """
    Acciones previas a parar el agente
    """
    global cola1
    # Guardar todos los planes antes de finalizar
    planes_db.serialize(DB_FILE, format="xml")
    logger.info(f"Base de datos guardada en {DB_FILE}")
    cola1.put(0)

def buscar_agente_clima():
    """
    Busca el agente de clima en el directorio, con mejor manejo de errores
    """
    logger.info("[VERIFICACIÓN] Buscando AgenteClima...")
    
    try:
        # Intentar buscar por tipo correcto
        agente = buscar_agente_por_tipo(DSO.WeatherAgent)
        if agente:
            logger.info(f"[VERIFICACIÓN] Encontrado AgenteClima en {agente['address']}")
            return agente
        
        # Intentar con tipo SolverAgent (alternativo)
        logger.info("[VERIFICACIÓN] Intentando con tipo SolverAgent...")
        agente = buscar_agente_por_tipo(DSO.SolverAgent)
        if agente:
            logger.info(f"[VERIFICACIÓN] Encontrado AgenteClima como SolverAgent en {agente['address']}")
            return agente
    except Exception as e:
        logger.warning(f"[VERIFICACIÓN] Error al buscar en directorio: {e}")
    
    # Configuración de respaldo si falla la búsqueda
    logger.warning("[VERIFICACIÓN] Usando configuración de respaldo para AgenteClima")
    
    # Probar diferentes puertos comunes para el AgenteClima
    puertos_clima = [9001, 9002, 9004, 9008]
    hostname = socket.gethostname()
    
    for puerto in puertos_clima:
        address = f'http://{hostname}:{puerto}/comm'
        try:
            # Prueba básica de conectividad
            logger.info(f"[VERIFICACIÓN] Probando conectividad con posible AgenteClima en {address}")
            response = requests.get(address, timeout=1)
            if response.status_code == 200:
                logger.info(f"[VERIFICACIÓN] ¡Conectividad exitosa con {address}!")
                return {
                    'name': 'AgenteClima',
                    'uri': 'http://www.agentes.org#AgenteClima',
                    'address': address
                }
        except:
            continue
    
    # Si todo falla, retornar configuración por defecto
    return {
        'name': 'AgenteClima',
        'uri': 'http://www.agentes.org#AgenteClima',
        'address': f'http://{hostname}:9001/comm'
    }

def limpiar_planes_finalizados():
    """
    Elimina de la base de datos los planes cuya fecha de fin ya ha pasado
    y solicita valoraciones para estos planes al AgenteValoraciones
    """
    global planes_db
    
    fecha_actual = datetime.datetime.now().date()
    logger.info(f"Limpiando planes finalizados. Fecha actual: {fecha_actual}")
    
    planes_procesados = 0
    
    # Buscar todos los planes activos
    for plan_uri, _, _ in planes_db.triples((None, RDF.type, onto.Plan)):
        # Verificar si está activo
        estado = planes_db.value(subject=plan_uri, predicate=onto.estado)
        if estado and str(estado) == "activo":
            # Obtener fecha de fin
            fecha_fin_str = planes_db.value(subject=plan_uri, predicate=onto.fecha_fin)
            
            if fecha_fin_str:
                try:
                    # Convertir a objeto date
                    fecha_fin = datetime.datetime.fromisoformat(str(fecha_fin_str)).date()
                    
                    # Si la fecha de fin es anterior a la fecha actual, cambiar estado a finalizado
                    if fecha_fin < fecha_actual:
                        logger.info(f"Plan finalizado: {plan_uri}, fecha fin: {fecha_fin}")
                        planes_procesados += 1
                        
                        # Buscar usuario asociado al plan (si existe)
                        usuario = planes_db.value(subject=plan_uri, predicate=onto.esRealizadoPor)
                        if not usuario:
                            # Si no hay usuario asignado, usamos uno genérico para pruebas
                            usuario = URIRef("http://www.semanticweb.org/usuario/default")
                            logger.warning(f"[VALORACIÓN] Plan {plan_uri} no tiene usuario asignado, usando valor por defecto")
                        
                        # Cambiar estado a "finalizado"
                        planes_db.remove((plan_uri, onto.estado, None))
                        planes_db.add((plan_uri, onto.estado, Literal("finalizado")))
                        
                        # Solicitar valoración al AgenteValoraciones
                        logger.info(f"[VALORACIÓN] Solicitando valoración para plan {plan_uri}")
                        solicitar_valoracion(plan_uri, usuario)
                        
                except Exception as e:
                    logger.error(f"Error al procesar fecha de fin {fecha_fin_str}: {e}")
    
    if planes_procesados > 0:
        # Guardar cambios en el archivo
        planes_db.serialize(DB_FILE, format="xml")
        logger.info(f"Se procesaron {planes_procesados} planes finalizados")


def buscar_agente_actividades():
    """
    Busca el agente de actividades en el directorio
    """
    agente = buscar_agente_por_tipo(DSO.ActivitiesAgent)
    if not agente:
        # Configuración de respaldo si no se encuentra en el directorio
        agente = {
            'name': 'AgenteActividades',
            'uri': 'http://www.agentes.org#AgenteActividades',
            'address': f'http://{socket.gethostname()}:9003/comm'
        }
        logger.info(f"Usando configuración de respaldo para AgenteActividades: {agente['address']}")
    return agente

def buscar_agente_por_tipo(tipo_agente):
    """
    Busca un agente por su tipo en el directorio
    
    :param tipo_agente: Tipo del agente a buscar (DSO.TransportAgent, DSO.HotelsAgent, etc)
    :return: Información del agente o None si no se encuentra
    """
    global mss_cnt

    tipo_str = str(tipo_agente).split('#')[-1]
    logger.info(f"Buscando agente de tipo {tipo_str} en el directorio...")
    
    # Crear grafo para buscar en el directorio
    gmess = Graph()
    gmess.bind('dso', DSO)
    gmess.bind('rdf', RDF)
    
    search_obj = agn[f'search-{str(uuid.uuid4())}']
    gmess.add((search_obj, RDF.type, DSO.Search))
    gmess.add((search_obj, DSO.AgentType, tipo_agente))
    
    # Construir el mensaje
    msg = build_message(gmess, ACL.request,
                       sender=AgenteMantenedorPlanes.uri,
                       receiver=DirectoryAgent.uri,
                       content=search_obj,
                       msgcnt=mss_cnt)
    mss_cnt += 1
    
    # Mejor manejo de errores
    try:
        # Enviar el mensaje
        gr = send_message(msg, DirectoryAgent.address)
        
        # Verificación básica de la respuesta
        if not isinstance(gr, Graph):
            logger.error(f"La respuesta del directorio no es un grafo válido: {gr}")
            return None
        
        # Procesar la respuesta
        msg = gr.value(predicate=RDF.type, object=ACL.FipaAclMessage)
        if not msg:
            logger.error("No se encontró un mensaje FIPA ACL en la respuesta")
            return None
            
        content = gr.value(subject=msg, predicate=ACL.content)
        if not content:
            logger.error("No se encontró el contenido del mensaje")
            return None
        
        # Buscar todos los agentes en la respuesta
        agentes_encontrados = []
        
        # Método 1: Buscar por AgentType
        for s, p, o in gr.triples((None, DSO.AgentType, tipo_agente)):
            uri = gr.value(subject=s, predicate=DSO.Uri)
            name = gr.value(subject=s, predicate=FOAF.name)
            address = gr.value(subject=s, predicate=DSO.Address)
            
            if uri and address:
                agentes_encontrados.append({
                    'name': str(name) if name else tipo_str,
                    'uri': uri,
                    'address': address
                })
        
        # Método 2: Buscar en el contenido
        if not agentes_encontrados:
            for s, p, o in gr.triples((content, DSO.Address, None)):
                uri = gr.value(subject=s, predicate=DSO.Uri)
                name = gr.value(subject=s, predicate=FOAF.name)
                
                if uri:
                    agentes_encontrados.append({
                        'name': str(name) if name else tipo_str,
                        'uri': uri,
                        'address': o
                    })
        
        if agentes_encontrados:
            agente = agentes_encontrados[0]
            logger.info(f"Encontrado agente {agente['name']} en {agente['address']}")
            return agente
        else:
            logger.warning(f"No se encontró ningún agente de tipo {tipo_str}")
            return None
            
    except Exception as e:
        logger.error(f"Error al buscar agente: {e}")
        logger.error(traceback.format_exc())
        return None

def consultar_clima(ciudad, fecha):
    """
    Consulta el clima para una ciudad y fecha específicas, con mejor manejo de errores
    """
    global mss_cnt
    
    logger.info(f"[CLIMA] Consultando clima para {ciudad} en fecha {fecha}")
    
    # Si no hay agente de clima o falla la conexión, simulamos un clima neutro
    try:
        # Buscar el agente de clima (con retries)
        max_intentos = 2
        for intento in range(max_intentos):
            agente_clima = buscar_agente_clima()
            if agente_clima:
                # Crear petición de clima
                g = Graph()
                g.bind('rdf', RDF)
                g.bind('onto', onto)
                g.bind('xsd', XSD)
                
                peticion_id = URIRef('peticion_clima_' + str(uuid.uuid4()))
                g.add((peticion_id, RDF.type, onto.PeticionClima))
                
                # Crear nodo para la ciudad
                ciudad_id = URIRef('ciudad_' + str(uuid.uuid4()))
                g.add((ciudad_id, onto.NombreCiudad, Literal(ciudad)))
                g.add((peticion_id, onto.comoRestriccionLocalidad, ciudad_id))
                
                # Añadir días a consultar (solo 1 para la fecha específica)
                g.add((peticion_id, onto.duranteUnTiempo, Literal(1)))
                
                # Construir mensaje ACL
                msg = build_message(g, ACL.request,
                                  sender=AgenteMantenedorPlanes.uri,
                                  receiver=URIRef(agente_clima['uri']),
                                  content=peticion_id,
                                  msgcnt=mss_cnt)
                mss_cnt += 1
                
                # Enviar la petición
                try:
                    response = requests.get(agente_clima['address'], 
                                         params={'content': msg.serialize(format='xml')},
                                         timeout=3)
                    
                    if response.status_code == 200:
                        logger.info(f"Respuesta del clima recibida para {ciudad}, fecha {fecha}")
                        g_resp = Graph()
                        g_resp.parse(data=response.text, format='xml')
                        
                        # Extraer información relevante
                        clima_info = {}
                        
                        # Buscar la respuesta de clima
                        for s, p, o in g_resp.triples((None, RDF.type, onto.RespuestaClima)):
                            respuesta_id = s
                            
                            # Buscar previsiones
                            for s1, p1, o1 in g_resp.triples((respuesta_id, onto.previsiones, None)):
                                prevision_id = o1
                                
                                # Extraer fecha de la previsión
                                fecha_prevision = None
                                for s2, p2, o2 in g_resp.triples((prevision_id, onto.fecha, None)):
                                    fecha_prevision = str(o2)
                                
                                # Si es la fecha que buscamos
                                if fecha_prevision and fecha_prevision == fecha:
                                    # Extraer si hay temporal perjudicial
                                    for s2, p2, o2 in g_resp.triples((prevision_id, onto.TemporalPerjudicial, None)):
                                        clima_info['temporal_perjudicial'] = str(o2).lower() == "true"
                                    
                                    # Extraer temperatura
                                    for s2, p2, o2 in g_resp.triples((prevision_id, onto.temperatura, None)):
                                        clima_info['temperatura'] = float(o2)
                                    
                                    # Extraer descripción
                                    for s2, p2, o2 in g_resp.triples((prevision_id, RDFS.comment, None)):
                                        clima_info['descripcion'] = str(o2)
                                    
                                    return clima_info
                
                    else:
                        logger.warning(f"[CLIMA] Error en respuesta: {response.status_code}")
                        continue
                except Exception as e:
                    logger.warning(f"[CLIMA] Error al contactar agente clima: {e}")
                    continue
        
        # Si llegamos aquí, no se pudo consultar el clima
        logger.warning(f"[CLIMA] No se pudo obtener clima para {ciudad} en {fecha}")
        
        # Devolver un clima "neutro" por defecto
        return {
            'temporal_perjudicial': False,
            'temperatura': 20.0,
            'descripcion': "Información no disponible - usando clima predeterminado"
        }
        
    except Exception as e:
        logger.error(f"[CLIMA] Error general al consultar clima: {e}")
        return {
            'temporal_perjudicial': False,
            'temperatura': 20.0,
            'descripcion': f"Error en consulta: {str(e)}"
        }

def buscar_actividad_interior(ciudad, fecha, franja):
    """
    Busca una actividad de interior para sustituir una actividad exterior
    
    :param ciudad: Nombre de la ciudad
    :param fecha: Fecha en formato YYYY-MM-DD
    :param franja: Franja horaria (mañana, tarde, noche)
    :return: URI de la actividad o None si no se encuentra
    """
    global mss_cnt
    
    # Buscar el agente de actividades
    agente_actividades = buscar_agente_actividades()
    if not agente_actividades:
        logger.error("No se pudo encontrar el AgenteActividades")
        return None
    
    # Crear petición de actividad interior
    g = Graph()
    g.bind('rdf', RDF)
    g.bind('onto', onto)
    g.bind('xsd', XSD)
    
    peticion_id = URIRef('peticion_actividad_' + str(uuid.uuid4()))
    g.add((peticion_id, RDF.type, onto.PeticionActividad))
    
    # Crear nodo para la ciudad
    ciudad_id = URIRef('ciudad_' + str(uuid.uuid4()))
    g.add((ciudad_id, onto.NombreCiudad, Literal(ciudad)))
    g.add((peticion_id, onto.comoRestriccionLocalidad, ciudad_id))
    
    # Añadir fecha y franja horaria
    g.add((peticion_id, onto.fecha, Literal(fecha, datatype=XSD.date)))
    g.add((peticion_id, onto.franjaHoraria, Literal(franja)))
    
    # Especificar que queremos actividades de interior
    g.add((peticion_id, onto.tipoActividad, onto.Interior))
    
    # Construir mensaje ACL
    msg = build_message(g, ACL.request,
                      sender=AgenteMantenedorPlanes.uri,
                      receiver=URIRef(agente_actividades['uri']),
                      content=peticion_id,
                      msgcnt=mss_cnt)
    mss_cnt += 1
    
    # Enviar la petición
    try:
        response = requests.get(agente_actividades['address'], params={'content': msg.serialize(format='xml')})
        
        if response.status_code == 200:
            logger.info(f"Respuesta de actividades recibida para {ciudad}, fecha {fecha}, franja {franja}")
            g_resp = Graph()
            g_resp.parse(data=response.text, format='xml')
            
            # Buscar la primera actividad de interior
            for s, p, o in g_resp.triples((None, RDF.type, onto.RespuestaActividad)):
                respuesta_id = s
                
                # Buscar actividades
                for s1, p1, o1 in g_resp.triples((respuesta_id, onto.formadoPorActividades, None)):
                    actividad_id = o1
                    
                    # Verificar si es de interior
                    es_interior = False
                    for s2, p2, o2 in g_resp.triples((actividad_id, RDF.type, onto.Interior)):
                        es_interior = True
                        break
                    
                    if es_interior:
                        # Copiar todos los detalles de la actividad a nuestra base de datos
                        for s2, p2, o2 in g_resp.triples((actividad_id, None, None)):
                            planes_db.add((s2, p2, o2))
                        
                        return actividad_id
            
            # Si no encontramos ninguna actividad de interior
            return None
        else:
            logger.error(f"Error en la respuesta de actividades: {response.status_code}")
            return None
    except Exception as e:
        logger.error(f"Error al buscar actividad de interior: {e}")
        return None

def verificar_actividades_exteriores():
    """
    Verifica todas las actividades exteriores de los planes activos y las modifica si es necesario
    """
    logger.info("[VERIFICACIÓN] Iniciando verificación de actividades exteriores en planes activos")
    planes_procesados = 0
    planes_modificados = 0
    
    # Buscar todos los planes activos
    for plan_uri, _, _ in planes_db.triples((None, RDF.type, onto.Plan)):
        # Verificar si está activo
        estado = planes_db.value(subject=plan_uri, predicate=onto.estado)
        if estado and str(estado) == "activo":
            logger.info(f"[VERIFICACIÓN] Procesando plan activo: {plan_uri}")
            planes_procesados += 1
            
            # Obtener ciudad de destino
            ciudad = None
            for s, p, o in planes_db.triples((plan_uri, onto.llegaA, None)):
                ciudad_uri = o
                ciudad = planes_db.value(subject=ciudad_uri, predicate=onto.NombreCiudad)
            
            logger.debug(f"[VERIFICACIÓN] Ciudad de destino: {ciudad}")
            
            if not ciudad:
                logger.warning(f"[VERIFICACIÓN] Plan {plan_uri} no tiene ciudad de destino")
                continue
            
            # Obtener fechas del plan
            fecha_inicio = None
            fecha_fin = None
            
            for s, p, o in planes_db.triples((plan_uri, onto.fecha_inicio, None)):
                fecha_inicio = str(o)
            
            for s, p, o in planes_db.triples((plan_uri, onto.fecha_fin, None)):
                fecha_fin = str(o)
            
            logger.debug(f"[VERIFICACIÓN] Fechas del plan: {fecha_inicio} a {fecha_fin}")
            
            if not fecha_inicio or not fecha_fin:
                logger.warning(f"[VERIFICACIÓN] Plan {plan_uri} no tiene fechas definidas")
                continue
            
            # Calcular días del plan
            try:
                fecha_inicio_dt = datetime.datetime.fromisoformat(fecha_inicio)
                fecha_fin_dt = datetime.datetime.fromisoformat(fecha_fin)
                logger.debug(f"[VERIFICACIÓN] Periodo del plan: {(fecha_fin_dt - fecha_inicio_dt).days + 1} días")
            except ValueError as e:
                logger.error(f"[VERIFICACIÓN] Error al procesar fechas del plan {plan_uri}: {e}")
                continue
            
            # Para cada día del plan
            fecha_actual = fecha_inicio_dt
            dias_procesados = 0
            while fecha_actual <= fecha_fin_dt:
                fecha_str = fecha_actual.strftime('%Y-%m-%d')
                dias_procesados += 1
                logger.debug(f"[VERIFICACIÓN] Procesando día {dias_procesados}: {fecha_str}")
                
                # Consultar clima para este día
                logger.info(f"[VERIFICACIÓN] Consultando clima para {ciudad} en fecha {fecha_str}")
                clima_info = consultar_clima(ciudad, fecha_str)
                
                if clima_info:
                    logger.debug(f"[VERIFICACIÓN] Información del clima: {clima_info}")
                    
                    # Si hay mal tiempo
                    if clima_info.get('temporal_perjudicial'):
                        logger.warning(f"[VERIFICACIÓN] ¡ALERTA! Detectado mal tiempo en {ciudad} para {fecha_str}: {clima_info.get('descripcion')}")
                        
                        # Buscar actividades exteriores para este día
                        logger.info(f"[VERIFICACIÓN] Buscando actividades exteriores para el día {fecha_str}")
                        actividades_encontradas = 0
                        actividades_modificadas = 0
                        
                        for dia_id, _, _ in planes_db.triples((None, RDF.type, onto.PlanDe1Dia)):
                            # Verificar si este día pertenece al plan
                            if (plan_uri, onto.estaCompuestoPor, dia_id) in planes_db:
                                # Verificar si la fecha coincide
                                dia_fecha = planes_db.value(subject=dia_id, predicate=RDFS.label)
                                if dia_fecha and fecha_str in str(dia_fecha):
                                    logger.debug(f"[VERIFICACIÓN] Encontrado día {dia_id} con fecha {dia_fecha}")
                                    
                                    # Buscar franjas horarias de este día
                                    franjas_procesadas = 0
                                    for franja_id in planes_db.objects(subject=dia_id, predicate=onto.incluyeFranja):
                                        franjas_procesadas += 1
                                        
                                        # Obtener franja horaria
                                        franja = planes_db.value(subject=franja_id, predicate=RDFS.label)
                                        if not franja:
                                            logger.warning(f"[VERIFICACIÓN] Franja {franja_id} sin etiqueta en día {dia_id}")
                                            continue
                                        
                                        logger.debug(f"[VERIFICACIÓN] Procesando franja {franja_id}: {franja}")
                                        
                                        # Buscar actividades en esta franja
                                        for actividad_id in planes_db.objects(subject=franja_id, predicate=onto.seRealizan):
                                            actividades_encontradas += 1
                                            
                                            # Verificar si es una actividad exterior
                                            es_exterior = False
                                            for s, p, o in planes_db.triples((actividad_id, RDF.type, onto.Exterior)):
                                                es_exterior = True
                                                break
                                            
                                            # Obtener nombre de la actividad para logs
                                            nombre_actividad = planes_db.value(subject=actividad_id, predicate=RDFS.label)
                                            nombre_actividad = str(nombre_actividad) if nombre_actividad else str(actividad_id)
                                            
                                            if es_exterior:
                                                logger.warning(f"[VERIFICACIÓN] Encontrada actividad exterior: {nombre_actividad} en día {fecha_str}, franja {franja}")
                                                
                                                # Buscar una actividad de interior para sustituirla
                                                logger.info(f"[VERIFICACIÓN] Buscando actividad interior para sustituir a {nombre_actividad}")
                                                nueva_actividad_id = buscar_actividad_interior(ciudad, fecha_str, str(franja))
                                                
                                                if nueva_actividad_id:
                                                    # Obtener nombre de la nueva actividad
                                                    nuevo_nombre = planes_db.value(subject=nueva_actividad_id, predicate=RDFS.label)
                                                    nuevo_nombre = str(nuevo_nombre) if nuevo_nombre else str(nueva_actividad_id)
                                                    
                                                    logger.info(f"[VERIFICACIÓN] ✓ Reemplazando actividad exterior '{nombre_actividad}' por actividad interior '{nuevo_nombre}'")
                                                    
                                                    # Eliminar la actividad exterior
                                                    planes_db.remove((franja_id, onto.seRealizan, actividad_id))
                                                    
                                                    # Añadir la nueva actividad
                                                    planes_db.add((franja_id, onto.seRealizan, nueva_actividad_id))
                                                    
                                                    # Guardar cambios
                                                    planes_db.serialize(DB_FILE, format="xml")
                                                    actividades_modificadas += 1
                                                    planes_modificados += 1
                                                else:
                                                    logger.warning(f"[VERIFICACIÓN] ✗ No se encontró actividad interior para sustituir a '{nombre_actividad}'")
                                    
                                    if franjas_procesadas == 0:
                                        logger.warning(f"[VERIFICACIÓN] Día {dia_id} no tiene franjas horarias definidas")
                        
                        logger.info(f"[VERIFICACIÓN] Día {fecha_str}: {actividades_encontradas} actividades procesadas, {actividades_modificadas} modificadas")
                else:
                    logger.warning(f"[VERIFICACIÓN] No se pudo obtener información del clima para {ciudad} en fecha {fecha_str}")
                
                # Avanzar al siguiente día
                fecha_actual += datetime.timedelta(days=1)
            
            logger.info(f"[VERIFICACIÓN] Plan {plan_uri}: procesados {dias_procesados} días")
    
    logger.info(f"[VERIFICACIÓN] Completada: {planes_procesados} planes procesados, {planes_modificados} planes modificados")

def agentbehavior1(cola):
    """
    Comportamiento del agente - Registrarse en el directorio y verificar planes
    """
    global mss_cnt
    
    # Registrar el agente en el servicio de directorio
    gmess = Graph()
    gmess.bind('foaf', FOAF)
    gmess.bind('dso', DSO)
    reg_obj = agn[AgenteMantenedorPlanes.name + '-Register']
    gmess.add((reg_obj, RDF.type, DSO.Register))
    gmess.add((reg_obj, DSO.Uri, AgenteMantenedorPlanes.uri))
    gmess.add((reg_obj, FOAF.name, Literal(AgenteMantenedorPlanes.name)))
    gmess.add((reg_obj, DSO.Address, Literal(AgenteMantenedorPlanes.address)))
    gmess.add((reg_obj, DSO.AgentType, DSO.PlanAgent))

    # Lo metemos en el registro de servicios
    try:
        send_message(
            build_message(gmess, ACL.request,
                        sender=AgenteMantenedorPlanes.uri,
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
    
    # Bucle principal del comportamiento
    while True:
        try:
            # Verificar si hay un mensaje en la cola
            try:
                msg = cola.get_nowait()
                if msg == 0:
                    logger.info("Finalizando comportamiento del agente")
                    break
            except:
                pass  # No hay mensajes, continuar
            
            hora_actual = datetime.datetime.now().hour
            if hora_actual in [8, 20]:  # Ejecutar a las 8am y 8pm
                limpiar_planes_finalizados()

            # Verificar actividades exteriores en planes activos
            verificar_actividades_exteriores()
            
            # Esperar antes de la siguiente verificación (cada 1 hora)
            time.sleep(3600)
            
        except Exception as e:
            logger.error(f"Error en el comportamiento del agente: {e}")
            time.sleep(300)  # Esperar 5 minutos antes de reintentar

@app.route("/planes")
def listar_planes():
    """
    Muestra un listado de todos los planes activos
    """
    planes_activos = []
    
    # Extraer todos los planes activos
    for plan_uri, _, _ in planes_db.triples((None, RDF.type, onto.Plan)):
        # Verificar si está activo
        estado = planes_db.value(subject=plan_uri, predicate=onto.estado)
        if estado and str(estado) == "activo":
            plan = {
                'uri': plan_uri,
                'destino': 'Desconocido',
                'fecha_inicio': 'Desconocida',
                'fecha_fin': 'Desconocida'
            }
            
            # Obtener ciudad de destino
            for s, p, o in planes_db.triples((plan_uri, onto.llegaA, None)):
                ciudad_uri = o
                ciudad = planes_db.value(subject=ciudad_uri, predicate=onto.NombreCiudad)
                if ciudad:
                    plan['destino'] = str(ciudad)
            
            # Obtener fechas
            for s, p, o in planes_db.triples((plan_uri, onto.fecha_inicio, None)):
                plan['fecha_inicio'] = str(o)
            
            for s, p, o in planes_db.triples((plan_uri, onto.fecha_fin, None)):
                plan['fecha_fin'] = str(o)
            
            planes_activos.append(plan)
    
    # Construir HTML de respuesta
    html = """
    <html>
        <head>
            <title>Planes Activos</title>
            <style>
                body { font-family: Arial, sans-serif; margin: 20px; }
                h1 { color: #2c3e50; }
                table { border-collapse: collapse; width: 100%; margin-top: 20px; }
                th, td { border: 1px solid #ddd; padding: 8px; text-align: left; }
                th { background-color: #f2f2f2; }
                tr:nth-child(even) { background-color: #f9f9f9; }
                .btn { display: inline-block; padding: 10px 15px; background: #3498db; color: white; 
                      text-decoration: none; border-radius: 4px; margin-top: 20px; }
            </style>
        </head>
        <body>
            <h1>Planes Activos</h1>
            <p>Total de planes activos: <strong>""" + str(len(planes_activos)) + """</strong></p>
            
            <table>
                <tr>
                    <th>Destino</th>
                    <th>Fecha Inicio</th>
                    <th>Fecha Fin</th>
                    <th>URI</th>
                </tr>
    """
    
    for plan in planes_activos:
        html += f"""
                <tr>
                    <td>{plan['destino']}</td>
                    <td>{plan['fecha_inicio']}</td>
                    <td>{plan['fecha_fin']}</td>
                    <td>{plan['uri']}</td>
                </tr>
        """
    
    html += """
            </table>
            
            <a href="/verificar" class="btn">Verificar Planes Ahora</a>
        </body>
    </html>
    """
    
    return html

@app.route("/verificar")
def verificar_planes():
    """
    Fuerza una verificación inmediata de todos los planes activos
    """
    try:
        verificar_actividades_exteriores()
        return """
        <html>
            <head>
                <title>Verificación Completada</title>
                <style>
                    body { font-family: Arial, sans-serif; margin: 20px; }
                    h1 { color: #2c3e50; }
                    .success { color: #27ae60; }
                    .btn { display: inline-block; padding: 10px 15px; background: #3498db; 
                          color: white; text-decoration: none; border-radius: 4px; margin-top: 20px; }
                </style>
            </head>
            <body>
                <h1>Verificación de Planes</h1>
                <p class="success">La verificación de planes se ha completado correctamente.</p>
                <a href="/planes" class="btn">Volver a Planes Activos</a>
            </body>
        </html>
        """
    except Exception as e:
        return f"""
        <html>
            <head>
                <title>Error en Verificación</title>
                <style>
                    body {{ font-family: Arial, sans-serif; margin: 20px; }}
                    h1 {{ color: #2c3e50; }}
                    .error {{ color: #c0392b; }}
                    .btn {{ display: inline-block; padding: 10px 15px; background: #3498db; 
                          color: white; text-decoration: none; border-radius: 4px; margin-top: 20px; }}
                </style>
            </head>
            <body>
                <h1>Error en Verificación</h1>
                <p class="error">Ocurrió un error durante la verificación: {str(e)}</p>
                <a href="/planes" class="btn">Volver a Planes Activos</a>
            </body>
        </html>
        """

@app.route("/test_valoracion")
def test_valoracion():
    """
    Endpoint para pruebas: muestra planes y permite marcar como finalizados
    """
    global planes_db
    
    # List all plans with their status
    all_plans = []
    
    for plan_uri, _, _ in planes_db.triples((None, RDF.type, onto.Plan)):
        estado = planes_db.value(subject=plan_uri, predicate=onto.estado)
        
        # Get destination city if available
        destino = "Desconocido"
        ciudad_uri = planes_db.value(subject=plan_uri, predicate=onto.llegaA)
        if ciudad_uri:
            ciudad = planes_db.value(subject=ciudad_uri, predicate=onto.NombreCiudad)
            if ciudad:
                destino = str(ciudad)
        
        # If plan has no status, set it to "activo" (for testing)
        if not estado:
            logger.info(f"Plan {plan_uri} no tiene estado, asignando 'activo'")
            planes_db.add((plan_uri, onto.estado, Literal("activo")))
            estado = Literal("activo")
            planes_db.serialize(DB_FILE, format="xml")
        
        all_plans.append({
            'uri': plan_uri,
            'estado': str(estado),
            'destino': destino
        })
    
    # Process action if specified
    action = request.args.get('action')
    plan_id = request.args.get('plan_id')
    
    if action and plan_id:
        plan_uri = URIRef(plan_id)
        
        if action == 'finalize':
            # Mark as finalized and request rating
            planes_db.remove((plan_uri, onto.estado, None))
            planes_db.add((plan_uri, onto.estado, Literal("finalizado")))
            
            # Find user or use default
            usuario = planes_db.value(subject=plan_uri, predicate=onto.esRealizadoPor)
            if not usuario:
                usuario = URIRef("http://www.semanticweb.org/usuario/default")
            
            # Request rating from AgenteValoraciones
            solicitar_valoracion(plan_uri, usuario)
            planes_db.serialize(DB_FILE, format="xml")
            
            return f"""
            <html>
                <head>
                    <title>Plan Finalizado</title>
                    <style>
                        body {{ font-family: Arial, sans-serif; margin: 20px; }}
                        .success {{ color: green; }}
                        .btn {{ display: inline-block; padding: 10px 15px; background: #3498db; 
                              color: white; text-decoration: none; border-radius: 4px; margin-top: 20px; }}
                    </style>
                </head>
                <body>
                    <h1>Plan Finalizado</h1>
                    <p class="success">El plan {plan_uri} ha sido marcado como finalizado.</p>
                    <p>Se ha enviado solicitud de valoración al AgenteValoraciones.</p>
                    <a href="/test_valoracion" class="btn">Volver</a>
                    <a href="http://{socket.gethostname()}:9012/test" class="btn" style="background: #27ae60;">Ir a AgenteValoraciones</a>
                </body>
            </html>
            """
        elif action == 'activate':
            # Mark as active
            planes_db.remove((plan_uri, onto.estado, None))
            planes_db.add((plan_uri, onto.estado, Literal("activo")))
            planes_db.serialize(DB_FILE, format="xml")
            return """
            <html>
                <head>
                    <title>Plan Activado</title>
                    <style>
                        body { font-family: Arial, sans-serif; margin: 20px; }
                        .success { color: green; }
                        .btn { display: inline-block; padding: 10px 15px; background: #3498db; 
                              color: white; text-decoration: none; border-radius: 4px; margin-top: 20px; }
                    </style>
                </head>
                <body>
                    <h1>Plan Activado</h1>
                    <p class="success">El plan ha sido marcado como activo.</p>
                    <a href="/test_valoracion" class="btn">Volver</a>
                </body>
            </html>
            """
    
    # Generate HTML with all plans
    html = f"""
    <html>
        <head>
            <title>Gestión de Planes para Valoraciones</title>
            <style>
                body {{ font-family: Arial, sans-serif; margin: 20px; }}
                h1 {{ color: #2c3e50; }}
                table {{ border-collapse: collapse; width: 100%; margin: 20px 0; }}
                th, td {{ border: 1px solid #ddd; padding: 8px; }}
                th {{ background-color: #f2f2f2; }}
                tr:nth-child(even) {{ background-color: #f9f9f9; }}
                .btn {{ display: inline-block; padding: 5px 10px; text-decoration: none; color: white; 
                       border-radius: 3px; margin: 2px; }}
                .btn-finalizar {{ background-color: #e74c3c; }}
                .btn-activar {{ background-color: #27ae60; }}
                .estado-activo {{ color: green; }}
                .estado-finalizado {{ color: blue; }}
            </style>
        </head>
        <body>
            <h1>Gestión de Planes para Valoraciones</h1>
            
            <p>Total de planes: {len(all_plans)}</p>
            
            <table>
                <tr>
                    <th>Plan URI</th>
                    <th>Destino</th>
                    <th>Estado</th>
                    <th>Acciones</th>
                </tr>
    """
    
    for plan in all_plans:
        estado_class = "estado-activo" if plan['estado'] == "activo" else "estado-finalizado" if plan['estado'] == "finalizado" else ""
        
        html += f"""
                <tr>
                    <td>{plan['uri']}</td>
                    <td>{plan['destino']}</td>
                    <td class="{estado_class}">{plan['estado']}</td>
                    <td>
                """
                
        if plan['estado'] == "activo":
            html += f'<a href="/test_valoracion?action=finalize&plan_id={plan["uri"]}" class="btn btn-finalizar">Finalizar</a>'
        elif plan['estado'] == "finalizado":
            html += f'<a href="/test_valoracion?action=activate&plan_id={plan["uri"]}" class="btn btn-activar">Reactivar</a>'
        else:
            html += f'''
                <a href="/test_valoracion?action=activate&plan_id={plan["uri"]}" class="btn btn-activar">Activar</a>
                <a href="/test_valoracion?action=finalize&plan_id={plan["uri"]}" class="btn btn-finalizar">Finalizar</a>
            '''
            
        html += """
                    </td>
                </tr>
        """
    
    html += """
            </table>
            
            <a href="/planes" class="btn" style="background: #3498db; padding: 10px 15px; margin-top: 20px;">Volver a Planes</a>
        </body>
    </html>
    """
    
    return html

@app.route("/activar_plan", methods=['GET'])
def activar_plan():
    """
    Endpoint para activar el primer plan encontrado o crear uno nuevo de prueba
    """
    global planes_db
    
    # Buscar si hay algún plan (de cualquier estado)
    planes_encontrados = False
    for plan_uri, _, _ in planes_db.triples((None, RDF.type, onto.Plan)):
        planes_encontrados = True
        # Cambiar su estado a activo
        planes_db.remove((plan_uri, onto.estado, None))
        planes_db.add((plan_uri, onto.estado, Literal("activo")))
        
        # Asegurar que tenga fechas (necesario para la finalización posterior)
        fecha_inicio = planes_db.value(subject=plan_uri, predicate=onto.fecha_inicio)
        fecha_fin = planes_db.value(subject=plan_uri, predicate=onto.fecha_fin)
        
        if not fecha_inicio:
            # Agregar fecha de inicio (ayer)
            fecha_ayer = (datetime.datetime.now() - datetime.timedelta(days=1)).strftime('%Y-%m-%d')
            planes_db.add((plan_uri, onto.fecha_inicio, Literal(fecha_ayer)))
        
        if not fecha_fin:
            # Agregar fecha de fin (hoy - para que se pueda finalizar inmediatamente)
            fecha_hoy = datetime.datetime.now().strftime('%Y-%m-%d')
            planes_db.add((plan_uri, onto.fecha_fin, Literal(fecha_hoy)))
        
        # Guardar cambios
        planes_db.serialize(DB_FILE, format="xml")
        
        return f"""
        <html>
            <head>
                <title>Plan Activado</title>
                <style>
                    body {{ font-family: Arial, sans-serif; margin: 20px; }}
                    .success {{ color: green; }}
                    .btn {{ display: inline-block; padding: 10px 15px; background: #3498db; color: white; text-decoration: none; border-radius: 4px; margin-top: 20px; }}
                </style>
            </head>
            <body>
                <h1>Plan Activado</h1>
                <p class="success">El plan {plan_uri} ha sido marcado como activo y sus fechas actualizadas.</p>
                <p>Ahora puedes usar la función <strong>test_valoracion</strong> para finalizar este plan y probarlo con el sistema de valoraciones.</p>
                <a href="/test_valoracion" class="btn">Ir a Test Valoraciones</a>
            </body>
        </html>
        """
    
    # Si no hay planes, crear uno nuevo
    if not planes_encontrados:
        # Crear un plan de prueba
        plan_id = f'plan_test_{uuid.uuid4()}'
        plan_uri = URIRef(plan_id)
        
        # Datos básicos del plan
        planes_db.add((plan_uri, RDF.type, onto.Plan))
        planes_db.add((plan_uri, RDF.type, onto.PlanGeneral))
        planes_db.add((plan_uri, onto.estado, Literal("activo")))
        
        # Fechas (ayer al día siguiente)
        fecha_ayer = (datetime.datetime.now() - datetime.timedelta(days=1)).strftime('%Y-%m-%d')
        fecha_manana = (datetime.datetime.now() + datetime.timedelta(days=1)).strftime('%Y-%m-%d')
        planes_db.add((plan_uri, onto.fecha_inicio, Literal(fecha_ayer)))
        planes_db.add((plan_uri, onto.fecha_fin, Literal(fecha_manana)))
        
        # Ciudad destino
        ciudad_uri = URIRef(f'ciudad_{uuid.uuid4()}')
        planes_db.add((ciudad_uri, RDF.type, onto.Ciudad))
        planes_db.add((ciudad_uri, onto.NombreCiudad, Literal("Barcelona")))
        planes_db.add((plan_uri, onto.llegaA, ciudad_uri))
        
        # Usuario (para valoración)
        planes_db.add((plan_uri, onto.esRealizadoPor, URIRef("http://www.semanticweb.org/usuario/default")))
        
        # Guardar cambios
        planes_db.serialize(DB_FILE, format="xml")
        
        return f"""
        <html>
            <head>
                <title>Plan de Prueba Creado</title>
                <style>
                    body {{ font-family: Arial, sans-serif; margin: 20px; }}
                    .success {{ color: green; }}
                    .btn {{ display: inline-block; padding: 10px 15px; background: #3498db; color: white; text-decoration: none; border-radius: 4px; margin-top: 20px; }}
                </style>
            </head>
            <body>
                <h1>Plan de Prueba Creado</h1>
                <p class="success">Se ha creado un nuevo plan de prueba: {plan_uri}</p>
                <p>Este plan está marcado como activo y tiene fechas realistas para realizar pruebas.</p>
                <a href="/test_valoracion" class="btn">Ir a Test Valoraciones</a>
            </body>
        </html>
        """
def solicitar_valoracion(plan_uri, usuario_uri):
    """
    Solicita al AgenteValoraciones que gestione la valoración de un plan
    
    :param plan_uri: URI del plan a valorar
    :param usuario_uri: URI del usuario que debe valorar el plan
    """
    global mss_cnt
    
    # Buscar el agente de valoraciones
    agente_valoraciones = buscar_agente_valoraciones()
    if not agente_valoraciones:
        logger.error("[VALORACIÓN] No se pudo encontrar el AgenteValoraciones")
        return False
    
    # Crear grafo con la petición
    g = Graph()
    g.bind('rdf', RDF)
    g.bind('onto', onto)
    
    # Crear la petición de valoración
    peticion_id = URIRef('notificacion_plan_terminado_' + str(uuid.uuid4()))
    g.add((peticion_id, RDF.type, onto.NotificacionPlanTerminado))
    g.add((peticion_id, onto.planAValorar, plan_uri))
    g.add((peticion_id, onto.paraUsuario, usuario_uri))
    g.add((peticion_id, onto.fechaSolicitud, Literal(datetime.datetime.now().isoformat(), datatype=XSD.dateTime)))
    
    # Construir mensaje ACL
    msg = build_message(g, 
                      ACL.request,
                      sender=AgenteMantenedorPlanes.uri,
                      receiver=URIRef(agente_valoraciones['uri']),
                      content=peticion_id,
                      msgcnt=mss_cnt)
    mss_cnt += 1
    
    # Enviar la petición
    try:
        logger.info(f"[VALORACIÓN] Enviando solicitud de valoración a {agente_valoraciones['address']}")
        response = requests.get(agente_valoraciones['address'], params={'content': msg.serialize(format='xml')})
        
        if response.status_code == 200:
            logger.info("[VALORACIÓN] Respuesta recibida correctamente")
            return True
        else:
            logger.error(f"[VALORACIÓN] Error en la respuesta: {response.status_code}")
            return False
    except Exception as e:
        logger.error(f"[VALORACIÓN] Error al solicitar valoración: {e}")
        return False

def buscar_agente_valoraciones():
    """
    Busca el agente de valoraciones en el directorio
    """
    try:
        # Intenta buscar utilizando el tipo correcto
        agente = buscar_agente_por_tipo(DSO.ValoracionAgent)
        if agente:
            return agente
    except Exception as e:
        logger.warning(f"Error al buscar AgenteValoraciones: {e}")
    
    # Configuración de respaldo si no se encuentra en el directorio
    logger.info("Usando configuración de respaldo para AgenteValoraciones")
    agente = {
        'name': 'AgenteValoraciones',
        'uri': 'http://www.agentes.org#AgenteValoraciones',
        'address': f'http://{socket.gethostname()}:9012/comm'
    }
    
    # Verificar si AgenteValoraciones está activo en la dirección de respaldo
    try:
        # Intenta hacer una solicitud de prueba para verificar conectividad
        logger.info(f"Verificando conectividad con AgenteValoraciones en {agente['address']}")
        response = requests.get(agente['address'], timeout=1, params={'check': 'true'})
        if response.status_code == 200:
            logger.info(f"Conectividad con AgenteValoraciones confirmada")
        else:
            logger.warning(f"AgenteValoraciones respondió con código {response.status_code}")
    except Exception as e:
        logger.warning(f"Error al verificar conectividad con AgenteValoraciones: {e}")
    
    return agente

if __name__ == '__main__':
    try:
        # Iniciar el comportamiento de registro en el directorio y verificación
        ab1 = Process(target=agentbehavior1, args=(cola1,))
        ab1.start()
        
        # Informar sobre la configuración
        logger.info(f"AgenteMantenedorPlanes iniciándose en {hostname}:{port}")
        logger.info(f"Directorio en {dhostname}:{dport}")
        
        # Iniciar el servidor Flask
        app.run(host=hostname, port=port, debug=False)
        
        # Esperar a que termine el proceso
        ab1.join()
        
    except Exception as e:
        logger.error(f"Error al iniciar el agente: {e}")
        if 'ab1' in locals() and ab1.is_alive():
            ab1.terminate()
        logger.info('Agente terminado debido a un error')