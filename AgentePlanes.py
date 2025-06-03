# -*- coding: utf-8 -*-
"""
*** Agente de Planes ***

Este agente recibe peticiones de planes, solicita transportes al AgenteTransportes
y determina el mejor plan basado en las opciones disponibles.
Se comunica usando el protocolo FIPA ACL como en los ejemplos.

@author: Sergi
"""

from multiprocessing import Process, Queue
import socket
import argparse
import datetime
import uuid
import logging
import time
import random
import traceback
import requests
import concurrent.futures

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
    port = 9010  # Puerto para AgentePlanes
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
AgentePlanes = Agent('AgentePlanes',
                     agn.AgentePlanes,
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

# Cola para comunicación entre procesos
cola1 = Queue()

# Flask app
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

    # Si es una nueva petición de plan
    if msgdic['performative'] == ACL.request:
        # Buscar el contenido de la petición
        content = msgdic['content']
        
        # Buscar petición de plan
        for s, p, o in gm.triples((None, RDF.type, onto.PeticionPlan)):
            # Extraer información de la petición
            origen = None
            destino = None
            fecha_ida = None
            fecha_vuelta = None
            precio_max = None
            
            # Extraer origen
            for s1, p1, o1 in gm.triples((s, onto.comoOrigen, None)):
                for s2, p2, o2 in gm.triples((o1, onto.NombreCiudad, None)):
                    origen = str(o2)
            
            # Extraer destino
            for s1, p1, o1 in gm.triples((s, onto.comoDestino, None)):
                for s2, p2, o2 in gm.triples((o1, onto.NombreCiudad, None)):
                    destino = str(o2)
            
            # Extraer fechas
            for s1, p1, o1 in gm.triples((s, onto.fecha_inicio, None)):
                fecha_ida = str(o1)
            
            for s1, p1, o1 in gm.triples((s, onto.fecha_fin, None)):
                fecha_vuelta = str(o1)
            
            # Obtener precio máximo si existe
            for s1, p1, o1 in gm.triples((s, onto.PrecioMax, None)):
                precio_max = float(o1)
            
            if origen and destino and fecha_ida and fecha_vuelta:
                # En vez de procesar directamente, lo añadimos a la cola de problemas
                problema_id = str(uuid.uuid4())
                problemas_pendientes[problema_id] = {
                    'id': problema_id,
                    'origen': origen,
                    'destino': destino,
                    'fecha_ida': fecha_ida,
                    'fecha_vuelta': fecha_vuelta,
                    'precio_max': precio_max,
                    'content': content,
                    'sender': msgdic['sender'],
                    'timestamp': datetime.datetime.now()
                }
                
                # Responder que el problema fue aceptado
                g = Graph()
                g.bind('rdf', RDF)
                g.bind('onto', onto)
                
                respuesta_id = URIRef(f'aceptacion_{str(uuid.uuid4())}')
                g.add((respuesta_id, RDF.type, onto.AceptacionPeticion))
                g.add((respuesta_id, RDFS.comment, Literal(f"Problema aceptado con ID: {problema_id}")))
                g.add((respuesta_id, onto.EstadoPeticion, Literal("Pendiente")))
                
                mss_cnt += 1
                return Response(build_message(g, ACL.agree,
                               sender=AgentePlanes.uri,
                               receiver=msgdic['sender'],
                               content=respuesta_id,
                               msgcnt=mss_cnt).serialize(format='xml'),
                               mimetype='text/xml')
            else:
                logger.warning("Petición incompleta: faltan datos básicos")
                return Response(status=400)
    
    # Si es una respuesta a una petición de transportes que hicimos
    elif msgdic['performative'] == ACL.inform:
        # Verificar si es una respuesta del AgenteTransportes
        content = msgdic['content']
        for s, p, o in gm.triples((None, RDF.type, onto.RespuestaTransporte)):
            # Extraer el ID de petición original (si lo hubiera)
            peticion_original = None
            for s1, p1, o1 in gm.triples((s, onto.respuestaA, None)):
                peticion_original = o1
            
            # Procesar la respuesta y retornar el resultado
            respuesta = procesar_respuesta_transportes(gm, s, peticion_original)
            return Response(respuesta, mimetype='text/xml')
    
    # Si no sabemos cómo manejar la petición
    logger.warning(f"Petición desconocida: {msgdic.get('performative')}")
    return Response(status=400)


@app.route("/Stop")
def stop():
    """
    Entrypoint que para el agente
    """
    tidyup()
    shutdown_server()
    return "Parando Agente de Planes"


def tidyup():
    """
    Acciones previas a parar el agente
    """
    global cola1
    cola1.put(0)


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
                       sender=AgentePlanes.uri,
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
        
        logger.debug(f"Respuesta del directorio: {len(gr)} tripletas")
        
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
        import traceback
        logger.error(traceback.format_exc())
        return None


def buscar_agente_transportes():
    """
    Busca el agente de transportes en el directorio
    """
    agente = buscar_agente_por_tipo(DSO.TransportAgent)
    if not agente:
        # Configuración de respaldo si no se encuentra en el directorio
        agente = {
            'name': 'AgenteTransportes',
            'uri': 'http://www.agentes.org#AgenteTransportes',
            'address': f'http://{socket.gethostname()}:9004/comm'
        }
        logger.info(f"Usando configuración de respaldo para AgenteTransportes: {agente['address']}")
    return agente


def buscar_agente_alojamientos():
    """
    Busca el agente de alojamientos en el directorio
    """
    agente = buscar_agente_por_tipo(DSO.HotelsAgent)
    if not agente:
        # Configuración de respaldo si no se encuentra en el directorio
        agente = {
            'name': 'AgenteAlojamientos',
            'uri': 'http://www.agentes.org#AgenteAlojamientos',
            'address': f'http://{socket.gethostname()}:9002/comm'
        }
        logger.info(f"Usando configuración de respaldo para AgenteAlojamientos: {agente['address']}")
    return agente


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


def solicitar_transportes(origen, destino, fecha_ida, fecha_vuelta, precio_max):
    """
    Solicita opciones de transporte al AgenteTransportes
    
    :param origen: Nombre de la ciudad origen
    :param destino: Nombre de la ciudad destino
    :param fecha_ida: Fecha de ida
    :param fecha_vuelta: Fecha de vuelta
    :param precio_max: Precio máximo (opcional)
    :return: Grafo RDF con la respuesta o None si hay error
    """
    global mss_cnt
    
    # Buscar el agente de transportes en el directorio
    agente_transportes = buscar_agente_transportes()
    if not agente_transportes:
        logger.error("No se pudo encontrar el AgenteTransportes en el directorio")
        # Intentar usar una dirección hardcodeada como fallback
        agente_transportes = {
            'name': 'AgenteTransportes',
            'uri': 'http://www.agentes.org#AgenteTransportes',
            'address': f'http://{socket.gethostname()}:9004/comm'
        }
        logger.info(f"Usando dirección hardcodeada: {agente_transportes['address']}")
    
    # Crear el grafo con la petición
    g = Graph()
    g.bind('rdf', RDF)
    g.bind('onto', onto)
    g.bind('xsd', XSD)
    
    # Crear la petición de transporte
    peticion_id = URIRef('peticion_transporte_' + str(uuid.uuid4()))
    g.add((peticion_id, RDF.type, onto.PeticionTransporte))
    
    # Crear nodo para origen
    origen_id = URIRef('ciudad_origen_' + str(uuid.uuid4()))
    g.add((origen_id, onto.NombreCiudad, Literal(origen)))
    g.add((peticion_id, onto.comoOrigen, origen_id))
    
    # Crear nodo para destino
    destino_id = URIRef('ciudad_destino_' + str(uuid.uuid4()))
    g.add((destino_id, onto.NombreCiudad, Literal(destino)))
    g.add((peticion_id, onto.comoDestino, destino_id))
    
    # Fechas
    g.add((peticion_id, onto.fecha_inicio, Literal(fecha_ida, datatype=XSD.date)))
    g.add((peticion_id, onto.fecha_fin, Literal(fecha_vuelta, datatype=XSD.date)))
    
    # Precio máximo si se ha indicado
    if precio_max:
        g.add((peticion_id, onto.PrecioMax, Literal(precio_max, datatype=XSD.float)))
    
    # Construir mensaje ACL
    msg = build_message(g, 
                      ACL.request,
                      sender=AgentePlanes.uri,
                      receiver=URIRef(agente_transportes['uri']),
                      content=peticion_id,
                      msgcnt=mss_cnt)
    mss_cnt += 1
    
    # Mostrar el mensaje que vamos a enviar (para depuración)
    xml_msg = msg.serialize(format='xml')
    logger.debug(f"Mensaje a enviar: {xml_msg[:200]}...")
    
    # Enviar la petición
    logger.info(f"Enviando petición de transportes a {agente_transportes['name']} en {agente_transportes['address']}")
    try:
        # Usar requests directamente para más control y mejor manejo de errores
        import requests
        response = requests.get(agente_transportes['address'], params={'content': xml_msg})
        
        if response.status_code == 200:
            logger.info("Respuesta recibida correctamente")
            g_resp = Graph()
            g_resp.parse(data=response.text, format='xml')
            
            # Verificar si la respuesta tiene contenido útil
            tiene_transportes = False
            for s, p, o in g_resp.triples((None, onto.formadoPorTransportes, None)):
                tiene_transportes = True
                break
                
            if tiene_transportes:
                logger.info("La respuesta contiene transportes")
                return g_resp
            else:
                logger.warning("La respuesta NO contiene transportes")
                logger.warning(f"Respuesta: {response.text[:200]}...")
                return g_resp  # Devolver la respuesta de todas formas
        else:
            logger.error(f"Error en la respuesta: {response.status_code}")
            logger.error(response.text[:200])
            return None
    except Exception as e:
        logger.error(f"Error al solicitar transportes: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return None


def solicitar_alojamientos(ciudad, fecha_entrada, fecha_salida, precio_max=None, num_personas=2):
    """
    Solicita opciones de alojamiento al AgenteAlojamientos
    
    :param ciudad: Nombre de la ciudad
    :param fecha_entrada: Fecha de entrada
    :param fecha_salida: Fecha de salida
    :param precio_max: Precio máximo por noche (opcional)
    :param num_personas: Número de personas (opcional)
    :return: Grafo RDF con la respuesta o None si hay error
    """
    global mss_cnt
    
    # Buscar el agente de alojamientos en el directorio
    agente_alojamientos = buscar_agente_alojamientos()
    if not agente_alojamientos:
        return None
    
    # Crear el grafo con la petición
    g = Graph()
    g.bind('rdf', RDF)
    g.bind('onto', onto)
    g.bind('xsd', XSD)
    
    # Crear la petición de alojamiento
    peticion_id = URIRef('peticion_alojamiento_' + str(uuid.uuid4()))
    g.add((peticion_id, RDF.type, onto.PeticionAlojamiento))
    
    # Crear nodo para la ciudad
    ciudad_id = URIRef('ciudad_' + str(uuid.uuid4()))
    g.add((ciudad_id, onto.NombreCiudad, Literal(ciudad)))
    g.add((peticion_id, onto.comoRestriccionLocalidad, ciudad_id))
    
    # Fechas
    g.add((peticion_id, onto.fecha_inicio, Literal(fecha_entrada, datatype=XSD.date)))
    g.add((peticion_id, onto.fecha_fin, Literal(fecha_salida, datatype=XSD.date)))
    
    # Precio máximo si se ha indicado
    if precio_max:
        g.add((peticion_id, onto.PrecioMax, Literal(precio_max, datatype=XSD.float)))
    
    # Número de personas
    g.add((peticion_id, onto.NumPersonas, Literal(num_personas, datatype=XSD.integer)))
    
    # Construir mensaje ACL
    msg = build_message(g, 
                      ACL.request,
                      sender=AgentePlanes.uri,
                      receiver=URIRef(agente_alojamientos['uri']),
                      content=peticion_id,
                      msgcnt=mss_cnt)
    mss_cnt += 1
    
    # Enviar la petición
    logger.info(f"Enviando petición de alojamientos a {agente_alojamientos['address']}")
    try:
        response = requests.get(agente_alojamientos['address'], params={'content': msg.serialize(format='xml')})
        
        if response.status_code == 200:
            logger.info("Respuesta recibida correctamente")
            g_resp = Graph()
            g_resp.parse(data=response.text, format='xml')
            
            # Verificar si la respuesta tiene contenido útil
            tiene_alojamientos = False
            for s, p, o in g_resp.triples((None, RDF.type, onto.RespuestaAlojamiento)):
                tiene_alojamientos = True
                break
                
            if tiene_alojamientos:
                logger.info("La respuesta contiene alojamientos")
                return g_resp
            else:
                logger.warning("La respuesta NO contiene alojamientos")
                return g_resp  # Devolver la respuesta de todas formas
        else:
            logger.error(f"Error en la respuesta: {response.status_code}")
            logger.error(response.text[:200])
            return None
    except Exception as e:
        logger.error(f"Error al solicitar alojamientos: {e}")
        return None


def solicitar_actividades(ciudad, fecha_inicio, fecha_fin, preferencias=None, precio_max=None):
    """
    Solicita opciones de actividades al AgenteActividades para todo el periodo de estancia
    
    :param ciudad: Nombre de la ciudad
    :param fecha_inicio: Fecha de inicio de la estancia (formato YYYY-MM-DD)
    :param fecha_fin: Fecha de fin de la estancia (formato YYYY-MM-DD)
    :param preferencias: Lista de tipos de actividades preferidas ["Cultural", "Aventura", etc.]
    :param precio_max: Precio máximo total para actividades
    :return: Grafo RDF con la respuesta y diccionario estructurado
    """
    global mss_cnt
    
    # Calcular días de estancia
    try:
        fecha_inicio_dt = datetime.datetime.fromisoformat(fecha_inicio)
        fecha_fin_dt = datetime.datetime.fromisoformat(fecha_fin)
        dias_estancia = (fecha_fin_dt - fecha_inicio_dt).days
        if dias_estancia < 1:
            dias_estancia = 1
    except Exception as e:
        logger.error(f"Error calculando días de estancia: {e}")
        dias_estancia = 3  # Valor por defecto
    
    # Ajustar precio máximo por día si se proporciona
    precio_max_dia = None
    if precio_max:
        precio_max_dia = precio_max / dias_estancia
    
    # Buscar el agente de actividades en el directorio
    agente_actividades = buscar_agente_actividades()
    if not agente_actividades:
        logger.error("No se pudo encontrar el AgenteActividades")
        return None, None
    
    # Crear grafo para las actividades de todos los días
    g_completo = Graph()
    g_completo.bind('rdf', RDF)
    g_completo.bind('onto', onto)
    g_completo.bind('xsd', XSD)
    
    # Crear ID para la respuesta completa
    respuesta_completa_id = URIRef('respuesta_actividades_completa_' + str(uuid.uuid4()))
    g_completo.add((respuesta_completa_id, RDF.type, onto.RespuestaActividad))
    
    # Para cada día, solicitar actividades para cada franja horaria
    actividades_por_dia = {}
    fecha_actual = fecha_inicio_dt
    
    # Preparar tareas para ejecutar en paralelo
    tareas = []
    
    for dia in range(1, dias_estancia + 1):
        fecha_str = fecha_actual.strftime('%Y-%m-%d')
        actividades_por_dia[dia] = {'fecha': fecha_str, 'franjas': {}}
        
        # Para cada franja horaria (mañana, tarde, noche)
        for franja in ["mañana", "tarde", "noche"]:
            # Añadir tarea para esta combinación de día y franja
            tareas.append((dia, fecha_str, franja))
        
        # Avanzar al siguiente día
        fecha_actual += datetime.timedelta(days=1)
    
    # Ejecutar tareas en paralelo (máximo 10 hilos o el número de tareas si es menor)
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(10, len(tareas))) as executor:
        # Mapear cada tarea a la función correspondiente
        future_to_task = {
            executor.submit(solicitar_actividad_franja, ciudad, fecha_str, franja, precio_max_dia, agente_actividades): 
            (dia, franja) for dia, fecha_str, franja in tareas
        }
        
        # Procesar resultados
        for future in concurrent.futures.as_completed(future_to_task):
            dia, franja = future_to_task[future]
            try:
                actividades_franja, g_resp = future.result()
                
                # Guardar actividades en el diccionario
                actividades_por_dia[dia]['franjas'][franja] = actividades_franja
                
                # Añadir triples al grafo completo
                for s, p, o in g_resp.triples((None, None, None)):
                    g_completo.add((s, p, o))
                
            except Exception as e:
                logger.error(f"Error procesando resultado para día {dia}, franja {franja}: {e}")
                actividades_por_dia[dia]['franjas'][franja] = []
    
    # Estructurar días y franjas en el grafo RDF
    for dia, datos_dia in actividades_por_dia.items():
        dia_id = URIRef(f'dia_{dia}_{str(uuid.uuid4())}')
        g_completo.add((dia_id, RDF.type, onto.PlanDe1Dia))
        g_completo.add((dia_id, RDFS.label, Literal(f"Día {dia}: {datos_dia['fecha']}")))
        g_completo.add((respuesta_completa_id, onto.estaCompuestoPor, dia_id))
        
        for franja, actividades in datos_dia['franjas'].items():
            franja_id = URIRef(f'franja_{franja}_{str(uuid.uuid4())}')
            g_completo.add((franja_id, RDF.type, URIRef(f"{onto}FranjaHoraria")))
            g_completo.add((franja_id, RDFS.label, Literal(franja)))
            g_completo.add((dia_id, URIRef(f"{onto}incluyeFranja"), franja_id))
            
            for act in actividades:
                g_completo.add((franja_id, onto.seRealizan, act['uri']))
    
    return g_completo, actividades_por_dia



def solicitar_actividad_franja(ciudad, fecha_str, franja, precio_max_dia, agente_actividades):
    """
    Solicita actividades para una franja horaria específica
    
    :param ciudad: Nombre de la ciudad
    :param fecha_str: Fecha en formato YYYY-MM-DD
    :param franja: Franja horaria (mañana, tarde, noche)
    :param precio_max_dia: Precio máximo por día (se dividirá por 3 para la franja)
    :param agente_actividades: Información del agente de actividades
    :return: Tupla (actividades_franja, grafo_respuesta)
    """
    global mss_cnt
    
    # Crear grafo para la petición
    g_peticion = Graph()
    g_peticion.bind('rdf', RDF)
    g_peticion.bind('onto', onto)
    g_peticion.bind('xsd', XSD)
    
    # Crear la petición de actividad
    peticion_id = URIRef('peticion_actividad_' + str(uuid.uuid4()))
    g_peticion.add((peticion_id, RDF.type, onto.PeticionActividad))
    
    # Crear nodo para la ciudad
    ciudad_id = URIRef('ciudad_' + str(uuid.uuid4()))
    g_peticion.add((ciudad_id, onto.NombreCiudad, Literal(ciudad)))
    g_peticion.add((peticion_id, onto.comoRestriccionLocalidad, ciudad_id))

    # Añadir fecha y franja horaria
    g_peticion.add((peticion_id, onto.fecha, Literal(fecha_str, datatype=XSD.date)))
    g_peticion.add((peticion_id, onto.franjaHoraria, Literal(franja)))
    
    # Precio máximo para esta franja
    if precio_max_dia:
        g_peticion.add((peticion_id, onto.PrecioMax, Literal(precio_max_dia/3, datatype=XSD.float)))
    
    # Construir mensaje ACL
    msg = build_message(g_peticion, 
                      ACL.request,
                      sender=AgentePlanes.uri,
                      receiver=URIRef(agente_actividades['uri']),
                      content=peticion_id,
                      msgcnt=mss_cnt)
    mss_cnt += 1
    
    # Enviar la petición
    logger.info(f"Enviando petición de actividades para franja {franja} - {fecha_str} a {agente_actividades['address']}")
    try:
        response = requests.get(agente_actividades['address'], params={'content': msg.serialize(format='xml')})
        
        if response.status_code == 200:
            logger.info(f"Respuesta recibida para franja {franja} - {fecha_str}")
            g_resp = Graph()
            g_resp.parse(data=response.text, format='xml')
            
            # Verificar si la respuesta tiene contenido útil
            tiene_actividades = False
            for s, p, o in g_resp.triples((None, RDF.type, onto.RespuestaActividad)):
                tiene_actividades = True
                break
                
            if tiene_actividades:
                # Extraer las actividades
                actividades_franja = []
                for s, p, o in g_resp.triples((None, onto.formadoPorActividades, None)):
                    actividad_uri = o
                    
                    # Extraer datos básicos para nuestra organización interna
                    actividad_data = {
                        'uri': actividad_uri,
                        'nombre': 'Sin nombre',
                        'precio': 0,
                        'tipo': 'General',
                        'ubicacion': ciudad,
                        'descripcion': ''
                    }
                    
                    # Nombre/etiqueta
                    for s2, p2, o2 in g_resp.triples((actividad_uri, RDFS.label, None)):
                        actividad_data['nombre'] = str(o2)
                    
                    # Precio
                    for s2, p2, o2 in g_resp.triples((actividad_uri, onto.Precio, None)):
                        actividad_data['precio'] = float(o2)
                    
                    # Tipo de actividad
                    actividad_data['tipo'] = 'General'
                    for tipo in ['Aventura', 'Cultural', 'Exterior', 'Gastronomica', 'Interior', 'Naturaleza']:
                        tipo_uri = getattr(onto, tipo)
                        if (actividad_uri, RDF.type, tipo_uri) in g_resp:
                            actividad_data['tipo'] = tipo
                            break
                    
                    # Descripción
                    for s2, p2, o2 in g_resp.triples((actividad_uri, RDFS.comment, None)):
                        actividad_data['descripcion'] = str(o2)
                    
                    # Ubicación
                    actividad_data['ubicacion'] = ciudad
                    for s2, p2, o2 in g_resp.triples((actividad_uri, onto.Ubicacion, None)):
                        actividad_data['ubicacion'] = str(o2)
                    
                    # Añadir a la lista de esta franja
                    actividades_franja.append(actividad_data)
                
                logger.info(f"Encontradas {len(actividades_franja)} actividades para franja {franja} - {fecha_str}")
                return actividades_franja, g_resp
            else:
                logger.warning(f"No se encontraron actividades para franja {franja} - {fecha_str}")
                return [], g_resp
        else:
            logger.error(f"Error en la respuesta: {response.status_code}")
            return [], Graph()
    except Exception as e:
        logger.error(f"Error al solicitar actividades: {e}")
        return [], Graph()
    
def evaluar_transportes(grafo_transportes, content_uri, precio_max=None):
    """
    Evalúa los transportes recibidos del AgenteTransportes y selecciona los mejores
    usando una heurística mejorada
    """
    # Extraer todos los vuelos
    vuelos_ida = []
    vuelos_vuelta = []
    
    # Buscar la respuesta de transporte
    for s, p, o in grafo_transportes.triples((None, RDF.type, onto.RespuestaTransporte)):
        respuesta_uri = s
        
        # Recuperar todos los transportes
        for s1, p1, o1 in grafo_transportes.triples((s, onto.formadoPorTransportes, None)):
            transporte_uri = o1
            
            # Extraer detalles del transporte
            precio = None
            for s2, p2, o2 in grafo_transportes.triples((transporte_uri, onto.Precio, None)):
                precio = float(o2)
            
            salida = None
            for s2, p2, o2 in grafo_transportes.triples((transporte_uri, onto.Salida, None)):
                salida = o2
            
            llegada = None
            for s2, p2, o2 in grafo_transportes.triples((transporte_uri, onto.Llegada, None)):
                llegada = o2
            
            # Extraer más información para la heurística
            duracion_minutos = None
            for s2, p2, o2 in grafo_transportes.triples((transporte_uri, onto.DuracionMinutos, None)):
                duracion_minutos = int(o2)
            
            num_escalas = 0
            for s2, p2, o2 in grafo_transportes.triples((transporte_uri, onto.numEscalas, None)):
                num_escalas = int(o2)
            
            tiene_escalas = False
            for s2, p2, o2 in grafo_transportes.triples((transporte_uri, onto.tieneEscalas, None)):
                tiene_escalas = str(o2).lower() == "true"
            
            aerolinea = None
            for s2, p2, o2 in grafo_transportes.triples((transporte_uri, onto.operadoPor, None)):
                aerolinea_uri = o2
                for s3, p3, o3 in grafo_transportes.triples((aerolinea_uri, RDFS.label, None)):
                    aerolinea = str(o3)
            
            # Extraer fecha para determinar si es ida o vuelta
            fecha_str = None
            if salida:
                fecha_str = str(salida).split('T')[0]
            
            # Identificar si es vuelo de ida o vuelta
            # Primero por el URI, luego por fecha si está disponible
            es_ida = True
            if 'vuelta' in str(transporte_uri).lower():
                es_ida = False
            
            # Comprobar hora de salida/llegada (preferimos horas cómodas)
            hora_salida_buena = False
            hora_llegada_buena = False
            
            # Convertir formato ISO a datetime para análisis
            try:
                if salida:
                    fecha_hora_salida = datetime.datetime.fromisoformat(str(salida).replace('Z', '+00:00'))
                    hora_salida = fecha_hora_salida.hour
                    # Horas cómodas: 8-11 y 17-20
                    hora_salida_buena = (8 <= hora_salida <= 11) or (17 <= hora_salida <= 20)
                else:
                    hora_salida_buena = False;
                    
                if llegada:
                    fecha_hora_llegada = datetime.datetime.fromisoformat(str(llegada).replace('Z', '+00:00'))
                    hora_llegada = fecha_hora_llegada.hour
                    # Horas cómodas: 12-15 y 19-22
                    hora_llegada_buena = (12 <= hora_llegada <= 15) or (19 <= hora_llegada <= 22)
                else:
                    hora_llegada_buena = False
            except Exception:
                hora_salida_buena = False
                hora_llegada_buena = False
            
            # Crear objeto con detalles para evaluar
            detalle_vuelo = {
                'uri': transporte_uri,
                'precio': precio if precio is not None else float('inf'),
                'salida': salida,
                'llegada': llegada,
                'duracion_minutos': duracion_minutos,
                'num_escalas': num_escalas,
                'tiene_escalas': tiene_escalas,
                'aerolinea': aerolinea,
                'hora_salida_buena': hora_salida_buena,
                'hora_llegada_buena': hora_llegada_buena,
                'fecha': fecha_str
            }
            
            # Añadir a la lista correspondiente (filtrar por precio máximo si aplica)
            if precio_max is None or precio <= precio_max/2:  # La mitad para ida, la otra mitad para vuelta
                if es_ida:
                    vuelos_ida.append(detalle_vuelo)
                else:
                    vuelos_vuelta.append(detalle_vuelo)
    
    # Si no hay vuelos, devolver None
    if not vuelos_ida or not vuelos_vuelta:
        logger.warning("No se encontraron suficientes vuelos para evaluar")
        return None, None
    
    # Aplicar heurística para clasificar vuelos
    for lista_vuelos in [vuelos_ida, vuelos_vuelta]:
        for v in lista_vuelos:
            # Normalizar factores para heurística
            precio_norm = min(1.0, 100.0 / max(v['precio'], 100.0))  # Más bajo es mejor
            
            duracion_norm = 1.0
            if v['duracion_minutos']:
                duracion_norm = max(0.0, 1.0 - (v['duracion_minutos'] - 90) / 300)  # Menos duración es mejor
            
            escalas_norm = 1.0 if not v['tiene_escalas'] else (0.7 if v['num_escalas'] <= 1 else 0.3)  # Directo es mejor
            
            horario_norm = 0.5
            if v['hora_salida_buena'] and v['hora_llegada_buena']:
                horario_norm = 1.0
            elif v['hora_salida_buena'] or v['hora_llegada_buena']:
                horario_norm = 0.7
            
            # Puntuación ponderada
            v['puntuacion'] = (0.4 * precio_norm +          # 40% precio
                              0.25 * duracion_norm +        # 25% duración
                              0.25 * escalas_norm +         # 25% escalas
                              0.1 * horario_norm)           # 10% horario
    
    # Ordenar por puntuación (descendente)
    vuelos_ida_ordenados = sorted(vuelos_ida, key=lambda x: x['puntuacion'], reverse=True)
    vuelos_vuelta_ordenados = sorted(vuelos_vuelta, key=lambda x: x['puntuacion'], reverse=True)
    
    mejor_ida = vuelos_ida_ordenados[0] if vuelos_ida_ordenados else None
    mejor_vuelta = vuelos_vuelta_ordenados[0] if vuelos_vuelta_ordenados else None
    
    if mejor_ida and mejor_vuelta:
        logger.info(f"Vuelo ida seleccionado: {mejor_ida.get('aerolinea', 'N/A')} - {mejor_ida['precio']:.2f}€")
        logger.info(f"Vuelo vuelta seleccionado: {mejor_vuelta.get('aerolinea', 'N/A')} - {mejor_vuelta['precio']:.2f}€")
    
    return mejor_ida, mejor_vuelta


def evaluar_alojamientos(grafo_alojamientos, precio_max=None):
    """
    Evalúa los alojamientos recibidos del AgenteAlojamientos y selecciona el mejor
    """
    alojamientos = []
    
    # Buscar la respuesta de alojamiento
    for s, p, o in grafo_alojamientos.triples((None, RDF.type, onto.RespuestaAlojamiento)):
        respuesta_uri = s
        
        # Recuperar todos los alojamientos (probar ambas propiedades)
        for propiedad_alojamiento in [onto.contieneAlojamiento, onto.formadoPorAlojamientos]:
            for s1, p1, o1 in grafo_alojamientos.triples((s, propiedad_alojamiento, None)):
                alojamiento_uri = o1
                
                # Extraer detalles del alojamiento
                nombre = None
                for s2, p2, o2 in grafo_alojamientos.triples((alojamiento_uri, RDFS.label, None)):
                    nombre = str(o2)
                
                precio = None
                # Probar diferentes propiedades para el precio
                for precio_prop in [onto.PrecioPorNoche, onto.Precio]:
                    for s2, p2, o2 in grafo_alojamientos.triples((alojamiento_uri, precio_prop, None)):
                        precio = float(o2)
                        break
                    if precio is not None:
                        break
                
                valoracion = None
                for s2, p2, o2 in grafo_alojamientos.triples((alojamiento_uri, onto.Valoracion, None)):
                    valoracion = float(o2)
                
                ubicacion = None
                distancia_centro = None
                direccion = None
                
                for s2, p2, o2 in grafo_alojamientos.triples((alojamiento_uri, onto.UbicacionCentrica, None)):
                    ubicacion = str(o2).lower() == "true"
                    
                for s2, p2, o2 in grafo_alojamientos.triples((alojamiento_uri, onto.DistanciaCentro, None)):
                    distancia_centro = float(o2)
                    
                for s2, p2, o2 in grafo_alojamientos.triples((alojamiento_uri, onto.Direccion, None)):
                    direccion = str(o2)
                
                # Crear objeto con detalles para evaluar
                detalle_alojamiento = {
                    'uri': alojamiento_uri,
                    'nombre': nombre,
                    'precio': precio if precio is not None else float('inf'),
                    'valoracion': valoracion if valoracion is not None else 0,
                    'ubicacion_centrica': ubicacion,
                    'distancia_centro': distancia_centro,
                    'direccion': direccion
                }
                
                # Filtrar por precio máximo si se proporcionó
                if precio_max is None or detalle_alojamiento['precio'] <= precio_max:
                    alojamientos.append(detalle_alojamiento)
    
    if not alojamientos:
        logger.warning("No se encontraron alojamientos que cumplan los criterios")
        return None
    
    # Aplicar heurística para clasificar alojamientos
    # Combinamos precio (40%), valoración (40%) y distancia al centro (20%)
    for a in alojamientos:
        precio_normalizado = min(1.0, 50.0 / max(a['precio'], 50.0))  # Normalizar precio (más bajo es mejor)
        valoracion_normalizada = a['valoracion'] / 5.0 if a['valoracion'] else 0.5  # Normalizar valoración
        distancia_normalizada = 1.0 - min(1.0, a['distancia_centro'] / 5.0) if a['distancia_centro'] else 0.5  # Normalizar distancia
        
        # Puntuación ponderada
        a['puntuacion'] = (0.4 * precio_normalizado +
                          0.4 * valoracion_normalizada +
                          0.2 * distancia_normalizada)
    
    # Ordenar por puntuación (descendente)
    alojamientos_ordenados = sorted(alojamientos, key=lambda x: x['puntuacion'], reverse=True)
    
    if alojamientos_ordenados:
        mejor = alojamientos_ordenados[0]
        logger.info(f"Mejor alojamiento seleccionado: {mejor['nombre']} - {mejor['precio']:.2f}€/noche")
        return mejor
    
    return None


def evaluar_actividades(grafo_actividades, actividades_por_dia, dias_estancia, preferencias=None, precio_max=None):
    """
    Evalúa las actividades recibidas y las organiza optimizando las preferencias del usuario
    """
    if not grafo_actividades or not actividades_por_dia:
        logger.warning("No hay actividades para evaluar")
        return None, 0
    
    # Presupuesto diario si se especifica
    precio_max_dia = None
    if precio_max:
        precio_max_dia = precio_max / dias_estancia
    
    # Estructura para el plan final
    plan_actividades = {}
    precio_total = 0
    
    # Conjunto para registrar actividades ya incluidas (evitar duplicados)
    actividades_incluidas = set()
    
    # Definir preferencias de tipo
    preferencias_ponderadas = {
        'Cultural': 0.2,
        'Aventura': 0.1,
        'Gastronomica': 0.15,
        'Naturaleza': 0.1,
        'Exterior': 0.05,
        'Interior': 0.0
    }
    
    # Modificar ponderaciones según preferencias específicas del usuario
    if preferencias:
        for pref in preferencias:
            if pref in preferencias_ponderadas:
                preferencias_ponderadas[pref] += 0.3  # Bonus por preferencia explícita
    
    # Número máximo de actividades por día y franja
    max_actividades_por_franja = {
        'mañana': 3,
        'tarde': 3,
        'noche': 3
    }
    
    # Aplicar heurística para seleccionar actividades
    for dia, datos_dia in actividades_por_dia.items():
        plan_actividades[dia] = {
            'fecha': datos_dia['fecha'],
            'franjas': {
                'mañana': [],
                'tarde': [],
                'noche': []
            }
        }
        
        # Presupuesto restante para este día
        presupuesto_dia = precio_max_dia if precio_max_dia else float('inf')
        
        # Para cada franja, seleccionar las mejores actividades que quepan en el presupuesto
        for franja in ['mañana', 'tarde', 'noche']:
            # Si no hay actividades para esta franja, continuar
            if franja not in datos_dia['franjas'] or not datos_dia['franjas'][franja]:
                continue
            
            # Presupuesto para esta franja
            presupuesto_franja = presupuesto_dia / 3  # Dividir equitativamente
            
            # Ordenar actividades por preferencia y relación calidad/precio
            actividades = datos_dia['franjas'][franja]
            for act in actividades:
                precio_norm = min(1.0, 50.0 / max(act.get('precio', 50.0), 10.0))
                tipo_bonus = preferencias_ponderadas.get(act.get('tipo', ''), 0.0)
                act['puntuacion'] = precio_norm + tipo_bonus
            
            # Ordenar por puntuación (mayor primero)
            actividades_ordenadas = sorted(actividades, key=lambda x: x.get('puntuacion', 0), reverse=True)
            
            # Seleccionar actividades hasta agotar el presupuesto o el límite de actividades
            actividades_seleccionadas = []
            max_acts = max_actividades_por_franja[franja]
            
            for act in actividades_ordenadas:
                # Verificar si la actividad ya está incluida en cualquier parte del plan
                if str(act['uri']) in actividades_incluidas:
                    continue  # Saltar esta actividad
                
                if len(actividades_seleccionadas) >= max_acts:
                    break
                    
                precio = act.get('precio', 0)
                if precio <= presupuesto_franja:
                    actividades_seleccionadas.append(act)
                    # Agregar la URI a las actividades ya incluidas
                    actividades_incluidas.add(str(act['uri']))
                    presupuesto_franja -= precio
                    presupuesto_dia -= precio
                    precio_total += precio
            
            # Guardar en el plan
            plan_actividades[dia]['franjas'][franja] = actividades_seleccionadas
    
    return plan_actividades, precio_total


def procesar_respuesta_transportes(grafo_respuesta, respuesta_uri, peticion_original):
    """
    Procesa la respuesta del AgenteTransportes y genera una respuesta de plan
    
    :param grafo_respuesta: Grafo RDF con la respuesta de transportes
    :param respuesta_uri: URI de la respuesta de transportes
    :param peticion_original: URI de la petición original (opcional)
    :return: Mensaje XML con la respuesta de plan
    """
    global mss_cnt
    
    # Evaluar los transportes y seleccionar el mejor
    mejor_ida, mejor_vuelta = evaluar_transportes(grafo_respuesta, respuesta_uri)
    
    if not mejor_ida or not mejor_vuelta:
        logger.warning("No se pudieron encontrar transportes adecuados")
        # Crear respuesta de error
        g = Graph()
        g.bind('rdf', RDF)
        g.bind('rdfs', RDFS)
        g.bind('onto', onto)
        
        respuesta_id = URIRef(f'respuesta_plan_{str(uuid.uuid4())}')
        g.add((respuesta_id, RDF.type, onto.AceptacionPeticion))
        g.add((respuesta_id, RDFS.comment, Literal("No se pudieron encontrar transportes adecuados")))
        
        # Si hay una petición original, referenciarla
        if peticion_original:
            g.add((respuesta_id, onto.respuestaA, peticion_original))
        
        # Construir mensaje completo
        mss_cnt += 1
        return build_message(g, ACL.inform,
                            sender=AgentePlanes.uri,
                            receiver=AgentePlanes.uri,  # Cambia esto según corresponda
                            msgcnt=mss_cnt).serialize(format='xml')
    
    # Crear respuesta con el plan seleccionado
    g = Graph()
    g.bind('rdf', RDF)
    g.bind('rdfs', RDFS)
    g.bind('onto', onto)
    g.bind('xsd', XSD)
    
    plan_id = URIRef(f'plan_{str(uuid.uuid4())}')
    g.add((plan_id, RDF.type, onto.Plan))
    
    # Incluir los transportes seleccionados
    g.add((plan_id, onto.incluyeTransporteIda, mejor_ida['uri']))
    g.add((plan_id, onto.incluyeTransporteVuelta, mejor_vuelta['uri']))
    
    # Añadir el precio total
    precio_total = mejor_ida['precio'] + mejor_vuelta['precio']
    g.add((plan_id, onto.PrecioTotal, Literal(precio_total, datatype=XSD.float)))
    
    # Crear la respuesta
    respuesta_id = URIRef(f'respuesta_plan_{str(uuid.uuid4())}')
    g.add((respuesta_id, RDF.type, onto.RespuestaPlan))
    g.add((respuesta_id, onto.contienePlan, plan_id))
    
    # Añadir los detalles de los transportes
    # (copiar todos los detalles relevantes del grafo original)
    for s, p, o in grafo_respuesta.triples((mejor_ida['uri'], None, None)):
        g.add((mejor_ida['uri'], p, o))
    
    for s, p, o in grafo_respuesta.triples((mejor_vuelta['uri'], None, None)):
        g.add((mejor_vuelta['uri'], p, o))
    
    # Si hay una petición original, referenciarla
    if peticion_original:
        g.add((respuesta_id, onto.respuestaA, peticion_original))
    
    # Registrar el plan en el AgenteMantenedorPlanes
    registrar_plan_en_mantenedor(plan_id, g)

    # Construir mensaje completo
    mss_cnt += 1
    return build_message(g, ACL.inform,
                        sender=AgentePlanes.uri,
                        receiver=AgentePlanes.uri,  # Cambia esto según corresponda
                        msgcnt=mss_cnt).serialize(format='xml')


def procesar_peticion_plan(origen, destino, fecha_ida, fecha_vuelta, precio_max, content, sender):
    """
    Procesa una petición de plan completo
    
    :param origen: Nombre de la ciudad origen
    :param destino: Nombre de la ciudad destino
    :param fecha_ida: Fecha de ida
    :param fecha_vuelta: Fecha de vuelta
    :param precio_max: Precio máximo (opcional)
    :param content: URI del contenido para responder
    :param sender: URI del remitente
    :return: Mensaje XML con la respuesta
    """
    global mss_cnt
    
    logger.info(f"Procesando petición de plan desde {origen} hacia {destino}")
    
    # Solicitar transportes al AgenteTransportes
    grafo_transportes = solicitar_transportes(origen, destino, fecha_ida, fecha_vuelta, precio_max)
    
    if not grafo_transportes:
        logger.warning("No se pudieron obtener opciones de transporte")
        # Crear respuesta de error
        g = Graph()
        g.bind('rdf', RDF)
        g.bind('onto', onto)
        
        respuesta_id = URIRef(f'respuesta_plan_{str(uuid.uuid4())}')
        g.add((respuesta_id, RDF.type, onto.RespuestaPlan))
        g.add((respuesta_id, RDFS.comment, Literal("No se pudieron obtener opciones de transporte")))
        
        # Construir mensaje completo
        mss_cnt += 1
        return build_message(g, ACL.inform,
                            sender=AgentePlanes.uri,
                            receiver=sender,
                            content=respuesta_id,
                            msgcnt=mss_cnt).serialize(format='xml')
    
    # Evaluar transportes y crear un plan
    mejor_ida, mejor_vuelta = evaluar_transportes(grafo_transportes, content)
    
    if not mejor_ida or not mejor_vuelta:
        logger.warning("No se pudieron encontrar transportes adecuados")
        # Crear respuesta de error
        g = Graph()
        g.bind('rdf', RDF)
        g.bind('rdfs', RDFS)
        g.bind('onto', onto)
        
        respuesta_id = URIRef(f'respuesta_plan_{str(uuid.uuid4())}')
        g.add((respuesta_id, RDF.type, onto.RespuestaPlan))
        g.add((respuesta_id, RDFS.comment, Literal("No se pudieron encontrar transportes adecuados")))
        
        # Construir mensaje completo
        mss_cnt += 1
        return build_message(g, ACL.inform,
                            sender=AgentePlanes.uri,
                            receiver=sender,
                            content=respuesta_id,
                            msgcnt=mss_cnt).serialize(format='xml')
    
    # Crear respuesta con el plan seleccionado
    g = Graph()
    g.bind('rdf', RDF)
    g.bind('rdfs', RDFS)
    g.bind('onto', onto)
    g.bind('xsd', XSD)
    
    plan_id = URIRef(f'plan_{str(uuid.uuid4())}')
    g.add((plan_id, RDF.type, onto.Plan))
    
    # Incluir los transportes seleccionados
    g.add((plan_id, onto.incluyeTransporteIda, mejor_ida['uri']))
    g.add((plan_id, onto.incluyeTransporteVuelta, mejor_vuelta['uri']))
    
    # Añadir el precio total
    precio_total = mejor_ida['precio'] + mejor_vuelta['precio']
    g.add((plan_id, onto.PrecioTotal, Literal(precio_total, datatype=XSD.float)))
    
    # Crear la respuesta
    respuesta_id = URIRef(f'respuesta_plan_{str(uuid.uuid4())}')
    g.add((respuesta_id, RDF.type, onto.RespuestaPlan))
    g.add((respuesta_id, onto.contienePlan, plan_id))
    g.add((respuesta_id, onto.respuestaA, content))
    
    # Añadir los detalles de los transportes
    # (copiar todos los detalles relevantes del grafo original)
    for s, p, o in grafo_transportes.triples((mejor_ida['uri'], None, None)):
        g.add((mejor_ida['uri'], p, o))
    
    for s, p, o in grafo_transportes.triples((mejor_vuelta['uri'], None, None)):
        g.add((mejor_vuelta['uri'], p, o))
    
    # Construir mensaje completo
    mss_cnt += 1
    return build_message(g, ACL.inform,
                        sender=AgentePlanes.uri,
                        receiver=sender,
                        content=respuesta_id,
                        msgcnt=mss_cnt).serialize(format='xml')


def agentbehavior1(cola):
    """
    Comportamiento del agente - Registrarse en el directorio
    """
    global mss_cnt
    # Registrar el agente en el servicio de directorio
    gmess = Graph()
    gmess.bind('foaf', FOAF)
    gmess.bind('dso', DSO)
    reg_obj = agn[AgentePlanes.name + '-Register']
    gmess.add((reg_obj, RDF.type, DSO.Register))
    gmess.add((reg_obj, DSO.Uri, AgentePlanes.uri))
    gmess.add((reg_obj, FOAF.name, Literal(AgentePlanes.name)))
    gmess.add((reg_obj, DSO.Address, Literal(AgentePlanes.address)))
    gmess.add((reg_obj, DSO.AgentType, DSO.SolverAgent))  # Registrarse como agente solucionador

    # Lo metemos en el registro de servicios
    try:
        send_message(
            build_message(gmess, ACL.request,
                        sender=AgentePlanes.uri,
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
            # Esperar a un mensaje en la cola
            msg = cola.get()
            if msg == 0:
                logger.info("Finalizando comportamiento del agente")
                break
        except Exception as e:
            logger.error(f"Error en el comportamiento del agente: {e}")
            break


@app.route("/test", methods=['GET', 'POST'])
def test_interface():
    """
    Interfaz web para probar el agente de planes
    """
    if request.method == 'GET':
        # Mostrar un formulario para pruebas
        return '''
        <html>
            <head>
                <title>Test Agente Planes</title>
                <style>
                    body { font-family: Arial, sans-serif; margin: 20px; }
                    .form-group { margin-bottom: 15px; }
                    label { display: block; margin-bottom: 5px; }
                    input, select { padding: 8px; width: 300px; }
                    .checkbox-group { display: flex; flex-wrap: wrap; }
                    .checkbox-item { margin-right: 15px; margin-bottom: 10px; }
                    button { padding: 10px 15px; background-color: #4CAF50; color: white; border: none; cursor: pointer; }
                    h2 { margin-top: 30px; }
                </style>
            </head>
            <body>
                <h1>Test Agente Planes</h1>
                
                <form method="post">
                    <div class="form-group">
                        <label>Ciudad origen:</label>
                        <select name="origen">
                            <option value="Barcelona">Barcelona</option>
                            <option value="Madrid">Madrid</option>
                            <option value="Valencia">Valencia</option>
                            <option value="Sevilla">Sevilla</option>
                            <option value="Paris">Paris</option>
                            <option value="Roma">Roma</option>
                            <option value="Londres">Londres</option>
                            <option value="Berlin">Berlin</option>
                            <option value="Amsterdam">Amsterdam</option>
                            <option value="Lisboa">Lisboa</option>
                        </select>
                    </div>
                    
                    <div class="form-group">
                        <label>Ciudad destino:</label>
                        <select name="destino">
                            <option value="Madrid">Madrid</option>
                            <option value="Barcelona">Barcelona</option>
                            <option value="Valencia">Valencia</option>
                            <option value="Sevilla">Sevilla</option>
                            <option value="Paris">Paris</option>
                            <option value="Roma">Roma</option>
                            <option value="Londres">Londres</option>
                            <option value="Berlin">Berlin</option>
                            <option value="Amsterdam">Amsterdam</option>
                            <option value="Lisboa">Lisboa</option>
                        </select>
                    </div>
                    
                    <div class="form-group">
                        <label>Fecha ida:</label>
                        <input type="date" name="fecha_ida" required>
                    </div>
                    
                    <div class="form-group">
                        <label>Fecha vuelta:</label>
                        <input type="date" name="fecha_vuelta" required>
                    </div>
                    
                    <div class="form-group">
                        <label>Precio máximo total (€):</label>
                        <input type="number" name="precio_max" min="1" step="1">
                    </div>
                    
                    <div class="form-group">
                        <label>Preferencias de actividades:</label>
                        <div class="checkbox-group">
                            <div class="checkbox-item">
                                <input type="checkbox" id="pref_cultural" name="preferencias" value="Cultural">
                                <label for="pref_cultural">Cultural</label>
                            </div>
                            <div class="checkbox-item">
                                <input type="checkbox" id="pref_aventura" name="preferencias" value="Aventura">
                                <label for="pref_aventura">Aventura</label>
                            </div>
                            <div class="checkbox-item">
                                <input type="checkbox" id="pref_gastronomica" name="preferencias" value="Gastronomica">
                                <label for="pref_gastronomica">Gastronómica</label>
                            </div>
                            <div class="checkbox-item">
                                <input type="checkbox" id="pref_naturaleza" name="preferencias" value="Naturaleza">
                                <label for="pref_naturaleza">Naturaleza</label>
                            </div>
                            <div class="checkbox-item">
                                <input type="checkbox" id="pref_exterior" name="preferencias" value="Exterior">
                                <label for="pref_exterior">Exterior</label>
                            </div>
                            <div class="checkbox-item">
                                <input type="checkbox" id="pref_interior" name="preferencias" value="Interior">
                                <label for="pref_interior">Interior</label>
                            </div>
                        </div>
                    </div>
                    
                    <button type="submit">Crear Plan</button>
                </form>
            </body>
        </html>
        '''
    else:
        # Procesar la petición POST
        origen = request.form['origen']
        destino = request.form['destino']
        fecha_ida = request.form['fecha_ida']
        fecha_vuelta = request.form['fecha_vuelta']
        precio_max = request.form.get('precio_max')
        preferencias = request.form.getlist('preferencias')
        
        if precio_max:
            precio_max = float(precio_max)
        
        # Calcular días de estancia
        try:
            fecha_ida_dt = datetime.datetime.fromisoformat(fecha_ida)
            fecha_vuelta_dt = datetime.datetime.fromisoformat(fecha_vuelta)
            dias_estancia = (fecha_vuelta_dt - fecha_ida_dt).days
            if dias_estancia < 1:
                dias_estancia = 1
        except:
            dias_estancia = 3
        
        # Solicitar transportes y alojamientos
        grafo_transportes = solicitar_transportes(origen, destino, fecha_ida, fecha_vuelta,
                                             precio_max * 0.6 if precio_max else None)
        grafo_alojamientos = solicitar_alojamientos(destino, fecha_ida, fecha_vuelta,
                                              (precio_max * 0.4 / dias_estancia) if precio_max else None)
        
        # Verificar si tenemos respuestas
        error_msg = None
        if not grafo_transportes:
            error_msg = "No se pudieron obtener opciones de transporte"
        elif not grafo_alojamientos:
            error_msg = "No se pudieron obtener opciones de alojamiento"
        
        if error_msg:
            return f'''
            <html>
                <head>
                    <title>Error</title>
                    <style>body {{ font-family: Arial, sans-serif; margin: 20px; }}</style>
                </head>
                <body>
                    <h1>Error</h1>
                    <p>{error_msg}</p>
                    <p><a href="/test">Volver a intentar</a></p>
                </body>
            </html>
            '''
        
        # Evaluar transportes y alojamientos
        mejor_ida, mejor_vuelta = evaluar_transportes(grafo_transportes, None, precio_max * 0.6 if precio_max else None)
        mejor_alojamiento = evaluar_alojamientos(grafo_alojamientos, (precio_max * 0.4 / dias_estancia) if precio_max else None)
        
        # Solicitar y evaluar actividades
        grafo_actividades, actividades_por_dia = solicitar_actividades(destino, fecha_ida, fecha_vuelta, preferencias, 
                                                (precio_max * 0.2) if precio_max else None)
        
        plan_actividades = None
        precio_actividades = 0
        
        if grafo_actividades and actividades_por_dia:
            plan_actividades, precio_actividades = evaluar_actividades(
                grafo_actividades, 
                actividades_por_dia, 
                dias_estancia,
                preferencias,
                (precio_max * 0.2) if precio_max else None
            )

        # Verificar si tenemos opciones válidas
        if not mejor_ida or not mejor_vuelta:
            return '''
            <html>
                <head>
                    <title>Error</title>
                    <style>body { font-family: Arial, sans-serif; margin: 20px; }</style>
                </head>
                <body>
                    <h1>Error</h1>
                    <p>No se pudieron encontrar transportes adecuados</p>
                    <p><a href="/test">Volver a intentar</a></p>
                </body>
            </html>
            '''
        
        if not mejor_alojamiento:
            return '''
            <html>
                <head>
                    <title>Error</title>
                    <style>body { font-family: Arial, sans-serif; margin: 20px; }</style>
                </head>
                <body>
                    <h1>Error</h1>
                    <p>No se pudieron encontrar alojamientos adecuados</p>
                    <p><a href="/test">Volver a intentar</a></p>
                </body>
            </html>
            '''
        
        # Calcular precios
        precio_transporte = mejor_ida['precio'] + mejor_vuelta['precio']
        precio_alojamiento = mejor_alojamiento['precio'] * dias_estancia
        precio_total = precio_transporte + precio_alojamiento + precio_actividades
        
        # Construir respuesta HTML
        html = f'''
        <html>
            <head>
                <title>Plan Completo Creado</title>
                <style>
                    body {{ font-family: Arial, sans-serif; margin: 20px; }}
                    h1, h2, h3 {{ color: #333; }}
                    .plan-box {{ background: #f9f9f9; padding: 20px; margin: 20px 0; border-radius: 5px; }}
                    .item-box {{ background: #ffffff; padding: 15px; margin: 10px 0; border: 1px solid #ddd; border-radius: 5px; }}
                    .precio {{ font-weight: bold; color: #4CAF50; }}
                    .total {{ font-size: 1.2em; margin-top: 20px; }}
                    .back-btn {{ margin-top: 20px; padding: 10px; background: #4CAF50; color: white; text-decoration: none; display: inline-block; border-radius: 5px; }}
                    .seccion {{ margin-bottom: 30px; }}
                    .info-hotel {{ color: #555; }}
                    .puntuacion {{ display: inline-block; background: #4CAF50; color: white; padding: 4px 8px; border-radius: 4px; font-weight: bold; }}
                    .actividades-lista {{ list-style-type: none; padding: 0; }}
                    .actividad-tipo {{ font-style: italic; color: #777; }}
                    .actividad-desc {{ margin: 5px 0; }}
                </style>
            </head>
            <body>
                <h1>Plan de Viaje Completo</h1>
                <div class="plan-box">
                    <h2>Detalles del Plan</h2>
                    <p>Viaje desde <strong>{origen}</strong> hacia <strong>{destino}</strong></p>
                    <p>Estancia del <strong>{fecha_ida}</strong> al <strong>{fecha_vuelta}</strong> ({dias_estancia} noches)</p>
                    
                    <div class="seccion">
                        <h2>Vuelos</h2>
                        <div class="item-box">
                            <h3>Vuelo de ida</h3>
                            <p>Aerolínea: <strong>{mejor_ida.get('aerolinea', 'No disponible')}</strong></p>
                            <p>Fecha y hora de salida: {mejor_ida['salida']}</p>
                            <p>Fecha y hora de llegada: {mejor_ida['llegada']}</p>
                            <p>Duración: {(mejor_ida.get('duracion_minutos') or 0) // 60}h {(mejor_ida.get('duracion_minutos') or 0) % 60}min</p>
                            <p class="precio">Precio: {mejor_ida['precio']:.2f}€</p>
                            <p>{'' if not mejor_ida.get('tiene_escalas') else f"Vuelo con {mejor_ida.get('num_escalas', 0)} escala(s)"}</p>
                        </div>
                        
                        <div class="item-box">
                            <h3>Vuelo de vuelta</h3>
                            <p>Aerolínea: <strong>{mejor_vuelta.get('aerolinea', 'No disponible')}</strong></p>
                            <p>Fecha y hora de salida: {mejor_vuelta['salida']}</p>
                            <p>Fecha y hora de llegada: {mejor_vuelta['llegada']}</p>
                            <p>Duración: {(mejor_vuelta.get('duracion_minutos') or 0) // 60}h {(mejor_ida.get('duracion_minutos') or 0) % 60}min</p>
                            <p class="precio">Precio: {mejor_vuelta['precio']:.2f}€</p>
                            <p>{'' if not mejor_vuelta.get('tiene_escalas') else f"Vuelo con {mejor_vuelta.get('num_escalas', 0)} escala(s)"}</p>
                        </div>
                    </div>
                    
                    <div class="seccion">
                        <h2>Alojamiento</h2>
                        <div class="item-box">
                            <h3>{mejor_alojamiento['nombre']}</h3>
                            <p class="info-hotel">Dirección: {mejor_alojamiento.get('direccion', 'No disponible')}</p>
                            <p class="info-hotel">Distancia al centro: {mejor_alojamiento.get('distancia_centro', 'N/A')} km</p>
                            <p class="info-hotel">Valoración: <span class="puntuacion">{mejor_alojamiento.get('valoracion', 'N/A')}/5</span></p>
                            <p class="precio">Precio por noche: {mejor_alojamiento['precio']:.2f}€</p>
                            <p class="precio">Precio total ({dias_estancia} noches): {precio_alojamiento:.2f}€</p>
                        </div>
                    </div>
                    
                    <div class="seccion">
                        <h2>Actividades</h2>
'''

        if plan_actividades:
            for dia in range(1, dias_estancia + 1):
                if dia in plan_actividades:
                    tiene_actividades = False
                    
                    # Contar total de actividades para este día
                    actividades_dia = 0
                    for franja in plan_actividades[dia]['franjas'].values():
                        actividades_dia += len(franja)
                        
                    if actividades_dia > 0:
                        tiene_actividades = True
                        html += f'''
                        <div class="dia-actividades">
                            <h3>Día {dia} - {plan_actividades[dia]['fecha']}</h3>
                        '''
                        
                        for franja in ["mañana", "tarde", "noche"]:
                            if franja in plan_actividades[dia]['franjas'] and plan_actividades[dia]['franjas'][franja]:
                                html += f'''
                                <div class="franja-horaria">
                                    <h4>Franja: {franja}</h4>
                                    <ul class="lista-actividades">
                                '''
                                
                                for act in plan_actividades[dia]['franjas'][franja]:
                                    html += f'''
                                    <li>
                                        <strong>{act['nombre']}</strong>
                                        <span class="precio"> - {act.get('precio', 0):.2f}€</span>                                        <p class="actividad-tipo">Tipo: {act['tipo']} ({act['ubicacion']})</p>
                                        {f'<p class="actividad-desc">{act["descripcion"]}</p>' if act.get('descripcion') else ''}
                                    </li>
                                    '''
                                
                                html += '''
                                    </ul>
                                </div>
                                '''
                        
                        html += '''
                        </div>
                        '''
                    
                    if not tiene_actividades:
                        html += f'''
                        <div class="dia-actividades">
                            <h3>Día {dia} - {plan_actividades[dia]['fecha']}</h3>
                            <p>No hay actividades planificadas para este día.</p>
                        </div>
                        '''
        else:
            html += '''
            <p>No se pudieron encontrar actividades para este destino.</p>
            '''

        html += '''
                    </div>
                    
                    <div class="seccion">
                        <h2>Resumen de Precios</h2>
                        <p>Vuelos: <span class="precio">{:.2f}€</span></p>
                        <p>Alojamiento: <span class="precio">{:.2f}€</span></p>
                        <p>Actividades: <span class="precio">{:.2f}€</span></p>
                        <p class="total">Precio total: <span class="precio">{:.2f}€</span></p>
                    </div>
                    
                    <a href="/test" class="back-btn">Volver</a>
                </div>
            </body>
        </html>
        '''.format(precio_transporte, precio_alojamiento, precio_actividades, precio_total)
        
        return html

@app.route("/status")
def status():
    """
    Muestra el estado del agente y la información de depuración
    """

    # Verificar conexiones con otros agentes
    transporte_info = "No comprobado"

    try:
       
        agente_transporte = buscar_agente_transportes()
        if agente_transporte:
            transporte_info = f"Encontrado: {agente_transporte['name']} en {agente_transporte['address']}"
        else:
            transporte_info = "No encontrado en directorio"
    except Exception as e:
        transporte_info = f"Error al buscar: {str(e)}"
    
    alojamiento_info = "No comprobado"
    try:
        agente_alojamiento = buscar_agente_alojamientos()
        if agente_alojamiento:
            alojamiento_info = f"Encontrado: {agente_alojamiento['name']} en {agente_alojamiento['address']}"
        else:
            alojamiento_info = "No encontrado en directorio"
    except Exception as e:
        alojamiento_info = f"Error al buscar: {str(e)}"
    
    # Preparar HTML
    html = f"""
    <html>
        <head>
            <title>Estado del Agente de Planes</title>
            <style>
                body {{ font-family: Arial, sans-serif; margin: 20px; }}
                h1, h2 {{ color: #333; }}
                .status-box {{ background: #f5f5f5; padding: 15px; margin: 15px 0; border-radius: 5px; }}
                .success {{ color: green; }}
                .warning {{ color: orange; }}
                .error {{ color: red; }}
                table {{ border-collapse: collapse; width: 100%; margin: 15px 0; }}
                th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; }}
                th {{ background-color: #f2f2f2; }}
                .btn {{ padding: 10px; background: #4CAF50; color: white; text-decoration: none; display: inline-block; border-radius: 5px; margin: 10px 0; }}
            </style>
        </head>
        <body>
            <h1>Estado del Agente de Planes</h1>
            
            <div class="status-box">
                <h2>Información del agente</h2>
                <p><strong>Nombre:</strong> {AgentePlanes.name}</p>
                <p><strong>URI:</strong> {AgentePlanes.uri}</p>
                <p><strong>Dirección:</strong> {AgentePlanes.address}</p>
            </div>
            
            <div class="status-box">
                <h2>Conexiones con otros agentes</h2>
                <p><strong>Directorio:</strong> {DirectoryAgent.address}</p>
                <p><strong>AgenteTransportes:</strong> <span class="{'success' if 'Encontrado' in transporte_info else 'warning'}">{transporte_info}</span></p>
                <p><strong>AgenteAlojamientos:</strong> <span class="{'success' if 'Encontrado' in alojamiento_info else 'warning'}">{alojamiento_info}</span></p>
            </div>
            
            <div class="status-box">
                <h2>Estado de los problemas</h2>
                <p><strong>Pendientes:</strong> {len(problemas_pendientes)}</p>
                <p><strong>En proceso:</strong> {len(problemas_en_proceso)}</p>
                <p><strong>Resueltos:</strong> {len(problemas_resueltos)}</p>
            </div>
            
            <h2>Problemas resueltos recientes</h2>
            <table>
                <tr>
                    <th>ID</th>
                    <th>Origen</th>
                    <th>Destino</th>
                    <th>Fecha</th>
                </tr>
    """
    
    # Mostrar los últimos 5 problemas resueltos
    for problema_id, problema in list(problemas_resueltos.items())[-5:]:
        html += f"""
                <tr>
                    <td>{problema_id[:8]}...</td>
                    <td>{problema['problema']['origen']}</td>
                    <td>{problema['problema']['destino']}</td>
                    <td>{problema['timestamp'].strftime('%Y-%m-%d %H:%M:%S')}</td>
                </tr>
        """
    
    html += """
            </table>
            
            <div>
                <a href="/test" class="btn">Ir al formulario de prueba</a>
            </div>
        </body>
    </html>
    """
    
    return html

# Estructuras para manejar problemas en proceso
problemas_pendientes = {}  # Problemas recibidos pendientes de procesar
problemas_en_proceso = {}  # Problemas que se están procesando actualmente
problemas_resueltos = {}   # Problemas ya resueltos con sus soluciones

# Función para procesar problemas asíncrono (como haría un solver)
def procesar_cola_problemas():
    """
    Procesador de problemas asíncrono. Toma problemas de la cola pendiente y los procesa.
    Similar al comportamiento de un solver distribuido.
    """
    logger.info("Iniciando procesador de cola de problemas")
    
    while True:
        try:
            # Si hay problemas pendientes, tomar uno
            if problemas_pendientes:
                # Seleccionar un problema pendiente (el más antiguo)
                problemas_ordenados = sorted(problemas_pendientes.items(), 
                                           key=lambda x: x[1]['timestamp'])
                problema_id, problema = problemas_ordenados[0]
                
                # Mover de pendiente a en proceso
                del problemas_pendientes[problema_id]
                problemas_en_proceso[problema_id] = problema
                
                logger.info(f"Procesando problema {problema_id}: {problema['origen']} a {problema['destino']}")
                
                # Extraer datos del problema
                origen = problema['origen']
                destino = problema['destino']
                fecha_ida = problema['fecha_ida']
                fecha_vuelta = problema['fecha_vuelta']
                precio_max = problema.get('precio_max')
                content = problema['content']
                sender = problema['sender']
                
                # Procesar el plan completo (transporte + alojamiento)
                respuesta = procesar_peticion_plan_completo(origen, destino, fecha_ida, fecha_vuelta, 
                                                     precio_max, content, sender)
                
                # Guardar la solución
                problemas_resueltos[problema_id] = {
                    'problema': problema,
                    'solucion': respuesta,
                    'timestamp': datetime.datetime.now()
                }
                
                # Eliminar de en proceso
                del problemas_en_proceso[problema_id]
                
                logger.info(f"Problema {problema_id} resuelto")
            
            # Esperar antes del siguiente ciclo
            time.sleep(0.5)
                
        except Exception as e:
            logger.error(f"Error procesando cola de problemas: {e}")
            import traceback
            logger.error(traceback.format_exc())
            time.sleep(1)


def procesar_peticion_plan_completo(origen, destino, fecha_ida, fecha_vuelta, precio_max=None, preferencias=None, content=None, sender=None):
    """
    Procesa una petición de plan completo incluyendo transporte, alojamiento y actividades
    
    :param origen: Ciudad de origen
    :param destino: Ciudad de destino
    :param fecha_ida: Fecha de ida
    :param fecha_vuelta: Fecha de vuelta
    :param precio_max: Precio máximo total (opcional)
    :param preferencias: Lista de preferencias de actividades
    :param content: URI del contenido para responder
    :param sender: URI del remitente
    :return: Mensaje XML con la respuesta completa
    """
    global mss_cnt
    
    logger.info(f"Procesando plan completo desde {origen} hacia {destino}")
    
    # Si no hay preferencias, inicializar como lista vacía
    if not preferencias:
        preferencias = []
    
    # Calcular precio máximo para cada componente
    precio_max_transporte = None
    precio_max_alojamiento = None
    precio_max_actividades = None
    
    if precio_max:
        precio_max_transporte = precio_max * 0.5
        precio_max_alojamiento = precio_max * 0.3
        precio_max_actividades = precio_max * 0.2
    
    # Calcular días de estancia
    try:
        fecha_ida_dt = datetime.datetime.fromisoformat(fecha_ida)
        fecha_vuelta_dt = datetime.datetime.fromisoformat(fecha_vuelta)
        dias_estancia = (fecha_vuelta_dt - fecha_ida_dt).days
        if dias_estancia < 1:
            dias_estancia = 1
    except Exception as e:
        logger.error(f"Error al calcular días de estancia: {e}")
        dias_estancia = 3  # Valor por defecto
    
    # Solicitar transportes, alojamientos y actividades en paralelo
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        future_transportes = executor.submit(solicitar_transportes, origen, destino, fecha_ida, fecha_vuelta, precio_max_transporte)
        future_alojamientos = executor.submit(solicitar_alojamientos, destino, fecha_ida, fecha_vuelta, precio_max_alojamiento/dias_estancia)
        future_actividades = executor.submit(solicitar_actividades, destino, fecha_ida, fecha_vuelta, preferencias, precio_max_actividades)
        
        # Obtener resultados
        grafo_transportes = future_transportes.result()
        grafo_alojamientos = future_alojamientos.result()
        grafo_actividades, actividades_por_dia = future_actividades.result()
    
    # Verificar que tenemos respuestas válidas
    if not grafo_transportes:
        logger.warning("No se pudieron obtener opciones de transporte")
        return crear_respuesta_error("No se pudieron obtener opciones de transporte", content, sender)
    
    if not grafo_alojamientos:
        logger.warning("No se pudieron obtener opciones de alojamiento")
        return crear_respuesta_error("No se pudieron obtener opciones de alojamiento", content, sender)
    
    # Evaluar transportes y alojamientos
    mejor_ida, mejor_vuelta = evaluar_transportes(grafo_transportes, content, precio_max_transporte)
    mejor_alojamiento = evaluar_alojamientos(grafo_alojamientos, precio_max_alojamiento/dias_estancia)
    
    # Evaluar actividades si existen
    plan_actividades = None
    precio_actividades = 0
    
    if grafo_actividades and actividades_por_dia:
        plan_actividades, precio_actividades = evaluar_actividades(
            grafo_actividades, 
            actividades_por_dia, 
            dias_estancia,
            preferencias,
            precio_max_actividades
        )
    
    # Verificar componentes críticos
    if not mejor_ida or not mejor_vuelta:
        logger.warning("No se encontraron transportes adecuados")
        return crear_respuesta_error("No se encontraron transportes adecuados", content, sender)
    
    if not mejor_alojamiento:
        logger.warning("No se encontraron alojamientos adecuados")
        return crear_respuesta_error("No se encontraron alojamientos adecuados", content, sender)
    
    # Calcular precio total del plan
    precio_transporte = mejor_ida['precio'] + mejor_vuelta['precio']
    precio_alojamiento = mejor_alojamiento['precio'] * dias_estancia
    precio_total = precio_transporte + precio_alojamiento + precio_actividades
    
    # Crear respuesta con el plan completo
    g = Graph()
    g.bind('rdf', RDF)
    g.bind('rdfs', RDFS)
    g.bind('onto', onto)
    g.bind('xsd', XSD)
    
    # Crear el plan
    plan_id = URIRef(f'plan_{str(uuid.uuid4())}')
    g.add((plan_id, RDF.type, onto.Plan))
    g.add((plan_id, RDF.type, onto.PlanGeneral))
    
    # Añadir transportes y alojamiento
    g.add((plan_id, onto.hasTransport, mejor_ida['uri']))
    g.add((plan_id, onto.transporteVuelta, mejor_vuelta['uri']))
    g.add((plan_id, onto.tieneAlojamiento, mejor_alojamiento['uri']))
    
    # Añadir origen, destino y fechas
    origen_uri = URIRef(f'ciudad_origen_{str(uuid.uuid4())}')
    g.add((origen_uri, RDF.type, onto.Ciudad))
    g.add((origen_uri, onto.NombreCiudad, Literal(origen)))
    g.add((plan_id, onto.saleDe, origen_uri))
    
    destino_uri = URIRef(f'ciudad_destino_{str(uuid.uuid4())}')
    g.add((destino_uri, RDF.type, onto.Ciudad))
    g.add((destino_uri, onto.NombreCiudad, Literal(destino)))
    g.add((plan_id, onto.llegaA, destino_uri))
    
    g.add((plan_id, onto.fecha_inicio, Literal(fecha_ida, datatype=XSD.date)))
    g.add((plan_id, onto.fecha_fin, Literal(fecha_vuelta, datatype=XSD.date)))
    
    # Añadir precios
    g.add((plan_id, onto.Precio, Literal(precio_total, datatype=XSD.float)))
    
    # Añadir actividades al plan si existen
    if plan_actividades:
        for dia, datos_dia in plan_actividades.items():
            dia_id = URIRef(f'dia_{dia}_{str(uuid.uuid4())}')
            g.add((dia_id, RDF.type, onto.PlanDe1Dia))
            g.add((dia_id, RDFS.label, Literal(f"Día {dia}: {datos_dia['fecha']}")))
            g.add((respuesta_completa_id, onto.estaCompuestoPor, dia_id))
            
            for franja, actividades in datos_dia['franjas'].items():
                for act in actividades:
                    g.add((dia_id, onto.seRealizan, act['uri']))
                    
                    # Copiar todos los detalles de la actividad del grafo original
                    for s, p, o in grafo_actividades.triples((act['uri'], None, None)):
                        g.add((s, p, o))
    
    # Crear respuesta de éxito
    g.bind('rdf', RDF)
    g.bind('rdfs', RDFS)
    g.bind('onto', onto)
    g.bind('xsd', XSD)
    
    respuesta_id = URIRef(f'respuesta_plan_{str(uuid.uuid4())}')
    g.add((respuesta_id, RDF.type, onto.RespuestaPlan))
    g.add((respuesta_id, onto.formadoPorPlan, plan_id))
    
    if content:
        g.add((respuesta_id, onto.respuestaA, content))
    
    # Añadir los detalles de los transportes y alojamiento
    for s, p, o in grafo_transportes.triples((mejor_ida['uri'], None, None)):
        g.add((s, p, o))
    
    for s, p, o in grafo_transportes.triples((mejor_vuelta['uri'], None, None)):
        g.add((s, p, o))
    
    for s, p, o in grafo_alojamientos.triples((mejor_alojamiento['uri'], None, None)):
        g.add((s, p, o))
    
    # Registrar el plan en el AgenteMantenedorPlanes
    registrar_plan_en_mantenedor(plan_id, g)

    # Construir mensaje completo siguiendo la especificación FIPA ACL
    mss_cnt += 1
    return build_message(g, ACL.inform,
                        sender=AgentePlanes.uri,
                        receiver=sender if sender else AgentePlanes.uri,
                        content=respuesta_id,
                        msgcnt=mss_cnt).serialize(format='xml')


def crear_respuesta_error(mensaje_error, content=None, sender=None):
    """
    Crea una respuesta de error
    """
    global mss_cnt
    
    g = Graph()
    g.bind('rdf', RDF)
    g.bind('rdfs', RDFS)
    g.bind('onto', onto)
    
    respuesta_id = URIRef(f'respuesta_plan_{str(uuid.uuid4())}')
    g.add((respuesta_id, RDF.type, onto.RespuestaPlan))
    g.add((respuesta_id, RDFS.comment, Literal(mensaje_error)))
    
    if content:
        g.add((respuesta_id, onto.respuestaA, content))
    
    # Construir mensaje completo
    mss_cnt += 1
    return build_message(g, ACL.inform,
                        sender=AgentePlanes.uri,
                        receiver=sender if sender else AgentePlanes.uri,
                        content=respuesta_id,
                        msgcnt=mss_cnt).serialize(format='xml')

def registrar_plan_en_mantenedor(plan_uri, grafo_plan):
    """
    Registra un plan en el AgenteMantenedorPlanes
    
    :param plan_uri: URI del plan a registrar
    :param grafo_plan: Grafo RDF con todos los datos del plan
    :return: True si se registró correctamente, False en caso contrario
    """
    global mss_cnt
    
    # Buscar el agente mantenedor de planes
    agente_mantenedor = buscar_agente_por_tipo(DSO.PlanAgent)
    if not agente_mantenedor:
        logger.error("No se pudo encontrar el AgenteMantenedorPlanes")
        return False
    
    # Crear petición de registro
    g = Graph()
    g.bind('rdf', RDF)
    g.bind('onto', onto)
    
    registro_id = URIRef('registro_plan_' + str(uuid.uuid4()))
    g.add((registro_id, RDF.type, onto.RegistroPlan))
    g.add((registro_id, onto.planARegistrar, plan_uri))
    
    # Copiar todo el grafo del plan
    for s, p, o in grafo_plan:
        g.add((s, p, o))
    
    # Construir mensaje ACL
    msg = build_message(g, ACL.request,
                      sender=AgentePlanes.uri,
                      receiver=URIRef(agente_mantenedor['uri']),
                      content=registro_id,
                      msgcnt=mss_cnt)
    mss_cnt += 1
    
    # Enviar la petición
    try:
        response = requests.get(agente_mantenedor['address'], params={'content': msg.serialize(format='xml')})
        
        if response.status_code == 200:
            logger.info(f"Plan {plan_uri} registrado correctamente en AgenteMantenedorPlanes")
            return True
        else:
            logger.error(f"Error al registrar plan: {response.status_code}")
            return False
    except Exception as e:
        logger.error(f"Error al registrar plan: {e}")
        return False

if __name__ == '__main__':
    try:
        # Iniciar el comportamiento de registro en el directorio
        ab1 = Process(target=agentbehavior1, args=(cola1,))
        ab1.start()
        
        # Iniciar el procesador de cola de problemas como un proceso independiente
        ab2 = Process(target=procesar_cola_problemas)
        ab2.start()
        
        # Informar sobre la configuración
        logger.info(f"AgentePlanes iniciándose en {hostname}:{port}")
        logger.info(f"Directorio en {dhostname}:{dport}")
        
        # Iniciar el servidor Flask
        app.run(host=hostname, port=port, debug=False)
        
        # Esperar a que terminen los procesos
        ab1.join()
        ab2.join()
        
    except Exception as e:
        logger.error(f"Error al iniciar el agente: {e}")
        if 'ab1' in locals() and ab1.is_alive():
            ab1.terminate()
        if 'ab2' in locals() and ab2.is_alive():
            ab2.terminate()
        logger.info('Agente terminado debido a un error')