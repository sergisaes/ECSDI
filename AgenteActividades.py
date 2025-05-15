# -*- coding: utf-8 -*-
"""
*** Agente de Actividades ***

Este agente recibe peticiones de actividades para una ciudad específica y
responde con una lista de actividades disponibles según los criterios de búsqueda.
Utiliza la API de Foursquare para obtener datos de lugares reales.

@author: Sergi
"""

from multiprocessing import Process, Queue
import socket
import argparse
import datetime
import uuid
import foursquare
import logging

from rdflib import Namespace, Graph, Literal, URIRef
from rdflib.namespace import RDF, RDFS, XSD
from flask import Flask, request, Response

from AgentUtil.FlaskServer import shutdown_server
from AgentUtil.Agent import Agent
from AgentUtil.ACLMessages import build_message, send_message, get_message_properties
from AgentUtil.OntoNamespaces import ACL, DSO
from APIKeys import FQCLIENT_ID, FQCLIENT_SECRET

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
    port = 9001
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

# Diccionario de ciudades y sus coordenadas (latitud, longitud)
CIUDADES_COORDS = {
    'Barcelona': {'ll': '41.3851,2.1734', 'country': 'España'},
    'Madrid': {'ll': '40.4168,3.7038', 'country': 'España'},
    'Valencia': {'ll': '39.4699,0.3763', 'country': 'España'},
    'Sevilla': {'ll': '37.3891,-5.9845', 'country': 'España'},
    'Paris': {'ll': '48.8566,2.3522', 'country': 'Francia'},
    'Roma': {'ll': '41.9028,12.4964', 'country': 'Italia'},
    'Londres': {'ll': '51.5074,-0.1278', 'country': 'Reino Unido'},
    'Berlin': {'ll': '52.5200,13.4050', 'country': 'Alemania'},
    'Amsterdam': {'ll': '52.3676,4.9041', 'country': 'Países Bajos'},
    'Lisboa': {'ll': '38.7223,-9.1393', 'country': 'Portugal'},
}

# Mapeo de tipos de actividades a categorías de Foursquare
TIPOS_ACTIVIDADES = {
    str(onto.Cultural): 'museo,arte,histórico,teatro,monumento',
    str(onto.Aventura): 'aventura,parque,deporte,senderismo',
    str(onto.Gastronomica): 'restaurante,bar,cafe,food',
    str(onto.Naturaleza): 'parque,jardín,naturaleza,montaña,playa'
}

# Inicializar cliente de Foursquare
try:
    foursquare_client = foursquare.Foursquare(client_id=FQCLIENT_ID, client_secret=FQCLIENT_SECRET)
    logger.info("Cliente Foursquare inicializado correctamente")
except Exception as e:
    logger.error(f"Error al inicializar el cliente Foursquare: {e}")
    foursquare_client = None

# Contador de mensajes
mss_cnt = 0

