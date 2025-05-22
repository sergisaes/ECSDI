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
            
            # Obtener origen
            for s1, p1, o1 in gm.triples((s, onto.comoOrigen, None)):
                for s2, p2, o2 in gm.triples((o1, onto.NombreCiudad, None)):
                    origen = str(o2)
            
            # Obtener destino
            for s1, p1, o1 in gm.triples((s, onto.comoDestino, None)):
                for s2, p2, o2 in gm.triples((o1, onto.NombreCiudad, None)):
                    destino = str(o2)
            
            # Obtener fechas
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
            
            # Procesar la respuesta de transportes
            respuesta_plan = procesar_respuesta_transportes(gm, s, peticion_original)
            return Response(respuesta_plan, mimetype='text/xml')
    
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
    return "Parando Agente de Planes"


def tidyup():
    """
    Acciones previas a parar el agente
    """
    global cola1
    cola1.put(0)


def buscar_agente_transportes():
    """
    Busca el agente de transportes en el directorio con mejor manejo de errores
    """
    global mss_cnt

    logger.info("Buscando agente de transportes en el directorio...")
    
    # Crear grafo para buscar en el directorio
    gmess = Graph()
    gmess.bind('dso', DSO)
    gmess.bind('rdf', RDF)
    search_obj = agn['search-' + str(uuid.uuid4())]
    gmess.add((search_obj, RDF.type, DSO.Search))
    gmess.add((search_obj, DSO.AgentType, DSO.TransportAgent))
    
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
            
        # Contar el número de tripletas en la respuesta
        num_triples = len(gr)
        logger.info(f"La respuesta del directorio tiene {num_triples} tripletas")
        
        if num_triples == 0:
            logger.error("La respuesta del directorio está vacía")
            return None
            
        # Imprimir la respuesta completa para diagnóstico
        logger.debug("Respuesta completa del directorio:")
        for s, p, o in gr:
            logger.debug(f"{s} {p} {o}")
        
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
        for s, p, o in gr.triples((None, DSO.AgentType, DSO.TransportAgent)):
            uri = gr.value(subject=s, predicate=DSO.Uri)
            name = gr.value(subject=s, predicate=FOAF.name)
            address = gr.value(subject=s, predicate=DSO.Address)
            
            if uri and address:
                logger.info(f"Encontrado agente: {name} en {address}")
                agentes_encontrados.append({
                    'name': name if name else "Desconocido",
                    'uri': uri,
                    'address': address
                })
        
        # Si no se encontraron agentes con ese método, intentar con el método original
        if not agentes_encontrados:
            logger.info("Intentando método alternativo de búsqueda...")
            for s, p, o in gr.triples((content, DSO.Address, None)):
                uri = gr.value(subject=s, predicate=DSO.Uri)
                name = gr.value(subject=s, predicate=FOAF.name)
                
                logger.info(f"Encontrado agente (alt): {name} en {o}")
                agentes_encontrados.append({
                    'name': name if name else "Desconocido",
                    'uri': uri,
                    'address': o
                })
        
        if agentes_encontrados:
            logger.info(f"Total de agentes de transporte encontrados: {len(agentes_encontrados)}")
            return agentes_encontrados[0]
        else:
            logger.warning("No se encontró ningún agente de transportes")
            return None
            
    except Exception as e:
        logger.error(f"Error al buscar agente de transportes: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return None


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


def evaluar_transportes(grafo_transportes, content_uri):
    """
    Evalúa los transportes recibidos del AgenteTransportes y selecciona el mejor
    
    :param grafo_transportes: Grafo RDF con los transportes disponibles
    :param content_uri: URI del contenido para responder
    :return: Tupla (mejor_ida, mejor_vuelta) con los mejores transportes
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
            
            # Extraer fecha para determinar si es ida o vuelta
            fecha_str = None
            if salida:
                fecha_str = str(salida).split('T')[0]
            
            # Identificar si es vuelo de ida o vuelta (basado en la fecha o en la estructura del URI)
            es_ida = True
            if 'vuelta' in str(transporte_uri).lower():
                es_ida = False
            
            # Crear objeto con detalles para evaluar
            detalle_vuelo = {
                'uri': transporte_uri,
                'precio': precio if precio is not None else float('inf'),
                'salida': salida,
                'llegada': llegada,
                'fecha': fecha_str
            }
            
            # Añadir a la lista correspondiente
            if es_ida:
                vuelos_ida.append(detalle_vuelo)
            else:
                vuelos_vuelta.append(detalle_vuelo)
    
    # Si no hay vuelos, devolver None
    if not vuelos_ida or not vuelos_vuelta:
        logger.warning("No se encontraron suficientes vuelos para evaluar")
        return None, None
    
    # Evaluar los vuelos según precio (en este caso, elegimos el más económico)
    mejor_ida = min(vuelos_ida, key=lambda x: x['precio'])
    mejor_vuelta = min(vuelos_vuelta, key=lambda x: x['precio'])
    
    return mejor_ida, mejor_vuelta


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
        g.add((respuesta_id, RDF.type, onto.RespuestaPlan))
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
                        <label>Precio máximo (€):</label>
                        <input type="number" name="precio_max" min="1" step="1">
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
        
        if precio_max:
            precio_max = float(precio_max)
        
        # Solicitar transportes al AgenteTransportes
        grafo_transportes = solicitar_transportes(origen, destino, fecha_ida, fecha_vuelta, precio_max)
        
        if not grafo_transportes:
            return '''
            <html>
                <head>
                    <title>Error</title>
                    <style>body { font-family: Arial, sans-serif; margin: 20px; }</style>
                </head>
                <body>
                    <h1>Error</h1>
                    <p>No se pudieron obtener opciones de transporte.</p>
                    <p><a href="/test">Volver a intentar</a></p>
                </body>
            </html>
            '''
        
        # Evaluar transportes y crear un plan
        mejor_ida, mejor_vuelta = evaluar_transportes(grafo_transportes, None)
        
        if not mejor_ida or not mejor_vuelta:
            return '''
            <html>
                <head>
                    <title>Error</title>
                    <style>body { font-family: Arial, sans-serif; margin: 20px; }</style>
                </head>
                <body>
                    <h1>Error</h1>
                    <p>No se pudieron encontrar transportes adecuados.</p>
                    <p><a href="/test">Volver a intentar</a></p>
                </body>
            </html>
            '''
        
        # Calcular precio total
        precio_total = mejor_ida['precio'] + mejor_vuelta['precio']
        
        # Construir respuesta HTML
        html = f'''
        <html>
            <head>
                <title>Plan Creado</title>
                <style>
                    body {{ font-family: Arial, sans-serif; margin: 20px; }}
                    h1, h2 {{ color: #333; }}
                    .plan-box {{ background: #f9f9f9; padding: 20px; margin: 20px 0; border-radius: 5px; }}
                    .transporte-box {{ background: #ffffff; padding: 15px; margin: 10px 0; border: 1px solid #ddd; border-radius: 5px; }}
                    .precio {{ font-weight: bold; color: #4CAF50; }}
                    .total {{ font-size: 1.2em; margin-top: 20px; }}
                    .back-btn {{ margin-top: 20px; padding: 10px; background: #4CAF50; color: white; text-decoration: none; display: inline-block; border-radius: 5px; }}
                </style>
            </head>
            <body>
                <h1>Plan de Viaje Creado</h1>
                <div class="plan-box">
                    <h2>Detalles del Plan</h2>
                    <p>Desde <strong>{origen}</strong> hacia <strong>{destino}</strong></p>
                    <p>Fecha ida: <strong>{fecha_ida}</strong> - Fecha vuelta: <strong>{fecha_vuelta}</strong></p>
                    
                    <h3>Transporte de Ida</h3>
                    <div class="transporte-box">
                        <p>Precio: <span class="precio">{mejor_ida['precio']:.2f}€</span></p>
                        <p>Fecha y hora de salida: {mejor_ida['salida']}</p>
                        <p>Fecha y hora de llegada: {mejor_ida['llegada']}</p>
                    </div>
                    
                    <h3>Transporte de Vuelta</h3>
                    <div class="transporte-box">
                        <p>Precio: <span class="precio">{mejor_vuelta['precio']:.2f}€</span></p>
                        <p>Fecha y hora de salida: {mejor_vuelta['salida']}</p>
                        <p>Fecha y hora de llegada: {mejor_vuelta['llegada']}</p>
                    </div>
                    
                    <p class="total">Precio Total: <span class="precio">{precio_total:.2f}€</span></p>
                </div>
                
                <a href="/test" class="back-btn">Crear Otro Plan</a>
            </body>
        </html>
        '''
        
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
                
                # Procesar el plan
                respuesta = procesar_peticion_plan(origen, destino, fecha_ida, fecha_vuelta, 
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
            time.sleep(1)


if __name__ == '__main__':
    try:
        # Poner en marcha los behaviors
        ab1 = Process(target=agentbehavior1, args=(cola1,))
        ab1.start()
        
        # Iniciar el procesador de problemas (como en DistributedSolver)
        ab2 = Process(target=procesar_cola_problemas, args=())
        ab2.start()

        logger.info(f"Iniciando servidor en {hostname}:{port}")
        # Ponemos en marcha el servidor
        app.run(host=hostname, port=port, debug=False)

        # Esperamos a que acaben los behaviors
        ab1.join()
        ab2.join()
        logger.info('Agente de Planes finalizado')
        
    except Exception as e:
        logger.error(f"Error al iniciar el agente: {e}")
        if 'ab1' in locals():
            ab1.terminate()
        if 'ab2' in locals():
            ab2.terminate()
        print('Error en el Agente de Planes')