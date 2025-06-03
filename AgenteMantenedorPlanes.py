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
    logger.debug(f"Recibido mensaje con performativa: {msgdic['performative']}")

    # Si es un nuevo plan para registrar
    if msgdic['performative'] == ACL.request:
        content = msgdic['content']
        
        # Buscar solicitud de registro de plan
        for s, p, o in gm.triples((None, RDF.type, onto.RegistroPlan)):
            plan_uri = None
            # Extraer URI del plan a registrar
            for s1, p1, o1 in gm.triples((s, onto.planARegistrar, None)):
                plan_uri = o1
                
            if plan_uri:
                # Copiar todo el grafo del plan a nuestra base de datos
                for s1, p1, o1 in gm.triples((plan_uri, None, None)):
                    planes_db.add((s1, p1, o1))
                    
                # Copiar también todas las propiedades de los componentes del plan
                for s1, p1, o1 in gm.triples((None, None, None)):
                    if s1 != s and s1 != content:  # No copiar la petición en sí
                        planes_db.add((s1, p1, o1))
                
                # Añadir estado "activo" al plan
                planes_db.add((plan_uri, onto.estado, Literal("activo")))
                
                # Añadir timestamp
                planes_db.add((plan_uri, onto.timestamp, Literal(datetime.datetime.now().isoformat(), datatype=XSD.dateTime)))
                
                # Guardar en el archivo para persistencia
                planes_db.serialize(DB_FILE, format="xml")
                
                logger.info(f"Plan registrado: {plan_uri}")
                
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
    
    # Si es una consulta de planes activos
    elif msgdic['performative'] == ACL.query_ref:
        content = msgdic['content']
        
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
    logger.warning(f"Petición desconocida: {msgdic.get('performative')}")
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
    Busca el agente de clima en el directorio
    """
    agente = buscar_agente_por_tipo(DSO.WeatherAgent)
    if not agente:
        # Configuración de respaldo si no se encuentra en el directorio
        agente = {
            'name': 'AgenteClima',
            'uri': 'http://www.agentes.org#AgenteClima',
            'address': f'http://{socket.gethostname()}:9001/comm'
        }
        logger.info(f"Usando configuración de respaldo para AgenteClima: {agente['address']}")
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
    Consulta el clima para una ciudad y fecha específicas
    
    :param ciudad: Nombre de la ciudad
    :param fecha: Fecha en formato YYYY-MM-DD
    :return: Información del clima o None si hay error
    """
    global mss_cnt
    
    # Buscar el agente de clima
    agente_clima = buscar_agente_clima()
    if not agente_clima:
        logger.error("No se pudo encontrar el AgenteClima")
        return None
    
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
        response = requests.get(agente_clima['address'], params={'content': msg.serialize(format='xml')})
        
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
            
            # Si no encontramos la fecha específica, devolver None
            return None
        else:
            logger.error(f"Error en la respuesta del clima: {response.status_code}")
            return None
    except Exception as e:
        logger.error(f"Error al consultar clima: {e}")
        return None

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
    # Buscar todos los planes activos
    for plan_uri, _, _ in planes_db.triples((None, RDF.type, onto.Plan)):
        # Verificar si está activo
        estado = planes_db.value(subject=plan_uri, predicate=onto.estado)
        if estado and str(estado) == "activo":
            # Obtener ciudad de destino
            ciudad = None
            for s, p, o in planes_db.triples((plan_uri, onto.llegaA, None)):
                ciudad_uri = o
                ciudad = planes_db.value(subject=ciudad_uri, predicate=onto.NombreCiudad)
            
            if not ciudad:
                logger.warning(f"Plan {plan_uri} no tiene ciudad de destino")
                continue
            
            # Obtener fechas del plan
            fecha_inicio = None
            fecha_fin = None
            
            for s, p, o in planes_db.triples((plan_uri, onto.fecha_inicio, None)):
                fecha_inicio = str(o)
            
            for s, p, o in planes_db.triples((plan_uri, onto.fecha_fin, None)):
                fecha_fin = str(o)
            
            if not fecha_inicio or not fecha_fin:
                logger.warning(f"Plan {plan_uri} no tiene fechas definidas")
                continue
            
            # Calcular días del plan
            fecha_inicio_dt = datetime.datetime.fromisoformat(fecha_inicio)
            fecha_fin_dt = datetime.datetime.fromisoformat(fecha_fin)
            
            # Para cada día del plan
            fecha_actual = fecha_inicio_dt
            while fecha_actual <= fecha_fin_dt:
                fecha_str = fecha_actual.strftime('%Y-%m-%d')
                
                # Consultar clima para este día
                clima_info = consultar_clima(ciudad, fecha_str)
                
                # Si hay mal tiempo
                if clima_info and clima_info.get('temporal_perjudicial'):
                    logger.info(f"Detectado mal tiempo en {ciudad} para {fecha_str}: {clima_info.get('descripcion')}")
                    
                    # Buscar actividades exteriores para este día
                    for dia_id, _, _ in planes_db.triples((None, RDF.type, onto.PlanDe1Dia)):
                        # Verificar si este día pertenece al plan
                        if (plan_uri, onto.estaCompuestoPor, dia_id) in planes_db:
                            # Verificar si la fecha coincide
                            dia_fecha = planes_db.value(subject=dia_id, predicate=RDFS.label)
                            if dia_fecha and fecha_str in str(dia_fecha):
                                # Buscar actividades de este día
                                for franja_id in planes_db.objects(subject=dia_id, predicate=onto.incluyeFranja):
                                    # Obtener franja horaria
                                    franja = planes_db.value(subject=franja_id, predicate=RDFS.label)
                                    if not franja:
                                        continue
                                    
                                    # Buscar actividades en esta franja
                                    for actividad_id in planes_db.objects(subject=franja_id, predicate=onto.seRealizan):
                                        # Verificar si es una actividad exterior
                                        es_exterior = False
                                        for s, p, o in planes_db.triples((actividad_id, RDF.type, onto.Exterior)):
                                            es_exterior = True
                                            break
                                        
                                        if es_exterior:
                                            logger.info(f"Encontrada actividad exterior {actividad_id} en día {fecha_str}, franja {franja}")
                                            
                                            # Buscar una actividad de interior para sustituirla
                                            nueva_actividad_id = buscar_actividad_interior(ciudad, fecha_str, str(franja))
                                            
                                            if nueva_actividad_id:
                                                logger.info(f"Reemplazando actividad exterior {actividad_id} por actividad interior {nueva_actividad_id}")
                                                
                                                # Eliminar la actividad exterior
                                                planes_db.remove((franja_id, onto.seRealizan, actividad_id))
                                                
                                                # Añadir la nueva actividad
                                                planes_db.add((franja_id, onto.seRealizan, nueva_actividad_id))
                                                
                                                # Guardar cambios
                                                planes_db.serialize(DB_FILE, format="xml")
                                            else:
                                                logger.warning(f"No se encontró actividad interior para sustituir a {actividad_id}")
                
                # Avanzar al siguiente día
                fecha_actual += datetime.timedelta(days=1)

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