# Datos del Agente
AgenteActividades = Agent('AgenteActividades',
                       agn.AgenteActividades,
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

cola1 = Queue()

# Flask stuff
app = Flask(__name__)


@app.route("/comm")
def comunicacion():
    """
    Punto de entrada de comunicación para recibir peticiones de actividades
    """
    global dsgraph
    global mss_cnt

    message = request.args['content']
    gm = Graph()
    gm.parse(data=message, format='xml')
    
    msgdic = get_message_properties(gm)
    logger.debug(f"Recibido mensaje con performativa: {msgdic['performative']}")

    # Verificar si es una petición de actividades
    if msgdic['performative'] == ACL.request:
        # Buscar el contenido de la petición
        content = msgdic['content']
        # Buscar en el contenido una petición de actividades
        for s, p, o in gm.triples((None, RDF.type, onto.PeticionActividad)):
            ciudad_uri = None
            ciudad_nombre = None
            precio_max = None
            tipo_actividad = None
            
            # Extraer la ciudad de la petición
            for s1, p1, o1 in gm.triples((s, onto.comoRestriccionLocalidad, None)):
                ciudad_uri = o1
                # Obtener el nombre de la ciudad
                for s2, p2, o2 in gm.triples((o1, onto.NombreCiudad, None)):
                    ciudad_nombre = str(o2)
            
            # Extraer restricciones de precio si existen
            for s1, p1, o1 in gm.triples((s, onto.PrecioMax, None)):
                precio_max = float(o1)
            
            # Extraer tipo de actividad si existe
            for s1, p1, o1 in gm.triples((s, RDF.type, None)):
                if o1 != onto.PeticionActividad and str(o1) in TIPOS_ACTIVIDADES:
                    tipo_actividad = o1

            logger.info(f"Buscando actividades para: Ciudad={ciudad_nombre}, Tipo={tipo_actividad}, Precio Máximo={precio_max}")
            
            # Buscar actividades para la ciudad especificada y construir respuesta
            activities = buscar_actividades(ciudad_uri, ciudad_nombre, precio_max, tipo_actividad)
            respuesta = construir_respuesta_actividades(activities, content)
            
            # Enviar respuesta
            logger.info(f"Enviando respuesta con {len(activities)} actividades encontradas")
            return Response(respuesta, mimetype='text/xml')
    
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
    return "Parando Agente de Actividades"


def tidyup():
    """
    Acciones previas a parar el agente
    """
    global cola1
    cola1.put(0)


def obtener_actividades_foursquare(ciudad_nombre, tipo_actividad=None, precio_max=None, radio=4000):
    """
    Obtiene actividades de Foursquare para una ciudad
    
    :param ciudad_nombre: Nombre de la ciudad
    :param tipo_actividad: Tipo de actividad a buscar
    :param precio_max: Precio máximo
    :param radio: Radio de búsqueda en metros
    :return: Lista de actividades de Foursquare
    """
    if not foursquare_client or ciudad_nombre not in CIUDADES_COORDS:
        logger.warning(f"No se puede buscar en Foursquare: cliente={foursquare_client}, ciudad={ciudad_nombre}")
        return []
    
    activities = []
    
    try:
        # Determinar la consulta en función del tipo de actividad
        query = ''
        if tipo_actividad and str(tipo_actividad) in TIPOS_ACTIVIDADES:
            query = TIPOS_ACTIVIDADES[str(tipo_actividad)]
        
        # Realizar varias búsquedas si hay varios términos separados por comas
        queries = query.split(',') if query else ['']
        
        for q in queries:
            if q.strip():
                logger.debug(f"Consultando Foursquare para {ciudad_nombre}, query={q.strip()}")
                params = {
                    'll': CIUDADES_COORDS[ciudad_nombre]['ll'],
                    'intent': 'browse',
                    'radius': str(radio),
                    'query': q.strip(),
                    'limit': 10  # Limitar resultados
                }
                
                venues = foursquare_client.venues.search(params=params)
                
                # Procesar los resultados
                for venue in venues.get('venues', []):
                    # Estimar un precio (Foursquare no proporciona precios directamente)
                    # Usamos el atributo price.tier si está disponible
                    price_info = venue.get('price', {})
                    estimated_price = None
                    
                    if 'tier' in price_info:
                        # tier va de 1 (económico) a 4 (lujoso)
                        tier = price_info.get('tier', 2)
                        estimated_price = tier * 15  # Estimación simple: €15 por tier
                    
                    # Si hay restricción de precio y podemos estimarlo, filtramos
                    if precio_max is not None and estimated_price is not None and estimated_price > precio_max:
                        continue
                    
                    # Crear URI para el lugar
                    venue_uri = URIRef(f"http://foursquare.com/v/{venue['id']}")
                    
                    # Añadir a la lista de actividades
                    activities.append({
                        'uri': venue_uri,
                        'nombre': venue.get('name', 'Sin nombre'),
                        'tipo': tipo_actividad,
                        'precio': estimated_price,
                        'fuente': 'foursquare',
                        'lat': venue.get('location', {}).get('lat'),
                        'lng': venue.get('location', {}).get('lng'),
                        'direccion': venue.get('location', {}).get('address', 'Sin dirección'),
                        'id': venue['id']
                    })
        
        logger.info(f"Encontradas {len(activities)} actividades en Foursquare para {ciudad_nombre}")
        return activities
    
    except Exception as e:
        logger.error(f"Error al consultar Foursquare: {e}")
        return []


def buscar_actividades(ciudad_uri, ciudad_nombre, precio_max=None, tipo_actividad=None):
    """
    Busca actividades combinando la ontología y Foursquare
    
    :param ciudad_uri: URI de la ciudad en la ontología
    :param ciudad_nombre: Nombre de la ciudad para buscar en Foursquare
    :param precio_max: Precio máximo
    :param tipo_actividad: Tipo de actividad a buscar
    :return: Lista combinada de actividades
    """
    global dsgraph
    activities = []
    
    # 1. Buscar en la ontología
    try:
        logger.debug("Buscando actividades en la ontología")
        for s, p, o in dsgraph.triples((None, onto.sehaceEn, ciudad_uri)):
            # Verificar que es una actividad
            if (s, RDF.type, onto.Actividad) in dsgraph:
                # Si se especificó tipo, verificar que coincida
                if tipo_actividad and not (s, RDF.type, tipo_actividad) in dsgraph:
                    continue
                
                precio = None
                nombre = None
                
                # Obtener el precio de la actividad
                for s1, p1, o1 in dsgraph.triples((s, onto.Precio, None)):
                    precio = float(o1)
                
                # Obtener nombre de la actividad si está disponible
                for s1, p1, o1 in dsgraph.triples((s, RDFS.label, None)):
                    nombre = str(o1)
                
                # Verificar restricciones de precio
                if precio_max is None or (precio is not None and precio <= precio_max):
                    # Añadir la actividad a la lista
                    activities.append({
                        'uri': s,
                        'nombre': nombre if nombre else str(s).split('/')[-1],
                        'tipo': tipo_actividad,
                        'precio': precio,
                        'fuente': 'ontologia'
                    })
    except Exception as e:
        logger.error(f"Error al buscar en la ontología: {e}")
    
    # 2. Buscar en Foursquare si el nombre de la ciudad está en nuestro diccionario
    if ciudad_nombre in CIUDADES_COORDS:
        foursquare_activities = obtener_actividades_foursquare(
            ciudad_nombre, 
            tipo_actividad, 
            precio_max
        )
        activities.extend(foursquare_activities)
    else:
        logger.warning(f"No se encontraron coordenadas para la ciudad: {ciudad_nombre}")
    
    logger.info(f"Total de actividades encontradas: {len(activities)}")
    return activities


def construir_respuesta_actividades(activities, content_uri):
    """
    Construye un mensaje de respuesta con las actividades encontradas
    
    :param activities: Lista de actividades
    :param content_uri: URI del contenido de la petición original
    :return: Mensaje XML con la respuesta
    """
    global mss_cnt
    global AgenteActividades
    
    # Crear nuevo grafo para la respuesta
    g = Graph()
    
    # Definir los espacios de nombres
    g.bind('onto', onto)
    g.bind('rdf', RDF)
    g.bind('rdfs', RDFS)
    g.bind('xsd', XSD)
    
    # Crear la respuesta
    respuesta_id = URIRef(f'respuesta_actividades_{str(uuid.uuid4())}')
    g.add((respuesta_id, RDF.type, onto.RespuestaActividad))
    
    # Añadir las actividades a la respuesta
    for i, act in enumerate(activities):
        # Para las actividades de Foursquare, necesitamos crear nodos en la ontología
        if act.get('fuente') == 'foursquare':
            # Generar una URI para la actividad
            act_uri = act['uri']
            
            # Añadir información básica sobre la actividad
            g.add((act_uri, RDF.type, onto.Actividad))
            
            # Añadir el tipo específico si está disponible
            if 'tipo' in act and act['tipo']:
                g.add((act_uri, RDF.type, act['tipo']))
            
            # Añadir nombre
            g.add((act_uri, RDFS.label, Literal(act['nombre'])))
            
            # Añadir precio si está disponible
            if 'precio' in act and act['precio'] is not None:
                g.add((act_uri, onto.Precio, Literal(act['precio'], datatype=XSD.float)))
            
            # Añadir coordenadas
            if 'lat' in act and 'lng' in act and act['lat'] and act['lng']:
                coords = f"{act['lat']},{act['lng']}"
                g.add((act_uri, onto.Ubicacion, Literal(coords)))
            
            # Añadir dirección si está disponible
            if 'direccion' in act and act['direccion']:
                g.add((act_uri, RDFS.comment, Literal(act['direccion'])))
            
            # Marcar como proveniente de Foursquare
            g.add((act_uri, RDFS.isDefinedBy, Literal("Foursquare")))
        else:
            # Para actividades de la ontología, simplemente usamos la URI existente
            act_uri = act['uri']
        
        # Añadir la actividad a la respuesta
        g.add((respuesta_id, onto.formadoPorActividades, act_uri))
    
    # Añadir metadata adicional a la respuesta
    g.add((respuesta_id, RDFS.comment, Literal(f"Respuesta con {len(activities)} actividades")))
    g.add((respuesta_id, onto.importe, Literal(len(activities), datatype=XSD.integer)))
    
    # Construir el mensaje completo
    mss_cnt += 1
    return build_message(g, ACL.inform, 
                         sender=AgenteActividades.uri, 
                         receiver=content_uri, 
                         msgcnt=mss_cnt).serialize(format='xml')


def agentbehavior1(cola):
    """
    Comportamiento del agente - Registrarse en el directorio
    """
    global mss_cnt
    # Registrar el agente en el servicio de directorio
    gmess = Graph()
    gmess.bind('foaf', Namespace('http://xmlns.com/foaf/0.1/'))
    gmess.bind('dso', DSO)
    reg_obj = agn[f'AgenteActividades-{str(uuid.uuid4())}']
    gmess.add((reg_obj, RDF.type, DSO.Agent))
    gmess.add((reg_obj, DSO.hasName, Literal('AgenteActividades')))
    gmess.add((reg_obj, DSO.hasURI, URIRef(AgenteActividades.uri)))
    gmess.add((reg_obj, DSO.hasServiceURI, URIRef(AgenteActividades.address)))
    gmess.add((reg_obj, DSO.hasServiceURI, URIRef(AgenteActividades.stop)))

    # Lo metemos en el registro de servicios
    send_message(build_message(gmess, ACL.register,
                              sender=AgenteActividades.uri,
                              receiver=DirectoryAgent.uri,
                              content=reg_obj,
                              msgcnt=mss_cnt))
    mss_cnt += 1
    
    logger.info("Agente registrado en el directorio")
    
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
        logger.info('Agente de Actividades finalizado')
        
    except Exception as e:
        logger.error(f"Error al iniciar el agente: {e}")
        if 'ab1' in locals():
            ab1.terminate()
        print('Error en el Agente de Actividades')